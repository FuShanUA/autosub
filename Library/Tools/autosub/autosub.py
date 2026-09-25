import os
import sys
import subprocess
import json
import time
import re
import glob
import shutil
import argparse
import io

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# --- Global Window Suppression Patch for Windows ---
if sys.platform == "win32":
    _original_popen = subprocess.Popen
    def _patched_popen(*args, **kwargs):
        cflags = kwargs.get('creationflags', 0)
        kwargs['creationflags'] = cflags | CREATE_NO_WINDOW
        return _original_popen(*args, **kwargs)
    subprocess.Popen = _patched_popen

if sys.platform == "win32":
    import msvcrt
else:
    import select
    import termios
    import tty

    def _getch():
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return None
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch.encode('utf-8')

    def _kbhit():
        if not os.isatty(sys.stdin.fileno()):
            return False
        return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])

    class _msvcrt_mock:
        @staticmethod
        def kbhit(): return _kbhit()
        @staticmethod
        def getch(): return _getch()
    
    msvcrt = _msvcrt_mock()

from datetime import datetime

# System and Third-party imports

# Force UTF-8 for stdout/stderr to handle emojis in logs on Windows
if sys.platform == "win32":
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

# --- Logging Helper ---
class Logger:
    def __init__(self, log_path):
        self.terminal = sys.stdout
        self.log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        
    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)
        self.terminal.flush()
        self.log_file.flush()
        
    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

    def close(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None  # type: ignore

# --- Configuration ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_FILE = os.path.join(CURRENT_DIR, "defaults.json")
SETTINGS_FILE = os.path.join(CURRENT_DIR, "settings.json")
DEFAULTS = {}
if os.path.exists(DEFAULTS_FILE):
    try:
        with open(DEFAULTS_FILE, "r", encoding="utf-8") as f:
            DEFAULTS.update(json.load(f))
    except: pass
if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            DEFAULTS = json.load(f)
    except Exception as e:
        print(f"Warning: Failed to load settings.json: {e}")

VENDOR_MAP = {
    "Google (Vertex AI)": "vertex",
    "Google (Gemini API)": "gemini",
    "OpenAI": "openai",
    "Anthropic": "anthropic",
    "DeepSeek": "deepseek",
    "Alibaba (Bailian)": "dashscope",
    "DashScope (Alibaba Cloud)": "dashscope",
    "Zhipu AI": "zhipu",
    "Moonshot (Kimi)": "moonshot",
    "SiliconFlow": "siliconflow",
    "MiniMax": "minimax"
}

import multiprocessing
multiprocessing.freeze_support()

# --- Environment Setup (Isolation Logic) ---
if getattr(sys, 'frozen', False):
    # Packaged / Installer Mode
    BUNDLE_DIR = getattr(sys, '_MEIPASS', '')
    TOOLS_DIR = os.path.join(BUNDLE_DIR, "Library", "Tools")
    USER_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub")
    PROJECT_ROOT = USER_DATA_DIR
    BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Projects")
    env_path = os.path.join(PROJECT_ROOT, ".env")
    
    # In bundled mode, common is in BUNDLE_DIR/Library/Tools/common
    sys.path.append(os.path.join(TOOLS_DIR, "common"))
else:
    # Developer Mode (d:\cc)
    CURRENT_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    TOOLS_DIR = os.environ.get("AUTOSUB_TOOLS_DIR") or os.path.dirname(CURRENT_SCRIPT_DIR)
    
    # Anchor to true repository root d:\cc
    tmp_root = os.path.dirname(TOOLS_DIR)
    if os.path.basename(tmp_root).lower() == "library":
        PROJECT_ROOT = os.path.dirname(tmp_root)
    else:
        PROJECT_ROOT = tmp_root

    # Guard: the naive "library" heuristic under-walks when the package is
    # nested inside itself (cc/Library/Tools/autosub/Library/Tools/autosub).
    # Walk up until we find the real root, identified by CLAUDE.md or AGENTS.md
    # (the workspace root signature). We cannot use .env or .agents because the
    # autosub package has its own .env and a symlinked .agents.
    _walk = PROJECT_ROOT
    while True:
        if os.path.exists(os.path.join(_walk, "CLAUDE.md")) or os.path.exists(os.path.join(_walk, "AGENTS.md")):
            PROJECT_ROOT = _walk
            break
        parent = os.path.dirname(_walk)
        if parent == _walk:
            break
        _walk = parent

    BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Projects")
    env_path = os.path.join(PROJECT_ROOT, ".env")
    
    # In dev mode, common is in d:\cc\Library\Tools\common
    sys.path.append(os.path.join(TOOLS_DIR, "common"))
    print(f"🏠 [DEV MODE] Root: {PROJECT_ROOT}")

# Load srt_utils after path setup
try:
    import srt_utils
except ImportError:
    srt_utils = None

def get_python_exe():
    """Returns python.exe even if running under pythonw.exe to ensure console output stability."""
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("pythonw.exe"):
        py_exe = exe.lower().replace("pythonw.exe", "python.exe")
        if os.path.exists(py_exe): return py_exe
        
    # Prefer virtual environment python if it exists
    curr_dir = os.path.dirname(os.path.abspath(__file__))
    venv_py = os.path.join(curr_dir, ".venv", "bin", "python" if sys.platform != "win32" else "python.exe")
    if os.path.exists(venv_py):
        return venv_py
        
    return exe

def trigger_sync(local_dir, remote_id, headless=False):
    """Launches a single-run Google Drive sync."""
    sync_script = os.path.join(PROJECT_ROOT, ".agents", "skills", "google-drive-sync", "scripts", "sync.py")
    creds_path = os.path.join(PROJECT_ROOT, ".agents", "skills", "google-drive-sync", "credentials.json")
    token_path = os.path.join(PROJECT_ROOT, ".agents", "skills", "google-drive-sync", "token.json")
    
    # Use console-stable python executable
    py_exe = get_python_exe()
    
    if headless:
        print(f"☁️  [Cloud Sync] Starting integrated GDrive sync (HEADLESS) for: {os.path.basename(local_dir)}")
        sync_cmd = [py_exe, sync_script, "--local-dir", local_dir, "--remote-id", remote_id, "--credentials", creds_path, "--token", token_path, "--wrap-folder", "--headless"]
        try:
            process = subprocess.Popen(sync_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in process.stdout or []:
                msg = line.strip()
                if "Progress:" in msg:
                    sys.stdout.write(f"\r   {msg}    ")
                    sys.stdout.flush()
                else:
                    print(f"   [Sync] {msg}", flush=True)
            process.wait()
            print(f"\n✅ [Cloud Sync] Completed for {os.path.basename(local_dir)}")
            return process.returncode == 0
        except Exception as e:
            print(f"⚠️  [Cloud Sync] Integrated sync failed: {e}")
            return False
    else:
        if sys.platform == "win32":
            final_cmd = (
                f"& '{py_exe}' '{sync_script}' --local-dir '{local_dir}' --remote-id '{remote_id}' "
                f"--credentials '{creds_path}' --token '{token_path}' --wrap-folder; "
                f"Write-Host '--- ✅ Sync task completed. Window will close in 5s ---' -ForegroundColor Green; "
                f"Start-Sleep 5"
            )
            ps_cmd = f"Start-Process powershell -ArgumentList '-NoExit', '-Command', \"{final_cmd}\""
            print(f"☁️  [Cloud Sync] Launching GDrive sync in a new window for: {os.path.basename(local_dir)}")
            try:
                subprocess.Popen(["powershell", "-Command", ps_cmd], creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                print(f"⚠️  [Cloud Sync] Failed to launch sync window: {e}")
        elif sys.platform == "darwin":
            print(f"☁️  [Cloud Sync] Launching GDrive sync in a new Terminal window for: {os.path.basename(local_dir)}")
            mac_cmd = (
                f"'{py_exe}' '{sync_script}' --local-dir '{local_dir}' --remote-id '{remote_id}' "
                f"--credentials '{creds_path}' --token '{token_path}' --wrap-folder; "
                f"echo '--- ✅ Sync task completed. Window will close in 5s ---'; "
                f"sleep 5; exit"
            )
            osascript_cmd = f'tell application "Terminal" to do script "{mac_cmd}"'
            try:
                subprocess.Popen(["osascript", "-e", osascript_cmd])
            except Exception as e:
                print(f"⚠️  [Cloud Sync] Failed to launch sync terminal: {e}")
        else:
            print(f"☁️  [Cloud Sync] Running sync in background (not supported in new window on this platform)")
            trigger_sync(local_dir, remote_id, headless=True)

# --- Bilibili Upload (canonical implementation; batch engine + GUI reuse this) ---
BILI_SCRIPT = "/Users/shanfu/.gemini/config/skills/bilibili-uploader/scripts/bili_upload.py"
BILI_PYTHON = "/Users/shanfu/.gemini/config/skills/bilibili-uploader/.venv/bin/python"
BILI_VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.ts')
WECHAT_SCRIPT = os.path.expanduser("~/.codex/skills/wechat-channel-uploader/scripts/wechat_upload.py")
WECHAT_MAX_UPLOAD_SECONDS = 600
SHARED_UPLOAD_TITLE_MAX_CHARS = 16
BILI_UPLOAD_TITLE_MAX_CHARS = 48
BILI_UPLOAD_TITLE_HARD_MAX_CHARS = 80
BILI_UPLOAD_TITLE_TARGET_MIN_CHARS = 24
_wechat_venv_python = os.path.join(
    CURRENT_DIR,
    ".venv",
    "Scripts" if sys.platform == "win32" else "bin",
    "python.exe" if sys.platform == "win32" else "python",
)
WECHAT_PYTHON = _wechat_venv_python if os.path.exists(_wechat_venv_python) else sys.executable

def find_hardsub_video(workdir):
    """Return the most recently modified *_hardsub* video in workdir, or None."""
    try:
        files = os.listdir(workdir)
    except Exception:
        return None
    candidates = [f for f in files if "_hardsub" in f and f.endswith(BILI_VIDEO_EXTS)]
    if not candidates:
        return None
    return os.path.join(workdir, max(candidates, key=lambda fn: os.path.getmtime(os.path.join(workdir, fn))))

def build_bili_title(hardsub_path):
    """Derive a Bilibili title from a hardsub filename, capped at 80 chars."""
    title = os.path.basename(hardsub_path).replace("_hardsub", "")
    title = os.path.splitext(title)[0]
    title = re.sub(r' \[[a-zA-Z0-9_-]+\](\s*\[1080p\])?$', '', title).strip()
    if len(title) > 80:
        cut = title.rfind(' ', 0, 77)
        if cut == -1:
            cut = 77
        title = title[:cut].rstrip().rstrip('.') + "..."
    return title


def sanitize_wechat_title(title, max_chars=16):
    """Clean a title to WeChat's short-title punctuation rules and length limit."""
    cleaned = str(title).replace(",", " ").replace("，", " ")
    cleaned = re.sub(
        r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff《》“”‘’：:+?%℃ ]",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or "未命名视频")[:max_chars]


def normalize_shared_upload_title(title, max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS):
    """Clean a shared title without silently truncating its meaning."""
    value = str(title or "").strip()
    if not value:
        return ""
    # Sanitize with the original length first so an over-limit title is
    # detected instead of being cut to 16 characters.
    cleaned = sanitize_wechat_title(value, max_chars=max(len(value), 1))
    if (
        not cleaned
        or cleaned == "未命名视频"
        or len(cleaned) > max_chars
        or _shared_title_has_quality_issue(cleaned)
    ):
        return ""
    return cleaned

def normalize_bili_upload_title(
    title,
    max_chars=BILI_UPLOAD_TITLE_HARD_MAX_CHARS,
):
    """Clean a Bilibili title without applying WeChat's punctuation rules."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    value = value.rstrip(" 。.！!？?；;，,")
    if (
        not value
        or len(value) > max_chars
        or _shared_title_has_quality_issue(value)
    ):
        return ""
    return value

def build_bili_cmd(
    workdir,
    title=None,
    tid=None,
    category_name=None,
    tags=None,
    season=None,
    desc=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    require_complete_metadata=False,
):
    """Build the Bilibili upload command for the hardsub video in workdir.
    Metadata (tid/tags/desc/season) is auto-generated per-video by bili_upload.py's
    built-in LLM logic unless explicitly overridden. Returns the cmd list or None."""
    if not os.path.exists(BILI_SCRIPT) or not os.path.exists(BILI_PYTHON):
        return None
    hardsub_path = find_hardsub_video(workdir)
    if not hardsub_path:
        return None
    metadata_values = (title, desc, cover_title)
    if require_complete_metadata and not all(
        str(value or "").strip() for value in metadata_values
    ):
        return None
    # When title/desc/cover_title are all provided, the caller has pre-generated
    # metadata; tell bili_upload.py to skip its own LLM title generation.
    _pre_generated = title is not None and desc is not None and cover_title is not None
    if title is None:
        title = build_bili_title(hardsub_path)
    cmd = [BILI_PYTHON, "-u", BILI_SCRIPT, hardsub_path, title]
    if tid:
        cmd.extend(["--tid", str(tid)])
    if category_name:
        cmd.extend(["--category_name", str(category_name)])
    if tags:
        cmd.extend(["--tags", str(tags)])
    if season:
        cmd.extend(["--season_name", str(season)])
    if desc:
        cmd.extend(["--desc", str(desc)])
    if cover_title is not None and str(cover_title).strip():
        cmd.extend(["--cover-title", str(cover_title).strip()])
    if _pre_generated:
        cmd.append("--no_auto_title")
    if llm_model:
        cmd.extend(["--llm-model", str(llm_model)])
    if llm_provider:
        cmd.extend(["--llm-provider", str(llm_provider)])
    if public:
        cmd.append("--public")
    return cmd

def run_bili_cmd(cmd, on_progress=None, on_log=None, abort_flag=None):
    """Run a pre-built Bilibili upload command. Returns True on success.
    on_progress(pct)/on_log(line)/abort_flag() are optional callbacks; final
    success/failure status is NOT logged here (callers decide how to report)."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        if on_log:
            on_log(f"❌ Failed to start Bilibili upload: {e}")
        return False
    if p.stdout:
        import queue as _queue
        import threading as _threading
        _line_q: _queue.Queue = _queue.Queue()
        _reader_stop = _threading.Event()

        def _stdout_reader():
            try:
                for line in p.stdout:
                    if _reader_stop.is_set(): break
                    _line_q.put(line)
            except Exception:
                pass
            finally:
                _line_q.put(None)

        _rt = _threading.Thread(target=_stdout_reader, daemon=True)
        _rt.start()

        while True:
            if abort_flag and abort_flag():
                p.kill()
                _reader_stop.set()
                break
            try:
                line = _line_q.get(timeout=1.0)
            except Exception:
                if p.poll() is not None:
                    while True:
                        try:
                            line = _line_q.get(timeout=0.5)
                        except Exception:
                            break
                        if line is None: break
                        stripped = line.strip()
                        if stripped and on_log:
                            on_log(stripped)
                        m = re.search(r'Progress:\s*([\d.]+)%', line)
                        if m and on_progress:
                            on_progress(float(m.group(1)))
                    break
                continue
            if line is None: break
            stripped = line.strip()
            if stripped and on_log:
                on_log(stripped)
            m = re.search(r'Progress:\s*([\d.]+)%', line)
            if m and on_progress:
                on_progress(float(m.group(1)))
    p.wait()
    return p.returncode == 0

def trigger_bili_upload(
    workdir,
    title=None,
    tid=None,
    category_name=None,
    tags=None,
    season=None,
    desc=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_progress=None,
    on_log=None,
    abort_flag=None,
):
    """Build and run a Bilibili upload for the hardsub video in workdir.
    Convenience wrapper: build_bili_cmd + run_bili_cmd. Returns True on success."""
    cmd = build_bili_cmd(
        workdir,
        title=title,
        tid=tid,
        category_name=category_name,
        tags=tags,
        season=season,
        desc=desc,
        public=public,
        cover_title=cover_title,
        llm_model=llm_model,
        llm_provider=llm_provider,
    )
    if not cmd:
        print("⚠️  [Bilibili] upload skipped (script/venv/hardsub not found)", flush=True)
        return False
    _log = on_log or (lambda msg: print(msg, flush=True))
    _log(f"📺 Starting Bilibili upload: {os.path.basename(cmd[2])}")
    ok = run_bili_cmd(
        cmd,
        on_progress=on_progress,
        on_log=_log,
        abort_flag=abort_flag,
    )
    _log(f"📺 {'✅ Success' if ok else '❌ Failed'}")
    return ok

# --- Shared Upload Metadata Generation ---
# BI and WX share one description and cover title. Their form titles are also
# generated from the same transcript: Bilibili gets the fuller editorial title
# first, then WeChat receives a 16-character title derived from it.
_COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)), "common")
if not os.path.isdir(_COMMON_DIR):
    _COMMON_DIR = "/Users/shanfu/cc/Library/Tools/common"
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)

def _load_llm_defaults():
    """Read the model/vendor selected by the main AutoSub configuration."""
    try:
        p = os.path.join(CURRENT_DIR, "settings.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            model = s.get("llm_model") or DEFAULTS.get("llm_model") or "glm-5.2"
            vendor = s.get("llm_vendor") or s.get("vendor") or ""
            provider = VENDOR_MAP.get(vendor, str(vendor).strip().lower() or "dashscope")
            return model, provider
    except Exception:
        pass
    return DEFAULTS.get("llm_model", "glm-5.2"), VENDOR_MAP.get(
        DEFAULTS.get("llm_vendor", ""), "dashscope"
    )


_SHARED_TITLE_BANNED_TERMS = (
    "它", "他们", "她们", "东西", "能力", "到底", "究竟",
    "为什么", "为何", "如何", "怎么", "怎样", "什么",
    "死活",
)
_SHARED_TITLE_ACTION_MARKERS = (
    "谈", "讲", "靠", "用", "做", "让", "成为", "押",
    "招", "卖", "买", "建立", "打造", "解决", "守住", "聚焦", "变成", "保护",
    "整合", "引领", "炮轰", "反对", "支持", "梳理", "展示",
    "被裁", "烂掉",
)
_SHARED_TITLE_CONTEXT_MARKERS = (
    "访谈", "对话", "谈", "讲", "为", "帮", "给", "客户", "企业",
    "问题", "不做", "不是", "不招", "押注", "才是", "隐私",
    "被裁", "烂掉",
)
_PUBLIC_TITLE_STYLE_EXAMPLES = (
    "Palantir才是引领应用层的公司，客户从没见过这种阵势",
    "ToB AI落地的正道何在？Palantir CEO Alex Karp炮轰模型大厂",
    "Alex Karp早年访谈：Palantir不做服务公司，为企业打造解决数据问题的产品",
)
_SHARED_TITLE_STANCE_MARKERS = (
    "不做", "不是", "没有", "拒绝", "不招", "不卖", "才是",
)
_SHARED_TITLE_CONCRETE_OBJECTS = (
    "数据", "应用", "平台", "工具", "模型", "算法", "本体",
)
_SHARED_TITLE_BENEFICIARIES = (
    "企业", "政企", "政府", "客户", "机构",
)


def _shared_title_has_quality_issue(title):
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if any(term in value for term in _SHARED_TITLE_BANNED_TERMS):
        return True
    if "把" in value:
        return True
    if "押注解决" in value:
        return True
    # "做成产品" is a valid predicate only when the title also names the
    # concrete capability or object being productized. Otherwise it is a vague
    # summary such as "Palantir为企业做成产品".
    if value.endswith("做成产品") and not any(
        term in value
        for term in ("数据", "应用", "平台", "工具", "方法论")
    ):
        return True
    # A shared form title must read as a sentence, not as a noun pile.
    if not any(marker in value for marker in _SHARED_TITLE_ACTION_MARKERS):
        return True
    # A grammatical verb alone does not make a title self-contained. Reject
    # compressed fragments such as "Palantir把数据整合成产品": they lack the
    # beneficiary, problem, interview context, or clear stance that lets the
    # sentence stand on its own.
    return not any(marker in value for marker in _SHARED_TITLE_CONTEXT_MARKERS)


def _shared_title_score(title, max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS):
    """Prefer complete titles that carry more than one substantive claim."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    score = 0.0
    score += 2.0 * sum(marker in value for marker in _SHARED_TITLE_ACTION_MARKERS)
    score += 2.0 * sum(marker in value for marker in _SHARED_TITLE_CONTEXT_MARKERS)
    score += 4.0 * sum(marker in value for marker in _SHARED_TITLE_CONCRETE_OBJECTS)
    score += 5.0 * sum(marker in value for marker in _SHARED_TITLE_STANCE_MARKERS)
    score += 2.0 * sum(marker in value for marker in _SHARED_TITLE_BENEFICIARIES)
    if max_chars >= BILI_UPLOAD_TITLE_MAX_CHARS:
        if 24 <= len(value) <= min(40, max_chars):
            score += 3.0
        elif len(value) < 20:
            score -= 2.0
    elif 14 <= len(value) <= 16:
        score += 1.0
    if value.endswith("产品") and not any(
        marker in value for marker in _SHARED_TITLE_CONCRETE_OBJECTS
    ):
        score -= 4.0
    if max_chars >= BILI_UPLOAD_TITLE_MAX_CHARS:
        if not any(marker in value for marker in _SHARED_TITLE_CONCRETE_OBJECTS):
            score -= 4.0
        for repeated_object in ("数据", "问题", "产品", "销售", "工程师"):
            occurrences = value.count(repeated_object)
            if occurrences > 1:
                score -= 3.0 * (occurrences - 1)
    return score


_TITLE_LATIN_STOPWORDS = {
    "a", "an", "and", "as", "at", "for", "in", "of", "on",
    "the", "to", "with",
}
_TITLE_CJK_FUNCTION_CHARS = set("的了着是在和与把被给对向就都很也又再")


def _title_scoring_tokens(value):
    """Extract stable reference tokens from a mixed Chinese/English title."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9_-]*", normalized)
        if token not in _TITLE_LATIN_STOPWORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        compact = "".join(
            char for char in run if char not in _TITLE_CJK_FUNCTION_CHARS
        )
        if len(compact) == 1:
            tokens.add(compact)
        tokens.update(
            compact[index:index + 2]
            for index in range(len(compact) - 1)
        )
    return tokens


def _title_reference_coverage(title, reference):
    """Measure how much of a candidate is covered by a reference text."""
    title_tokens = _title_scoring_tokens(title)
    if not title_tokens:
        return 0.0
    reference_tokens = _title_scoring_tokens(reference)
    if not reference_tokens:
        return 0.0
    return len(title_tokens & reference_tokens) / len(title_tokens)


def _title_candidate_score(
    title,
    max_chars,
    central_claim="",
    description="",
    source_title="",
):
    """Combine editorial structure with semantic alignment to the video."""
    score = _shared_title_score(title, max_chars=max_chars)
    if central_claim:
        score += 6.0 * _title_reference_coverage(title, central_claim)
    if description:
        first_sentence = re.split(
            r"[。！？\n]",
            str(description).strip(),
            maxsplit=1,
        )[0]
        score += 5.0 * _title_reference_coverage(title, first_sentence)
        score += 2.0 * _title_reference_coverage(title, description)
    if source_title:
        # The publisher title anchors the topic, but cannot outweigh the
        # transcript-derived description and central claim.
        score += 1.5 * _title_reference_coverage(title, source_title)
    return score


def _cover_title_repeats_form_title(title, form_title):
    """Reject generated covers that merely restate the form title."""
    normalize = lambda value: re.sub(r"\s+", "", str(value or ""))
    return bool(form_title) and normalize(title) == normalize(form_title)


def _cover_title_has_quality_issue(title):
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if not value:
        return True
    try:
        from subtitle_title import _title_has_quality_issue
        if _title_has_quality_issue(value, allow_first_person=True):
            return True
    except ImportError:
        pass
    if re.match(r"^(?:就像|好像|仿佛|在|需要|带着|做多|做空)", value):
        return True
    predicate_markers = (
        "是", "像", "就像", "不是", "没有", "开", "做", "看", "说", "谈",
        "解决", "变成", "成为", "却", "并", "让", "把", "给", "在",
        "对付", "阻止", "提供", "押注", "坚持", "拒绝",
        "没了", "断了", "烂掉",
    )
    return not any(marker in value for marker in predicate_markers)


def _normalize_cover_simile_title(title, full_subtitle_text):
    """Restore the simile marker when a model over-compresses a scene hook."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    simile_match = re.search(
        r"就有点像(.*)",
        str(full_subtitle_text or ""),
    )
    if not simile_match or not value.startswith("我们"):
        return value

    source_clause = simile_match.group(1)
    body = value[len("我们"):]
    body = re.sub(r"^(?:就像|像)", "", body)
    if re.search(r"带着(?:一帮)?工程师", source_clause):
        body = re.sub(r"带着(?:一帮)?工程师", "", body)
    if "正中央" in source_clause and "正中央" not in body:
        body = re.sub(
            r"(在[^，。 ]{1,16})(开)",
            r"\1正中央\2",
            body,
        )

    source_chars = set(re.findall(r"[\u3400-\u9fff]", source_clause))
    body_chars = set(re.findall(r"[\u3400-\u9fff]", body))
    if len(source_chars & body_chars) < 2:
        return value
    return "我们就像" + body


def _generate_title_from_description(
    description,
    subtitle_text,
    full_subtitle_text,
    llm_client,
    model_name,
    max_chars,
    provider=None,
    source_title=None,
):
    """Summarize the whole interview into a fuller Bilibili form title."""
    from subtitle_title import (
        parse_llm_title_candidates,
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not description or not subtitle_text or not full_subtitle_text or llm_client is None:
        return None

    min_chars = min(BILI_UPLOAD_TITLE_TARGET_MIN_CHARS, max_chars)
    source_title_reference = ""
    if source_title:
        source_title_reference = (
            "视频原标题是作者或平台给出的题材参考，不是唯一事实来源。"
            "先用它判断视频场景和受益者，再通读字幕确认实际主线。"
            "如果字幕反复聚焦另一个主体，标题应优先反映字幕主线，"
            "同时保留原标题中有用的场景或受益者。"
            "不要逐字翻译原标题，也不要只抽原标题里的一个词。"
            f"\n视频原标题：\n{source_title}\n\n"
        )
    base_prompt = (
        "基于完整中文字幕和视频简介，生成B站表单标题。"
        f"标题长度必须为 {min_chars}-{max_chars} 个字符，英文字母逐字符计数。"
        "优先 24-40 个字符，只有信息不足时才低于 24 个字符。"
        "输出前必须逐字重数一遍；超过上限的候选直接作废。"
        "B站标题没有微信的 16 字限制，要表达得更充分："
        "通常覆盖 3 个实质信息点，例如访谈主题、核心业务/做法、"
        "明确立场或结果；不要为了短而删成分类标签。"
        "表单标题禁用「把」字句；"
        "用「主体+为+受益者+动作+对象」或「主体+解决+对象+问题」表达完整谓语。"
        "先在内部完成三步：列出访谈的 3-5 个主要内容；"
        "判断哪条线索贯穿全片；再写成一句自然短句。"
        "参考下面公开B站标题的结构，只学句式，不复制事实或短语："
        + "；".join(_PUBLIC_TITLE_STYLE_EXAMPLES)
        + "。"
        "标题必须概括谈话主题、被访者核心做法或结论，"
        "优先覆盖谈话主题、被访者核心做法和明确立场/结果；"
        "其中至少一个必须是核心业务、产品或具体做法。"
        "组织理念只能作为第二信息点，不能替代核心业务。"
        "如果某个信息点会让句子变啰嗦，保留核心业务和最有力的立场/结果。"
        "优先使用「主体+字幕中真实存在的动作或判断+具体对象+受益者/问题/结果」这类自然句式。"
        "核心业务优先写成具体动作和对象，例如「整合数据」；"
        "不要只写「解决数据隐私问题」这类结果而漏掉核心做法。"
        "候选必须能独立读成一句完整陈述：读者要知道谁做了什么、为谁或解决什么，"
        "或者知道主体明确坚持/反对什么。"
        "禁止只有「主体+动作+对象」的压缩片段，"
        "例如不得输出「Palantir把数据整合成产品」这类没有受益者、问题或立场的标题。"
        "动作和宾语按中文习惯排列，不要用「把」字句，也不要名词化成短语拼接。"
        "不要为了句式新增字幕没有的动词，包括「谈」。"
        "如果字幕没有出现人名，就用公司、产品或字幕中的具体主体开头。"
        "不要输出名词短语拼接，不要机械堆关键词，也不要只抽一个短语。"
        "不要为了凑长度新增字幕没有的连接短语。"
        "如果信息过多，优先保留主体、主线对象、核心做法和结论，删除修饰词。"
        "不要把全片主体缩小成单一客户群体；若同时服务企业和政府，"
        "用「客户」或自然的并列表达，不要用「大型企业」这类限定词缩窄范围。"
        "不要把动词硬放在核心对象前面造成翻译腔。"
        "多个分句不要重复同一个核心宾语或谓语，"
        "例如不要同时写「解决重要问题」和「解决问题」。"
        "「押注」后面接自然宾语，不要输出「押注解决」这类生硬连用。"
        "不要疑问句、悬念句、代词、泛词或营销口吻。"
        "不要市井口语和情绪副词，例如「死活」「简直」。"
        "标题中的事实和术语必须来自字幕。"
        "生成 5 个候选，按编辑质量从高到低排列。"
        "每个候选给出 1-2 条 evidence，必须逐字复制字幕原文，不得改写或加省略号。"
        "只输出 JSON："
        '{"central_claim":"...","candidates":['
        '{"title":"...","evidence":["..."]}]}\n\n'
        f"{source_title_reference}"
        f"视频简介：\n{description}\n\n"
        f"完整中文字幕：\n{subtitle_text}"
    )
    last_error = None
    validation_feedback = ""
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮标题未通过字幕溯源校验：{validation_feedback}\n"
                "请只修正标题措辞和 evidence，保持全片概括不变。"
            )
        try:
            raw = llm_client.generate_content(
                prompt, model_name=model_name, provider=provider
            )
            _central_claim, candidates = parse_llm_title_candidates(raw)
            candidate_errors = []
            valid_candidates = []
            for title, evidence in candidates:
                title = re.sub(r"\s+", " ", str(title or "")).strip()
                if len(title) < min_chars:
                    candidate_errors.append(f"{title}: 标题少于 {min_chars} 字")
                    continue
                if len(title) > max_chars:
                    candidate_errors.append(f"{title}: 标题超过 {max_chars} 字")
                    continue
                if _shared_title_has_quality_issue(title):
                    candidate_errors.append(f"{title}: 标题不是自然完整短句")
                    continue
                try:
                    validate_title_from_subtitle(
                        title,
                        subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                    valid_candidates.append((title, evidence))
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                return max(
                    valid_candidates,
                    key=lambda item: _title_candidate_score(
                        item[0],
                        max_chars,
                        central_claim=_central_claim,
                        description=description,
                        source_title=source_title,
                    ),
                )[0]
            if candidate_errors:
                raise SubtitleTitleError("；".join(candidate_errors[:3]))
            raise SubtitleTitleError("未返回可校验标题候选")
        except SubtitleTitleError as exc:
            last_error = exc
            validation_feedback = str(exc)
    structured_title = _generate_structured_title_from_description(
        description,
        subtitle_text,
        llm_client,
        model_name,
        max_chars,
        source_title=source_title,
    )
    if structured_title:
        return structured_title
    if last_error is not None:
        raise last_error
    raise SubtitleTitleError("简介压缩标题生成失败")


def _generate_structured_title_from_description(
    description,
    subtitle_text,
    llm_client,
    model_name,
    max_chars,
    source_title=None,
):
    """Build a budgeted Bilibili title from semantic fields."""
    from subtitle_title import SubtitleTitleError, validate_title_from_subtitle

    if not description or not subtitle_text or llm_client is None:
        return None

    min_chars = min(BILI_UPLOAD_TITLE_TARGET_MIN_CHARS, max_chars)
    source_title_reference = ""
    if source_title:
        source_title_reference = (
            "视频原标题只作题材和场景参考，不是唯一事实来源。"
            "若字幕主线与原标题侧重点不同，以字幕主线为主。"
            f"\n视频原标题：\n{source_title}\n\n"
        )
    base_prompt = (
        "把视频简介和完整中文字幕概括成B站表单标题。"
        "输出5个结构化候选，按全片概括强度排序。"
        "字段：subject、beneficiary、action、object、second_claim、evidence。"
        "subject 来自字幕或简介；beneficiary 尽量2字；action 2字；object 2-4字。"
        "second_claim 是可选的明确立场或结果，尽量4-8字；"
        "只有字幕能支撑时才填写。"
        "只要字幕或简介能识别受益者，beneficiary 不得为空；"
        "若存在更具体受益者，不得用「客户」泛化。"
        "本地会拼接为「subject+为+beneficiary+action+object」，"
        "有 second_claim 时追加「，second_claim」。"
        f"完整标题必须为 {min_chars}-{max_chars} 个字符，英文字母逐字符计数。"
        "第一候选必须覆盖核心业务、产品或具体做法，"
        "object 必须是具体核心对象，例如数据、应用、平台或工具；"
        "不得用「产品」「价值」「问题」这类泛词充当 object。"
        "不要只选组织理念、单个案例或一句钩子。"
        "不要把全片主体缩小成单一客户群体；若同时服务企业和政府，"
        "beneficiary 用「客户」或自然的并列表达。"
        "不要市井口语和情绪副词，例如「死活」「简直」。"
        "参考公开B站标题的结构，只学句式，不复制事实。"
        "每个候选给1-2条 evidence，必须逐字复制中文字幕原文。"
        "只输出 JSON："
        '{"candidates":[{"subject":"...","beneficiary":"...",'
        '"action":"...","object":"...","second_claim":"...",'
        '"evidence":["..."]}]}\n\n'
        f"{source_title_reference}"
        f"视频简介：\n{description}\n\n"
        f"完整中文字幕：\n{subtitle_text}"
    )

    validation_feedback = ""
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮候选未通过校验：{validation_feedback}\n"
                "请删除修饰语或第二信息点，不要换成同义长词。"
            )
        try:
            raw = llm_client.generate_content(prompt, model_name=model_name)
            text = str(raw or "").strip()
            text = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                raise SubtitleTitleError("结构化标题输出不是 JSON 对象")
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise SubtitleTitleError("结构化标题 JSON 解析失败") from exc
            if not isinstance(payload, dict):
                raise SubtitleTitleError("结构化标题 JSON 结构不正确")

            raw_candidates = payload.get("candidates")
            if not isinstance(raw_candidates, list) or not raw_candidates:
                raise SubtitleTitleError("结构化标题未返回候选")

            candidate_errors = []
            valid_candidates = []
            for item in raw_candidates:
                if not isinstance(item, dict):
                    continue
                subject = re.sub(
                    r"\s+", "", str(item.get("subject", ""))
                )
                beneficiary = re.sub(
                    r"\s+", "", str(item.get("beneficiary", ""))
                )
                action = re.sub(r"\s+", "", str(item.get("action", "")))
                object_value = re.sub(
                    r"\s+", "", str(item.get("object", ""))
                )
                second_claim = re.sub(
                    r"\s+", "", str(item.get("second_claim", ""))
                )
                raw_evidence = item.get("evidence", [])
                if isinstance(raw_evidence, str):
                    raw_evidence = [raw_evidence]
                elif not isinstance(raw_evidence, list):
                    raw_evidence = []
                evidence = tuple(
                    str(value).strip()
                    for value in raw_evidence
                    if str(value).strip()
                )
                if not subject or not action or not object_value or not evidence:
                    continue

                title = subject
                if beneficiary:
                    title += "为" + beneficiary
                title += action + object_value
                if second_claim:
                    title += "，" + second_claim
                title = re.sub(r"\s+", " ", title).strip()
                if len(title) < min_chars:
                    candidate_errors.append(f"{title}: 标题少于 {min_chars} 字")
                    continue
                if len(title) > max_chars:
                    candidate_errors.append(f"{title}: 标题超过 {max_chars} 字")
                    continue
                if _shared_title_has_quality_issue(title):
                    candidate_errors.append(f"{title}: 标题不是自然完整短句")
                    continue
                try:
                    validate_title_from_subtitle(
                        title,
                        subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                    valid_candidates.append(title)
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                return max(
                    valid_candidates,
                    key=lambda title: _title_candidate_score(
                        title,
                        max_chars,
                        description=description,
                        source_title=source_title,
                    ),
                )
            if candidate_errors:
                raise SubtitleTitleError("；".join(candidate_errors[:3]))
            raise SubtitleTitleError("未返回可校验结构化标题候选")
        except SubtitleTitleError as exc:
            validation_feedback = str(exc)
        except Exception as exc:
            validation_feedback = str(exc)
    raise SubtitleTitleError("结构化共享标题生成失败")


def _extract_bili_title_subject(bili_title):
    """Return the leading subject that a WeChat title should preserve."""
    value = re.sub(r"\s+", " ", str(bili_title or "")).strip()
    if not value:
        return ""

    markers = (
        _SHARED_TITLE_ACTION_MARKERS
        + _SHARED_TITLE_STANCE_MARKERS
        + ("为", "访谈", "对话", "没有")
    )
    positions = [
        position
        for marker in markers
        if marker and marker in value
        for position in (value.find(marker),)
        if position > 0
    ]
    if not positions:
        return ""

    subject = value[:min(positions)]
    subject = subject.strip(" ：:，,。.？?；;！!")
    return subject


def _wechat_title_preserves_bili_subject(title, bili_title, max_chars):
    """Check whether a compressed title keeps the Bilibili title's subject."""
    subject = _extract_bili_title_subject(bili_title)
    if not subject or len(subject) > max_chars - 4:
        return True
    return subject in str(title or "")


def _compress_bili_title_for_wechat(bili_title, max_chars, description=""):
    """Locally shorten a validated Bilibili title when model counting fails."""
    value = re.sub(r"\s+", " ", str(bili_title or "")).strip()
    subject = _extract_bili_title_subject(value)
    clauses = [clause.strip() for clause in re.split(r"[，,；;。]", value) if clause.strip()]

    source_text = value + "\n" + str(description or "")
    action = next(
        (
            marker
            for marker in ("整合", "保护", "解决", "打造", "建立")
            if marker in source_text
        ),
        "",
    )
    concrete_object = next(
        (
            marker
            for marker in _SHARED_TITLE_CONCRETE_OBJECTS
            if marker in source_text
        ),
        "",
    )
    stance_match = re.search(
        r"(不招销售|不做服务|没有销售团队|不招市场|没有市场部门)",
        source_text,
    )
    if subject and action and concrete_object and stance_match:
        combined = subject + action + concrete_object + stance_match.group(1)
        combined = sanitize_wechat_title(
            combined,
            max_chars=max(len(combined), 1),
        )
        if (
            len(combined) <= max_chars
            and not _shared_title_has_quality_issue(combined)
            and _wechat_title_preserves_bili_subject(combined, value, max_chars)
        ):
            return combined

    for clause in clauses:
        body = clause[len(subject):] if subject and clause.startswith(subject) else clause
        without_modifiers = body
        for modifier in ("死活", "依然", "尽量", "所谓", "基本上", "散落", "各类", "底层"):
            without_modifiers = without_modifiers.replace(modifier, "")
        without_conjoined_object = re.sub(
            r"(?:与|和)[\u3400-\u9fff]{1,4}(?=团队|业务|问题|公司)",
            "",
            body,
        )
        without_beneficiary = re.sub(
            r"^为.{1,10}?(?=解决|整合|保护|打造|建立)",
            "",
            body,
        )
        without_bet_prefix = re.sub(
            r"^押注.{1,8}?(?=不招|不做|没有)",
            "",
            body,
        )
        compressed = re.sub(
            r"(?:与|和)[\u3400-\u9fff]{1,4}(?=团队|业务|问题|公司)",
            "",
            without_modifiers,
        )
        for candidate_body in (
            body,
            without_modifiers,
            without_conjoined_object,
            without_beneficiary,
            without_bet_prefix,
            compressed,
            compressed.replace("核心", ""),
            compressed.replace("团队", ""),
        ):
            candidate = subject + candidate_body if subject else candidate_body
            candidate = sanitize_wechat_title(
                candidate,
                max_chars=max(len(candidate), 1),
            )
            if (
                len(candidate) <= max_chars
                and not _shared_title_has_quality_issue(candidate)
                and _wechat_title_preserves_bili_subject(
                    candidate,
                    value,
                    max_chars,
                )
            ):
                return candidate
    return None


def _derive_wechat_title_from_bili_title(
    bili_title,
    description,
    subtitle_text,
    llm_client,
    model_name,
    max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
    provider=None,
    source_title=None,
):
    """Choose a transcript-grounded hook within the Bilibili title's topic."""
    from subtitle_title import (
        parse_llm_title_candidates,
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not bili_title or not description or not subtitle_text or llm_client is None:
        return None

    min_chars = min(12, max_chars)
    source_title_reference = ""
    if source_title:
        source_title_reference = (
            "视频原标题只用于校验题材和场景，不得取代B站标题的中心主张。"
            f"\n视频原标题：\n{source_title}\n\n"
        )
    base_prompt = (
        "为视频号生成更有吸引力的表单标题。"
        "B站标题只确定主题边界和必须保留的主体，不是逐字压缩模板。"
        "优先通读字幕，找出同一主线里最具体的冲突、后果、反差或人物立场；"
        "可以选字幕钩子，不得另起主题，也不得换成字幕外的概括。"
        f"标题长度必须为 {min_chars}-{max_chars} 个字符，英文字母逐字符计数。"
        "例如 Palantir 为 8 字符，中文最多还能放 8 个字。"
        "必须优先原样保留B站标题开头的主体，例如 Palantir、卡普或 OpenAI研究员；"
        "只有主体过长导致无法保留核心谓语时才允许省略。"
        "先计算主体字符数，再用标题上限减去主体字符数得到剩余额度；"
        "每个候选的其余文字都必须落在这个额度内，超限候选直接作废。"
        "优先保留主体和最有力的后果或立场；"
        "长度允许时再保留核心业务/做法或受益者；"
        "不要只留「主体+核心对象」的单信息点标题。"
        "禁止只留「主体+对象」或「主体+动作+对象」的分类标签。"
        "16字允许时优先输出「主体+具体后果/反差」或"
        "「主体+核心动作+对象+最有力的立场」，"
        "例如主体后接「被裁后组织会烂掉」或「整合数据不招销售」。"
        "禁用「把」字句，输出必须能独立读成一句完整陈述。"
        "不要为了凑满 16 字新增字幕没有的连接短语。"
        "生成 5 个候选，按编辑质量从高到低排列。"
        "每个候选给出 1-2 条 evidence，必须逐字复制中文字幕原文。"
        "只输出 JSON："
        '{"central_claim":"...","candidates":['
        '{"title":"...","evidence":["..."]}]}\n\n'
        f"{source_title_reference}"
        f"B站表单标题：\n{bili_title}\n\n"
        f"视频简介：\n{description}\n\n"
        f"完整中文字幕：\n{subtitle_text}"
    )

    last_error = None
    validation_feedback = ""
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮视频号标题未通过字幕溯源校验：{validation_feedback}\n"
                "请只修正措辞和 evidence，保持B站标题的核心主张不变。"
            )
        try:
            raw = llm_client.generate_content(
                prompt, model_name=model_name, provider=provider
            )
            _central_claim, candidates = parse_llm_title_candidates(raw)
            candidate_errors = []
            valid_candidates = []
            for title, evidence in candidates:
                title = sanitize_wechat_title(
                    title,
                    max_chars=max(len(str(title)), 1),
                )
                if len(title) < min_chars:
                    candidate_errors.append(f"{title}: 标题少于 {min_chars} 字")
                    continue
                if len(title) > max_chars:
                    candidate_errors.append(f"{title}: 标题超过 {max_chars} 字")
                    continue
                if _shared_title_has_quality_issue(title):
                    candidate_errors.append(f"{title}: 标题不是自然完整短句")
                    continue
                if not _wechat_title_preserves_bili_subject(
                    title,
                    bili_title,
                    max_chars,
                ):
                    candidate_errors.append(
                        f"{title}: 未保留B站标题主体 {_extract_bili_title_subject(bili_title)}"
                    )
                    continue
                try:
                    validate_title_from_subtitle(
                        title,
                        subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                    valid_candidates.append((title, evidence))
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                subject = _extract_bili_title_subject(bili_title)
                return max(
                    valid_candidates,
                    key=lambda item: (
                        _shared_title_score(item[0], max_chars)
                        + (6.0 if subject and subject in item[0] else 0.0)
                        + 8.0 * _title_reference_coverage(item[0], bili_title)
                        + 1.5 * _title_reference_coverage(item[0], source_title)
                    ),
                )[0]
            if candidate_errors:
                raise SubtitleTitleError("；".join(candidate_errors[:3]))
            raise SubtitleTitleError("未返回可校验视频号标题候选")
        except SubtitleTitleError as exc:
            last_error = exc
            validation_feedback = str(exc)
    if last_error is not None:
        fallback_title = _compress_bili_title_for_wechat(
            bili_title,
            max_chars,
            description=description,
        )
        if fallback_title:
            return fallback_title
        raise last_error
    fallback_title = _compress_bili_title_for_wechat(
        bili_title,
        max_chars,
        description=description,
    )
    if fallback_title:
        return fallback_title
    raise SubtitleTitleError("视频号标题压缩失败")


def _generate_cover_title_from_full_subtitle(
    full_subtitle_text,
    llm_client,
    model_name,
    min_chars=12,
    max_chars=48,
    provider=None,
    form_title=None,
    source_title=None,
):
    """Generate a cover headline by scanning the complete subtitle transcript."""
    from subtitle_title import (
        parse_llm_title_candidates,
        SubtitleTitleError,
        validate_title_from_subtitle,
    )

    if not full_subtitle_text or llm_client is None:
        return None

    title_context = ""
    if form_title or source_title:
        title_context = (
            "封面标题不是表单标题的压缩版。表单标题只用于判断主题边界和兜底，"
            "不得作为封面标题的主要模板。请从完整字幕中选一个具体原话或克制复述，"
            "让它与表单标题呼应同一主题，但呈现更具体的钩子、画面或冲突。"
        )
        if form_title:
            title_context += f"\nB站表单标题：\n{form_title}\n"
        if source_title:
            title_context += (
                "\n视频原标题（题材参考，不是唯一事实来源）：\n"
                f"{source_title}\n"
            )
        title_context += "\n"

    base_prompt = (
        "你是资深中文科技内容编辑。请通读下面的完整中文字幕，"
        "不要只看开头、结尾或采样片段。"
        "先在内部列出 5-8 个有潜力的原话钩子，按话题感、画面感、"
        "反常识程度和对全片主题的代表性排序，再生成封面标题候选。"
        "封面标题必须是语义完整的句子，可以比表单标题更具体，不必刻意压短。"
        "主语、谓语和比喻/冲突对象要保留，目标形态例如"
        "「我们就像在帕罗奥图正中央开马戏团」。"
        "候选必须以具体主体或完整分句主语开头，"
        "不得以「就像」「在」「需要」「带着」这类无主语成分开头。"
        "标题长度优先 18-32 个字符；超过 32 字时先删除修饰性短语，"
        "保留主语、谓语、比喻核心和地点，不得保留「带着一帮工程师」这类可删修饰。"
        "「正中央」这类承担画面强度的定位词必须保留。"
        "这个例子只学结构，不得把示例事实带入当前字幕。"
        "优先选具体画面、比喻、冲突或人物立场强烈的原话；"
        "不要把普通商务反差或抽象结论当成最强钩子。"
        "前 3 个候选必须按编辑判断从强到弱排列，"
        "优先使用原话或最小语法整理；"
        "原句太长或翻译腔重时，后 3 个候选可以做克制复述，"
        "复述时只删除真正的修饰语，不得删除主语、谓语或比喻核心；"
        "「我们」「的」「了」若承担语法或语境作用必须保留。"
        "字幕说「就有点像」时，标题必须压缩成「就像」，不得只写「像」。"
        "不得输出无语境的反常识短语或名词短语拼接。"
        "复述要保留原句的具体对象和语气，不得引入字幕外事实，"
        "不得使用市井口语、夸张营销或AI套话。"
        "标题长度 12-48 个字符，优先 18-32 个字符。"
        "每个候选给出 1-2 条 evidence，必须逐字复制字幕原文。"
        "只输出 JSON："
        '{"central_claim":"...","candidates":['
        '{"title":"...","evidence":["..."]}]}'
        f"\n{title_context}"
        f"\n\n完整字幕：\n{full_subtitle_text}"
    )

    last_error = None
    validation_feedback = ""
    for _ in range(2):
        prompt = base_prompt
        if validation_feedback:
            prompt += (
                f"\n上一轮候选未通过字幕溯源校验：{validation_feedback}\n"
                "请只修正标题措辞和 evidence，保持钩子不变。"
            )
        try:
            raw = llm_client.generate_content(
                prompt, model_name=model_name, provider=provider
            )
            _central_claim, candidates = parse_llm_title_candidates(raw)
            valid_candidates = []
            candidate_errors = []
            for title, evidence in candidates:
                try:
                    title = _normalize_cover_simile_title(title, full_subtitle_text)
                    if _cover_title_has_quality_issue(title):
                        raise SubtitleTitleError("封面标题不是完整短句")
                    if _cover_title_repeats_form_title(title, form_title):
                        raise SubtitleTitleError("封面标题复写表单标题")
                    validate_title_from_subtitle(
                        title,
                        full_subtitle_text,
                        evidence,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                    valid_candidates.append((title, evidence))
                except SubtitleTitleError as exc:
                    candidate_errors.append(f"{title}: {exc}")
            if valid_candidates:
                compact_subtitle = re.sub(r"\s+", "", full_subtitle_text)
                def _cover_score(indexed_item):
                    index, item = indexed_item
                    title = item[0]
                    compact_title = re.sub(r"\s+", "", title)
                    score = 0.0
                    score += 0.5 * _title_reference_coverage(title, source_title)
                    # The model orders its strongest hooks first. Local scoring
                    # only corrects obvious filler and abstract-business bias.
                    score -= index * 0.25
                    if compact_title in compact_subtitle:
                        score += 2.0
                    if title.startswith("我们"):
                        score += 1.0
                    if any(marker in title for marker in ("就像", "像")):
                        score += 2.0
                    if 18 <= len(title) <= 32:
                        score += 3.0
                    elif 14 <= len(title) <= 36:
                        score += 1.0
                    # A concrete scene or metaphor is the cover's strongest
                    # hook. Ordinary business contrasts remain valid candidates
                    # but should not outrank a grounded visual sentence.
                    scene_markers = (
                        "马戏团", "帕罗奥图", "工程师", "西装", "被车撞",
                        "战争", "武器", "核弹", "恐怖", "袭击", "网络攻击",
                        "欺诈", "算法", "人脑", "大脑", "深水区", "警局",
                        "医院", "前线", "病房", "临床",
                    )
                    if any(marker in title for marker in scene_markers):
                        score += 6.0
                    business_markers = (
                        "销售团队", "商业模式", "收入", "增长", "估值",
                        "市场份额",
                    )
                    if any(marker in title for marker in business_markers):
                        score -= 5.0
                    return score
                return max(enumerate(valid_candidates), key=_cover_score)[1][0]
            last_error = "；".join(candidate_errors[:3])
            validation_feedback = last_error or "未返回可校验候选"
        except SubtitleTitleError as exc:
            last_error = exc
            validation_feedback = str(exc)
        except Exception as exc:
            validation_feedback = str(exc)
    if last_error is not None:
        if isinstance(last_error, SubtitleTitleError):
            raise last_error
        raise SubtitleTitleError(str(last_error))
    raise SubtitleTitleError("封面标题生成失败")


def _extract_chinese_subtitle_lines(full_subtitle_text):
    """Return every Chinese subtitle line, preserving transcript order."""
    lines = [
        line.strip()
        for line in str(full_subtitle_text or "").splitlines()
        if line.strip() and re.search(r"[\u3400-\u9fff]", line)
    ]
    return "\n".join(lines)


_PROPER_NOUN_ASR_CORRECTIONS = {
    "Ermann": "Ermon",
    "Walkwest": "Wahlquist",
}


def _correct_known_asr_proper_nouns(value):
    """Apply deterministic ASR corrections before metadata generation."""
    corrected = str(value or "")
    for wrong, right in _PROPER_NOUN_ASR_CORRECTIONS.items():
        corrected = re.sub(
            rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])",
            right,
            corrected,
        )
    return corrected


def _metadata_text_issues(text, subtitle_text, video_path):
    """Check generated metadata against subtitle and filename proper nouns."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ["description is empty"]
    if any(fragment in value for fragment in ("快么服务", "集成么服务", "么服务")):
        return ["description contains a truncated service fragment"]
    for wrong in _PROPER_NOUN_ASR_CORRECTIONS:
        if re.search(rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])", value):
            return [f"known ASR proper noun misspelling: {wrong}"]

    allowed_text = (
        re.sub(r"\s+", " ", str(subtitle_text or ""))
        + " "
        + os.path.basename(str(video_path or ""))
    ).casefold()
    allowed_text = re.sub(r"[^a-z0-9'\- ]+", " ", allowed_text)
    allowed_tokens = set(allowed_text.split())

    # A Chinese summary should only introduce English tokens that are proper
    # nouns. Common sentence words are ignored so an English fallback summary
    # is not rejected merely for grammar words.
    ignored = {"a", "an", "and", "as", "at", "for", "in", "of", "on", "the", "to", "with"}
    tokens = re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9'-]*(?![A-Za-z0-9])", value)
    unsupported = []
    for token in tokens:
        normalized = token.casefold().strip("'-")
        if not normalized or normalized in ignored:
            continue
        if normalized not in allowed_tokens:
            unsupported.append(token)
    return [f"unsupported proper noun: {token}" for token in unsupported]


def _extract_source_title(video_path):
    """Recover the publisher title from an info file or the video filename."""
    path = str(video_path or "")
    directory = os.path.dirname(path) or "."
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"_hardsub$", "", base)

    info_candidates = [os.path.join(directory, base + ".info.json")]
    info_candidates.extend(glob.glob(os.path.join(directory, "*.info.json")))
    for info_path in info_candidates:
        try:
            with open(info_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            title = str(data.get("title") or "").strip()
            normalized_title = re.sub(r"[\s_\-]+", " ", title).strip().casefold()
            if title and normalized_title not in {
                "video", "hardsub", "video hardsub", "output", "untitled",
            }:
                return title
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    # Downloaded names normally end with the video id and quality labels.
    for _ in range(2):
        base = re.sub(r"\s*\[[^\]]+\]\s*$", "", base).strip()
    base = base.replace("_", " ")
    value = re.sub(r"\s+", " ", base).strip()
    normalized = value.casefold()
    return "" if normalized in {
        "video", "hardsub", "video hardsub", "output", "untitled",
    } else value


def _fallback_bili_title_from_source_title(source_title, max_chars):
    """Use the publisher title when model and subtitle titles both fail."""
    value = re.sub(r"\s+", " ", str(source_title or "")).strip()
    if not value:
        return ""
    if len(value) > max_chars:
        cut = value.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        value = value[:cut].rstrip(" .,:;!?")
    return value


def _fallback_wechat_title_from_source_title(source_title, max_chars):
    """Keep a word-boundary prefix of the publisher title for WeChat."""
    value = _fallback_bili_title_from_source_title(source_title, max_chars)
    return sanitize_wechat_title(value, max_chars=max_chars)


_SUBTITLE_HOOK_MARKERS = (
    "穿针引线", "结缔组织", "筋膜", "烂掉", "裁掉", "人格分裂",
    "深水区", "马戏团", "正中央", "没有销售", "不招销售", "不做服务",
    "一半", "断了", "没了",
)
_GENERIC_TITLE_MARKERS = (
    "帮助", "贡献", "展现价值", "时代", "生态", "可靠", "赋能",
    "打造", "构建", "保障", "创新", "从业者",
)


def _clean_subtitle_hook_line(line):
    """Turn one subtitle line into a headline-shaped local fallback."""
    value = re.sub(r"\s+", " ", str(line or "")).strip()
    value = value.strip("“”\"' 　")
    value = re.sub(r"^(?:嗯|哦|对|就是)[，,：: ]+", "", value)
    return value.rstrip(" 。.！!？?；;，,、")


def _subtitle_hook_score(value):
    """Prefer concrete images and consequences over generic value claims."""
    score = 0.0
    score += 16.0 * sum(
        marker in value
        for marker in ("结缔组织", "筋膜", "马戏团", "正中央", "深水区")
    )
    score += 8.0 * sum(
        marker in value
        for marker in _SUBTITLE_HOOK_MARKERS
    )
    score += 4.0 * sum(marker in value for marker in ("被", "没了", "断了"))
    score -= 10.0 * sum(marker in value for marker in _GENERIC_TITLE_MARKERS)
    if re.search(r"[?？]$", value):
        score -= 8.0
    if len(value) < 12:
        score -= 4.0
    return score


def _compress_subtitle_hook_for_wechat(line, max_chars):
    """Compress one grounded subtitle hook into WeChat's 16-character limit."""
    value = _clean_subtitle_hook_line(line)
    # Subtitles often break in the middle of an enumeration. The first clause
    # is the complete hook; the truncated remainder belongs to the next line.
    if "、" in value:
        value = value.split("、", 1)[0]
    replacements = (
        ("有一半的工作是在当", "一半是在"),
        ("有一半工作是", "一半是"),
        ("有一半的工作是", "一半是"),
        ("是在当", "是在"),
        ("的人", ""),
        ("的时候", ""),
    )
    for old, new in replacements:
        value = value.replace(old, new)
    value = value.rstrip("的了着")
    if not value or len(value) > max_chars:
        return ""
    candidate = sanitize_wechat_title(value, max_chars=max_chars)
    if (
        not candidate
        or candidate == "未命名视频"
        or any(marker in candidate for marker in _GENERIC_TITLE_MARKERS)
    ):
        return ""
    return candidate


def _fallback_consequence_wechat_title(subtitle_text, form_title, max_chars):
    """Turn a transcript cause and consequence into a short form title."""
    subject = _extract_bili_title_subject(form_title)
    compact_text = re.sub(r"\s+", "", str(subtitle_text or ""))
    if not subject:
        return ""
    if not re.search(r"被.{0,8}裁掉", compact_text) or "烂掉" not in compact_text:
        return ""
    candidate = f"{subject}被裁后组织会烂掉"
    candidate = sanitize_wechat_title(candidate, max_chars=max_chars)
    if (
        len(candidate) <= max_chars
        and not _shared_title_has_quality_issue(candidate)
        and subject in candidate
    ):
        return candidate
    return ""


def _fallback_consequence_cover_title(subtitle_text):
    """Combine a metaphor loss with its later consequence in the transcript."""
    compact_text = re.sub(r"\s+", "", str(subtitle_text or ""))
    match = re.search(r"没了([\u4e00-\u9fff]{1,6})，([^，。？！]{1,20})", compact_text)
    if not match or "烂掉" not in compact_text:
        return ""
    lost_object = match.group(1)
    affected = match.group(2).replace("、", "")
    candidate = f"没了{lost_object}，{affected}慢慢烂掉"
    if 12 <= len(candidate) <= 48 and not _cover_title_has_quality_issue(candidate):
        return candidate
    return ""


def _fallback_wechat_title_from_subtitle(
    subtitle_text,
    max_chars=16,
    form_title=None,
):
    """Choose a concrete transcript hook when the title model times out."""
    candidates = []
    consequence_title = _fallback_consequence_wechat_title(
        subtitle_text,
        form_title,
        max_chars,
    )
    if consequence_title:
        candidates.append((24.0, consequence_title))
    for line in str(subtitle_text or "").splitlines():
        cleaned = _clean_subtitle_hook_line(line)
        if not any(marker in cleaned for marker in _SUBTITLE_HOOK_MARKERS):
            continue
        candidate = _compress_subtitle_hook_for_wechat(cleaned, max_chars)
        if candidate:
            score = _subtitle_hook_score(candidate)
            if candidate.startswith("我们"):
                score -= 6.0
            if "数据治理" in candidate:
                score += 6.0
            candidates.append((score, candidate))
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda item: (
            not item[1].startswith("我们"),
            "数据治理" in item[1],
            item[0],
            len(item[1]),
        ),
    )[1]


def _fallback_cover_title_from_subtitle(
    subtitle_text,
    form_title=None,
    max_chars=48,
):
    """Use a complete subtitle hook instead of repeating the form title."""
    candidates = []
    consequence_cover = _fallback_consequence_cover_title(subtitle_text)
    if consequence_cover and not _cover_title_repeats_form_title(
        consequence_cover,
        form_title,
    ):
        candidates.append((100.0, consequence_cover))
    for line in str(subtitle_text or "").splitlines():
        cleaned = _clean_subtitle_hook_line(line)
        if "、" in cleaned:
            first_clause = cleaned.split("、", 1)[0]
            if len(first_clause) >= 12:
                cleaned = first_clause
        if not 12 <= len(cleaned) <= max_chars:
            continue
        if not any(marker in cleaned for marker in _SUBTITLE_HOOK_MARKERS):
            continue
        if _cover_title_has_quality_issue(cleaned):
            continue
        if _cover_title_repeats_form_title(cleaned, form_title):
            continue
        candidates.append((_subtitle_hook_score(cleaned), cleaned))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], len(item[1])))[1]


def _persist_upload_metadata(workdir, metadata):
    """Write generated upload metadata atomically for review and reuse."""
    if not metadata:
        return
    path = os.path.join(workdir, "upload_metadata.json")
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _load_upload_metadata(workdir):
    """Load persisted metadata so retries do not regenerate completed fields."""
    path = os.path.join(workdir, "upload_metadata.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _fallback_bili_title_from_description(description, max_chars):
    """Build a conservative shared title when title-model calls time out."""
    value = re.sub(r"\s+", " ", str(description or "")).strip()
    if not value:
        return ""

    subject_match = re.search(r"((?:[A-Za-z][A-Za-z0-9'&.-]*\s*)?员工)", value)
    if subject_match and "：" in value:
        before_colon, after_colon = value.split("：", 1)
        if "被" in before_colon:
            hook = re.split(r"[。！？\n]", before_colon)[-1]
        else:
            hook = after_colon
        hook = re.split(r"[。！？\n]", hook, maxsplit=1)[0]
        hook = re.split(r"[，,；;]", hook, maxsplit=1)[0]
        hook = hook.replace("“", "").replace("”", "")
        value = f"{subject_match.group(1)}谈{hook}"
    else:
        first_sentence = re.split(r"[。！？\n]", value, maxsplit=1)[0]
        if "：" in first_sentence:
            prefix, remainder = first_sentence.split("：", 1)
            first_clause = re.split(r"[，,；;]", remainder, maxsplit=1)[0]
            value = f"{prefix}{first_clause}"
        else:
            value = re.split(r"[，,；;]", first_sentence, maxsplit=1)[0]

    value = value.replace("“", "").replace("”", "")
    value = value.replace("现身说法", "谈")
    if value.startswith("几位 "):
        value = value[len("几位 "):]

    if len(value) > max_chars:
        cut = value.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        value = value[:cut].rstrip(" ：:，,。.！!？?；;")
    return normalize_bili_upload_title(value, max_chars=max_chars)


def _fallback_wechat_title_from_bili_title(bili_title, max_chars):
    """Compress the local Bilibili fallback without another model call."""
    value = re.sub(r"\s+", " ", str(bili_title or "")).strip()
    marker = value.find("谈")
    if marker <= 0:
        return ""

    subject = re.sub(r"\s+", "", value[:marker])
    body = re.sub(r"\s+", "", value[marker + len("谈"):])
    body = re.sub(r"一上来就?", "", body)
    parts = re.split(r"(?:即被|就被|被|[，,；;：:])", body, maxsplit=1)
    prefix = parts[0]
    suffix = parts[1] if len(parts) > 1 else ""
    if suffix:
        suffix = re.sub(r"^(?:扔进|丢进|放进)", "", suffix)
        if len(prefix) + len(suffix) <= max_chars - len(subject) - len("谈"):
            body = prefix + suffix
        else:
            body = prefix
    else:
        body = prefix
    remaining = max_chars - len(subject) - len("谈")
    if remaining <= 0:
        return ""
    body = body[:remaining].rstrip("的了着")
    candidate = f"{subject}谈{body}"
    return normalize_shared_upload_title(candidate, max_chars=max_chars)


def _fallback_cover_title(meta):
    """Reuse the shortest validated shared title as the cover headline."""
    for key in ("wechat_title", "title"):
        value = re.sub(r"\s+", " ", str(meta.get(key) or "")).strip()
        if 12 <= len(value) <= 48 and not _cover_title_has_quality_issue(value):
            return value
    return ""


def generate_upload_metadata(
    workdir,
    on_log=None,
    bili_title_max_chars=BILI_UPLOAD_TITLE_MAX_CHARS,
    wechat_title_max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
    llm_model=None,
    llm_provider=None,
):
    """Generate Bilibili/WeChat form titles and shared upload metadata.

    Returns ``{title, wechat_title, cover_title, description}`` or ``None``.
    ``title`` is the fuller Bilibili title; ``wechat_title`` is derived from it.
    """
    _log = on_log or (lambda msg: print(msg, flush=True))
    try:
        from subtitle_title import (
            build_subtitle_grounded_title,
            read_full_subtitle_text,
        )
    except Exception as e:
        _log(f"⚠️ [MetaGen] subtitle_title import failed: {e}")
        return None
    try:
        from llm_utils import get_client as _get_llm_client, LLMProvider
        llm_client = _get_llm_client()
    except Exception:
        llm_client = None
    default_model, default_provider = _load_llm_defaults()
    model = str(llm_model or default_model or "glm-5.2")
    provider_name = str(llm_provider or default_provider or "dashscope").strip().lower()
    provider = None
    if llm_client is not None:
        try:
            provider = LLMProvider(provider_name)
        except ValueError:
            provider = None
    hardsub_path = find_hardsub_video(workdir)
    if not hardsub_path:
        _log("⚠️ [MetaGen] No hardsub video found")
        return None
    meta = _load_upload_metadata(workdir)
    sub_text = None
    full_sub_text = None
    source_title = _extract_source_title(hardsub_path)
    if source_title:
        meta["source_title"] = source_title

    required_fields = ("title", "wechat_title", "description", "cover_title")
    if all(str(meta.get(field) or "").strip() for field in required_fields):
        return meta

    # Generate the description first. The form title is a compressed version of
    # this summary, so sharing its first sentence keeps the title tied to the
    # whole video instead of one striking transcript fragment.
    try:
        full_sub_text, _full_sub_path = read_full_subtitle_text(hardsub_path)
        full_sub_text = _correct_known_asr_proper_nouns(full_sub_text)
        sub_text = _extract_chinese_subtitle_lines(full_sub_text)
        if sub_text and llm_client:
            prompt = (
                "根据以下视频字幕，写一段100-200字的视频简介。简洁有力，概括核心内容，不要AI味。\n\n"
                "视频原标题只作题材和场景参考，不是唯一事实来源。"
                "简介应先判断字幕实际主线，再吸收原标题中的场景或受益者。"
                f"\n视频原标题：\n{source_title}\n\n"
                "所有数字、金额、时间和公司关系必须逐字来自字幕；"
                "不得换算数量级，也不得新增字幕没有的事实。\n"
                "英文人名、公司名、产品名必须逐字使用字幕或视频文件名中的拼写，"
                "不得改拼、音译回英文或使用相近姓氏。\n\n"
                f"{sub_text}\n\n只输出简介内容。"
            )
            for _attempt in range(2):
                desc = llm_client.generate_content(
                    prompt, model_name=model, provider=provider
                )
                desc = re.sub(r"\s+", " ", str(desc or "")).strip()
                issues = _metadata_text_issues(desc, full_sub_text, hardsub_path)
                if not issues:
                    meta["description"] = desc
                    _log(f">>> [MetaGen] 简介: {desc[:60]}...")
                    break
                _log(f"⚠️ [MetaGen] Description rejected: {'；'.join(issues)}")
                prompt = (
                    "上一轮简介未通过专有名词溯源校验："
                    f"{'；'.join(issues)}。\n"
                    "请保留原概括，只修正这些英文专名；"
                    "无法确认的英文专名改用中文字幕中的既有译名。\n\n"
                    f"{prompt}"
                )
    except Exception as e:
        _log(f"⚠️ [MetaGen] Description generation failed: {e}")

    # Prefer a title compressed from the whole-video description. The older
    # subtitle-candidate path remains as a fallback when this grounded path
    # cannot produce a valid short title.
    if meta.get("description") and sub_text and llm_client:
        try:
            summary_title = _generate_title_from_description(
                meta["description"],
                sub_text,
                full_sub_text,
                llm_client,
                model,
                bili_title_max_chars,
                provider,
                source_title=source_title,
            )
            if summary_title:
                meta["title"] = summary_title
                _log(f">>> [MetaGen] B站标题: {summary_title}")
        except Exception as e:
            _log(f"⚠️ [MetaGen] Bilibili title generation failed: {e}")

    # 1. Bilibili form title (fuller editorial summary)
    description_context = ""
    if meta.get("description"):
        description_context = str(meta["description"]).splitlines()[0].strip()[:120]
    if not meta.get("title"):
        try:
            platform_hint = (
                "B站表单标题没有微信的16字限制，优先24-40字。"
                "它必须像视频简介第一句的完整编辑版："
                "保留主体、谓语和最具体的核心结果，并保留受益者、问题或明确立场，"
                "禁止只剩过程、代词、泛化判断或「主体+动作+对象」压缩片段。"
                "优先覆盖访谈主题、核心业务/做法和明确立场或结果。"
                "英文名逐字符计数；不要为了覆盖第三个信息点超过 48 字符。"
                "英文字母逐字符计数，例如 Palantir 为 8 字符。"
                "候选 evidence 必须逐字复制字幕原文，禁止使用省略号；"
                "不确定时选择包含标题关键词的完整原行，不要改写成概括句。"
                "不得用「东西」「能力」等泛词替代字幕中的具体对象。"
                "信息过多时优先删除「把/了/的/一个」等功能词和修饰语，"
                "保留主体、具体对象、核心做法和结果。"
                "不得用「它/他们」替代主体。"
            )
            if description_context:
                platform_hint += f"视频简介第一句：{description_context}"
            if source_title:
                platform_hint += (
                    "视频原标题只作题材和场景参考，不是唯一事实来源。"
                    "若字幕反复聚焦另一个主体，优先反映字幕主线，"
                    "并保留原标题中有用的场景或受益者。"
                    f"视频原标题：{source_title}"
                )
            grounded = build_subtitle_grounded_title(
                hardsub_path, llm_client=llm_client, model_name=model,
                max_title_chars=bili_title_max_chars,
                min_title_chars=min(
                    BILI_UPLOAD_TITLE_TARGET_MIN_CHARS,
                    bili_title_max_chars,
                ),
                platform_hint=platform_hint,
                title_style="plain",
                provider=provider,
            )
            fallback_title = normalize_bili_upload_title(
                grounded.title,
                max_chars=bili_title_max_chars,
            )
            if fallback_title:
                meta["title"] = fallback_title
                _log(f">>> [MetaGen] B站标题: {fallback_title}")
            else:
                _log("⚠️ [MetaGen] Fallback Bilibili title rejected by quality rules")
        except Exception as e:
            _log(f"⚠️ [MetaGen] Fallback Bilibili title generation failed: {e}")

    if not meta.get("title"):
        fallback_title = _fallback_bili_title_from_source_title(
            source_title,
            max_chars=bili_title_max_chars,
        )
        fallback_source = "原标题"
        if not fallback_title:
            fallback_title = _fallback_bili_title_from_description(
                meta.get("description"),
                max_chars=bili_title_max_chars,
            )
            fallback_source = "本地"
        if fallback_title:
            meta["title"] = fallback_title
            _log(f">>> [MetaGen] B站标题 ({fallback_source}兜底): {fallback_title}")

    # 2. WeChat form title, derived from the Bilibili title rather than
    # regenerating a separate theme under a tighter character budget.
    if meta.get("title") and meta.get("description") and sub_text and llm_client:
        try:
            wechat_title = _derive_wechat_title_from_bili_title(
                meta["title"],
                meta["description"],
                sub_text,
                llm_client,
                model,
                max_chars=wechat_title_max_chars,
                provider=provider,
                source_title=source_title,
            )
            if wechat_title:
                meta["wechat_title"] = wechat_title
                _log(f">>> [MetaGen] 视频号标题: {wechat_title}")
        except Exception as e:
            _log(f"⚠️ [MetaGen] WeChat title generation failed: {e}")

    if not meta.get("wechat_title") and meta.get("title"):
        fallback_wechat_title = _fallback_wechat_title_from_subtitle(
            sub_text,
            max_chars=wechat_title_max_chars,
            form_title=meta.get("title"),
        )
        fallback_source = "字幕钩子"
        if not fallback_wechat_title:
            fallback_wechat_title = _fallback_wechat_title_from_bili_title(
                meta["title"],
                max_chars=wechat_title_max_chars,
            )
            fallback_source = "本地"
        if not fallback_wechat_title and source_title:
            fallback_wechat_title = _fallback_wechat_title_from_source_title(
                source_title,
                max_chars=wechat_title_max_chars,
            )
            fallback_source = "原标题"
        if fallback_wechat_title:
            meta["wechat_title"] = fallback_wechat_title
            _log(f">>> [MetaGen] 视频号标题 ({fallback_source}兜底): {fallback_wechat_title}")

    # 3. Cover title (editorial headline style)
    try:
        cover_title = _generate_cover_title_from_full_subtitle(
            sub_text,
            llm_client,
            model,
            min_chars=12,
            max_chars=48,
            provider=provider,
            form_title=meta.get("title"),
            source_title=source_title,
        )
        if cover_title:
            meta["cover_title"] = cover_title
            _log(f">>> [MetaGen] 封面标题: {cover_title}")
    except Exception as e:
        _log(f"⚠️ [MetaGen] Cover title generation failed: {e}")

    if not meta.get("cover_title"):
        fallback_cover_title = _fallback_cover_title_from_subtitle(
            sub_text,
            form_title=meta.get("title"),
            max_chars=48,
        )
        fallback_source = "字幕钩子"
        if not fallback_cover_title:
            fallback_cover_title = _fallback_cover_title(meta)
            fallback_source = "本地"
        if not fallback_cover_title and source_title:
            fallback_cover_title = _fallback_bili_title_from_source_title(
                source_title,
                max_chars=48,
            )
            fallback_source = "原标题"
        if fallback_cover_title:
            meta["cover_title"] = fallback_cover_title
            _log(f">>> [MetaGen] 封面标题 ({fallback_source}兜底): {fallback_cover_title}")

    if not meta.get("title"):
        _persist_upload_metadata(workdir, meta)
        return meta
    _persist_upload_metadata(workdir, meta)
    return meta if meta else None

# --- WeChat Channels Upload (canonical implementation; batch engine + GUI reuse this) ---
def build_wechat_title(hardsub_path):
    """Derive a WeChat Channels title, capped at the platform's short-title limit."""
    return sanitize_wechat_title(build_bili_title(hardsub_path))

def get_wechat_upload_skip_reason(workdir):
    """Return a user-facing reason when WeChat upload must not start."""
    if not os.path.exists(WECHAT_SCRIPT) or not os.path.exists(WECHAT_PYTHON):
        return "script/python not found"
    hardsub_path = find_hardsub_video(workdir)
    if not hardsub_path:
        return "hardsub video not found"
    duration = get_video_duration(hardsub_path)
    if duration <= 0:
        return f"cannot read duration: {os.path.basename(hardsub_path)}"
    if duration > WECHAT_MAX_UPLOAD_SECONDS:
        return (
            f"视频时长 {duration / 60:.1f} 分钟，超过视频号 "
            f"{WECHAT_MAX_UPLOAD_SECONDS // 60} 分钟限制"
        )
    return None

def build_wechat_cmd(
    workdir,
    title=None,
    desc=None,
    tags=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    require_complete_metadata=False,
):
    """Build the WeChat Channels upload command for the hardsub video in workdir.
    Description/tags are auto-generated by the skill unless explicitly overridden.
    The default action saves a draft; public publishing requires an explicit flag."""
    skip_reason = get_wechat_upload_skip_reason(workdir)
    if skip_reason:
        return None
    metadata_values = (title, desc, cover_title)
    if require_complete_metadata and not all(
        str(value or "").strip() for value in metadata_values
    ):
        return None
    hardsub_path = find_hardsub_video(workdir)
    # Omit the title by default so wechat_upload.py generates it from the
    # subtitle. A caller can still pass a title for an explicit manual override.
    _pre_generated = title is not None and desc is not None
    cmd = [WECHAT_PYTHON, "-u", WECHAT_SCRIPT, hardsub_path]
    if title is not None:
        cmd.append(str(title))
    if desc:
        cmd.extend(["--desc", str(desc)])
    if tags:
        cmd.extend(["--tags", str(tags)])
    if cover_title is not None and str(cover_title).strip():
        cmd.extend(["--cover-title", str(cover_title).strip()])
    if _pre_generated:
        cmd.append("--no_auto_title")
    if llm_model:
        cmd.extend(["--llm-model", str(llm_model)])
    if llm_provider:
        cmd.extend(["--llm-provider", str(llm_provider)])
    if public:
        cmd.append("--public")
    # Login is a separate, visible one-time `--login` flow. Normal uploads use
    # the persistent profile without opening a Chrome window.
    cmd.append("--headless")
    return cmd

def run_wechat_cmd(cmd, on_progress=None, on_log=None, abort_flag=None):
    """Run a pre-built WeChat Channels upload command. Returns True on success."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        if on_log:
            on_log(f"❌ Failed to start WeChat Channels upload: {e}")
        return False
    if p.stdout:
        import queue as _queue
        import threading as _threading
        _line_q: _queue.Queue = _queue.Queue()
        _reader_stop = _threading.Event()

        def _stdout_reader():
            try:
                for line in p.stdout:
                    if _reader_stop.is_set(): break
                    _line_q.put(line)
            except Exception:
                pass
            finally:
                _line_q.put(None)

        _rt = _threading.Thread(target=_stdout_reader, daemon=True)
        _rt.start()

        while True:
            if abort_flag and abort_flag():
                p.kill()
                _reader_stop.set()
                break
            try:
                line = _line_q.get(timeout=1.0)
            except Exception:
                if p.poll() is not None:
                    while True:
                        try:
                            line = _line_q.get(timeout=0.5)
                        except Exception:
                            break
                        if line is None: break
                        stripped = line.strip()
                        if stripped and on_log:
                            on_log(stripped)
                        m = re.search(r'(?:Progress|上传进度):\s*([\d.]+)%', line)
                        if m and on_progress:
                            on_progress(float(m.group(1)))
                    break
                continue
            if line is None: break
            stripped = line.strip()
            if stripped and on_log:
                on_log(stripped)
            m = re.search(r'(?:Progress|上传进度):\s*([\d.]+)%', line)
            if m and on_progress:
                on_progress(float(m.group(1)))
    p.wait()
    return p.returncode == 0

def trigger_wechat_upload(
    workdir,
    title=None,
    desc=None,
    tags=None,
    public=False,
    cover_title=None,
    llm_model=None,
    llm_provider=None,
    on_progress=None,
    on_log=None,
    abort_flag=None,
):
    """Build and run a WeChat Channels upload for the hardsub video in workdir."""
    _log = on_log or (lambda msg: print(msg, flush=True))
    skip_reason = get_wechat_upload_skip_reason(workdir)
    if skip_reason:
        _log(f"⚠️  [WeChat] upload skipped ({skip_reason})")
        return False
    cmd = build_wechat_cmd(
        workdir,
        title=title,
        desc=desc,
        tags=tags,
        public=public,
        cover_title=cover_title,
        llm_model=llm_model,
        llm_provider=llm_provider,
    )
    if not cmd:
        _log("⚠️  [WeChat] upload skipped (command could not be built)")
        return False
    _log(f"📹 Starting WeChat Channels upload: {os.path.basename(cmd[2])}")
    ok = run_wechat_cmd(cmd, on_progress=on_progress, on_log=_log, abort_flag=abort_flag)
    _log(f"📹 {'✅ Success' if ok else '❌ Failed'}")
    return ok


def _write_upload_marker(workdir, marker_name):
    try:
        with open(os.path.join(workdir, marker_name), "w", encoding="utf-8") as handle:
            handle.write("1")
    except Exception:
        pass


def run_post_processing_uploads(
    workdir,
    sync_gdrive=None,
    bili_upload=False,
    wechat_upload=False,
    headless=False,
    bili_cover_title=None,
    wechat_cover_title=None,
    wechat_public=False,
    llm_model=None,
    llm_provider=None,
):
    """Run GD/BI/WX uploads together and share BI/WX metadata."""
    need_sync = bool(sync_gdrive) and not os.path.exists(os.path.join(workdir, ".synced"))
    need_bili = bool(bili_upload) and not os.path.exists(os.path.join(workdir, ".bili_uploaded"))
    need_wechat = bool(wechat_upload) and not os.path.exists(os.path.join(workdir, ".wechat_uploaded"))
    if not any((need_sync, need_bili, need_wechat)):
        return

    shared_uploads = need_bili and need_wechat
    explicit_cover_titles = (
        str(bili_cover_title or "").strip(),
        str(wechat_cover_title or "").strip(),
    )
    if shared_uploads and all(explicit_cover_titles) and explicit_cover_titles[0] != explicit_cover_titles[1]:
        print("❌ [MetaGen] Bilibili and WeChat cover titles differ; shared uploads stopped", flush=True)
        need_bili = False
        need_wechat = False
        shared_uploads = False

    meta = None
    if shared_uploads:
        meta = generate_upload_metadata(
            workdir,
            bili_title_max_chars=BILI_UPLOAD_TITLE_MAX_CHARS,
            wechat_title_max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
            llm_model=llm_model,
            llm_provider=llm_provider,
        )

    bili_kwargs = {}
    wechat_kwargs = {"public": wechat_public}
    if llm_model:
        bili_kwargs["llm_model"] = llm_model
        wechat_kwargs["llm_model"] = llm_model
    if llm_provider:
        bili_kwargs["llm_provider"] = llm_provider
        wechat_kwargs["llm_provider"] = llm_provider
    if shared_uploads and not meta:
        print("❌ [MetaGen] shared metadata generation failed; BI/WX uploads stopped", flush=True)
        need_bili = False
        need_wechat = False
    elif meta:
        bili_title = normalize_bili_upload_title(
            meta.get("title"),
            max_chars=BILI_UPLOAD_TITLE_HARD_MAX_CHARS,
        )
        wechat_title = normalize_shared_upload_title(
            meta.get("wechat_title"),
            max_chars=SHARED_UPLOAD_TITLE_MAX_CHARS,
        )
        shared_desc = str(meta.get("description") or "").strip()
        shared_cover_title = str(
            bili_cover_title or wechat_cover_title or meta.get("cover_title") or ""
        ).strip()
        if not bili_title or not wechat_title or not shared_desc or not shared_cover_title:
            print("❌ [MetaGen] Bilibili/WeChat title, description, or cover title incomplete; BI/WX uploads stopped", flush=True)
            need_bili = False
            need_wechat = False
        else:
            shared_metadata = {
                "title": bili_title,
                "wechat_title": wechat_title,
                "desc": shared_desc,
                "cover_title": shared_cover_title,
            }
            bili_kwargs.update({
                "title": bili_title,
                "desc": shared_desc,
                "cover_title": shared_cover_title,
            })
            wechat_kwargs.update({
                "title": wechat_title,
                "desc": shared_desc,
                "cover_title": shared_cover_title,
            })
    elif bili_cover_title:
        bili_kwargs["cover_title"] = bili_cover_title
    elif wechat_cover_title:
        wechat_kwargs["cover_title"] = wechat_cover_title

    if sum((need_sync, need_bili, need_wechat)) == 1:
        if need_sync:
            if trigger_sync(workdir, sync_gdrive, headless=headless) is True:
                _write_upload_marker(workdir, ".synced")
        elif need_bili:
            if trigger_bili_upload(workdir, **bili_kwargs):
                _write_upload_marker(workdir, ".bili_uploaded")
        elif need_wechat:
            if trigger_wechat_upload(workdir, **wechat_kwargs):
                _write_upload_marker(workdir, ".wechat_uploaded")
        return

    import threading
    sync_result = [False]
    bili_result = [False]
    wechat_result = [False]
    threads = []

    if need_sync:
        def _run_sync():
            sync_result[0] = trigger_sync(workdir, sync_gdrive, headless=headless) is True
        threads.append(threading.Thread(target=_run_sync, daemon=True))
    if need_bili:
        def _run_bili():
            bili_result[0] = trigger_bili_upload(workdir, **bili_kwargs)
        threads.append(threading.Thread(target=_run_bili, daemon=True))
    if need_wechat:
        def _run_wechat():
            wechat_result[0] = trigger_wechat_upload(workdir, **wechat_kwargs)
        threads.append(threading.Thread(target=_run_wechat, daemon=True))

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if sync_result[0]:
        _write_upload_marker(workdir, ".synced")
    if bili_result[0]:
        _write_upload_marker(workdir, ".bili_uploaded")
    if wechat_result[0]:
        _write_upload_marker(workdir, ".wechat_uploaded")


# --- Robust FFmpeg/ffprobe Detection ---
def find_tool(tool_name):
    """Finds a tool in PATH or common installation directories."""
    import shutil
    # 0. Check bundled internal path (if frozen)
    if getattr(sys, 'frozen', False):
        bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        exe_ext = ".exe" if sys.platform == "win32" else ""
        internal_tool = os.path.join(bundle_dir, tool_name + exe_ext)
        if os.path.exists(internal_tool): return internal_tool
        # Also check root of exe
        root_tool = os.path.join(os.path.dirname(sys.executable), tool_name + exe_ext)
        if os.path.exists(root_tool): return root_tool

    # 1. Check PATH
    path = shutil.which(tool_name)
    if path: return path
    
    # 2. Check WinGet Gyan FFmpeg (User Specific)
    user_home = os.path.expanduser("~")
    winget_base = os.path.join(user_home, "AppData", "Local", "Microsoft", "Winget", "Packages")
    if os.path.exists(winget_base):
        # Search for Gyan FFmpeg
        for d in os.listdir(winget_base):
            if "Gyan.FFmpeg" in d:
                # Glob search for the bin folder
                for bin_dir in glob.glob(os.path.join(winget_base, d, "**/bin"), recursive=True):
                    exe_ext = ".exe" if sys.platform == "win32" else ""
                    tool_path = os.path.join(bin_dir, tool_name + exe_ext)
                    if os.path.exists(tool_path): return tool_path

    # 3. Check common hardcoded paths
    fallbacks = [
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
    ]
    
    # Proactively check for CapCut installation to steal its FFmpeg
    # Better: check for "CapCut.exe" path and then look for ffmpeg nearby
    for drive in ["C:", "D:"]:
        capcut_root = os.path.join(drive, "Program Files", "CapCut")
        if os.path.exists(capcut_root):
            # Find latest version folder
            try:
                versions = [d for d in os.listdir(capcut_root) if os.path.isdir(os.path.join(capcut_root, d)) and "." in d]
                if versions:
                    latest = sorted(versions, key=lambda x: [int(v) for v in x.split('.') if v.isdigit()], reverse=True)[0]
                    fallbacks.append(os.path.join(capcut_root, latest))
            except: pass

    for fb in fallbacks:
        exe_ext = ".exe" if sys.platform == "win32" else ""
        tool_path = os.path.join(fb, tool_name + exe_ext)
        if os.path.exists(tool_path): return tool_path
        
    return tool_name # Fallback to original name and hope for the best

FFMPEG_EXE = find_tool("ffmpeg")
FFPROBE_EXE = find_tool("ffprobe")

if FFMPEG_EXE != "ffmpeg":
    print(f"📦 Found FFmpeg at: {FFMPEG_EXE}")
    # Add to path for sub-scripts
    os.environ["PATH"] += os.pathsep + os.path.dirname(FFMPEG_EXE)

VDOWN_CMD = [sys.executable, os.path.join(TOOLS_DIR, "vdown", "download.py")]
TRANSCRIBER_CMD = [sys.executable, os.path.join(TOOLS_DIR, "transcriber", "transcribe_engine.py")]
SMART_TRANSLATE_CMD = [sys.executable, os.path.join(TOOLS_DIR, "autosub", "smart_translate.py")]
SUBTRANSLATOR_CMD = [sys.executable, os.path.join(TOOLS_DIR, "subtranslator", "subtranslator.py")]
SRT2ASS_CMD = [sys.executable, os.path.join(TOOLS_DIR, "hardsubber", "srt_to_ass.py")]
BURNSUB_CMD = [sys.executable, os.path.join(TOOLS_DIR, "hardsubber", "burn_engine.py")]

if os.path.exists(env_path):
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f :
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    if k and v: os.environ[k] = v.strip('"\'')
    except: pass

def get_workdir(input_val, base_output_dir):
    if input_val.startswith("http"):
        return os.path.join(base_output_dir, "temp_" + str(int(time.time())))
    else:
        abs_path = os.path.abspath(input_val)
        try:
            base = os.path.normpath(base_output_dir).lower()
            current = os.path.normpath(abs_path).lower()
            if current.startswith(base): return os.path.dirname(abs_path)
        except: pass
        return os.path.join(base_output_dir, os.path.splitext(os.path.basename(input_val))[0])

def get_video_title(url, cookies=None):
    """Fetches video title using download.py --get-title."""
    try:
        # Use --print title for less noisy output
        cmd = list(VDOWN_CMD) + [url, cookies or "", "", "--get-title"]
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            encoding='utf-8', 
            errors='replace',
            creationflags=CREATE_NO_WINDOW,
            timeout=40
        )
        if result.returncode == 0:
            title = result.stdout.strip()
            # If multiple lines, take the last non-empty one
            lines = [l.strip() for l in title.splitlines() if l.strip()]
            if lines:
                title = lines[-1]
                if "Error" not in title: return title
    except: pass
    return None

def get_video_folder_name(url, cookies=None):
    """Fetches stable folder name using download.py --get-folder-name."""
    try:
        cmd = list(VDOWN_CMD) + [url, cookies or "", "", "--get-folder-name"]
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            encoding='utf-8', 
            errors='replace',
            creationflags=CREATE_NO_WINDOW,
            timeout=40
        )
        if result.returncode == 0:
            folder_name = result.stdout.strip()
            # If multiple lines, take the last non-empty one
            lines = [l.strip() for l in folder_name.splitlines() if l.strip()]
            if lines:
                folder_name = lines[-1]
                if "Error" not in folder_name: return folder_name
    except: pass
    return None

def sanitize_filename(filename):
    """Cleans a string to be a safe filename."""
    clean = re.sub(r'[\\/*?:"<>|]', '_', filename)
    clean = clean.strip().strip('.') # Strip trailing spaces and dots
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:100] # Limit length

def rename_workdir_safely(old_workdir, new_workdir, current_video_path, logger_instance):
    """Safely closes the logger, renames the directory, updates paths, and reopens the logger."""
    import sys
    
    # 1. Close current logger to release file handles
    if logger_instance:
        try:
            # Restore stdout/stderr first
            sys.stdout = logger_instance.terminal
            sys.stderr = logger_instance.terminal
            logger_instance.close()
        except Exception as e:
            print(f"⚠️ Error closing logger for rename: {e}")
            
    # 2. Rename directory
    renamed = False
    try:
        if os.path.exists(old_workdir) and not os.path.exists(new_workdir):
            os.rename(old_workdir, new_workdir)
            renamed = True
            print(f"✅ Project folder renamed to: {os.path.basename(new_workdir)}")
    except Exception as e:
        print(f"⚠️ Failed to rename project folder: {e}")
        
    if renamed:
        resolved_workdir = new_workdir
        # Update video path
        video_filename = os.path.basename(current_video_path)
        resolved_video_path = os.path.join(new_workdir, video_filename)
    else:
        resolved_workdir = old_workdir
        resolved_video_path = current_video_path
        
    # 3. Re-initialize logger in the new/old path
    new_log_path = os.path.join(resolved_workdir, "workflow.log")
    try:
        new_logger = Logger(new_log_path)
        sys.stdout = new_logger
        sys.stderr = new_logger
    except Exception as e:
        print(f"⚠️ Failed to restart logger: {e}")
        new_logger = logger_instance # Fallback
        
    return resolved_workdir, resolved_video_path, new_logger

def get_remote_duration(url, cookies=None):
    """Fetch video duration (seconds) via yt-dlp --get-duration."""
    try:
        cmd = list(VDOWN_CMD) + [url, cookies or "", "", "--get-duration"]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW, timeout=40)
        if result.returncode == 0:
            raw = result.stdout.strip()
            # yt-dlp returns seconds for --get-duration (e.g. "2938")
            try:
                return float(raw)
            except ValueError:
                # Fallback: parse "HH:MM:SS" format
                parts = raw.split(':')
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                return 0
    except Exception:
        pass
    return 0

def download_video(url, workdir, cookies=None, quick=False, start_time=None, end_time=None, title=None):
    print(f"🎬 Downloading {url}...", flush=True)
    if not os.path.exists(workdir): os.makedirs(workdir)
    # Skip download if a valid source video already exists in workdir (re-run
    # of the same URL). Verify by comparing local video duration against the
    # remote duration reported by yt-dlp, with a 5% tolerance. _hardsub outputs
    # are never treated as source inputs.
    if not start_time and not end_time:
        existing = [f for f in os.listdir(workdir)
                    if f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts'))
                    and not f.endswith('.part')
                    and '_hardsub' not in f]
        existing.sort(key=lambda x: os.path.getsize(os.path.join(workdir, x)), reverse=True)
        for cand in existing:
            candidate = os.path.join(workdir, cand)
            local_dur = get_video_duration(candidate)
            if local_dur <= 0:
                continue
            remote_dur = get_remote_duration(url, cookies)
            if remote_dur > 0 and abs(local_dur - remote_dur) / remote_dur < 0.05:
                print(f"✅ Found existing video matching remote duration ({local_dur:.0f}s vs {remote_dur:.0f}s): {os.path.basename(candidate)} (Skipping download)")
                return candidate
            elif remote_dur <= 0:
                # yt-dlp could not fetch duration — fall back to size check
                # (>10 MB to exclude stubs / partial downloads).
                if os.path.getsize(candidate) > 10 * 1024 * 1024:
                    print(f"✅ Found existing video (remote duration unknown, local {local_dur:.0f}s, {os.path.getsize(candidate)/(1024*1024):.0f}MB): {os.path.basename(candidate)} (Skipping download)")
                    return candidate
    cmd = list(VDOWN_CMD) + [url, cookies or "", workdir]
    if title:
        cmd.extend(["--title", title])
    if quick:
        cmd.append("--write-subs")
    if start_time:
        cmd.extend(["--start", start_time])
    if end_time:
        cmd.extend(["--end", end_time])
    try: 
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
        for line in process.stdout or []:
            msg = line.strip()
            if "Progress:" in msg or ("[download]" in msg and "%" in msg):
                sys.stdout.write(f"\r   {msg}    ")
                sys.stdout.flush()
            else:
                print(msg, flush=True)
        process.wait()
        
        # --- Robustness Check with Transparency ---
        # _hardsub outputs are excluded — they are burn results, not sources.
        video_files = [f for f in os.listdir(workdir) if f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts')) and not f.endswith('.part') and '_hardsub' not in f]
        if video_files:
            video_files.sort(key=lambda x: os.path.getsize(os.path.join(workdir, x)), reverse=True)
            candidate = os.path.join(workdir, video_files[0])
            if os.path.getsize(candidate) > 1024 * 1024:
                if process.returncode != 0:
                    print(f"⚠️  Download reported error ({process.returncode}), but a valid video file was found.")
                    print(f"   Proceeding with: {os.path.basename(candidate)}")
                return candidate

        if process.returncode != 0:
            print(f"❌ Download command failed with code {process.returncode}")
            return None
    except Exception as e:
        print(f"❌ Error during download: {e}")
        return None
    return None
def transcribe_video(video_path, workdir, model="large-v2", quick=False, multilang=False):
    print(f"🎙️ Transcribing {os.path.basename(video_path)}...", flush=True)
    cmd = list(TRANSCRIBER_CMD) + [video_path, "--model", model, "--output", workdir, "--no-gui"]
    if quick:
        cmd.append("--quick")
    if multilang:
        cmd.append("--multilang")
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
        for line in process.stdout or []:
            msg = line.strip()
            if "Progress:" in msg or ("[download]" in msg and "%" in msg):
                sys.stdout.write(f"\r   {msg}    ")
                sys.stdout.flush()
            else:
                print(msg, flush=True)
        process.wait()
        res = os.path.join(workdir, os.path.splitext(os.path.basename(video_path))[0] + ".srt")
        if os.path.exists(res):
            en_res = res.replace(".srt", ".en.srt")
            if not os.path.exists(en_res):
                shutil.copy2(res, en_res)
                print(f"✅ Created copy: {os.path.basename(en_res)}")
            return res
        return None
    except: return None

def merge_bilingual(src_srt, zh_srt, main_lang="cn", llm_model=DEFAULTS.get("llm_model", "glm-5.3")):
    print(f"🔀 Smart-Merging into bilingual SRT...")
    bi_path = src_srt[:-7] + ".bi.srt" if src_srt.lower().endswith(".en.srt") else src_srt.replace(".srt", ".bi.srt")
    
    # Check if a healthy bi_path already exists
    if os.path.exists(bi_path) and os.path.getsize(bi_path) > 100:
        with open(bi_path, 'r', encoding='utf-8', errors='ignore') as f:
            if "[UNTRANSLATED]" not in f.read(): 
                print(f"✅ Reusing existing bilingual file: {os.path.basename(bi_path)}")
                return bi_path
            
    cmd = list(SUBTRANSLATOR_CMD) + ["merge", src_srt, "--translated-file", zh_srt]
    env = os.environ.copy(); env["GEMINI_MODEL"] = llm_model
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env, creationflags=CREATE_NO_WINDOW)
        for line in process.stdout or []:
            msg = line.strip()
            if "Progress:" in msg:
                sys.stdout.write(f"\r   {msg}    ")
                sys.stdout.flush()
            else:
                print(f"   [Merge] {msg}", flush=True)
        sys.stdout.write("\n")
        process.wait()
        
        if process.returncode == 0 and os.path.exists(bi_path):
            return bi_path
        else:
            print(f"❌ Merge process failed with code {process.returncode}")
            return None
    except Exception as e:
        print(f"❌ Merge launch error: {e}")
        return None

def burn_subtitle(video_path, srt_path, layout, main_lang, cn_font, en_font, ja_font, cn_size, en_size, cn_color, en_color, bg_box=True, quality="high", ja_size=None):
    print("🔥 Burning subtitles...", flush=True)
    base_srt, _ = os.path.splitext(srt_path)
    ass_path = base_srt + ".ass"
     
    # --- Get Video Dimensions for Styling ---
    width, height = get_video_dimensions(video_path)
    print(f"📐 Video Resolution: {width}x{height}")
 
    if not os.path.exists(ass_path):
         cmd = list(SRT2ASS_CMD) + [srt_path, ass_path, "--layout", layout, "--main-lang", main_lang, "--cn-font", cn_font, "--en-font", en_font, "--ja-font", ja_font, "--cn-size", cn_size, "--en-size", en_size, "--cn-color", cn_color, "--en-color", en_color]
         if ja_size: cmd += ["--ja-size", str(ja_size)]
         cmd += ["--width", str(width), "--height", str(height)]
         if not bg_box: cmd.append("--no-bg-box")
         subprocess.run(cmd, check=True, creationflags=CREATE_NO_WINDOW)
        
    video_base = os.path.splitext(os.path.basename(video_path))[0]
    video_ext = os.path.splitext(video_path)[1]
    out_video = os.path.join(os.path.dirname(srt_path), video_base + "_hardsub" + video_ext)
    
    # --- Critical: Prevent File Locking issues ---
    if os.path.exists(out_video):
        try:
            os.remove(out_video)
        except PermissionError:
            print(f"❌ Error: Output file is LOCKED: {os.path.basename(out_video)}")
            print(f"   Please close any video players or run: Stop-Process -Name ffmpeg -Force")
            return None
        except Exception as e:
            print(f"⚠️ Warning: Could not remove existing output: {e}")

    cmd = list(BURNSUB_CMD) + [video_path, ass_path, out_video, "--headless", "--quality", quality]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
        last_logs = []
        for line in process.stdout or []:
            msg = line.strip()
            if msg:
                if "Progress:" in msg:
                    sys.stdout.write(f"\r   {msg}    ")
                    sys.stdout.flush()
                else:
                    print(f"   [Burn] {msg}", flush=True)
                last_logs.append(msg)
                if len(last_logs) > 50: last_logs.pop(0)
        sys.stdout.write("\n")
        
        process.wait()
        if process.returncode != 0:
            print(f"❌ Burn failed with exit code {process.returncode}")
            # If logs didn't print or were short, show them again
            if not last_logs: print("   No log output captured.")
            raise subprocess.CalledProcessError(process.returncode, cmd)
        else:
            try:
                marker_path = os.path.join(os.path.dirname(out_video), ".burn_complete")
                with open(marker_path, "w", encoding="utf-8") as f:
                    f.write("1")
            except:
                pass
            
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError): raise e
        print(f"❌ Launch Error: {e}")
        return None
    return out_video if os.path.exists(out_video) else None

def get_video_duration(path):
    try:
        cmd = [FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        return float(subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode().strip())
    except: return 0

def get_video_dimensions(path):
    """Returns (width, height) using ffprobe."""
    try:
        cmd = [FFPROBE_EXE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path]
        out = subprocess.check_output(cmd, creationflags=CREATE_NO_WINDOW).decode().strip()
        if "x" in out:
            w, h = out.split("x")
            return int(w), int(h)
    except: pass
    return 1920, 1080

def is_audio_only(path):
    """Returns True if the file is audio-only (no burnable video stream).

    Burning subtitles into an audio file is impossible: the container cannot
    hold a burned-in video track, and the only "video" stream, if any, is a
    single cover-art frame (attached pic). The burn stage is skipped for these
    inputs; SRT/ASS deliverables are already complete by this point.
    """
    AUDIO_EXTS = ('.mp3', '.m4a', '.wav', '.aac', '.flac', '.ogg', '.opus',
                  '.m4b', '.wma', '.aiff', '.aif')
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS

def extract_cover_art(audio_path, out_path):
    """Extract embedded cover art (attached pic) from an audio file to out_path.
    Falls back to the bundled autosub logo if the file has no cover art.
    Returns out_path on success, None if neither cover nor fallback exists."""
    try:
        cmd = [FFMPEG_EXE, "-y", "-i", audio_path, "-map", "0:v", "-frames:v", "1", "-update", "1", out_path]
        subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception:
        pass
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autosub.png")
    if os.path.exists(fallback):
        try:
            shutil.copy2(fallback, out_path)
            return out_path
        except Exception:
            pass
    return None


def burn_audio_subtitles(audio_path, srt_path, layout, main_lang, cn_font, en_font, ja_font, cn_size, en_size, cn_color, en_color, bg_box=True, quality="high", ja_size=None):
    """Burn subtitles into an audio-only file by looping the embedded cover art
    into a video stream and rendering the ASS subtitles onto it. Output is an MP4
    (so the result is uploadable to Bilibili etc.). Returns output path or None."""
    print("🔥 Burning subtitles into audio (cover-art video)...", flush=True)
    import uuid
    uid = uuid.uuid4().hex[:8]
    workdir = os.path.dirname(srt_path)
    base_srt, _ = os.path.splitext(srt_path)
    ass_path = base_srt + ".ass"
 
    width, height = get_video_dimensions(audio_path)  # cover-art dims (e.g. 500x500)
    print(f"📐 Cover Resolution: {width}x{height}")
 
    if not os.path.exists(ass_path):
         cmd = list(SRT2ASS_CMD) + [srt_path, ass_path, "--layout", layout, "--main-lang", main_lang,
                                    "--cn-font", cn_font, "--en-font", en_font, "--ja-font", ja_font,
                                    "--cn-size", cn_size, "--en-size", en_size,
                                    "--cn-color", cn_color, "--en-color", en_color,
                                    "--width", str(width), "--height", str(height)]
         if ja_size: cmd += ["--ja-size", str(ja_size)]
         if not bg_box: cmd.append("--no-bg-box")
         subprocess.run(cmd, check=True, creationflags=CREATE_NO_WINDOW)

    cover_path = os.path.join(workdir, f"tmp_cover_{uid}.jpg")
    if not extract_cover_art(audio_path, cover_path):
        print("❌ No cover art in audio and no fallback image available; cannot burn.")
        return None

    base = os.path.splitext(os.path.basename(audio_path))[0]
    out_video = os.path.join(workdir, base + "_hardsub.mp4")
    if os.path.exists(out_video):
        try: os.remove(out_video)
        except Exception: pass

    temp_ass_name = f"tmp_sub_{uid}.ass"
    temp_ass_path = os.path.join(workdir, temp_ass_name)
    try:
        shutil.copy2(ass_path, temp_ass_path)
    except Exception as e:
        print(f"❌ Error copying subtitle: {str(e)[:60]}")
        return None
    escaped_ass = temp_ass_name.replace('\\', '/').replace(':', '\\:')
    vf = f"ass=filename='{escaped_ass}'"

    crf = {"lossless": "12", "high": "20", "standard": "23"}.get(str(quality).lower(), "20")
    total_dur = get_video_duration(audio_path)

    cmd = [
        FFMPEG_EXE, "-y",
        "-loop", "1", "-i", os.path.abspath(cover_path),
        "-i", os.path.abspath(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
        "-tune", "stillimage", "-r", "5", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        os.path.abspath(out_video),
    ]

    print(f"📦 Cover: {os.path.basename(cover_path)} | Subtitle: {os.path.basename(ass_path)} | Output: {os.path.basename(out_video)}")

    process = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
    last_logs = []
    for line in process.stdout or []:
        msg = line.strip()
        if not msg: continue
        if total_dur > 0:
            m = re.search(r'time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)', msg)
            if m:
                cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = min(100.0, cur / total_dur * 100.0)
                sys.stdout.write(f"\r   Progress: {pct:5.1f}% (Burning...)    ")
                sys.stdout.flush()
                continue
        print(f"   [Burn] {msg}", flush=True)
        last_logs.append(msg)
        if len(last_logs) > 50: last_logs.pop(0)
    sys.stdout.write("\n")
    process.wait()

    for p in (cover_path, temp_ass_path):
        try: os.remove(p)
        except Exception: pass

    if process.returncode != 0:
        print(f"❌ Burn failed with exit code {process.returncode}")
        if not last_logs: print("   No log output captured.")
        return None

    try:
        with open(os.path.join(workdir, ".burn_complete"), "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass
    print(f"✅ Audio hardsub created: {os.path.basename(out_video)}")
    return out_video if os.path.exists(out_video) else None

def validate_cookies_file(path):
    """Checks if a cookie file exists and has a valid Netscape format header."""
    if not path or not os.path.exists(path): return False, "文件不存在"
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline()
            if "# Netscape HTTP Cookie File" in first_line or "# HTTP Cookie File" in first_line:
                return True, "有效的 Netscape 格式"
            content = f.read(1024)
            if "\t" in content and content.count("\t") > 5:
                return True, "疑似有效的格式 (缺少文件头)"
            return False, "非标准 Cookie 格式"
    except Exception as e:
        return False, f"读取失败: {e}"

def confirm_cookies(args):
    """Auto-detects and confirms cookies with the user (CLI)."""
    if getattr(args, 'cookies', None) and args.cookies.lower() == "none":
        args.cookies = "none"
        return "none"
    if getattr(args, 'cookies', None) and args.cookies.lower() in {"chrome", "firefox", "safari", "edge"}:
        return args.cookies
    search_paths = []
    if getattr(args, 'cookies', None): search_paths.append(args.cookies)
    
    # Priority defaults
    paths_to_check = [
        r"D:\download\cookies.txt",
        r"D:\Downloads\cookies.txt",
        r"D:\cc\cookies.txt",
        os.path.join(os.getcwd(), "cookies.txt"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    ]
    
    for p in paths_to_check:
        if p not in search_paths:
            search_paths.append(p)

    found_path = None
    for p in search_paths:
        if os.path.exists(p):
            found_path = p
            break
    
    if not found_path:
        # Don't prompt for local files
        is_url = args.input.startswith("http") if hasattr(args, 'input') else False
        if is_url:
            print("\n[bold yellow]⚠️  警告: 未找到 Cookie 文件。[/bold yellow]")
            print("   如果处理 YouTube 视频，可能会遇到机器人检测错误。")
            time.sleep(1)
        return None

    is_valid, reason = validate_cookies_file(found_path)
    mtime = datetime.fromtimestamp(os.path.getmtime(found_path)).strftime('%Y-%m-%d %H:%M:%S')
    size_kb = os.path.getsize(found_path) / 1024

    print(f"\n🍪 [Cookies Check] 检测到相关文件:")
    print(f"   路径: {found_path}")
    print(f"   时间: {mtime} ({size_kb:.1f} KB)")
    print(f"   状态: {reason}")

    # No prompt if it's not a URL (local file processing doesn't need cookies)
    is_url = args.input.startswith("http") if hasattr(args, 'input') else False
    if not is_url:
        args.cookies = found_path
        return found_path

    print("\n   [Confirm] 按回车键直接确认 (5s)，按 'N' 取消任务，或输入新路径:")
    
    start_wait = time.time()
    user_input = ""
    while time.time() - start_wait < 5:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'\r', b'\n'): break
            if ch.lower() == b'n':
                print("\n❌ 任务取消。")
                sys.exit(0)
            try:
                char = ch.decode('utf-8')
                if char == '\x08': user_input = user_input[:-1]
                else: user_input += char
                sys.stdout.write(f"\r   新路径: {user_input} ")
                sys.stdout.flush()
            except: pass
        time.sleep(0.01)
    
    if user_input.strip():
        new_path = user_input.strip().strip('"')
        if os.path.exists(new_path):
            args.cookies = new_path
            print(f"\n   -> 已切换: {new_path}")
        else:
            print(f"\n   -> 路径无效，默认使用: {found_path}")
            args.cookies = found_path
    else:
        args.cookies = found_path
        print("\n   -> 已确认继续。")
    
    return args.cookies

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", default="", help="Input video URL or local file")
    
    # Batch mode arguments
    parser.add_argument("--batch-urls", nargs="+", help="Multiple URLs or path to a .txt/.md file containing URLs")
    parser.add_argument("--batch-dir", help="Directory containing multiple video project folders to process automatically")
    parser.add_argument("--workers", type=int, default=5, help="Number of concurrent workers max 10")
    parser.add_argument("--max-api-calls", type=int, default=20, help="Global concurrency limit for Translation APIs")

    parser.add_argument("--model", default=DEFAULTS.get("whisper_model", DEFAULTS.get("model", "large-v2")))
    default_llm_model, default_llm_provider = _load_llm_defaults()
    parser.add_argument("--llm-model", default=default_llm_model, help="LLM Model")
    parser.add_argument("--llm-provider", default=default_llm_provider, help="Explicit LLM Provider (e.g., dashscope, vertex)")
    parser.add_argument("--style", default=DEFAULTS.get("style", "auto"))
    parser.add_argument("--trans-mode", default=DEFAULTS.get("trans_mode", "balanced"), choices=["paraphrase", "balanced"])
    parser.add_argument("--cookies")
    parser.add_argument("--cookies-from-browser", choices=["chrome", "firefox", "safari", "edge"], help="Read cookies from a browser profile")
    parser.add_argument("--layout", default=DEFAULTS.get("layout", "bilingual"))
    parser.add_argument("--main-lang", default=DEFAULTS.get("main_lang", "cn"))
    parser.add_argument("--cn-font", default=DEFAULTS.get("cn_font", "KaiTi"))
    parser.add_argument("--en-font", default=DEFAULTS.get("en_font", "Arial"))
    parser.add_argument("--ja-font", default=DEFAULTS.get("ja_font", "Hiragino Sans"))
    parser.add_argument("--cn-size", default=DEFAULTS.get("cn_size", "60"))
    parser.add_argument("--en-size", default=DEFAULTS.get("en_size", "36"))
    parser.add_argument("--ja-size", default=DEFAULTS.get("ja_size", "36"), help="Japanese font size")
    parser.add_argument("--cn-color", default=DEFAULTS.get("cn_color", "Gold"))
    parser.add_argument("--en-color", default=DEFAULTS.get("en_color", "White"))
    parser.add_argument("--no-bg-box", action="store_true")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--output-dir", help="Project root directory for output files")
    parser.add_argument("--project-name", help="Dummy argument to prevent crashes")
    parser.add_argument("--sync-gdrive", help="Google Drive Folder ID to sync output files to")
    parser.add_argument("--bili-upload", action="store_true", help="Upload the hardsub video to Bilibili (auto-generates tid/tags/desc/season per video)")
    parser.add_argument("--bili-cover-title", help="Text rendered on Bilibili cover images (default: subtitle-grounded form title)")
    parser.add_argument("--wechat-upload", action="store_true", help="Upload the hardsub video to WeChat Channels as a draft (use --wechat-public to publish)")
    parser.add_argument("--wechat-public", action="store_true", help="Publish the WeChat Channels upload publicly instead of saving a draft")
    parser.add_argument("--wechat-cover-title", help="Text rendered on WeChat cover images (default: subtitle-grounded form title)")
    parser.add_argument("--quick", action="store_true", help="Experimental: Quick Transcribe (YouTube Subs + LLM)")
    parser.add_argument("--start", help="Start time (e.g. 00:01:30 or 90)")
    parser.add_argument("--end", help="End time (e.g. 00:03:00 or 180)")
    parser.add_argument("--quality", default="high", choices=["standard", "high", "lossless"], help="Burn quality (standard, high, lossless)")
    parser.add_argument("--upload-only", action="store_true", help="Skip transcription/translation/burn; only upload existing hardsub video to GDrive/Bilibili")
    parser.add_argument("--multilang", action="store_true", help="Enable per-segment language detection for mixed-language videos")
    args = parser.parse_args()

    if args.cookies_from_browser:
        args.cookies = args.cookies_from_browser
    
    # --- Intelligent Time Parsing ---
    if args.start and "-" in args.start and not args.end:
        parts = args.start.split("-")
        args.start = parts[0].strip()
        args.end = parts[1].strip()
        print(f"✂️  Parsed time range: {args.start} to {args.end}")

    # Cookie verification step for CLI
    confirm_cookies(args)

    # Determine if we should run batch mode
    if args.batch_urls or args.batch_dir:
        import autosub_batch  # type: ignore
        return autosub_batch.run_batch(args)
        
    if not args.input:
        return parser.print_help()

    # --- Directory Logic ---
    final_output_dir = args.output_dir or DEFAULTS.get("output_dir") or BASE_OUTPUT_DIR
    if not os.path.isabs(final_output_dir):
        final_output_dir = os.path.abspath(os.path.join(PROJECT_ROOT, final_output_dir))
    
    # Update global-like state for this run
    effective_base_output_dir = final_output_dir

    # 0. Intelligent Project Naming
    if args.input.startswith("http"):
        # Local, instantaneous extraction of YouTube Video ID
        video_id = None
        if "youtube.com" in args.input or "youtu.be" in args.input:
            match = re.search(r'(?:v=|\/|embed\/|shorts\/)([a-zA-Z0-9_-]{11})', args.input)
            if match:
                video_id = match.group(1)
        elif "x.com" in args.input or "twitter.com" in args.input:
            # X/Twitter status ID (numeric, variable length).
            # NOTE: This is the TWEET STATUS ID, not the video ID that yt-dlp
            # uses internally. yt-dlp's %(id)s is the X video ID, which differs
            # from the status ID in the URL. We use the status ID only as a
            # first-pass hint for folder lookup; if it fails, we fall through
            # to yt-dlp --get-folder-name which returns the correct video ID.
            match = re.search(r'/status/(\d+)', args.input)
            if match:
                video_id = match.group(1)
        
        # Check if a folder ending with [{video_id}] already exists locally
        existing_dir = None
        if video_id and os.path.exists(effective_base_output_dir):
            for item in os.listdir(effective_base_output_dir):
                item_path = os.path.join(effective_base_output_dir, item)
                if os.path.isdir(item_path) and item.endswith(f"[{video_id}]"):
                    existing_dir = item_path
                    break
        
        # For X/Twitter, the status ID from the URL != yt-dlp's video ID.
        # If the folder lookup failed, try yt-dlp --get-folder-name which
        # returns the correct video ID. This is slower (~15s for X) but
        # essential to avoid re-downloading already-completed videos.
        if not existing_dir and ("x.com" in args.input or "twitter.com" in args.input):
            print(f"🔍 Folder lookup by status ID failed. Fetching real video ID from yt-dlp (timeout 35s)...", flush=True)
            real_folder_name = get_video_folder_name(args.input, args.cookies)
            if real_folder_name:
                candidate_dir = os.path.join(effective_base_output_dir, real_folder_name)
                if os.path.exists(candidate_dir) and os.path.isdir(candidate_dir):
                    existing_dir = candidate_dir
                    video_id = real_folder_name.split("[")[-1].rstrip("]") if "[" in real_folder_name else video_id
        
        if existing_dir:
            workdir = existing_dir
            print(f"📁 Found existing Project Folder locally: {os.path.basename(workdir)}")
            title = get_video_title(args.input, args.cookies)
        else:
            print(f"🔍 Fetching video details (timeout in 35s)...", flush=True)
            folder_name = get_video_folder_name(args.input, args.cookies)
            title = get_video_title(args.input, args.cookies)
            if folder_name:
                workdir = os.path.join(effective_base_output_dir, folder_name)
                print(f"📁 Project Folder: {folder_name}")
            elif title:
                # Replicate stable restricted naming in Python fallback
                t_clean = title.replace(":", " - ").replace("|", " - ")
                t_clean = re.sub(r'\s+', '_', t_clean)
                t_clean = re.sub(r'[^a-zA-Z0-9_.-]', '', t_clean)
                t_clean = re.sub(r'_+', '_', t_clean).strip('_')
                
                if video_id:
                    folder_name_fallback = f"{t_clean} [{video_id}]"
                else:
                    folder_name_fallback = t_clean
                    
                workdir = os.path.join(effective_base_output_dir, folder_name_fallback)
                print(f"📁 Project Folder (fallback match): {folder_name_fallback}")
            else:
                workdir = get_workdir(args.input, effective_base_output_dir)
    else:
        workdir = get_workdir(args.input, effective_base_output_dir)

    if not os.path.exists(workdir): os.makedirs(workdir, exist_ok=True)
    
    # --- Initialize Workflow Logging ---
    log_path = os.path.join(workdir, "workflow.log")
    logger = Logger(log_path)
    sys.stdout = logger
    sys.stderr = logger

    # --- Upload-Only Mode ---
    # Skip the entire pipeline (transcribe, translate, burn) and jump straight
    # to GDrive sync / Bilibili upload. Use this when a previous run produced
    # the hardsub video but the upload step was skipped or failed.
    if getattr(args, 'upload_only', False):
        print(f"📤 Upload-only mode: {args.input}", flush=True)
        hardsub_path = None
        for f in os.listdir(workdir):
            if "hardsub" in f and f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.ts')):
                hardsub_path = os.path.join(workdir, f)
                break
        if not hardsub_path:
            print(f"❌ No hardsub video found in {workdir}", flush=True)
            return
        print(f"✅ Found hardsub: {os.path.basename(hardsub_path)}", flush=True)
        run_post_processing_uploads(
            workdir,
            sync_gdrive=getattr(args, 'sync_gdrive', None),
            bili_upload=getattr(args, 'bili_upload', False),
            wechat_upload=getattr(args, 'wechat_upload', False),
            headless=args.headless,
            bili_cover_title=getattr(args, 'bili_cover_title', None),
            wechat_cover_title=getattr(args, 'wechat_cover_title', None),
            wechat_public=getattr(args, 'wechat_public', False),
            llm_model=getattr(args, 'llm_model', None),
            llm_provider=getattr(args, 'llm_provider', None),
        )
        print("✅ Upload-only complete!", flush=True)
        return

    print(f"--- 任务启动: {args.input} ---")
    print(f"🏠 [DEV MODE] Root: {PROJECT_ROOT}")
    print(f"📦 Found FFmpeg at: {FFMPEG_EXE}")

    if args.input.startswith("http"):
        # Always download full video first (more robust/fast than --download-sections for live streams)
        video_path = download_video(args.input, workdir, args.cookies, quick=args.quick, title=title)
        
        # Dynamic project folder renaming if title was not pre-fetched (temp fallback case)
        if video_path and os.path.exists(video_path) and not title:
            # Extract real video title from downloaded file
            actual_filename = os.path.basename(video_path)
            # Remove extension first
            name_without_ext = os.path.splitext(actual_filename)[0]
            # Keep the ID but strip only the last bracketed section (e.g. resolution "[1080p]")
            nice_title = re.sub(r'\s*\[[^\]]+\]\s*$', '', name_without_ext)
            nice_title = sanitize_filename(nice_title)
            
            if nice_title and not nice_title.startswith("temp_"):
                new_workdir = os.path.join(effective_base_output_dir, nice_title)
                # If target directory already exists, append a timestamp
                if os.path.exists(new_workdir) and new_workdir.lower() != workdir.lower():
                    new_workdir = new_workdir + "_" + str(int(time.time()))
                
                print(f"📁 Video downloaded. Dynamically upgrading project folder to: {os.path.basename(new_workdir)}", flush=True)
                workdir, video_path, logger = rename_workdir_safely(workdir, new_workdir, video_path, logger)
    else:
        # For local files, copy to project folder first
        src_path = os.path.abspath(args.input)
        dest_path = os.path.join(workdir, os.path.basename(src_path))
        if os.path.exists(src_path):
            if os.path.abspath(src_path).lower() != os.path.abspath(dest_path).lower():
                print(f"📂 Copying video to project folder...")
                shutil.copy2(src_path, dest_path)
            video_path = dest_path
        elif os.path.exists(dest_path):
            print(f"ℹ️ Original source missing, using video in project folder.")
            video_path = dest_path
        else:
            video_path = src_path

    # Post-acquisition / Clipping
    if (args.start or args.end) and video_path and os.path.exists(video_path):
        src_path = video_path
        ext = os.path.splitext(src_path)[1]
        s_clean = sanitize_filename(args.start or "0")
        e_clean = sanitize_filename(args.end or "end")
        clip_path = os.path.join(workdir, f"clip_{s_clean}_{e_clean}{ext}")
        
        # Skip if clip already exists
        if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1024*1024:
            print(f"ℹ️  Clipped segment already exists, reusing: {os.path.basename(clip_path)}")
            video_path = clip_path
        else:
            print(f"✂️  Clipping segment: {args.start or '0'} -> {args.end or 'end'}")
            time.sleep(1) # Small delay to avoid file lock issues on Windows
            ss = ["-ss", args.start] if args.start else []
            to = ["-to", args.end] if args.end else []
            cmd = [FFMPEG_EXE, "-y"] + ss + ["-i", src_path] + to + ["-c", "copy", clip_path]
            try:
                subprocess.run(cmd, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
                video_path = clip_path
                print(f"✅ Segment clipped: {os.path.basename(clip_path)}")
            except Exception as e:
                print(f"⚠️  Clipping failed: {e}. Using full file instead.")
            
    if not video_path or not os.path.exists(video_path):
        print("❌ Invalid input")
        return 1

    vid_dur = get_video_duration(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    expected_srt = os.path.join(workdir, base + ".srt")
    expected_en = os.path.join(workdir, base + ".en.srt")
    expected_cn = os.path.join(workdir, base + ".cn.srt")

    # 1. Transcription (Sequential)
    src_srt = None
    
    # Priority: 1. .srt (Transcribed) 2. .en.srt (Downloaded)
    possible_sources = [expected_srt, expected_en]
    
    for candidate in possible_sources:
        if os.path.exists(candidate) and os.path.getsize(candidate) > 500:
            if srt_utils:
                if vid_dur > 0:
                    try:
                        srt_dur = srt_utils.get_srt_duration(candidate)
                        if srt_dur > vid_dur * 0.9:
                            print(f"✅ Found existing SRT: {os.path.basename(candidate)} (Duration matches)")
                            src_srt = candidate
                            break
                        else:
                            print(f"⚠️ {os.path.basename(candidate)} duration mismatch ({srt_dur:.1f}s vs {vid_dur:.1f}s).")
                    except Exception as e:
                        print(f"⚠️ Error checking {os.path.basename(candidate)}: {e}")
                else:
                    print(f"✅ Found existing SRT: {os.path.basename(candidate)} (Video duration unknown, skipping transcription)")
                    src_srt = candidate
                    break
            else:
                # If srt_utils is missing, we still trust the file if it's > 500 bytes
                print(f"✅ Found existing SRT: {os.path.basename(candidate)} (srt_utils missing, assuming OK)")
                src_srt = candidate
                break
    
    if not src_srt:
        src_srt = transcribe_video(video_path, workdir, args.model, quick=args.quick, multilang=args.multilang)
    
    if not src_srt:
        print("❌ Transcription failed")
        return 1

    # --- Integrated Acoustic Spellcheck Middleware ---
    has_translation = False
    for candidate_cn in [expected_cn, os.path.join(workdir, base + ".zh.srt")]:
        if os.path.exists(candidate_cn) and os.path.getsize(candidate_cn) > 500:
            if srt_utils:
                try:
                    src_count = len(srt_utils.parse_srt(src_srt))
                    zh_count = len(srt_utils.parse_srt(candidate_cn))
                    if zh_count >= src_count * 0.95:
                        has_translation = True
                        break
                except:
                    pass
            else:
                has_translation = True
                break

    if has_translation:
        print("✅ Translation file already exists, skipping Acoustic Spellcheck Preprocessing.")
    else:
        try:
            py_exe = get_python_exe()
            spellcheck_script = os.path.join(CURRENT_DIR, "spellcheck_srt.py")
            print("🎙️ Running Acoustic Spellcheck Preprocessing...")
            # Spellcheck is a deterministic correction task, not deep reasoning.
            # Use a fast non-reasoning model (spellcheck_model) instead of the
            # heavy llm_model (e.g. glm-5.2 reasoning -> 143s/chunk -> 90s timeout).
            spellcheck_model = DEFAULTS.get("spellcheck_model") or args.llm_model
            cmd = [py_exe, spellcheck_script, src_srt, "--model", spellcheck_model]
            if getattr(args, "llm_provider", None):
                cmd.extend(["--provider", args.llm_provider])
            # Stream output line-by-line into Logger (and workflow.log).
            # subprocess.run(capture_output=False) inherits the underlying fd,
            # bypassing Logger.write() — spellcheck logs vanished from workflow.log.
            sp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in sp.stdout or []:
                print(line.rstrip(), flush=True)
            sp.wait()
            if sp.returncode != 0:
                print(f"⚠️ Acoustic Spellcheck exited with code {sp.returncode}")
        except Exception as e:
            print(f"⚠️ Acoustic Spellcheck failed/skipped: {e}")

    # 2. Translation (Sequential)
    zh_srt = None
    expected_zh = os.path.join(workdir, base + ".zh.srt")
    
    for candidate_cn in [expected_cn, expected_zh]:
        if os.path.exists(candidate_cn) and os.path.getsize(candidate_cn) > 500:
            if srt_utils:
                try:
                    src_count = len(srt_utils.parse_srt(src_srt))
                    zh_count = len(srt_utils.parse_srt(candidate_cn))
                    if zh_count >= src_count * 0.95:
                        print(f"✅ Found existing translation: {os.path.basename(candidate_cn)}")
                        zh_srt = candidate_cn
                        break
                except Exception as e:
                    print(f"⚠️ Error parsing {os.path.basename(candidate_cn)}: {e}")
            else:
                zh_srt = candidate_cn
                break

    if not zh_srt:
        print("🌍 Smart-translating...")
        try:
            py_exe = get_python_exe()
            cmd = [py_exe, SMART_TRANSLATE_CMD[1], src_srt, "--style", args.style, "--model", args.llm_model, "--trans-mode", getattr(args, "trans_mode", "balanced")]
            if getattr(args, "llm_provider", None):
                cmd.extend(["--provider", args.llm_provider])
            # Stream output in real-time so the user sees progress
            # (translation can take several minutes with reasoning models)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in proc.stdout or []:
                line = line.rstrip('\n')
                if line:
                    print(line, flush=True)
            proc.wait()
            if proc.returncode != 0:
                print(f"⚠️ Translation exited with code {proc.returncode}")
            if os.path.exists(expected_cn): zh_srt = expected_cn
            elif os.path.exists(expected_zh): zh_srt = expected_zh
        except Exception as e:
            print(f"⚠️ Translation step error: {e}")
    
    if not zh_srt:
        print("❌ Translation failed")
        return 1

    # 3. Merge & Burn
    final_srt = zh_srt
    if args.layout == "bilingual":
        print(f"🔀 Merging {os.path.basename(src_srt)} and {os.path.basename(zh_srt)}...")
        merged = merge_bilingual(src_srt, zh_srt, args.main_lang, args.llm_model)
        if merged and os.path.exists(merged):
            final_srt = merged
        else:
            # Check if there's an existing .bi.srt anyway (maybe created but returned None due to error code)
            bi_path = src_srt[:-7] + ".bi.srt" if src_srt.lower().endswith(".en.srt") else src_srt.replace(".srt", ".bi.srt")
            if os.path.exists(bi_path) and os.path.getsize(bi_path) > 500:
                print(f"✅ Using legacy/existing bilingual file: {os.path.basename(bi_path)}")
                final_srt = bi_path
            else:
                print(f"⚠️ Bilingual merge failed. Falling back to primary translation: {os.path.basename(zh_srt)}")
                final_srt = zh_srt
    
    print(f"📍 Final subtitle for burning: {os.path.basename(final_srt)}", flush=True)
    
    video_base = os.path.splitext(os.path.basename(video_path))[0]
    marker_file = os.path.join(workdir, ".burn_complete")
    
    if is_audio_only(video_path):
        # Audio-only inputs have no burnable video stream. Synthesize one from
        # the embedded cover art (looped over the audio) and burn subtitles into
        # an MP4 so the result is uploadable (e.g. to Bilibili).
        out_video = os.path.join(workdir, video_base + "_hardsub.mp4")
        if os.path.exists(out_video) and os.path.getsize(out_video) > 1024 * 1024 and os.path.exists(marker_file):
            print(f"✅ Found existing hardsub: {os.path.basename(out_video)} (Skipping burn stage)")
        else:
            burn_audio_subtitles(video_path, final_srt, args.layout, args.main_lang, args.cn_font, args.en_font, args.ja_font, args.cn_size, args.en_size, args.cn_color, args.en_color, not args.no_bg_box, args.quality, args.ja_size)
    else:
       # Check if hardsub already exists (video path)
       video_ext = os.path.splitext(video_path)[1]
       out_video = os.path.join(workdir, video_base + "_hardsub" + video_ext)
       if os.path.exists(out_video) and os.path.getsize(out_video) > 1024 * 1024 and os.path.exists(marker_file):
            print(f"✅ Found existing hardsub: {os.path.basename(out_video)} (Skipping burn stage)")
       else:
            burn_subtitle(video_path, final_srt, args.layout, args.main_lang, args.cn_font, args.en_font, args.ja_font, args.cn_size, args.en_size, args.cn_color, args.en_color, not args.no_bg_box, args.quality, args.ja_size)
    
    print("✅ All done!", flush=True)

    # --- Parallel Post-Processing Uploads ---
    run_post_processing_uploads(
        workdir,
        sync_gdrive=getattr(args, 'sync_gdrive', None),
        bili_upload=getattr(args, 'bili_upload', False),
        wechat_upload=getattr(args, 'wechat_upload', False),
        headless=args.headless,
        bili_cover_title=getattr(args, 'bili_cover_title', None),
        wechat_cover_title=getattr(args, 'wechat_cover_title', None),
        wechat_public=getattr(args, 'wechat_public', False),
        llm_model=getattr(args, 'llm_model', None),
        llm_provider=getattr(args, 'llm_provider', None),
    )

    return 0

if __name__ == "__main__":
    sys.exit(main())
