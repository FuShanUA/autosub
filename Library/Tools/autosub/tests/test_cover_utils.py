import os
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
    _COVER_STYLE_VERSION,
    cover_output_path,
    generate_cover,
    generate_platform_covers,
    _cover_cache_marker,
    _centering_for_face,
    _draw_title,
    _find_latin_font,
    _fit_font,
    _frame_face_score,
    _largest_face_bbox,
    _load_title_fonts,
    _legacy_cover_cache_marker,
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

    def test_more_faces_win_even_when_each_face_is_smaller(self):
        one_large_face = [(760, 240, 400, 400)]
        two_smaller_faces = [(560, 420, 180, 180), (1180, 420, 180, 180)]

        with patch.object(cover_utils, "_face_bboxes", return_value=one_large_face):
            one_face_score = _frame_face_score(self.source)
        with patch.object(
            cover_utils, "_face_bboxes", return_value=two_smaller_faces
        ):
            two_face_score = _frame_face_score(self.source)

        self.assertGreater(two_face_score, one_face_score)

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

    def test_title_card_uses_lower_center_and_two_thirds_width(self):
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
        self.assertLessEqual(card_width, 0.72)
        self.assertAlmostEqual((left + right) / 2 / image.width, 0.5, delta=0.01)
        self.assertGreaterEqual(card_center_y, 0.58)
        self.assertLessEqual(card_center_y, 0.68)

    def test_cover_font_starts_larger(self):
        fonts, lines = _fit_font("抽象产品", 1920, 94)
        self.assertEqual(fonts[0].size, 94)
        self.assertEqual(lines, ["抽象产品"])

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
