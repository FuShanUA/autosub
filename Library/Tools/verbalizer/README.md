
# Verbalizer Skill (Persona Focus)

本技能的职责不是基础翻译，而是**风格迁移 (Style Transfer)**。
它接收已经准确但可能平淡的中文字幕或文字，根据设定的**角色 (Role)** 和 **调性 (Tone)** 进行深度重写。没有设定角色和调性时，默认使用[学术/严谨] (Academic/Formal) 风格。

## 核心理念 (Core Philosophy)
- **Subtranslator 负责"信"**: 确保意思准确，没有语法错误，去除基础翻译腔。
- **Verbalizer 负责"达"与"雅"**: 赋予文本灵魂，使其听起来像特定的人在特定的场合说的话。

## Parameters (风格参数)

在调用时，必须指定以下参数：

### 1. Style / Tone (风格/调性)
- **[另类/智性] (Edgy/Intellectual)**: 
  - 适用：Alex Karp (Palantir), Elon Musk 等科技思想领袖。
  - 特征：使用高势能词汇（底层逻辑、范式、博弈、穿透），冷峻，极简，带有哲学思辨色彩。
  - **拒绝文学化**: 所谓的智性是“深刻的思维”，而不是“华丽的辞藻”。严禁使用文艺气息重的词汇。用 backdrop 而不是 background，翻译成“大环境/宏观背景”，而不是“惊鸿一瞥”。
  - **去噪**: 强力去除口语废话和犹豫，使其听起来思维敏捷、直击要害。
- **[老炮/江湖] (Veteran/Colloquial)**:
  - 适用：经验丰富的行业老手，私下谈话。
  - 特征：大量使用口语连接词（其实、说白了、这就好比），节奏从容，词汇接地气但有力量。
  - **保留**: 可以保留部分语气词以增加"人味"。
- **[学术/严谨] (Academic/Formal)**:
  - 适用：教授，研究员，正式汇报。
  - 特征：逻辑严密，用词精准，语气平和，多用关联词。

### 2. Context (语境)
- **演讲/发布会**: 需要更有煽动性和感染力。
- **对谈/播客**: 需要自然的交互感，保留一定的呼吸感。

### 3. Input Types
- **Subtitle (SRT)**: Standard time-coded subtitles.
- **Narrative Text (TXT/MD)**: Interview transcripts, meeting notes, etc.
  - **Smart Filtering**: Should identify and preserve metadata, editorial notes (e.g., `[Laughter]`, `(Editor's note: ...)`), and narration.
  - **Speaker Recognition**: If specific speakers are identified (e.g., "Elon:", "Interviewer:"), automatically apply the corresponding legacy/persona.

## Guidelines
Detailed style definitions and examples can be found in this document. 
Refer to [SKILL.md](./SKILL.md) for technical execution steps.

## Examples

**原文**: "我们正在经历一场巨大的技术变革。"
- **[另类]**: "我们要谈的是一场范式转移。"

**原文**: "但是...如果...在这个...实际上..." (Dysfluency)
- **[另类]**: "实际上...核心在于..." (简化，保留逻辑转折)
- **[老炮]**: "咋说呢... 其实吧..." (保留犹豫)
