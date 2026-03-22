
# AutoSub: 全自动视频字幕生成工具

AutoSub 是一款专业的“零点击”自动化工具，旨在实现视频下载、语音识别、智能翻译及字幕压制的一体化流程。它既可以作为独立工具使用，也可以作为 AI 智能体（如 Antigravity, CC, 或 Codex）的扩展技能（Skill）。

## ✨ 功能特性
- **智能下载**：自动抓取来自 YouTube, Twitter, Bilibili 等平台的视频。
- **Whisper 转录**：基于 `faster-whisper` 实现高精度的英文语音转识别。
- **LLM 智能翻译**：利用 Gemini (Flash/Pro), DeepSeek, 硅基流动 (Silicon Flow), 智谱 (GLM), 或 OpenAI 进行上下文感知翻译，并内置“去 AI 味”的人性化润色规则。
- **专业字幕压制**：一键生成带有双语布局和专业样式盒（Vector-Box）的硬字幕视频。
- **动态模型探测**：自动发现并列出各厂商最新的 AI 模型。
- **项目隔离**：支持自定义项目根目录 (Project Root)，让视频素材与字幕文件保持整洁独立。
- **图形界面与命令行**：提供直观的 GUI 界面及强大的 CLI 自动化支持。

## 🆕 v9.1 更新说明 (Bugfixes)
- **环境安装修复**：全面补全了 `install.ps1` 和 `requirements.txt` 中遗漏的依赖组件（涵盖 Google API 系列、`tqdm`、`playwright` 及 `Pillow`）。
- **目录隔离优化**：现在 `.env` 配置文件和 `Projects` 输出文件夹会正确基于安装根目录独立或平行生成，不再污染工具内部源码文件夹。
- **Python 路径解绑**：彻底移除了下载模块中写死的 `yt-dlp` 绝对路径，改为动态搜寻系统环境。
- **UI 体验提升**：修复了 Windows 任务栏底座图标无法正确显示 AutoSub 定制 Icon 的问题。

## 🚀 一键安装 (Windows)

1. **下载或克隆** 本代码库到本地。
2. **右键点击** `install.ps1` 脚本，选择 **“使用 PowerShell 运行”**。
   - *该脚本会自动检查并安装 Python 3.12、FFmpeg 以及所有必要的 Python 库。*
3. **配置 API 密钥**：
   - **方法 A (最简单)**：启动图形界面 (`autosub_gui.py`)，在 "API Key" 输入框填入密钥并点击 **“保存该密钥”**。程序会自动在正确的目录下为您创建 `.env` 文件。
   - **方法 B (手动)**：在 `Library/Tools` 目录下（或如果您使用打包版，则在系统文档 `Documents/AutoSub` 目录）创建 `.env` 文件并添加：
     - `DEEPSEEK_API_KEY=您的密钥` (推荐深度思考模型)
     - `SILICONFLOW_API_KEY=您的密钥`
     - `ZHIPUAI_API_KEY=您的密钥`
     - `GEMINI_API_KEY=您的密钥`
     - `OPENAI_API_KEY=您的密钥`

## 📖 使用方法

### 1. 图形界面模式 (推荐)
运行以下命令启动：
```powershell
python Library\Tools\autosub\autosub_gui.py
```

### 2. Cookies 配置 (获取受限视频)
如果遇到 YouTube 机器人验证（Bot Detection）或处理 Bilibili 会员专享视频，需配置 Cookies：
1. 在浏览器安装扩展：[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/ccmclabimipcebeociikabmgepmeadon) 或 **EditThisCookie**。
2. 访问对应的视频网站并登录。
3. 点击扩展，导出为 **Netscape 格式** 的 `cookies.txt`。
4. **使用方法**：
   - **GUI**：在“Cookies”栏点击“选择...”，加载该文件。
   - **CLI**: Append `--cookies "path/to/cookies.txt"` to your command.

## ⚠️ 常见问题与解决方案 (Troubleshooting)

### 1. 视频下载失败 (YouTube/B站报错)
如果下载时报错（如 `Sign in to confirm you are not a bot` 或 `Video unavailable`），请尝试以下方案：
- **方案 A：配置 Cookies (强烈推荐)**
  按照上方 [Cookies 配置] 步骤操作。这是解决机器人检测和受限视频最有效的方法。
- **方案 B：手动更新下载核心**
  在终端运行以下命令确保 `yt-dlp` 是最新版本：
  ```powershell
  python -m pip install -U yt-dlp
  ```
- **方案 C：更换代理/网络**
  部分视频会根据 IP 地理位置锁定，请确保您的科学上网环境畅通。

### 2. 翻译卡住或报错
- 检查 `.env` 中的 API Key 是否正确。
- 检查网络是否能正常访问对应的 AI 厂商（特别是 Gemini 需要全球化网络环境）。

### 3. 硬压失败
- 确保系统已安装 **FFmpeg**。如果您使用 `install.ps1` 安装，程序会自动处理。

### 4. AI 智能体模式 (IDE / 工作区)
如果您正在使用支持智能体的工作空间（如 Antigravity 或 CC），只需输入：
> `/autosub`

智能体会自动读取 `.agents/workflows/autosub.md` 中的工作流逻辑并引导您完成处理。

## 🛠️ 项目结构
- `.agent/workflows/`：存放供 AI 智能体使用的自动化工作流定义。
- `Library/Tools/`：
  - `autosub/`：主逻辑程序及图形界面。
  - `vdown/`：视频下载引擎（基于 yt-dlp）。
  - `transcriber/`：语音转文字引擎（Whisper）。
  - `subtranslator/`：翻译逻辑及 SRT 工具集。
  - `hardsubber/`：ASS 转换及 FFmpeg 压制引擎。
  - `common/`：共享工具组件。

## 📄 许可证
MIT License。欢迎自由分享、修改和使用！
