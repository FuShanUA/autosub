import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(CURRENT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from subtitle_audit import (
    audit_bilingual_srt,
    apply_findings,
    local_risk_reasons,
    parse_audit_response,
    split_bilingual_lines,
    write_srt,
)
import srt_utils


class TestSubtitleAudit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_split_bilingual_lines_preserves_mixed_proper_nouns(self):
        cn, en = split_bilingual_lines(
            ["OpenAI研究员讨论递归自我改进", "OpenAI researchers discuss recursive self-improvement"]
        )
        self.assertEqual(cn, "OpenAI研究员讨论递归自我改进")
        self.assertEqual(en, "OpenAI researchers discuss recursive self-improvement")

    def test_local_risk_reasons_detect_missing_translation_and_negation(self):
        self.assertIn(
            "missing Chinese translation",
            local_risk_reasons("", "This is not a trial"),
        )
        self.assertIn(
            "English negation is absent in Chinese",
            local_risk_reasons("这是一次试用", "This was not a trial"),
        )

    def test_parse_audit_response_rejects_invalid_ids_and_severities(self):
        raw = """
        {"issues":[
          {"block":"1","type":"mistranslation","severity":"high","suggested":"这是一次试用"},
          {"block":"999","type":"mistranslation","severity":"high","suggested":"无效"},
          {"block":"2","type":"mistranslation","severity":"low","suggested":"低置信"},
          {"block":"3","type":"alignment","severity":"high"}
        ]}
        """
        findings = parse_audit_response(raw, {"1", "2", "3"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["block"], "1")

    def test_apply_findings_replaces_only_chinese_line(self):
        blocks = [
            {
                "index": "1",
                "time": "00:00:00,000 --> 00:00:02,000",
                "lines": ["试任", "trial again"],
            }
        ]
        findings = [
            {
                "block": "1",
                "type": "mistranslation",
                "severity": "high",
                "suggested": "再试一次",
            }
        ]
        self.assertEqual(apply_findings(blocks, findings), 1)
        self.assertEqual(blocks[0]["lines"], ["再试一次", "trial again"])

    def test_write_srt_round_trips_bilingual_blocks(self):
        path = os.path.join(self.temp_dir, "test.bi.srt")
        blocks = [
            {
                "index": "1",
                "time": "00:00:00,000 --> 00:00:02,000",
                "lines": ["这是一次试用", "This was a trial"],
            }
        ]
        write_srt(blocks, path)
        parsed = srt_utils.parse_srt(path)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["lines"], ["这是一次试用", "This was a trial"])

    def test_audit_bilingual_srt_retries_failed_batches(self):
        path = os.path.join(self.temp_dir, "test.bi.srt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:02,000\n"
                "这是一次试用\nThis was a trial\n"
            )
        client = MagicMock()
        client.generate_batch.side_effect = [
            [{"chunk_idx": 0, "batch": [], "result": None, "error": "timeout"}],
            [{"chunk_idx": 0, "batch": [], "result": '{"issues":[]}'}],
        ]

        findings, blocks, failed_batches = audit_bilingual_srt(
            path, client, "test-model", batch_size=30
        )

        self.assertEqual(findings, [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(failed_batches, [])
        self.assertEqual(client.generate_batch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
