import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

# Prefer the package path when tests run from the repository root. This avoids
# accidentally importing the older top-level autosub_batch_pro.py copy.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(CURRENT_DIR)
try:
    from Library.Tools.autosub import autosub, autosub_launcher
    from Library.Tools.autosub import autosub_batch_pro as batch_pro
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
    import autosub_batch_pro as batch_pro
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
        queued_active = dashboard.make_bar(12.7, True, "grey37")
        inactive = dashboard.make_bar(44.5, False, "bright_blue")

        self.assertIn("[green]", completed)
        self.assertNotIn("[bright_blue]", completed)
        self.assertIn("[bright_blue]", active)
        self.assertIn("[bright_blue]", queued_active)
        self.assertNotIn("[grey37]", queued_active)
        self.assertIn("[grey37]", inactive)

    def test_reset_skips_stages_with_completion_markers(self):
        """R must not clear GD when its completion marker exists."""
        workdir = os.path.join(self.temp_dir, "reset_marker_truth")
        os.makedirs(workdir)
        for marker in (".burn_complete", ".synced"):
            with open(os.path.join(workdir, marker), "w", encoding="utf-8") as handle:
                handle.write("1")

        task = SubTask(
            uid=2,
            url="http://test.com",
            title="Video 2",
            status="排队中 (GD)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 0.0,
                "BI": 12.7,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = True
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        self.assertEqual(engine.reset_task(task), "BI")
        self.assertEqual(task.status, "排队中 (BI)")
        self.assertEqual(task.pcts["GD"], 100.0)
        self.assertEqual(task.pcts["BI"], 0.0)
        self.assertEqual(task.pcts["WX"], 0.0)
        self.assertTrue(os.path.exists(os.path.join(workdir, ".synced")))

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

    def test_find_local_video_files_filters_outputs(self):
        """Direct folder mode should ignore hardsub and partial downloads."""
        source = os.path.join(self.temp_dir, "source")
        os.makedirs(source)
        for name in ("b.mkv", "a.mp4", "c.webm", "d.mov", "e.ts"):
            with open(os.path.join(source, name), "wb") as handle:
                handle.write(b"video")
        for name in ("done_hardsub.mp4", "partial.mp4.part", "stream.f123.mp4"):
            with open(os.path.join(source, name), "wb") as handle:
                handle.write(b"output")
        os.makedirs(os.path.join(source, "nested"))

        videos = autosub_launcher.find_local_video_files(source)
        self.assertEqual(
            [os.path.basename(path) for path in videos],
            ["a.mp4", "b.mkv", "c.webm", "d.mov", "e.ts"],
        )

    def test_build_local_video_tasks_creates_isolated_workspaces(self):
        """Each direct video gets its own workspace without copying the file."""
        source = os.path.join(self.temp_dir, "source")
        os.makedirs(source)
        video_paths = []
        for name in ("one.mp4", "two.mkv"):
            path = os.path.join(source, name)
            with open(path, "wb") as handle:
                handle.write(b"video")
            video_paths.append(path)

        state = autosub_launcher.build_local_video_tasks(source, video_paths)
        tasks = [value for key, value in state.items() if str(key).isdigit()]
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            workdir = task["workdir"]
            linked = os.path.join(workdir, os.path.basename(task["local_video"]))
            self.assertTrue(os.path.exists(linked))
            self.assertTrue(os.path.samefile(linked, task["local_video"]))
            self.assertEqual(task["status"], "排队中 (TR)")
            self.assertEqual(task["pcts"]["DL"], 100.0)

    @patch.object(autosub_launcher, "launch_engine")
    @patch.object(autosub_launcher, "onboard_model_selection")
    @patch.object(autosub_launcher, "confirm_wechat_upload", return_value=(False, False))
    @patch.object(autosub_launcher.Prompt, "ask", return_value="high")
    @patch.object(autosub_launcher.Confirm, "ask", side_effect=[False, False])
    def test_process_local_folder_mode_seeds_engine_state(
        self, mock_confirm, mock_prompt, mock_wechat, mock_model, mock_launch
    ):
        """A direct-video folder becomes a resumable batch project."""
        source = os.path.join(self.temp_dir, "My Videos")
        os.makedirs(source)
        for name in ("one.mp4", "two.mp4"):
            with open(os.path.join(source, name), "wb") as handle:
                handle.write(b"video")

        registry_path = os.path.join(self.temp_dir, "projects.json")
        with patch.object(autosub_launcher, "PROJECT_REGISTRY_PATH", registry_path):
            autosub_launcher.process_local_folder_mode(
                source, autosub_launcher.find_local_video_files(source)
            )
            with open(registry_path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), [os.path.abspath(source)])

        with open(os.path.join(source, "batch_state.json"), "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(len([key for key in state if str(key).isdigit()]), 2)
        self.assertFalse(state["gdsync"])
        self.assertFalse(state["bili"])
        self.assertFalse(state["wechat"])

        cmd = mock_launch.call_args[0][0]
        self.assertIn(os.path.dirname(source), cmd)
        self.assertIn("My Videos", cmd)
        self.assertIn("--quality", cmd)
        self.assertNotIn("--gdsync", cmd)
        self.assertNotIn("--bili-upload", cmd)
        self.assertNotIn("--wechat-upload", cmd)

    def test_find_uncompleted_projects_includes_local_locations(self):
        """Restart discovery covers default, indexed, and legacy local projects."""
        default_root = os.path.join(self.temp_dir, "default")
        indexed_root = os.path.join(self.temp_dir, "indexed")
        legacy_root = os.path.join(self.temp_dir, "legacy")
        for root in (default_root, indexed_root, legacy_root):
            os.makedirs(root)

        def write_state(project_name, status):
            project_dir = os.path.join(root, project_name)
            os.makedirs(project_dir, exist_ok=True)
            pcts = (
                {"BR": 100.0, "GD": 100.0, "BI": 100.0, "WX": 100.0}
                if status == "完成"
                else {"BR": 0.0, "GD": 0.0, "BI": 0.0, "WX": 0.0}
            )
            state = {
                "1": {
                    "status": status,
                    "pcts": pcts,
                }
            }
            with open(os.path.join(project_dir, "batch_state.json"), "w", encoding="utf-8") as handle:
                json.dump(state, handle)

        write_state("DefaultProject", "排队中")
        write_state("CompletedProject", "完成")
        write_state("IndexedProject", "排队中")
        write_state("LegacyProject", "排队中")

        registry_path = os.path.join(self.temp_dir, "registry.json")
        with open(registry_path, "w", encoding="utf-8") as handle:
            json.dump([os.path.join(indexed_root, "IndexedProject")], handle)

        with patch.object(autosub_launcher, "DEFAULT_OUTPUT_ROOT", default_root), patch.object(
            autosub_launcher, "PROJECT_REGISTRY_PATH", registry_path
        ), patch.object(
            autosub_launcher, "LEGACY_PROJECT_SCAN_ROOTS", (legacy_root,)
        ):
            found = autosub_launcher.find_uncompleted_projects()

        self.assertEqual(
            {name for name, _, _ in found},
            {"DefaultProject", "IndexedProject", "LegacyProject"},
        )

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

    def test_check_all_completed_treats_wechat_limit_as_not_success(self):
        """A WeChat duration-limit terminal state is not a completed upload."""
        state = {
            "wechat": True,
            "1": {
                "status": "超限 (WX)",
                "pcts": {
                    "BR": 100.0,
                    "GD": 100.0,
                    "BI": 100.0,
                    "WX": 0.0,
                },
            },
        }
        self.assertFalse(autosub_launcher.check_all_completed(state))

    def test_disk_truth_preserves_wechat_limit_terminal_state(self):
        """Physical state repair must not turn a WX limit skip back into a queue."""
        task = SubTask(
            uid=4,
            url="http://test.com",
            title="Video 4",
            status="超限 (WX)",
            workdir=os.path.join(self.temp_dir, "task_4"),
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "BI": 100.0,
                "WX": 0.0,
            },
            error="视频时长 10.2 分钟，超过视频号 10 分钟限制",
        )
        os.makedirs(task.workdir)
        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")

        engine.disk_truth(task)

        self.assertEqual(task.status, "超限 (WX)")
        self.assertIn("超过视频号 10 分钟限制", task.error)
        self.assertEqual(task.pcts["WX"], 0.0)

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

    def test_wechat_duration_no_longer_blocks_ten_minute_uploader(self):
        """The launcher builds commands for videos longer than ten minutes."""
        workdir = os.path.join(self.temp_dir, "wechat_duration_boundary")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        script = os.path.join(self.temp_dir, "wechat_upload.py")
        with open(script, "w") as handle:
            handle.write("# placeholder")

        with patch.object(autosub, "WECHAT_SCRIPT", script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 600):
            self.assertIsNotNone(autosub.build_wechat_cmd(workdir))

        with patch.object(autosub, "WECHAT_SCRIPT", script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 601):
            self.assertIsNotNone(autosub.build_wechat_cmd(workdir))

    def test_wechat_duration_skip_reason_has_no_upper_bound(self):
        """The shared duration check no longer enforces a ten-minute cap."""
        self.assertIsNone(autosub.get_wechat_duration_skip_reason(600))
        self.assertIsNone(autosub.get_wechat_duration_skip_reason(601))
        self.assertIsNone(autosub.get_wechat_duration_skip_reason(3600))

    def test_worker_clears_old_wechat_limit_before_upload_metadata(self):
        """A known long duration no longer terminal-skips WX or blocks MetaGen."""
        task = SubTask(
            uid=1,
            url="http://test.com",
            title="Video 1",
            duration="75:07",
            status="排队中 (GD)",
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "BI": 100.0,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = False
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        task.wx_skip_reason = "视频时长 75.1 分钟，超过视频号 10 分钟限制"
        self.assertIsNone(engine._refresh_wechat_skip(task))
        self.assertIsNone(task.wx_skip_reason)
        self.assertEqual(task.status, "排队中 (GD)")

    def test_download_skip_does_not_record_wechat_limit(self):
        """A local source video no longer records a WX duration-limit decision."""
        workdir = os.path.join(self.temp_dir, "early_wechat_limit")
        os.makedirs(workdir)
        source = os.path.join(workdir, "source.mp4")
        with open(source, "wb") as handle:
            handle.truncate(11 * 1024 * 1024)

        task = SubTask(
            uid=2,
            url="http://test.com",
            title="Video 2",
            duration="未知",
            status="排队中 (DL)",
            workdir=workdir,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        task.duration = "75:07"
        self.assertIsNone(engine._refresh_wechat_skip(task))
        self.assertIsNone(task.wx_skip_reason)
        engine.save_state()
        with open(engine.state_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertIsNone(state["2"].get("wx_skip_reason"))

    def test_upload_commands_follow_main_llm_selection(self):
        """Bilibili and WeChat uploaders receive the selected model/provider."""
        workdir = os.path.join(self.temp_dir, "upload_llm_selection")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        bili_script = os.path.join(self.temp_dir, "bili_upload.py")
        wechat_script = os.path.join(self.temp_dir, "wechat_upload.py")
        for script in (bili_script, wechat_script):
            with open(script, "w") as handle:
                handle.write("# placeholder")

        with patch.object(autosub, "BILI_SCRIPT", bili_script), \
             patch.object(autosub, "BILI_PYTHON", sys.executable), \
             patch.object(autosub, "WECHAT_SCRIPT", wechat_script), \
             patch.object(autosub, "WECHAT_PYTHON", sys.executable), \
             patch.object(autosub, "get_video_duration", lambda _path: 600):
            bili_cmd = autosub.build_bili_cmd(
                workdir,
                llm_model="glm-5.3",
                llm_provider="dashscope",
            )
            wechat_cmd = autosub.build_wechat_cmd(
                workdir,
                llm_model="glm-5.3",
                llm_provider="dashscope",
            )

        for cmd in (bili_cmd, wechat_cmd):
            self.assertEqual(cmd[cmd.index("--llm-model") + 1], "glm-5.3")
            self.assertEqual(cmd[cmd.index("--llm-provider") + 1], "dashscope")

    def test_batch_worker_starts_gd_while_metadata_generation_is_blocked(self):
        """GD sync must not wait for a slow shared metadata LLM call."""
        workdir = os.path.join(self.temp_dir, "batch_slow_meta")
        os.makedirs(workdir)
        task = SubTask(
            uid=1,
            url="http://test.com",
            title="Video 1",
            status="排队中 (GD)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 0.0,
                "BI": 0.0,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = True
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        metadata = {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体",
        }
        calls = []
        meta_started = threading.Event()
        release_meta = threading.Event()
        gd_started = threading.Event()

        def fake_disk_truth(current_task):
            if current_task.status == "排队中 (GD)" and not os.path.exists(
                os.path.join(workdir, ".synced")
            ):
                return
            current_task.status = "完成"

        def fake_run_process(*_args, **_kwargs):
            calls.append("GD")
            gd_started.set()
            if not release_meta.wait(2):
                return False
            return True

        def fake_generate_metadata(*_args, **_kwargs):
            calls.append("META")
            meta_started.set()
            if not release_meta.wait(2):
                raise TimeoutError("metadata generation stayed blocked")
            return metadata

        def fake_bili_cmd(*_args, **_kwargs):
            return ["bili-upload"]

        def fake_wechat_cmd(*_args, **_kwargs):
            return ["wechat-upload"]

        def fake_bili_run(*_args, **_kwargs):
            calls.append("BI")
            return True

        def fake_wechat_run(*_args, **_kwargs):
            calls.append("WX")
            return True

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        try:
            with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
                 patch.object(BatchEngine, "run_process", side_effect=fake_run_process), \
                 patch.object(batch_pro, "generate_upload_metadata", side_effect=fake_generate_metadata), \
                 patch.object(batch_pro, "build_bili_cmd", side_effect=fake_bili_cmd), \
                 patch.object(batch_pro, "build_wechat_cmd", side_effect=fake_wechat_cmd), \
                 patch.object(batch_pro, "run_bili_cmd", side_effect=fake_bili_run), \
                 patch.object(batch_pro, "run_wechat_cmd", side_effect=fake_wechat_run):
                worker.start()

                self.assertTrue(meta_started.wait(2))
                self.assertTrue(
                    gd_started.wait(0.2),
                    "GD sync should start while metadata generation is blocked",
                )
                release_meta.set()
                worker.join(5)

            self.assertFalse(worker.is_alive())
            self.assertIn("GD", calls)
            self.assertIn("META", calls)
            self.assertIn("BI", calls)
            self.assertIn("WX", calls)
            self.assertLess(calls.index("GD"), calls.index("BI"))
            self.assertLess(calls.index("META"), calls.index("BI"))
            for marker in (".synced", ".bili_uploaded", ".wechat_uploaded"):
                self.assertTrue(os.path.exists(os.path.join(workdir, marker)))
            self.assertEqual(task.status, "完成")
        finally:
            release_meta.set()
            worker.join(2)

    def test_batch_worker_stops_uploads_when_shared_metadata_is_incomplete(self):
        """A partial MetaGen result must not trigger per-platform fallback."""
        workdir = os.path.join(self.temp_dir, "batch_partial_meta")
        os.makedirs(workdir)
        task = SubTask(
            uid=2,
            url="http://test.com",
            title="Video 2",
            status="排队中 (GD)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "BI": 0.0,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        def fake_disk_truth(current_task):
            if current_task.status == "排队中 (GD)":
                return
            current_task.status = "完成"

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(
                 batch_pro,
                 "generate_upload_metadata",
                 return_value={"title": "OpenAI研究员谈智能体"},
             ) as mock_meta, \
             patch.object(batch_pro, "build_bili_cmd") as mock_bili, \
            patch.object(batch_pro, "build_wechat_cmd") as mock_wechat:
            worker.start()
            worker.join(20)

        self.assertFalse(worker.is_alive())
        self.assertEqual(mock_meta.call_count, 4)
        mock_bili.assert_not_called()
        mock_wechat.assert_not_called()
        self.assertEqual(task.status, "失败 (上传)")
        self.assertEqual(task.retries, 3)
        for marker in (".bili_uploaded", ".wechat_uploaded"):
            self.assertFalse(os.path.exists(os.path.join(workdir, marker)))

    def test_batch_worker_keeps_wechat_build_failure_unfinished(self):
        """A skipped WeChat upload must not create a success marker."""
        workdir = os.path.join(self.temp_dir, "batch_wechat_build_failed")
        os.makedirs(workdir)
        task = SubTask(
            uid=3,
            url="http://test.com",
            title="Video 3",
            status="排队中 (GD)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "BI": 100.0,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = False
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        def fake_disk_truth(current_task):
            if current_task.status == "排队中 (GD)":
                return
            current_task.status = "完成"

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(
            batch_pro,
            "get_wechat_upload_skip_reason",
            return_value="视频时长 10.2 分钟，超过视频号 10 分钟限制",
        ), patch.object(batch_pro, "build_wechat_cmd", return_value=None):
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertIn("超过视频号 10 分钟限制", task.error)
        self.assertEqual(task.status, "超限 (WX)")
        self.assertEqual(task.pcts["WX"], 0.0)
        self.assertFalse(
            os.path.exists(os.path.join(workdir, ".wechat_uploaded"))
        )

    def test_parallel_upload_marks_wechat_limit_as_terminal(self):
        """When only WX is skipped for duration, GD/BI success must yield LIMIT."""
        workdir = os.path.join(self.temp_dir, "batch_wechat_limit")
        os.makedirs(workdir)
        task = SubTask(
            uid=5,
            url="http://test.com",
            title="Video 5",
            status="排队中 (GD)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 0.0,
                "BI": 0.0,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = True
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        metadata = {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体",
        }

        def fake_disk_truth(current_task):
            if current_task.status == "排队中 (GD)":
                return
            current_task.status = "完成"

        def fake_bili_run(_task, _cmd, result_box):
            result_box[0] = True
            return True

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(BatchEngine, "run_process", return_value=True), \
             patch.object(
                 batch_pro,
                 "generate_upload_metadata",
                 return_value=metadata,
             ), \
             patch.object(batch_pro, "build_bili_cmd", return_value=["bili-upload"]), \
             patch.object(
                 batch_pro,
                 "get_wechat_upload_skip_reason",
                 return_value="视频时长 10.2 分钟，超过视频号 10 分钟限制",
             ), \
             patch.object(batch_pro, "build_wechat_cmd", return_value=None), \
             patch.object(BatchEngine, "_run_bili_upload", side_effect=fake_bili_run):
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            task.error,
            "视频号上传未启动：视频时长 10.2 分钟，超过视频号 10 分钟限制",
        )
        self.assertEqual(task.pcts["GD"], 100.0)
        self.assertEqual(task.pcts["BI"], 100.0)
        self.assertEqual(task.pcts["WX"], 0.0)
        self.assertEqual(task.status, "超限 (WX)")

    def test_llm_defaults_accept_legacy_vendor_key(self):
        """The GUI's vendor key must resolve to the same provider."""
        settings = json.dumps(
            {"vendor": "Alibaba (Bailian)", "llm_model": "glm-5.3"}
        )
        with patch.object(autosub, "DEFAULTS", {}), \
             patch("builtins.open", mock_open(read_data=settings)):
            self.assertEqual(
                autosub._load_llm_defaults(),
                ("glm-5.3", "dashscope"),
            )

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
            '{"central_claim":"卡普谈Palantir数据整合","candidates":[{"title":"Palantir没有销售团队，客户却大规模采用","evidence":["我们没有销售团队，却看到了大规模的采用。"]}]}',
        ]
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title._title_has_quality_issue.return_value = False
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
                (("Palantir没有销售团队，客户却大规模采用", ("我们没有销售团队，却看到了大规模的采用。",)),),
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
        self.assertEqual(metadata["cover_title"], "Palantir没有销售团队，客户却大规模采用")
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
        self.assertIn(
            "B站标题只确定主题边界和必须保留的主体",
            llm_client.generate_content.call_args_list[2].args[0],
        )
        plain_title_calls = [
            call for call in subtitle_title.build_subtitle_grounded_title.call_args_list
            if call.kwargs.get("title_style") == "plain"
        ]
        self.assertEqual(plain_title_calls, [])

    def test_metadata_description_proper_nouns_must_be_grounded(self):
        """A summary cannot invent or misspell subtitle proper nouns."""
        subtitle = "Stefano Ermon谈Inception和Mercury。"
        video = "/tmp/Stefano_Ermon_Inception_hardsub.mp4"
        self.assertEqual(
            autosub._metadata_text_issues(
                "Stefano Ermon介绍Inception的Mercury。",
                subtitle,
                video,
            ),
            [],
        )
        self.assertEqual(
            autosub._metadata_text_issues(
                "Stefano Ermann介绍Inception的Mercury。",
                subtitle,
                video,
            ),
            ["known ASR proper noun misspelling: Ermann"],
        )
        self.assertEqual(
            autosub._metadata_text_issues(
                "Chad Walkwest谈Palantir。",
                "Chad Wahlquist谈Palantir。",
                "/tmp/Palantir_Chard_Wahlquist_hardsub.mp4",
            ),
            ["known ASR proper noun misspelling: Walkwest"],
        )
        self.assertEqual(
            autosub._metadata_text_issues(
                "低成本交付集成么服务。",
                "低成本交付集成服务。",
                "/tmp/video_hardsub.mp4",
            ),
            ["description contains a truncated service fragment"],
        )

    def test_generate_upload_metadata_corrects_known_asr_names(self):
        """Known ASR aliases must not be copied into metadata prompts."""
        workdir = os.path.join(self.temp_dir, "metadata_asr_names")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "Stefano_Ermon_Chard_Wahlquist_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        llm_client = MagicMock()
        llm_client.generate_content.return_value = "共享简介"
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.read_full_subtitle_text.return_value = (
            "Stefano Ermann和Chad Walkwest谈数据。",
            "subtitle.srt",
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
            autosub.generate_upload_metadata(workdir)

        prompt = llm_client.generate_content.call_args_list[0].args[0]
        self.assertIn("Stefano Ermon和Chad Wahlquist谈数据。", prompt)
        self.assertNotIn("Ermann", prompt)
        self.assertNotIn("Walkwest", prompt)

    def test_local_metadata_fallback_survives_llm_timeout(self):
        """A saved description can produce complete upload metadata offline."""
        description = (
            "几位 Palantir 员工现身说法：新人入职即被“扔进深水区”，"
            "有人第二周就进驻贝德福德郡警局上线工具。"
        )
        reframed_description = (
            "几位 Palantir 员工回答“给新人的建议”。"
            "新人一上来就被扔进深水区：入职第二周就去贝德福德郡警局上线工具。"
        )
        title = autosub._fallback_bili_title_from_description(description, 48)
        wechat_title = autosub._fallback_wechat_title_from_bili_title(title, 16)
        cover_title = autosub._fallback_cover_title(
            {"title": title, "wechat_title": wechat_title}
        )

        self.assertEqual(title, "Palantir 员工谈新人入职即被扔进深水区")
        self.assertEqual(wechat_title, "Palantir员工谈新人入职")
        self.assertEqual(cover_title, "Palantir员工谈新人入职")
        self.assertTrue(autosub.normalize_bili_upload_title(title))
        self.assertTrue(autosub.normalize_shared_upload_title(wechat_title))

        reframed_title = autosub._fallback_bili_title_from_description(
            reframed_description, 48
        )
        reframed_wechat_title = autosub._fallback_wechat_title_from_bili_title(
            reframed_title, 16
        )
        self.assertEqual(
            reframed_title,
            "Palantir 员工谈新人一上来就被扔进深水区",
        )
        self.assertEqual(reframed_wechat_title, "Palantir员工谈新人深水区")

    def test_teaching_metadata_fallback_survives_llm_timeout(self):
        """A good saved summary must not degrade to a random subtitle line."""
        description = (
            "Andrej Karpathy教你用好ChatGPT这类LLM应用。"
            "他把模型讲成“1TB压缩包”：知识来自预训练对互联网的有损压缩，"
            "性格靠后训练调教，自带知识截止日期。"
        )
        title = autosub._fallback_bili_title_from_description(description, 48)
        wechat_title = autosub._fallback_wechat_title_from_description(
            description,
            16,
        )
        cover_title = autosub._fallback_cover_title_from_description(
            description,
            form_title=title,
        )

        self.assertEqual(title, "Andrej Karpathy讲透ChatGPT这类LLM应用")
        self.assertEqual(wechat_title, "Karpathy讲透LLM应用")
        self.assertEqual(cover_title, "Andrej Karpathy用1TB压缩包讲模型")
        self.assertTrue(autosub.normalize_bili_upload_title(title))
        self.assertTrue(autosub.normalize_shared_upload_title(wechat_title))
        self.assertFalse(autosub._cover_title_has_quality_issue(cover_title))

    def test_offline_fallback_uses_distinct_subtitle_hooks(self):
        """LLM timeouts must still yield concrete, non-duplicate titles."""
        subtitle_text = (
            "数据治理有一半的工作是在当穿针引线的人、当知识\n"
            "我们就是结缔组织，是那层筋膜"
        )
        form_title = "正在帮助从业者在当今的数据治理时代做出贡献、展现价值"

        wechat_title = autosub._fallback_wechat_title_from_subtitle(
            subtitle_text,
            max_chars=16,
        )
        cover_title = autosub._fallback_cover_title_from_subtitle(
            subtitle_text,
            form_title=form_title,
        )

        self.assertEqual(wechat_title, "数据治理一半是在穿针引线")
        self.assertEqual(cover_title, "数据治理一半工作是在穿针引线")
        self.assertNotEqual(cover_title, form_title)
        self.assertNotEqual(cover_title, wechat_title)

    def test_offline_fallback_prefers_cause_and_consequence_hooks(self):
        """A form title may use a grounded consequence, not only a metaphor."""
        subtitle_text = (
            "当数据治理办公室的核心成员，甚至整个数据治理办公室\n"
            "被整个裁掉的时候，会怎么样？\n"
            "慢慢地、慢慢地、慢慢烂掉。你提供过的服务没人做了\n"
            "我们就是结缔组织，是那层筋膜\n"
            "没了筋膜，身体、肌肉、骨骼\n"
        )
        form_title = "数据治理团队展示不做的后果"

        wechat_title = autosub._fallback_wechat_title_from_subtitle(
            subtitle_text,
            max_chars=16,
            form_title=form_title,
        )
        cover_title = autosub._fallback_cover_title_from_subtitle(
            subtitle_text,
            form_title=form_title,
        )

        self.assertEqual(wechat_title, "数据治理团队被裁后组织会烂掉")
        self.assertEqual(cover_title, "没了筋膜，身体肌肉骨骼慢慢烂掉")

    def test_generate_upload_metadata_completes_after_llm_timeout(self):
        """A timeout must not leave BI/WX blocked without local metadata."""
        workdir = os.path.join(self.temp_dir, "metadata_llm_timeout")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "title": "正在帮助从业者在当今的数据治理时代做出贡献、展现价值",
                    "description": "数据治理实践者讨论组织连接和基础工作。",
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_text = (
            "数据治理有一半的工作是在当穿针引线的人、当知识\n"
            "我们就是结缔组织，是那层筋膜"
        )
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            subtitle_text,
            "subtitle.srt",
        )
        subtitle_title._title_has_quality_issue.return_value = False
        llm_client = MagicMock()

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_generate_title_from_description", side_effect=TimeoutError), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", side_effect=TimeoutError), \
             patch.object(autosub, "_generate_cover_title_from_full_subtitle", side_effect=TimeoutError), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: llm_client),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["wechat_title"], "数据治理一半是在穿针引线")
        self.assertEqual(metadata["cover_title"], "数据治理一半工作是在穿针引线")

    def test_generate_upload_metadata_falls_back_from_spoken_title(self):
        """A spoken Bilibili title still yields a WeChat title offline."""
        workdir = os.path.join(self.temp_dir, "metadata_spoken_title")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "title": "再说一次，如果从德国面临的核心问题说起，那就是出口导向型文化",
                    "description": "Palantir CEO Alex Karp 回应技术法西斯指控，并分析德国社会问题。",
                    "cover_title": "Palantir：还没真正好好利用这些资源，反而先去搞道德批判",
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            "德国面临的核心问题是出口导向型文化。",
            "subtitle.srt",
        )

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", side_effect=TimeoutError), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: MagicMock()),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(
            metadata["wechat_title"],
            "德国核心问题是出口导向型文化",
        )

    def test_generate_upload_metadata_falls_back_from_source_title(self):
        """A publisher title still yields a WeChat title when hooks fail."""
        workdir = os.path.join(self.temp_dir, "metadata_source_title")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "title": "一切终会以某种方式成为这样一个和平、社会民主主义的社会",
                    "description": "Peter Thiel在柏林受访时指出，AI潜力超越互联网。",
                    "cover_title": "Peter：通谕是在帮中共的忙",
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            "Peter Thiel discusses AI in Berlin.",
            "subtitle.srt",
        )

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(
                 autosub,
                 "_extract_source_title",
                 return_value="Peter Thiel - The AI Crisis Europe s Decline the Battle for America",
             ), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", side_effect=TimeoutError), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: MagicMock()),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["wechat_title"], "Peter Thiel谈AI")

    def test_extract_source_title_uses_info_file_then_filename(self):
        """Metadata generation should recover the publisher title reliably."""
        workdir = os.path.join(self.temp_dir, "source_title")
        os.makedirs(workdir)
        hardsub = os.path.join(
            workdir,
            "Responsible_AI_Innovation_for_British_Public_Services "
            "[7uIX9Zd4-VA] [1080p]_hardsub.mp4",
        )
        info_path = os.path.join(workdir, "source.info.json")
        with open(info_path, "w", encoding="utf-8") as handle:
            json.dump({"title": "Publisher Title"}, handle)

        self.assertEqual(autosub._extract_source_title(hardsub), "Publisher Title")

        os.remove(info_path)
        self.assertEqual(
            autosub._extract_source_title(hardsub),
            "Responsible AI Innovation for British Public Services",
        )
        self.assertEqual(
            autosub._extract_source_title(
                os.path.join(workdir, "video_hardsub.mp4")
            ),
            "",
        )

    def test_generate_upload_metadata_prefers_source_title_over_random_hook(self):
        """A publisher title is the last fallback when no Chinese subtitle exists."""
        workdir = os.path.join(self.temp_dir, "metadata_source_fallback")
        os.makedirs(workdir)
        hardsub = os.path.join(
            workdir,
            "Responsible_AI_Innovation_for_British_Public_Services "
            "[7uIX9Zd4-VA] [1080p]_hardsub.mp4",
        )
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump({"description": "Palantir UK FDE interview"}, handle)

        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            "Palantir FDE interview.",
            "subtitle.srt",
        )
        subtitle_title.build_subtitle_grounded_title.return_value = SimpleNamespace(title="")

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_generate_title_from_description", side_effect=ValueError), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", return_value=None), \
             patch.object(autosub, "_generate_cover_title_from_full_subtitle", return_value=None), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: MagicMock()),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["title"], "Responsible AI Innovation for British Public")
        self.assertEqual(metadata["wechat_title"], "Responsible AI")
        self.assertEqual(metadata["cover_title"], "Responsible AI Innovation for British Public")

    def test_generate_upload_metadata_rejects_english_title_with_chinese_subtitle(self):
        """A Chinese transcript must not fall back to an English source title."""
        workdir = os.path.join(self.temp_dir, "metadata_chinese_fallback")
        os.makedirs(workdir)
        hardsub = os.path.join(
            workdir,
            "Palantir_AIPCon_11_Panel_hardsub.mp4",
        )
        with open(hardsub, "w") as handle:
            handle.write("video")
        subtitle_text = (
            "AIPCon 11 的主题就是 Palantir 标准。\n"
            "医院被攻击，就有人做不了透析。\n"
            "脓毒症这个病，是全球医院病人的头号杀手。\n"
        )
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            subtitle_text,
            "subtitle.srt",
        )
        subtitle_title._title_has_quality_issue.return_value = False
        subtitle_title.build_subtitle_grounded_title.return_value = SimpleNamespace(title="")

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_generate_title_from_description", side_effect=TimeoutError), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", return_value=None), \
             patch.object(autosub, "_generate_cover_title_from_full_subtitle", return_value=None), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: MagicMock()),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertIn("Palantir 标准", metadata["title"])
        self.assertTrue(autosub._contains_chinese_text(metadata["title"], 6))
        self.assertNotEqual(metadata["title"], metadata["cover_title"])
        self.assertIn("透析", metadata["cover_title"])

    def test_bad_upload_metadata_cache_is_regenerated(self):
        """A complete cache is not reused when its titles violate quality rules."""
        workdir = os.path.join(self.temp_dir, "metadata_bad_cache")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "Palantir_AIPCon_11_Panel_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        cached = {
            "source_title": "Palantir AIPCon 11 Panel",
            "description": "Palantir AIPCon 现场访谈合集，聚焦行业落地成果。",
            "title": "Palantir AIPCon 11 Panel",
            "wechat_title": "Palantir AIPCon 11 Panel",
            "cover_title": "Palantir AIPCon 11 Panel",
        }
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(cached, handle, ensure_ascii=False)

        subtitle_text = (
            "AIPCon 11 的主题就是 Palantir 标准。\n"
            "医院被攻击，就有人做不了透析。\n"
            "脓毒症这个病，是全球医院病人的头号杀手。\n"
            "所以我们建了自己的内部模型，给患者打上脓毒症标记。\n"
        )
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            subtitle_text,
            "subtitle.srt",
        )
        subtitle_title._title_has_quality_issue.return_value = False
        llm_client = MagicMock()

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_generate_title_from_description", return_value="Palantir AIPCon访谈聚焦行业落地"), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", return_value="Palantir聚焦落地"), \
             patch.object(autosub, "_generate_cover_title_from_full_subtitle", return_value="医院被攻击，就有人做不了透析"), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: llm_client),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["title"], "Palantir AIPCon访谈聚焦行业落地")
        self.assertEqual(metadata["wechat_title"], "Palantir聚焦落地")
        self.assertEqual(
            metadata["cover_title"],
            "AIPCon访谈：我们用自建模型提前识别了脓毒症",
        )
        self.assertNotEqual(metadata["title"], metadata["cover_title"])
        self.assertFalse(llm_client.generate_content.called)

    def test_cached_upload_title_must_cover_whole_video_summary(self):
        """A single highlight or spoken quote must not survive metadata reuse."""
        source_title = "The Wildest Hot Takes From the All-In Summit 2026"
        description = (
            "All-In Summit 2026现场，多位嘉宾讨论AI、能源与人体创新。"
            "内容包括SaaS、天然气、超加工食品、自由意志和生物黑客。"
        )

        self.assertTrue(
            autosub._metadata_title_has_whole_video_issue(
                "GenSpark建立管控机制加速普及模型",
                source_title=source_title,
                description=description,
            )
        )
        self.assertTrue(
            autosub._metadata_title_has_whole_video_issue(
                "因为我来这里不只是为了拍个视频",
                source_title=source_title,
                description=description,
            )
        )
        self.assertFalse(
            autosub._metadata_title_has_whole_video_issue(
                "All-In峰会嘉宾谈AI、能源与人体创新",
                source_title=source_title,
                description=description,
            )
        )

    def test_title_and_cover_prompts_anchor_on_source_and_form_titles(self):
        """Prompts must compare the publisher title with the transcript主线."""
        llm_client = MagicMock()
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir英国FDE讲真实工作情况和节奏感",
            (("Palantir英国FDE讲真实工作情况和节奏感", ("Palantir FDE 谈英国公共服务。",)),),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "Palantir FDE 谈英国公共服务。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._generate_title_from_description(
                "Palantir UK FDE interview",
                "Palantir FDE 谈英国公共服务。",
                "Palantir FDE 谈英国公共服务。",
                llm_client,
                "glm-5.2",
                48,
                source_title="Responsible AI Innovation for British Public Services",
            )
            with self.assertRaises(ValueError):
                autosub._generate_cover_title_from_full_subtitle(
                    "Palantir FDE 谈英国公共服务。",
                    llm_client,
                    "glm-5.2",
                    form_title=title,
                    source_title="Responsible AI Innovation for British Public Services",
                )

        title_prompt = llm_client.generate_content.call_args_list[0].args[0]
        cover_prompt = llm_client.generate_content.call_args_list[-1].args[0]
        self.assertIn("视频原标题：\nResponsible AI Innovation", title_prompt)
        self.assertIn("先用它判断视频场景和受益者", title_prompt)
        self.assertIn("多嘉宾、多段访谈或合集视频", title_prompt)
        self.assertIn("现代汉语自然短句", title_prompt)
        self.assertIn("不得有翻译腔、口语填充、机械拼接或AI套话", title_prompt)
        self.assertIn("封面标题不是表单标题的压缩版", cover_prompt)
        self.assertIn("不得作为封面标题的主要模板", cover_prompt)
        self.assertIn("B站表单标题：\nPalantir英国FDE讲真实工作情况和节奏感", cover_prompt)
        self.assertIn("不要求一个钩子覆盖所有嘉宾", cover_prompt)
        self.assertIn("不能整句照抄", cover_prompt)
        self.assertIn("观众三秒内要能读出", cover_prompt)
        self.assertIn("主体或场景、冲突、后果", cover_prompt)
        self.assertIn("不得留谜面", cover_prompt)
        self.assertIn("人称视角按证据强度选择", cover_prompt)
        self.assertIn("我们+具体动作+了", cover_prompt)
        self.assertIn("不得写成「内部模型」", cover_prompt)
        self.assertIn("不得把别段的本地部署或本体拼进当前案例", cover_prompt)

    def test_title_scoring_prefers_transcript_mainline_over_source_topic(self):
        """The publisher title must not outweigh the transcript's main subject."""
        llm_client = MagicMock()
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title._title_has_quality_issue.return_value = False
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir英国FDE讲真实工作情况",
            (
                (
                    "为英国公共服务打造负责任AI创新",
                    ("英国公共服务谈负责任AI创新。",),
                ),
                (
                    "Palantir英国FDE讲真实工作情况和节奏感",
                    ("Palantir FDE 谈真实工作情况。",),
                ),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "Palantir FDE 谈真实工作情况。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._generate_title_from_description(
                "Palantir员工和英国FDE讲真实工作情况，涉及英国公共服务场景。",
                "Palantir FDE 谈英国公共服务和真实工作情况。",
                "Palantir FDE 谈英国公共服务和真实工作情况。",
                llm_client,
                "glm-5.2",
                48,
                source_title="Responsible AI Innovation for British Public Services",
            )

        self.assertEqual(title, "Palantir英国FDE讲真实工作情况和节奏感")

    def test_cover_title_prefers_subtitle_hook_over_form_title_duplicate(self):
        """The form title is context, while the cover carries its own hook."""
        llm_client = MagicMock()
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title._title_has_quality_issue.return_value = False
        subtitle_title.parse_llm_title_candidates.return_value = (
            "Palantir英国FDE讲真实工作情况",
            (
                (
                    "Palantir英国FDE讲真实工作情况",
                    ("Palantir英国FDE讲真实工作情况。",),
                ),
                (
                    "Palantir FDE一上来就被扔进深水区",
                    ("一上来就被扔进深水区，还要自己把问题讲清楚。",),
                ),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = ()

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            cover_title = autosub._generate_cover_title_from_full_subtitle(
                "Palantir英国FDE讲真实工作情况。一上来就被扔进深水区，还要自己把问题讲清楚。",
                llm_client,
                "glm-2",
                form_title="Palantir英国FDE讲真实工作情况",
                source_title="Responsible AI Innovation for British Public Services",
            )

        self.assertEqual(cover_title, "Palantir FDE一上来就被扔进深水区")

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

    def test_title_quality_gate_rejects_unnatural_chinese(self):
        """Bad translation-style hooks cannot pass merely by being Chinese."""
        bad_titles = (
            "就裁掉员工的公司 思路完全不对",
            "光是身处深水区，你就能获得大量的支持，学到大量东西",
            "AI如何赋能企业底层逻辑",
        )
        for title in bad_titles:
            self.assertTrue(autosub._title_has_naturalness_issue(title))
            self.assertTrue(autosub._shared_title_has_quality_issue(title))
            self.assertTrue(autosub._cover_title_has_quality_issue(title))

        self.assertFalse(autosub._title_has_naturalness_issue("医院被攻击，就有人做不了透析"))
        self.assertFalse(autosub._title_has_naturalness_issue("AIPCon访谈聚焦行业落地"))

    def test_cover_fallback_removes_conversational_prefix(self):
        """A strong transcript hook must not keep the interviewee's filler."""
        self.assertEqual(
            autosub._clarify_cover_hook_line(
                "当然可以。脓毒症这个病，是全球医院病人的头号杀手。"
            ),
            "我们用自建模型提前识别了脓毒症",
        )
        self.assertFalse(
            autosub._cover_title_has_quality_issue(
                "AIPCon：我们用自建模型提前识别了脓毒症"
            )
        )
        self.assertTrue(
            autosub._cover_title_has_quality_issue(
                "AIPCon：医院用内部模型提前识别脓毒症"
            )
        )

    def test_cover_title_must_link_to_video_thesis(self):
        """A striking local fact cannot pass without the whole-video subject."""
        form_title = "Palantir AIPCon访谈聚焦行业落地"
        source_title = "Palantir AIPCon 11 - Ondas Zeta Tampa General"
        description = (
            "本期视频为 Palantir 第 11 届 AIPCon 现场访谈合集，"
            "聚焦 Foundry 与 AIP 的行业落地成果。"
        )

        self.assertTrue(
            autosub._cover_title_has_thesis_issue(
                "脓毒症是全球医院病人的头号杀手",
                form_title=form_title,
                source_title=source_title,
                description=description,
            )
        )
        self.assertFalse(
            autosub._cover_title_has_thesis_issue(
                "AIPCon：我们用自建模型提前识别了脓毒症",
                form_title=form_title,
                source_title=source_title,
                description=description,
            )
        )

    def test_panel_wechat_fallback_rewrites_summary_naturally(self):
        """A Panel summary yields a short Chinese title instead of a raw hook."""
        description = (
            "本期视频为 Palantir 第 11 届 AIPCon 现场访谈合集，"
            "聚焦 Foundry 与 AIP 的行业落地成果。"
        )

        self.assertEqual(
            autosub._fallback_wechat_title_from_description(description, 16),
            "AIPCon访谈聚焦行业落地",
        )

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
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title._title_has_quality_issue.return_value = False
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
        subtitle_title.SubtitleTitleError = ValueError
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
        self.assertIn("为视频号生成更有吸引力的表单标题", prompt)
        self.assertIn("B站标题只确定主题边界和必须保留的主体", prompt)
        self.assertIn("优先通读字幕", prompt)
        self.assertIn("不得另起主题", prompt)
        self.assertIn("B站表单标题：\nPalantir为企业整合数据，不做服务，打造产品", prompt)
        self.assertIn("中文最多还能放 8 个字", prompt)
        self.assertIn("必须优先原样保留B站标题开头的主体", prompt)
        self.assertIn("优先保留主体和最有力的后果或立场", prompt)
        self.assertIn("主体+具体后果/反差", prompt)
        self.assertIn("不得以「就」「光是」开头", prompt)
        self.assertIn("不得保留字幕里的口语填充或直译腔", prompt)
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
        subtitle_title.SubtitleTitleError = ValueError
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
        self.assertTrue(
            autosub._cover_title_has_engagement_issue(
                "我们就像在帕罗奥图正中央开马戏团"
            )
        )
        self.assertTrue(
            autosub._cover_title_has_engagement_issue(
                "医院被攻击，就有人做不了透析"
            )
        )
        self.assertFalse(
            autosub._cover_title_has_engagement_issue(
                "医院被攻击，透析先受影响"
            )
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
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title._title_has_quality_issue.return_value = False
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

        self.assertEqual(cover_title, "没有销售团队，却看到大规模采用")
        prompt = llm_client.generate_content.call_args.args[0]
        self.assertIn("通读下面的完整中文字幕", prompt)
        self.assertIn("参考高播放封面标题的结构", prompt)
        self.assertIn("观众三秒内要能读出", prompt)
        self.assertIn("不得留谜面", prompt)
        self.assertIn("不要只看开头、结尾或采样片段", prompt)
        self.assertIn("按话题感、画面感", prompt)
        self.assertIn("不要把普通商务反差或抽象结论当成最强钩子", prompt)
        self.assertIn("主语、谓语和比喻/冲突对象要保留", prompt)
        self.assertIn("不得以「就像」「在」「需要」「带着」这类无主语成分开头", prompt)
        self.assertIn("优先 18-32 个字符", prompt)
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
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.read_full_subtitle_text.return_value = (
            "中文第一行\nEnglish line\n中文第二行",
            "subtitle.srt",
        )
        subtitle_title._title_has_quality_issue.return_value = False

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
                return_value="Palantir没有销售团队，客户却大规模采用",
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
        self.assertEqual(metadata["cover_title"], "Palantir没有销售团队，客户却大规模采用")
        metadata_path = os.path.join(workdir, "upload_metadata.json")
        with open(metadata_path, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        self.assertEqual(persisted, metadata)
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
                llm_model="glm-5.3",
                llm_provider="dashscope",
            )

        mock_meta.assert_called_once_with(
            workdir,
            bili_title_max_chars=48,
            wechat_title_max_chars=16,
            llm_model="glm-5.3",
            llm_provider="dashscope",
        )
        bili_kwargs = next(call[1] for call in calls if call[0] == "BI")
        wechat_kwargs = next(call[1] for call in calls if call[0] == "WX")
        self.assertEqual(bili_kwargs["llm_model"], "glm-5.3")
        self.assertEqual(bili_kwargs["llm_provider"], "dashscope")
        self.assertEqual(wechat_kwargs["llm_model"], "glm-5.3")
        self.assertEqual(wechat_kwargs["llm_provider"], "dashscope")
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

    def test_load_state_preserves_wechat_limit_terminal_state(self):
        """Restarting the engine must not requeue a WX duration-limit skip."""
        project_dir = os.path.join(self.temp_dir, "project")
        task_dir = os.path.join(project_dir, "task_1")
        os.makedirs(task_dir)
        state_file = os.path.join(project_dir, "batch_state.json")
        state = {
            "wechat": True,
            "1": {
                "uid": 1,
                "url": "http://test.com",
                "vid_id": "vid1",
                "title": "Video 1",
                "status": "超限 (WX)",
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
                "error": "视频时长 10.2 分钟，超过视频号 10 分钟限制",
            },
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.load_state()

        task = engine.task_map[1]
        self.assertEqual(task.status, "超限 (WX)")
        self.assertIn("超过视频号 10 分钟限制", task.error)
        self.assertEqual(task.pcts["WX"], 0.0)
        self.assertTrue(engine.do_wechat)

if __name__ == "__main__":
    unittest.main()
