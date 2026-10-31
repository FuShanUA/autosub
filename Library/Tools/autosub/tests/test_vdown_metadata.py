import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


VDOWN_SCRIPT = (
    Path(__file__).resolve().parents[2] / "vdown" / "download.py"
)


@unittest.skipUnless(VDOWN_SCRIPT.is_file(), "vdown script is unavailable")
class TestVdownMetadata(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        spec = importlib.util.spec_from_file_location(
            "vdown_download_under_test", VDOWN_SCRIPT
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_normalizes_ytdlp_publish_dates(self):
        self.assertEqual(
            self.module.normalize_publish_date("20261029"),
            "2026-10-29",
        )
        self.assertEqual(
            self.module.normalize_publish_date("2026-10-29T08:00:00Z"),
            "2026-10-29",
        )
        self.assertEqual(self.module.normalize_publish_date(None), "")

    def test_writes_sidecar_metadata_from_info_json(self):
        info_path = os.path.join(self.temp_dir, "source video.info.json")
        with open(info_path, "w", encoding="utf-8") as handle:
            json.dump({"upload_date": "20261029"}, handle)

        result = self.module.write_publish_date_metadata(self.temp_dir)

        self.assertEqual(
            result,
            os.path.join(self.temp_dir, "source video.video_metadata.json"),
        )
        with open(result, encoding="utf-8") as handle:
            metadata = json.load(handle)
        self.assertEqual(metadata["publish_date"], "2026-10-29")
        self.assertEqual(
            metadata["source_info_file"],
            "source video.info.json",
        )

    def test_write_metadata_handles_literal_brackets_in_directory(self):
        bracket_dir = os.path.join(self.temp_dir, "project [iGTXWPy3CY4]")
        os.makedirs(bracket_dir, exist_ok=True)
        info_path = os.path.join(bracket_dir, "source video.info.json")
        with open(info_path, "w", encoding="utf-8") as handle:
            json.dump({"upload_date": "20261029"}, handle)

        result = self.module.write_publish_date_metadata(bracket_dir)

        self.assertEqual(
            result,
            os.path.join(bracket_dir, "source video.video_metadata.json"),
        )


if __name__ == "__main__":
    unittest.main()
