import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
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
                self.module._wait_cover_editor_ready(object(), timeout=5)
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
                self.module._wait_cover_editor_ready(object(), timeout=5)
            )

        self.assertTrue(ready)
        self.assertGreaterEqual(calls["count"], 5)

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
        progress_selectors = set(self.module.UPLOAD_PROGRESS_SELECTORS)

        class FakeLocator:
            def __init__(self, text):
                self._text = text
                self.first = self

            async def count(self):
                return 1 if self._text else 0

            async def text_content(self):
                return self._text

            async def is_visible(self):
                return False

            async def get_attribute(self, _name):
                return None

        class FakePage:
            def locator(self, selector):
                return FakeLocator("67%" if selector in progress_selectors else None)

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
        progress_selectors = set(self.module.UPLOAD_PROGRESS_SELECTORS)
        state = {"pct": 10}

        class FakeLocator:
            def __init__(self, text):
                self._text = text
                self.first = self

            async def count(self):
                return 1 if self._text else 0

            async def text_content(self):
                return self._text

        class FakePage:
            def locator(self, selector):
                if selector in progress_selectors:
                    state["pct"] = min(100, state["pct"] + 1)
                    return FakeLocator(f"{state['pct']}%")
                return FakeLocator(None)

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
            [("share", "抽象产品"), ("portrait", "抽象产品")],
        )
        self.assertEqual(set(previews), {"portrait", "share"})

    def test_cover_card_failure_is_retried_before_continuing(self):
        calls = []

        async def fake_upload(page, cover, orientation, title):
            calls.append(orientation)
            if orientation == "share" and len(calls) == 1:
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

        self.assertEqual(calls, ["share", "share", "portrait"])
        self.assertEqual(set(previews), {"portrait", "share"})

    def test_private_draft_cover_readback_is_warning_not_failure(self):
        async def fake_open_draft_list(page):
            return True

        async def fake_draft_entries(page):
            return [{"index": 0, "title": "本次描述", "time": "2026年10月10日 23:00"}]

        async def fake_open_draft_entry(page, index):
            return True

        async def fake_wait(page, timeout=0):
            raise AssertionError("private draft must not wait for a flaky cover editor")

        async def fake_verify_fields(page, title, desc):
            return title == "FDE的核心职能：抽象产品" and desc == "本次描述"

        async def fake_verify_covers(page, previews):
            return False

        with patch.object(self.module, "_open_draft_list", fake_open_draft_list), \
             patch.object(self.module, "_draft_list_entries", fake_draft_entries), \
             patch.object(self.module, "_open_draft_entry", fake_open_draft_entry), \
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
                    strict_covers=False,
                )
            )

        self.assertTrue(ok)

    def test_private_upload_can_save_draft_when_covers_are_unconfirmed(self):
        async def fake_same_page(*args, **kwargs):
            return False

        with patch.object(self.module, "_set_wechat_covers_same_page", fake_same_page):
            previews = asyncio.run(
                self.module._set_wechat_covers_for_upload(
                    object(),
                    "portrait.jpg",
                    "share.jpg",
                    "抽象产品",
                    require_both=False,
                )
            )

        self.assertEqual(previews, {})

    def test_verify_draft_saved_checks_form_title_and_both_covers(self):
        opened = []

        async def fake_open_draft_list(page):
            return True

        async def fake_draft_entries(page):
            return [
                {"index": 0, "title": "旧描述", "time": "2026年09月18日 12:52"},
                {"index": 1, "title": "本次描述", "time": "2026年09月18日 15:13"},
            ]

        async def fake_open_draft_entry(page, index):
            opened.append(index)
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
             patch.object(self.module, "_open_draft_entry", fake_open_draft_entry), \
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

        async def fake_open_draft_entry(page, index):
            return True

        async def fake_wait(page, timeout=0):
            raise AssertionError("cover editor must not block a coverless draft check")

        async def fake_verify_fields(page, title, desc):
            return title == "FDE的核心职能：抽象产品" and desc == "本次描述"

        with patch.object(self.module, "_open_draft_list", fake_open_draft_list), \
             patch.object(self.module, "_draft_list_entries", fake_draft_entries), \
             patch.object(self.module, "_open_draft_entry", fake_open_draft_entry), \
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
            with self.assertRaises(RuntimeError):
                asyncio.run(self.module._click_final_action(object(), False))

        self.assertEqual(attempted, [self.module.SAVE_DRAFT_SELECTORS])

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

        with patch.object(self.module, "BROWSER_DATA_DIR", self.temp_dir):
            _context, _page = asyncio.run(
                self.module._ensure_browser(FakePlaywright(), headless=True)
            )

        self.assertTrue(launch_kwargs["headless"])
        self.assertIn("--no-proxy-server", launch_kwargs["args"])
        self.assertNotIn("HeadlessChrome", launch_kwargs["user_agent"])


async def _noop_sleep(_delay):
    return None


if __name__ == "__main__":
    unittest.main()
