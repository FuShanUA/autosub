import asyncio
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

from PIL import Image


WECHAT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "wechat-channel-uploader"
    / "scripts"
    / "wechat_upload.py"
)
AUTOSUB_SCRIPT = Path(__file__).resolve().parents[1] / "autosub.py"


class FakeLLM:
    def __init__(self, response):
        self.response = response

    def generate_content(self, prompt, model_name=None, provider=None):
        return self.response


@unittest.skipUnless(os.path.isfile(AUTOSUB_SCRIPT), "AutoSUB script is unavailable")
class TestAutosubWechatCommand(unittest.TestCase):
    def _load_module(self):
        spec = importlib.util.spec_from_file_location(
            "autosub_wechat_command_under_test", AUTOSUB_SCRIPT
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        return spec, module

    def _make_upload_fixture(self, temp_dir):
        python = os.path.join(temp_dir, "python")
        script = os.path.join(temp_dir, "wechat_upload.py")
        video = os.path.join(temp_dir, "video_hardsub.mp4")
        for path in (python, script, video):
            with open(path, "wb") as handle:
                handle.write(b"1")
        return python, script, video

    def test_wechat_profile_defaults_to_uploader_assets(self):
        spec, module = self._load_module()
        expected_profile = (
            Path(__file__).resolve().parents[1]
            / "wechat-channel-uploader"
            / "assets"
            / "browser_data"
        )
        try:
            with patch.dict(os.environ):
                os.environ.pop("WECHAT_BROWSER_DATA_DIR", None)
                spec.loader.exec_module(module)

            self.assertEqual(
                Path(module.WECHAT_BROWSER_DATA_DIR), expected_profile
            )
            self.assertEqual(
                module.os.environ["WECHAT_BROWSER_DATA_DIR"],
                str(expected_profile),
            )
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)

    def test_launcher_upload_command_is_headless(self):
        spec, module = self._load_module()
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp_dir:
                _python, script, video = self._make_upload_fixture(temp_dir)

                with patch.object(module, "WECHAT_SCRIPT", script), \
                     patch.object(module, "WECHAT_PYTHON", _python), \
                     patch.object(module, "find_hardsub_video", lambda _workdir: video), \
                     patch.object(module, "get_video_duration", lambda _path: 600):
                    cmd = module.build_wechat_cmd(temp_dir)

                self.assertIsNotNone(cmd)
                self.assertEqual(cmd[-1], "--headless")
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)

    def test_wechat_command_accepts_video_over_ten_minutes(self):
        spec, module = self._load_module()
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp_dir:
                python, script, video = self._make_upload_fixture(temp_dir)

                with patch.object(module, "WECHAT_SCRIPT", script), \
                     patch.object(module, "WECHAT_PYTHON", python), \
                     patch.object(module, "find_hardsub_video", lambda _workdir: video), \
                     patch.object(module, "get_video_duration", lambda _path: 601):
                    cmd = module.build_wechat_cmd(temp_dir)

                self.assertIsNotNone(cmd)
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)

    def test_wechat_command_accepts_exactly_ten_minutes(self):
        spec, module = self._load_module()
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp_dir:
                python, script, video = self._make_upload_fixture(temp_dir)

                with patch.object(module, "WECHAT_SCRIPT", script), \
                     patch.object(module, "WECHAT_PYTHON", python), \
                     patch.object(module, "find_hardsub_video", lambda _workdir: video), \
                     patch.object(module, "get_video_duration", lambda _path: 600):
                    cmd = module.build_wechat_cmd(temp_dir)

                self.assertIsNotNone(cmd)
                self.assertEqual(cmd[3], video)
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)

    def test_wechat_command_rejects_unknown_duration(self):
        spec, module = self._load_module()
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp_dir:
                python, script, video = self._make_upload_fixture(temp_dir)

                with patch.object(module, "WECHAT_SCRIPT", script), \
                     patch.object(module, "WECHAT_PYTHON", python), \
                     patch.object(module, "find_hardsub_video", lambda _workdir: video), \
                     patch.object(module, "get_video_duration", lambda _path: 0):
                    cmd = module.build_wechat_cmd(temp_dir)

                self.assertIsNone(cmd)
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)

    def test_missing_script_reason_includes_script_path(self):
        spec, module = self._load_module()
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp_dir:
                _python, _script, video = self._make_upload_fixture(temp_dir)
                missing_script = os.path.join(temp_dir, "missing_wechat_upload.py")

                with patch.object(module, "WECHAT_SCRIPT", missing_script), \
                     patch.object(module, "WECHAT_PYTHON", _python), \
                     patch.object(module, "find_hardsub_video", lambda _workdir: video), \
                     patch.object(module, "get_video_duration", lambda _path: 601):
                    reason = module.get_wechat_upload_skip_reason(temp_dir)

                self.assertEqual(reason, f"WeChat upload script not found: {missing_script}")
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)

    def test_wechat_trigger_runs_upload_over_ten_minutes(self):
        spec, module = self._load_module()
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp_dir:
                python, script, video = self._make_upload_fixture(temp_dir)
                logs = []
                commands = []

                with patch.object(module, "WECHAT_SCRIPT", script), \
                     patch.object(module, "WECHAT_PYTHON", python), \
                     patch.object(module, "find_hardsub_video", lambda _workdir: video), \
                     patch.object(module, "get_video_duration", lambda _path: 601), \
                     patch.object(
                         module,
                         "run_wechat_cmd",
                         lambda cmd, *_args, **_kwargs: commands.append(cmd) or True,
                     ):
                    ok = module.trigger_wechat_upload(
                        temp_dir, on_log=logs.append
                    )

                self.assertTrue(ok)
                self.assertEqual(len(commands), 1)
                self.assertFalse(any("10 分钟限制" in message for message in logs))
        finally:
            sys.modules.pop("autosub_wechat_command_under_test", None)


@unittest.skipUnless(os.path.isfile(WECHAT_SCRIPT), "WeChat uploader is not installed")
class TestWechatUploadTitle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.video = os.path.join(self.temp_dir, "filename_hardsub.mp4")
        self.subtitle = os.path.join(self.temp_dir, "filename.bi.srt")
        with open(self.video, "wb") as handle:
            handle.write(b"video")
        with open(self.subtitle, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:04,000\n"
                "一个FDE最主要的职能，\n"
                "The primary function of an FDE\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\n"
                "是建立实施文化，在里面守住核心优势。\n"
                "is building a culture that maintains the alpha.\n"
            )

        spec = importlib.util.spec_from_file_location("wechat_upload_under_test", WECHAT_SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        sys.modules.pop("wechat_upload_under_test", None)

    def _cover_page(self, nodes_attached=True):
        class FakeLocator:
            async def count(self):
                return 1 if nodes_attached else 0

        class FakePage:
            def locator(self, _selector):
                return FakeLocator()

        return FakePage()

    def test_find_ffprobe_uses_homebrew_fallback_on_macos(self):
        with patch.object(self.module.shutil, "which", return_value=None), \
             patch.object(self.module.sys, "platform", "darwin"), \
             patch.object(
                 self.module.os.path,
                 "exists",
                 side_effect=lambda path: path == "/opt/homebrew/bin/ffprobe",
             ):
            ffprobe = self.module._find_ffprobe()

        self.assertEqual(ffprobe, "/opt/homebrew/bin/ffprobe")

    def test_normalize_wechat_title_removes_curly_quotes(self):
        title = self.module._normalize_wechat_title("Palantir’s卡普谈Tru")

        self.assertNotIn("’", title)
        self.assertNotIn("'", title)
        self.assertLessEqual(len(title), self.module.WECHAT_TITLE_MAX_CHARS)

    def test_known_draft_description_prevents_duplicate_upload(self):
        known = {("已有描述", "2026年11月15日 20:31")}

        self.assertTrue(
            self.module._known_draft_has_description(known, "已有 描述")
        )
        self.assertFalse(
            self.module._known_draft_has_description(known, "另一条描述")
        )

    def test_cover_canvas_selector_is_orientation_aware(self):
        self.assertEqual(
            self.module._cover_dialog_canvas_selector("portrait"),
            ".cover-croppie.vertical canvas.cr-image",
        )
        self.assertIn(
            ".cover-croppie.horizon canvas.cr-image",
            self.module._cover_dialog_canvas_selector("share"),
        )

    def test_expected_cover_ratio_matches_card_shape(self):
        self.assertAlmostEqual(self.module._expected_cover_ratio("portrait"), 3 / 4)
        self.assertAlmostEqual(self.module._expected_cover_ratio("share"), 4 / 3)

    def test_wait_cover_dialog_canvas_accepts_bound_cover_ratio(self):
        sizes = iter([
            {"width": 1920, "height": 802},
            {"width": 1080, "height": 810},
        ])

        async def fake_size(_page, _orientation):
            return next(sizes)

        async def fake_sleep(_seconds):
            return None

        with patch.object(self.module, "_cover_dialog_canvas_size", fake_size), \
             patch.object(self.module.asyncio, "sleep", fake_sleep):
            result = asyncio.run(
                self.module._wait_cover_dialog_canvas(object(), "share", seconds=30)
            )

        self.assertTrue(result)

    def test_wait_cover_dialog_canvas_rejects_video_frame_ratio(self):
        async def fake_size(_page, _orientation):
            return {"width": 1920, "height": 802}

        async def fake_sleep(_seconds):
            return None

        clock = {"now": 0.0}

        def fake_time():
            clock["now"] += 1.0
            return clock["now"]

        with patch.object(self.module, "_cover_dialog_canvas_size", fake_size), \
             patch.object(self.module.asyncio, "sleep", fake_sleep), \
             patch.object(self.module.time, "time", fake_time):
            result = asyncio.run(
                self.module._wait_cover_dialog_canvas(object(), "portrait", seconds=3)
            )

        self.assertFalse(result)

    def test_draft_entries_dedupe_repeated_page_reads(self):
        entries = [
            {"page": 1, "index": 0, "title": "同描述", "time": "21:43"},
            {"page": 1, "index": 0, "title": "同描述", "time": "21:43"},
            {"page": 2, "index": 0, "title": "同描述", "time": "21:43"},
            {"page": 1, "index": 1, "title": "另一条", "time": "21:44"},
        ]

        result = self.module._dedupe_draft_entries(entries)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "同描述")
        self.assertEqual(result[1]["title"], "另一条")

    def test_auto_title_comes_from_subtitle_not_filename(self):
        fake = FakeLLM(
            '{"title":"FDE守住核心优势","evidence":["守住核心优势"]}'
        )
        with patch.object(self.module, "_get_llm_client", lambda: fake), \
             patch.object(self.module, "_LLMProvider", object):
            title = self.module.build_title(self.video, "Filename-derived title")

        self.assertEqual(title, "FDE守住核心优势")
        self.assertLessEqual(len(title), self.module.WECHAT_TITLE_MAX_CHARS)

    def test_invalid_llm_title_falls_back_to_exact_subtitle_clause(self):
        fake = FakeLLM(
            '{"title":"Palantir CEO卡普揭秘FDE","evidence":["一个FDE最主要的职能"]}'
        )
        with patch.object(self.module, "_get_llm_client", lambda: fake), \
             patch.object(self.module, "_LLMProvider", object):
            title = self.module.build_title(self.video, "Filename-derived title")

        with open(self.subtitle, encoding="utf-8") as handle:
            subtitle_text = handle.read()
        self.assertIn(title, subtitle_text)
        self.assertNotIn("filename", title.lower())

    def test_form_and_cover_titles_use_separate_generation_styles(self):
        calls = []

        def fake_build(video, **kwargs):
            calls.append(kwargs["title_style"])
            return SimpleNamespace(
                title="FDE守住核心优势",
                evidence=("守住核心优势",),
                subtitle_path=self.subtitle,
                source="test",
            )

        with patch.object(self.module, "build_subtitle_grounded_title", fake_build):
            form_title = self.module.build_title(
                self.video,
                "Filename-derived title",
            )
            cover_title = self.module.build_cover_title(self.video)

        self.assertEqual(form_title, "FDE守住核心优势")
        self.assertEqual(cover_title, "FDE守住核心优势")
        self.assertEqual(calls, ["plain", "cover_headline"])

    def test_final_title_validation_uses_full_subtitle(self):
        long_video = os.path.join(self.temp_dir, "long_hardsub.mp4")
        long_subtitle = os.path.join(self.temp_dir, "long.cn.srt")
        target = "先找出主要瓶颈再集中疏通"
        blocks = []
        for index in range(1, 602):
            text = target if index % 2 == 0 else f"这是第{index}条字幕内容"
            start = index - 1
            end = index
            blocks.append(
                f"{index}\n00:{start // 60:02d}:{start % 60:02d},000 --> "
                f"00:{end // 60:02d}:{end % 60:02d},000\n{text}\n"
            )
        with open(long_video, "wb") as handle:
            handle.write(b"video")
        with open(long_subtitle, "w", encoding="utf-8") as handle:
            handle.write("\n".join(blocks))

        sampled, _ = self.module.read_strict_subtitle_text(long_video)
        self.assertNotIn(target, sampled or "")

        grounded = SimpleNamespace(
            title=target,
            evidence=(target,),
            subtitle_path=long_subtitle,
            source="test",
        )
        with patch.object(
            self.module,
            "build_subtitle_grounded_title",
            lambda *_args, **_kwargs: grounded,
        ):
            title = self.module.build_title(long_video, "Filename-derived title")

        self.assertEqual(title, target)

    def test_upload_stops_without_subtitle(self):
        os.remove(self.subtitle)
        with patch.object(self.module, "_get_video_duration_seconds", lambda _path: 600):
            ok = asyncio.run(
                self.module.upload_one_video(self.video, "Filename-derived title")
            )
        self.assertFalse(ok)

    def test_upload_allows_video_over_ten_minutes_to_reach_title_generation(self):
        class ReachedTitleGeneration(Exception):
            pass

        with patch.object(
            self.module, "_get_video_duration_seconds", lambda _path: 601
        ), patch.object(
            self.module,
            "build_title",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ReachedTitleGeneration("title generation must run")
            ),
        ):
            with self.assertRaises(ReachedTitleGeneration):
                asyncio.run(
                self.module.upload_one_video(self.video, "Filename-derived title")
                )

    def test_wechat_duration_check_only_rejects_unknown_duration(self):
        with patch.object(self.module, "_get_video_duration_seconds", lambda _path: 600):
            self.assertTrue(self.module._within_wechat_duration_limit(self.video))
        with patch.object(self.module, "_get_video_duration_seconds", lambda _path: 601):
            self.assertTrue(self.module._within_wechat_duration_limit(self.video))
        with patch.object(self.module, "_get_video_duration_seconds", lambda _path: 0):
            self.assertFalse(self.module._within_wechat_duration_limit(self.video))

    def test_upload_flow_allows_slow_wechat_dom_load(self):
        class NavigationObserved(Exception):
            pass

        class FakePage:
            url = self.module.WECHAT_HOME_URL

            def __init__(self):
                self.calls = []

            async def goto(self, url, **kwargs):
                self.calls.append((url, kwargs))
                raise NavigationObserved()

        class FakeContext:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        class FakeProfileLock:
            def __init__(self):
                self.released = False

            async def release(self):
                self.released = True

        class FakePlaywright:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return False

        page = FakePage()
        context = FakeContext()
        profile_lock = FakeProfileLock()

        async def fake_ensure_browser(_playwright, headless=False):
            return context, page, profile_lock

        with patch.object(self.module, "_within_wechat_duration_limit", lambda _path: True), \
             patch.object(self.module, "_load_cached_upload_metadata", lambda _path: {}), \
             patch.object(self.module, "build_description", lambda *_args, **_kwargs: "description"), \
             patch.object(self.module, "_read_subtitle_text", lambda _path: ("", None)), \
             patch.object(
                 self.module,
                 "_generate_wechat_covers",
                 lambda *_args, **_kwargs: ("portrait.jpg", "share.jpg"),
             ), \
             patch.object(self.module, "_ensure_browser", fake_ensure_browser), \
             patch("playwright.async_api.async_playwright", lambda: FakePlaywright()):
            ok = asyncio.run(
                self.module.upload_one_video(
                    self.video,
                    "上传标题",
                    desc="description",
                    tags=["人工智能"],
                    auto_title=False,
                    cover_title="封面标题",
                    max_retries=1,
                )
            )

        self.assertFalse(ok)
        self.assertEqual(
            page.calls,
            [
                (
                    self.module.WECHAT_HOME_URL,
                    {
                        "wait_until": "domcontentloaded",
                        "timeout": self.module.WECHAT_PAGE_LOAD_TIMEOUT_MS,
                    },
                )
            ],
        )
        self.assertEqual(self.module.WECHAT_PAGE_LOAD_TIMEOUT_MS, 60000)
        self.assertTrue(context.closed)
        self.assertTrue(profile_lock.released)

    def test_cover_title_is_independent_from_form_title(self):
        calls = []

        def fake_generate_platform_covers(video, platform, title):
            calls.append((title, platform))
            return (
                os.path.join(self.temp_dir, "video.cover.wechat.jpg"),
                os.path.join(self.temp_dir, "video.cover.wechat.share.jpg"),
            )

        with patch.object(
            self.module, "generate_platform_covers", fake_generate_platform_covers
        ):
            portrait, share = self.module._generate_wechat_covers(
                self.video,
                "FDE的核心职能：抽象产品",
                cover_title="抽象产品",
            )

        self.assertTrue(portrait.endswith(".cover.wechat.jpg"))
        self.assertTrue(share.endswith(".cover.wechat.share.jpg"))
        self.assertEqual(
            calls,
            [
                ("抽象产品", "wechat"),
            ],
        )

    def test_cover_title_defaults_to_form_title(self):
        calls = []

        def fake_generate_platform_covers(video, platform, title):
            calls.append(title)
            return ("portrait.jpg", "share.jpg")

        with patch.object(
            self.module, "generate_platform_covers", fake_generate_platform_covers
        ):
            self.module._generate_wechat_covers(
                self.video,
                "FDE的核心职能：抽象产品",
                cover_title="   ",
            )

        self.assertEqual(calls, ["FDE的核心职能：抽象产品"])

    def test_upload_uses_cached_metadata_before_llm(self):
        metadata_path = os.path.join(self.temp_dir, "upload_metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "description": "cached description",
                    "wechat_title": "cached title",
                    "cover_title": "cached cover",
                },
                handle,
                ensure_ascii=False,
            )

        seen = {}

        def fake_build_description(_video, _title, description=None, *_args, **_kwargs):
            seen["description"] = description
            return description or "fallback"

        def fake_generate_covers(video, title, cover_title=None):
            seen["title"] = title
            seen["cover_title"] = cover_title
            return None, None

        with patch.object(
            self.module, "_within_wechat_duration_limit", lambda _path: True
        ), patch.object(
            self.module,
            "build_title",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("cached title must bypass LLM")
            ),
        ), patch.object(
            self.module, "build_description", fake_build_description
        ), patch.object(
            self.module,
            "build_cover_title",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("cached cover title must bypass LLM")
            ),
        ), patch.object(
            self.module, "_generate_wechat_covers", fake_generate_covers
        ):
            ok = asyncio.run(
                self.module.upload_one_video(self.video, "Filename-derived title")
            )

        self.assertFalse(ok)
        self.assertEqual(seen["description"], "cached description")
        self.assertEqual(seen["title"], "cached title")
        self.assertEqual(seen["cover_title"], "cached cover")

    def test_wait_cover_editor_ready_waits_for_video_processing(self):
        texts = [
            "封面生成中",
            "预览图生成中",
            "视频处理中，请等待完成后再编辑",
            "",
        ]
        calls = {"count": 0}
        sleeps = []

        async def fake_wujie_text(page):
            index = min(calls["count"], len(texts) - 1)
            calls["count"] += 1
            return texts[index]

        async def fake_cover_editor_text(page):
            return ""

        async def fake_sleep(delay):
            sleeps.append(delay)

        fake_asyncio = SimpleNamespace(sleep=fake_sleep)
        with patch.object(self.module, "_wujie_text", fake_wujie_text), \
             patch.object(self.module, "_cover_editor_text", fake_cover_editor_text), \
             patch.object(self.module, "asyncio", fake_asyncio):
            ready = asyncio.run(
                self.module._wait_cover_editor_ready(
                    self._cover_page(), timeout=5
                )
            )

        self.assertTrue(ready)
        self.assertGreaterEqual(calls["count"], 5)
        self.assertIn(1, sleeps)

    def test_wait_cover_editor_ready_reads_hidden_tooltip(self):
        visible_texts = ["", ""]
        hidden_texts = ["预览图生成中，请等待完成后再编辑", ""]
        calls = {"count": 0}

        async def fake_wujie_text(page):
            return ""

        async def fake_cover_editor_text(page):
            index = min(calls["count"], len(hidden_texts) - 1)
            calls["count"] += 1
            return hidden_texts[index]

        async def fake_sleep(delay):
            return None

        fake_asyncio = SimpleNamespace(sleep=fake_sleep)
        with patch.object(self.module, "_wujie_text", fake_wujie_text), \
             patch.object(self.module, "_cover_editor_text", fake_cover_editor_text), \
             patch.object(self.module, "asyncio", fake_asyncio):
            ready = asyncio.run(
                self.module._wait_cover_editor_ready(
                    self._cover_page(), timeout=5
                )
            )

        self.assertTrue(ready)
        self.assertGreaterEqual(calls["count"], 5)

    def test_wait_cover_editor_ready_requires_real_cover_nodes(self):
        async def fake_wujie_text(_page):
            return ""

        async def fake_cover_editor_text(_page):
            return ""

        async def fake_sleep(_delay):
            return None

        fake_asyncio = SimpleNamespace(sleep=fake_sleep)
        with patch.object(self.module, "_wujie_text", fake_wujie_text), \
             patch.object(self.module, "_cover_editor_text", fake_cover_editor_text), \
             patch.object(self.module, "asyncio", fake_asyncio):
            ready = asyncio.run(
                self.module._wait_cover_editor_ready(
                    self._cover_page(nodes_attached=False), timeout=1
                )
            )

        self.assertFalse(ready)

    def test_cover_preview_matches_expected_card_ratio(self):
        self.assertTrue(
            self.module._cover_preview_matches_orientation(
                [{"naturalWidth": 810, "naturalHeight": 1080}], "portrait"
            )
        )
        self.assertTrue(
            self.module._cover_preview_matches_orientation(
                [{"naturalWidth": 810, "naturalHeight": 608}], "landscape"
            )
        )
        self.assertFalse(
            self.module._cover_preview_matches_orientation(
                [{"naturalWidth": 1280, "naturalHeight": 720}], "portrait"
            )
        )

    def test_wait_cover_preview_changed_requires_loaded_new_image(self):
        states = [
            [{"src": "old", "naturalWidth": 1280, "naturalHeight": 720}],
            [{"src": "new", "naturalWidth": 0, "naturalHeight": 0}],
            [{"src": "new", "naturalWidth": 810, "naturalHeight": 1080}],
        ]

        async def fake_cover_wrap_images(page, selector):
            index = min(fake_cover_wrap_images.calls, len(states) - 1)
            fake_cover_wrap_images.calls += 1
            return states[index]

        fake_cover_wrap_images.calls = 0
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        fake_asyncio = SimpleNamespace(sleep=fake_sleep)
        with patch.object(
            self.module, "_cover_wrap_images", fake_cover_wrap_images
        ), patch.object(self.module, "asyncio", fake_asyncio):
            changed = asyncio.run(
                self.module._wait_cover_preview_changed(
                    object(),
                    ".vertical-cover-wrap",
                    "portrait",
                    {"old"},
                    timeout=1,
                )
            )

        self.assertTrue(changed)

    def test_wait_cover_preview_changed_requires_pixel_match(self):
        states = [
            [{"src": "new", "naturalWidth": 810, "naturalHeight": 1080}],
        ]
        verification_calls = []

        async def fake_cover_wrap_images(page, selector):
            return states[0]

        async def fake_verify_cover(page, expected, orientation):
            verification_calls.append((expected, orientation))
            return len(verification_calls) > 1

        fake_asyncio = SimpleNamespace(sleep=_noop_sleep)
        with patch.object(
            self.module, "_cover_wrap_images", fake_cover_wrap_images
        ), patch.object(
            self.module, "_verify_cover_preview_matches", fake_verify_cover
        ), patch.object(self.module, "asyncio", fake_asyncio):
            changed = asyncio.run(
                self.module._wait_cover_preview_changed(
                    object(),
                    ".vertical-cover-wrap",
                    "portrait",
                    {"old"},
                    timeout=1,
                    expected_path="/tmp/portrait.jpg",
                )
            )

        self.assertTrue(changed)
        self.assertEqual(len(verification_calls), 2)
        self.assertEqual(
            verification_calls[0][0],
            {"local_path": "/tmp/portrait.jpg"},
        )

    def test_validate_cover_file_rejects_stale_title_marker(self):
        cover = os.path.join(self.temp_dir, "cover.jpg")
        Image.new("RGB", (1080, 1440), (216, 8, 0)).save(cover)

        with patch.object(self.module, "cover_style_matches", lambda *args: True):
            self.assertTrue(
                self.module._validate_cover_file(cover, "portrait", "抽象产品")
            )
        with patch.object(self.module, "cover_style_matches", lambda *args: False):
            self.assertFalse(
                self.module._validate_cover_file(cover, "portrait", "旧标题")
            )

    def test_wait_upload_complete_aborts_when_progress_freezes(self):
        """A frozen progress bar fails fast instead of burning the whole timeout.

        The page keeps polling WeChat's own telemetry while the upload is dead, so
        "still connected" says nothing. Only the percentage not moving does, and
        waiting out a 10000s timeout blocks the serial WX queue for hours.
        """
        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

            async def get_attribute(self, _name):
                return None

        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                return {
                    "pct": 67,
                    "source": "progress-node",
                    "detail": "67%",
                }

            def locator(self, _selector):
                return FakeLocator()

        clock = {"now": 0.0}

        def fake_time():
            clock["now"] += 200.0
            return clock["now"]

        with patch.object(self.module.time, "time", fake_time), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            result = asyncio.run(
                self.module._wait_upload_complete(FakePage(), timeout=10000)
            )

        self.assertFalse(result)
        self.assertLess(clock["now"], 10000.0)

    def test_wait_upload_complete_keeps_waiting_while_progress_moves(self):
        """Moving progress must not trip the stall guard."""
        state = {"pct": 10}

        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

            async def get_attribute(self, _name):
                return None

        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                state["pct"] = min(100, state["pct"] + 1)
                return {
                    "pct": state["pct"],
                    "source": "progress-node",
                    "detail": f"{state['pct']}%",
                }

            def locator(self, _selector):
                return FakeLocator()

        clock = {"now": 0.0}

        def fake_time():
            clock["now"] += 1.0
            return clock["now"]

        with patch.object(self.module.time, "time", fake_time), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            result = asyncio.run(
                self.module._wait_upload_complete(FakePage(), timeout=10000)
            )

        self.assertTrue(result)

    def test_wait_upload_complete_allows_long_processing_at_99_percent(self):
        """Near-complete server processing gets a longer stall budget."""
        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

            async def get_attribute(self, _name):
                return None

        class FakePage:
            calls = {"count": 0}

            async def evaluate(self, *_args, **_kwargs):
                self.calls["count"] += 1
                pct = 99 if self.calls["count"] == 1 else 0
                return {
                    "pct": pct,
                    "source": "aria-progressbar",
                    "detail": f"{pct}%",
                }

            def locator(self, _selector):
                return FakeLocator()

        clock = {"now": 0.0}

        def fake_time():
            clock["now"] += 200.0
            return clock["now"]

        with patch.object(self.module.time, "time", fake_time), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            result = asyncio.run(
                self.module._wait_upload_complete(FakePage(), timeout=10000)
            )

        self.assertFalse(result)
        self.assertGreaterEqual(clock["now"], self.module.UPLOAD_PROCESSING_STALL_SECONDS)
        self.assertLess(clock["now"], 10000.0)

    def test_wait_upload_complete_fails_when_page_reports_network_error(self):
        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                return {
                    "pct": None,
                    "source": "upload-error",
                    "detail": "网络出错，请重新上传",
                    "error": "网络出错，请重新上传",
                }

            def locator(self, _selector):
                return FakeLocator()

        with patch.object(self.module.asyncio, "sleep", _noop_sleep):
            result = asyncio.run(self.module._wait_upload_complete(FakePage(), timeout=10000))

        self.assertFalse(result)

    def test_wait_upload_complete_fails_when_no_progress_appears(self):
        class FakeLocator:
            def __init__(self):
                self.first = self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

            async def get_attribute(self, _name):
                return None

        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                return {
                    "pct": None,
                    "source": "no-progress",
                    "detail": "等待上传",
                }

            def locator(self, _selector):
                return FakeLocator()

        clock = {"now": 0.0}

        def fake_time():
            clock["now"] += 60.0
            return clock["now"]

        with patch.object(self.module.time, "time", fake_time), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            result = asyncio.run(
                self.module._wait_upload_complete(FakePage(), timeout=10000)
            )

        self.assertFalse(result)
        self.assertLess(clock["now"], 10000.0)

    def test_read_upload_progress_times_out_a_stuck_evaluate(self):
        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                await asyncio.sleep(0.05)
                return {"pct": 1}

        with patch.object(self.module, "UPLOAD_EVALUATE_TIMEOUT_SECONDS", 0.01):
            progress = asyncio.run(self.module._read_upload_progress(FakePage()))

        self.assertIsNone(progress["pct"])
        self.assertEqual(progress["source"], "evaluate-timeout")

    def test_read_upload_progress_prefers_largest_visible_candidate(self):
        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                return {
                    "candidates": [
                        {
                            "pct": 0,
                            "source": "aria-progressbar",
                            "detail": "0/100",
                            "visible": True,
                        },
                        {
                            "pct": 67,
                            "source": "progress-node",
                            "detail": "67%",
                            "visible": True,
                        },
                    ],
                    "pct": None,
                    "source": "no-progress",
                    "detail": "",
                }

        progress = asyncio.run(self.module._read_upload_progress(FakePage()))

        self.assertEqual(progress["pct"], 67)
        self.assertEqual(progress["source"], "progress-node")

    def test_read_upload_progress_ignores_hidden_larger_candidate(self):
        class FakePage:
            async def evaluate(self, *_args, **_kwargs):
                return {
                    "candidates": [
                        {
                            "pct": 72,
                            "source": "progress-node",
                            "detail": "72%",
                            "visible": False,
                        },
                        {
                            "pct": 0,
                            "source": "aria-progressbar",
                            "detail": "0/100",
                            "visible": True,
                        },
                    ],
                    "pct": None,
                    "source": "no-progress",
                    "detail": "",
                }

        progress = asyncio.run(self.module._read_upload_progress(FakePage()))

        self.assertEqual(progress["pct"], 0)
        self.assertEqual(progress["source"], "aria-progressbar")

    def test_noisy_wechat_request_failures_are_filtered(self):
        noisy = (
            ("blob:https://channels.weixin.qq.com/object", "net::ERR_ABORTED"),
            (
                "https://channels.weixin.qq.com/cgi-bin/.../report-perf?pf=web",
                "net::ERR_ABORTED",
            ),
            (
                "https://finder.video.qq.com/251/20302/stodownload?x=1",
                "net::ERR_ABORTED",
            ),
        )
        for url, failure in noisy:
            self.assertTrue(self.module._is_noisy_wechat_request_failure(url, failure))

        self.assertFalse(
            self.module._is_noisy_wechat_request_failure(
                "https://channels.weixin.qq.com/api/upload", "net::ERR_ABORTED"
            )
        )
        self.assertFalse(
            self.module._is_noisy_wechat_request_failure(
                "https://finder.video.qq.com/251/20302/stodownload?x=1",
                "net::ERR_CONNECTION_REFUSED",
            )
        )

    def test_upload_progress_label_separates_processing_after_near_completion(self):
        self.assertEqual(
            self.module._upload_progress_stage_label(
                {"source": "aria-progressbar"}, previous_pct=91
            ),
            "页面处理进度",
        )
        self.assertEqual(
            self.module._upload_progress_stage_label(
                {"source": "aria-progressbar"}, previous_pct=10
            ),
            "视频上传进度",
        )
        self.assertEqual(
            self.module._upload_progress_stage_label(
                {"source": "progress-node"}, previous_pct=91
            ),
            "视频上传进度",
        )

    def test_headless_login_failure_distinguishes_page_load_from_login_state(self):
        class FakePage:
            def __init__(self, module):
                self.module = module
                self.url = module.WECHAT_CREATE_URL

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "<html>create form did not load</html>"

        async def fake_wait_input(_page, timeout=45):
            self.assertEqual(timeout, 45)
            return False

        async def fake_wait_login(_page, timeout=600):
            raise AssertionError("headless upload must not wait for QR login")

        output = StringIO()
        with patch.object(self.module, "SKILL_DIR", self.temp_dir), \
             patch.object(self.module, "_wait_for_upload_input", fake_wait_input), \
             patch.object(self.module, "_wait_login", fake_wait_login), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep), \
             redirect_stdout(output):
            ok = asyncio.run(
                self.module._ensure_logged_in(
                    FakePage(self.module), allow_qr=False, timeout=45
                )
            )

        self.assertFalse(ok)
        self.assertIn("发布页加载失败", output.getvalue())
        self.assertIn("登录态可能仍有效", output.getvalue())
        self.assertNotIn("登录态不可用", output.getvalue())

    def test_required_publish_settings_apply_all_three_actions(self):
        class FakeLocator:
            def __init__(self, page, selector, has_text=None):
                self.page = page
                self.selector = selector
                self.has_text = has_text
                self.checked = False

            @property
            def first(self):
                return self

            async def count(self):
                return (
                    0
                    if "declare-original-dialog" in self.selector
                    or "post-with-link" in self.selector
                    else 1
                )

            async def click(self):
                if self.has_text:
                    self.page.actions.append(f"{self.selector} ({self.has_text})")
                else:
                    self.page.actions.append(self.selector)
                if "checkbox" in self.selector:
                    self.checked = True

            async def is_checked(self):
                return self.checked

        class FakePage:
            def __init__(self):
                self.actions = []

            def locator(self, selector, has_text=None):
                return FakeLocator(self, selector, has_text)

        page = FakePage()
        output = StringIO()
        with redirect_stdout(output):
            ok = asyncio.run(self.module._set_required_publish_settings(page))

        self.assertTrue(ok)
        self.assertEqual(
            page.actions,
            [
                ".post-position-wrap .position-display",
                ".post-position-wrap .option-item (不显示位置)",
                ".post-with-mark-tag .mark-tag-select .select-display",
                ".post-with-mark-tag .mark-tag-option (无需标注)",
                ".declare-original-checkbox .ant-checkbox-wrapper",
                ".declare-original-checkbox input[type='checkbox']",
            ],
        )
        self.assertIn("微信位置、视频标注、原创标志均已设置", output.getvalue())

    def test_location_option_waits_for_async_attachment(self):
        class DelayedOptionLocator:
            def __init__(self, page):
                self.page = page
                self.count_calls = 0

            @property
            def first(self):
                return self

            async def count(self):
                self.count_calls += 1
                return 0 if self.count_calls == 1 else 1

            async def click(self):
                self.page.actions.append("delayed-location-option")

        class FakePage:
            def __init__(self):
                self.actions = []
                self.option = DelayedOptionLocator(self)

            def locator(self, selector, has_text=None):
                if "option-item" in selector and has_text == "不显示位置":
                    return self.option
                return SimpleNamespace(
                    first=SimpleNamespace(
                        count=self._always_one,
                        click=self._click_display,
                    )
                )

            async def _always_one(self):
                return 1

            async def _click_display(self):
                self.actions.append("position-display")

        async def noop_sleep(_):
            return None

        page = FakePage()
        output = StringIO()
        with patch.object(self.module.asyncio, "sleep", noop_sleep), redirect_stdout(output):
            ok = asyncio.run(self.module._select_wechat_location_none(page))

        self.assertTrue(ok)
        self.assertEqual(page.actions, ["position-display", "delayed-location-option"])
        self.assertEqual(page.option.count_calls, 3)
        self.assertIn("微信位置已设置为不显示", output.getvalue())

    def test_required_publish_settings_can_be_read_back(self):
        class FakeLocator:
            def __init__(self, text="", checked=False):
                self.text = text
                self.checked = checked

            @property
            def first(self):
                return self

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def inner_text(self):
                return self.text

            async def is_checked(self):
                return self.checked

        class FakePage:
            def locator(self, selector):
                if "position-display" in selector:
                    return FakeLocator("不显示位置")
                if "mark-tag-select" in selector:
                    return FakeLocator("无需标注")
                if "declare-original-checkbox" in selector:
                    return FakeLocator(checked=True)
                return FakeLocator()

        output = StringIO()
        with redirect_stdout(output):
            ok = asyncio.run(
                self.module._verify_required_publish_settings(FakePage())
            )

        self.assertTrue(ok)
        self.assertIn("位置=不显示", output.getvalue())
        self.assertIn("标注=无需标注", output.getvalue())
        self.assertIn("原创=已选", output.getvalue())

    def test_ensure_required_publish_settings_reapplies_missing_state(self):
        class FakeLocator:
            def __init__(self, text="", checked=False):
                self.text = text
                self.checked = checked

            async def count(self):
                return 1

            async def inner_text(self):
                return self.text

            async def is_checked(self):
                return self.checked

        class FakePage:
            def __init__(self):
                self.calls = 0

            def locator(self, selector):
                if "position-display" in selector:
                    return FakeLocator("上海市")
                if "mark-tag-select" in selector:
                    return FakeLocator("无需标注")
                if "declare-original-checkbox" in selector:
                    return FakeLocator(checked=True)
                return FakeLocator()

        async def fake_set(_page):
            return True

        page = FakePage()
        output = StringIO()
        with redirect_stdout(output), patch.object(
            self.module,
            "_set_required_publish_settings",
            fake_set,
        ):
            ok = asyncio.run(
                self.module._ensure_required_publish_settings(page)
            )

        self.assertTrue(ok)
        self.assertIn("微信发布设置不完整，自动补设", output.getvalue())

    def test_same_page_cover_flow_sets_both_previews_without_draft_reopen(self):
        calls = []

        async def fake_upload(page, cover, orientation, title):
            calls.append((orientation, title))
            return {
                "src": f"https://example.test/{orientation}.jpg",
                "naturalWidth": 810 if orientation == "portrait" else 1080,
                "naturalHeight": 1080 if orientation == "portrait" else 810,
                "local_path": cover,
            }

        with patch.object(self.module, "_upload_wechat_cover_card", fake_upload), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            previews = asyncio.run(
                self.module._set_wechat_covers_same_page(
                    object(),
                    "portrait.jpg",
                    "share.jpg",
                    "抽象产品",
                )
            )

        self.assertEqual(
            calls,
            [("portrait", "抽象产品"), ("share", "抽象产品")],
        )
        self.assertEqual(set(previews), {"portrait", "share"})

    def test_sequential_cover_flow_saves_and_reopens_between_cards(self):
        calls = []
        portrait_preview = {
            "src": "https://example.test/portrait.jpg",
            "naturalWidth": 810,
            "naturalHeight": 1080,
            "local_path": "portrait.jpg",
        }
        share_preview = {
            "src": "https://example.test/share.jpg",
            "naturalWidth": 1080,
            "naturalHeight": 810,
            "local_path": "share.jpg",
        }

        async def fake_upload(page, cover, orientation, title):
            calls.append(("upload", orientation, title))
            return portrait_preview if orientation == "portrait" else share_preview

        async def fake_click(page, is_public):
            calls.append(("save", is_public))
            return "存草稿"

        async def fake_verify(
            page,
            title,
            desc,
            expected_covers=None,
            known_drafts=None,
            timeout=40,
        ):
            calls.append(("verify", expected_covers, known_drafts))
            return True

        with patch.object(self.module, "_upload_wechat_cover_card", fake_upload), \
             patch.object(self.module, "_click_final_action", fake_click), \
             patch.object(self.module, "_verify_draft_saved", fake_verify), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            previews = asyncio.run(
                self.module._set_wechat_covers_sequentially(
                    object(),
                    "portrait.jpg",
                    "share.jpg",
                    "抽象产品",
                    "FDE的核心职能：抽象产品",
                    "本次描述",
                    {"旧草稿"},
                )
            )

        self.assertEqual(
            calls,
            [
                ("upload", "portrait", "抽象产品"),
                ("save", False),
                ("verify", None, {"旧草稿"}),
                ("upload", "share", "抽象产品"),
                ("save", False),
                ("verify", previews, {"旧草稿"}),
            ],
        )
        self.assertEqual(set(previews), {"portrait", "share"})

    def test_cover_card_failure_is_retried_before_continuing(self):
        calls = []

        async def fake_upload(page, cover, orientation, title):
            calls.append(orientation)
            if orientation == "portrait" and len(calls) == 1:
                return False
            return {
                "src": f"https://example.test/{orientation}.jpg",
                "naturalWidth": 810 if orientation == "portrait" else 1080,
                "naturalHeight": 1080 if orientation == "portrait" else 810,
                "local_path": cover,
            }

        with patch.object(self.module, "_upload_wechat_cover_card", fake_upload):
            previews = asyncio.run(
                self.module._set_wechat_covers_same_page(
                    object(),
                    "portrait.jpg",
                    "share.jpg",
                    "抽象产品",
                )
            )

        self.assertEqual(calls, ["portrait", "portrait", "share"])
        self.assertEqual(set(previews), {"portrait", "share"})

    def test_active_cover_file_input_selects_visible_dialog(self):
        class FakeLocator:
            def __init__(self, index):
                self.index = index

            async def count(self):
                return 2

            def nth(self, index):
                return f"input-{index}"

        class FakePage:
            def __init__(self):
                self.selectors = []

            async def evaluate(self, _script, title):
                self.title = title
                return 1

            def locator(self, selector):
                self.selectors.append(selector)
                return FakeLocator(1)

        page = FakePage()
        with patch.object(self.module.asyncio, "sleep", _noop_sleep):
            share_input = asyncio.run(
                self.module._active_cover_file_input(page, "share", timeout=1)
            )

        self.assertEqual(page.title, "编辑分享卡片")
        self.assertEqual(share_input, "input-1")

    def test_active_cover_file_input_does_not_fallback_to_another_cover(self):
        class FakePage:
            async def evaluate(self, _script, _title):
                return -1

        page = FakePage()
        with patch.object(self.module.asyncio, "sleep", _noop_sleep):
            share_input = asyncio.run(
                self.module._active_cover_file_input(page, "share", timeout=1)
            )

        self.assertIsNone(share_input)

    def test_cover_recommend_direct_edit_clicks_visible_button(self):
        class FakePage:
            def __init__(self):
                self.scripts = []

            async def evaluate(self, script):
                self.scripts.append(script)
                return True

        page = FakePage()
        clicked = asyncio.run(
            self.module._click_cover_recommend_direct_edit(page, timeout=1)
        )

        self.assertTrue(clicked)
        self.assertIn(".img-recommend-wrap .btn-directly-edit button", page.scripts[0])

    def test_cover_dialog_confirm_waits_out_disabled_button(self):
        states = [
            {"title": "编辑个人主页卡片", "mask": False, "clicked": False,
             "reason": "disabled",
             "buttons": [{"text": "确认", "disabled": True, "visible": True}]},
            {"title": "编辑个人主页卡片", "mask": True, "clicked": False,
             "reason": "masked",
             "buttons": [{"text": "确认", "disabled": False, "visible": True}]},
            {"title": "编辑个人主页卡片", "mask": False, "clicked": True,
             "reason": "clicked",
             "buttons": [{"text": "确认", "disabled": False, "visible": True}]},
        ]

        class FakePage:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, label):
                self.calls.append(label)
                return states[min(len(self.calls) - 1, len(states) - 1)]

        page = FakePage()
        with patch.object(self.module.asyncio, "sleep", _noop_sleep):
            confirmed = asyncio.run(
                self.module._click_cover_dialog_confirm(page, "portrait", timeout=5000)
            )

        self.assertTrue(confirmed)
        self.assertEqual(page.calls, ["确认", "确认", "确认"])

    def test_cover_dialog_confirm_falls_back_to_crop_dialog(self):
        responses = {
            "确认": {"title": "裁剪封面图", "mask": False, "clicked": False,
                     "reason": "not-found", "buttons": []},
            "确定": {"title": "裁剪封面图", "mask": False, "clicked": True,
                     "reason": "clicked",
                     "buttons": [{"text": "确定", "disabled": False, "visible": True}]},
        }

        class FakePage:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, label):
                self.calls.append(label)
                return responses[label]

        page = FakePage()
        with patch.object(self.module.asyncio, "sleep", _noop_sleep):
            confirmed = asyncio.run(
                self.module._click_cover_dialog_confirm(page, "portrait", timeout=5000)
            )

        self.assertTrue(confirmed)
        self.assertEqual(page.calls, ["确认", "确定"])

    def test_cover_dialog_confirm_timeout_saves_debug(self):
        class FakePage:
            async def evaluate(self, _script, _label):
                return {"title": "", "mask": False, "clicked": False,
                        "reason": "no-cover-dialog", "buttons": []}

        saved = []

        async def fake_debug(_page, tag):
            saved.append(tag)

        with patch.object(self.module.asyncio, "sleep", _noop_sleep), patch.object(
            self.module, "_save_cover_dialog_debug", fake_debug
        ):
            confirmed = asyncio.run(
                self.module._click_cover_dialog_confirm(FakePage(), "portrait", timeout=1)
            )

        self.assertFalse(confirmed)
        self.assertEqual(saved, ["confirm_timeout"])

    def test_cover_dialog_confirm_script_scopes_to_cover_dialog(self):
        script = self.module._COVER_DIALOG_CONFIRM_JS
        self.assertIn(".edit-cover-dialog-container .weui-desktop-dialog__wrp", script)
        self.assertIn("setting-cover-mask", script)

    def _run_repair(self, nodes_ready_at_verify, entries=None, cached=None,
                    draft_time="09:03"):
        """Drive repair_existing_draft with a stubbed browser and draft list."""
        module = self.module
        opened = []
        temp_dir = tempfile.mkdtemp()
        video = os.path.join(temp_dir, "video_hardsub.mp4")
        with open(video, "wb") as handle:
            handle.write(b"1")
        if entries is None:
            entries = [{"index": 1, "title": "draft desc", "time": "2026年10月25日 09:03"}]
        metadata = {
            "wechat_title": "draft title",
            "description": "draft desc",
            "cover_title": "cover title",
        }
        metadata.update(cached or {})

        class FakeLock:
            async def release(self):
                return None

        class FakeContext:
            async def close(self):
                return None

        class FakePlaywright:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *_args):
                return False

        async def fake_ensure_browser(_p, headless=False):
            return FakeContext(), SimpleNamespace(url="https://channels.weixin.qq.com/platform/post/create"), FakeLock()

        async def fake_true(*_args, **_kwargs):
            return True

        async def fake_entries(_page):
            return list(entries)

        async def fake_open_entry(_page, entry):
            opened.append(entry["index"])
            return True

        async def fake_cover_card(_page, cover_path, orientation, _title):
            return {"naturalWidth": 810, "naturalHeight": 1080, "local_path": cover_path}

        async def fake_final_action(_page, _publish):
            return "存草稿"

        async def fake_nodes_ready(_page):
            # Only the post-loop verification probe reaches this stub.
            return nodes_ready_at_verify

        required_settings_calls = []

        async def fake_required_settings(_page):
            required_settings_calls.append(True)
            return True

        patches = [
            patch.object(module, "_load_cached_upload_metadata", lambda _p: dict(metadata)),
            patch.object(module, "_generate_wechat_covers",
                         lambda *_a, **_k: ("portrait.jpg", "share.jpg")),
            patch.object(module, "_ensure_browser", fake_ensure_browser),
            patch.object(module, "_goto_wechat_page", fake_true),
            patch.object(module, "_ensure_logged_in", fake_true),
            patch.object(module, "_open_draft_list", fake_true),
            patch.object(module, "_draft_list_entries", fake_entries),
            patch.object(module, "_open_draft_entry_by_signature", fake_open_entry),
            patch.object(module, "_set_required_publish_settings", fake_required_settings),
            patch.object(module, "_verify_required_publish_settings", fake_true),
            patch.object(module, "_wait_cover_editor_ready", fake_true),
            patch.object(module, "_upload_wechat_cover_card", fake_cover_card),
            patch.object(module, "_click_final_action", fake_final_action),
            patch.object(module, "_cover_editor_nodes_ready", fake_nodes_ready),
            patch.object(module, "_verify_draft_form_fields", fake_true),
            patch.object(module, "_verify_wechat_cover_previews", fake_true),
            patch.object(module.asyncio, "sleep", _noop_sleep),
            patch("playwright.async_api.async_playwright", lambda: FakePlaywright()),
        ]
        for item in patches:
            item.start()
        try:
            result = asyncio.run(module.repair_existing_draft(
                video, draft_time=draft_time, headless=True
            ))
        finally:
            for item in patches:
                item.stop()
        marker = os.path.join(temp_dir, ".wechat_uploaded")
        wrote_marker = os.path.isfile(marker)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return result, opened, wrote_marker, required_settings_calls

    def test_repair_verifies_open_draft_without_reopening(self):
        result, opened, wrote_marker, required_calls = self._run_repair(
            nodes_ready_at_verify=True
        )

        self.assertTrue(result)
        self.assertTrue(wrote_marker)
        self.assertEqual(required_calls, [True])
        # Initial open plus one reopen per cover; verification stays in place.
        self.assertEqual(opened, [1, 1, 1])

    def test_repair_reopens_draft_when_editor_is_gone(self):
        result, opened, wrote_marker, required_calls = self._run_repair(
            nodes_ready_at_verify=False
        )

        self.assertTrue(result)
        self.assertTrue(wrote_marker)
        self.assertEqual(opened, [1, 1, 1, 1])

    def test_repair_uses_recorded_draft_time_from_metadata(self):
        """A recorded draft reference disambiguates duplicate drafts."""
        entries = [
            {"index": 0, "title": "draft desc", "time": "2026年10月25日 09:17"},
            {"index": 6, "title": "draft desc", "time": "2026年10月24日 22:02"},
        ]
        result, opened, wrote_marker, _required_calls = self._run_repair(
            nodes_ready_at_verify=True,
            entries=entries,
            cached={"wechat_draft_time": "09:17"},
            draft_time=None,
        )

        self.assertTrue(result)
        self.assertTrue(wrote_marker)
        self.assertEqual(opened[0], 0)

    def test_unverified_share_cover_still_saves_draft_and_succeeds(self):
        """A saved draft must not be reported as a failed upload."""
        module = self.module
        saves = []

        async def fake_cover_card(_page, cover_path, orientation, _title):
            if orientation == "portrait":
                return {"local_path": cover_path}
            return None

        async def fake_final_action(_page, _publish):
            saves.append("save")
            return "存草稿"

        async def fake_verify(*_args, **_kwargs):
            return True

        async def fake_record(*_args, **_kwargs):
            return None

        with patch.object(module, "_upload_wechat_cover_card", fake_cover_card), \
                patch.object(module, "_click_final_action", fake_final_action), \
                patch.object(module, "_verify_draft_saved", fake_verify), \
                patch.object(module, "_record_draft_reference", fake_record), \
                patch.object(module, "_close_cover_dialog", fake_verify), \
                patch.object(module.asyncio, "sleep", _noop_sleep):
            previews = asyncio.run(module._set_wechat_covers_sequentially(
                "page", "portrait.jpg", "share.jpg", "headline",
                "title", "desc", set(), video_path="video.mp4",
            ))

        self.assertIn("portrait", previews)
        self.assertTrue(previews["share"].get("unverified"))
        # One save for the portrait step, one to protect the upload after the
        # share cover could not be pixel-verified.
        self.assertEqual(len(saves), 2)

    def test_draft_exists_for_matches_description(self):
        module = self.module

        async def fake_open_list(_page):
            return True

        async def fake_entries(_page):
            return [{"index": 0, "title": "mine", "time": "12:59"}]

        with patch.object(module, "_open_draft_list", fake_open_list), patch.object(
            module, "_draft_list_entries", fake_entries
        ):
            self.assertTrue(asyncio.run(module._draft_exists_for("page", "mine")))
            self.assertFalse(asyncio.run(module._draft_exists_for("page", "other")))

    def test_record_draft_reference_refuses_duplicates(self):
        module = self.module

        async def fake_open_list(_page):
            return True

        async def fake_entries(_page):
            return [
                {"index": 0, "title": "same", "time": "09:17"},
                {"index": 1, "title": "same", "time": "22:02"},
            ]

        with patch.object(module, "_open_draft_list", fake_open_list), patch.object(
            module, "_draft_list_entries", fake_entries
        ):
            recorded = asyncio.run(module._record_draft_reference("/tmp/x.mp4", "page", "same"))

        self.assertIsNone(recorded)

    def test_record_draft_reference_writes_metadata(self):
        module = self.module
        temp_dir = tempfile.mkdtemp()
        video = os.path.join(temp_dir, "video_hardsub.mp4")
        with open(video, "wb") as handle:
            handle.write(b"1")

        async def fake_open_list(_page):
            return True

        async def fake_entries(_page):
            return [{"index": 3, "title": "same", "time": "2026年10月25日 09:17"}]

        reopened = []

        async def fake_open_entry(_page, entry):
            reopened.append(entry["index"])
            return True

        async def fake_editor_ready(_page, timeout=1800):
            return True

        with patch.object(module, "_open_draft_list", fake_open_list), patch.object(
            module, "_draft_list_entries", fake_entries
        ), patch.object(module, "_open_draft_entry_by_signature", fake_open_entry), patch.object(
            module, "_wait_cover_editor_ready", fake_editor_ready
        ):
            recorded = asyncio.run(module._record_draft_reference(video, "page", "same"))

        try:
            self.assertEqual(recorded, "2026年10月25日 09:17")
            # The caller keeps working on the edit form, not the draft list.
            self.assertEqual(reopened, [3])
            with open(os.path.join(temp_dir, "upload_metadata.json"), encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored["wechat_draft_time"], "2026年10月25日 09:17")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_repair_draft_selection_requires_unique_match(self):
        entries = [
            {"index": 0, "title": "same description", "time": "23:36"},
            {"index": 6, "title": "same description", "time": "14:10"},
        ]

        self.assertIsNone(
            self.module._select_repair_draft_entry(entries, "same description")
        )
        self.assertEqual(
            self.module._select_repair_draft_entry(
                entries, "same description", draft_time="23:36"
            )["index"],
            0,
        )

    def test_repair_draft_selection_rejects_wrong_time(self):
        entries = [{"index": 0, "title": "same description", "time": "23:36"}]

        self.assertIsNone(
            self.module._select_repair_draft_entry(
                entries, "same description", draft_time="14:10"
            )
        )

    def test_open_draft_entry_ref_clicks_signature_not_stale_index(self):
        opened = []

        async def fake_goto(_page, _page_number):
            return True

        async def fake_entries(_page):
            return [{
                "index": 4,
                "title": "富士通为客户扩展核心优势",
                "time": "2026年10月31日 22:05",
            }]

        async def fake_open_entry(_page, index):
            opened.append(index)
            return True

        with patch.object(self.module, "_goto_draft_page", fake_goto), \
             patch.object(self.module, "_draft_list_entries", fake_entries), \
             patch.object(self.module, "_open_draft_entry", fake_open_entry):
            ok = asyncio.run(
                self.module._open_draft_entry_ref(
                    object(),
                    {
                        "index": 7,
                        "page": 2,
                        "title": "富士通为客户扩展核心优势",
                        "time": "2026年10月31日 22:05",
                    },
                )
            )

        self.assertTrue(ok)
        self.assertEqual(opened, [4])

    def test_draft_cover_readback_failure_is_failure(self):
        async def fake_open_draft_list(page):
            return True

        async def fake_draft_entries(page):
            return [{"index": 0, "title": "本次描述", "time": "2026年10月10日 23:00"}]

        async def fake_open_draft_entry(page, entry):
            return True

        async def fake_wait(page, timeout=0):
            return True

        async def fake_verify_fields(page, title, desc):
            return title == "FDE的核心职能：抽象产品" and desc == "本次描述"

        async def fake_verify_covers(page, previews):
            return False

        with patch.object(self.module, "_open_draft_list", fake_open_draft_list), \
             patch.object(self.module, "_draft_list_entries", fake_draft_entries), \
             patch.object(self.module, "_open_draft_entry_by_signature", fake_open_draft_entry), \
             patch.object(self.module, "_wait_cover_editor_ready", fake_wait), \
             patch.object(self.module, "_verify_draft_form_fields", fake_verify_fields), \
             patch.object(self.module, "_verify_wechat_cover_previews", fake_verify_covers), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(
                self.module._verify_draft_saved(
                    object(),
                    "FDE的核心职能：抽象产品",
                    "本次描述",
                    expected_covers={"portrait": {}, "share": {}},
                )
            )

        self.assertFalse(ok)

    def test_draft_cover_preview_missing_is_warning_not_failure(self):
        results = {"portrait": None, "share": True}

        async def fake_verify(page, expected, orientation):
            self.assertEqual(expected, {"local_path": f"/tmp/{orientation}.jpg"})
            return results[orientation]

        with patch.object(
            self.module, "_verify_cover_preview_matches", fake_verify
        ):
            ok = asyncio.run(
                self.module._verify_wechat_cover_previews(
                    object(),
                    {
                        "portrait": {"local_path": "/tmp/portrait.jpg"},
                        "share": {"local_path": "/tmp/share.jpg"},
                    },
                )
            )

        self.assertTrue(ok)

    def test_draft_cover_preview_mismatch_is_failure(self):
        results = {"portrait": False, "share": True}

        async def fake_verify(page, expected, orientation):
            return results[orientation]

        with patch.object(
            self.module, "_verify_cover_preview_matches", fake_verify
        ):
            ok = asyncio.run(
                self.module._verify_wechat_cover_previews(
                    object(),
                    {
                        "portrait": {"local_path": "/tmp/portrait.jpg"},
                        "share": {"local_path": "/tmp/share.jpg"},
                    },
                )
            )

        self.assertFalse(ok)

    def test_upload_rejects_unconfirmed_covers(self):
        async def fake_same_page(*args, **kwargs):
            return False

        with patch.object(self.module, "_set_wechat_covers_same_page", fake_same_page):
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    self.module._set_wechat_covers_for_upload(
                        object(),
                        "portrait.jpg",
                        "share.jpg",
                        "抽象产品",
                    )
                )

    def test_verify_draft_saved_checks_form_title_and_both_covers(self):
        opened = []

        async def fake_open_draft_list(page):
            return True

        async def fake_draft_entries(page):
            return [
                {"index": 0, "title": "旧描述", "time": "2026年09月18日 12:52"},
                {"index": 1, "title": "本次描述", "time": "2026年09月18日 15:13"},
            ]

        async def fake_open_draft_entry(page, entry):
            opened.append(entry["index"])
            return True

        async def fake_wait(page, timeout=0):
            return True

        async def fake_verify_fields(page, title, desc):
            self.assertEqual(title, "FDE的核心职能：抽象产品")
            self.assertEqual(desc, "本次描述")
            return True

        async def fake_verify_covers(page, previews):
            self.assertEqual(set(previews), {"portrait", "share"})
            return True

        with patch.object(self.module, "_open_draft_list", fake_open_draft_list), \
             patch.object(self.module, "_draft_list_entries", fake_draft_entries), \
             patch.object(self.module, "_open_draft_entry_by_signature", fake_open_draft_entry), \
             patch.object(self.module, "_wait_cover_editor_ready", fake_wait), \
             patch.object(self.module, "_verify_draft_form_fields", fake_verify_fields), \
             patch.object(self.module, "_verify_wechat_cover_previews", fake_verify_covers), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(
                self.module._verify_draft_saved(
                    object(),
                    "FDE的核心职能：抽象产品",
                    "本次描述",
                    expected_covers={"portrait": {}, "share": {}},
                    known_drafts={("旧描述", "2026年09月18日12:52")},
                )
            )

        self.assertTrue(ok)
        self.assertEqual(opened, [1])

    def test_verify_draft_saved_without_covers_skips_editor_wait(self):
        async def fake_open_draft_list(page):
            return True

        async def fake_draft_entries(page):
            return [{"index": 0, "title": "本次描述", "time": "2026年09月28日 22:19"}]

        async def fake_open_draft_entry(page, entry):
            return True

        async def fake_wait(page, timeout=0):
            raise AssertionError("cover editor must not block a coverless draft check")

        async def fake_verify_fields(page, title, desc):
            return title == "FDE的核心职能：抽象产品" and desc == "本次描述"

        with patch.object(self.module, "_open_draft_list", fake_open_draft_list), \
             patch.object(self.module, "_draft_list_entries", fake_draft_entries), \
             patch.object(self.module, "_open_draft_entry_by_signature", fake_open_draft_entry), \
             patch.object(self.module, "_wait_cover_editor_ready", fake_wait), \
             patch.object(self.module, "_verify_draft_form_fields", fake_verify_fields), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(
                self.module._verify_draft_saved(
                    object(),
                    "FDE的核心职能：抽象产品",
                    "本次描述",
                    expected_covers=None,
                )
            )

        self.assertTrue(ok)

    def test_resource_timeout_saves_draft_before_retrying_covers(self):
        class FakePage:
            url = self.module.WECHAT_CREATE_URL

        calls = []

        async def fake_click(page, is_public):
            calls.append(("click", is_public))
            return "存草稿"

        async def fake_verify(page, title, desc, expected_covers=None, known_drafts=None, timeout=40):
            calls.append(("verify", title, desc, expected_covers, known_drafts))
            return True

        with patch.object(self.module, "_click_final_action", fake_click), \
             patch.object(self.module, "_verify_draft_saved", fake_verify), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(
                self.module._save_draft_after_resource_timeout(
                    FakePage(),
                    "FDE的核心职能：抽象产品",
                    "本次描述",
                    {"旧草稿"},
                )
            )

        self.assertTrue(ok)
        self.assertEqual(calls[0], ("click", False))
        self.assertEqual(
            calls[1],
            (
                "verify",
                "FDE的核心职能：抽象产品",
                "本次描述",
                None,
                {"旧草稿"},
            ),
        )

    def test_wait_draft_list_ready_accepts_zero_count_empty_state(self):
        class FakePage:
            url = self.module.WECHAT_DRAFT_URL

        async def fake_entries(_page):
            return []

        async def fake_text(_page):
            return "草稿箱 (0)"

        with patch.object(self.module, "_draft_list_entries", fake_entries), \
             patch.object(self.module, "_wujie_text", fake_text):
            ready = asyncio.run(
                self.module._wait_draft_list_ready(FakePage(), timeout=1)
            )

        self.assertTrue(ready)

    def test_menu_draft_click_expands_folded_sidebar(self):
        scripts = []

        class FakePage:
            async def evaluate(self, script, *args):
                scripts.append(script)
                return True

        with patch.object(self.module.asyncio, "sleep", _noop_sleep):
            clicked = asyncio.run(self.module._click_menu_draft_link(FakePage()))

        self.assertTrue(clicked)
        self.assertTrue(any("fold-sidebar" in script for script in scripts))
        self.assertTrue(any("内容管理" in script for script in scripts))
        self.assertTrue(any("草稿箱" in script for script in scripts))

    def test_cover_failure_stops_upload_instead_of_saving_draft(self):
        async def fake_same_page(*args, **kwargs):
            return False

        with patch.object(
            self.module, "_set_wechat_covers_same_page", fake_same_page
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    self.module._set_wechat_covers_for_upload(
                        object(), "portrait.jpg", "share.jpg", "抽象产品"
                    )
                )

    def test_draft_mode_never_falls_back_to_publish_button(self):
        attempted = []

        async def fake_try_selectors(page, selectors, description="element"):
            attempted.append(list(selectors))
            return None

        with patch.object(self.module, "_try_selectors", fake_try_selectors):
            with patch.object(
                self.module,
                "_confirm_wechat_original_dialog",
                _noop_async_true,
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(self.module._click_final_action(object(), False))

        self.assertEqual(attempted, [self.module.SAVE_DRAFT_SELECTORS])

    def test_final_action_uses_dom_click_when_dialog_overlaps_button(self):
        class OverlayBlockedButton:
            def __init__(self):
                self.dom_clicked = False

            async def click(self):
                raise RuntimeError("dialog intercepts pointer events")

            async def evaluate(self, _script):
                self.dom_clicked = True
                return True

        button = OverlayBlockedButton()

        async def fake_try_selectors(_page, _selectors, description="element"):
            return button

        with patch.object(self.module, "_try_selectors", fake_try_selectors), \
             patch.object(
                 self.module,
                 "_confirm_wechat_original_dialog",
                 _noop_async_true,
             ):
            action = asyncio.run(self.module._click_final_action(object(), False))

        self.assertEqual(action, "存草稿")
        self.assertTrue(button.dom_clicked)

    def test_ensure_logged_in_proves_login_with_create_form(self):
        class FakePage:
            def __init__(self, module):
                self.module = module
                self.url = module.WECHAT_CREATE_URL
                self.visited = []

            async def goto(self, url, **_kwargs):
                self.visited.append(url)
                self.url = url

            async def content(self):
                return "<html>login check</html>"

        page = FakePage(self.module)
        input_results = []

        async def fake_wait_input(_page, timeout=15):
            input_results.append(timeout)
            return len(input_results) == 2

        async def fake_wait_login(_page, timeout=600):
            return True

        with patch.object(self.module, "_wait_for_upload_input", fake_wait_input), \
             patch.object(self.module, "_wait_login", fake_wait_login), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(self.module._ensure_logged_in(page))

        self.assertTrue(ok)
        self.assertIn(self.module.WECHAT_CREATE_URL, page.visited)
        self.assertIn(self.module.WECHAT_LOGIN_URL, page.visited)

    def test_wait_login_requires_upload_form_after_qr_scan(self):
        class FakePage:
            def __init__(self):
                self.url = "https://channels.weixin.qq.com/platform/home"
                self.visited = []

            async def goto(self, url, **_kwargs):
                self.visited.append(url)
                self.url = url

        page = FakePage()

        async def fake_check(_page):
            return True

        async def fake_wait_input(_page, timeout=10):
            return True

        with patch.object(self.module, "_check_logged_in", fake_check), \
             patch.object(self.module, "_wait_for_upload_input", fake_wait_input), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(self.module._wait_login(page, timeout=1))

        self.assertTrue(ok)
        self.assertEqual(page.visited, [self.module.WECHAT_CREATE_URL])

    def test_headless_mode_does_not_wait_for_qr(self):
        class FakePage:
            def __init__(self, module):
                self.module = module
                self.url = module.WECHAT_CREATE_URL

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "<html>login check</html>"

        async def fake_wait_input(_page, timeout=45):
            self.assertEqual(timeout, 45)
            return False

        async def fake_wait_login(_page, timeout=600):
            raise AssertionError("headless upload must not wait for QR login")

        with patch.object(self.module, "_wait_for_upload_input", fake_wait_input), \
             patch.object(self.module, "_wait_login", fake_wait_login), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(
                self.module._ensure_logged_in(
                    FakePage(self.module), allow_qr=False, timeout=45
                )
            )

        self.assertFalse(ok)

    def test_slow_create_page_gets_longer_login_grace(self):
        class FakePage:
            def __init__(self, module):
                self.module = module
                self.url = module.WECHAT_CREATE_URL

            async def goto(self, _url, **_kwargs):
                return None

        calls = []

        async def fake_wait_input(_page, timeout=45):
            calls.append(timeout)
            return True

        with patch.object(self.module, "_wait_for_upload_input", fake_wait_input), \
             patch.object(self.module.asyncio, "sleep", _noop_sleep):
            ok = asyncio.run(
                self.module._ensure_logged_in(FakePage(self.module), timeout=45)
            )

        self.assertTrue(ok)
        self.assertEqual(calls, [45])

    def test_browser_launch_is_headless_compatible_and_direct(self):
        launch_kwargs = {}

        class FakeContext:
            pages = []

            async def add_init_script(self, _script):
                return None

            async def new_page(self):
                return object()

        class FakeChromium:
            async def launch_persistent_context(self, **kwargs):
                launch_kwargs.update(kwargs)
                return FakeContext()

        class FakePlaywright:
            chromium = FakeChromium()

        async def launch_and_release():
            context, page, lock = await self.module._ensure_browser(
                FakePlaywright(), headless=True
            )
            await lock.release()
            return context, page

        with patch.object(self.module, "BROWSER_DATA_DIR", self.temp_dir), \
             patch.object(
                 self.module,
                 "BROWSER_LOCK_PATH",
                 os.path.join(self.temp_dir, "browser_data.lock"),
             ):
            _context, _page = asyncio.run(launch_and_release())

        self.assertTrue(launch_kwargs["headless"])
        self.assertIn("--no-proxy-server", launch_kwargs["args"])
        self.assertNotIn("HeadlessChrome", launch_kwargs["user_agent"])


async def _noop_sleep(_delay):
    return None


async def _noop_async_true(*_args, **_kwargs):
    return True


if __name__ == "__main__":
    unittest.main()
