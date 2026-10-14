import os
import shutil
import contextlib
import io
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

try:
    from Library.Tools.autosub import smart_translate
    from Library.Tools.autosub import autosub_batch_pro as batch_pro
    from Library.Tools.autosub.autosub_batch_pro import BatchEngine, SubTask
    from Library.Tools.common import srt_utils
except ModuleNotFoundError:
    from smart_translate import (
        apply_translated_lines,
        audit_second_person_references,
        clean_untranslated_english,
        detect_misaligned_blocks,
        is_untranslated,
        merge_processed_blocks,
        parse_translated_lines,
        postprocess_retry_loop,
        _llm_alignment_check,
    )
    import autosub_batch_pro as batch_pro
    from autosub_batch_pro import BatchEngine, SubTask
    import srt_utils
    smart_translate = SimpleNamespace(
        apply_translated_lines=apply_translated_lines,
        audit_second_person_references=audit_second_person_references,
        clean_untranslated_english=clean_untranslated_english,
        detect_misaligned_blocks=detect_misaligned_blocks,
        is_untranslated=is_untranslated,
        merge_processed_blocks=merge_processed_blocks,
        parse_translated_lines=parse_translated_lines,
        postprocess_retry_loop=postprocess_retry_loop,
        _llm_alignment_check=_llm_alignment_check,
    )


class TranslationDetectionTests(unittest.TestCase):
    def test_non_speech_marker_is_not_untranslated(self):
        self.assertFalse(smart_translate.is_untranslated({"lines": ["（杂音）"]}))

    def test_korean_only_is_untranslated(self):
        self.assertTrue(smart_translate.is_untranslated({"lines": ["한국어 자막"]}))

    def test_chinese_translation_is_not_untranslated(self):
        self.assertFalse(smart_translate.is_untranslated({"lines": ["这是一句中文翻译"]}))

    def test_short_korean_proper_noun_in_chinese_is_allowed(self):
        self.assertFalse(
            smart_translate.is_untranslated({"lines": ["这里是한국团队的工作"]})
        )

    def test_english_proper_noun_list_in_chinese_is_translated(self):
        self.assertFalse(
            smart_translate.is_untranslated(
                {"lines": ["所以我做过很多工作：Google、BigQuery、AppScale、Tableau、PowerBI，这些工具"]}
            )
        )

    def test_mixed_common_english_is_still_untranslated(self):
        self.assertTrue(
            smart_translate.is_untranslated({"lines": ["这是AI actually good"]})
        )


class TranslationRebuildTests(unittest.TestCase):
    def test_single_retry_and_cached_blocks_survive_rebuild(self):
        base_blocks = [
            {"index": 1, "lines": ["한국어 원문"]},
            {"index": 2, "lines": ["Korean source"]},
            {"index": 3, "lines": ["这是缓存翻译"]},
        ]
        translations = {
            "1": "这是单块补翻结果",
            "2": "这是批量翻译结果",
        }

        processed = smart_translate.apply_translated_lines(base_blocks[:2], translations)
        merged = smart_translate.merge_processed_blocks(base_blocks, processed)

        self.assertEqual(merged[0]["lines"], ["这是单块补翻结果"])
        self.assertEqual(merged[1]["lines"], ["这是批量翻译结果"])
        self.assertEqual(merged[2]["lines"], ["这是缓存翻译"])

    def test_parse_translated_lines_rejects_empty_content(self):
        result = smart_translate.parse_translated_lines("[1]\n[2] 中文\nnoise")

        self.assertEqual(result, {"2": "中文"})

    def test_postprocess_reports_tui_progress(self):
        class FakeClient:
            def generate_batch(self, tasks, model, fallback=False, provider=None,
                               progress_callback=None, **_kwargs):
                if progress_callback:
                    progress_callback(1, len(tasks))
                results = []
                for task in tasks:
                    text = "\n".join(
                        f"[{block['index']}] 第{block['index']}块中文"
                        for block in task["chunk"]
                    )
                    results.append({**task, "result": text})
                return results

        blocks = [
            {"index": 1, "lines": ["한국어 원문"]},
            {"index": 2, "lines": ["한국어 원문"]},
        ]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = smart_translate.postprocess_retry_loop(
                blocks,
                FakeClient(),
                "test-model",
                "casual",
                "",
                "",
                "",
            )

        self.assertEqual(result[0]["lines"], ["第1块中文"])
        self.assertEqual(result[1]["lines"], ["第2块中文"])
        self.assertIn("Progress: 96.2%", output.getvalue())
        self.assertIn("Progress: 97.0%", output.getvalue())

    def test_postprocess_scales_deadline_and_enables_model_fallback(self):
        class FakeClient:
            def __init__(self):
                self.kwargs = None

            def generate_batch(self, tasks, model, fallback=False, provider=None,
                               progress_callback=None, **kwargs):
                self.kwargs = {"fallback": fallback, **kwargs}
                if progress_callback:
                    progress_callback(len(tasks), len(tasks))
                results = []
                for task in tasks:
                    text = "\n".join(
                        f"[{block['index']}] 第{block['index']}块中文"
                        for block in task["chunk"]
                    )
                    results.append({**task, "result": text})
                return results

        blocks = [
            {"index": i, "lines": ["한국어 원문"]}
            for i in range(1, 42)
        ]
        client = FakeClient()

        result = smart_translate.postprocess_retry_loop(
            blocks,
            client,
            "glm-5.3",
            "casual",
            "",
            "",
            "",
        )

        self.assertFalse(any(smart_translate.is_untranslated(block) for block in result))
        self.assertTrue(client.kwargs["fallback"])
        self.assertLessEqual(client.kwargs["request_timeout"], 180)
        self.assertGreaterEqual(client.kwargs["batch_timeout"], 800)

    def test_postprocess_stops_after_a_no_progress_pass(self):
        class FakeClient:
            def __init__(self):
                self.batch_calls = 0
                self.single_calls = 0
                self.single_kwargs = None

            def generate_batch(self, tasks, model, fallback=False, provider=None,
                               progress_callback=None, request_timeout=None,
                               max_retries=None, batch_timeout=None):
                self.batch_calls += 1
                self.batch_kwargs = {
                    "request_timeout": request_timeout,
                    "max_retries": max_retries,
                    "batch_timeout": batch_timeout,
                }
                if progress_callback:
                    progress_callback(1, len(tasks))
                return [{**task, "result": None} for task in tasks]

            def generate_content(self, *_args, **kwargs):
                self.single_calls += 1
                self.single_kwargs = kwargs
                return ""

        blocks = [
            {"index": 1, "lines": ["한국어 원문"]},
            {"index": 2, "lines": ["한국어 원문"]},
        ]
        client = FakeClient()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = smart_translate.postprocess_retry_loop(
                blocks,
                client,
                "test-model",
                "casual",
                "",
                "",
                "",
                deadline_seconds=60,
            )

        self.assertEqual(client.batch_calls, 1)
        self.assertEqual(client.batch_kwargs["max_retries"], 1)
        self.assertGreaterEqual(client.batch_kwargs["request_timeout"], 50)
        self.assertLessEqual(client.batch_kwargs["request_timeout"], 60)
        self.assertLessEqual(client.batch_kwargs["batch_timeout"], 60)
        self.assertEqual(client.single_calls, 2)
        self.assertTrue(client.single_kwargs["fallback"])
        self.assertTrue(smart_translate.is_untranslated(result[0]))
        self.assertIn("No segments were resolved", output.getvalue())

    def test_postprocess_deadline_prevents_llm_calls(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def generate_batch(self, *_args, **_kwargs):
                self.calls += 1
                return []

            def generate_content(self, *_args, **_kwargs):
                self.calls += 1
                return ""

        blocks = [{"index": 1, "lines": ["한국어 원문"]}]
        client = FakeClient()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            smart_translate.postprocess_retry_loop(
                blocks,
                client,
                "test-model",
                "casual",
                "",
                "",
                "",
                deadline_seconds=0,
            )

        self.assertEqual(client.calls, 0)
        self.assertIn("deadline reached", output.getvalue())

    def test_naked_fallback_uses_final_translation_validator(self):
        class FakeClient:
            def generate_batch(self, *_args, **_kwargs):
                return []

            def generate_content(self, *_args, **_kwargs):
                return "[24] 这是AI actually good"

        blocks = [{"index": 24, "lines": ["한국어 원문"]}]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = smart_translate.postprocess_retry_loop(
                blocks,
                FakeClient(),
                "test-model",
                "casual",
                "",
                "",
                "",
                deadline_seconds=60,
            )

        self.assertEqual(result[0]["lines"], ["한국어 원문"])
        self.assertIn("naked fallback failed", output.getvalue())


class BatchQualityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _write_srt(self, name, text):
        path = os.path.join(self.temp_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\n")
            handle.write(text + "\n")
        return path

    def test_korean_cn_srt_fails_quality_gate(self):
        path = self._write_srt("video.cn.srt", "한국어 자막")
        reason = batch_pro.validate_translation_srt(path, bilingual=False)

        self.assertIsNotNone(reason)
        self.assertIn("没有中文", reason)
        self.assertIn("ID: 1", reason)

    def test_non_speech_marker_cn_srt_passes_quality_gate(self):
        path = self._write_srt("video.cn.srt", "（杂音）")

        reason = batch_pro.validate_translation_srt(path, bilingual=False)

        self.assertIsNone(reason)

    def test_non_speech_marker_bilingual_srt_passes_quality_gate(self):
        path = self._write_srt("video.bi.srt", "（杂音）\n치킨 치킨")

        reason = batch_pro.validate_translation_srt(path, bilingual=True)

        self.assertIsNone(reason)

    def test_index_merge_preserves_non_speech_marker(self):
        source = self._write_srt("source.srt", "치킨 치킨")
        translated = self._write_srt("translated.srt", "（杂音）")
        output = os.path.join(self.temp_dir, "merged.bi.srt")

        srt_utils.merge_tracks(translated, source, output)

        blocks = srt_utils.parse_srt(output)
        self.assertEqual(blocks[0]["lines"][0], "（杂音）")

    def test_time_merge_preserves_non_speech_marker(self):
        source = os.path.join(self.temp_dir, "source-time.srt")
        translated = os.path.join(self.temp_dir, "translated-time.srt")
        output = os.path.join(self.temp_dir, "merged-time.bi.srt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:02,000\n치킨 치킨\n\n"
                "2\n00:00:03,000 --> 00:00:05,000\n두 번째 문장\n\n"
                "3\n00:00:06,000 --> 00:00:08,000\n빠진 문장\n"
            )
        with open(translated, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:02,000\n（杂音）\n\n"
                "2\n00:00:03,000 --> 00:00:05,000\n这是第二句\n"
            )

        srt_utils.merge_tracks(translated, source, output)

        blocks = srt_utils.parse_srt(output)
        self.assertEqual(blocks[0]["lines"][0], "（杂音）")
        self.assertEqual(blocks[1]["lines"][0], "这是第二句")

    def test_merge_still_removes_ordinary_parenthetical_gloss(self):
        source = self._write_srt("source-gloss.srt", "Source line")
        translated = self._write_srt("translated-gloss.srt", "中文 (English gloss)")
        output = os.path.join(self.temp_dir, "merged-gloss.bi.srt")

        srt_utils.merge_tracks(translated, source, output)

        blocks = srt_utils.parse_srt(output)
        self.assertEqual(blocks[0]["lines"][0], "中文")

    def test_bilingual_srt_checks_translation_line_not_source_line(self):
        path = os.path.join(self.temp_dir, "video.bi.srt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\n")
            handle.write("한국어 자막\nSource line\n")

        reason = batch_pro.validate_translation_srt(path, bilingual=True)
        self.assertIsNotNone(reason)

    def test_disk_truth_blocks_old_bad_cn_file_before_merge(self):
        workdir = os.path.join(self.temp_dir, "15")
        os.makedirs(workdir)
        with open(os.path.join(workdir, "video.cn.srt"), "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\n한국어 자막\n")

        engine = BatchEngine.__new__(BatchEngine)
        engine.res = SimpleNamespace(global_pause=False)
        engine.lock = threading.RLock()
        engine.save_state = MagicMock()
        engine.do_bili = False
        engine.do_gdsync = False
        engine.do_wechat = False
        task = SubTask(uid=15, url="https://example.com", duration="00:01")
        task.workdir = workdir

        engine.disk_truth(task)

        self.assertEqual(task.status, "失败 (TL)")
        self.assertIn("没有中文", task.error)
        engine.save_state.assert_called_once()

    def test_reset_tl_removes_invalid_downstream_outputs(self):
        workdir = os.path.join(self.temp_dir, "15")
        os.makedirs(workdir)
        names = (
            "video.srt",
            "video.cn.srt",
            "video.bi.srt",
            "video.bi.ass",
            "video_hardsub.mp4",
            ".burn_complete",
        )
        for name in names:
            with open(os.path.join(workdir, name), "w", encoding="utf-8") as handle:
                handle.write("data\n")

        engine = BatchEngine.__new__(BatchEngine)
        engine.res = SimpleNamespace(global_pause=False)
        engine.lock = threading.RLock()
        engine.save_state = MagicMock()
        task = SubTask(uid=15, url="https://example.com", duration="00:01")
        task.workdir = workdir
        task.status = "失败 (TL)"
        task.pcts.update({"DL": 100.0, "TR": 100.0, "TL": 0.0, "MR": 0.0, "BR": 0.0})

        stage = engine.reset_task(task)

        self.assertEqual(stage, "TL")
        self.assertEqual(task.status, "排队中 (TL)")
        remaining = set(os.listdir(workdir))
        self.assertIn("video.srt", remaining)
        self.assertNotIn("video.cn.srt", remaining)
        self.assertNotIn("video.bi.srt", remaining)
        self.assertNotIn("video.bi.ass", remaining)
        self.assertNotIn("video_hardsub.mp4", remaining)
        self.assertNotIn(".burn_complete", remaining)

    def test_reset_explicit_tl_overrides_stale_br_state(self):
        workdir = os.path.join(self.temp_dir, "15-explicit")
        os.makedirs(workdir)
        names = (
            "video.srt",
            "video.cn.srt",
            "video.bi.srt",
            "video.bi.ass",
            "video_hardsub.mp4",
            ".burn_complete",
            ".synced",
            ".bili_uploaded",
            ".wechat_uploaded",
            "upload_metadata.json",
        )
        for name in names:
            with open(os.path.join(workdir, name), "w", encoding="utf-8") as handle:
                handle.write("data\n")

        engine = BatchEngine.__new__(BatchEngine)
        engine.res = SimpleNamespace(global_pause=False)
        engine.lock = threading.RLock()
        engine.save_state = MagicMock()
        task = SubTask(uid=15, url="https://example.com", duration="00:01")
        task.workdir = workdir
        task.status = "排队中 (BR)"
        task.pcts.update({
            "DL": 100.0,
            "TR": 100.0,
            "TL": 100.0,
            "MR": 100.0,
            "BR": 36.7,
            "GD": 100.0,
            "MG": 100.0,
            "BI": 100.0,
            "WX": 100.0,
        })

        stage = engine.reset_task(task, stage="TL")

        self.assertEqual(stage, "TL")
        self.assertEqual(task.status, "排队中 (TL)")
        self.assertEqual(task.pcts["TR"], 100.0)
        self.assertEqual(task.pcts["TL"], 0.0)
        self.assertEqual(task.pcts["BR"], 0.0)
        self.assertEqual(task.pcts["WX"], 0.0)
        remaining = set(os.listdir(workdir))
        self.assertIn("video.srt", remaining)
        for name in names[1:]:
            self.assertNotIn(name, remaining)


class SecondPersonReferenceAuditTests(unittest.TestCase):
    def _blocks(self):
        return [
            {"index": 1, "lines": ["Thanks for joining us."]},
            {"index": 2, "lines": ["谢谢你们今天抽出时间。"]},
            {"index": 3, "lines": ["Let's talk about Palantir."]},
        ]

    def _source_lines(self):
        return {
            "1": "Thanks for joining us.",
            "2": "Thank you for your time today.",
            "3": "Let's talk about Palantir.",
        }

    def test_audit_corrects_plural_thanks_for_single_guest(self):
        client = MagicMock()

        def fake_generate_batch(tasks, *_args, **_kwargs):
            return [{**tasks[0], "result": "FLAG: 1"}]

        client.generate_batch.side_effect = fake_generate_batch
        client.generate_content.return_value = "谢谢你今天抽出时间。"
        profile = "### CONVERSATION PROFILE\n- Second-person guidance: one guest"

        result = smart_translate.audit_second_person_references(
            self._blocks(),
            self._source_lines(),
            client,
            "test-model",
            conversation_profile=profile,
        )

        self.assertEqual(result[1]["lines"], ["谢谢你今天抽出时间。"])
        audit_prompt = client.generate_batch.call_args.args[0][0]["prompt"]
        self.assertIn("one guest", audit_prompt)
        self.assertIn("TARGET EN: Thank you for your time today.", audit_prompt)
        self.assertIn("CURRENT CN: 谢谢你们今天抽出时间。", audit_prompt)
        correction_prompt = client.generate_content.call_args.args[0]
        self.assertIn("PREVIOUS BLOCK", correction_prompt)
        self.assertIn("TARGET BLOCK (translate ONLY this)", correction_prompt)
        self.assertIn("NEXT BLOCK", correction_prompt)

    def test_audit_keeps_organizational_your_company(self):
        blocks = [{"index": 1, "lines": ["Palantir，你们公司没签。"]}]
        source_lines = {"1": "Palantir, your company didn't."}
        client = MagicMock()
        client.generate_batch.return_value = [{"result": "ALL_OK"}]

        result = smart_translate.audit_second_person_references(
            blocks, source_lines, client, "test-model"
        )

        self.assertEqual(result[0]["lines"], ["Palantir，你们公司没签。"])
        client.generate_content.assert_not_called()

    def test_audit_failure_keeps_original_blocks(self):
        blocks = self._blocks()
        client = MagicMock()
        client.generate_batch.side_effect = RuntimeError("provider unavailable")

        result = smart_translate.audit_second_person_references(
            blocks, self._source_lines(), client, "test-model"
        )

        self.assertEqual(result, self._blocks())
        client.generate_content.assert_not_called()

    def test_conversation_profile_is_compacted_into_prompt_guidance(self):
        client = MagicMock()
        client.generate_content.return_value = "\n".join([
            "FORMAT: interview",
            "SPEAKERS: one host and one guest",
            "SECOND_PERSON: host addresses the singular guest",
            "NOTES: guest represents Palantir",
        ])

        profile = smart_translate.detect_conversation_profile(
            self._blocks(), client, "test-model", video_title="interview.srt"
        )

        self.assertIn("### CONVERSATION PROFILE", profile)
        self.assertIn("Second-person guidance: host addresses the singular guest", profile)
        prompt = client.generate_content.call_args.args[0]
        self.assertIn("VIDEO TITLE: interview.srt", prompt)
        self.assertIn("TRANSCRIPT SAMPLE", prompt)


class QualityDeadlineTests(unittest.TestCase):
    def _blocks(self):
        return [
            {"index": 1, "lines": ["这是 actually 中文"]},
            {"index": 2, "lines": ["这是第二句中文"]},
        ]

    def _source_lines(self):
        return {
            "1": "This is actually Chinese",
            "2": "This is the second Chinese line",
        }

    def test_expired_deadline_skips_alignment_backstop(self):
        client = MagicMock()
        blocks = self._blocks()

        result = smart_translate.detect_misaligned_blocks(
            blocks,
            self._source_lines(),
            client,
            "test-model",
            "casual",
            "",
            "",
            "",
            deadline=time.monotonic() - 1,
        )

        self.assertEqual(result, blocks)
        client.generate_batch.assert_not_called()
        client.generate_content.assert_not_called()

    def test_alignment_check_forwards_remaining_deadline(self):
        class FakeClient:
            def __init__(self):
                self.kwargs = None

            def generate_batch(self, _tasks, _model, **kwargs):
                self.kwargs = kwargs
                return [{"result": "ALL_ALIGNED"}]

            def generate_content(self, *_args, **_kwargs):
                raise AssertionError("aligned result should not trigger a correction")

        client = FakeClient()
        deadline = time.monotonic() + 60

        smart_translate._llm_alignment_check(
            self._blocks(),
            self._source_lines(),
            client,
            "test-model",
            "casual",
            "",
            "",
            "",
            deadline=deadline,
        )

        self.assertIsNotNone(client.kwargs)
        self.assertEqual(client.kwargs["max_retries"], 1)
        self.assertGreaterEqual(client.kwargs["request_timeout"], 50)
        self.assertLessEqual(client.kwargs["request_timeout"], 60)
        self.assertGreater(client.kwargs["batch_timeout"], 0)
        self.assertLessEqual(client.kwargs["batch_timeout"], 60)

    def test_residual_english_cleanup_forwards_remaining_deadline(self):
        class FakeClient:
            def __init__(self):
                self.kwargs = None

            def generate_content(self, _prompt, *_args, **kwargs):
                self.kwargs = kwargs
                return "这确实是中文"

        client = FakeClient()
        deadline = time.monotonic() + 30

        result = smart_translate.clean_untranslated_english(
            self._blocks(),
            self._source_lines(),
            client,
            "test-model",
            "casual",
            "",
            "",
            "",
            deadline=deadline,
        )

        self.assertEqual(result[0]["lines"], ["这确实是中文"])
        self.assertEqual(client.kwargs["max_retries"], 1)
        self.assertGreaterEqual(client.kwargs["request_timeout"], 20)
        self.assertLessEqual(client.kwargs["request_timeout"], 30)

    def test_expired_deadline_skips_second_person_audit(self):
        blocks = [{"index": 1, "lines": ["谢谢你们。"]}]
        source_lines = {"1": "Thank you."}
        client = MagicMock()

        result = smart_translate.audit_second_person_references(
            blocks,
            source_lines,
            client,
            "test-model",
            deadline=time.monotonic() - 1,
        )

        self.assertEqual(result, blocks)
        client.generate_batch.assert_not_called()
        client.generate_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()
