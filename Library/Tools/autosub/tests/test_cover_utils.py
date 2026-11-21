import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
COMMON_DIR = os.path.join(REPO_ROOT, "Library", "Tools", "common")
if os.path.isdir(COMMON_DIR):
    import sys
    if COMMON_DIR not in sys.path:
        sys.path.insert(0, COMMON_DIR)

import cover_utils
from PIL import Image, ImageFont

from cover_utils import (
    _BILI_BADGE_WIDTH_RATIO,
    _DENSE_SAMPLE_FRAME_COUNT,
    _COVER_STYLE_VERSION,
    _COVER_DATE_BACKGROUND,
    _COVER_DATE_TEXT,
    _COMPOSITION_CONVERSATION,
    _COMPOSITION_NONE,
    _COMPOSITION_PANEL,
    _COMPOSITION_SINGLE,
    _frame_brightness,
    _frame_is_usable,
    _frame_face_rank,
    _sample_video_frames,
    cover_output_path,
    generate_cover,
    generate_platform_covers,
    _cover_cache_marker,
    _centering_for_face,
    _centering_for_faces,
    _composition_metrics,
    _draw_title,
    _find_latin_font,
    _fit_font,
    _frame_face_score,
    _largest_face_bbox,
    _read_publish_date,
    _publish_date_badge_text,
    _load_title_fonts,
    _legacy_cover_cache_marker,
    _title_layout,
    cover_style_matches,
    read_cover_metadata,
    _wrap_title,
)


class TestCoverUtils(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source = os.path.join(self.temp_dir, "source.jpg")
        Image.new("RGB", (1920, 1080), (42, 82, 126)).save(self.source, "JPEG")

    def test_face_frame_scores_above_frame_without_faces(self):
        with patch.object(cover_utils, "_face_bboxes", return_value=[]):
            no_face_score = _frame_face_score(self.source)
        with patch.object(
            cover_utils,
            "_face_bboxes",
            return_value=[(860, 390, 200, 200)],
        ):
            face_score = _frame_face_score(self.source)

        self.assertLess(no_face_score, 2.0)
        self.assertGreater(face_score, 30.0)
        self.assertLess(no_face_score, face_score)

    def test_frame_usability_rejects_dark_frames(self):
        dark = os.path.join(self.temp_dir, "dark.jpg")
        bright = os.path.join(self.temp_dir, "bright.jpg")
        Image.new("RGB", (2000, 2000), (0, 0, 0)).save(dark, "JPEG")
        Image.new("RGB", (1000, 1000), (180, 190, 200)).save(bright, "JPEG")

        self.assertFalse(_frame_is_usable(dark))
        self.assertTrue(_frame_is_usable(bright))

    def test_sampling_uses_bounded_timestamp_seeks(self):
        with patch.object(cover_utils, "subprocess") as subprocess_mock:
            subprocess_mock.run.return_value = type(
                "CompletedProcess", (), {"returncode": 0}
            )()
            frames = _sample_video_frames(
                "/tmp/video.mp4",
                self.temp_dir,
                "ffmpeg",
                3600.0,
            )

        self.assertEqual(frames, [])
        self.assertEqual(subprocess_mock.run.call_count, 16)
        for call in subprocess_mock.run.call_args_list:
            args = call.args[0]
            self.assertIn("-ss", args)
            self.assertNotIn("-i", args[: args.index("-ss")])
            self.assertNotIn("fps=", args)

    def test_extract_best_frame_fails_when_all_candidates_are_black(self):
        black = os.path.join(self.temp_dir, "black.jpg")
        Image.new("RGB", (2000, 2000), (0, 0, 0)).save(black, "JPEG")
        output = os.path.join(self.temp_dir, "best.jpg")

        with patch.object(cover_utils, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
            cover_utils, "_video_duration_seconds", return_value=100.0
        ), patch.object(
            cover_utils, "_sample_video_frames", return_value=[(10.0, black)]
        ):
            with self.assertRaisesRegex(RuntimeError, "bright enough"):
                cover_utils.extract_best_frame("/tmp/video.mp4", output)

    def test_extract_best_frame_uses_dim_candidate_when_no_bright_frames(self):
        dim = os.path.join(self.temp_dir, "dim.jpg")
        Image.new("RGB", (2000, 2000), (20, 20, 20)).save(dim, "JPEG")
        output = os.path.join(self.temp_dir, "best.jpg")
        completed = type("CompletedProcess", (), {"returncode": 0})()

        def fake_run(args, **kwargs):
            shutil.copyfile(dim, args[9])
            return completed

        with patch.object(cover_utils, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
            cover_utils, "_video_duration_seconds", return_value=100.0
        ), patch.object(
            cover_utils, "_sample_video_frames", return_value=[(10.0, dim)]
        ), patch.object(cover_utils, "subprocess") as subprocess_mock:
            subprocess_mock.run.side_effect = fake_run
            result = cover_utils.extract_best_frame("/tmp/video.mp4", output)

        self.assertEqual(result, output)
        self.assertGreaterEqual(_frame_brightness(output), 8.0)
        self.assertLess(_frame_brightness(output), 40.0)

    def test_extract_best_frame_prefers_conversation_composition_over_raw_score(self):
        no_face = os.path.join(self.temp_dir, "no-face.jpg")
        one_face = os.path.join(self.temp_dir, "one-face.jpg")
        two_faces = os.path.join(self.temp_dir, "two-faces.jpg")
        for path in (no_face, one_face, two_faces):
            Image.new("RGB", (1920, 1080), (42, 82, 126)).save(path, "JPEG")
        output = os.path.join(self.temp_dir, "best.jpg")
        completed = type("CompletedProcess", (), {"returncode": 0})()
        ranks = {
            no_face: (_COMPOSITION_NONE, 100.0, 0, 0),
            one_face: (_COMPOSITION_SINGLE, 80.0, 1, 1),
            two_faces: (_COMPOSITION_CONVERSATION, 80.0, 2, 2),
        }

        def fake_run(args, **kwargs):
            source = two_faces if "validated-1" in args[9] else one_face
            shutil.copyfile(source, args[9])
            return completed

        with patch.object(cover_utils, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
            cover_utils, "_video_duration_seconds", return_value=100.0
        ), patch.object(
            cover_utils,
            "_sample_video_frames",
            return_value=[(10.0, no_face), (20.0, one_face), (30.0, two_faces)],
        ), patch.object(
            cover_utils,
            "_frame_face_rank",
            side_effect=lambda path: ranks.get(
                path,
                ranks[two_faces if "validated-1" in path else one_face],
            ),
        ) as rank_mock, patch.object(cover_utils, "subprocess") as subprocess_mock:
            subprocess_mock.run.side_effect = fake_run
            result = cover_utils.extract_best_frame("/tmp/video.mp4", output)
            self.assertEqual(rank_mock.call_count, 4)

        self.assertEqual(result, output)

    def test_extract_best_frame_dense_samples_when_first_pass_has_no_faces(self):
        no_face = os.path.join(self.temp_dir, "sparse-no-face.jpg")
        one_face = os.path.join(self.temp_dir, "dense-one-face.jpg")
        Image.new("RGB", (1920, 1080), (42, 82, 126)).save(no_face, "JPEG")
        Image.new("RGB", (1920, 1080), (72, 112, 156)).save(one_face, "JPEG")
        output = os.path.join(self.temp_dir, "best.jpg")
        completed = type("CompletedProcess", (), {"returncode": 0})()
        ranks = {
            no_face: (_COMPOSITION_NONE, 100.0, 0, 0),
            one_face: (_COMPOSITION_SINGLE, 80.0, 1, 1),
        }
        sample_calls = []

        def fake_sample(video, temp_dir, ffmpeg, duration, frame_count=None):
            sample_calls.append(frame_count)
            if frame_count == _DENSE_SAMPLE_FRAME_COUNT:
                return [(25.0, one_face)]
            return [(10.0, no_face)]

        def fake_run(args, **kwargs):
            source = one_face if "validated-1" in args[9] else no_face
            shutil.copyfile(source, args[9])
            return completed

        with patch.object(cover_utils, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
            cover_utils, "_video_duration_seconds", return_value=100.0
        ), patch.object(
            cover_utils, "_sample_video_frames", side_effect=fake_sample
        ), patch.object(
            cover_utils,
            "_frame_face_rank",
            side_effect=lambda path: ranks.get(
                path,
                ranks[one_face if "validated-1" in path else no_face],
            ),
        ), patch.object(cover_utils, "subprocess") as subprocess_mock:
            subprocess_mock.run.side_effect = fake_run
            result = cover_utils.extract_best_frame("/tmp/video.mp4", output)

        self.assertEqual(result, output)
        self.assertEqual(sample_calls, [None, _DENSE_SAMPLE_FRAME_COUNT])

    def test_extract_best_frame_dense_samples_for_multi_face_after_one_face(self):
        one_face = os.path.join(self.temp_dir, "sparse-one-face.jpg")
        two_faces = os.path.join(self.temp_dir, "dense-two-faces.jpg")
        Image.new("RGB", (1920, 1080), (42, 82, 126)).save(one_face, "JPEG")
        Image.new("RGB", (1920, 1080), (72, 112, 156)).save(two_faces, "JPEG")
        output = os.path.join(self.temp_dir, "best.jpg")
        completed = type("CompletedProcess", (), {"returncode": 0})()
        ranks = {
            one_face: (_COMPOSITION_SINGLE, 100.0, 1, 1),
            two_faces: (_COMPOSITION_CONVERSATION, 80.0, 2, 2),
        }
        sample_calls = []

        def fake_sample(video, temp_dir, ffmpeg, duration, frame_count=None):
            sample_calls.append(frame_count)
            if frame_count == _DENSE_SAMPLE_FRAME_COUNT:
                return [(25.0, two_faces)]
            return [(10.0, one_face)]

        def fake_run(args, **kwargs):
            source = two_faces if "validated-1" in args[9] else one_face
            shutil.copyfile(source, args[9])
            return completed

        with patch.object(cover_utils, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
            cover_utils, "_video_duration_seconds", return_value=100.0
        ), patch.object(
            cover_utils, "_sample_video_frames", side_effect=fake_sample
        ), patch.object(
            cover_utils,
            "_frame_face_rank",
            side_effect=lambda path: ranks.get(
                path,
                ranks[two_faces if "validated-1" in path else one_face],
            ),
        ), patch.object(cover_utils, "subprocess") as subprocess_mock:
            subprocess_mock.run.side_effect = fake_run
            result = cover_utils.extract_best_frame("/tmp/video.mp4", output)

        self.assertEqual(result, output)
        self.assertEqual(sample_calls, [None, _DENSE_SAMPLE_FRAME_COUNT])

    def test_generate_cover_rejects_black_explicit_source(self):
        dark = os.path.join(self.temp_dir, "dark-source.jpg")
        Image.new("RGB", (2000, 2000), (0, 0, 0)).save(dark, "JPEG")
        output = os.path.join(self.temp_dir, "cover.jpg")

        with self.assertRaisesRegex(RuntimeError, "too dark"):
            generate_cover(
                "/tmp/does-not-exist.mp4",
                output,
                "Palantir FDE",
                source_path=dark,
            )

    def test_generate_cover_accepts_dim_explicit_source(self):
        dim = os.path.join(self.temp_dir, "dim-source.jpg")
        Image.new("RGB", (2000, 2000), (20, 20, 20)).save(dim, "JPEG")
        output = os.path.join(self.temp_dir, "cover.jpg")

        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Palantir数据层",
            source_path=dim,
        )

        self.assertEqual(result, output)
        self.assertGreaterEqual(_frame_brightness(output), 8.0)

    def test_conversation_faces_beat_one_large_face(self):
        one_large_face = [(760, 240, 400, 400)]
        two_smaller_faces = [(560, 420, 180, 180), (1180, 420, 180, 180)]

        with patch.object(cover_utils, "_face_bboxes", return_value=one_large_face):
            one_face_score = _frame_face_score(self.source)
        with patch.object(
            cover_utils, "_face_bboxes", return_value=two_smaller_faces
        ):
            two_face_score = _frame_face_score(self.source)

        self.assertGreater(two_face_score, one_face_score)

    def test_audience_crowd_loses_to_conversation_frame(self):
        conversation_faces = [(560, 420, 180, 180), (1180, 420, 180, 180)]
        audience_faces = [
            (80 + 100 * (index % 10), 120 + 90 * (index // 10), 40, 40)
            for index in range(30)
        ]

        with patch.object(cover_utils, "_face_bboxes", return_value=conversation_faces):
            conversation_score = _frame_face_score(self.source)
        with patch.object(cover_utils, "_face_bboxes", return_value=audience_faces):
            audience_score = _frame_face_score(self.source)

        self.assertGreater(conversation_score, audience_score)
        self.assertLess(audience_score, 0.0)

    def test_panel_group_beats_single_subject(self):
        single_face = [(760, 240, 400, 400)]
        panel_faces = [
            (180, 420, 120, 120),
            (580, 420, 120, 120),
            (980, 420, 120, 120),
            (1380, 420, 120, 120),
        ]

        with patch.object(cover_utils, "_face_bboxes", return_value=single_face):
            single_score = _frame_face_score(self.source)
        with patch.object(cover_utils, "_face_bboxes", return_value=panel_faces):
            panel_score = _frame_face_score(self.source)

        self.assertGreater(panel_score, single_score)

    def test_composition_metrics_classifies_expected_frames(self):
        conversation_faces = [(560, 420, 180, 180), (1180, 420, 180, 180)]
        panel_faces = [
            (180, 420, 120, 120),
            (580, 420, 120, 120),
            (980, 420, 120, 120),
            (1380, 420, 120, 120),
        ]
        audience_faces = [
            (80 + 100 * (index % 10), 120 + 90 * (index // 10), 40, 40)
            for index in range(30)
        ]

        conversation = _composition_metrics(1920, 1080, conversation_faces)
        panel = _composition_metrics(1920, 1080, panel_faces)
        audience = _composition_metrics(1920, 1080, audience_faces)

        self.assertEqual(conversation[0], _COMPOSITION_CONVERSATION)
        self.assertEqual(conversation[2], 0)
        self.assertEqual(panel[0], _COMPOSITION_PANEL)
        self.assertEqual(panel[2], 0)
        self.assertEqual(audience[0], _COMPOSITION_NONE)
        self.assertEqual(audience[2], 30)

    def test_largest_face_bbox_keeps_dominant_face_for_cropping(self):
        faces = [(100, 100, 120, 120), (650, 300, 260, 260)]
        with patch.object(cover_utils, "_face_bboxes", return_value=faces):
            self.assertEqual(_largest_face_bbox(self.source), (650, 300, 260, 260))

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_generates_landscape_cover(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Alex Karp explains FDE",
            orientation="landscape",
            source_path=self.source,
        )
        self.assertEqual(result, output)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1146, 717))
            self.assertEqual(image.format, "JPEG")
            self._assert_frame_overlay_style(image)

    def test_generates_portrait_cover(self):
        output = os.path.join(self.temp_dir, "video.cover.wechat.jpg")
        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Palantir FDE",
            orientation="portrait",
            source_path=self.source,
        )
        self.assertEqual(result, output)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1080, 1440))
            self._assert_frame_overlay_style(image)

    def test_generates_wechat_share_cover(self):
        output = os.path.join(self.temp_dir, "video.cover.wechat.share.jpg")
        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Palantir FDE",
            orientation="share",
            source_path=self.source,
        )
        self.assertEqual(result, output)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1080, 810))
            self._assert_frame_overlay_style(image)

    def test_generates_wide_cover(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "抽象产品",
            orientation="wide",
            source_path=self.source,
        )
        self.assertEqual(result, output)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1920, 1080))
            self._assert_frame_overlay_style(image)

    def test_generates_bili_wide_and_4x3_from_one_source(self):
        video = os.path.join(self.temp_dir, "video.mp4")
        wide, four_by_three = generate_platform_covers(
            video,
            "bilibili",
            "抽象产品",
            source_path=self.source,
        )
        self.assertTrue(wide.endswith(".cover.bili.jpg"))
        self.assertTrue(four_by_three.endswith(".cover.bili.4x3.jpg"))
        with Image.open(wide) as image:
            self.assertEqual(image.size, (1920, 1080))
        with Image.open(four_by_three) as image:
            self.assertEqual(image.size, (1080, 810))

    def test_reuses_existing_cover(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "First title",
            orientation="landscape",
            source_path=self.source,
        )
        os.utime(output, (1000000000, 1000000000))
        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "First title",
            orientation="landscape",
            source_path=self.source,
        )
        self.assertEqual(result, output)
        self.assertEqual(os.stat(output).st_mtime_ns, 1000000000000000000)

    def test_embeds_readable_title_metadata(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Palantir CEO卡普：FDE抽象产品",
            orientation="wide",
            source_path=self.source,
        )
        metadata = read_cover_metadata(output)
        self.assertEqual(metadata, {
            "app": "autosub",
            "type": "cover",
            "style_version": _COVER_STYLE_VERSION,
            "orientation": "wide",
            "title": "Palantir CEO卡普：FDE抽象产品",
            "title_sha256": metadata["title_sha256"],
        })
        self.assertRegex(metadata["title_sha256"], r"^[0-9a-f]{64}$")

    def test_publish_date_is_read_from_info_json_and_drawn_top_right(self):
        video = os.path.join(self.temp_dir, "source video.mp4")
        info_path = os.path.join(self.temp_dir, "source video.info.json")
        output = os.path.join(self.temp_dir, "source video.cover.wechat.jpg")
        with open(info_path, "w", encoding="utf-8") as handle:
            json.dump({"upload_date": "20261029"}, handle)

        result = generate_cover(
            video,
            output,
            "Palantir FDE",
            orientation="portrait",
            source_path=self.source,
        )

        self.assertEqual(result, output)
        metadata = read_cover_metadata(output)
        self.assertEqual(metadata["publish_date"], "2026-10-29")
        self.assertEqual(
            _publish_date_badge_text(metadata["publish_date"]),
            "原视频发布时间：2026年-10月-29日",
        )
        with Image.open(output) as image:
            top_right = image.crop((image.width - 900, 0, image.width, 180))
            colors = set(top_right.getdata())
            yellow_pixels = [
                (x, y)
                for y in range(200)
                for x in range(image.width - 900, image.width)
                if abs(image.getpixel((x, y))[0] - _COVER_DATE_BACKGROUND[0]) < 20
                and abs(image.getpixel((x, y))[1] - _COVER_DATE_BACKGROUND[1]) < 20
                and abs(image.getpixel((x, y))[2] - _COVER_DATE_BACKGROUND[2]) < 20
            ]
            yellow_bbox = (
                min(x for x, _ in yellow_pixels),
                min(y for _, y in yellow_pixels),
                max(x for x, _ in yellow_pixels),
                max(y for _, y in yellow_pixels),
            )
            self.assertTrue(
                any(
                    abs(red - _COVER_DATE_BACKGROUND[0]) < 20
                    and abs(green - _COVER_DATE_BACKGROUND[1]) < 20
                    and abs(blue - _COVER_DATE_BACKGROUND[2]) < 20
                    for red, green, blue in colors
                )
            )
            self.assertGreaterEqual(yellow_bbox[1], 20)
            self.assertLess(yellow_bbox[3], 180)
            self.assertGreater(yellow_bbox[0], 0)
            self.assertLess(yellow_bbox[2], image.width)
            title_red_rows = [
                y
                for y in range(700, image.height)
                for x in range(0, image.width, 4)
                if image.getpixel((x, y))[0] > 180
                and image.getpixel((x, y))[1] < 50
                and image.getpixel((x, y))[2] < 50
            ]
            self.assertLess(yellow_bbox[3], min(title_red_rows))
            self.assertTrue(
                any(
                    red < 50 and green < 50 and blue < 50
                    for red, green, blue in colors
                )
            )

    def test_cover_regenerates_when_publish_date_changes(self):
        output = os.path.join(self.temp_dir, "video.cover.wechat.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Same title",
            orientation="portrait",
            source_path=self.source,
            publish_date="2026-10-28",
        )
        os.utime(output, (1000000000, 1000000000))

        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Same title",
            orientation="portrait",
            source_path=self.source,
            publish_date="2026-10-29",
        )

        self.assertEqual(result, output)
        self.assertGreater(os.stat(output).st_mtime_ns, 1000000000000000000)
        self.assertEqual(
            read_cover_metadata(output)["publish_date"],
            "2026-10-29",
        )

    def test_uploader_cache_check_accepts_date_badge_without_explicit_date(self):
        output = os.path.join(self.temp_dir, "video.cover.wechat.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Same title",
            orientation="portrait",
            source_path=self.source,
            publish_date="2026-10-29",
        )

        self.assertTrue(cover_style_matches(output, "Same title", "portrait"))

    def test_read_publish_date_fallback_handles_literal_brackets(self):
        bracket_dir = os.path.join(self.temp_dir, "project [iGTXWPy3CY4]")
        os.makedirs(bracket_dir, exist_ok=True)
        info_path = os.path.join(bracket_dir, "source video.info.json")
        with open(info_path, "w", encoding="utf-8") as handle:
            json.dump({"upload_date": "20261029"}, handle)

        value = _read_publish_date(
            os.path.join(bracket_dir, "different video_hardsub.mp4")
        )

        self.assertEqual(value, "2026-10-29")

    def test_read_publish_date_falls_back_to_dated_filename(self):
        value = _read_publish_date(
            os.path.join(self.temp_dir, "2026-10-29 interview static.mp4")
        )

        self.assertEqual(value, "2026-10-29")

    def test_accepts_legacy_hash_only_cover_marker(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        Image.new("RGB", (1920, 1080), (42, 82, 126)).save(
            output,
            "JPEG",
            comment=_legacy_cover_cache_marker("Legacy title", "wide"),
        )
        metadata = read_cover_metadata(output)
        self.assertIsNone(metadata["title"])
        self.assertTrue(metadata["legacy_marker"])
        self.assertTrue(generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Legacy title",
            orientation="wide",
            source_path=self.source,
        ))

    def test_regenerates_cover_when_title_changes(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "First title",
            orientation="landscape",
            source_path=self.source,
        )
        os.utime(output, (1000000000, 1000000000))

        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Second title",
            orientation="landscape",
            source_path=self.source,
        )

        self.assertEqual(result, output)
        self.assertGreater(os.stat(output).st_mtime_ns, 1000000000000000000)
        with Image.open(output) as image:
            comment = image.info.get("comment", b"")
            if isinstance(comment, str):
                comment = comment.encode("utf-8")
            self.assertEqual(comment, _cover_cache_marker("Second title", "landscape"))

    def test_regenerates_cover_when_only_cover_title_changes(self):
        output = os.path.join(self.temp_dir, "video.cover.wechat.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "FDE的核心职能：抽象产品",
            orientation="portrait",
            source_path=self.source,
        )
        os.utime(output, (1000000000, 1000000000))

        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "抽象产品",
            orientation="portrait",
            source_path=self.source,
        )

        self.assertEqual(result, output)
        self.assertGreater(os.stat(output).st_mtime_ns, 1000000000000000000)
        with Image.open(output) as image:
            comment = image.info.get("comment", b"")
            if isinstance(comment, str):
                comment = comment.encode("utf-8")
            self.assertEqual(comment, _cover_cache_marker("抽象产品", "portrait"))

    def test_regenerates_cover_when_orientation_changes(self):
        output = os.path.join(self.temp_dir, "video.cover.wechat.jpg")
        generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Same title",
            orientation="landscape",
            source_path=self.source,
        )
        os.utime(output, (1000000000, 1000000000))

        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Same title",
            orientation="portrait",
            source_path=self.source,
        )

        self.assertEqual(result, output)
        self.assertGreater(os.stat(output).st_mtime_ns, 1000000000000000000)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1080, 1440))
            comment = image.info.get("comment", b"")
            if isinstance(comment, str):
                comment = comment.encode("utf-8")
            self.assertEqual(comment, _cover_cache_marker("Same title", "portrait"))

    def test_regenerates_cover_without_current_style_marker(self):
        output = os.path.join(self.temp_dir, "video.cover.bili.jpg")
        Image.new("RGB", (1146, 717), (222, 24, 38)).save(output, "JPEG")
        os.utime(output, (1000000000, 1000000000))

        result = generate_cover(
            "/tmp/does-not-exist.mp4",
            output,
            "Alex Karp explains FDE",
            orientation="landscape",
            source_path=self.source,
        )

        self.assertEqual(result, output)
        self.assertGreater(os.stat(output).st_mtime_ns, 1000000000000000000)
        with Image.open(output) as image:
            comment = image.info.get("comment", b"")
            if isinstance(comment, str):
                comment = comment.encode("utf-8")
            self.assertEqual(
                comment,
                _cover_cache_marker("Alex Karp explains FDE", "landscape"),
            )

    def test_platform_output_paths(self):
        video = "/tmp/example_hardsub.mp4"
        self.assertEqual(
            cover_output_path(video, "bilibili"),
            "/tmp/example_hardsub.cover.bili.jpg",
        )
        self.assertEqual(
            cover_output_path(video, "wechat"),
            "/tmp/example_hardsub.cover.wechat.jpg",
        )
        self.assertEqual(
            cover_output_path(video, "bilibili", "share"),
            "/tmp/example_hardsub.cover.bili.4x3.jpg",
        )
        self.assertEqual(
            cover_output_path(video, "bilibili", "wide"),
            "/tmp/example_hardsub.cover.bili.jpg",
        )

    def test_wraps_two_lines_without_short_orphan(self):
        fonts = _load_title_fonts(64)
        lines = _wrap_title("本体技术重塑军事威慑力", fonts, 420)
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(abs(len(lines[0]) - len(lines[1])), 1)
        self.assertGreaterEqual(len(lines[1]), 3)

    def test_uses_alliance_font_and_balances_mixed_script_title(self):
        path, index = _find_latin_font()
        font = ImageFont.truetype(path, size=16, index=index)
        self.assertEqual(font.getname()[0], "Alliance No.2")

        fonts = _load_title_fonts(64)
        lines = _wrap_title(
            "Palantir CEO卡普揭秘FDE核心优势",
            fonts,
            820,
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines, ["Palantir CEO卡普", "揭秘FDE核心优势"])

    def test_portrait_crop_keeps_left_aligned_subject(self):
        centering = _centering_for_face(
            (1920, 1080),
            (1080, 1440),
            (204, 134, 301, 301),
        )
        self.assertEqual(centering, (0.0, 0.5))

    def test_portrait_crop_keeps_more_visible_faces(self):
        faces = [(240, 200, 300, 300), (720, 220, 180, 180)]
        centering = _centering_for_faces(
            (1920, 1080),
            (1080, 1440),
            faces,
        )
        crop_width = round(1080 * 1080 / 1440)
        left = round(centering[0] * (1920 - crop_width))
        visible_faces = 0
        for x, _, width, _ in faces:
            visible_width = max(0, min(x + width, left + crop_width) - max(x, left))
            if visible_width == width:
                visible_faces += 1
        self.assertGreaterEqual(visible_faces, 1)
        self.assertGreater(centering[0], 0.0)

    def test_portrait_crop_never_splits_two_distant_faces(self):
        """A 3:4 crop of a wide two-shot must keep one whole face, not a gap."""
        faces = [(200, 300, 200, 200), (1500, 300, 200, 200)]
        centering = _centering_for_faces((1920, 1080), (1080, 1440), faces)
        crop_width = round(1080 * 1080 / 1440)
        left = round(centering[0] * (1920 - crop_width))
        contained = [
            x
            for x, _, width, _ in faces
            if x >= left and x + width <= left + crop_width
        ]
        self.assertEqual(len(contained), 1)

    def test_title_card_moves_below_subject(self):
        image = Image.new("RGB", (1920, 1080), (42, 82, 126))
        face_bbox = (199, 129, 312, 312)
        _draw_title(
            image,
            "Palantir CEO卡普：FDE的真正职责是抽象产品，守护用户核心优势",
            "wide",
            face_bbox=face_bbox,
        )

        rgb = image.convert("RGB")
        red_points = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if self._is_red(rgb.getpixel((x, y)))
        ]
        self.assertTrue(red_points)
        top = min(point[1] for point in red_points)
        self.assertGreaterEqual(top, face_bbox[1] + face_bbox[3] + 10)

    def test_title_card_never_moves_above_low_subject(self):
        image = Image.new("RGB", (1920, 1080), (42, 82, 126))
        face_bbox = (1249, 330, 469, 469)
        _draw_title(
            image,
            "Ali访谈：正在发生的网络攻击，等等等等，我们就会迎来书中描述的",
            "wide",
            face_bbox=face_bbox,
        )

        rgb = image.convert("RGB")
        red_points = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if self._is_red(rgb.getpixel((x, y)))
        ]
        self.assertTrue(red_points)
        top = min(point[1] for point in red_points)
        self.assertGreaterEqual(top, face_bbox[1] + face_bbox[3] + 10)
        self.assertGreaterEqual(top / image.height, 0.55)

    def test_title_card_uses_lower_center_and_wide_sentence_layout(self):
        image = Image.new("RGB", (1920, 1080), (42, 82, 126))
        _draw_title(
            image,
            "Palantir CEO卡普：FDE的真正职责是抽象产品，守护用户核心优势",
            "wide",
        )

        rgb = image.convert("RGB")
        points = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if self._is_red(rgb.getpixel((x, y)))
        ]
        left = min(point[0] for point in points)
        right = max(point[0] for point in points)
        top = min(point[1] for point in points)
        bottom = max(point[1] for point in points)
        card_width = (right - left + 1) / image.width
        card_center_y = (top + bottom) / 2 / image.height
        self.assertGreaterEqual(card_width, 0.58)
        self.assertLessEqual(card_width, 0.82)
        self.assertLessEqual((right + 1) / image.width, 0.82)
        self.assertGreaterEqual(card_center_y, 0.68)
        self.assertLessEqual(card_center_y, 0.90)

    def test_bili_title_card_reserves_bottom_right_badge(self):
        image = Image.new("RGB", (1920, 1080), (42, 82, 126))
        _draw_title(
            image,
            "Databricks CEO：企业AI落后的核心障碍是数据、流程和组织",
            "wide",
        )

        rgb = image.convert("RGB")
        badge = rgb.crop((
            int(image.width * (1 - _BILI_BADGE_WIDTH_RATIO)),
            int(image.height * 0.88),
            image.width,
            image.height,
        ))
        self.assertFalse(any(self._is_red(pixel) for pixel in badge.getdata()))

    def test_cover_font_starts_larger(self):
        fonts, lines = _fit_font("抽象产品", 1920, 94)
        self.assertEqual(fonts[0].size, 94)
        self.assertEqual(lines, ["抽象产品"])

    def test_sentence_title_fits_full_width_in_at_most_two_lines(self):
        title = "Palantir英国FDE的真实情况：一上来就被扔进深水区，还要自己把问题讲清楚"
        fonts, lines = _fit_font(title, 1920, 94)
        self.assertLessEqual(len(lines), 2)
        self.assertGreaterEqual(fonts[0].size, 30)
        for line in lines:
            self.assertLessEqual(
                cover_utils._line_metrics(line, fonts)[0],
                int(1920 * 0.88),
            )

    def test_narrow_covers_wrap_before_accepting_layout(self):
        title = "我们提前两年就在美妆圈播下种子，不想让品牌突然冒出来"
        cases = (
            ("share", 1080, 810, 74, 0.80),
            ("portrait", 1080, 1440, 84, 0.88),
        )
        for orientation, width, height, start_size, width_ratio in cases:
            with self.subTest(orientation=orientation):
                fonts, lines, metrics, _, _, box_height = _title_layout(
                    title,
                    width,
                    height,
                    start_size,
                    None,
                    orientation,
                )
                self.assertEqual(len(lines), 2)
                self.assertGreater(fonts[0].size, 48)
                self.assertLessEqual(box_height, height)
                for line, metric in zip(lines, metrics):
                    self.assertLessEqual(
                        metric[0],
                        int(width * width_ratio),
                        msg=line,
                    )

    def _assert_frame_overlay_style(self, image):
        rgb = image.convert("RGB")
        width, height = image.size
        corners = [
            rgb.getpixel((2, 2)),
            rgb.getpixel((width - 3, 2)),
            rgb.getpixel((2, height - 3)),
            rgb.getpixel((width - 3, height - 3)),
        ]
        for pixel in corners:
            self.assertLess(abs(pixel[0] - 42), 15)
            self.assertLess(abs(pixel[1] - 82), 15)
            self.assertLess(abs(pixel[2] - 126), 15)

        pixels = list(rgb.getdata())
        red = sum(self._is_red(pixel) for pixel in pixels) / len(pixels)
        white = sum(
            pixel[0] > 230 and pixel[1] > 230 and pixel[2] > 230
            for pixel in pixels
        ) / len(pixels)
        yellow = sum(
            pixel[0] > 220 and pixel[1] > 170 and pixel[2] < 90
            for pixel in pixels
        ) / len(pixels)
        self.assertGreater(red, 0.015)
        self.assertGreater(white, 0.003)
        self.assertEqual(yellow, 0.0)
        exact_red = sum(
            all(abs(channel - expected) <= 12 for channel, expected in zip(pixel, (216, 8, 0)))
            for pixel in pixels
        ) / len(pixels)
        self.assertGreater(exact_red, 0.015)

        red_points = [
            (x, y)
            for y in range(height)
            for x in range(width)
            if self._is_red(rgb.getpixel((x, y)))
        ]
        self.assertTrue(red_points)
        x0 = min(point[0] for point in red_points)
        x1 = max(point[0] for point in red_points)
        y0 = min(point[1] for point in red_points)
        y1 = max(point[1] for point in red_points)

        center_y = (y0 + y1) // 2
        box_height = y1 - y0 + 1
        shadow_offset = max(10, int(box_height * 0.12))
        shadow_pixel = rgb.getpixel(
            (min(x1 + shadow_offset - 1, width - 1), center_y)
        )
        self.assertGreater(shadow_pixel[0], 230)
        self.assertGreater(shadow_pixel[1], 230)
        self.assertGreater(shadow_pixel[2], 230)

        # There is no full white border: the left and top edges stay video frame.
        left_pixel = rgb.getpixel((max(0, x0 - 4), center_y))
        top_pixel = rgb.getpixel(((x0 + x1) // 2, max(0, y0 - 4)))
        for pixel in (left_pixel, top_pixel):
            self.assertFalse(pixel[0] > 230 and pixel[1] > 230 and pixel[2] > 230)

    @staticmethod
    def _is_red(pixel):
        return pixel[0] > 150 and pixel[1] < 70 and pixel[2] < 80


if __name__ == "__main__":
    unittest.main()
