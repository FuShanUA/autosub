# Universal Text Post-Processing Style Guide

These rules are applied to translated texts, subtitles, and articles across various workflows BEFORE finalizing.

> **RULE FOR ADDING ENTRIES TO SECTIONS 1-4**: Replacement values MUST be a **single string**.
> Never use slash-separated alternatives (e.g., `做/处理`) as a replacement — the slash outputs literally and corrupts text.
> Section 5 is **declarative guidance for LLM prompts only** — it is NOT mechanically executed as regex.

---

## 1. Terminology Replacement (Glossary)
Rules are auto-classified by the post-processing script based on the replacement value:
- **ASR rules** (replacement is pure English): Applied PRE-LLM on source text. MUST NOT contain any CJK characters.
- **TERM rules** (replacement contains CJK): Applied POST-LLM on translated output only. Never touch source text before the LLM sees it.

| Original (Regex or String) | Replacement | Notes |
| :--- | :--- | :--- |
| Claude ASR Fix (conservative) | `(?i)\bCloud\b(?=\s*(?:3\.5\|Sonnet\|Haiku\|Opus\|Anthropic\|Code\|Force\|Tag\|Desktop\|API\|Pro\|Team\|Enterprise\|is\s+(?:the\|a\|an\|our\|best\|keeping)\|was\|within))` | `Claude` | Fix when Cloud is followed by Claude model names, product names (Code, Force, Tag, Desktop, API), or Anthropic. Also fix common sentence patterns like "Cloud is the best model". Excludes generic cloud computing contexts like "cloud business", "AI cloud", "cloud spend". |
| Fix Antrophic Typo | `(?i)Antrophic` | `Anthropic` | Fix typo |
| Fix Gemini Flash Casing | `(?i)Gemini\s*Flash` | `Gemini Flash` | Ensure consistent casing |
| FDE Pattern | `(?i)Forward[\s-]*Deployed\s*Engineering\b` | `前线部署模式` | Must come BEFORE FDE Person to avoid substring match; concept/methodology |
| FDE Person | `(?i)Forward[\s-]*Deployed\s*Engineer(s)?\b` | `前线部署工程师` | `\b` prevents matching "Engineering" |
| Vibe Coding | `(?i)(?:vibe\s-?coding\|凭感觉写代码)` | `vibe coding` | Preserve specific term without translation |
| OpenClaw Fix | `(?i)Open\s*(Clause\|Cloud\|Clawd?\|Claw)s?` | `OpenClaw` | Fix ASR error for OpenClaw |
| Clawd Protection | `(?i)\bClawd\b` | `Clawd` | Protect earlier project name |
| Claw-like | `(?i)claw\s*-like` | `Claw-like` | Ensure consistent terminology |
| Standalone Claw | `(?i)\b(claws?\|clause)\b` | `Claw` | Standardize Karpathy's agent term (cautious) |
| Ondas ASR Fix | `(?i)\b(Andas\|OnDos)\b` | `Ondas` | Fix ASR typo for Ondas |
| Spracks ASR Fix | `(?i)\bSpracks\b` | `Servers, racks` | Whisper merges "Servers, racks" into "Spracks" when spoken fast. Restore the two-word phrase. |
| CompleteStack ASR Fix | `(?i)\bCompleteStack\b` | `complete stack` | Whisper merges "complete stack" into camelCase "CompleteStack", making the LLM treat it as a proper noun. Restore to lowercase two-word phrase so LLM translates it normally. |
| Cloth→Claude ASR Fix | `(?i)\b(using\|at\|with)\s+cloth\b` | `\1 Claude` | Whisper mishears "Claude" as "cloth" in AI/agent contexts. Only fix after prepositions like using/at/with. |
| Quinn→Qwen ASR Fix | `(?i)Alibaba'?s\s+Quinn\b` | `Alibaba's Qwen` | Whisper mishears "Qwen" (Alibaba's LLM) as "Quinn". Fix in Alibaba context to prevent LLM treating it as a person name. |
| Kimmy→Kimi ASR Fix | `(?i)the\s+Kimmy\b` | `the Kimi` | Whisper mishears "Kimi" (Moonshot AI's LLM) as "Kimmy". Fix after "the" (model reference, not person name). |
| Kimmy K3→Kimi K3 Fix | `(?i)Kimmy\s+K3\b` | `Kimi K3` | Whisper mishears "Kimi" as "Kimmy". K3 is the latest Moonshot model (2025); do NOT downgrade to K2. |
| Jensen Wang→Huang ASR Fix | `(?i)Jensen\s+Wang\b` | `Jensen Huang` | Whisper mishears "Huang" as "Wang" for NVIDIA CEO Jensen Huang (黄仁勋). Very common ASR error in tech interviews. |
| Alex Karb→Karp ASR Fix | `(?i)Alex\s+Karb\b` | `Alex Karp` | Whisper mishears "Karp" as "Karb" for Palantir CEO Alex Karp. |
| Dario Amadei→Amodei ASR Fix | `(?i)Dario\s+Amadei\b` | `Dario Amodei` | Whisper mishears "Amodei" as "Amadei" for Anthropic CEO Dario Amodei. |
| Jensen Hwang→Huang ASR Fix | `(?i)Jensen\s+Hwang\b` | `Jensen Huang` | Whisper mishears "Huang" as "Hwang" for NVIDIA CEO Jensen Huang. |
| Anthropics→Anthropic's ASR Fix | `(?i)\bAnthropics\b(?!\s+Fable\b)` | `Anthropic's` | Whisper drops apostrophe from "Anthropic's". Fix unless followed by "Fable" (handled by Fable rule below). |
| Fable Model Protection | `(?i)Anthropic'?s?\s*(?:的\s*)?寓言` | `Anthropic 的 Fable` | Protect Anthropic's "Fable" model name from being translated to "寓言". |
| Jensen Huang→黄仁勋 | `(?i)(?<![a-zA-Z])Jensen\s+Huang(?![a-zA-Z])` | `黄仁勋` | NVIDIA CEO is universally known as 黄仁勋 in Chinese media. Translate in all contexts. Uses ASCII lookaround instead of \b because \b fails at CJK boundaries. |
| Enderlin→Anduril ASR Fix | `(?i)\bEnderlin\s+Palmer\b` | `Anduril, Palmer` | Whisper merges "Anduril, Palmer" into "Enderlin Palmer". Fix when Palmer Luckey context exists. |
| Enderlin Standalone Fix | `(?i)\bEnderlin\b` | `Anduril` | Whisper mishears "Anduril" as "Enderlin" in tech/defense context. |
| She→Xi ASR Fix (political) | `(?i)\bshe\s+and\s+the\s+president\b` | `Xi and the president` | Whisper mishears "Xi" (Chinese leader) as "she" in US-China political context. Only fix when followed by "and the president" to avoid false positives. |
| Tough→Sharp Confusion Guard | `(?i)you\s+guys\s+are\s+tough\b` | `you guys are tough` | Protect "tough" (meaning difficult/hard to deal with) from being mistranslated as 锐利. In interview context, "you guys are tough" means the interviewers ask hard questions. |

## 2. Text Cleanup (Regex)
These regex patterns will be executed in order by the post-processing script.

| description | pattern | replacement |
| :--- | :--- | :--- |
| Remove Long Dashes | `—+` | `，` | Replace em-dashes with commas for cleaner reading |
| Remove Leading Punctuation | `^[，。！？、,\.\?!]\s*` | `` | Remove punctuation at the start of a line |
| Fix Period Comma | `([。．！？\.\?!])\s*[，,]` | `\1` | Fix comma occurring right after a sentence terminator |
| Remove Ellipsis Start | `^\s*\.{2,}` | `` | Remove leading ellipsis "..." |
| Remove Parentheses (En) | `\([^)]*\)` | `` | Remove content in standard brackets (comments) |
| Remove Parentheses (Cn) | `（[^）]*）` | `` | Remove content in full-width brackets |
| Remove Square Brackets | `\[[A-Za-z0-9\s,.?!'-]{2,}\]` | `` | Remove [sound effects] etc. |
| Fix Double Punctuation | `[，,]{2,}` | `，` | Fix accidental double commas |
| Space after Comma | `，(?=[a-zA-Z0-9])` | `， ` | Add space after comma if followed by English/Number |
| Exclamation Mark Normalization | `!` | `！` | Convert English exclamation to Chinese |
| Question Mark Normalization | `\?` | `？` | Convert English question mark to Chinese |
| Comma Normalization | `,` | `，` | Convert English comma to Chinese |
| Colon Normalization | `:` | `：` | Convert English colon to Chinese |
| Semicolon Normalization | `;` | `；` | Convert English semicolon to Chinese |

## 3. Absolute Bans (Hardcoded AI Clichés)

All entries are currently commented out pending validation.
To activate a rule, move it out of the HTML comment and confirm the replacement is a single, context-safe string.

| Description | Pattern | Replacement | Notes |
| Ban 此外 | `此外` | `另外` | |
| Ban 总而言之 | `总而言之` | `简单说` | |
| Ban 至关重要 | `至关重要` | `非常重要` | |
| Ban 见证 | `见证` | `看到` | |
| Ban 宝贵的 | `宝贵的` | `有价值的` | |
| Ban 不可或缺 | `不可或缺` | `很重要` | |
| Ban 赋予 | `赋予` | `给` | |
| Ban 赋能 | `赋能` | `支持` | |
| Ban 意味着 | `意味着` | `说明` | |
| Ban 深入探讨 | `深入探讨` | `聊聊` | |
| Ban 致力于 | `致力于` | `专注` | |
| Ban 闭环 | `闭环` | `完整流程` | |
| Ban 抓手 | `抓手` | `切入点` | |
| Ban 行业格局 | `不断演变的格局` | `行业现状` | |
| Ban 前所未有 | `前所未有的` | `少见的` | |
| Ban 相互作用 | `相互作用` | `影响` | |
| Ban 有一说一 | `有一说一` | `` | |
| Ban 不得不说 | `不得不说` | `` | |
| Ban 综上所述 | `综上所述` | `` | |
| Ban 未来可期 | `未来可期` | `` | |
| Ban 降维打击 | `降维打击` | `压制性优势` | 或直接描述具体效果 |
| Ban 提了个醒 | `提了个醒` | `反映了...趋势` | 视语境而定 |
| Ban 不是...而是 | `不是.*而是` | `` | 见 WritingStyle/README.md |
| Ban 不仅是...更是 | `不仅是.*更是` | `` | 见 WritingStyle/README.md |
| Ban 不要...而 | `不要.*而` | `` | 见 WritingStyle/README.md |
| Ban 深挖 | `深挖` | `深入分析` | 严格禁用 |

<!--
COMMENTED OUT — 所有条目均待验证，机械替换风险高，暂由 Section 5 声明式规则覆盖。
如需启用某条，请确认替换词在所有上下文中均安全且为单一词，再移入正式 Section 3 表格。

--- 原 Section 3（此前认为无歧义的 AI 腔） ---
| Description | Pattern | Replacement | Notes |
| Ban 此外 | `此外` | `另外` | |
| Ban 总而言之 | `总而言之` | `简单说` | |
| Ban 至关重要 | `至关重要` | `非常重要` | |
| Ban 见证 | `见证` | `看到` | |
| Ban 宝贵的 | `宝贵的` | `有价值的` | |
| Ban 不可或缺 | `不可或缺` | `很重要` | |
| Ban 赋予 | `赋予` | `给` | |
| Ban 赋能 | `赋能` | `支持` | |
| Ban 意味着 | `意味着` | `说明` | |
| Ban 深入探讨 | `深入探讨` | `聊聊` | |
| Ban 致力于 | `致力于` | `专注` | |
| Ban 闭环 | `闭环` | `完整流程` | |
| Ban 抓手 | `抓手` | `切入点` | |
| Ban 行业格局 | `不断演变的格局` | `行业现状` | |
| Ban 前所未有 | `前所未有的` | `少见的` | |
| Ban 相互作用 | `相互作用` | `影响` | |
| Ban 有一说一 | `有一说一` | `` | |
| Ban 不得不说 | `不得不说` | `` | |
| Ban 综上所述 | `综上所述` | `` | |
| Ban 未来可期 | `未来可期` | `` | |

--- 上下文歧义类（俚语/术语） ---
| Ban 啥 | `啥` | `什么` | Standardize informalism |
| Ban 爽 | `爽` | `舒畅` | Avoid overly casual tone |
| Ban 死磕 | `死磕` | `坚持` | Professionalize vague slang |
| Ban 飞起 | `(?:极快\|)*飞起` | `极快` | Standardize speed descriptors |
| Ban 估摸 | `估摸` | `估计` | Professionalize estimation |
| Ban 渣渣 | `渣渣` | `很差` | Professionalize negative descriptor |
| Ban 顶 | `\b顶\b` | `出色` | Professionalize positive descriptor |
| Ban 犀利 | `犀利` | `锐利` | Avoid AI habitual adverb |
| Ban 缘分邂逅 | `缘分邂逅` | `不期而遇` | Remove romantic/AI phrasing |
| Ban 护城河 | `护城河` | `业务壁垒` | Overused AI metaphor |
| Ban 硬刚 | `硬刚` | `硬碰` | Professionalize slang |
| Ban 另类 | `另类` | `独特` | Avoid "alt" style terminology |
| Ban 祖传代码 | `祖传代码` | `遗留代码` | Professional technical term |
| Ban 拿来主义 | `拿来主义` | `直接引用` | Avoid cliché |
| Ban 心血 | `心血` | `努力` | Professionalize effort |
| Ban 手到擒来 | `手到擒来` | `轻而易举` | Avoid AI hyperbole |
| Ban 极度垂直 | `极度垂直` | `高度专业` | Avoid internet buzzwords |
| Ban 捞好处 | `捞好处` | `获益` | Professionalize benefit |
| Ban 秒成渣 | `秒成渣` | `彻底失效` | De-dramatize |
| Ban 更抗打 | `更抗打` | `更具韧性` | De-slang |
| Ban 溜溜地 | `溜溜地` | `流畅地` | De-slang |
| Ban 缺啥补啥 | `缺啥补啥` | `按需取用` | Professionalize selection |
| Ban 手搓 | `手搓` | `手工开发` | De-slang |
| Ban 秒秒钟/秒杀 | `秒秒钟\|秒杀` | `迅速` | Professionalize speed/dominance |
| Ban 门儿清 | `门儿清` | `心中有数` | Avoid over-casual jargon |
| Ban 递刀子 | `递刀子` | `助力` | Avoid overly dramatic slang |
| Ban 骨子里 | `骨子里` | `本质上` | Avoid anthropomorphic metaphor |
| Ban 一根筋 | `一根筋` | `执着` | Professionalize persistence |
| Ban 瞎吹 | `瞎吹` | `夸大其词` | Professionalize dismissal |
| Ban 卷上天 | `卷上天` | `竞争异常激烈` | Avoid net-slang |
| Ban 撒胡椒面 | `撒胡椒面` | `全面铺开` | Avoid agricultural metaphor |
| Ban 破局 | `破局` | `突破现状` | Overused AI business buzzword |
| Ban 发力点 | `发力点` | `切入点` | Avoid abstract management jargon |
| Ban 掉链子 | `掉链子` | `出现纰漏` | Avoid informal slang |
| Ban 怂得很 | `怂得很` | `过于保守` | Avoid derogatory slang |
| Ban 捅娄子 | `捅娄子` | `惹麻烦` | Avoid informal slang |
| Ban 彻底重塑 | `彻底重塑` | `重塑` | Remove AI emphasis inflation |
| Ban 爆发式 | `爆发式` | `显著` | De-dramatize hyperbole |
| Ban 不是...而是 | `不是.*而是` | (禁止排比对比) | 见 WritingStyle/README.md |
| Ban 不仅是...更是 | `不仅是.*更是` | (禁止升华排比) | 见 WritingStyle/README.md |
| Ban 不要...而 | `不要.*而` | (禁止说教语气) | 见 WritingStyle/README.md |
-->

## 4. Compliance & Terminology (合规与统一术语)

These are strict media compliance rules. They are mechanically applied by the post-processing script.

| Description | Pattern | Replacement | Notes |
| :--- | :--- | :--- | :--- |
| Taiwan Reg/Status 1 | `中华民国` | `台湾地区` | Compliance |
| Taiwan Reg/Status 2 | `台湾政府` | `台湾当局` | Compliance |
| Taiwan Authorities | `(总统\|台湾地区领导人)大选` | `台湾地区领导人选举` | Compliance |
| Taiwan Exec | `行政院` | `台湾地区行政管理机构` | Compliance |
| Taiwan Legis | `立法院` | `台湾地区立法机构` | Compliance |
| Vulnerable Grp 1 | `残废人` | `残疾人` | Media Standard |
| Vulnerable Grp 2 | `(傻子\|呆子)` | `智力障碍` | Media Standard |
| Cross Strait | `中国(?:和\|与)台湾` | `海峡两岸` | Compliance |
| Hong Kong / Macao | `(中港\|中澳)` | `内地与香港` | General replacement, manual check required if Macao |
| Names Protection | `约翰·温费尔特` | `John Wernfeldt` | 禁止翻译人名 |


---
*Last updated: 2026-03-08*

> **Note**: Declarative LLM style guidance (formerly Section 5) has been moved to
> `/Users/shanfu/cc/Library/Tools/WritingStyle/README.md` — loaded as `WRITING_STYLE_RULES` in `smart_translate.py`.
