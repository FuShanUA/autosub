import contextlib
import inspect
import os
import sys
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, mock_open, patch

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

# Importing autosub_batch_pro runs logging.basicConfig() against the production
# log under Projects/logs. Detach the root handlers so a test run does not write
# fake "Task N" lines into the log used to diagnose real batch runs.
_root_logger = logging.getLogger()
for _handler in list(_root_logger.handlers):
    _root_logger.removeHandler(_handler)
_root_logger.addHandler(logging.NullHandler())

class TestAutoSubStateAndLauncher(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._bili_login_patcher = patch.object(
            batch_pro, "get_bili_login_skip_reason", return_value=None
        )
        self._bili_login_patcher.start()
        self.addCleanup(self._bili_login_patcher.stop)
        
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
        self.assertIn("[grey37]", queued_active)
        self.assertNotIn("[bright_blue]", queued_active)
        self.assertIn("[grey37]", inactive)

    def test_find_tool_checks_macos_homebrew_fallback(self):
        """A GUI-launched process must find Homebrew ffprobe without a login PATH."""
        expected = "/opt/homebrew/bin/ffprobe"
        with patch.dict(os.environ, {"PATH": ""}), \
             patch.object(autosub.sys, "platform", "darwin"), \
             patch.object(autosub.os.path, "exists", lambda path: path == expected):
            self.assertEqual(autosub.find_tool("ffprobe"), expected)

    def test_gui_pause_controls_all_post_processing_processes(self):
        """Pause must cover concurrent GD/BI/WX jobs, not only the main pipeline."""
        import autosub_gui
        from autosub_gui import AutoSubGUI

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def poll(self):
                return None

        gui = AutoSubGUI.__new__(AutoSubGUI)
        gui.root = SimpleNamespace(after=lambda _delay, callback: callback())
        gui.current_processes = [FakeProcess(101), FakeProcess(202)]
        gui._process_lock = threading.Lock()
        gui.is_paused = False
        pause_texts = []
        gui.pause_btn = SimpleNamespace(
            configure=lambda **kwargs: pause_texts.append(kwargs.get("text"))
        )
        logs = []
        gui.log = logs.append

        with patch.object(autosub_gui.ProcessTree, "suspend", return_value=True) as mock_suspend, \
             patch.object(autosub_gui.ProcessTree, "resume", return_value=True) as mock_resume:
            gui.toggle_pause()
            self.assertTrue(gui.is_paused)
            self.assertEqual(mock_suspend.call_args_list, [call(101), call(202)])

            gui.toggle_pause()
            self.assertFalse(gui.is_paused)
            self.assertEqual(mock_resume.call_args_list, [call(101), call(202)])

        self.assertIn("⏸️ 已暂停 2 个进程", logs)
        self.assertIn("▶️ 已继续 2 个进程", logs)
        self.assertEqual(pause_texts, ["继续", "暂停"])

    def test_gui_progress_maps_stage_percent_to_normalized_bar(self):
        """A stage's 100% must not fill the whole workflow bar."""
        import autosub_gui
        from autosub_gui import AutoSubGUI

        class FakeProgressVar:
            def __init__(self):
                self.value = 0.0

            def get(self):
                return self.value

            def set(self, value):
                self.value = float(value)

        gui = AutoSubGUI.__new__(AutoSubGUI)
        gui.progress_var = FakeProgressVar()
        gui.progress_stage = None
        gui.progress_stage_start = 0.0
        gui.progress_stage_end = 1.0

        gui._update_progress_from_log("🎬 Downloading https://example.com/video...")
        gui._update_progress_from_log("Progress: 100.0%")
        self.assertAlmostEqual(gui.progress_var.get(), 0.10)

        gui._update_progress_from_log("🎙️ Transcribing video.mp4...")
        gui._update_progress_from_log("Progress: 50.0%")
        self.assertAlmostEqual(gui.progress_var.get(), 0.275)

        gui._update_progress_from_log("🌍 Smart-translating...")
        gui._update_progress_from_log("Progress: 100.0%")
        self.assertAlmostEqual(gui.progress_var.get(), 0.80)

        gui._update_progress_from_log("🔥 Burning subtitles...")
        gui._update_progress_from_log("Progress: 100.0%")
        self.assertAlmostEqual(gui.progress_var.get(), 0.95)

        gui._update_progress_from_log("📤 [Bilibili] 开始上传...")
        gui._update_upload_progress(50.0)
        self.assertAlmostEqual(gui.progress_var.get(), 0.975)

        gui._update_progress_from_log("✅ 全部完成！")
        self.assertEqual(gui.progress_var.get(), 1.0)

        gui._reset_progress(stage="upload")
        gui._update_progress_from_log("覆盖率 90%，不是进度行")
        self.assertEqual(gui.progress_var.get(), 0.0)

    def test_upload_only_leaves_platform_uploads_to_worker(self):
        """Upload-only must not let autosub.py bypass ad removal and job setup."""
        import autosub_gui

        gui = autosub_gui.AutoSubGUI.__new__(autosub_gui.AutoSubGUI)
        gui.root = SimpleNamespace(after=lambda _delay, callback: callback())
        gui._upload_abort = threading.Event()
        notebook_calls = []
        gui.notebook = SimpleNamespace(set=notebook_calls.append)
        gui.start_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.upload_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.stop_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.progress_var = SimpleNamespace(get=lambda: 0.0, set=lambda _value: None)
        gui.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.log_text = SimpleNamespace(
            configure=lambda **_kwargs: None,
            delete=lambda *_args: None,
        )
        gui.input_var = SimpleNamespace(get=lambda: "video.mp4")
        gui.output_dir_var = SimpleNamespace(get=lambda: "")
        gui.vendor_var = SimpleNamespace(get=lambda: "Alibaba (Bailian)")
        gui.llm_model_var = SimpleNamespace(get=lambda: "qwen3.7-max")
        gui.sync_gdrive_var = SimpleNamespace(get=lambda: "remote-folder")
        gui.gdrive_enabled_var = SimpleNamespace(get=lambda: True)
        gui.bili_enabled_var = SimpleNamespace(get=lambda: True)
        gui.wechat_enabled_var = SimpleNamespace(get=lambda: True)
        gui.wechat_public_var = SimpleNamespace(get=lambda: True)
        gui.vendor_map = {"Alibaba (Bailian)": "dashscope"}

        with patch.object(autosub_gui.threading, "Thread") as mock_thread, \
             patch.object(gui, "_persist_input_value") as mock_persist:
            mock_thread.return_value = SimpleNamespace(start=lambda: None)
            gui.start_upload_only()

        cmd = mock_thread.call_args.kwargs["args"][0]
        mock_persist.assert_called_once_with("video.mp4")
        self.assertEqual(notebook_calls, ["日志"])
        self.assertIn("--upload-only", cmd)
        self.assertNotIn("--sync-gdrive", cmd)
        self.assertNotIn("--bili-upload", cmd)
        self.assertNotIn("--wechat-upload", cmd)
        self.assertNotIn("--wechat-public", cmd)

    def test_start_process_switches_to_log_tab(self):
        """Starting the main pipeline must immediately show the log page."""
        import autosub_gui

        gui = autosub_gui.AutoSubGUI.__new__(autosub_gui.AutoSubGUI)
        gui.root = SimpleNamespace(after=lambda _delay, callback: callback())
        gui.input_var = SimpleNamespace(get=lambda: "video.mp4")
        notebook_calls = []
        gui.notebook = SimpleNamespace(set=notebook_calls.append)
        gui.start_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.stop_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.progress_var = SimpleNamespace(set=lambda _value: None)
        gui.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.log_text = SimpleNamespace(
            configure=lambda **_kwargs: None,
            delete=lambda *_args: None,
        )
        gui.current_processes = []
        gui._process_lock = threading.Lock()
        gui._upload_abort = threading.Event()
        gui.trans_mode_var = SimpleNamespace(get=lambda: "balanced")
        gui.style_var = SimpleNamespace(get=lambda: "auto")
        gui.whisper_var = SimpleNamespace(get=lambda: "large-v2")
        gui.llm_model_var = SimpleNamespace(get=lambda: "qwen3.7-max")
        gui.transcription_enabled_var = SimpleNamespace(get=lambda: True)
        gui.translation_enabled_var = SimpleNamespace(get=lambda: True)
        gui.burn_enabled_var = SimpleNamespace(get=lambda: True)
        gui.vendor_var = SimpleNamespace(get=lambda: "Alibaba (Bailian)")
        gui.vendor_map = {"Alibaba (Bailian)": "dashscope"}
        gui.quality_var = SimpleNamespace(get=lambda: "high")
        gui.layout_var = SimpleNamespace(get=lambda: "bilingual")
        gui.main_lang_var = SimpleNamespace(get=lambda: "cn")
        gui.cn_font_var = SimpleNamespace(get=lambda: "STKaiti")
        gui.en_font_var = SimpleNamespace(get=lambda: "Arial")
        gui.jp_font_var = SimpleNamespace(get=lambda: "Hiragino Maru Gothic ProN")
        gui.cn_size_var = SimpleNamespace(get=lambda: "60")
        gui.en_size_var = SimpleNamespace(get=lambda: "36")
        gui.jp_size_var = SimpleNamespace(get=lambda: "36")
        gui.bg_box_var = SimpleNamespace(get=lambda: True)
        gui.quick_var = SimpleNamespace(get=lambda: False)
        gui.multilang_var = SimpleNamespace(get=lambda: False)
        gui.cookies_var = SimpleNamespace(get=lambda: "")
        gui.output_dir_var = SimpleNamespace(get=lambda: "")
        gui.sync_gdrive_var = SimpleNamespace(get=lambda: "")
        gui.gdrive_enabled_var = SimpleNamespace(get=lambda: False)
        gui._persist_input_value = MagicMock()

        with patch.object(autosub_gui.threading, "Thread") as mock_thread:
            mock_thread.return_value = SimpleNamespace(start=lambda: None)
            gui.start_process()

        self.assertEqual(notebook_calls, ["日志"])
        self.assertEqual(mock_thread.call_count, 1)

    def test_start_process_passes_skip_burn_flag(self):
        """Unchecked burn stage must reach the CLI command."""
        import autosub_gui

        gui = autosub_gui.AutoSubGUI.__new__(autosub_gui.AutoSubGUI)
        gui.root = SimpleNamespace(after=lambda _delay, callback: callback())
        gui.input_var = SimpleNamespace(get=lambda: "video.mp4")
        gui.notebook = SimpleNamespace(set=lambda _value: None)
        gui.start_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.stop_btn = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.progress_var = SimpleNamespace(set=lambda _value: None)
        gui.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        gui.log_text = SimpleNamespace(
            configure=lambda **_kwargs: None,
            delete=lambda *_args: None,
        )
        gui.current_processes = []
        gui._process_lock = threading.Lock()
        gui._upload_abort = threading.Event()
        gui.trans_mode_var = SimpleNamespace(get=lambda: "balanced")
        gui.style_var = SimpleNamespace(get=lambda: "auto")
        gui.whisper_var = SimpleNamespace(get=lambda: "large-v2")
        gui.llm_model_var = SimpleNamespace(get=lambda: "qwen3.7-max")
        gui.transcription_enabled_var = SimpleNamespace(get=lambda: True)
        gui.translation_enabled_var = SimpleNamespace(get=lambda: True)
        gui.burn_enabled_var = SimpleNamespace(get=lambda: False)
        gui.vendor_var = SimpleNamespace(get=lambda: "Alibaba (Bailian)")
        gui.vendor_map = {"Alibaba (Bailian)": "dashscope"}
        gui.quality_var = SimpleNamespace(get=lambda: "high")
        gui.layout_var = SimpleNamespace(get=lambda: "bilingual")
        gui.main_lang_var = SimpleNamespace(get=lambda: "cn")
        gui.cn_font_var = SimpleNamespace(get=lambda: "STKaiti")
        gui.en_font_var = SimpleNamespace(get=lambda: "Arial")
        gui.jp_font_var = SimpleNamespace(get=lambda: "Hiragino Maru Gothic ProN")
        gui.cn_size_var = SimpleNamespace(get=lambda: "60")
        gui.en_size_var = SimpleNamespace(get=lambda: "36")
        gui.jp_size_var = SimpleNamespace(get=lambda: "36")
        gui.bg_box_var = SimpleNamespace(get=lambda: True)
        gui.quick_var = SimpleNamespace(get=lambda: False)
        gui.multilang_var = SimpleNamespace(get=lambda: False)
        gui.cookies_var = SimpleNamespace(get=lambda: "")
        gui.output_dir_var = SimpleNamespace(get=lambda: "")
        gui.sync_gdrive_var = SimpleNamespace(get=lambda: "")
        gui.gdrive_enabled_var = SimpleNamespace(get=lambda: False)
        gui._persist_input_value = lambda _value: None

        with patch.object(autosub_gui.threading, "Thread") as mock_thread:
            mock_thread.return_value = SimpleNamespace(start=lambda: None)
            gui.start_process()

        cmd = mock_thread.call_args.kwargs["args"][0]
        self.assertIn("--skip-burn", cmd)

    def test_resolve_upload_targets_prefers_explicit_list(self):
        """Explicit upload targets should override the legacy flags."""
        args = SimpleNamespace(
            no_upload=False,
            upload_targets=["gdrive", "wechat"],
            sync_gdrive="folder",
            bili_upload=True,
            wechat_upload=False,
        )
        self.assertEqual(
            autosub.resolve_upload_targets(args),
            {"gdrive", "wechat"},
        )

    def test_resolve_upload_targets_no_upload_wins(self):
        """--no-upload must disable every destination."""
        args = SimpleNamespace(
            no_upload=True,
            upload_targets=["gdrive", "bilibili", "wechat"],
            sync_gdrive="folder",
            bili_upload=True,
            wechat_upload=True,
        )
        self.assertEqual(autosub.resolve_upload_targets(args), set())

    def test_gui_stage_flags_skip_disabled_stages(self):
        """Unchecked GUI stages must map to the CLI skip flags."""
        import autosub_gui

        gui = autosub_gui.AutoSubGUI.__new__(autosub_gui.AutoSubGUI)
        gui.transcription_enabled_var = SimpleNamespace(get=lambda: False)
        gui.translation_enabled_var = SimpleNamespace(get=lambda: True)
        gui.burn_enabled_var = SimpleNamespace(get=lambda: True)
        self.assertEqual(
            gui._pipeline_stage_flags(),
            ["--skip-transcription"],
        )

    def test_gui_stage_flags_skip_burn(self):
        """Unchecked burn stage must map to --skip-burn."""
        import autosub_gui

        gui = autosub_gui.AutoSubGUI.__new__(autosub_gui.AutoSubGUI)
        gui.transcription_enabled_var = SimpleNamespace(get=lambda: True)
        gui.translation_enabled_var = SimpleNamespace(get=lambda: True)
        gui.burn_enabled_var = SimpleNamespace(get=lambda: False)
        self.assertIn("--skip-burn", gui._pipeline_stage_flags())

    def test_history_requirements_ignore_uploads_when_disabled(self):
        """A disabled upload stage must not keep completed tasks pending."""
        import autosub_gui

        gui = autosub_gui.AutoSubGUI.__new__(autosub_gui.AutoSubGUI)
        gui.upload_enabled_var = SimpleNamespace(get=lambda: False)
        gui.gdrive_enabled_var = SimpleNamespace(get=lambda: True)
        gui.bili_enabled_var = SimpleNamespace(get=lambda: True)
        gui.wechat_enabled_var = SimpleNamespace(get=lambda: True)
        self.assertEqual(gui._history_requirements(), [])

    def test_scan_task_history_reads_disk_stage_truth(self):
        """History must report outputs and upload markers from each project."""
        import autosub_gui

        base_dir = os.path.join(self.temp_dir, "Projects")
        complete_dir = os.path.join(base_dir, "Complete Video")
        partial_dir = os.path.join(base_dir, "Partial Video")
        failed_dir = os.path.join(base_dir, "Failed Video")
        for workdir in (complete_dir, partial_dir, failed_dir):
            os.makedirs(workdir)
        with open(os.path.join(base_dir, "workflow.log"), "w", encoding="utf-8") as handle:
            handle.write("--- 任务启动: root-level legacy log ---\n")

        source_url = "https://example.com/watch?v=complete"
        partial_source = os.path.join(self.temp_dir, "partial.mp4")
        with open(os.path.join(complete_dir, "workflow.log"), "w", encoding="utf-8") as handle:
            handle.write(f"--- 任务启动: {source_url} ---\n")
        with open(os.path.join(partial_dir, "workflow.log"), "w", encoding="utf-8") as handle:
            handle.write(f"--- 任务启动: {partial_source} ---\n")
        with open(os.path.join(failed_dir, "workflow.log"), "w", encoding="utf-8") as handle:
            handle.write("--- 任务启动: https://example.com/watch?v=failed ---\n")

        with open(os.path.join(complete_dir, "video.srt"), "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        with open(os.path.join(complete_dir, "video_hardsub.mp4"), "wb") as handle:
            handle.write(b"video")
        for marker in (".synced", ".bili_uploaded", ".wechat_uploaded"):
            with open(os.path.join(complete_dir, marker), "w", encoding="utf-8") as handle:
                handle.write("1")
        os.makedirs(os.path.join(complete_dir, "noads"))

        with open(os.path.join(partial_dir, "video.srt"), "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        with open(os.path.join(partial_dir, "video_hardsub.mp4"), "wb") as handle:
            handle.write(b"video")

        records = {
            os.path.basename(record["workdir"]): record
            for record in autosub_gui.scan_task_history(base_dir)
        }

        complete = records["Complete Video"]
        partial = records["Partial Video"]
        failed = records["Failed Video"]
        self.assertEqual(complete["source"], source_url)
        self.assertEqual(complete["status"], "全部完成")
        self.assertIn("字幕", complete["completed"])
        self.assertIn("压制", complete["completed"])
        self.assertIn("GDrive", complete["completed"])
        self.assertIn("B站", complete["completed"])
        self.assertIn("微信", complete["completed"])
        self.assertIn("去广告", complete["completed"])
        self.assertEqual(complete["missing"], [])

        self.assertEqual(partial["source"], partial_source)
        self.assertEqual(partial["status"], "本地完成，上传未完成")
        self.assertEqual(partial["missing"], ["GDrive", "B站", "微信"])
        self.assertEqual(failed["status"], "未生成字幕和压制视频")

    def test_history_index_survives_app_update(self):
        """History records persist outside the app's install directory."""
        import autosub_gui

        path = os.path.join(self.temp_dir, "history.json")
        record = {
            "workdir": "/moved/project",
            "name": "Moved Project",
            "source": "https://example.com/moved",
            "updated_at": "2026-10-16 12:00",
            "status": "全部完成",
            "completed": ["字幕", "压制"],
            "missing": [],
        }

        autosub_gui.save_history_index([record], path)
        loaded = autosub_gui.load_history_index(path)
        merged = autosub_gui.merge_history_records(
            loaded,
            [{"workdir": "/new/project", "updated_at": "2026-10-16 11:00"}],
        )

        self.assertEqual(loaded, [record])
        self.assertEqual(merged[0], record)
        self.assertEqual(len(merged), 2)

    def test_startup_input_keeps_only_unfinished_work(self):
        """A completed saved task clears the input; unfinished work remains."""
        import autosub_gui

        base_dir = os.path.join(self.temp_dir, "Projects")
        complete_dir = os.path.join(base_dir, "Complete Video")
        partial_dir = os.path.join(base_dir, "Partial Video")
        for workdir in (complete_dir, partial_dir):
            os.makedirs(workdir)

        complete_source = os.path.join(self.temp_dir, "complete.mp4")
        partial_source = os.path.join(self.temp_dir, "partial.mp4")
        for workdir, source in (
            (complete_dir, complete_source),
            (partial_dir, partial_source),
        ):
            with open(os.path.join(workdir, "workflow.log"), "w", encoding="utf-8") as handle:
                handle.write(f"--- 任务启动: {source} ---\n")
            with open(os.path.join(workdir, "video.srt"), "w", encoding="utf-8") as handle:
                handle.write("subtitle\n")
            with open(os.path.join(workdir, "video_hardsub.mp4"), "wb") as handle:
                handle.write(b"video")
        for marker in (".synced", ".bili_uploaded", ".wechat_uploaded"):
            with open(os.path.join(complete_dir, marker), "w", encoding="utf-8") as handle:
                handle.write("1")

        records = autosub_gui.scan_task_history(base_dir)
        requirements = ["GDrive", "B站", "微信"]

        self.assertEqual(
            autosub_gui.select_startup_input(records, complete_source, requirements),
            "",
        )
        self.assertEqual(
            autosub_gui.select_startup_input(records, partial_source, requirements),
            partial_source,
        )
        self.assertEqual(
            autosub_gui.select_startup_input(records, "", requirements),
            partial_source,
        )

        hardsub_path = os.path.join(partial_dir, "video_hardsub.mp4")
        record = autosub_gui.find_history_record_for_input(records, hardsub_path)
        self.assertEqual(record["source"], partial_source)
        record = autosub_gui.find_history_record_for_input(records, partial_dir)
        self.assertEqual(record["source"], partial_source)

    def test_project_completion_follows_enabled_uploads(self):
        """A task is complete only for uploads the current run requested."""
        import autosub_gui

        record = {
            "completed": ["字幕", "压制", "B站"],
            "missing": ["GDrive", "微信"],
        }
        self.assertTrue(autosub_gui.is_project_complete(record, ["B站"]))
        self.assertFalse(autosub_gui.is_project_complete(record, ["微信"]))
        self.assertFalse(autosub_gui.is_project_complete(record, ["GDrive", "B站"]))

    def test_gdrive_sync_excludes_ad_cut_directory(self):
        """Google Drive receives the full project, not the noads derivative."""
        process = MagicMock()
        process.stdout = []
        process.wait.return_value = 0
        process.returncode = 0

        with patch.object(autosub.subprocess, "Popen", return_value=process) as mock_popen:
            autosub.trigger_sync("/tmp/workdir", "remote-id", headless=True)

        cmd = mock_popen.call_args.args[0]
        exclude_index = cmd.index("--exclude")
        self.assertEqual(cmd[exclude_index + 1], "noads")

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

        self.assertEqual(engine.reset_task(task), "MG")
        self.assertEqual(task.status, "排队中 (MG)")
        self.assertEqual(task.pcts["GD"], 100.0)
        self.assertEqual(task.pcts["BI"], 0.0)
        self.assertEqual(task.pcts["WX"], 0.0)
        self.assertTrue(os.path.exists(os.path.join(workdir, ".synced")))

    def test_parse_reset_target_accepts_explicit_stage(self):
        from autosub_batch_pro import parse_reset_target

        self.assertEqual(parse_reset_target("15"), (15, None))
        self.assertEqual(parse_reset_target("15tl"), (15, "TL"))

        with self.assertRaises(ValueError):
            parse_reset_target("15XX")

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

    def test_disk_truth_does_not_promote_partial_download(self):
        """A merged partial MP4 must stay in DL until it matches remote duration."""
        workdir = os.path.join(self.temp_dir, "partial_download")
        os.makedirs(workdir)
        source = os.path.join(workdir, "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"A" * (2 * 1024 * 1024))

        task = SubTask(
            uid=7,
            url="http://test.com",
            title="Partial Video",
            vid_id="partial",
            status="排队中 (DL)",
            duration="25:45",
            workdir=workdir,
            pcts={"DL": 65.3, "TR": 0.0, "TL": 0.0, "MR": 0.0, "BR": 0.0, "GD": 0.0},
        )
        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")

        with patch.object(BatchEngine, "get_duration", return_value="16:52"):
            engine.disk_truth(task)

        self.assertEqual(task.status, "排队中 (DL)")
        self.assertEqual(task.pcts["DL"], 65.5)
        self.assertIn("源视频时长不完整", task.error)
        self.assertFalse(os.path.exists(source))
        backups = [
            entry for entry in os.listdir(workdir)
            if entry.startswith(".invalid-partial-run-")
        ]
        self.assertEqual(len(backups), 1)
        self.assertTrue(
            os.path.exists(os.path.join(workdir, backups[0], "source.mp4"))
        )

    def test_partial_source_can_be_quarantined_for_retry(self):
        """Incomplete merged downloads move to the .part namespace before retry."""
        workdir = os.path.join(self.temp_dir, "quarantine_partial")
        os.makedirs(workdir)
        source = os.path.join(workdir, "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"partial")

        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")
        partial = engine.quarantine_partial_source(source)

        self.assertFalse(os.path.exists(source))
        self.assertTrue(os.path.exists(partial))
        self.assertTrue(partial.endswith(".part"))

    def test_completed_partial_download_requires_manual_reset(self):
        """A completed task built from a partial source must not auto re-upload."""
        workdir = os.path.join(self.temp_dir, "completed_partial")
        os.makedirs(workdir)
        source = os.path.join(workdir, "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"A" * (2 * 1024 * 1024))

        task = SubTask(
            uid=8,
            url="http://test.com",
            title="Completed Partial",
            vid_id="completed-partial",
            status="完成",
            duration="25:45",
            workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0, "BR": 100.0, "GD": 100.0},
        )
        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")

        with patch.object(BatchEngine, "get_duration", return_value="16:52"):
            engine.disk_truth(task)

        self.assertEqual(task.status, "失败 (DL)")
        self.assertIn("人工重置", task.error)
        self.assertTrue(os.path.exists(source))
        self.assertEqual(task.pcts["DL"], 65.5)

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

    def test_disk_truth_queues_remaining_uploads_after_gd(self):
        """A finished GD sync must not make remaining BI/WX look like GD work."""
        workdir = os.path.join(self.temp_dir, "task_remaining_uploads")
        os.makedirs(workdir)
        with open(os.path.join(workdir, "video_hardsub.mp4"), "w") as handle:
            handle.write("A" * 2 * 1024 * 1024)
        for marker in (".burn_complete", ".synced"):
            with open(os.path.join(workdir, marker), "w", encoding="utf-8") as handle:
                handle.write("1")

        task = SubTask(
            uid=6,
            url="http://test.com",
            title="Video 6",
            vid_id="vid6",
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
        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")
        engine.do_gdsync = True
        engine.do_bili = True
        engine.do_wechat = True

        with patch.object(BatchEngine, "get_duration", return_value="00:00:10"):
            engine.disk_truth(task)

        self.assertEqual(task.status, "排队中 (MG)")
        self.assertEqual(task.pcts["GD"], 100.0)
        self.assertEqual(task.pcts["BI"], 0.0)
        self.assertEqual(task.pcts["WX"], 0.0)

    def test_disk_truth_keeps_mg_complete_after_bili_success(self):
        """A successful BI upload must not reset shared metadata progress."""
        workdir = os.path.join(self.temp_dir, "task_bili_done_wx_pending")
        os.makedirs(workdir)
        with open(os.path.join(workdir, "video_hardsub.mp4"), "w") as handle:
            handle.write("A" * 2 * 1024 * 1024)
        for marker in (".burn_complete", ".bili_uploaded"):
            with open(os.path.join(workdir, marker), "w", encoding="utf-8") as handle:
                handle.write("1")

        task = SubTask(
            uid=7,
            url="http://test.com",
            title="Video 7",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "MG": 0.0,
                "BI": 100.0,
                "WX": 0.0,
            },
        )
        engine = BatchEngine(output_dir=self.temp_dir, sub_dir_name="project_1")
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = True

        with patch.object(BatchEngine, "get_duration", return_value="00:00:10"):
            engine.disk_truth(task)

        self.assertEqual(task.status, "排队中 (MG)")
        self.assertEqual(task.pcts["MG"], 100.0)
        self.assertEqual(task.pcts["BI"], 100.0)
        self.assertEqual(task.pcts["WX"], 0.0)

    def test_worker_enters_upload_pipeline_from_remaining_bi_wx_status(self):
        """A restarted task with only BI/WX left must run uploads, not wait as GD."""
        workdir = os.path.join(self.temp_dir, "worker_remaining_bi_wx")
        os.makedirs(workdir)
        task = SubTask(
            uid=7,
            url="http://test.com",
            title="Video 7",
            duration="01:00",
            status="排队中 (BI/WX)",
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

        metadata = {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体",
        }
        observed_statuses = []
        calls = []
        upload_statuses = []

        def fake_disk_truth(current_task):
            if current_task.status.startswith("排队中"):
                return
            current_task.status = "完成"

        def fake_bili_metadata(*_args, **_kwargs):
            observed_statuses.append(task.status)
            return {
                "title": metadata["title"],
                "desc": metadata["description"],
                "cover_title": metadata["cover_title"],
            }

        def fake_wechat_metadata(*_args, **_kwargs):
            return {
                "wechat_title": metadata["wechat_title"],
                "desc": metadata["description"],
                "cover_title": metadata["cover_title"],
            }

        def fake_bili_run(_task, _cmd, result_box):
            calls.append("BI")
            upload_statuses.append(task.status)
            result_box[0] = True

        def fake_wechat_run(_task, _cmd, result_box):
            calls.append("WX")
            upload_statuses.append(task.status)
            result_box[0] = True

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(batch_pro, "prepare_bili_upload_metadata", side_effect=fake_bili_metadata), \
             patch.object(batch_pro, "prepare_wechat_upload_metadata", side_effect=fake_wechat_metadata), \
             patch.object(batch_pro, "build_bili_cmd", return_value=["bili-upload"]), \
             patch.object(batch_pro, "build_wechat_cmd", return_value=["wechat-upload"]), \
             patch.object(batch_pro, "get_wechat_upload_skip_reason", return_value=None), \
             patch.object(BatchEngine, "_run_bili_upload", side_effect=fake_bili_run), \
             patch.object(BatchEngine, "_run_wechat_upload", side_effect=fake_wechat_run):
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(observed_statuses, ["运行中 (MG)"])
        self.assertEqual(set(upload_statuses), {"运行中 (MG)"})
        self.assertEqual(set(calls), {"BI", "WX"})
        self.assertEqual(task.status, "完成")
        for marker in (".bili_uploaded", ".wechat_uploaded"):
            self.assertTrue(os.path.exists(os.path.join(workdir, marker)))

    def test_login_failures_stop_upload_retry(self):
        """Expired login states are terminal and must not consume upload retries."""
        workdir = os.path.join(self.temp_dir, "worker_login_failed")
        os.makedirs(workdir)
        task = SubTask(
            uid=12,
            url="http://test.com",
            title="Video 12",
            duration="01:00",
            status="排队中 (BI/WX)",
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
        calls = []

        def fake_disk_truth(current_task):
            if current_task.status.startswith("排队中"):
                return
            current_task.status = "完成"

        def fake_bili_run(_task, _cmd, result_box):
            calls.append("BI")
            result_box[0] = False
            result_box[1] = "B站 Bilibili (BI) 登录态失效，请先执行: login.py"

        def fake_wechat_run(_task, _cmd, result_box):
            calls.append("WX")
            result_box[0] = False
            result_box[1] = "视频号 WeChat (WX) 登录态失效，请先执行: wechat_upload.py --login"

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(batch_pro, "prepare_bili_upload_metadata", return_value={
                 "title": "OpenAI研究员谈智能体",
                 "desc": "共享简介",
                 "cover_title": "智能体",
             }), \
             patch.object(batch_pro, "prepare_wechat_upload_metadata", return_value={
                 "wechat_title": "OpenAI研究员谈智能体",
                 "desc": "共享简介",
                 "cover_title": "智能体",
             }), \
             patch.object(batch_pro, "build_bili_cmd", return_value=["bili-upload"]), \
             patch.object(batch_pro, "build_wechat_cmd", return_value=["wechat-upload"]), \
             patch.object(batch_pro, "get_wechat_upload_skip_reason", return_value=None), \
             patch.object(BatchEngine, "_run_bili_upload", side_effect=fake_bili_run), \
             patch.object(BatchEngine, "_run_wechat_upload", side_effect=fake_wechat_run):
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(set(calls), {"BI", "WX"})
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.status, "失败 (上传)")
        self.assertIn("login.py", task.error)
        self.assertIn("wechat_upload.py --login", task.error)

    def test_bili_marker_written_without_waiting_for_wechat(self):
        """A finished BI upload is marked at once, so a retry cannot repost it.

        Markers used to be written only after every parallel upload thread joined.
        A BI upload that finished while WX was still running stayed unmarked, and a
        restart or retry in that window uploaded the same video to Bilibili again.
        """
        workdir = os.path.join(self.temp_dir, "task_marker")
        os.makedirs(workdir)
        task = SubTask(
            uid=8,
            url="http://test.com",
            title="Video 8",
            vid_id="vid8",
            workdir=workdir,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.task_map[task.uid] = task
        result_box = [False]

        with patch.object(batch_pro, "run_bili_cmd", return_value=True):
            engine._run_bili_upload(task, ["bili-upload"], result_box)

        self.assertTrue(result_box[0])
        self.assertEqual(task.pcts["BI"], 100.0)
        self.assertTrue(os.path.exists(os.path.join(workdir, ".bili_uploaded")))

    def test_failed_bili_upload_writes_no_marker(self):
        """A failed upload must stay unmarked so the retry really re-uploads."""
        workdir = os.path.join(self.temp_dir, "task_marker_failed")
        os.makedirs(workdir)
        task = SubTask(
            uid=9,
            url="http://test.com",
            title="Video 9",
            vid_id="vid9",
            workdir=workdir,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.task_map[task.uid] = task
        # Callers initialize the box to False; a failure must leave it that way.
        result_box = [False]

        with patch.object(batch_pro, "run_bili_cmd", return_value=False):
            engine._run_bili_upload(task, ["bili-upload"], result_box)

        self.assertFalse(result_box[0])
        self.assertEqual(task.error, "BI upload failed")
        self.assertFalse(os.path.exists(os.path.join(workdir, ".bili_uploaded")))

    def test_bili_login_failure_surfaces_recovery_command(self):
        """The batch worker must return the Bilibili QR-login command."""
        workdir = os.path.join(self.temp_dir, "task_bili_login_failed")
        os.makedirs(workdir)
        task = SubTask(
            uid=10,
            url="http://test.com",
            title="Video 10",
            vid_id="vid10",
            workdir=workdir,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.task_map[task.uid] = task
        result_box = [False, None]

        def fake_run(_cmd, on_log=None, **_kwargs):
            on_log("接口返回错误代码：-101，信息：账号未登录")
            return False

        with patch.object(batch_pro, "run_bili_cmd", side_effect=fake_run):
            engine._run_bili_upload(task, ["bili-upload"], result_box)

        self.assertFalse(result_box[0])
        self.assertIn("登录态失效", result_box[1])
        self.assertIn("login.py", result_box[1])
        self.assertEqual(task.error, result_box[1])
        self.assertFalse(os.path.exists(os.path.join(workdir, ".bili_uploaded")))

    def test_bili_cookie_preflight_stops_before_upload(self):
        """Missing local cookie fields must not start or retry the uploader."""
        workdir = os.path.join(self.temp_dir, "task_bili_cookie_missing")
        os.makedirs(workdir)
        with open(os.path.join(workdir, "video_hardsub.mp4"), "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, ".burn_complete"), "w") as handle:
            handle.write("1")
        task = SubTask(
            uid=25,
            url="http://test.com",
            title="Video 25",
            vid_id="vid25",
            status="排队中 (BI)",
            workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                  "BR": 100.0, "GD": 100.0, "BI": 0.0, "WX": 100.0},
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = False
        engine.task_map[task.uid] = task

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(
                    batch_pro,
                    "get_bili_login_skip_reason",
                    return_value="Bilibili cookies missing: SESSDATA, bili_jct",
                ), \
             patch.object(BatchEngine, "disk_truth", lambda self, current_task: None), \
             patch.object(batch_pro, "build_bili_cmd") as mock_build:
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        mock_build.assert_not_called()
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.status, "失败 (上传)")
        self.assertIn("登录态失效", task.error)
        self.assertIn("login.py", task.error)
        self.assertIn("SESSDATA", task.error)

    def test_wechat_login_failure_surfaces_recovery_command(self):
        """An expired WeChat login must open the visible QR-login flow."""
        workdir = os.path.join(self.temp_dir, "task_wechat_login_failed")
        os.makedirs(workdir)
        task = SubTask(
            uid=11,
            url="http://test.com",
            title="Video 11",
            vid_id="vid11",
            status="运行中 (WX)",
            workdir=workdir,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.task_map[task.uid] = task
        result_box = [False, None]
        commands = []

        def fake_run(cmd, on_log=None, **_kwargs):
            commands.append(list(cmd))
            if len(commands) == 1:
                on_log("微信登录态不可用或发布页加载失败；无头上传已停止")
                return False
            on_log(">>> 登录超时")
            return False

        with patch.object(
                    batch_pro,
                    "build_wechat_login_cmd",
                    return_value=["wechat-login"],
                ), \
             patch.object(batch_pro, "run_wechat_cmd", side_effect=fake_run):
            engine._run_wechat_upload(task, ["wechat-upload"], result_box)

        self.assertFalse(result_box[0])
        self.assertEqual(commands, [["wechat-upload"], ["wechat-login"]])
        self.assertIn("登录态失效", result_box[1])
        self.assertIn("wechat_upload.py --login", result_box[1])
        self.assertEqual(task.error, result_box[1])
        self.assertEqual(task.status, "运行中 (WX)")
        self.assertFalse(os.path.exists(os.path.join(workdir, ".wechat_uploaded")))

    def test_wechat_login_success_retries_upload_in_same_task(self):
        """A completed QR login must automatically retry the headless upload."""
        workdir = os.path.join(self.temp_dir, "task_wechat_login_retry")
        os.makedirs(workdir)
        task = SubTask(
            uid=12,
            url="http://test.com",
            title="Video 12",
            vid_id="vid12",
            status="运行中 (BI/WX)",
            workdir=workdir,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.task_map[task.uid] = task
        result_box = [False, None]
        commands = []
        observed_login_status = None

        def fake_run(cmd, on_log=None, on_progress=None, **_kwargs):
            commands.append(list(cmd))
            if len(commands) == 1:
                on_log("微信登录态不可用或发布页加载失败；无头上传已停止")
                return False
            if len(commands) == 2:
                nonlocal observed_login_status
                observed_login_status = task.status
                on_log(">>> 登录成功！")
                return True
            on_progress(25.0)
            on_log(">>> 上传完成！")
            return True

        with patch.object(
                    batch_pro,
                    "build_wechat_login_cmd",
                    return_value=["wechat-login"],
                ), \
             patch.object(batch_pro, "run_wechat_cmd", side_effect=fake_run):
            engine._run_wechat_upload(task, ["wechat-upload"], result_box)

        self.assertTrue(result_box[0])
        self.assertEqual(
            commands,
            [["wechat-upload"], ["wechat-login"], ["wechat-upload"]],
        )
        self.assertEqual(observed_login_status, "等待登录 (WX)")
        self.assertEqual(task.status, "运行中 (BI/WX)")
        self.assertEqual(task.pcts["WX"], 100.0)
        self.assertIsNone(task.error)
        self.assertTrue(os.path.exists(os.path.join(workdir, ".wechat_uploaded")))

    def test_load_state_requeues_stale_wechat_limit_status(self):
        """A 超限 status left over from the removed cap must not stay terminal."""
        state_file = os.path.join(self.temp_dir, "batch_state.json")
        workdir = os.path.join(self.temp_dir, "task_stale_limit")
        os.makedirs(workdir)
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "10": {
                        "uid": 10,
                        "url": "http://test.com",
                        "title": "Video 10",
                        "vid_id": "vid10",
                        "duration": "10:12",
                        "status": "超限 (WX)",
                        "workdir": workdir,
                        "error": "视频时长 10.2 分钟，超过视频号 10 分钟限制",
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
                    }
                },
                handle,
                ensure_ascii=False,
            )

        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.do_wechat = True
        engine.load_state()

        task = engine.task_map[10]
        self.assertEqual(task.status, "排队中 (WX)")
        self.assertIsNone(task.error)
        self.assertIsNone(task.wx_skip_reason)
        self.assertEqual(task.retries, 0)

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

    def _make_single_stage_engine(self, name, metadata):
        """Build a workdir, a BatchEngine, and the patches its cmd builders need.

        batch_pro imports the upload helpers from a top-level ``autosub`` module
        object that is distinct from ``Library.Tools.autosub.autosub``, so the
        script/path constants are patched in the builders' own globals.
        """
        workdir = os.path.join(self.temp_dir, name)
        os.makedirs(workdir)
        with open(os.path.join(workdir, "video_hardsub.mp4"), "w") as handle:
            handle.write("video")
        bili_script = os.path.join(self.temp_dir, f"{name}_bili_upload.py")
        wechat_script = os.path.join(self.temp_dir, f"{name}_wechat_upload.py")
        for script in (bili_script, wechat_script):
            with open(script, "w") as handle:
                handle.write("# placeholder")
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        stack = contextlib.ExitStack()
        stack.enter_context(
            patch.dict(
                batch_pro.build_wechat_cmd.__globals__,
                {
                    "BILI_SCRIPT": bili_script,
                    "BILI_PYTHON": sys.executable,
                    "WECHAT_SCRIPT": wechat_script,
                    "WECHAT_PYTHON": sys.executable,
                    "get_video_duration": lambda _path: 600,
                },
            )
        )
        def fake_bili_metadata(*_args, **_kwargs):
            if not all(str(metadata.get(field) or "").strip() for field in (
                "title", "description", "cover_title"
            )):
                return None
            return {
                "title": metadata["title"],
                "desc": metadata["description"],
                "cover_title": metadata["cover_title"],
            }

        def fake_wechat_metadata(*_args, **_kwargs):
            if not all(str(metadata.get(field) or "").strip() for field in (
                "wechat_title", "description", "cover_title"
            )):
                return None
            return {
                "wechat_title": metadata["wechat_title"],
                "desc": metadata["description"],
                "cover_title": metadata["cover_title"],
            }

        stack.enter_context(
            patch.object(
                batch_pro,
                "prepare_bili_upload_metadata",
                side_effect=fake_bili_metadata,
            )
        )
        stack.enter_context(
            patch.object(
                batch_pro,
                "prepare_wechat_upload_metadata",
                side_effect=fake_wechat_metadata,
            )
        )
        self.addCleanup(stack.close)
        return workdir, engine

    def test_single_stage_upload_cmds_reuse_pregenerated_metadata(self):
        """A lone remaining stage must not fall back to uploader-side LLM titles."""
        workdir, engine = self._make_single_stage_engine("single_stage_meta", {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "智能体",
            "description": "共享简介",
            "cover_title": "封面标题",
        })
        bili_cmd = engine._build_bili_cmd(workdir)
        wechat_cmd = engine._build_wechat_cmd(workdir)

        self.assertIsNotNone(bili_cmd)
        self.assertIsNotNone(wechat_cmd)
        self.assertEqual(bili_cmd[4], "OpenAI研究员谈智能体")
        self.assertEqual(bili_cmd[bili_cmd.index("--desc") + 1], "共享简介")
        self.assertEqual(bili_cmd[bili_cmd.index("--cover-title") + 1], "封面标题")
        self.assertIn("--no_auto_title", bili_cmd)
        self.assertEqual(wechat_cmd[4], "智能体")
        self.assertEqual(wechat_cmd[wechat_cmd.index("--desc") + 1], "共享简介")
        self.assertEqual(wechat_cmd[wechat_cmd.index("--cover-title") + 1], "封面标题")
        self.assertIn("--no_auto_title", wechat_cmd)

    def test_single_stage_upload_cmds_refuse_incomplete_metadata(self):
        """Incomplete metadata stops a retry instead of publishing a guessed title."""
        workdir, engine = self._make_single_stage_engine("single_stage_gap", {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "智能体",
            "description": "  ",
            "cover_title": "封面标题",
        })
        self.assertIsNone(engine._build_bili_cmd(workdir))
        self.assertIsNone(engine._build_wechat_cmd(workdir))

    def test_single_stage_wechat_cmd_requires_wechat_title(self):
        """A missing wechat_title blocks WX only, matching the parallel pipeline."""
        workdir, engine = self._make_single_stage_engine("single_stage_wxgap", {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "",
            "description": "共享简介",
            "cover_title": "封面标题",
        })
        self.assertIsNotNone(engine._build_bili_cmd(workdir))
        self.assertIsNone(engine._build_wechat_cmd(workdir))

    def test_batch_worker_starts_gd_and_bili_before_wechat_metadata(self):
        """GD and BI start independently before WeChat's short title is derived."""
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
        bili_started = threading.Event()

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

        def fake_bili_metadata(*_args, **_kwargs):
            calls.append("META")
            meta_started.set()
            if not release_meta.wait(2):
                raise TimeoutError("metadata generation stayed blocked")
            return {
                "title": metadata["title"],
                "desc": metadata["description"],
                "cover_title": metadata["cover_title"],
            }

        def fake_wechat_metadata(*_args, **_kwargs):
            if not bili_started.wait(2):
                raise TimeoutError("Bilibili upload did not start first")
            calls.append("WX_META")
            return {
                "wechat_title": metadata["wechat_title"],
                "desc": metadata["description"],
                "cover_title": metadata["cover_title"],
            }

        def fake_bili_cmd(*_args, **_kwargs):
            return ["bili-upload"]

        def fake_wechat_cmd(*_args, **_kwargs):
            return ["wechat-upload"]

        def fake_bili_run(*_args, **_kwargs):
            calls.append("BI")
            bili_started.set()
            return True

        def fake_wechat_run(*_args, **_kwargs):
            calls.append("WX")
            return True

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        try:
            with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
                 patch.object(BatchEngine, "run_process", side_effect=fake_run_process), \
                 patch.object(batch_pro, "prepare_bili_upload_metadata", side_effect=fake_bili_metadata), \
                 patch.object(batch_pro, "prepare_wechat_upload_metadata", side_effect=fake_wechat_metadata), \
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
            self.assertIn("WX_META", calls)
            self.assertLess(calls.index("GD"), calls.index("BI"))
            self.assertLess(calls.index("META"), calls.index("BI"))
            self.assertLess(calls.index("BI"), calls.index("WX_META"))
            for marker in (".synced", ".bili_uploaded", ".wechat_uploaded"):
                self.assertTrue(os.path.exists(os.path.join(workdir, marker)))
            self.assertEqual(task.status, "完成")
        finally:
            release_meta.set()
            worker.join(2)

    def test_batch_worker_keeps_bili_when_wechat_metadata_is_incomplete(self):
        """A WeChat title failure must not stop the already-publishable BI upload."""
        workdir = os.path.join(self.temp_dir, "batch_wechat_meta_gap")
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

        def fake_bili_run(current_task, _cmd, result_box):
            result_box[0] = True
            engine._write_upload_marker(current_task, "BI")

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(batch_pro, "prepare_bili_upload_metadata", return_value={
                 "title": "OpenAI研究员谈智能体",
                 "desc": "共享简介",
                 "cover_title": "智能体",
             }), \
             patch.object(batch_pro, "prepare_wechat_upload_metadata", return_value=None), \
             patch.object(batch_pro, "build_bili_cmd") as mock_bili, \
             patch.object(batch_pro, "build_wechat_cmd") as mock_wechat, \
             patch.object(BatchEngine, "_run_bili_upload", side_effect=fake_bili_run):
            worker.start()
            worker.join(15)

        self.assertFalse(worker.is_alive())
        mock_bili.assert_called_once()
        mock_wechat.assert_not_called()
        self.assertEqual(task.status, "失败 (WX)")
        self.assertEqual(task.retries, 0)
        self.assertTrue(task.wx_metadata_failed)
        self.assertTrue(os.path.exists(os.path.join(workdir, ".bili_uploaded")))
        self.assertFalse(os.path.exists(os.path.join(workdir, ".wechat_uploaded")))

    def test_reset_after_wechat_metadata_failure_targets_wx_only(self):
        """R+ID must preserve a successful BI upload when WX metadata failed."""
        workdir = os.path.join(self.temp_dir, "batch_reset_wx_metadata")
        os.makedirs(workdir)
        metadata_path = os.path.join(workdir, "upload_metadata.json")
        bili_marker = os.path.join(workdir, ".bili_uploaded")
        with open(metadata_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(bili_marker, "w", encoding="utf-8") as handle:
            handle.write("1")

        state_file = os.path.join(self.temp_dir, "wx_metadata_state.json")
        task = SubTask(
            uid=25,
            url="http://test.com",
            title="Video 25",
            status="失败 (WX)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "MG": 60.0,
                "BI": 100.0,
                "WX": 0.0,
            },
            wx_metadata_failed=True,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        self.assertEqual(engine.reset_task(task), "WX")
        self.assertEqual(task.status, "排队中 (WX)")
        self.assertTrue(os.path.exists(bili_marker))
        self.assertTrue(os.path.exists(metadata_path))
        self.assertEqual(task.pcts["BI"], 100.0)
        self.assertEqual(task.pcts["MG"], 60.0)

        restarted_engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        restarted_engine.do_gdsync = False
        restarted_engine.do_bili = True
        restarted_engine.do_wechat = True
        restarted_engine.load_state()
        self.assertEqual(restarted_engine.task_map[25].status, "排队中 (WX)")

    def test_old_state_migrates_wechat_metadata_failure_to_wx_retry(self):
        """A pre-upgrade task 25 state must not reset BI through MG."""
        workdir = os.path.join(self.temp_dir, "old_wx_metadata_state")
        os.makedirs(workdir)
        with open(os.path.join(workdir, ".bili_uploaded"), "w", encoding="utf-8") as handle:
            handle.write("1")

        task_data = {
            "uid": 25,
            "url": "http://test.com",
            "title": "Video 25",
            "status": "失败 (WX)",
            "error": "视频号元数据生成失败或不完整，WX上传未启动",
            "workdir": workdir,
            "pcts": {
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "MG": 60.0,
                "BI": 100.0,
                "WX": 0.0,
            },
        }
        state_file = os.path.join(self.temp_dir, "old_batch_state.json")
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump({"25": task_data, "bili": True, "wechat": True}, handle)

        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.load_state()

        task = engine.task_map[25]
        self.assertTrue(task.wx_metadata_failed)
        self.assertEqual(task.status, "排队中 (WX)")
        self.assertEqual(engine.reset_task(task), "WX")
        self.assertTrue(os.path.exists(os.path.join(workdir, ".bili_uploaded")))

    def test_wechat_metadata_retry_prefers_local_fallback(self):
        """A WX metadata retry must not loop through remote title models."""
        workdir = os.path.join(self.temp_dir, "wechat_local_retry")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w", encoding="utf-8") as handle:
            handle.write("video")
        metadata = {
            "source_title": "A Conversation: Palantir Meritocracy Fellows",
            "description": (
                "Palantir在Tribeca直播对话三位Meritocracy Fellowship成员。"
                "这期25人的项目让年轻人跳过大学，直接参与核心业务。"
            ),
            "title": "Palantir在Tribeca直播对话三位Meritocracy Fellowship成员",
            "cover_title": "Isaac他们为Hertz做的东西，让客户每天省下了X个小时的工作量",
            "wechat_title": "",
        }
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False)

        result = autosub.prepare_wechat_upload_metadata(
            workdir,
            prefer_local_wechat_fallback=True,
        )

        self.assertEqual(result["wechat_title"], "Palantir培养年轻人才")
        with open(os.path.join(workdir, "upload_metadata.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["wechat_title"], "Palantir培养年轻人才")

    def test_wechat_metadata_uses_publisher_title_after_bili_success(self):
        """A completed BI upload keeps WX recoverable with a source-title prefix."""
        workdir = os.path.join(self.temp_dir, "wechat_publisher_fallback")
        os.makedirs(workdir)
        with open(os.path.join(workdir, ".bili_uploaded"), "w", encoding="utf-8") as handle:
            handle.write("1")
        metadata = {
            "source_title": "How To Take Enterprise AI Pilots To Production — With Russell Jukes",
            "title": "Russell联合Amazon共同创新，通过自研与采购互补的AI技术栈为企业解决落地问题",
            "description": "共享简介",
            "cover_title": "封面标题",
            "wechat_title": "",
        }

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata):
            result = autosub.prepare_wechat_upload_metadata(
                workdir,
                source_title=(
                    "How To Take Enterprise AI Pilots To Production — "
                    "With Russell Jukes"
                ),
            )

        self.assertEqual(result["wechat_title"], "How To Take")
        self.assertLessEqual(len(result["wechat_title"]), 16)
        with open(os.path.join(workdir, "upload_metadata.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["wechat_title"], "How To Take")

    def test_wechat_only_retry_reports_wx_not_mg(self):
        """A lone WX retry is a WX stage even while its metadata is rebuilt."""
        workdir = os.path.join(self.temp_dir, "wx_only_retry")
        os.makedirs(workdir)
        with open(os.path.join(workdir, ".bili_uploaded"), "w", encoding="utf-8") as handle:
            handle.write("1")
        task = SubTask(
            uid=25,
            url="http://test.com",
            title="Video 25",
            status="排队中 (WX)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "MG": 0.0,
                "BI": 100.0,
                "WX": 0.0,
            },
            wx_metadata_failed=True,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "wx_only_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task
        observed_statuses = []

        def fake_wechat_metadata(_workdir, **kwargs):
            observed_statuses.append(task.status)
            self.assertTrue(kwargs["prefer_local_wechat_fallback"])
            return {
                "wechat_title": "Palantir培养年轻人才",
                "desc": "共享简介",
                "cover_title": "封面标题",
            }

        def fake_wechat_run(current_task, _cmd, result_box):
            result_box[0] = True
            engine._write_upload_marker(current_task, "WX")
            return True

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(
            BatchEngine,
            "disk_truth",
            side_effect=lambda current_task: setattr(
                current_task,
                "status",
                "完成" if os.path.exists(os.path.join(workdir, ".wechat_uploaded")) else current_task.status,
            ),
        ), \
             patch.object(batch_pro, "prepare_wechat_upload_metadata", side_effect=fake_wechat_metadata), \
             patch.object(batch_pro, "build_wechat_cmd", return_value=["wechat-upload"]), \
             patch.object(BatchEngine, "_run_wechat_upload", side_effect=fake_wechat_run):
            worker.start()
            worker.join(15)

        self.assertFalse(worker.is_alive())
        self.assertEqual(observed_statuses, ["运行中 (WX)"])
        self.assertEqual(task.status, "完成")
        self.assertTrue(os.path.exists(os.path.join(workdir, ".wechat_uploaded")))

    def test_explicit_mg_reset_preserves_successful_platform_markers(self):
        """Regenerating metadata must not invalidate an already-published BI upload."""
        workdir = os.path.join(self.temp_dir, "batch_reset_mg_preserve_bi")
        os.makedirs(workdir)
        metadata_path = os.path.join(workdir, "upload_metadata.json")
        bili_marker = os.path.join(workdir, ".bili_uploaded")
        with open(metadata_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(bili_marker, "w", encoding="utf-8") as handle:
            handle.write("1")

        task = SubTask(
            uid=25,
            url="http://test.com",
            title="Video 25",
            status="失败 (WX)",
            workdir=workdir,
            pcts={
                "DL": 100.0,
                "TR": 100.0,
                "TL": 100.0,
                "MR": 100.0,
                "BR": 100.0,
                "GD": 100.0,
                "MG": 60.0,
                "BI": 100.0,
                "WX": 0.0,
            },
            wx_metadata_failed=True,
        )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "mg_reset_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = True
        engine.task_map[task.uid] = task

        self.assertEqual(engine.reset_task(task, stage="MG"), "MG")
        self.assertEqual(task.status, "排队中 (MG)")
        self.assertTrue(os.path.exists(bili_marker))
        self.assertFalse(os.path.exists(metadata_path))
        self.assertEqual(task.pcts["BI"], 100.0)
        self.assertEqual(task.pcts["MG"], 0.0)
        self.assertFalse(task.wx_metadata_failed)

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
             patch.object(batch_pro, "prepare_wechat_upload_metadata", return_value={
                 "wechat_title": "OpenAI研究员谈智能体",
                 "desc": "共享简介",
                 "cover_title": "智能体",
             }), \
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
                 "prepare_bili_upload_metadata",
                 return_value={
                     "title": metadata["title"],
                     "desc": metadata["description"],
                     "cover_title": metadata["cover_title"],
                 },
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

    def test_bili_and_wechat_prefer_ad_cut_hardsub(self):
        """BI/WX publish the noads version; the full hardsub remains available."""
        workdir = os.path.join(self.temp_dir, "noads_upload")
        noads_dir = os.path.join(workdir, "noads")
        os.makedirs(noads_dir)
        full_video = os.path.join(workdir, "video_hardsub.mp4")
        noads_video = os.path.join(noads_dir, "video_hardsub.mp4")
        for path in (full_video, noads_video):
            with open(path, "w") as handle:
                handle.write("video")
        bili_script = os.path.join(self.temp_dir, "bili_upload.py")
        wechat_script = os.path.join(self.temp_dir, "wechat_upload.py")
        for path in (bili_script, wechat_script):
            with open(path, "w") as handle:
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

        self.assertEqual(bili_cmd[3], noads_video)
        self.assertEqual(wechat_cmd[3], noads_video)
        self.assertTrue(os.path.exists(full_video))

    def test_generate_upload_metadata_uses_llm_client_model_name(self):
        """The shared description and summary-title calls use the model name."""
        workdir = os.path.join(self.temp_dir, "metadata_llm_interface")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        llm_client = MagicMock()
        llm_client.generate_content.side_effect = [
            "共享简介：卡普谈Palantir数据整合，不做服务；客户没有销售团队也大规模采用。",
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
        subtitle_title.read_full_subtitle_text.return_value = (
            "卡普谈Palantir数据整合，不做服务。我们没有销售团队，却看到了大规模的采用。",
            "subtitle.srt",
        )
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

        self.assertEqual(
            metadata["description"],
            "共享简介：卡普谈Palantir数据整合，不做服务；客户没有销售团队也大规模采用。",
        )
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
        self.assertIn(
            "视频简介：\n共享简介：卡普谈Palantir数据整合，不做服务；客户没有销售团队也大规模采用。",
            llm_client.generate_content.call_args_list[1].args[0],
        )
        self.assertIn("优先 20-40 个字符", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("基于完整中文字幕和视频简介", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("列出全片 5-8 个主要内容", llm_client.generate_content.call_args_list[1].args[0])
        self.assertIn("生成 10 个候选", llm_client.generate_content.call_args_list[1].args[0])
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

    def test_cover_title_must_keep_source_title_anchor(self):
        source_title = (
            "Ondas Sentinel: Sovereign AI Is National Security | "
            "Sovereignty Bootcamp 3"
        )

        self.assertTrue(
            autosub._cover_title_has_source_title_issue(
                "模型在边缘端评估证据，操作员逐行审计决策包",
                source_title,
            )
        )
        self.assertFalse(
            autosub._cover_title_has_source_title_issue(
                "Ondas Sentinel：主权AI就是国家安全",
                source_title,
            )
        )

    def test_cover_title_fallback_translates_source_title(self):
        source_title = (
            "Ondas Sentinel: Sovereign AI Is National Security | "
            "Sovereignty Bootcamp 3"
        )
        llm_client = MagicMock()
        llm_client.generate_content.return_value = (
            "Ondas Sentinel：主权AI就是国家安全"
        )

        cover_title = autosub._fallback_cover_title_from_source_title(
            source_title,
            llm_client,
            "glm-5.2",
        )

        self.assertEqual(cover_title, "Ondas Sentinel：主权AI就是国家安全")

    def test_generic_wechat_fallback_compresses_announcement_title(self):
        """Announcement titles must still yield a short WeChat title offline."""
        bili_title = (
            "a16z宣布扩展加密基金为A16z Frontier，"
            "尽早押注极具野心的前沿创业者"
        )
        description = (
            "a16z宣布将 ACC加密基金扩展为 A16z Frontier。"
            "Chris Dixon指出，此举源于加密技术融入主流科技版图，"
            "以及团队需要覆盖落在垂直领域之外的“外行星”机会。"
            "Marc Andreessen与Ben Horowitz强调，当下AI能力飞速跃升，"
            "前沿极客与老牌企业间的认知落差已达职业生涯最大。"
            "如今30人团队就能打造全栈设备公司，"
            "创始人获得了前所未有的超级能力。"
        )

        subject = autosub._extract_bili_title_subject(bili_title)
        wechat_title = autosub._compress_bili_title_for_wechat(
            bili_title,
            16,
            description=description,
        )

        self.assertEqual(subject, "a16z")
        self.assertEqual(wechat_title, "a16z押注极具野心的前沿创业者")
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                wechat_title,
                "",
                True,
                description,
            )
        )

    def test_generic_wechat_fallback_compresses_beneficiary_title(self):
        """A product title must keep its subject, beneficiary, and action."""
        bili_title = "Skyweaver为操作员生成决策包，可逐行审计"
        description = (
            "在本期Sovereignty训练营中，Palantir与首个客户Ondas合作展示"
            "主权AI产品Skyweaver C4I。该系统在边缘端自主运行，抗断连与拒止。"
            "在北部湾非法捕捞演练中，模型于边缘端评估证据并生成可逐行审计的决策包，"
            "将最终决策权交还操作员。"
        )

        wechat_title = autosub._fallback_wechat_title_from_bili_title(
            bili_title,
            16,
        )

        self.assertEqual(wechat_title, "Skyweaver帮操作员做决策")
        self.assertLessEqual(len(wechat_title), 16)
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                wechat_title,
                "Ondas Sentinel: Sovereign AI Is National Security",
                True,
                description,
            )
        )

    def test_ontology_metadata_fallback_survives_llm_timeout(self):
        """The ontology summary must keep a concrete offline title."""
        description = (
            "你以为爱的是B，其实是脑海里的“B本体”。"
            "本期用A与B二选一的恋爱情境，讲透本体的本质："
            "我们比较、喜欢的，从来不是实体，而是自己构建的虚拟化身。"
        )

        title = autosub._fallback_bili_title_from_description(description, 80)
        wechat_title = autosub._fallback_wechat_title_from_bili_title(title, 16)

        self.assertEqual(title, "爱的不是B实体，而是脑海里的B本体")
        self.assertEqual(wechat_title, "爱的不是B 而是脑海里的B本体")
        self.assertFalse(autosub._metadata_title_has_quality_issue(
            title,
            "",
            True,
            description,
        ))

    def test_shared_title_gate_allows_grasp_predicate(self):
        """The structural “把” ban must not reject the verb “把握”."""
        self.assertFalse(
            autosub._shared_title_has_quality_issue(
                "人类为对象赋予意义并构建出本体，与实体的落差引出深度学习"
            )
        )
        self.assertTrue(
            autosub._shared_title_has_quality_issue("把数据做成产品")
        )

    def test_recent_interview_metadata_fallbacks_survive_llm_timeout(self):
        """The two weak-title cases must have concrete offline fallbacks."""
        sequence_description = (
            "本期《No Priors》专访 Sequence Holdings 联合创始人兼CEO。"
            "他们以77亿美元收购保险经纪公司 Baldwin，"
            "成为迄今规模最大的 AI 私有化交易。"
            "他详解永久持有控股公司的模式：以所有权取代服务商角色，"
            "靠自建工程平台 Atlas 改造在位企业。"
        )
        anthropic_description = (
            "对话 Anthropic 研究员。"
            "访谈讨论AGI安全、产出和2030年代的技术路径。"
        )

        self.assertEqual(
            autosub._fallback_bili_title_from_description(sequence_description, 48),
            "Sequence Holdings谈AI私有化：用所有权和工程平台改造在位企业",
        )
        self.assertEqual(
            autosub._fallback_wechat_title_from_description(sequence_description, 16),
            "Sequence用所有权改造企业",
        )
        self.assertEqual(
            autosub._fallback_cover_title_from_description(
                sequence_description,
                "Sequence Holdings谈AI私有化：用所有权和工程平台改造在位企业",
                48,
            ),
            "AI私有化最大交易：77亿美元收购Baldwin",
        )
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                "Sequence Holdings：77亿美元收购后，用所有权改造在位企业",
                "",
                True,
                sequence_description,
            )
        )
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                "Sequence Holdings谈AI私有化：用所有权和工程平台改造在位企业",
                "",
                True,
                sequence_description,
            )
        )
        self.assertTrue(
            autosub._cover_title_repeats_form_title(
                "Sequence：77亿美元收购后，用所有权改造在位企业",
                "Sequence Holdings：77亿美元收购后，用所有权改造在位企业",
            )
        )
        self.assertFalse(
            autosub._cover_title_repeats_form_title(
                "AI私有化最大交易：77亿美元收购Baldwin",
                "Sequence Holdings谈AI私有化：用所有权和工程平台改造在位企业",
            )
        )
        self.assertEqual(
            autosub._fallback_bili_title_from_description(anthropic_description, 48),
            "Anthropic研究员谈AGI安全与产出",
        )
        self.assertEqual(
            autosub._fallback_wechat_title_from_description(anthropic_description, 16),
            "Anthropic谈AGI安全",
        )
        self.assertEqual(
            autosub._fallback_wechat_title_from_source_title(
                "Anthropic Researchers - How AI Will Create A Radically Better 2030s",
                16,
            ),
            "",
        )

    def test_interview_fallbacks_use_public_chinese_subjects(self):
        """Tasks 6 and 9 must survive offline title generation."""
        thiel_description = (
            "Peter Thiel 回忆恩师 René Girard："
            "初闻模仿理论只觉太疯狂，多年琢磨后才体会到它强大得不可思议。"
            "这套思想后来塑造了他的商业直觉。"
        )
        self.assertEqual(
            autosub._fallback_bili_title_from_source_title(
                "Peter Thiel on René Girard",
                80,
                description=thiel_description,
            ),
            "蒂尔谈吉拉尔与模仿理论",
        )
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                "蒂尔谈吉拉尔与模仿理论",
                "Peter Thiel on René Girard",
                True,
                thiel_description,
            )
        )

        bili_title = "Marc Andreessen在访谈中直言旧的制造业岗位回不来"
        wechat_title = autosub._fallback_wechat_title_from_bili_title(
            bili_title,
            16,
        )
        self.assertEqual(wechat_title, "安德森谈旧的制造业岗位回不来")
        self.assertLessEqual(len(wechat_title), 16)
        self.assertTrue(autosub.normalize_shared_upload_title(wechat_title, 16))

    def test_person_interview_title_algorithm_prefers_mainline_hooks(self):
        """A person interview title must not collapse into one news event."""
        source_title = (
            "Ben Affleck s Turbulent Year - Navigating Grief "
            "The Art of Not Caring"
        )
        description = (
            "54岁的Ben Affleck敞开谈他“挺复杂的一年”：父母在两个月内相继离世，"
            "让他直面人生的转瞬即逝，也更笃定要把时间花在孩子身上。"
            "他推掉了本要执导的新戏，守在洛杉矶天天去Artists Equity上班。"
            "聊起小报疯传他家被烧、遭FBI突袭的假消息，他已练就"
            "“中年老爸该有的心态”。他还谈到与Matt Damon多年搭档的默契："
            "活得够久，人生本身就成了最丰富的素材库。"
        )
        grief_event_title = (
            "54岁的Ben Affleck敞开谈他挺复杂的一年"
            "父母在两个月内相继离世"
        )
        editorial_summary_title = "GQ访谈大本：好莱坞一线男星的中年感悟"
        spoken_hook_title = "GQ访谈大本：人生苦短，每天上班的感觉好极了"

        grief_score = autosub._title_candidate_score(
            grief_event_title,
            80,
            central_claim=description,
            description=description,
            source_title=source_title,
        )
        self.assertGreater(
            autosub._title_candidate_score(
                editorial_summary_title,
                80,
                central_claim=description,
                description=description,
                source_title=source_title,
            ),
            grief_score,
        )
        self.assertGreater(
            autosub._title_candidate_score(
                spoken_hook_title,
                80,
                central_claim=description,
                description=description,
                source_title=source_title,
            ),
            grief_score,
        )

        bili_title = autosub._fallback_bili_title_from_description(
            description,
            48,
        )
        self.assertEqual(
            bili_title,
            "大本访谈：好莱坞一线男星的中年感悟",
        )
        self.assertEqual(
            autosub._fallback_wechat_title_from_bili_title(bili_title, 16),
            "大本：人到中年的感悟",
        )
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                bili_title,
                source_title,
                True,
                description,
            )
        )
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                "大本：人到中年的感悟",
                source_title,
                True,
                description,
            )
        )
        self.assertEqual(
            autosub._fallback_interview_cover_title_from_description(
                description,
                form_title=bili_title,
                source_title=source_title,
                max_chars=48,
            ),
            "大本访谈：活得够久，人生成了素材库",
        )

        cached_meta = {
            "title": (
                "54岁的Ben Affleck敞开谈他挺复杂的一年"
                "父母在两个月内相继离世"
            ),
            "wechat_title": "54岁的大本敞开谈他挺复杂的一年",
            "cover_title": (
                "Ben：同时传言说FBI突袭了我家，又说我家烧没了"
            ),
            "description": description,
        }
        invalid_fields = autosub._invalidate_cached_upload_metadata(
            cached_meta,
            source_title,
            "54岁的Ben Affleck谈中年、上班和人生素材库。",
        )
        self.assertEqual(invalid_fields, ["title", "wechat_title", "cover_title"])
        self.assertEqual(cached_meta["title"], "")
        self.assertEqual(cached_meta["wechat_title"], "")
        self.assertEqual(cached_meta["cover_title"], "")

    def test_person_interview_subtitle_fallback_uses_representative_hook(self):
        """The short WeChat hook should choose the strongest life-stage line."""
        subtitle_text = (
            "人一到50，那肯定就没性生活了。\n"
            "人生都快到头了。我20岁的时候，觉得50岁的人跟我毫不相干。\n"
            "然后40岁、50岁，你会说，等等，怎么突然就打到人生第三节了。\n"
            "同时传言说FBI突袭了我家，又说我家烧没了。\n"
        )
        form_title = "GQ访谈大本：好莱坞一线男星的中年感悟"

        self.assertEqual(
            autosub._fallback_wechat_title_from_subtitle(
                subtitle_text,
                max_chars=16,
                form_title=form_title,
            ),
            "大本：人到中年没杏生活",
        )
        self.assertEqual(
            autosub._fallback_cover_title_from_subtitle(
                subtitle_text,
                form_title=form_title,
                source_title="Ben Affleck interview",
                description="54岁的Ben Affleck敞开谈中年心态。",
            ),
            "大本访谈：人一到50，就没性生活了",
        )
        self.assertEqual(
            autosub._anchor_cover_hook_with_context(
                "40岁、50岁，突然打到人生第三节",
                form_title=form_title,
                source_title="Ben Affleck interview",
                description="54岁的Ben Affleck敞开谈中年心态。",
            ),
            "大本访谈：40岁、50岁，突然打到人生第三节",
        )

    def test_person_interview_prompts_use_separate_editorial_rules(self):
        """Person interviews need their own prompt guidance and length gate."""
        llm_client = MagicMock()
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.parse_llm_title_candidates.return_value = (
            "GQ访谈大本：人生苦短，每天上班的感觉好极了",
            (
                (
                    "GQ访谈大本：人生苦短，每天上班的感觉好极了",
                    ("人生都快到头了。我每天都来上班。",),
                ),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "人生都快到头了。我每天都来上班。"
        )
        description = "54岁的Ben Affleck敞开谈中年心态、上班与人生素材库。"
        subtitle_text = "54岁的Ben Affleck谈中年、上班和人生素材库。"

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._generate_title_from_description(
                description,
                subtitle_text,
                subtitle_text,
                llm_client,
                "glm-5.2",
                48,
                source_title="Ben Affleck interview",
            )
            with self.assertRaises(ValueError):
                autosub._generate_cover_title_from_full_subtitle(
                    subtitle_text,
                    llm_client,
                    "glm-5.2",
                    form_title=title,
                    source_title="Ben Affleck interview",
                    description=description,
                )

        title_prompt = llm_client.generate_content.call_args_list[0].args[0]
        cover_prompt = llm_client.generate_content.call_args_list[-1].args[0]
        self.assertIn("人物访谈或人物特写", title_prompt)
        self.assertIn("不要套用企业产品标题模板", title_prompt)
        self.assertIn("Ben Affleck 用「大本」", title_prompt)
        self.assertIn("至少 3 个候选使用原话钩子", title_prompt)
        self.assertIn("顿号并列关键词不是自然编辑标题", title_prompt)
        self.assertIn("频道或栏目名是可选信号", title_prompt)
        self.assertIn("优先 20-40 个字符", title_prompt)
        self.assertIn("人物访谈", cover_prompt)
        self.assertIn("人生阶段、通勤上班、素材库", cover_prompt)
        self.assertIn("单一谣言或八卦事件", cover_prompt)
        self.assertIn("例如「大本」", cover_prompt)
        self.assertIn("人一到50，就没性生活了", cover_prompt)

    def test_wechat_hook_fallback_derives_short_title_from_bili_title(self):
        """The Palantir title can yield a 16-character WeChat hook offline."""
        source_title = "Palantir expands tech talent pipeline with Meritocracy Fellowship"
        description = (
            "科技公司不再只盯着名校学位，Palantir干脆自己培养人才："
            "其“唯才是举奖学金”为期四个月、带薪，去年近700名学生申请仅26个名额，"
            "促使公司扩大项目。项目负责人解释，许多才华横溢的年轻人"
            "不符合传统教育僵化狭窄的模子。"
        )
        bili_title = (
            "科技公司不再只盯着名校学位，"
            "Palantir干脆自己培养人才其唯才是举奖学金为期四个月、带薪"
        )
        wechat_title = autosub._fallback_wechat_hook_from_bili_title(
            bili_title, 16
        )

        self.assertEqual(wechat_title, "Palantir培养人才不看名校")
        self.assertEqual(len(wechat_title), 16)
        self.assertTrue(autosub.normalize_shared_upload_title(wechat_title, 16))
        self.assertFalse(
            autosub._metadata_title_has_quality_issue(
                wechat_title,
                source_title,
                True,
                description,
            )
        )

    def test_low_quality_cached_upload_metadata_is_invalidated(self):
        """A complete cache with a spoken title must not reach BI/WX."""
        source_title = "Re-Founding Incumbents for the AI Era"
        description = (
            "对话 Sequence Holdings 联合创始人兼CEO。"
            "公司以77亿美元收购在位企业，并用所有权改造组织和经营。"
        )
        meta = {
            "title": "Sequence我们有两个问题",
            "wechat_title": "Sequence我们有两个问题",
            "cover_title": "Sequence我们有两个问题",
            "description": description,
        }

        invalid_fields = autosub._invalidate_cached_upload_metadata(
            meta,
            source_title,
            "中文字幕",
        )

        self.assertEqual(invalid_fields, ["title", "wechat_title", "cover_title"])
        self.assertEqual(meta["title"], "")
        self.assertEqual(meta["wechat_title"], "")
        self.assertEqual(meta["cover_title"], "")
        self.assertEqual(meta["description"], description)

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

        self.assertEqual(title, "卡帕西讲透ChatGPT这类LLM应用")
        self.assertEqual(wechat_title, "卡帕西讲透LLM应用")
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

    def test_generate_upload_metadata_uses_local_fallback_after_llm_timeout(self):
        """A timeout can use a conservative description fallback if it validates."""
        workdir = os.path.join(self.temp_dir, "metadata_llm_timeout")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "description": (
                        "Cresta请来Hanze Li。"
                        "他具备FDE的复合技能——懂技术、会交付、能担责，"
                        "在客户现场替AI担责，而AI恰恰担不了责任。"
                    ),
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_text = (
            "Hanze Li谈FDE需要复合技能。\n"
            "AI不担责，最终担责的是人。"
        )
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            subtitle_text,
            "subtitle.srt",
        )
        subtitle_title._title_has_quality_issue.return_value = False
        llm_client = MagicMock()

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(
                 autosub,
                 "_extract_source_title",
                 return_value="Don't Buy the Espresso Machine",
             ), \
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

        self.assertEqual(
            metadata.get("title", ""),
            "Hanze Li谈FDE复合技能与担责",
        )
        self.assertEqual(metadata.get("wechat_title", ""), "FDE为客户做AI担责")
        self.assertEqual(metadata.get("cover_title", ""), "AI不担责，FDE解决责任")

    def test_generate_upload_metadata_rejects_low_quality_local_fallback(self):
        """A generic local fallback must still stop BI/WX uploads."""
        workdir = os.path.join(self.temp_dir, "metadata_bad_local_fallback")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {"description": "这个视频讨论了很多重要问题，内容非常丰富。"},
                handle,
                ensure_ascii=False,
            )

        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            "这是一个重要视频。",
            "subtitle.srt",
        )
        llm_client = MagicMock()

        with patch.object(autosub, "find_hardsub_video", return_value=hardsub), \
             patch.object(
                 autosub,
                 "_extract_source_title",
                 return_value="Important Video",
             ), \
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

        self.assertEqual(metadata.get("title", ""), "")
        self.assertEqual(metadata.get("wechat_title", ""), "")
        self.assertEqual(metadata.get("cover_title", ""), "")

    def test_metadata_model_candidates_follow_gateway_and_provider_chain(self):
        """Metadata generation gets its own model cascade."""
        llm_client = SimpleNamespace(
            DASHSCOPE_FALLBACK_CHAIN=["kimi-k2.6", "deepseek-v4-pro"]
        )
        provider = SimpleNamespace(value="dashscope")

        with patch.dict(
            os.environ,
            {"DASHSCOPE_METADATA_FALLBACK_MODELS": "glm-5.3"},
            clear=False,
        ):
            candidates = autosub._metadata_model_candidates(
                "glm-5.2",
                provider,
                llm_client,
            )

        self.assertEqual(
            candidates,
            ["glm-5.2", "glm-5.3"],
        )

        with patch.dict(
            os.environ,
            {"DASHSCOPE_METADATA_FALLBACK_MODELS": "qwen3.7-max,kimi-k2.6"},
            clear=False,
        ):
            candidates = autosub._metadata_model_candidates(
                "glm-5.2",
                provider,
                llm_client,
            )

        self.assertEqual(candidates, ["glm-5.2", "qwen3.7-max", "kimi-k2.6"])

    def test_generate_upload_metadata_retries_bad_title_with_next_model(self):
        """A rejected title must move to the next metadata model candidate."""
        workdir = os.path.join(self.temp_dir, "metadata_model_fallback")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "video_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "description": (
                        "Palantir现场访谈，讨论数据整合、客户业务和销售组织。"
                    )
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_text = "Palantir 整合数据，不做服务。\n"
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            subtitle_text,
            "subtitle.srt",
        )
        subtitle_title._title_has_quality_issue.return_value = False
        llm_client = MagicMock()
        llm_client.DASHSCOPE_FALLBACK_CHAIN = []

        with patch.object(autosub, "find_upload_hardsub_video", return_value=hardsub), \
             patch.object(
                 autosub,
                 "_extract_source_title",
                 return_value="Palantir AIPCon 11 Panel",
             ), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(
                 autosub,
                 "_generate_title_from_description",
                 side_effect=["AI", "Palantir整合数据不做服务"],
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
                 os.environ,
                {"DASHSCOPE_METADATA_FALLBACK_MODELS": "glm-5.3"},
                 clear=False,
             ), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(
                         get_client=lambda: llm_client,
                         LLMProvider=lambda value: SimpleNamespace(value=value),
                     ),
                 },
             ):
            metadata = autosub.generate_upload_metadata(workdir)

        self.assertEqual(metadata["title"], "Palantir整合数据不做服务")
        self.assertEqual(metadata["wechat_title"], "Palantir整合数据不做服务")
        self.assertEqual(
            metadata["cover_title"],
            "Palantir没有销售团队，客户却大规模采用",
        )
        self.assertEqual(
            [call.args[4] for call in mock_title.call_args_list],
            ["glm-5.2", "glm-5.3"],
        )
        self.assertEqual(mock_wechat_title.call_args.args[4], "glm-5.3")
        self.assertEqual(mock_cover.call_args.args[2], "glm-5.3")

    def test_generate_upload_metadata_keeps_spoken_title_incomplete(self):
        """A spoken Bilibili title must not yield an offline WeChat title."""
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

        self.assertEqual(metadata.get("wechat_title", ""), "")

    def test_generate_upload_metadata_uses_publisher_title_as_last_resort(self):
        """A publisher title may rescue an otherwise blocked upload."""
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

        self.assertEqual(metadata.get("title", ""), "蒂尔谈AI")
        self.assertEqual(metadata.get("wechat_title", ""), "蒂尔谈AI")
        self.assertEqual(
            metadata.get("cover_title", ""),
            "Peter：通谕是在帮中共的忙",
        )

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

    def test_extract_source_title_prefers_task_folder_over_sequence_filename(self):
        """A playlist filename must not erase the publisher title."""
        workdir = os.path.join(
            self.temp_dir,
            "[ENG] Palantir Ontology #1 - A groundbreaking interpretation "
            "(deep learning) you [gAggqvsYODs]",
        )
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "ENG_1 [gAggqvsYODs] [1080p]_hardsub.mp4")

        self.assertEqual(
            autosub._extract_source_title(hardsub),
            "[ENG] Palantir Ontology #1 - A groundbreaking interpretation "
            "(deep learning) you",
        )

    def test_source_title_topic_fallback_translates_common_topics(self):
        """The last resort must remain a natural Chinese title."""
        source_title = (
            "[ENG] Palantir Ontology #1 - A groundbreaking interpretation "
            "(deep learning) you [gAggqvsYODs]"
        )

        self.assertEqual(
            autosub._fallback_bili_title_from_source_title(source_title, 80),
            "Palantir谈本体论：深度学习新解读",
        )
        self.assertEqual(
            autosub._fallback_wechat_title_from_source_title(source_title, 16),
            "Palantir谈本体论",
        )

    def test_korean_source_title_survives_language_gates(self):
        """Korean publisher titles must remain usable without [ENG] crutches."""
        source_title = (
            "[ENG]팔란티어 온톨로지 #2 - "
            "온톨로지의 목적은 딥러닝과 같다"
        )

        self.assertTrue(autosub._is_usable_source_title(source_title))
        self.assertTrue(autosub._contains_hangul_text(source_title))
        self.assertTrue(
            autosub._metadata_title_is_source_copy(source_title, source_title)
        )
        self.assertEqual(
            autosub._fallback_bili_title_from_source_title(source_title, 80),
            "Palantir谈本体论：目的与深度学习相同",
        )
        self.assertEqual(
            autosub._fallback_wechat_title_from_source_title(source_title, 16),
            "Palantir谈本体论",
        )
        self.assertEqual(
            autosub._fallback_description_from_source_title(source_title, True),
            "Palantir 本体论第2期：本体论的目的与深度学习相同",
        )

    def test_metadata_llm_timeout_uses_client_environment(self):
        """Metadata calls must not override LLM_REQUEST_TIMEOUT with 90s."""
        client = MagicMock()

        autosub._generate_content_with_timeout(
            client,
            "prompt",
            "glm-5.3",
        )

        client.generate_content.assert_called_once_with(
            "prompt",
            model_name="glm-5.3",
            provider=None,
            request_timeout=None,
            max_retries=1,
            disable_thinking=False,
        )

    def test_generate_upload_metadata_uses_explicit_source_title(self):
        """Batch retries must override a useless cached filename title."""
        workdir = os.path.join(self.temp_dir, "metadata_explicit_source")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "ENG_1 [gAggqvsYODs] [1080p]_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "source_title": "ENG 1",
                    "description": "同时和A、B两人暧昧，比较只能在脑海里靠印象进行。",
                    "cover_title": "我们把握的不是实体，而是自己构建的本体",
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            "同时和A、B两人暧昧，比较只能在脑海里靠印象进行。",
            "subtitle.srt",
        )
        with patch.object(autosub, "find_upload_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.2", "dashscope")), \
             patch.object(autosub, "_generate_title_from_description", side_effect=TimeoutError), \
             patch.object(autosub, "_derive_wechat_title_from_bili_title", side_effect=TimeoutError), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: MagicMock()),
                 },
             ):
            metadata = autosub.generate_upload_metadata(
                workdir,
                source_title=(
                    "[ENG] Palantir Ontology #1 - A groundbreaking interpretation "
                    "(deep learning) you [gAggqvsYODs]"
                ),
            )

        self.assertEqual(metadata["source_title"], "[ENG] Palantir Ontology #1 - A groundbreaking interpretation (deep learning) you [gAggqvsYODs]")
        self.assertEqual(metadata["title"], "Palantir谈本体论：深度学习新解读")
        self.assertEqual(metadata["wechat_title"], "Palantir谈本体论")

    def test_korean_metadata_timeout_does_not_reinvalidate_fallback(self):
        """A Korean source title must not create a regenerate-clear loop."""
        workdir = os.path.join(self.temp_dir, "metadata_korean_timeout")
        os.makedirs(workdir)
        hardsub = os.path.join(workdir, "ENG_2 [V5fBnnBM2Go] [1080p]_hardsub.mp4")
        with open(hardsub, "w") as handle:
            handle.write("video")
        source_title = (
            "[ENG]팔란티어 온톨로지 #2 - "
            "온톨로지의 목적은 딥러닝과 같다"
        )
        with open(os.path.join(workdir, "upload_metadata.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "source_title": source_title,
                    "description": source_title,
                    "title": (
                        "[ENG]팔란티어 온톨로지 #2谈"
                        "온톨로지의 목적은 딥러닝과 같다"
                    ),
                    "cover_title": "无数用户的深度学习不断汇聚，最终形成了一个巨大的深度学习",
                    "wechat_title": "",
                },
                handle,
                ensure_ascii=False,
            )

        subtitle_text = "本体论的目的与深度学习相同。\n无数用户的深度学习不断汇聚。"
        subtitle_title = MagicMock()
        subtitle_title.read_full_subtitle_text.return_value = (
            subtitle_text,
            "subtitle.srt",
        )
        llm_client = MagicMock()

        with patch.object(autosub, "find_upload_hardsub_video", return_value=hardsub), \
             patch.object(autosub, "_load_llm_defaults", return_value=("glm-5.3", "dashscope")), \
             patch.object(autosub, "_generate_content_with_timeout", side_effect=TimeoutError), \
             patch.dict(
                 sys.modules,
                 {
                     "subtitle_title": subtitle_title,
                     "llm_utils": MagicMock(get_client=lambda: llm_client),
                 },
             ):
            metadata = autosub.generate_upload_metadata(
                workdir,
                source_title=source_title,
            )

        self.assertEqual(
            metadata["description"],
            "Palantir 本体论第2期：本体论的目的与深度学习相同",
        )
        self.assertEqual(metadata["title"], "Palantir谈本体论：目的与深度学习相同")
        self.assertEqual(metadata["wechat_title"], "Palantir谈本体论")
        self.assertEqual(
            metadata["cover_title"],
            "无数用户的深度学习不断汇聚，最终形成了一个巨大的深度学习",
        )
        self.assertEqual(
            autosub._invalidate_cached_upload_metadata(
                metadata,
                source_title,
                subtitle_text,
            ),
            [],
        )

    def test_batch_worker_passes_task_title_to_metadata_generation(self):
        """The engine must not rely on a collapsed download filename."""
        workdir = os.path.join(self.temp_dir, "batch_source_title")
        os.makedirs(workdir)
        task = SubTask(
            uid=21,
            url="https://www.youtube.com/watch?v=gAggqvsYODs",
            title=(
                "[ENG] Palantir Ontology #1 - A groundbreaking interpretation "
                "(deep learning) you [gAggqvsYODs]"
            ),
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
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )
        engine.do_gdsync = False
        engine.do_bili = True
        engine.do_wechat = False
        engine.task_map[task.uid] = task
        observed_kwargs = {}

        def fake_metadata(_workdir, **kwargs):
            observed_kwargs.update(kwargs)
            return None

        def fake_disk_truth(current_task):
            if current_task.status == "排队中 (GD)":
                return
            current_task.status = "完成"

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(batch_pro, "prepare_bili_upload_metadata", side_effect=fake_metadata):
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            observed_kwargs.get("source_title"),
            "[ENG] Palantir Ontology #1 - A groundbreaking interpretation "
            "(deep learning) you [gAggqvsYODs]",
        )

    def test_generate_upload_metadata_stops_without_source_title_fallback(self):
        """Incomplete metadata is safer than a pure English publisher-title copy."""
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

        self.assertEqual(metadata.get("title", ""), "")
        self.assertEqual(metadata.get("wechat_title", ""), "")
        self.assertEqual(metadata.get("cover_title", ""), "")

    def test_generate_upload_metadata_uses_local_hook_with_chinese_subtitle(self):
        """A Chinese transcript can still provide a local cover hook."""
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

        self.assertEqual(metadata.get("title", ""), "")
        self.assertEqual(metadata.get("wechat_title", ""), "")
        self.assertEqual(
            metadata.get("cover_title", ""),
            "Palantir：医院被攻击，透析先受影响",
        )

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
             patch.object(autosub, "_derive_wechat_title_from_bili_title", return_value="Palantir访谈聚焦落地"), \
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
        self.assertEqual(metadata["wechat_title"], "Palantir访谈聚焦落地")
        self.assertEqual(
            metadata.get("cover_title", ""),
            "医院被攻击，就有人做不了透析",
        )
        self.assertFalse(llm_client.generate_content.called)

    def test_cached_upload_title_must_cover_whole_video_summary(self):
        """A single highlight or spoken quote must not survive metadata reuse."""
        source_title = "The Wildest Hot Takes From the All-In Summit 2026"
        description = (
            "All-In Summit 2026现场，多位嘉宾讨论AI、能源与人体创新。"
            "内容包括SaaS、天然气、超加工食品、自由意志和生物黑客。"
            "嘉宾还讨论了AI安全和监管。"
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
        self.assertTrue(
            autosub._metadata_title_lacks_mainline_coverage(
                "GenSpark建立管控机制加速普及模型",
                description,
            )
        )
        self.assertFalse(
            autosub._metadata_title_lacks_mainline_coverage(
                "All-In峰会嘉宾谈AI、能源与人体创新",
                description,
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

    def test_generate_upload_metadata_stops_when_summary_title_is_bad(self):
        """Invalid summary compression must stop uploads, not use subtitle fallback."""
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
        ]
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.read_subtitle_text.return_value = ("字幕内容", "subtitle.srt")
        subtitle_title.read_full_subtitle_text.return_value = ("完整字幕内容", "subtitle.srt")
        subtitle_title.parse_llm_title_candidates.side_effect = [
            ("", ()),
            ("", ()),
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

        self.assertEqual(metadata.get("title", ""), "")
        self.assertEqual(metadata.get("wechat_title", ""), "")
        self.assertEqual(metadata.get("cover_title", ""), "")
        self.assertEqual(subtitle_title.build_subtitle_grounded_title.call_count, 0)

    def test_bili_title_prompt_requires_a_complete_summary_sentence(self):
        """The Bilibili title must summarize the interview, not pile up nouns."""
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir数据整合产品化"))
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir把数据整合成产品"))
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir为企业做成产品"))
        self.assertFalse(autosub._shared_title_has_quality_issue("Palantir为企业整合数据"))
        self.assertFalse(autosub._shared_title_has_quality_issue("卡普谈Palantir数据整合"))
        self.assertFalse(autosub._shared_title_has_quality_issue("Palantir押注工程团队"))
        self.assertTrue(autosub._shared_title_has_quality_issue("Palantir押注解决重要问题"))

    def test_shared_title_accepts_reported_claims_and_consequences(self):
        """A complete reported claim is not a fragment merely because it omits a whitelisted verb."""
        good_titles = (
            "卡普：成本只有目前的一半",
            "卡普称许多CEO没魄力改核心业务",
            "卡普指多数公司做AI都在做无用功",
            "卡普称AI理论退场有效现实正进场",
        )
        for title in good_titles:
            self.assertFalse(autosub._shared_title_has_quality_issue(title))

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
        self.assertIn("列出全片 5-8 个主要内容", prompt)
        self.assertIn("优先 20-40 个字符", prompt)
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
        self.assertIn("生成 10 个候选", prompt)
        subtitle_title.validate_title_from_subtitle.assert_called_once_with(
            "卡普谈Palantir数据整合，不做服务并坚持工程为本",
            "卡普谈Palantir把数据整合做成产品。",
            ("卡普谈Palantir数据整合",),
            min_chars=18,
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
        self.assertIn("必须优先保留B站标题开头的主体，可用其中文通行名或公众熟知简称", prompt)
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

    def test_wechat_title_replaces_english_person_before_length_check(self):
        """A known English person must not consume WeChat's character budget."""
        llm_client = MagicMock()
        llm_client.generate_content.return_value = "{}"
        subtitle_title = MagicMock()
        subtitle_title.SubtitleTitleError = ValueError
        subtitle_title.parse_llm_title_candidates.return_value = (
            "卡普谈AI核心业务",
            (
                (
                    "Alex Karp：核心业务五倍成本减半",
                    ("Alex Karp谈核心业务五倍成本减半。",),
                ),
            ),
        )
        subtitle_title.validate_title_from_subtitle.return_value = (
            "Alex Karp谈核心业务五倍成本减半。",
        )

        with patch.dict(sys.modules, {"subtitle_title": subtitle_title}):
            title = autosub._derive_wechat_title_from_bili_title(
                "卡普谈AI核心业务",
                "卡普谈AI核心业务。",
                "Alex Karp谈核心业务五倍成本减半。",
                llm_client,
                "kimi-k2.6",
                max_chars=16,
            )

        self.assertEqual(title, "卡普：核心业务五倍成本减半")
        prompt = llm_client.generate_content.call_args.args[0]
        self.assertIn("视频号标题不得输出英文人名全名", prompt)
        self.assertIn("例如 Alex Karp 写成「卡普」", prompt)
        subtitle_title.validate_title_from_subtitle.assert_called_once_with(
            "Alex Karp：核心业务五倍成本减半",
            "Alex Karp谈核心业务五倍成本减半。",
            ("Alex Karp谈核心业务五倍成本减半。",),
            min_chars=1,
            max_chars=20,
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

    def test_proper_noun_aliases_are_parsed_and_grounded(self):
        """Chinese public names validate against their subtitle source."""
        raw = (
            '{"central_claim":"...","subject_aliases":'
            '{"Ben Affleck":["本·阿弗莱克","大本"]},'
            '"candidates":[]}'
        )
        self.assertEqual(
            autosub._parse_llm_title_aliases(raw),
            {"Ben Affleck": ("本·阿弗莱克", "大本")},
        )

        def validator(title, subtitle_text, evidence, min_chars, max_chars):
            if "Ben Affleck" not in title:
                raise ValueError("canonical subject missing")
            if "推掉新戏" not in title:
                raise ValueError("claim missing")
            return evidence

        evidence = ("Ben Affleck推掉新戏陪孩子。",)
        self.assertEqual(
            autosub._validate_title_with_proper_noun_aliases(
                "大本推掉新戏陪孩子",
                "Ben Affleck推掉新戏陪孩子。",
                evidence,
                min_chars=8,
                max_chars=16,
                aliases={"Ben Affleck": ("大本",)},
                validator=validator,
                error_class=ValueError,
            ),
            evidence,
        )

        with self.assertRaises(ValueError):
            autosub._validate_title_with_proper_noun_aliases(
                "大本推掉新戏陪孩子",
                "字幕里没有这个英文原名。",
                evidence,
                min_chars=8,
                max_chars=16,
                aliases={"Ben Affleck": ("大本",)},
                validator=validator,
                error_class=ValueError,
            )

    def test_local_wechat_fallback_uses_short_public_nicknames(self):
        """A known nickname preserves the subject inside the 16-char budget."""
        self.assertEqual(
            autosub._compress_bili_title_for_wechat(
                "Ben Affleck推掉新戏陪孩子",
                16,
            ),
            "大本推掉新戏陪孩子",
        )
        self.assertEqual(
            autosub._fallback_wechat_hook_from_bili_title(
                "Ben Affleck推掉新戏陪孩子",
                16,
            ),
            "大本推掉新戏陪孩子",
        )

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
            min_chars=18,
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
        self.assertIn("按主体明确、画面具体、后果清楚", prompt)
        self.assertIn("再生成 10 个候选", prompt)
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

    def test_bili_login_preflight_detects_missing_cookie_fields(self):
        """A malformed local cookie file is a definite pre-upload failure."""
        cookie_path = os.path.join(self.temp_dir, "cookies.json")
        with open(cookie_path, "w", encoding="utf-8") as handle:
            json.dump({"SESSDATA": "session", "bili_jct": ""}, handle)

        with patch.object(autosub, "BILI_COOKIES", cookie_path):
            reason = autosub.get_bili_login_skip_reason()

        self.assertEqual(reason, "Bilibili cookies missing: bili_jct, buvid3")

    def test_bili_login_preflight_classifies_code_101(self):
        """The account API check must distinguish invalid login from network errors."""
        cookie_path = os.path.join(self.temp_dir, "cookies.json")
        with open(cookie_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"SESSDATA": "session", "bili_jct": "csrf", "buvid3": "buvid"},
                handle,
            )
        mock_subprocess = MagicMock()
        mock_subprocess.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="ResponseCodeException: code=-101, 账号未登录",
        )

        with patch.object(autosub, "BILI_COOKIES", cookie_path), \
             patch.object(autosub, "BILI_PYTHON", sys.executable), \
             patch.object(autosub, "subprocess", mock_subprocess):
            status, reason = autosub.check_bili_login()

        self.assertFalse(status)
        self.assertIn("code -101", reason)
        self.assertIn(cookie_path, mock_subprocess.run.call_args.args[0][3])

    def test_bili_login_preflight_keeps_network_errors_recoverable(self):
        """An inconclusive check must not prevent the real upload from running."""
        cookie_path = os.path.join(self.temp_dir, "cookies.json")
        with open(cookie_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"SESSDATA": "session", "bili_jct": "csrf", "buvid3": "buvid"},
                handle,
            )
        mock_subprocess = MagicMock()
        mock_subprocess.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="NetworkException: timeout",
        )

        with patch.object(autosub, "BILI_COOKIES", cookie_path), \
             patch.object(autosub, "BILI_PYTHON", sys.executable), \
             patch.object(autosub, "subprocess", mock_subprocess):
            status, reason = autosub.check_bili_login()

        self.assertIsNone(status)
        self.assertIn("upload will still be attempted", reason)

    def test_wechat_login_preflight_requires_browser_profile(self):
        """A missing persistent profile is a definite pre-upload failure."""
        profile = os.path.join(self.temp_dir, "browser_data")
        os.makedirs(profile)

        with patch.object(autosub, "WECHAT_BROWSER_DATA_DIR", profile):
            self.assertIsNone(autosub.get_wechat_login_skip_reason())

        with patch.object(autosub, "WECHAT_BROWSER_DATA_DIR", "/missing/profile"):
            reason = autosub.get_wechat_login_skip_reason()

        self.assertIn("WeChat browser profile not found", reason)

    def test_wechat_login_command_is_visible(self):
        """The recovery command must not inherit upload-only headless mode."""
        cmd = autosub.build_wechat_login_cmd()

        self.assertIn("--login", cmd)
        self.assertNotIn("--headless", cmd)

    def test_upload_login_failure_hints_include_recovery_commands(self):
        """Login failures surface the exact QR-login command, not only code=1."""
        bili_hint = autosub.get_upload_login_failure_hint(
            "📱 [Bilibili]", "{'code': -101, 'message': '账号未登录'}"
        )
        wechat_hint = autosub.get_upload_login_failure_hint(
            "💬 [WeChat]", "微信登录态不可用或发布页加载失败"
        )

        self.assertIn("login.py", bili_hint)
        self.assertIn("wechat_upload.py --login", wechat_hint)

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

    def test_wechat_upload_updates_task_progress(self):
        """WeChat page transfer progress must map to the whole WX stage."""
        task = SubTask(
            uid=1,
            url="http://test.com",
            title="Video 1",
            status="运行中 (BI/WX)",
            workdir=os.path.join(self.temp_dir, "wechat_progress"),
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
        engine.task_map[task.uid] = task

        def fake_run(_cmd, on_progress=None, **_kwargs):
            self.assertIsNotNone(on_progress)
            on_progress(12.3)
            return False

        result_box = [False]
        with patch.object(batch_pro, "run_wechat_cmd", side_effect=fake_run):
            engine._run_wechat_upload(task, ["wechat-upload"], result_box)

        self.assertEqual(task.pcts["WX"], 40.5)
        self.assertFalse(result_box[0])

    def test_wechat_upload_progress_combines_milestones_and_transfer(self):
        """The WX bar advances through stages and never regresses."""
        task = SubTask(
            uid=2,
            url="http://test.com",
            title="Video 2",
            status="运行中 (BI/WX)",
            workdir=os.path.join(self.temp_dir, "wechat_stage_progress"),
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
        engine.task_map[task.uid] = task
        snapshots = []

        def fake_run(_cmd, on_progress=None, on_log=None, **_kwargs):
            on_log(">>> 视频文件已选择，等待上传...")
            snapshots.append(task.pcts["WX"])
            on_progress(50.0)
            snapshots.append(task.pcts["WX"])
            on_log(">>> 等待视频上传完成（最多 10000 秒）...")
            snapshots.append(task.pcts["WX"])
            on_progress(0.0)
            snapshots.append(task.pcts["WX"])
            on_log(">>> 上传完成！")
            snapshots.append(task.pcts["WX"])
            on_log(">>> 已填写标题")
            snapshots.append(task.pcts["WX"])
            return True

        result_box = [False]
        with patch.object(batch_pro, "run_wechat_cmd", side_effect=fake_run):
            engine._run_wechat_upload(task, ["wechat-upload"], result_box)

        snapshots.append(task.pcts["WX"])
        self.assertEqual(snapshots, [35.0, 57.5, 57.5, 57.5, 80.0, 82.0, 100.0])
        self.assertTrue(result_box[0])

    def test_parallel_uploads_show_metadata_status_before_upload_threads(self):
        """Upload threads start while the task is still marked as metadata generation."""
        workdir = os.path.join(self.temp_dir, "parallel_upload_status")
        os.makedirs(workdir)
        task = SubTask(
            uid=2,
            url="http://test.com",
            title="Video 2",
            duration="01:00",
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

        metadata = {
            "title": "OpenAI研究员谈智能体",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体",
        }
        observed_statuses = []

        def fake_disk_truth(current_task):
            if current_task.status != "排队中 (GD)":
                current_task.status = "完成"

        def fake_bili_run(current_task, _cmd, result_box):
            observed_statuses.append(("BI", current_task.status))
            result_box[0] = True

        def fake_wechat_run(current_task, _cmd, result_box):
            observed_statuses.append(("WX", current_task.status))
            result_box[0] = True

        worker = threading.Thread(target=engine.worker, args=(task,), daemon=True)
        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(batch_pro, "prepare_bili_upload_metadata", return_value={
                 "title": metadata["title"],
                 "desc": metadata["description"],
                 "cover_title": metadata["cover_title"],
             }), \
             patch.object(batch_pro, "prepare_wechat_upload_metadata", return_value={
                 "wechat_title": metadata["wechat_title"],
                 "desc": metadata["description"],
                 "cover_title": metadata["cover_title"],
             }), \
             patch.object(batch_pro, "build_bili_cmd", return_value=["bili-upload"]), \
             patch.object(batch_pro, "build_wechat_cmd", return_value=["wechat-upload"]), \
             patch.object(batch_pro, "get_wechat_upload_skip_reason", return_value=None), \
             patch.object(BatchEngine, "_run_bili_upload", side_effect=fake_bili_run), \
             patch.object(BatchEngine, "_run_wechat_upload", side_effect=fake_wechat_run):
            worker.start()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            observed_statuses,
            [("BI", "运行中 (MG)"), ("WX", "运行中 (MG)")],
        )
        self.assertEqual(task.status, "完成")
        for marker in (".bili_uploaded", ".wechat_uploaded"):
            self.assertTrue(os.path.exists(os.path.join(workdir, marker)))

    def test_parallel_uploads_generate_title_at_wechat_limit(self):
        """BI/WX use separate titles, with WeChat capped at its own limit."""
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

        self.assertEqual(mock_meta.call_count, 2)
        self.assertEqual(
            mock_meta.call_args_list[0].kwargs,
            {
                "bili_title_max_chars": 48,
                "llm_model": "glm-5.3",
                "llm_provider": "dashscope",
                "on_log": None,
                "include_wechat": False,
                "source_title": None,
            },
        )
        self.assertEqual(
            mock_meta.call_args_list[1].kwargs,
            {
                "bili_title_max_chars": 48,
                "wechat_title_max_chars": 16,
                "llm_model": "glm-5.3",
                "llm_provider": "dashscope",
                "on_log": None,
                "include_wechat": True,
                "source_title": None,
                "prefer_local_wechat_fallback": False,
            },
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

    def test_prepare_shared_upload_metadata_prefers_explicit_cover_title(self):
        """The GUI's explicit cover headline overrides generated metadata only."""
        workdir = os.path.join(self.temp_dir, "prepare_shared_metadata")
        os.makedirs(workdir)
        metadata = {
            "title": "OpenAI研究员谈智能体和自我改进的边界",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体与自我改进",
        }

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata):
            prepared = autosub.prepare_shared_upload_metadata(
                workdir,
                explicit_cover_title="智能体的边界",
            )

        self.assertEqual(prepared["title"], metadata["title"])
        self.assertEqual(prepared["wechat_title"], metadata["wechat_title"])
        self.assertEqual(prepared["desc"], metadata["description"])
        self.assertEqual(prepared["cover_title"], "智能体的边界")

    def test_lone_bili_upload_uses_shared_metadata(self):
        """A single pending platform must not fall back to uploader-side titles."""
        workdir = os.path.join(self.temp_dir, "lone_bili_metadata")
        os.makedirs(workdir)
        metadata = {
            "title": "OpenAI研究员谈智能体和自我改进的边界",
            "wechat_title": "OpenAI研究员谈智能体",
            "description": "共享简介",
            "cover_title": "智能体与自我改进",
        }
        calls = []

        def fake_bili(_workdir, **kwargs):
            calls.append(kwargs)
            return True

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata), \
             patch.object(autosub, "trigger_bili_upload", side_effect=fake_bili), \
             patch.object(autosub, "trigger_wechat_upload") as mock_wechat:
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
            )

        mock_wechat.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["title"], metadata["title"])
        self.assertEqual(calls[0]["desc"], metadata["description"])
        self.assertEqual(calls[0]["cover_title"], metadata["cover_title"])
        self.assertTrue(os.path.exists(os.path.join(workdir, ".bili_uploaded")))

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
        """Explicit per-platform cover titles stay with their own upload."""
        workdir = os.path.join(self.temp_dir, "different_covers")
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

        with patch.object(autosub, "generate_upload_metadata") as mock_meta, \
             patch.object(autosub, "trigger_bili_upload", side_effect=fake_bili), \
             patch.object(autosub, "trigger_wechat_upload", side_effect=fake_wechat):
            mock_meta.return_value = metadata
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
                wechat_upload=True,
                bili_cover_title="智能体与自我改进",
                wechat_cover_title="智能体会自我改进",
            )

        self.assertEqual(mock_meta.call_count, 2)
        bili_kwargs = next(call[1] for call in calls if call[0] == "BI")
        wechat_kwargs = next(call[1] for call in calls if call[0] == "WX")
        self.assertEqual(bili_kwargs["cover_title"], "智能体与自我改进")
        self.assertEqual(wechat_kwargs["cover_title"], "智能体会自我改进")

    def test_bili_upload_starts_when_wechat_title_is_missing(self):
        """A WeChat title failure must not stop the completed Bilibili upload."""
        workdir = os.path.join(self.temp_dir, "bili_without_wechat_title")
        os.makedirs(workdir)
        metadata = {
            "title": "OpenAI研究员谈智能体和自我改进的边界",
            "description": "共享简介",
            "cover_title": "智能体与自我改进",
        }

        with patch.object(autosub, "generate_upload_metadata", return_value=metadata), \
             patch.object(autosub, "trigger_bili_upload", return_value=True) as mock_bili, \
             patch.object(autosub, "trigger_wechat_upload", return_value=True) as mock_wechat:
            autosub.run_post_processing_uploads(
                workdir,
                bili_upload=True,
                wechat_upload=True,
            )

        mock_bili.assert_called_once()
        mock_wechat.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(workdir, ".bili_uploaded")))
        self.assertFalse(os.path.exists(os.path.join(workdir, ".wechat_uploaded")))

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

    def test_monitor_state_restores_flags_and_marker_truth(self):
        """Live Monitor must show enabled upload stages and disk-truth completion."""
        workdir = os.path.join(self.temp_dir, "task")
        os.makedirs(workdir, exist_ok=True)
        for marker in (".burn_complete", ".synced", ".bili_uploaded", ".wechat_uploaded"):
            with open(os.path.join(workdir, marker), "w", encoding="utf-8") as handle:
                handle.write("1")

        state_file = os.path.join(self.temp_dir, "batch_state.json")
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "1": {
                        "uid": 1,
                        "url": "",
                        "vid_id": "",
                        "title": "task",
                        "status": "失败 (WX)",
                        "pcts": {
                            "DL": 100.0,
                            "TR": 100.0,
                            "TL": 100.0,
                            "MR": 100.0,
                            "BR": 100.0,
                            "GD": 100.0,
                            "MG": 100.0,
                            "BI": 100.0,
                            "WX": 90.0,
                        },
                        "workdir": workdir,
                    },
                    "bili": True,
                    "gdsync": True,
                    "wechat": True,
                },
                handle,
                ensure_ascii=False,
            )

        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.load_state_monitor()

        self.assertTrue(engine.do_bili)
        self.assertTrue(engine.do_wechat)
        self.assertTrue(engine.do_gdsync)
        self.assertEqual(engine.task_map[1].status, "完成")
        self.assertEqual(engine.task_map[1].pcts["WX"], 100.0)

    def _monitor_engine_with_running_task(self, pid):
        """Load a monitor whose only task was persisted as 运行中."""
        # load_state_monitor only reads the stage flags from a file literally
        # named batch_state.json, so each case needs its own directory.
        case_dir = os.path.join(self.temp_dir, f"monitor-{pid}")
        os.makedirs(case_dir, exist_ok=True)
        workdir = os.path.join(case_dir, "task")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, ".bili_uploaded"), "w", encoding="utf-8") as handle:
            handle.write("1")
        state_file = os.path.join(case_dir, "batch_state.json")
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "1": {
                        "uid": 1,
                        "url": "",
                        "vid_id": "",
                        "title": "task",
                        "status": "运行中 (WX)",
                        "pid": pid,
                        "pcts": {
                            "DL": 100.0,
                            "TR": 100.0,
                            "TL": 100.0,
                            "MR": 100.0,
                            "BR": 100.0,
                            "GD": 100.0,
                            "MG": 100.0,
                            "BI": 100.0,
                            "WX": 0.0,
                        },
                        "workdir": workdir,
                    },
                    "bili": True,
                    "wechat": True,
                },
                handle,
                ensure_ascii=False,
            )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.load_state_monitor()
        return engine

    def test_monitor_drops_stale_running_status_without_live_pid(self):
        """An engine killed mid-task must not leave the row spinning forever."""
        engine = self._monitor_engine_with_running_task(None)

        self.assertEqual(engine.task_map[1].status, "排队中 (WX)")

    def test_monitor_keeps_running_status_for_live_pid(self):
        """A genuinely live worker pid keeps the persisted 运行中 status."""
        engine = self._monitor_engine_with_running_task(os.getpid())

        self.assertEqual(engine.task_map[1].status, "运行中 (WX)")

    def test_monitor_preserves_persisted_failure_status(self):
        """Re-deriving a stale row must not erase a real failure."""
        case_dir = os.path.join(self.temp_dir, "monitor-failed")
        os.makedirs(case_dir, exist_ok=True)
        workdir = os.path.join(case_dir, "task")
        os.makedirs(workdir, exist_ok=True)
        state_file = os.path.join(case_dir, "batch_state.json")
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "1": {
                        "uid": 1,
                        "url": "",
                        "vid_id": "",
                        "title": "task",
                        "status": "失败 (上传)",
                        "error": "BI upload failed",
                        "pid": None,
                        "pcts": {
                            "DL": 100.0,
                            "TR": 100.0,
                            "TL": 100.0,
                            "MR": 100.0,
                            "BR": 100.0,
                            "GD": 100.0,
                            "MG": 100.0,
                            "BI": 68.0,
                            "WX": 70.5,
                        },
                        "workdir": workdir,
                    },
                    "bili": True,
                    "wechat": True,
                },
                handle,
                ensure_ascii=False,
            )
        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.load_state_monitor()

        self.assertEqual(engine.task_map[1].status, "失败 (上传)")
        self.assertEqual(engine.task_map[1].error, "BI upload failed")

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
        """A limit skip the current duration rule still justifies stays terminal.

        The cap is re-validated on load rather than trusted from the saved status,
        so a lifted cap requeues the task (see
        test_load_state_requeues_stale_wechat_limit_status) while a live one does
        not.
        """
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
                "duration": "10:12",
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
        limit_reason = "视频时长 10.2 分钟，超过视频号 10 分钟限制"
        with patch.object(
            batch_pro, "get_wechat_duration_skip_reason", return_value=limit_reason
        ):
            engine.load_state()

        task = engine.task_map[1]
        self.assertEqual(task.status, "超限 (WX)")
        self.assertIn("超过视频号 10 分钟限制", task.error)
        self.assertEqual(task.pcts["WX"], 0.0)
        self.assertTrue(engine.do_wechat)

    def test_load_state_preserves_ambiguous_wechat_failure(self):
        """A submitted-but-unconfirmed WX upload must not auto-retry on restart."""
        project_dir = os.path.join(self.temp_dir, "project")
        task_dir = os.path.join(project_dir, "task_1")
        os.makedirs(task_dir)
        state_file = os.path.join(project_dir, "batch_state.json")
        error = (
            "视频号上传结果不确定：文件已提交给微信，请先检查草稿箱；"
            "为避免重复投稿，已停止自动重试"
        )
        state = {
            "wechat": True,
            "1": {
                "uid": 1,
                "url": "http://test.com",
                "vid_id": "vid1",
                "title": "Video 1",
                "status": "失败 (WX)",
                "pcts": {
                    "DL": 100.0,
                    "TR": 100.0,
                    "TL": 100.0,
                    "MR": 100.0,
                    "BR": 100.0,
                    "GD": 100.0,
                    "MG": 100.0,
                    "BI": 100.0,
                    "WX": 66.5,
                },
                "workdir": task_dir,
                "error": error,
            },
        }
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)

        engine = BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=state_file,
        )
        engine.load_state()

        task = engine.task_map[1]
        self.assertEqual(task.status, "失败 (WX)")
        self.assertEqual(task.error, error)
        self.assertEqual(task.pcts["WX"], 66.5)


class TestUploadWatchdogAndProcessTree(unittest.TestCase):
    """A wedged uploader must be killed instead of holding its serial slot.

    The WeChat slot has exactly one permit because the uploads share a browser
    profile. A child that keeps printing polling lines while the percentage
    never moves therefore parks every remaining task at "queued" until the
    engine is restarted by hand, which is the failure these tests pin down.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._bili_login_patcher = patch.object(
            batch_pro, "get_bili_login_skip_reason", return_value=None
        )
        self._bili_login_patcher.start()
        self.addCleanup(self._bili_login_patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _engine(self):
        return BatchEngine(
            output_dir=self.temp_dir,
            sub_dir_name="project",
            state_file=os.path.join(self.temp_dir, "batch_state.json"),
        )

    def _spawn_parent_with_grandchild(self):
        """Start a child that itself spawns a sleeper, and report both pids."""
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        grandchild = int(proc.stdout.readline().strip())
        return proc, grandchild

    @staticmethod
    def _died(pid):
        """Wait up to 2.5s for a pid to disappear."""
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.05)
        return False

    def _run_upload(self, script, **kwargs):
        logs, pcts, killed = [], [], []
        original = autosub.kill_proc_tree

        def spy(proc, terminate=False):
            killed.append(getattr(proc, "pid", None))
            return original(proc, terminate=terminate)

        with patch.object(autosub, "kill_proc_tree", spy):
            ok = autosub._run_upload_cmd(
                [sys.executable, "-u", "-c", script],
                "WeChat",
                r'(?:Progress|上传进度):\s*([\d.]+)%',
                on_progress=pcts.append,
                on_log=logs.append,
                **kwargs,
            )
        return ok, logs, pcts, killed

    def test_upload_runner_reports_process_lifecycle(self):
        """GUI can register canonical uploader children for stop and pause."""
        started, ended = [], []
        ok = autosub._run_upload_cmd(
            [sys.executable, "-u", "-c", "print('上传进度: 100%', flush=True)"],
            "WeChat",
            r'(?:Progress|上传进度):\s*([\d.]+)%',
            on_process_start=started.append,
            on_process_end=ended.append,
        )
        self.assertTrue(ok)
        self.assertEqual(len(started), 1)
        self.assertEqual(ended, started)

    def test_shutdown_requeues_running_tasks_after_manual_exit(self):
        """Closing the Launcher must not persist phantom running tasks."""
        task = SubTask(
            uid=11,
            url="http://test.com",
            title="Video 11",
            status="运行中 (MG)",
            pcts={
                "DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                "BR": 100.0, "GD": 100.0, "MG": 50.0, "BI": 0.0, "WX": 0.0,
            },
        )
        engine = self._engine()
        engine.task_map[task.uid] = task

        with patch.object(BatchEngine, "kill_children", lambda self: None):
            engine.shutdown()

        self.assertEqual(task.status, "排队中 (MG)")
        self.assertIsNone(task.pid)
        self.assertIsNone(task.error)
        with open(engine.state_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["11"]["status"], "排队中 (MG)")

    # --- autosub._run_upload_cmd watchdog ---

    def test_stalled_upload_is_killed_even_while_it_keeps_printing(self):
        """Output without progress is the exact WeChat wedge seen in production."""
        script = (
            "import time\n"
            "print('上传进度: 42%', flush=True)\n"
            "while True:\n"
            "    print('>>> 微信接口响应: 201', flush=True)\n"
            "    time.sleep(0.02)\n"
        )
        ok, logs, pcts, killed = self._run_upload(
            script, stall_timeout=0.5, silence_timeout=None
        )
        self.assertFalse(ok)
        self.assertEqual(pcts, [42.0])
        self.assertTrue(killed)
        self.assertTrue(any("Watchdog" in line for line in logs))

    def test_silent_upload_is_killed(self):
        """A child that stops printing entirely is caught by the silence timer."""
        script = "import time\nprint('starting', flush=True)\ntime.sleep(30)\n"
        ok, logs, _pcts, killed = self._run_upload(
            script, stall_timeout=None, silence_timeout=0.5
        )
        self.assertFalse(ok)
        self.assertTrue(killed)
        self.assertTrue(any("no output for" in line for line in logs))

    def test_moving_progress_is_not_killed(self):
        """A healthy upload must run to completion untouched."""
        script = (
            "import time\n"
            "for i in range(1, 101):\n"
            "    print(f'Progress: {i}%', flush=True)\n"
            "    time.sleep(0.005)\n"
        )
        ok, _logs, pcts, killed = self._run_upload(
            script, stall_timeout=30, silence_timeout=30
        )
        self.assertTrue(ok)
        self.assertEqual(killed, [])
        self.assertEqual(pcts[-1], 100.0)

    def test_kill_proc_tree_takes_the_browser_child_with_it(self):
        """Killing only the wrapper would leave chromium holding the profile."""
        proc, grandchild = self._spawn_parent_with_grandchild()
        autosub.kill_proc_tree(proc)
        proc.wait(timeout=10)
        self.assertTrue(self._died(grandchild))

    # --- batch_pro psutil-free process management ---

    def test_kill_pid_tree_works_without_psutil(self):
        """The engine runs without psutil, so the fallback path is the real one."""
        proc, grandchild = self._spawn_parent_with_grandchild()
        with patch.object(batch_pro, "psutil", None):
            self.assertTrue(batch_pro.kill_pid_tree(proc.pid))
        proc.wait(timeout=10)
        self.assertTrue(self._died(grandchild))

    def test_iter_process_table_falls_back_to_ps(self):
        """The orphan scan must still run when psutil is missing."""
        engine = self._engine()
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="  101 /bin/sh foo\n202 python bar.py\n"
        )
        with patch.object(batch_pro, "psutil", None), \
             patch.object(batch_pro.subprocess, "run", return_value=done):
            rows = list(engine._iter_process_table())
        self.assertEqual(rows, [(101, "/bin/sh foo"), (202, "python bar.py")])

    def test_cleanup_orphans_kills_leftover_wechat_uploader_without_psutil(self):
        """A previous session's uploader keeps the shared profile locked."""
        engine = self._engine()
        project = os.path.abspath(engine.project_dir)
        table = [
            (os.getpid(), "python autosub_batch_pro.py"),
            (4242, f"/venv/bin/python -u /skills/wechat_upload.py {project}/Video [abc]/v_hardsub.mp4 --headless"),
            (4343, "/venv/bin/python -u /tools/transcribe_engine.py /elsewhere"),
        ]
        killed = []
        with patch.object(batch_pro, "psutil", None), \
             patch.object(engine, "_iter_process_table", lambda: iter(table)), \
             patch.object(batch_pro, "kill_pid_tree",
                          side_effect=lambda pid, terminate=False: killed.append(pid) or True):
            engine.cleanup_orphans()
        self.assertEqual(killed, [4242])

    # --- batch_pro.run_process watchdog ---

    def test_run_process_watchdog_frees_the_upload_slot(self):
        workdir = os.path.join(self.temp_dir, "wedged")
        os.makedirs(workdir)
        task = SubTask(
            uid=7, url="http://test.com", title="Video 7", duration="01:00",
            status="排队中 (WX)", workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                  "BR": 100.0, "GD": 100.0, "BI": 100.0, "WX": 0.0},
        )
        engine = self._engine()
        engine.task_map[task.uid] = task
        script = (
            "import time\n"
            "print('上传进度: 30%', flush=True)\n"
            "while True:\n"
            "    print('>>> 微信接口响应: 201', flush=True)\n"
            "    time.sleep(0.05)\n"
        )
        cmd = [sys.executable, "-u", "-c", script]
        with patch.object(BatchEngine, "save_state", lambda self: None):
            ok = engine.run_process(
                task, "WX", cmd, start_pct=0.0, end_pct=100.0,
                stall_timeout=0.6, silence_timeout=None,
            )
        self.assertFalse(ok)
        self.assertEqual(task.status, "失败 (WX)")
        self.assertIn("看门狗", task.error or "")
        self.assertIsNone(task.pid)

    def test_run_process_watchdog_resets_on_internal_retry(self):
        """An uploader retry from 0% must not be judged against the old maximum."""
        workdir = os.path.join(self.temp_dir, "bili_retry")
        os.makedirs(workdir)
        task = SubTask(
            uid=20, url="http://test.com", title="Video 20", duration="01:00",
            status="排队中 (BI)", workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                  "BR": 100.0, "GD": 100.0, "BI": 0.0, "WX": 100.0},
        )
        engine = self._engine()
        engine.task_map[task.uid] = task
        script = (
            "import time\n"
            "print('Progress: 70%', flush=True)\n"
            "time.sleep(0.3)\n"
            "print('>>> 开始上传: test (attempt 2/3)', flush=True)\n"
            "for i in range(1, 101):\n"
            "    print(f'Progress: {i}%', flush=True)\n"
            "    time.sleep(0.01)\n"
        )
        cmd = [sys.executable, "-u", "-c", script]
        with patch.object(BatchEngine, "save_state", lambda self: None):
            ok = engine.run_process(
                task, "BI", cmd, start_pct=0.0, end_pct=100.0,
                stall_timeout=1.0, silence_timeout=None,
            )
        self.assertTrue(ok)
        self.assertEqual(task.pcts["BI"], 100.0)

    def test_run_process_without_timeouts_does_not_watchdog(self):
        """GD's sync.py has irregular output, so it stays unmonitored."""
        workdir = os.path.join(self.temp_dir, "unmonitored")
        os.makedirs(workdir)
        task = SubTask(
            uid=8, url="http://test.com", title="Video 8", duration="01:00",
            status="排队中 (GD)", workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                  "BR": 100.0, "GD": 0.0, "BI": 100.0, "WX": 100.0},
        )
        engine = self._engine()
        engine.task_map[task.uid] = task
        script = (
            "import time\n"
            "for _ in range(8):\n"
            "    print('syncing', flush=True)\n"
            "    time.sleep(0.1)\n"
        )
        cmd = [sys.executable, "-u", "-c", script]
        with patch.object(BatchEngine, "save_state", lambda self: None):
            ok = engine.run_process(task, "GD", cmd, start_pct=0.0, end_pct=100.0)
        self.assertTrue(ok)
        self.assertEqual(task.pcts["GD"], 100.0)

    def test_run_process_surfaces_bili_login_error(self):
        """The lone-BI path must preserve the login cause instead of Exit 1."""
        workdir = os.path.join(self.temp_dir, "bili_login_exit")
        os.makedirs(workdir)
        task = SubTask(
            uid=9, url="http://test.com", title="Video 9", duration="01:00",
            status="排队中 (BI)", workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                  "BR": 100.0, "GD": 100.0, "BI": 0.0, "WX": 100.0},
        )
        engine = self._engine()
        engine.task_map[task.uid] = task
        script = (
            "print('CredentialNoSessdataException: "
            "Credential 类未提供 sessdata 或者为空。', flush=True)\n"
            "raise SystemExit(1)\n"
        )
        cmd = [sys.executable, "-u", "-c", script]
        with patch.object(BatchEngine, "save_state", lambda self: None):
            ok = engine.run_process(task, "BI", cmd, start_pct=0.0, end_pct=100.0)

        self.assertFalse(ok)
        self.assertEqual(task.status, "失败 (BI)")
        self.assertIn("登录态失效", task.error or "")
        self.assertIn("login.py", task.error or "")

    # --- which stages get a watchdog ---

    def _run_single_stage(self, stage):
        """Drive the worker down the lone-remaining-upload path, capture kwargs."""
        workdir = os.path.join(self.temp_dir, f"single_{stage}")
        os.makedirs(workdir)
        task = SubTask(
            uid=3, url="http://test.com", title="Video 3", duration="01:00",
            status=f"排队中 ({stage})", workdir=workdir,
            pcts={"DL": 100.0, "TR": 100.0, "TL": 100.0, "MR": 100.0,
                  "BR": 100.0, "GD": 0.0, "BI": 0.0, "WX": 0.0},
        )
        engine = self._engine()
        engine.do_gdsync = stage == "GD"
        engine.do_bili = stage == "BI"
        engine.do_wechat = stage == "WX"
        engine.task_map[task.uid] = task

        marker = os.path.join(workdir, {
            "GD": ".synced", "BI": ".bili_uploaded", "WX": ".wechat_uploaded",
        }[stage])

        def fake_disk_truth(current_task):
            if os.path.exists(marker):
                current_task.status = "完成"

        captured = {}

        def fake_run_process(_task, _stage, _cmd, **kwargs):
            captured.update(kwargs)
            with open(marker, "w") as handle:
                handle.write("1")
            return True

        with patch.object(BatchEngine, "disk_truth", side_effect=fake_disk_truth), \
             patch.object(BatchEngine, "run_process", side_effect=fake_run_process), \
             patch.object(BatchEngine, "save_state", lambda self: None), \
             patch.object(BatchEngine, "_refresh_wechat_skip", lambda self, t: None), \
             patch.object(BatchEngine, "_get_upload_llm_settings", lambda self: (None, None)), \
             patch.object(BatchEngine, "_build_bili_cmd", lambda self, wd, **kw: ["bili-upload"]), \
             patch.object(BatchEngine, "_build_wechat_cmd", lambda self, wd, **kw: ["wechat-upload"]), \
             patch.object(batch_pro, "get_wechat_upload_skip_reason", lambda wd: None):
            engine.worker(task)
        self.assertEqual(task.status, "完成")
        return captured

    def test_lone_wechat_upload_gets_a_watchdog(self):
        parameters = inspect.signature(batch_pro.run_wechat_cmd).parameters
        self.assertEqual(
            parameters["stall_timeout"].default,
            batch_pro.UPLOAD_STALL_TIMEOUT,
        )
        self.assertEqual(
            parameters["silence_timeout"].default,
            batch_pro.UPLOAD_SILENCE_TIMEOUT,
        )

    def test_lone_bili_upload_gets_a_watchdog(self):
        parameters = inspect.signature(batch_pro.run_bili_cmd).parameters
        self.assertEqual(
            parameters["stall_timeout"].default,
            batch_pro.UPLOAD_STALL_TIMEOUT,
        )
        self.assertEqual(
            parameters["silence_timeout"].default,
            batch_pro.UPLOAD_SILENCE_TIMEOUT,
        )

    def test_lone_gd_sync_gets_no_watchdog(self):
        kwargs = self._run_single_stage("GD")
        self.assertNotIn("stall_timeout", kwargs)
        self.assertNotIn("silence_timeout", kwargs)

    # --- launcher must hand the engine a working psutil ---

    def test_venv_site_packages_contains_psutil(self):
        site_packages = autosub_launcher.venv_site_packages()
        self.assertIsNotNone(site_packages)
        self.assertTrue(os.path.isdir(os.path.join(site_packages, "psutil")))

    def test_launch_engine_exports_venv_site_packages_on_pythonpath(self):
        """Stripping PYTHONPATH is what silently disabled every kill path."""
        saved = os.environ.get("PYTHONPATH")

        def restore():
            if saved is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = saved

        self.addCleanup(restore)
        fake_site = os.path.join(self.temp_dir, "site-packages")
        os.makedirs(fake_site)
        with patch.object(autosub_launcher, "venv_site_packages", return_value=fake_site), \
             patch.object(autosub_launcher.os, "execv"), \
             patch.object(autosub_launcher.subprocess, "Popen"):
            autosub_launcher.launch_engine([sys.executable, "autosub_batch_pro.py"])

        exported = os.environ["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(exported[0], fake_site)
        self.assertIn(autosub_launcher.CURRENT_DIR, exported)

if __name__ == "__main__":
    unittest.main()
