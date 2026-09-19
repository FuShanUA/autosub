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
            "title": "OpenAI研究员谈智能体",
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
        self.assertEqual(wechat_kwargs["title"], metadata["title"])
        self.assertEqual(wechat_kwargs["desc"], metadata["description"])
        self.assertEqual(wechat_kwargs["cover_title"], metadata["cover_title"])
        self.assertTrue(wechat_kwargs["public"])
        for marker in (".synced", ".bili_uploaded", ".wechat_uploaded"):
            self.assertTrue(os.path.exists(os.path.join(workdir, marker)))

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
