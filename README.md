
# AutoSub: End-to-End Automated Subtitling

[English](README.md) | [简体中文](README_ZH.md)

AutoSub is a professional, "zero-click" workflow to download, transcribe, translate, and hardburn subtitles for any video. It is designed to be used both as a standalone tool and as an extension (Skill) for AI Agents (like Antigravity, CC, or Codex).

## ✨ Features
- **Smart Download**: Automatically fetches videos from YouTube, Twitter, Bilibili, etc.
- **Whisper Transcription**: Uses `faster-whisper` for high-accuracy English transcription.
- **LLM Translation**: Context-aware translation using Gemini (Flash/Pro) with humanized style rules.
- **Professional Styling**: One-click generation of hard-subbed videos with bilingual layouts and vector-box styles.
- **GUI & CLI**: User-friendly interface or powerful command-line automation.

## 🚀 One-Click Installation (Windows)

1. **Clone or Download** this repository to your computer.
2. **Right-click** `install.ps1` and select **"Run with PowerShell"**.
   - *This script will automatically install Python 3.12, FFmpeg, and all required Python libraries.*
3. **Configure API Key**:
   - Open the `.env` file in the root directory.
   - Add your `GEMINI_API_KEY=your_actual_key_here`.

## 📖 How to Use

### 1. GUI Mode (Recommended)
Run the following command:
```powershell
python Library\Tools\autosub\autosub_gui.py
```

### 2. AI Agent Mode (IDE / Workspace)
If you are using an AI Agent (like Antigravity or CC), you can simply type:
> `/autosub`

The agent will read the workflow in `.agent/workflows/autosub.md` and guide you through the process.

## 🛠️ Project Structure
- `.agent/workflows/`: Contains the workflow definition for AI Agents.
- `Library/Tools/`:
  - `autosub/`: Main logic and GUI.
  - `vdown/`: Video download engine (yt-dlp based).
  - `transcriber/`: Transcription engine (Whisper).
  - `subtranslator/`: Translation & SRT utilities.
  - `hardsubber/`: ASS conversion and FFmpeg burning.
  - `common/`: Shared utility sets.

## 📄 License
MIT License. Feel free to share and modify!
