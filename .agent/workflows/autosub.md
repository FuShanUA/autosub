---
description: "AutoSub: Interactive Subtitling Workflow"
---

# AutoSub Workflow

This workflow guides you through downloading, transcribing, translating, and burning subtitles for any video using the latest logic from the [AutoSub Skill](file:///d:/cc/.agents/skills/autosub/SKILL.md).

## Step 1: Select Operation Mode

**Ask the user:**
1.  **GUI Mode** (Graphical Interface) - **Recommended** for ease of use.
2.  **CLI Mode** (Command Line) - For automation or custom parameters.

## Step 2: Execution

### Option A: GUI Mode
Run the following command to launch the interface:
```powershell
Start-Process "C:\Program Files\Python\Python312\python.exe" -ArgumentList "d:\cc\Library\Tools\autosub\autosub_gui.py" -WindowStyle Hidden
```

### Option B: CLI Mode

**Ask the user to select preferences:**

1.  **Transcription Model**:
    *   `large-v2` (Default)
    *   `large-v3-turbo`

2.  **Translation Method**:
    *   `api` (Gemini Flash)
    *   `ide` (Agent / IDE Mode)

3.  **Style / Tone**:
    *   `casual` (Default)
    *   `formal`

4.  **Subtitle Format**:
    *   `bilingual` (Default)
    *   `monolingual`

5.  **Visual Settings**:
    *   **Layout**: `bilingual` (Default), `cn`, `en`
    *   **CN Font**: `KaiTi` (Default), `SimSun`, etc.
    *   **EN Font**: `Arial` (Default), etc.
    *   **CN Size**: `72` (Default)
    *   **EN Size**: `48` (Default)
    *   **CN Color**: `Yellow` (Default)
    *   **EN Color**: `White` (Default)

**Run the script:**
```powershell
python d:\cc\Library\Tools\autosub\autosub.py "${URL_OR_FILE}" --model "${MODEL}" --translator "${TRANSLATOR}" --style "${STYLE}" --layout "${LAYOUT}" --cn-font "${CN_FONT}" --en-font "${EN_FONT}" --cn-size "${CN_SIZE}" --en-size "${EN_SIZE}" --cn-color "${CN_COLOR}" --en-color "${EN_COLOR}"
```

## Step 3: Agent-Assisted Translation (CLI `ide` Mode Only)

If `ide` mode is selected, you MUST follow these [AutoSub Strategy](file:///d:/cc/.agents/skills/autosub/SKILL.md#L28-L35) rules:

1.  **Read** the generated SRT from the script output.
2.  **Smart Chunking**: Read 3 lines at a time to maintain sentence flow.
3.  **Verbalize & Humanize**: Apply `verbalizer` and `humanizer-zh` principles (De-AI).
4.  **No Reverse Explanations**: Avoid including English terms in parentheses.
5.  **Save**: Save the result as `[filename].zh.srt` in the same directory.
6.  **Auto-Detect**: The script will automatically detect the file and finish the "Burn" phase.
