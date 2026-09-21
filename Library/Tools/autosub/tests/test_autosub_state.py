import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Prefer the package path when tests run from the repository root. This avoids
# accidentally importing the older top-level autosub_batch_pro.py copy.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(CURRENT_DIR)
try:
    from Library.Tools.autosub import autosub, autosub_launcher
    from Library.Tools.autosub.autosub_batch_pro import (
        BatchEngine,
        SubTask,
        ResourceManager,
    )
except ModuleNotFoundError:
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)
    import autosub
    import autosub_launcher
    from autosub_batch_pro import BatchEngine, SubTask, ResourceManager

class TestAutoSubStateAndLauncher(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_parse_indices_empty(self):
        """Empty inputs should keep all indices."""
        res = autosub_launcher.parse_indices("", 10)
        self.assertEqual(res, set(range(1, 11)))

    def test_dashboard_completed_bar_stays_green(self):
        """A 100% stage must render green even while its task row is running."""
        from autosub_batch_pro import Dashboard

        dashboard = Dashboard(SimpleNamespace())
        completed = dashboard.make_bar(100.0, True, "bright_blue")
        active = dashboard.make_bar(44.5, True, "bright_blue")
        inactive = dashboard.make_bar(44.5, False, "bright_blue")

        self.assertIn("[green]", completed)
        self.assertNotIn("[bright_blue]", completed)
        self.assertIn("[bright_blue]", active)
        self.assertIn("[grey37]", inactive)

    def test_parse_indices_exclusion(self):
        """Standard input list should exclude specified indices."""
        res = autosub_launcher.parse_indices("1,3,5", 5)
        self.assertEqual(res, {2, 4})

    def test_parse_indices_inclusion(self):
        """Input list prefixed with '+' or 'i'/'I' should only keep specified indices."""
        res1 = autosub_launcher.parse_indices("+2,4", 5)
        self.assertEqual(res1, {2, 4})
        res2 = autosub_launcher.parse_indices("i1,3", 5)
        self.assertEqual(res2, {1, 3})

    def test_parse_indices_ranges(self):
        """Range patterns (e.g. 1-3) should be parsed correctly."""
        res1 = autosub_launcher.parse_indices("1-3", 5)  # Exclusion of 1,2,3 -> keep 4,5
        self.assertEqual(res1, {4, 5})
        res2 = autosub_launcher.parse_indices("+2-4", 5)  # Inclusion of 2,3,4 -> keep 2,3,4
        self.assertEqual(res2, {2, 3, 4})

    @patch("subprocess.Popen")
    def test_fetch_metadata_anonymous(self, mock_popen):
        """Anonymous metadata fetch should build the command and run subprocess."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b'{"id": "123", "title": "Test Vid"}', b'')
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        data = autosub_launcher.fetch_metadata("http://test-url.com")
        self.assertIsNotNone(data)
        self.assertEqual(data.get("id"), "123")
        self.assertEqual(data.get("title"), "Test Vid")

    @patch("subprocess.Popen")
    def test_fetch_metadata_with_browser(self, mock_popen):
        """Browser cookies fetch should add --cookies-from-browser option and execute command."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b'{"id": "456", "title": "Browser Vid"}', b'')
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        data = autosub_launcher.fetch_metadata("http://test-url.com", browser="chrome")
        self.assertIsNotNone(data)
        self.assertEqual(data.get("id"), "456")

        # Verify command argument contains chrome cookies
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertIn("--cookies-from-browser", cmd)
        self.assertIn("chrome", cmd)

    @patch("subprocess.Popen")
    @patch.object(autosub_launcher, "use_temp_cookies")
    def test_fetch_metadata_with_cookies(self, mock_temp_cookies, mock_popen):
        """Cookies.txt path fetch should copy cookies to temp file and run inside context manager."""
        mock_temp_cookies.return_value.__enter__.return_value = "/tmp/fake_temp_cookies.txt"
        
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b'{"id": "789", "title": "Cookies Vid"}', b'')
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        data = autosub_launcher.fetch_metadata("http://test-url.com", cookies_path="/path/to/cookies.txt")
        self.assertIsNotNone(data)
        self.assertEqual(data.get("id"), "789")

        # Verify command argument contains temp cookies file
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertIn("--cookies", cmd)
        self.assertIn("/tmp/fake_temp_cookies.txt", cmd)

    def test_disk_truth_fully_completed(self):
        """If workdir contains _hardsub.mp4 and .burn_complete, disk_truth should set status to Completed."""
        task = SubTask(
            uid=1,
            url="http://test.com",
            title="Video 1",
            vid_id="vid1",
            workdir=os.path.join(self.temp_dir, "task_1"),
            pcts={"DL": 0.0, "TR": 0.0, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0}
        )
        os.makedirs(task.workdir)
        
        # Create completed files
        with open(os.path.join(task.workdir, "video1_hardsub.mp4"), "w") as f:
            f.write("A" * 2 * 1024 * 1024)  # 2MB file
        with open(os.path.join(task.workdir, ".burn_complete"), "w") as f:
            f.write("1")

        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")
        with patch.object(BatchEngine, "get_duration", return_value="00:00:10"):
            engine.disk_truth(task)

        self.assertEqual(task.status, "完成")
        for stage, val in task.pcts.items():
            self.assertEqual(val, 100.0)

    def test_disk_truth_missing_burn_complete_auto_repair(self):
        """If workdir contains _hardsub.mp4 (>1MB) but lacks .burn_complete, it should auto-repair and set status to Completed."""
        task = SubTask(
            uid=2,
            url="http://test.com",
            title="Video 2",
            vid_id="vid2",
            workdir=os.path.join(self.temp_dir, "task_2"),
            pcts={"DL": 0.0, "TR": 0.0, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0}
        )
        os.makedirs(task.workdir)
        
        # Create completed hardsub video but NO .burn_complete
        with open(os.path.join(task.workdir, "video2_hardsub.mp4"), "w") as f:
            f.write("A" * 2 * 1024 * 1024)  # 2MB file

        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")
        with patch.object(BatchEngine, "get_duration", return_value="00:00:10"):
            engine.disk_truth(task)

        # It should have auto-created .burn_complete
        self.assertTrue(os.path.exists(os.path.join(task.workdir, ".burn_complete")))
        self.assertEqual(task.status, "完成")
        for stage, val in task.pcts.items():
            self.assertEqual(val, 100.0)

    def test_check_all_completed_empty(self):
        """None or empty state should return False."""
        self.assertFalse(autosub_launcher.check_all_completed(None))
        self.assertFalse(autosub_launcher.check_all_completed({}))

    def test_check_all_completed_launcher_format(self):
        """Launcher format (direct dict map) with all completed should return True."""
        state = {
            "1": {"status": "完成", "pcts": {"DL": 100.0, "TR": 100.0, "BR": 100.0}},
            "2": {"status": "压制中", "pcts": {"DL": 100.0, "TR": 100.0, "BR": 100.0}},  # BR >= 100.0 is also completed!
        }
        self.assertTrue(autosub_launcher.check_all_completed(state))

    def test_check_all_completed_pro_format(self):
        """Pro format (nested tasks key) with some incomplete should return False."""
        state = {
            "tasks": {
                "1": {"status": "完成", "pcts": {"DL": 100.0, "TR": 100.0, "BR": 100.0}},
                "2": {"status": "转录中", "pcts": {"DL": 100.0, "TR": 45.0, "BR": 0.0}},
            }
        }
        self.assertFalse(autosub_launcher.check_all_completed(state))

    def test_build_wechat_cmd_draft_and_public_flags(self):
        """WeChat upload defaults to a draft and only adds --public when requested."""
        workdir = os.path.join(self.temp_dir, "wechat_task")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as f:
            f.write("video")
        script = os.path.join(self.temp_dir, "wechat_upload.py")
        with open(script, "w") as f:
            f.write("# placeholder")

        with patch.object(autosub, "WECHAT_SCRIPT", script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 600):
            draft_cmd = autosub.build_wechat_cmd(workdir, title="Test title")
            public_cmd = autosub.build_wechat_cmd(workdir, title="Test title", public=True)

        self.assertIsNotNone(draft_cmd)
        self.assertIsNotNone(public_cmd)
        self.assertEqual(draft_cmd[:4], [sys.executable, "-u", script, hardsub])
        self.assertNotIn("--public", draft_cmd)
        self.assertIn("--public", public_cmd)

    def test_build_wechat_cmd_omits_title_for_subtitle_grounding(self):
        """Launcher/GUI lets the WeChat skill derive the title from subtitles."""
        workdir = os.path.join(self.temp_dir, "wechat_auto_title")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as f:
            f.write("video")
        script = os.path.join(self.temp_dir, "wechat_upload.py")
        with open(script, "w") as f:
            f.write("# placeholder")

        with patch.object(autosub, "WECHAT_SCRIPT", script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 600):
            cmd = autosub.build_wechat_cmd(workdir)

        self.assertEqual(cmd, [sys.executable, "-u", script, hardsub, "--headless"])

    def test_build_wechat_cmd_separates_cover_title_from_form_title(self):
        """A cover-only headline must not become the positional form title."""
        workdir = os.path.join(self.temp_dir, "wechat_cover_title")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as f:
            f.write("video")
        script = os.path.join(self.temp_dir, "wechat_upload.py")
        with open(script, "w") as f:
            f.write("# placeholder")

        with patch.object(autosub, "WECHAT_SCRIPT", script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 600):
            cmd = autosub.build_wechat_cmd(
                workdir,
                title="FDE的核心职能：抽象产品",
                cover_title="抽象产品",
            )

        self.assertEqual(cmd[:4], [sys.executable, "-u", script, hardsub])
        self.assertIn("FDE的核心职能：抽象产品", cmd)
        cover_index = cmd.index("--cover-title")
        self.assertEqual(cmd[cover_index + 1], "抽象产品")

    def test_pre_generated_metadata_disables_platform_auto_title(self):
        """Shared metadata must be passed through, not regenerated per platform."""
        workdir = os.path.join(self.temp_dir, "pre_generated_metadata")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        bili_script = os.path.join(self.temp_dir, "bili_upload.py")
        wechat_script = os.path.join(self.temp_dir, "wechat_upload.py")
        with open(bili_script, "w") as handle:
            handle.write("# placeholder")
        with open(wechat_script, "w") as handle:
            handle.write("# placeholder")

        with patch.object(autosub, "BILI_SCRIPT", bili_script), \
             patch.object(autosub, "BILI_PYTHON", sys.executable), \
             patch.object(autosub, "WECHAT_SCRIPT", wechat_script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 600):
            bili_cmd = autosub.build_bili_cmd(
                workdir,
                title="OpenAI研究员谈智能体",
                desc="共享简介",
                cover_title="智能体与自我改进",
            )
            wechat_cmd = autosub.build_wechat_cmd(
                workdir,
                title="OpenAI研究员谈智能体",
                desc="共享简介",
                cover_title="智能体与自我改进",
            )

        self.assertIn("--no_auto_title", bili_cmd)
        self.assertIn("--no_auto_title", wechat_cmd)
        self.assertIn("OpenAI研究员谈智能体", bili_cmd)
        self.assertIn("OpenAI研究员谈智能体", wechat_cmd)
        self.assertIn("共享简介", bili_cmd)
        self.assertIn("共享简介", wechat_cmd)

    def test_generate_upload_metadata_uses_llm_client_model_name(self):
        """The shared description and summary-title calls use the model name."""
        workdir = os.path.join(self.temp_dir, "metadata_llm_interface")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        llm_client = MagicMock()
        llm_client.generate_content.side_effect = [
            "共享简介",
            '{"title":"卡普谈Palantir数据整合，不做服务并坚持工程为本","evidence":["卡普谈Palantir数据整合"]}',
            '{"title":"卡普谈Palantir数据整合","evidence":["卡普谈Palantir数据整合"]}',
            '{"central_claim":"卡普谈Palantir数据整合","candidates":[{"title":"没有销售团队，却看到大规模采用","evidence":["我们没有销售团队，却看到了大规模的采用。"]}]}',
        ]
        subtitle_title = MagicMock()
        subtitle_title.build_subtitle_grounded_title.return_value = SimpleNamespace(
            title="Palantir是什么"
        )
        subtitle_title.read_subtitle_text.return_value = ("字幕内容", "subtitle.srt")
        subtitle_title.read_full_subtitle_text.return_value = ("完整字幕内容", "subtitle.srt")
        subtitle_title.parse_llm_title_response.return_value = (
            "卡普谈Palantir数据整合",
            ("卡普谈Palantir数据整合",),
        )
        subtitle_title.parse_llm_title_candidates.side_effect = [
            (
                "卡普谈Palantir数据整合，不做服务并坚持工程为本",
                (("卡普谈Palantir数据整合，不做服务并坚持工程为本", ("卡普谈Palantir数据整合",)),),
            ),
            (
                "卡普谈Palantir数据整合",
                (("卡普谈Palantir数据整合", ("卡普谈Palantir数据整合",)),),
            ),
            (
                "卡普谈Palantir数据整合",
                (("没有销售团队，却看到大规模采用", ("我们没有销售团队，却看到了大规模的采用。",)),),
            ),
        ]
        subtitle_title.validate_title_from_subtitle.return_value = (
            "Palantir把数据整合做成产品",
        )

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(
                 autosub,
                 "_load_llm_defaults",
                 return_value=("glm-5.2", "dashscope"),
             ), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: llm_client),
                 },
            ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["description"], "共享简介")
        self.assertEqual(metadata["title"], "卡普谈Palantir数据整合，不做服务并坚持工程为本")
        self.assertEqual(metadata["wechat_title"], "卡普谈Palantir数据整合")
        self.assertEqual(metadata["cover_title"], "没有销售团队，却看到大规模采用")
        self.assertEqual(llm_client.generate_content.call_count, 4)
        self.assertEqual(
            llm_client.generate_content.call_args.kwargs["model_name"],
            "glm-5.2",
        )
        self.assertNotIn("视频标题：", llm_client.generate_content.call_args.args[0])
        self.assertIn(
            "所有数字、金额、时间和公司关系必须逐字来自字幕",
            llm_client.generate_content.call_args_list[0].args[0],
        )
        self.assertIn(
            "不得换算数量级",
            llm_client.generate_content.call_args_list[0].args[0],
        )
        self.assertIn("视频简介：\n共享简介", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("优先 24-40 个字符", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("基于完整中文字幕和视频简介", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("列出访谈的 3-5 个主要内容", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("不要输出名词短语拼接", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("逐字复制字幕原文", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("B站表单标题：", llm_client.generate_content.call_args_list[2].args[0])
        self.assertIn("必须从B站标题的主张出发", llm_client.generate_content.call_args_list[2].args[0])
        plain_title_calls = [
            call for call in subtitle_title.build_subtitle_grounded_title.call_args_list
            if call.kwargs.get("title_style") == "plain"
        ]
        self.assertEqual(plain_title_calls, [])

    def test_generate_upload_metadata_falls_back_when_summary_title_is_bad(self):
        """Invalid summary compression must not bypass subtitle grounding."""
        workdir = os.path.join(self.temp_dir, "metadata_summary_fallback")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        llm_client = MagicMock()
        llm_client.generate_content.side_effect = [
            "共享简介",
            "bad response",
            "bad response",
            "bad response",
            "bad response",
            '{"title":"Palantir整合数据不做服务"}',
            '{"central_claim":"卡普谈Palantir数据整合","candidates":[{"title":"没有销售团队，却看到大规模采用","evidence":["我们没有销售团队，却看到了大规模的采用。"]}]}',
        ]
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.read_subtitle_text.return_value = ("字幕内容", "subtitle.srt")
        subtitle_title.read_full_subtitle_text.return_value = ("完整字幕内容", "subtitle.srt")
        subtitle_title.parse_llm_title_candidates.side_effect = [
            ("", ()),
            ("", ()),
            (
                "卡普谈Palantir数据整合",
                (("卡普谈Palantir数据整合", ("卡普谈Palantir数据整合",)),),
            ),
            (
                "卡普谈Palantir数据整合",
                (("没有销售团队，却看到大规模采用", ("我们没有销售团队，却看到了大规模的采用。",)),),
            ),
        ]
        subtitle_title.parse_llm_title_response.return_value = (
            "Palantir到底做什么",
            ("Palantir把数据整合做成产品",),
        )
        subtitle_title.build_subtitle_grounded_title.return_value = SimpleNamespace(
            title="卡普谈Palantir数据整合，不做服务"
        )

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: llm_client),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["title"], "卡普谈Palantir数据整合，不做服务")
        self.assertEqual(metadata["wechat_title"], "卡普谈Palantir数据整合")
        self.assertEqual(subtitle_title.build_subtitle_grounded_title.call_count, 1)
        fallback_call = subtitle_title.build_subtitle_grounded_title.call_args_list[0]
        self.assertIn("保留主体、谓语和最具体的核心结果", fallback_call.kwargs["platform_hint"])
        self.assertIn("保留受益者、问题或明确立场", fallback_call.kwargs["platform_hint"])
        self.assertIn("「主体+动作+对象」压缩片段", fallback_call.kwargs["platform_hint"])
        self.assertIn("B站表单标题没有微信的16字限制", fallback_call.kwargs["platform_hint"])
        self.assertIn("优先24-40字", fallback_call.kwargs["platform_hint"])
        self.assertIn("英文字母逐字符计数", fallback_call.kwargs["platform_hint"])
        self.assertIn("禁止使用省略号", fallback_call.kwargs["platform_hint"])
        self.assertIn("不要改写成概括句", fallback_call.kwargs["platform_hint"])
        self.assertIn("不得用「东西」「能力」等泛词", fallback_call.kwargs["platform_hint"])
        self.assertIn("优先删除「把/了/的/一个」等功能词和修饰语", fallback_call.kwargs["platform_hint"])
        self.assertIn("不得用「它/他们」替代主体", fallback_call.kwargs["platform_hint"])
        self.assertIn("视频简介第一句：共享简介", fallback_call.kwargs["platform_hint"])

    def test_bili_title_prompt_requires_a_complete_summary_sentence(self):
        """The Bilibili title must summarize the interview, not pile up nouns."""
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir数据整合产品化"))
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir把数据整合成产品"))
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir为企业做成产品"))
        self.assertFalse(autosub._shared_title_has_quality_issue("Palantir为企业整合数据"))
        self.assertFalse(autosub._shared_title_has_quality_issue("卡普谈Palantir数据整合"))
        self.assertFalse(autosub._shared_title_has_quality_issue("Palantir押注工程团队"))
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir押注解决重要问题"))

        llm_client = MagicMock()
        llm_client.generate_content.return_value = (
            '{"title":"卡普谈Palantir数据整合，不做服务并坚持工程为本"}'
        )
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.parse_llm_title_candidates.return_value = (
            "卡普谈Palantir数据整合，不做服务并坚持工程为本",
            (
                ("Palantir把数据整合成产品并坚持工程为本", ("卡普谈Palantir数据整合",)),
                (
                    "卡普谈Palantir数据整合，不做服务并坚持工程为本",
                    ("卡普谈Palantir数据整合",),
                ),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "卡普谈Palantir数据整合，不做服务",
        )

        with patch.dict(
            sys.modules,
            {"subtitle_title": subtitle_title},
        ):
            title = autosub._generate_title_from_description(
                "卡普谈Palantir如何把数据整合做成产品。",
                "卡普谈Palantir把数据整合做成产品。",
                "卡普谈Palantir把数据整合做成产品。",
                llm_client,
                "glm-5.2",
                48,
            )

        self.assertEqual(title, "卡普谈Palantir数据整合，不做服务并坚持工程为本")
        prompt = llm_client.generate_content.call_args.args[0]
        self.assertIn("完整中文字幕", prompt)
        self.assertIn("列出访谈的 3-5 个主要内容", prompt)
        self.assertIn("优先 24-40 个字符", prompt)
        self.assertIn("通常覆盖 3 个实质信息点", prompt)
        self.assertIn("参考下面公开B站标题的结构", prompt)
        self.assertIn("B站标题没有微信的 16 字限制", prompt)
        self.assertIn("谁做了什么、为谁或解决什么", prompt)
        self.assertIn("Palantir把数据整合成产品", prompt)
        self.assertIn("不要输出名词短语拼接", prompt)
        self.assertIn("不要机械堆关键词", prompt)
        self.assertIn("不要把全片主体缩小成单一客户群体", prompt)
        self.assertIn("不要用「大型企业」这类限定词缩窄范围", prompt)
        self.assertIn("核心业务优先写成具体动作和对象", prompt)
        self.assertIn("多个分句不要重复同一个核心宾语或谓语", prompt)
        self.assertIn("不要输出「押注解决」", prompt)
        self.assertIn("生成 5 个候选", prompt)
        subtitle_title.validate_title_from_subtitle.assert_called_once_with(
            "卡普谈Palantir数据整合，不做服务并坚持工程为本",
            "卡普谈Palantir把数据整合做成产品。",
            ("卡普谈Palantir数据整合",),
            min_chars=24,
            max_chars=48,
        )

    def test_shared_title_prefers_information_density_over_first_candidate(self):
        """A richer grounded claim should beat a safe single-action title."""
        llm_client = MagicMock()
        llm_client.generate_content.return_value = "{}"
        subtitle_title = MagicMock()
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir整合数据不做服务，打造产品",
            (
                (
                    "Palantir为企业整合数据，坚持产品化并服务客户",
                    ("我们干的，基本上就是整合数据。",),
                ),
                (
                    "Palantir整合数据不做服务，打造客户可用产品",
                    ("我们不想把它做成服务。",),
                ),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "我们不想把它做成服务。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._generate_title_from_description(
                "Palantir整合企业数据，不做服务。",
                "我们干的，基本上就是整合数据。我们不想把它做成服务。",
                "我们干的，基本上就是整合数据。我们不想把它做成服务。",
                llm_client,
                "glm-5.2",
                48,
            )

        self.assertEqual(title, "Palantir整合数据不做服务，打造客户可用产品")

    def test_bili_title_scoring_penalizes_repeated_objects(self):
        """A title should add claims, not repeat the same object in clauses."""
        self.assertLess(
            autosub._shared_title_score(
                "Palantir押注解决重要问题，不招销售，让工程师迭代产品解决问题",
                48,
            ),
            autosub._shared_title_score(
                "Palantir为客户整合数据，不做服务，坚持工程为本",
                48,
            ),
        )

    def test_wechat_title_is_derived_from_bili_title(self):
        """WeChat compression must preserve the Bilibili title's main claim."""
        llm_client = MagicMock()
        llm_client.generate_content.return_value = "{}"
        subtitle_title = MagicMock()
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir整合数据不做服务",
            (
                ("Palantir为企业整合数据", ("我们干的，基本上就是整合数据。",)),
                ("Palantir整合数据不做服务", ("我们不想把它做成服务。",)),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "我们不想把它做成服务。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._derive_wechat_title_from_bili_title(
                "Palantir为企业整合数据，不做服务，打造产品",
                "Palantir整合企业数据，不做服务。",
                "我们干的，基本上就是整合数据。我们不想把它做成服务。",
                llm_client,
                "glm-5.2",
                max_chars=16,
            )

        self.assertEqual(title, "Palantir整合数据不做服务")
        prompt = llm_client.generate_content.call_args.args[0]
        self.assertIn("把下面的B站表单标题压缩成视频号表单标题", prompt)
        self.assertIn("必须从B站标题的主张出发", prompt)
        self.assertIn("不得另起主题", prompt)
        self.assertIn("B站表单标题：\nPalantir为企业整合数据，不做服务，打造产品", prompt)
        self.assertIn("中文最多还能放 8 个字", prompt)
        self.assertIn("必须优先原样保留B站标题开头的主体", prompt)
        self.assertIn("长度允许时保留两个信息点", prompt)
        self.assertIn("主体+核心动作+对象+最有力的立场", prompt)
        self.assertEqual(
            autosub.normalize_bili_upload_title("Palantir为客户整合数据。"),
            "Palantir为客户整合数据",
        )
        self.assertEqual(
            subtitle_title.validate_title_from_subtitle.call_count,
            2,
        )
        subtitle_title.validate_title_from_subtitle.assert_any_call(
            "Palantir整合数据不做服务",
            "我们干的，基本上就是整合数据。我们不想把它做成服务。",
            ("我们不想把它做成服务。",),
            min_chars=12,
            max_chars=16,
        )

    def test_wechat_title_prefers_a_candidate_that_keeps_the_subject(self):
        """A subjectless compression is invalid when the subject fits."""
        self.assertEqual(
            autosub._extract_bili_title_subject(
                "Palantir押注工程团队解决长期问题"
            ),
            "Palantir",
        )
        self.assertEqual(
            autosub._extract_bili_title_subject("OpenAI研究员谈智能体边界"),
            "OpenAI研究员",
        )
        self.assertFalse(
            autosub._wechat_title_preserves_bili_subject(
                "让工程师解决长期问题",
                "Palantir押注工程团队解决长期问题",
                16,
            )
        )
        self.assertEqual(
            autosub._compress_bili_title_for_wechat(
                "Palantir为工程师迭代核心问题，死活不招销售与市场团队",
                16,
            ),
            "Palantir不招销售团队",
        )
        self.assertEqual(
            autosub._compress_bili_title_for_wechat(
                "Palantir为企业和政府解决数据隐私问题，押注工程师团队不招销售",
                16,
                description="Palantir将各类数据源整合，并保护隐私。",
            ),
            "Palantir整合数据不招销售",
        )

        llm_client = MagicMock()
        llm_client.generate_content.return_value = "{}"
        subtitle_title = MagicMock()
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir押注长期问题",
            (
                ("让工程师解决长期问题", ("我们押注工程团队。",)),
                ("Palantir押注长期问题", ("我们押注工程团队。",)),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "我们押注工程团队。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._derive_wechat_title_from_bili_title(
                "Palantir押注工程团队解决三年后依然重要的问题",
                "Palantir押注工程团队解决长期问题。",
                "我们押注工程团队，解决三年后依然重要的问题。",
                llm_client,
                "glm-5.2",
                max_chars=16,
            )

        self.assertEqual(title, "Palantir押注长期问题")

    def test_structured_title_fallback_assembles_a_budgeted_sentence(self):
        """The structured fallback avoids model miscounts of full titles."""
        llm_client = MagicMock()
        llm_client.generate_content.return_value = (
            '{"candidates":[{"subject":"Palantir","beneficiary":"企业",'
            '"action":"整合","object":"数据","second_claim":"不做服务，坚持工程为本",'
            '"evidence":["我们干的，基本上就是整合数据。"]}]}'
        )
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.validate_title_from_subtitle.return_value = (
            "我们干的，基本上就是整合数据。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._generate_structured_title_from_description(
                "Palantir为企业整合数据。",
                "我们干的，基本上就是整合数据。我们为企业整合数据。",
                llm_client,
                "glm-5.2",
                48,
            )

        self.assertEqual(title, "Palantir为企业整合数据，不做服务，坚持工程为本")
        prompt = llm_client.generate_content.call_args.args[0]
        self.assertIn("输出5个结构化候选", prompt)
        self.assertIn("有 second_claim 时追加「，second_claim」", prompt)
        self.assertIn("第一候选必须覆盖核心业务、产品或具体做法", prompt)
        self.assertIn("不得用「产品」「价值」「问题」这类泛词充当 object", prompt)
        subtitle_title.validate_title_from_subtitle.assert_called_once_with(
            "Palantir为企业整合数据，不做服务，坚持工程为本",
            "我们干的，基本上就是整合数据。我们为企业整合数据。",
            ("我们干的，基本上就是整合数据。",),
            min_chars=24,
            max_chars=48,
        )

    def test_cover_title_prompt_scans_full_chinese_subtitle(self):
        """Cover titles must scan the full transcript and prefer exact hooks."""
        self.assertTrue(autosub._cover_title_has_quality_issue("帕罗奥图马戏团"))
        self.assertTrue(autosub._cover_title_has_quality_issue("就像在帕罗奥图正中央开马戏团"))
        self.assertFalse(autosub._cover_title_has_quality_issue("人脑对付善变对手"))
        self.assertFalse(
            autosub._cover_title_has_quality_issue("我们就像在帕罗奥图正中央开马戏团")
        )
        self.assertEqual(
            autosub._normalize_cover_simile_title(
                "我们在帕罗奥图正中央开马戏团",
                "就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。",
            ),
            "我们就像在帕罗奥图正中央开马戏团",
        )
        self.assertEqual(
            autosub._normalize_cover_simile_title(
                "我们带着工程师在帕罗奥图开马戏团",
                "就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。",
            ),
            "我们就像在帕罗奥图正中央开马戏团",
        )
        self.assertEqual(
            autosub._normalize_cover_simile_title(
                "我们没有销售团队",
                "就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。",
            ),
            "我们没有销售团队",
        )
        full_subtitle = (
            "我们没有销售团队，却看到了大规模的采用。\n"
            "就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。"
        )
        llm_client = MagicMock()
        llm_client.generate_content.return_value = "{}"
        subtitle_title = MagicMock()
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir没有销售团队",
            (
                ("没有销售团队，却看到大规模采用", ("我们没有销售团队，却看到了大规模的采用。",)),
                ("我们像在帕罗奥图正中央开马戏团", ("就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。",)),
            ),
        )
        subtitle_title.validate_title_from_subtitle.side_effect = lambda *args, **kwargs: args[2]

        with patch.dict(
            sys.modules,
            {"subtitle_title": subtitle_title},
        ):
            cover_title = autosub._generate_cover_title_from_full_subtitle(
                full_subtitle,
                llm_client,
                "glm-5.2",
            )

        self.assertEqual(cover_title, "我们就像在帕罗奥图正中央开马戏团")
        prompt = llm_client.generate_content.call_args.args[0]
        self.assertIn("通读下面的完整中文字幕", prompt)
        self.assertIn("不要只看开头、结尾或采样片段", prompt)
        self.assertIn("按话题感、画面感", prompt)
        self.assertIn("不要把普通商务反差或抽象结论当成最强钩子", prompt)
        self.assertIn("主语、谓语和比喻/冲突对象要保留", prompt)
        self.assertIn("不得以「就像」「在」「需要」「带着」这类无主语成分开头", prompt)
        self.assertIn("优先 14-18 个字符", prompt)
        self.assertIn("不得保留「带着一帮工程师」这类可删修饰", prompt)
        self.assertIn("「正中央」这类承担画面强度的定位词必须保留", prompt)
        self.assertIn("「我们」「的」「了」若承担语法或语境作用必须保留", prompt)
        self.assertIn("字幕说「就有点像」时，标题必须压缩成「就像」", prompt)
        self.assertIn("不得输出无语境的反常识短语", prompt)
        self.assertIn("克制复述", prompt)
        self.assertIn("市井口语、夸张营销或AI套话", prompt)

    def test_cover_validation_allows_minimal_simile_compression(self):
        """A grounded simile may be tightened without inventing facts."""
        from subtitle_title import validate_title_from_subtitle

        evidence = validate_title_from_subtitle(
            "我们就像在帕罗奥图正中央开马戏团",
            "我们干的，基本上就是整合数据。\n"
            "就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。",
            ["就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。"],
            min_chars=12,
            max_chars=48,
        )
        self.assertEqual(
            evidence,
            ("就有点像带着一帮工程师，在帕罗奥图正中央开马戏团。",),
        )

    def test_generate_upload_metadata_passes_chinese_lines_to_cover_generator(self):
        """Metadata generation should use every Chinese line, not sampled text."""
        workdir = os.path.join(self.temp_dir, "metadata_full_chinese")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        llm_client = MagicMock()
        llm_client.generate_content.return_value = "共享简介"
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            "中文第一行\nEnglish line\n中文第二行",
            "subtitle.srt",
        )

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(
                 autosub,
                 "_generate_title_from_description",
                 return_value="卡普谈Palantir数据整合，不做服务",
             ) as mock_title, \
             patch.object(
                 autosub,
                 "_derive_wechat_title_from_bili_title",
                 return_value="Palantir整合数据不做服务",
             ) as mock_wechat_title, \
             patch.object(
                 autosub,
                 "_generate_cover_title_from_full_subtitle",
                 return_value="没有销售团队，却看到大规模采用",
             ) as mock_cover, \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: llm_client),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["title"], "卡普谈Palantir数据整合，不做服务")
        self.assertEqual(metadata["wechat_title"], "Palantir整合数据不做服务")
        self.assertEqual(metadata["cover_title"], "没有销售团队，却看到大规模采用")
        self.assertEqual(mock_title.call_args.args[1], "中文第一行\n中文第二行")
        self.assertEqual(mock_wechat_title.call_args.args[0], "卡普谈Palantir数据整合，不做服务")
        self.assertEqual(mock_wechat_title.call_args.args[2], "中文第一行\n中文第二行")
        self.assertEqual(mock_cover.call_args.args[0], "中文第一行\n中文第二行")

    def test_build_bili_cmd_separates_cover_title_from_form_title(self):
        """A Bilibili cover-only headline must not replace the form title."""
        workdir = os.path.join(self.temp_dir, "bili_cover_title")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as f:
            f.write("video")
        script = os.path.join(self.temp_dir, "bili_upload.py")
        with open(script, "w") as f:
            f.write("# placeholder")
        python = sys.executable

        with patch.object(autosub, "BILI_SCRIPT", script), \
             patch.object(autosub, "BILI_PYTHON", python):
            cmd = autosub.build_bili_cmd(
                workdir,
                title="FDE的核心职能：抽象产品",
                cover_title="抽象产品",
            )

        self.assertEqual(cmd[:5], [python, "-u", script, hardsub, "FDE的核心职能：抽象产品"])
        cover_index = cmd.index("--cover-title")
        self.assertEqual(cmd[cover_index + 1], "抽象产品")

    def test_trigger_bili_upload_runs_built_command(self):
        """The Bilibili trigger must execute the uploader, not just log success."""
        workdir = os.path.join(self.temp_dir, "bili_trigger")
        os.makedirs(workdir)
        cmd = [sys.executable, "bili_upload.py", "video_hardsub.mp4"]

        with patch.object(autosub, "build_bili_cmd", return_value=cmd), \
             patch.object(autosub, "run_bili_cmd", return_value=True) as mock_run:
            ok = autosub.trigger_bili_upload(workdir)

        self.assertTrue(ok)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args, (cmd,))
        self.assertIsNone(mock_run.call_args.kwargs["on_progress"])
        self.assertIsNotNone(mock_run.call_args.kwargs["on_log"])
        self.assertIsNone(mock_run.call_args.kwargs["abort_flag"])

    def test_post_processing_uploads_run_gd_bili_wechat_in_parallel(self):
        """After burn, GD/BI/WX start together and BI/WX share metadata."""
        workdir = os.path.join(self.temp_dir, "post_uploads")
        os.makedirs(workdir)
        metadata = {
            "title": "OpenAI研究员谈智能体和自我改进的边界",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体与自我改进",
        }
        barrier = threading.Barrier(3, timeout=2)
        calls = []

        def fake_sync(*_args, **_kwargs):
            calls.append(("GD", threading.current_thread().name))
            barrier.wait()
            return True

        def fake_bili(_workdir, **kwargs):
            calls.append(("BI", kwargs))
            barrier.wait()
            return True

        def fake_wechat(_workdir, **kwargs):
            calls.append(("WX", kwargs))
            barrier.wait()
            return True

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata), \
             patch.object(autosub, "trigger_sync", side_effect=fake_sync), \
             patch.object(autosub, "trigger_bili_upload", side_effect=fake_bili), \
             patch.object(autosub, "trigger_wechat_upload", side_effect=fake_wechat):
            autosub.run_post_processing_uploads(
                workdir,
                sync_gdrive="drive-id",
                bili_upload=True,
                wechat_upload=True,
                headless=True,
                wechat_public=True,
            )

        self.assertFalse(barrier.broken)
        self.assertEqual({call[0] for call in calls}, {"GD", "BI", "WX"})
        bili_kwargs = next(call[1] for call in calls if call[0] == "BI")
        wechat_kwargs = next(call[1] for call in calls if call[0] == "WX")
        self.assertEqual(bili_kwargs["title"], metadata["title"])
        self.assertEqual(bili_kwargs["desc"], metadata["description"])
        self.assertEqual(bili_kwargs["cover_title"], metadata["cover_title"])
        self.assertEqual(wechat_kwargs["title"], metadata["wechat_title"])
        self.assertEqual(wechat_kwargs["desc"], metadata["description"])
        self.assertEqual(wechat_kwargs["cover_title"], metadata["cover_title"])
        self.assertTrue(wechat_kwargs["public"])
        for marker in (".synced", ".bili_uploaded", ".wechat_uploaded"):
            self.assertTrue(os.path.exists(os.path.join(workdir, marker)))

    def test_parallel_uploads_generate_title_at_wechat_limit(self):
        """BI/WX must use one title short enough for WeChat, not truncate only WX."""
        workdir = os.path.join(self.temp_dir, "shared_title_limit")
        os.makedirs(workdir)
        metadata = {
            "title": "OpenAI研究员谈智能体和自我改进的边界",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体与自我改进",
        }
        calls = []

        def fake_bili(_workdir, **kwargs):
            calls.append(("BI", kwargs))
            return True

        def fake_wechat(_workdir, **kwargs):
            calls.append(("WX", kwargs))
            return True

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata) as mock_meta, \
             patch.object(autosub, "trigger_bili_upload", side_effect=fake_bili), \
             patch.object(autosub, "trigger_wechat_upload", side_effect=fake_wechat):
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
                wechat_upload=True,
            )

        mock_meta.assert_called_once_with(
            workdir,
            bili_title_max_chars=48,
            wechat_title_max_chars=16,
        )
        bili_kwargs = next(call[1] for call in calls if call[0] == "BI")
        wechat_kwargs = next(call[1] for call in calls if call[0] == "WX")
        self.assertEqual(bili_kwargs["title"], metadata["title"])
        self.assertEqual(wechat_kwargs["title"], metadata["wechat_title"])
        self.assertLessEqual(len(wechat_kwargs["title"]), 16)

    def test_shared_title_is_rejected_instead_of_silently_truncated(self):
        """A 19-character title must stop shared uploads, not cut to 16."""
        title = "OpenAI研究员谈智能体和自我改进"
        self.assertEqual(
            autosub.normalize_shared_upload_title("OpenAI研究员谈智能体"),
            "OpenAI研究员谈智能体",
        )
        self.assertEqual(autosub.normalize_shared_upload_title(title), "")

    def test_parallel_uploads_stop_when_shared_metadata_is_incomplete(self):
        """A platform must not silently generate its own substitute metadata."""
        workdir = os.path.join(self.temp_dir, "incomplete_metadata")
        os.makedirs(workdir)
        metadata = {
            "title": "OpenAI研究员谈智能体和自我改进的边界",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "",
            "cover_title": "智能体与自我改进",
        }

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata), \
             patch.object(autosub, "trigger_bili_upload") as mock_bili, \
             patch.object(autosub, "trigger_wechat_upload") as mock_wechat:
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
                wechat_upload=True,
            )

        mock_bili.assert_not_called()
        mock_wechat.assert_not_called()
        for marker in (".bili_uploaded", ".wechat_uploaded"):
            self.assertFalse(os.path.exists(os.path.join(workdir, marker)))

    def test_parallel_uploads_stop_when_shared_metadata_generation_fails(self):
        """A total metadata failure must not fall back to per-platform generation."""
        workdir = os.path.join(self.temp_dir, "metadata_generation_failed")
        os.makedirs(workdir)

        with patch.object(autosub, "generate_upload_metadata", return_value=None), \
             patch.object(autosub, "trigger_bili_upload") as mock_bili, \
             patch.object(autosub, "trigger_wechat_upload") as mock_wechat:
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
                wechat_upload=True,
            )

        mock_bili.assert_not_called()
        mock_wechat.assert_not_called()
        for marker in (".bili_uploaded", ".wechat_uploaded"):
            self.assertFalse(os.path.exists(os.path.join(workdir, marker)))

    def test_parallel_uploads_reject_different_explicit_cover_titles(self):
        """Explicit per-platform cover titles must not diverge in shared mode."""
        workdir = os.path.join(self.temp_dir, "different_covers")
        os.makedirs(workdir)

        with patch.object(autosub, "generate_upload_metadata") as mock_meta, \
             patch.object(autosub, "trigger_bili_upload") as mock_bili, \
             patch.object(autosub, "trigger_wechat_upload") as mock_wechat:
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
                wechat_upload=True,
                bili_cover_title="智能体与自我改进",
                wechat_cover_title="智能体会自我改进",
            )

        mock_meta.assert_not_called()
        mock_bili.assert_not_called()
        mock_wechat.assert_not_called()

    def test_build_wechat_title_caps_at_platform_limit(self):
        """WeChat's short-title field rejects long titles and unsupported punctuation."""
        hardsub = os.path.join(self.temp_dir, "A_long-title!_that_exceeds_the_limit_hardsub.mp4")
        title = autosub.build_wechat_title(hardsub)
        self.assertLessEqual(len(title), 16)
        self.assertNotIn("_", title)
        self.assertNotIn("-", title)

    def test_confirm_wechat_upload_defaults_to_draft(self):
        """WeChat upload confirmation defaults to saving a draft."""
        state = {}
        with patch.object(autosub_launcher.Prompt, "ask", return_value="2") as mock_ask:
            enabled, public = autosub_launcher.confirm_wechat_upload(state)

        self.assertTrue(enabled)
        self.assertFalse(public)
        self.assertTrue(state["wechat"])
        self.assertFalse(state["wechat_public"])
        mock_ask.assert_called_once()
        self.assertEqual(mock_ask.call_args.kwargs.get("default"), "2")

    def test_confirm_wechat_upload_choice_mapping(self):
        """The single WeChat prompt maps choices to upload and publish flags."""
        for choice, expected_enabled, expected_public in (
            ("1", False, False),
            ("2", True, False),
            ("3", True, True),
        ):
            with self.subTest(choice=choice):
                state = {}
                with patch.object(autosub_launcher.Prompt, "ask", return_value=choice):
                    enabled, public = autosub_launcher.confirm_wechat_upload(state)

                self.assertEqual((enabled, public), (expected_enabled, expected_public))
                self.assertEqual((state["wechat"], state["wechat_public"]), (expected_enabled, expected_public))

    def test_confirm_cookies_accepts_browser_name_without_prompt(self):
        """Browser cookie names bypass cookies.txt validation and prompts."""
        args = SimpleNamespace(cookies="chrome", input="https://example.com/video")
        self.assertEqual(autosub.confirm_cookies(args), "chrome")

    def test_state_preserves_browser_cookie_source(self):
        """Batch state saves and restores the selected browser cookie source."""
        state_file = os.path.join(self.temp_dir, "batch_state.json")
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.cookie_path = "chrome"
        engine.save_state()

        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state.get("browser"), "chrome")

        resumed = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        resumed.cookie_path = "edge"
        resumed.load_state()
        self.assertEqual(resumed.cookie_path, "chrome")

    def test_check_all_completed_requires_wechat_progress(self):
        """A WeChat-enabled task is incomplete until WX reaches 100%."""
        pcts = {
            "DL": 100.0,
            "TR": 100.0,
            "TL": 100.0,
            "MR": 100.0,
            "BR": 100.0,
            "GD": 100.0,
            "BI": 100.0,
            "WX": 0.0,
        }
        state = {
            "wechat": True,
            "1": {"status": "完成", "pcts": pcts},
        }
        self.assertFalse(autosub_launcher.check_all_completed(state))

        pcts["WX"] = 100.0
        self.assertTrue(autosub_launcher.check_all_completed(state))

    def test_load_state_recovers_wechat_marker(self):
        """An existing .wechat_uploaded marker restores WX to 100%."""
        project_dir = os.path.join(self.temp_dir, "project")
        task_dir = os.path.join(project_dir, "task_1")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, ".wechat_uploaded"), "w") as f:
            f.write("1")

        state_file = os.path.join(project_dir, "batch_state.json")
        state = {
            "1": {
                "uid": 1,
                "url": "http://test.com",
                "vid_id": "vid1",
                "title": "Video 1",
                "status": "上传中 (WX)",
                "pcts": {
                    "DL": 100.0,
                    "TR": 100.0,
                    "TL": 100.0,
                    "MR": 100.0,
                    "BR": 100.0,
                    "GD": 100.0,
                    "BI": 100.0,
                    "WX": 0.0,
                },
                "workdir": task_dir,
            }
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.do_wechat = True
        engine.load_state()

        task = engine.task_map[1]
        self.assertEqual(task.pcts["WX"], 100.0)
        self.assertEqual(task.status, "完成")
        self.assertTrue(engine.do_wechat)

if __name__ == "__main__":
    unittest.main()
