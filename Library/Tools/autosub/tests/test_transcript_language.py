import importlib.util
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from Library.Tools.autosub import autosub


def _load_transcribe_engine():
    sys.modules.setdefault("faster_whisper", MagicMock())
    sys.modules.setdefault("mlx_engine", MagicMock())
    path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "transcriber",
            "transcribe_engine.py",
        )
    )
    spec = importlib.util.spec_from_file_location("transcribe_engine_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestTranscriptLanguage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def _write_srt(self, name, text):
        path = os.path.join(self.temp_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n\n")
        return path

    def test_normalizes_chinese_and_fragmented_english(self):
        engine = _load_transcribe_engine()
        normalized = engine.normalize_transcript_text(
            "我覺得這是一個很有趣的點，V in cent 是 V ent us AI 的 Co - F ound er"
        )
        self.assertEqual(
            normalized,
            "我觉得这是一个很有趣的点，Vincent是Ventus AI的Co-Founder",
        )

    def test_normalizes_rolling_ai_and_anthropic_fragments(self):
        engine = _load_transcribe_engine()
        normalized = engine.normalize_transcript_text(
            "R oll ing AI 和 An throp ic 都在讨论 F DE"
        )
        self.assertEqual(normalized, "Rolling AI和Anthropic都在讨论FDE")

    def test_normalizes_common_mixed_language_asr_fragments(self):
        engine = _load_transcribe_engine()
        cases = {
            "必须把FDE放在一个a iaging的框架里面": "必须把FDE放在一个AI Agent的框架里面",
            "a i agent fde是一个新物种": "AI Agent FDE是一个新物种",
            "enterprise a i就属于": "enterprise AI就属于",
            "voicea i或者是AI": "voice AI或者是AI",
            "low - h anging fruit的use case": "low-hanging fruit的use case",
            "agent iccore": "agentic core",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(engine.normalize_transcript_text(source), expected)

    def test_restores_missing_spaces_in_english_transcript(self):
        engine = _load_transcribe_engine()
        cases = {
            "It'snice to be with three of our meritocracy fellows.":
                "It's nice to be with three of our meritocracy fellows.",
            "with palant ir for fiveyears, and then Iguess you guys Two and ahalf months.":
                "with Palantir for five years, and then I guess you guys Two and a half months.",
            "I'm from Westport, Connecticut, graduated staples highschool, and iwould beat the OhioState University.":
                "I'm from Westport, Connecticut, graduated staples high school, and I would be at the Ohio State University.",
            "my name ise it I'm from the BayArea, and iwould betakinga gap year and maybe reapplying to schools.":
                "my name is it I'm from the Bay Area, and I would be taking a gap year and maybe reapplying to schools.",
            "line and is redefining what it means, whata ilooks like and how it integrates and":
                "line and is redefining what it means, what AI looks like and how it integrates and",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    engine.normalize_transcript_text(
                        source,
                        simplify_chinese=False,
                        normalize_latin_runs=False,
                    ),
                    expected,
                )

    def test_chunking_keeps_spaces_before_english_articles_and_pronouns(self):
        engine = _load_transcribe_engine()
        segment = {
            "start": 0.0,
            "end": 2.0,
            "text": "It is a test and I said so",
            "words": [
                {"start": 0.0, "end": 0.2, "word": "It"},
                {"start": 0.2, "end": 0.4, "word": "is"},
                {"start": 0.4, "end": 0.5, "word": "a"},
                {"start": 0.5, "end": 0.7, "word": "test"},
                {"start": 0.7, "end": 0.9, "word": "and"},
                {"start": 0.9, "end": 1.0, "word": "I"},
                {"start": 1.0, "end": 1.2, "word": "said"},
                {"start": 1.2, "end": 1.4, "word": "so"},
            ],
        }

        chunks = list(engine.chunk_segments([segment], "spoken"))

        self.assertEqual(chunks[0].text, "It is a test and I said so")

    def test_removes_inserted_speaker_labels(self):
        engine = _load_transcribe_engine()
        cases = {
            "Richard Averbeck, Jr.: Wait, explain that.": "Wait, explain that.",
            "Peter Robinson AI is around the corner.": "AI is around the corner.",
            "So, how do you feel? Tom Hooper Okay.": "So, how do you feel? Okay.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    engine.normalize_transcript_text(
                        source,
                        simplify_chinese=False,
                        normalize_latin_runs=False,
                    ),
                    expected,
                )

    def test_preserves_real_names_in_english_sentences(self):
        engine = _load_transcribe_engine()
        cases = {
            "Peter Thiel, thank you for joining us.",
            "Peter Thiel is a high profile figure in Silicon Valley.",
            "Walter Isaacson. Welcome to The Builders.",
        }
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(
                    engine.normalize_transcript_text(
                        source,
                        simplify_chinese=False,
                        normalize_latin_runs=False,
                    ),
                    source,
                )

    def test_removes_repeated_pseudo_label_first_name_as_vocative(self):
        engine = _load_transcribe_engine()
        segments = [
            {"text": "Rilke Mc Clendon It's certainly a history."},
            {"text": "Rilke Mc Clendon Well, yeah, the question is."},
            {"text": "But, Rilke, before you go on, you said something."},
            {"text": "Peter Robinson The way AI was classically defined."},
            {"text": "Peter Thiel, thank you for joining us."},
        ]

        engine.normalize_segments_for_output(segments)

        self.assertEqual(segments[0]["text"], "It's certainly a history.")
        self.assertEqual(segments[1]["text"], "Well, yeah, the question is.")
        self.assertEqual(
            segments[2]["text"],
            "But, before you go on, you said something.",
        )
        self.assertEqual(
            segments[3]["text"],
            "The way AI was classically defined.",
        )
        self.assertEqual(
            segments[4]["text"],
            "Peter Thiel, thank you for joining us.",
        )

    def test_chunking_preserves_mixed_language_terms_from_segment_text(self):
        engine = _load_transcribe_engine()
        segment = {
            "start": 0.0,
            "end": 2.0,
            "text": "Jove,欢迎来到catavalue jove,",
            "words": [
                {"start": 0.0, "end": 0.2, "word": "J"},
                {"start": 0.2, "end": 0.4, "word": "ove,"},
                {"start": 0.4, "end": 0.8, "word": "欢"},
                {"start": 0.8, "end": 1.0, "word": "迎"},
                {"start": 1.0, "end": 1.2, "word": "来"},
                {"start": 1.2, "end": 1.4, "word": "到"},
                {"start": 1.4, "end": 1.6, "word": "C"},
                {"start": 1.6, "end": 1.7, "word": "ata"},
                {"start": 1.7, "end": 1.8, "word": "V"},
                {"start": 1.8, "end": 1.9, "word": "al"},
                {"start": 1.9, "end": 1.95, "word": "ue"},
                {"start": 1.95, "end": 2.0, "word": "J"},
                {"start": 2.0, "end": 2.0, "word": "ove,"},
            ],
        }

        engine.normalize_segments_for_output([segment])
        chunks = list(engine.chunk_segments([segment], "spoken"))

        self.assertEqual(chunks[0].text, "Jove,欢迎来到catavalue jove,")
        self.assertEqual([word["word"] for word in segment["words"]], [
            "Jove,",
            "欢迎来到",
            "catavalue",
            "jove,",
        ])

    def test_chunking_uses_segment_text_when_word_tokens_disagree(self):
        engine = _load_transcribe_engine()
        segment = {
            "start": 10.0,
            "end": 12.0,
            "text": "必须把FDE放在一个AI Agent的框架里面",
            "words": [
                {"start": 10.0, "end": 10.2, "word": "必须"},
                {"start": 10.2, "end": 10.4, "word": "把"},
                {"start": 10.4, "end": 10.6, "word": "F"},
                {"start": 10.6, "end": 10.8, "word": "DE"},
                {"start": 10.8, "end": 11.0, "word": "放在一个"},
                {"start": 11.0, "end": 11.2, "word": "a"},
                {"start": 11.2, "end": 11.4, "word": "i"},
                {"start": 11.4, "end": 11.6, "word": "ag"},
                {"start": 11.6, "end": 11.8, "word": "ing"},
                {"start": 11.8, "end": 12.0, "word": "的框架里面"},
            ],
        }

        engine.normalize_segments_for_output([segment])
        chunks = list(engine.chunk_segments([segment], "spoken"))

        self.assertIn("AI Agent", chunks[0].text)
        self.assertIn("AI Agent", [word["word"] for word in segment["words"]])

    def test_detects_primary_language(self):
        engine = _load_transcribe_engine()
        cases = {
            "zh": [{"text": "大家好我们的AI透镜系列"}],
            "ja": [{"text": "これは日本語のテストです"}],
            "ko": [{"text": "이것은 한국어 테스트입니다"}],
            "en": [{"text": "This is an English transcript"}],
        }
        for expected, segments in cases.items():
            self.assertEqual(engine.detect_primary_language(segments), expected)

    def test_fixes_terms_split_across_chunks(self):
        engine = _load_transcribe_engine()
        chunks = [
            SimpleNamespace(text="前面Pal ant"),
            SimpleNamespace(text="ir后面"),
        ]
        engine.normalize_chunk_boundaries(chunks)
        self.assertEqual(chunks[0].text, "前面Palantir")
        self.assertEqual(chunks[1].text, "后面")

    def test_detects_srt_language(self):
        cases = {
            "zh": ("chinese.srt", "大家好我们的AI透镜系列"),
            "ja": ("japanese.srt", "これは日本語のテストです"),
            "ko": ("korean.srt", "이것은 한국어 테스트입니다"),
            "en": ("english.srt", "This is an English transcript"),
        }
        for expected, (name, text) in cases.items():
            path = self._write_srt(name, text)
            self.assertEqual(autosub.detect_srt_language(path), expected)

    def test_language_copy_uses_detected_suffix(self):
        cases = {
            "zh": ("chinese.srt", "大家好我们的AI透镜系列", ".zh.srt"),
            "ko": ("korean.srt", "이것은 한국어 테스트입니다", ".kr.srt"),
            "ja": ("japanese.srt", "これは日本語のテストです", ".jp.srt"),
            "en": ("english.srt", "This is an English transcript", ".en.srt"),
        }
        for _language, (name, text, suffix) in cases.items():
            path = self._write_srt(name, text)
            copy_path = autosub.create_language_copy(path)
            self.assertTrue(copy_path.endswith(suffix))
            self.assertTrue(os.path.exists(copy_path))

    def test_removes_identical_legacy_english_copy(self):
        base = self._write_srt("chinese.srt", "大家好我们的AI透镜系列")
        legacy = base[:-4] + ".en.srt"
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:00,000 --> 00:00:02,000\n大家好我们的AI透镜系列\n\n")

        copy_path = autosub.create_language_copy(base)

        self.assertTrue(copy_path.endswith(".zh.srt"))
        self.assertTrue(os.path.exists(copy_path))
        self.assertFalse(os.path.exists(legacy))


if __name__ == "__main__":
    unittest.main()
