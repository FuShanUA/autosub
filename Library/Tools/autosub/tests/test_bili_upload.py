import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


BILI_SCRIPT = os.path.expanduser(
    "~/.codex/skills/bilibili-uploader/scripts/bili_upload.py"
)


@unittest.skipUnless(os.path.isfile(BILI_SCRIPT), "Bilibili uploader is not installed")
class TestBiliUploadCovers(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.video = os.path.join(self.temp_dir, "video_hardsub.mp4")
        self.cookies = os.path.join(self.temp_dir, "cookies.json")
        with open(self.video, "wb") as handle:
            handle.write(b"video")
        with open(self.cookies, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "SESSDATA": "sess",
                    "bili_jct": "jct",
                    "buvid3": "buvid3",
                    "buvid4": "buvid4",
                },
                handle,
            )

        spec = importlib.util.spec_from_file_location(
            "bili_upload_under_test", BILI_SCRIPT
        )
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        sys.modules.pop("bili_upload_under_test", None)

    def test_bili_covers_use_shared_platform_generator(self):
        calls = []
        wide = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        four_by_three = os.path.join(self.temp_dir, "video.cover.bili.4x3.jpg")

        def fake_generate_platform_covers(video, platform, title):
            calls.append((video, platform, title))
            return wide, four_by_three

        with patch.object(
            self.module,
            "generate_platform_covers",
            fake_generate_platform_covers,
        ):
            result_wide, result_four_by_three = self.module._generate_bili_covers(
                self.video,
                "抽象产品",
            )

        self.assertEqual(result_wide, wide)
        self.assertEqual(result_four_by_three, four_by_three)
        self.assertEqual(
            calls,
            [(self.video, "bilibili", "抽象产品")],
        )

    def test_upload_stops_before_uploader_when_covers_are_missing(self):
        class FakeApi:
            def __init__(self, *_args, **_kwargs):
                pass

                async def result():
                    return {}

                self.result = result()

        def fail_extract_best_cover(*_args, **_kwargs):
            raise AssertionError("random screenshot fallback must not run")

        def fail_video_uploader(*_args, **_kwargs):
            raise AssertionError("uploader must not be created without covers")

        with patch.object(self.module, "DEFAULT_COOKIES", self.cookies), \
             patch.object(self.module, "Api", FakeApi), \
             patch.object(self.module, "build_title", lambda *_args, **_kwargs: "抽象产品"), \
             patch.object(self.module, "get_smart_partition", lambda *_args, **_kwargs: (1, "测试")), \
             patch.object(self.module, "build_description", lambda *_args, **_kwargs: "测试简介"), \
             patch.object(self.module, "_generate_tags_via_llm", lambda *_args, **_kwargs: None), \
             patch.object(self.module, "_generate_bili_covers", lambda *_args, **_kwargs: (None, None)), \
             patch.object(self.module, "extract_best_cover", fail_extract_best_cover), \
             patch.object(self.module.video_uploader, "VideoUploader", fail_video_uploader):
            ok = asyncio.run(
                self.module.upload_video(
                    self.video,
                    "Filename-derived title",
                    tags=["人工智能"],
                )
            )

        self.assertFalse(ok)

    def test_form_and_cover_titles_use_separate_generation_styles(self):
        calls = []

        def fake_build(video, **kwargs):
            calls.append(kwargs["title_style"])
            return SimpleNamespace(
                title="Databricks CEO聚焦主要瓶颈",
                evidence=("先找出主要瓶颈",),
                subtitle_path=self.video,
                source="test",
            )

        with patch.object(self.module, "build_subtitle_grounded_title", fake_build):
            form_title = self.module.build_title(
                self.video,
                "Filename-derived title",
            )
            cover_title = self.module.build_cover_title(self.video)

        self.assertEqual(form_title, "Databricks CEO聚焦主要瓶颈")
        self.assertEqual(cover_title, "Databricks CEO聚焦主要瓶颈")
        self.assertEqual(calls, ["plain", "cover_headline"])


if __name__ == "__main__":
    unittest.main()
