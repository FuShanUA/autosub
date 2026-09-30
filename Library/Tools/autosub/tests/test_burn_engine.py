import unittest
from unittest.mock import MagicMock, patch

from Library.Tools.hardsubber import burn_engine


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


if __name__ == "__main__":
    unittest.main()
