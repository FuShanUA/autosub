# Post-Processing Rules for AutoSub

These rules are applied to all translated subtitles BEFORE they are burned or merged.
Feel free to add new rules here.

## 1. Terminology Replacement (Glossary)
These specific strings will be replaced globally. Case-insensitive matching is preferred.

| Original (Regex or String) | Replacement | Notes |
| :--- | :--- | :--- |
| Cloud to Claude | `(?i)\bCloud\b(?=\s*(Code\|AI\|3\|Sonnet\|Haiku\|Opus))` | `Claude` | Fix common whisper error "Cloud Code" -> "Claude Code" |
| Cloud Family to Claude Family | `(?i)\bCloud\b(?=\s*(Family\|Model))` | `Claude` | "Cloud Family" -> "Claude Family" |
| Fix Antrophic Typo | `(?i)Antrophic` | `Anthropic` | Fix typo |
| Fix Gemini Flash Casing | `(?i)Gemini\s*Flash` | `Gemini Flash` | Ensure consistent casing |

| Fix Andrew to Anduril | `(?i)\bAndrew\b` | `Anduril` | ASR mishears "Anduril" as "Andrew" |
| Fix Pot of Jazz to Pot of Jake | `(?i)Pot of Jazz` | `Pot of Jake` | ASR mishears "Jake" as "Jazz" |
| Fix Philippi ad to Philippic | `(?i)Philippi ad` | `Philippic` | ASR mishears "Philippic" as "Philippi ad" |

## 2. Text Cleanup (Regex)
These regex patterns will be executed in order.

| description | pattern | replacement |
| :--- | :--- | :--- |
| Remove Long Dashes | `—+` | `，` | Replace em-dashes with commas for cleaner reading |
| Remove Ellipsis Start | `^\s*\.{2,}` | `` | Remove leading ellipsis "..." |
| Remove Parentheses (En) | `\([^)]*\)` | `` | Remove content in standard brackets (comments) |
| Remove Parentheses (Cn) | `（[^）]*）` | `` | Remove content in full-width brackets |
| Remove Square Brackets | `\[[^\]]*\]` | `` | Remove [sound effects] etc. |
| Fix Double Punctuation | `[，,]{2,}` | `，` | Fix accidental double commas |
| Space after Comma | `，(?=[a-zA-Z0-9])` | `， ` | Add space after comma if followed by English/Number |
| Exclamation Mark Normalization | `!` | `！` | Convert English exclamation to Chinese |
| Question Mark Normalization | `\?` | `？` | Convert English question mark to Chinese |
| Colon Normalization | `:` | `：` | Convert English colon to Chinese |
| Semicolon Normalization | `;` | `；` | Convert English semicolon to Chinese |
