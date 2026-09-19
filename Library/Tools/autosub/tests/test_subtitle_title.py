import os
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
COMMON_DIR = os.path.join(REPO_ROOT, "..", "common")
if os.path.isdir(COMMON_DIR):
    import sys
    if COMMON_DIR not in sys.path:
        sys.path.insert(0, COMMON_DIR)

from subtitle_title import (
    GroundedTitle,
    SubtitleTitleError,
    _hook_evidence_markers,
    _cjk_sequence_is_grounded,
    _evidence_in_subtitle,
    _llm_prompt,
    _primary_claim_hint,
    _score_title_candidate,
    _title_has_quality_issue,
    _detect_result_terms,
    _ensure_result_term,
    _tighten_title,
    build_subtitle_grounded_title,
    parse_llm_title_candidates,
    parse_llm_title_response,
    read_subtitle_text,
    validate_title_from_subtitle,
)


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, prompt, model_name=None):
        self.calls.append((prompt, model_name))
        return self.response


class ExplodingLLM:
    def generate_content(self, prompt, model_name=None):
        raise RuntimeError("network unavailable")


class TestSubtitleTitle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.video = os.path.join(self.temp_dir, "video_hardsub.mp4")
        self.subtitle = os.path.join(self.temp_dir, "video.bi.srt")
        with open(self.video, "w", encoding="utf-8") as handle:
            handle.write("video")
        with open(self.subtitle, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:04,000\n"
                "前线部署工程师有很多职能，一个FDE最主要的职能，\n"
                "FDEs have many functions, but the primary function of an FDE\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\n"
                "是建立实施文化，在里面守住核心优势。\n"
                "is building an implementation culture where you maintain the alpha.\n"
            )

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_accepts_existing_alpha_translation(self):
        evidence = validate_title_from_subtitle(
            "FDE守住核心优势",
            "一个FDE最主要的职能，是建立实施文化，在里面守住核心优势。",
            ["在里面守住核心优势"],
        )
        self.assertEqual(evidence, ("在里面守住核心优势",))

    def test_rejects_title_entities_missing_from_subtitle(self):
        with self.assertRaises(SubtitleTitleError):
            validate_title_from_subtitle(
                "Palantir CEO卡普揭秘FDE",
                "一个FDE最主要的职能，是建立实施文化，在里面守住核心优势。",
                ["一个FDE最主要的职能"],
            )

    def test_rejects_alpha_translated_against_subtitle_glossary(self):
        with self.assertRaises(SubtitleTitleError):
            validate_title_from_subtitle(
                "FDE守住阿尔法",
                "一个FDE最主要的职能，是建立实施文化，在里面守住核心优势。",
                ["在里面守住核心优势"],
            )

    def test_rejects_evidence_not_in_subtitle(self):
        with self.assertRaises(SubtitleTitleError):
            validate_title_from_subtitle(
                "FDE守住核心优势",
                "一个FDE最主要的职能，是建立实施文化，在里面守住核心优势。",
                ["字幕里不存在这句话"],
            )

    def test_rejects_interior_truncation_of_subtitle_term(self):
        subtitle = "是在事业部层面做业务评估"
        self.assertTrue(_cjk_sequence_is_grounded("事业部层", subtitle))
        self.assertFalse(_cjk_sequence_is_grounded("业部层", subtitle))

    def test_evidence_cannot_join_distant_subtitle_lines(self):
        subtitle = "第一行讲的是主要瓶颈。\n中间隔了很多内容。\n最后一行讲的是集中精力。"
        self.assertTrue(
            _evidence_in_subtitle("讲的是主要瓶颈", "第一行讲的是主要瓶颈。")
        )
        self.assertFalse(
            _evidence_in_subtitle("主要瓶颈，然后集中精力", subtitle)
        )
        self.assertFalse(
            _evidence_in_subtitle(
                "先找出主要瓶颈，然后就集中疏通",
                "先找出主要瓶颈\n然后就集中疏通",
            )
        )
        self.assertTrue(
            _evidence_in_subtitle(
                "先找出主要瓶颈然后就集中疏通",
                "先找出主要瓶颈\n然后就集中疏通",
            )
        )

    def test_llm_response_requires_json_and_evidence(self):
        title, evidence = parse_llm_title_response(
            '```json\n{"title":"FDE守住核心优势","evidence":["守住核心优势"]}\n```'
        )
        self.assertEqual(title, "FDE守住核心优势")
        self.assertEqual(evidence, ("守住核心优势",))

        with self.assertRaises(SubtitleTitleError):
            parse_llm_title_response("FDE守住核心优势")

    def test_llm_prompt_distinguishes_claim_from_supporting_condition(self):
        prompt = _llm_prompt("一个FDE最主要的职能，是打造抽象产品。")
        self.assertIn("先区分结论和支撑条件", prompt)
        self.assertIn("不要把实施方式、限定条件误作结论", prompt)

    def test_plain_prompt_rejects_hooks_and_low_quality_phrases(self):
        prompt = _llm_prompt("一个FDE最主要的职能，是打造抽象产品。")
        self.assertIn("平实、准确的新闻式短标题", prompt)
        self.assertIn("不要疑问句", prompt)
        self.assertIn("不要「为什么」「如何」「才是」", prompt)
        self.assertIn("从没见过", prompt)
        self.assertIn("central_claim", prompt)
        self.assertIn("candidates", prompt)
        self.assertIn("AI 套话", prompt)

    def test_prompt_uses_primary_claim_hint_without_letting_mechanisms_win(self):
        prompt = _llm_prompt(
            "一个FDE最主要的职能，是打造抽象产品。你需要FDE加上本体。",
            primary_claim_hint="一个FDE最主要的职能，是打造抽象产品。",
        )
        self.assertIn("本地检测到主张线索", prompt)
        self.assertIn("不能替代结论", prompt)

    def test_cover_prompt_uses_editorial_judgment_not_default_hooks(self):
        prompt = _llm_prompt(
            "一个FDE最主要的职能，是打造抽象产品，守住客户核心优势。",
            title_style="cover_headline",
        )
        self.assertIn("这是封面标题，不是表单标题", prompt)
        self.assertIn("专业媒体判断句", prompt)
        self.assertIn("不要默认疑问句", prompt)
        self.assertIn("不要为了钩子硬加", prompt)

    def test_cover_prompt_prioritizes_real_subtitle_hooks(self):
        prompt = _llm_prompt(
            "他说AI时代最强二刀流是工程师。直接用大厂模型就像强迫我不带套。",
            title_style="cover_headline",
        )
        self.assertIn("先扫描字幕里的强钩子", prompt)
        self.assertIn("AI时代最强二刀流", prompt)
        self.assertIn("直接用大厂模型就像强迫我不带套", prompt)
        self.assertIn("只学钩子结构", prompt)
        self.assertIn("禁止把示例事实带入当前字幕", prompt)
        self.assertIn("第一人称只允许出现在字幕原话的强钩子", prompt)

    def test_prompt_learns_public_bili_title_structure_without_facts(self):
        plain_prompt = _llm_prompt(
            "一个FDE最主要的职能，是打造抽象产品，守住客户核心优势。",
        )
        cover_prompt = _llm_prompt(
            "一个FDE最主要的职能，是打造抽象产品，守住客户核心优势。",
            title_style="cover_headline",
        )
        for prompt in (plain_prompt, cover_prompt):
            self.assertIn("公开B站标题的结构特征", prompt)
            self.assertIn("只学结构，不学事实", prompt)
            self.assertIn("不强制", prompt)
            self.assertIn("冒号只用于", prompt)
            self.assertIn("疑问句只在", prompt)
            self.assertIn("语气像专业科技媒体", prompt)
        self.assertIn("目标是高度概括全片内容", plain_prompt)
        self.assertIn("目标是吸引人点开观看", cover_prompt)

    def test_primary_claim_hint_prefers_chinese_lines_in_bilingual_srt(self):
        hint = _primary_claim_hint(self.subtitle)
        self.assertIn("一个FDE最主要的职能", hint)
        self.assertIn("是建立实施文化", hint)
        self.assertNotIn("primary function", hint)

    def test_scoring_penalizes_hooks_and_low_quality_phrases(self):
        subtitle_text = (
            "FDE让核心优势长成一个产品，事实上全世界没别人能做得出来。"
            "我们卖的是一个抽象层。"
        )
        claim = "FDE让核心优势长成抽象层产品"
        terms = ("抽象层", "产品")
        plain = _score_title_candidate(
            "FDE如何让核心优势长成产品",
            ("让这份核心优势长成一个产品",),
            claim,
            subtitle_text,
            terms,
        )
        declarative = _score_title_candidate(
            "FDE让核心优势长成抽象层产品",
            ("让这份核心优势长成一个产品",),
            claim,
            subtitle_text,
            terms,
        )
        low_quality = _score_title_candidate(
            "FDE第一次提供价值，全程一条龙支持到底",
            ("让这份核心优势长成一个产品",),
            claim,
            subtitle_text,
            terms,
        )
        self.assertGreater(declarative, plain)
        self.assertEqual(low_quality, -1000.0)
        self.assertTrue(_title_has_quality_issue("客户从没见过这种阵势"))
        self.assertTrue(_title_has_quality_issue("把注意力推到值十倍的价"))

    def test_quality_gate_rejects_first_person_and_fragment_piles(self):
        self.assertTrue(_title_has_quality_issue("我到现在还是这么管公司"))
        self.assertFalse(
            _title_has_quality_issue(
                "直接用大厂模型就像强迫我不带套",
                allow_first_person=True,
            )
        )
        self.assertTrue(_title_has_quality_issue("FDE第一次提供价值"))
        self.assertTrue(_title_has_quality_issue("应用层让你用上产品层模型"))
        self.assertTrue(_title_has_quality_issue("FDE深入现场，解决课题，取得成果"))
        self.assertTrue(_title_has_quality_issue("Databricks把注意力推到"))
        self.assertTrue(_title_has_quality_issue("CEO集中精力疏通瓶颈转型为产品"))
        self.assertTrue(_title_has_quality_issue("Palantir主导应用层掌控产品层产品"))
        self.assertTrue(_title_has_quality_issue("FDE贯通全程支持"))
        self.assertFalse(_title_has_quality_issue("FDE守住核心优势"))

    def test_cover_title_can_select_first_person_hook_from_subtitle(self):
        hook_video = os.path.join(self.temp_dir, "hook_hardsub.mp4")
        hook_subtitle = os.path.join(self.temp_dir, "hook.cn.srt")
        with open(hook_video, "w", encoding="utf-8") as handle:
            handle.write("video")
        with open(hook_subtitle, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:04,000\n"
                "他说AI时代最强二刀流是工程师。\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\n"
                "直接用大厂模型就像强迫我不带套。\n"
            )

        client = FakeLLM(
            '{"central_claim":"直接用大厂模型就像强迫我不带套",'
            '"candidates":['
            '{"title":"AI时代最强二刀流","evidence":["他说AI时代最强二刀流是工程师。"]},'
            '{"title":"直接用大厂模型就像强迫我不带套",'
            '"evidence":["直接用大厂模型就像强迫我不带套。"]}'
            ']}'
        )
        grounded = build_subtitle_grounded_title(
            hook_video,
            llm_client=client,
            title_style="cover_headline",
            min_title_chars=8,
            max_title_chars=48,
        )

        self.assertEqual(grounded.title, "直接用大厂模型就像强迫我不带套")
        self.assertEqual(grounded.source, "llm-subtitle")

    def test_cover_scoring_prefers_balanced_headline_not_rare_word_pile(self):
        subtitle_text = (
            "Databricks CEO先找出主要瓶颈，再集中精力疏通。"
            "把注意力放在商业引擎上，取得产品进展。"
        )
        claim = "Databricks CEO聚焦主要瓶颈并疏通"
        terms = ("商业引擎", "产品")
        balanced = _score_title_candidate(
            "Databricks CEO聚焦主要瓶颈",
            ("先找出主要瓶颈",),
            claim,
            subtitle_text,
            terms,
            title_style="cover_headline",
        )
        pile = _score_title_candidate(
            "Databricks CEO把注意力放在商业引擎上取得产品进展",
            ("把注意力放在商业引擎上",),
            claim,
            subtitle_text,
            terms,
            title_style="cover_headline",
        )
        self.assertGreater(balanced, pile)

    def test_scoring_rewards_balanced_public_title_colon(self):
        subtitle_text = "Databricks CEO先找出主要瓶颈，再集中精力疏通。"
        claim = "Databricks CEO先找出主要瓶颈"
        colon = _score_title_candidate(
            "Databricks CEO：先找出主要瓶颈",
            ("先找出主要瓶颈",),
            claim,
            subtitle_text,
            title_style="cover_headline",
        )
        no_colon = _score_title_candidate(
            "Databricks CEO先找出主要瓶颈",
            ("先找出主要瓶颈",),
            claim,
            subtitle_text,
            title_style="cover_headline",
        )
        self.assertGreater(colon, no_colon)

    def test_prompts_require_complete_sentences_and_no_first_person(self):
        plain_prompt = _llm_prompt("我们先找出主要瓶颈，再集中精力疏通。")
        cover_prompt = _llm_prompt(
            "我们先找出主要瓶颈，再集中精力疏通。",
            title_style="cover_headline",
        )
        for prompt in (plain_prompt, cover_prompt):
            self.assertIn("语法必须完整", prompt)
            self.assertIn("不要把两个动词短语直接拼接", prompt)
        self.assertIn("禁止第一人称", plain_prompt)
        self.assertIn("第一人称只允许出现在字幕原话的强钩子", cover_prompt)
        self.assertIn("最多一个逗号", cover_prompt)

    def test_detects_specific_result_terms(self):
        terms = _detect_result_terms(
            "FDE让核心优势长成产品。我们卖的是一个抽象层。"
        )
        self.assertEqual(terms[0], "抽象层")
        self.assertIn("产品", terms)

    def test_attaches_specific_result_to_generic_noun(self):
        title = _ensure_result_term(
            "FDE如何让核心优势长成产品",
            ("抽象层", "产品"),
            40,
        )
        self.assertEqual(title, "FDE如何让核心优势长成抽象层产品")

    def test_tightens_verbose_subtitle_phrasing(self):
        self.assertEqual(
            _tighten_title("FDE如何打造出全世界没别人能做得出来的抽象层产品"),
            "FDE如何打造全世界没别人能做的抽象层产品",
        )

    def test_editorial_title_uses_duty_result_value_structure(self):
        editorial_video = os.path.join(self.temp_dir, "editorial_hardsub.mp4")
        editorial_subtitle = os.path.join(self.temp_dir, "editorial.cn.srt")
        with open(editorial_video, "w", encoding="utf-8") as handle:
            handle.write("video")
        with open(editorial_subtitle, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:04,000\n"
                "一个FDE最主要的职能，是让我们打造出可以规模化、部署到全世界的产品。\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\n"
                "我们卖的是一个抽象层。\n\n"
                "3\n00:00:08,000 --> 00:00:12,000\n"
                "在里面守住客户的核心优势。\n"
            )

        grounded = build_subtitle_grounded_title(
            editorial_video,
            llm_client=ExplodingLLM(),
            title_style="editorial",
            attribution="Palantir CEO卡普",
        )

        self.assertEqual(
            grounded.title,
            "Palantir CEO卡普：FDE的真正职责是抽象产品，守护用户核心优势",
        )
        self.assertEqual(grounded.core_title, "FDE的真正职责是抽象产品，守护用户核心优势")
        self.assertEqual(grounded.attribution, "Palantir CEO卡普")
        self.assertEqual(grounded.source, "editorial-subtitle")
        self.assertGreaterEqual(len(grounded.evidence), 2)

    def test_editorial_prompt_requires_media_judgment_structure(self):
        prompt = _llm_prompt(
            "一个FDE最主要的职能，是打造抽象产品，守住客户核心优势。",
            title_style="editorial",
        )
        self.assertIn("媒体判断句", prompt)
        self.assertIn("不要默认疑问句", prompt)
        self.assertIn("抽象产品", prompt)

    def test_parses_multi_candidate_llm_response(self):
        central_claim, candidates = parse_llm_title_candidates(
            '{"central_claim":"FDE建立实施文化",'
            '"candidates":[{"title":"为什么FDE守住核心优势",'
            '"evidence":["守住核心优势"]}]}'
        )
        self.assertEqual(central_claim, "FDE建立实施文化")
        self.assertEqual(candidates, (("为什么FDE守住核心优势", ("守住核心优势",)),))

    def test_title_connectors_are_not_treated_as_facts(self):
        evidence = validate_title_from_subtitle(
            "为什么核心优势长成产品",
            "让这份核心优势长成一个产品，事实上全世界没别人能做得出来。",
            ["让这份核心优势长成一个产品"],
        )
        self.assertEqual(evidence, ("让这份核心优势长成一个产品",))

    def test_llm_title_must_pass_grounding_before_use(self):
        client = FakeLLM(
            '{"title":"Palantir CEO卡普揭秘FDE","evidence":["一个FDE最主要的职能"]}'
        )
        grounded = build_subtitle_grounded_title(self.video, llm_client=client)

        self.assertEqual(client.calls and len(client.calls), 2)
        self.assertEqual(grounded.source, "extractive-subtitle")
        with open(self.subtitle, encoding="utf-8") as handle:
            self.assertIn(grounded.title, handle.read())

    def test_llm_candidates_are_ranked_after_grounding(self):
        client = FakeLLM(
            '{"central_claim":"FDE最主要的职能是建立实施文化并守住核心优势",'
            '"candidates":['
            '{"title":"FDE建立实施文化","evidence":["是建立实施文化"]},'
            '{"title":"为什么FDE守住核心优势","evidence":["守住核心优势"]},'
            '{"title":"Palantir CEO揭秘FDE","evidence":["一个FDE最主要的职能"]}'
            ']}'
        )
        grounded = build_subtitle_grounded_title(self.video, llm_client=client)

        self.assertEqual(grounded.source, "llm-subtitle")
        self.assertEqual(grounded.title, "FDE建立实施文化")
        self.assertEqual(len(client.calls), 1)

    def test_prefers_chinese_subtitle_over_bilingual_subtitle(self):
        chinese_path = os.path.join(self.temp_dir, "video.cn.srt")
        with open(chinese_path, "w", encoding="utf-8") as handle:
            handle.write(
                "1\n00:00:00,000 --> 00:00:04,000\n"
                "FDE最主要的职能是打造抽象产品。\n"
            )

        grounded = build_subtitle_grounded_title(self.video)

        self.assertEqual(grounded.subtitle_path, chinese_path)
        self.assertEqual(grounded.title, "FDE最主要的职能是打造抽象产品")

    def test_extractive_fallback_never_uses_filename(self):
        grounded = build_subtitle_grounded_title(self.video)
        self.assertIsInstance(grounded, GroundedTitle)
        self.assertEqual(grounded.source, "extractive-subtitle")
        self.assertTrue(grounded.evidence)
        self.assertNotIn("video", grounded.title)

    def test_llm_network_failure_uses_subtitle_fallback(self):
        grounded = build_subtitle_grounded_title(self.video, llm_client=ExplodingLLM())
        self.assertEqual(grounded.source, "extractive-subtitle")
        self.assertTrue(grounded.evidence)

    def test_long_subtitle_fallback_validates_against_full_transcript(self):
        long_video = os.path.join(self.temp_dir, "long_hardsub.mp4")
        long_subtitle = os.path.join(self.temp_dir, "long.cn.srt")
        target = "先找出主要瓶颈再集中疏通"
        blocks = []
        for index in range(1, 602):
            text = target if index % 2 == 0 else f"这是第{index}条很长的字幕内容"
            start = index - 1
            end = index
            blocks.append(
                f"{index}\n00:{start // 60:02d}:{start % 60:02d},000 --> "
                f"00:{end // 60:02d}:{end % 60:02d},000\n{text}\n"
            )
        with open(long_video, "w", encoding="utf-8") as handle:
            handle.write("video")
        with open(long_subtitle, "w", encoding="utf-8") as handle:
            handle.write("\n".join(blocks))

        sampled, _ = read_subtitle_text(long_video)
        self.assertNotIn(target, sampled or "")

        grounded = build_subtitle_grounded_title(
            long_video,
            llm_client=ExplodingLLM(),
            max_title_chars=30,
            min_title_chars=8,
        )

        self.assertEqual(grounded.source, "extractive-subtitle")
        self.assertEqual(grounded.title, target)
        self.assertEqual(grounded.evidence, (target,))

    def test_no_subtitle_is_a_hard_error(self):
        os.remove(self.subtitle)
        with self.assertRaises(SubtitleTitleError):
            build_subtitle_grounded_title(self.video)


if __name__ == "__main__":
    unittest.main()
