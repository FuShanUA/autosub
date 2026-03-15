---
name: verbalizer
description: Tools for stylistic rewriting of subtitles (Persona/Tone adjustment).
---

# Verbalizer Skill

This skill allows you to **rewrite** translations to match a specific persona or tone (e.g., "Edgy Tech Leader", "Casual Chat", "Formal Academic").

## User Guide
The linguistic rules, core philosophy, and style definitions are documented in [README.md](./README.md). **Always refer to README.md to determine the correct tone and vocabulary mapping.**

## Technical Workflow

1.  **Input**:
    -   Single file: Monolingual SRT, or Text/Markdown.
    -   **Note**: For bilingual SRTs, use `subtranslator` to handle splitting/merging first.

2.  **Execution**:
    -   Run the command: `python verbalizer.py <input> --style [Style]`
    -   Available styles are listed in `README.md` (e.g., `Edgy`, `Veteran`, `Academic`).
    -   Outputs will follow the pattern: `*.verbalized.*`.

3.  **Deployment**:
    -   When used in a larger workflow, this skill should be applied **after** initial translation.

## When to Use
1. The user asks for a specific "tone" or "persona".
2. The speaker is known (e.g., Alex Karp, Elon Musk) and has a distinct style.
3. The content needs to be "humanized" or "stylized" beyond basic translation.
