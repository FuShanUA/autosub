import unittest
import tempfile
import os
from unittest.mock import MagicMock, patch

from Library.Tools.hardsubber import burn_engine
from Library.Tools.hardsubber import srt_to_ass


class BurnEncoderTests(unittest.TestCase):
    def test_videotoolbox_does_not_pin_h264_level(self):
        """Non-standard widths need the encoder to select its own H.264 level."""
        result = MagicMock(returncode=0)
        with patch.object(burn_engine.subprocess, "run", return_value=result):
            encoder, options = burn_engine.get_optimized_encoder(
                burn_engine.FFMPEG_PATH,
                quality="high",
                original_bitrate=2_278_000,
                source_codec="h264",
            )

        self.assertEqual(encoder, "h264_videotoolbox")
        self.assertNotIn("-level", options)


class SrtToAssTests(unittest.TestCase):
    def test_bilingual_ass_accepts_non_chinese_first_block(self):
        """A Korean-only first block must render in bilingual ASS output."""
        parsed = [
            {
                "s": "0:00:00.00",
                "e": "0:00:02.00",
                "cn": "",
                "en": "한국어 자막",
            }
        ]
        config = srt_to_ass.TkinterConfig()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "output.ass")
            srt_to_ass.generate_ass(parsed, output_path, config)
            with open(output_path, encoding="utf-8") as handle:
                content = handle.read()

        self.assertIn("한국어 자막", content)


if __name__ == "__main__":
    unittest.main()
