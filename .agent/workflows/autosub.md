---
description: "AutoSub: Interactive Subtitling Workflow"
---

# AutoSub Workflow

This workflow guides you through downloading, transcribing, translating, and burning subtitles for any video.

## Step 1: Select Mode

**Ask the user:**
1.  **GUI Mode** (Graphical Interface) - **Recommended**
2.  **CLI Mode** (Command Line, for automation)

## Step 2: Execution

### Option A: GUI Mode
Run the following command to launch the interface:
```powershell
python "Library/Tools/autosub/autosub_gui.py"
```

### Option B: CLI Mode

**Ask the user to select preferences:**

1.  **Transcription Model**:
    *   `large-v2` (Default)
    *   `large-v3-turbo`

2.  **Translation Method**:
    *   `api` (Gemini Flash 1.5)
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
python Library/Tools/autosub/autosub.py "${URL_OR_FILE}" --model "${MODEL}" --translator "${TRANSLATOR}" --style "${STYLE}" --layout "${LAYOUT}" --cn-font "${CN_FONT}" --en-font "${EN_FONT}" --cn-size "${CN_SIZE}" --en-size "${EN_SIZE}" --cn-color "${CN_COLOR}" --en-color "${EN_COLOR}"
```

## Step 3: IDE / Agent Translation (Only for CLI `ide` strategy)

If you selected CLI IDE mode:
1.  **Read** the generated SRT file path from the output.
2.  **Translate** the content to Simplified Chinese using your Agent capabilities.
3.  **Save** the translated file as `[filename].zh.srt` in the same directory.
4.  **The script** will automatically detect the new file and proceed to burn subtitles.
