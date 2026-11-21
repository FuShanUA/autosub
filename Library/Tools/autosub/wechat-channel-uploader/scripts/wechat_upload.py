#!/usr/bin/env python3
"""
微信视频号自动上传工具
基于 Playwright 持久化 browser context，实现扫码登录态保持与自动化上传。

用法:
    # 首次扫码登录
    python wechat_upload.py --login

    # 单个上传
    python wechat_upload.py "/path/to/video.mp4" "视频标题"
    python wechat_upload.py "/path/to/video.mp4" "标题" --desc "描述" --tags "标签1,标签2" --public

    # 批量上传
    python wechat_upload.py --batch /path/to/dir --pattern "*hardsub*.mp4"
"""

import argparse
import asyncio
import base64
import fcntl
import glob
import json
import math
import os
import re
import sys
import subprocess
import shutil
import time
import tempfile
import io

from PIL import Image, ImageChops, ImageOps

# --- 路径配置 ---
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER_DATA_DIR = os.environ.get(
    "WECHAT_BROWSER_DATA_DIR",
    os.path.join(SKILL_DIR, "assets", "browser_data"),
)
_BROWSER_DATA_REAL_PATH = os.path.realpath(BROWSER_DATA_DIR)
BROWSER_LOCK_PATH = os.environ.get(
    "WECHAT_BROWSER_LOCK_PATH",
    os.path.join(
        os.path.dirname(_BROWSER_DATA_REAL_PATH),
        os.path.basename(_BROWSER_DATA_REAL_PATH) + ".lock",
    ),
)
QUEUE_PATH = os.path.join(SKILL_DIR, "assets", "failed_queue.json")

# Upload metadata has local fallbacks, so avoid long LLM retry chains here.
os.environ.setdefault("LLM_REQUEST_TIMEOUT", "30")
os.environ.setdefault("LLM_MAX_RETRIES", "1")


class _BrowserProfileLock:
    """Serialize all Playwright users of one persistent WeChat profile."""

    def __init__(self):
        self.path = BROWSER_LOCK_PATH
        self._fd = None

    async def acquire(self):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._acquire)

    def _acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        waited = 0
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited == 0:
                    print(
                        ">>> 另一个微信上传/登录进程正在使用浏览器 profile，本进程排队等待..."
                    )
                elif waited % 15 == 0:
                    print(f">>> 仍在等待微信浏览器 profile 锁（{waited} 秒）...")
                time.sleep(1)
                waited += 1
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))
        print(f">>> 已获得微信浏览器 profile 锁: {self.path} (pid={os.getpid()})")
        return self

    async def release(self):
        if self._fd is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._release)

    def _release(self):
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

# 复用统一 LLM 接口（与 bili_upload.py 对齐）
# Prefer the common package inside this repository (same rule as autosub.py):
# the external Tools/common copy can lag behind repo-side fixes such as the
# composition-aware cover frame selection.
_COMMON_DIR = os.environ.get("AUTOSUB_COMMON_DIR")
if not _COMMON_DIR:
    _COMMON_DIR = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "common")
    )
if not os.path.isdir(_COMMON_DIR):
    _COMMON_DIR = "/Users/shanfu/cc/Library/Tools/common"
if os.path.isdir(_COMMON_DIR) and _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)
if not os.path.isfile(os.path.join(_COMMON_DIR, "subtitle_title.py")):
    _SECONDARY_COMMON_DIR = "/Users/shanfu/cc/Library/Tools/common"
    if os.path.isdir(_SECONDARY_COMMON_DIR) and _SECONDARY_COMMON_DIR not in sys.path:
        # Keep repository-side cover_utils first, but allow the shared
        # subtitle_title module to be found in nested checkout layouts.
        sys.path.insert(1, _SECONDARY_COMMON_DIR)
try:
    from llm_utils import get_client as _get_llm_client, LLMProvider as _LLMProvider  # type: ignore
except Exception:
    _get_llm_client = None
    _LLMProvider = None
try:
    from cover_utils import (
        cover_style_matches,
        generate_platform_covers,
    )
    _COVER_UTILS_AVAILABLE = True
except Exception:
    _COVER_UTILS_AVAILABLE = False
try:
    from subtitle_title import (
        SubtitleTitleError,
        build_subtitle_grounded_title,
        read_full_subtitle_text,
        read_subtitle_text as read_strict_subtitle_text,
        validate_title_from_subtitle,
    )
    _SUBTITLE_TITLE_AVAILABLE = True
except Exception:
    SubtitleTitleError = ValueError
    _SUBTITLE_TITLE_AVAILABLE = False

# 微信视频号创作者平台
WECHAT_CREATOR_URL = "https://channels.weixin.qq.com/platform/home"
WECHAT_LOGIN_URL = "https://channels.weixin.qq.com/platform"
WECHAT_HOME_URL = "https://channels.weixin.qq.com/platform/home"
WECHAT_DRAFT_URL = "https://channels.weixin.qq.com/post/draftListManager"
WECHAT_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
WECHAT_DRAFT_URL = "https://channels.weixin.qq.com/platform/post/draftListManager"
WECHAT_TITLE_MAX_CHARS = 16
WECHAT_TITLE_FALLBACK = "未命名视频"
# The creator platform is on a domestic direct route, while the system DNS may
# point at the VPN resolver. First-page DOM loading can exceed 30s on that path.
WECHAT_PAGE_LOAD_TIMEOUT_MS = 60000


async def _goto_wechat_page(page, url):
    """Open a WeChat creator-platform route with the shared load timeout."""
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=WECHAT_PAGE_LOAD_TIMEOUT_MS,
    )


def _is_noisy_wechat_request_failure(url, failure):
    """Filter browser-cancelled requests that do not affect the upload."""
    lower_url = str(url).lower()
    failure_text = str(failure or "").lower()
    if lower_url.startswith("blob:"):
        return True
    if "report-perf" in lower_url:
        return True
    return (
        "stodownload" in lower_url
        and "err_aborted" in failure_text
    )


def _find_ffprobe():
    """Find ffprobe even when the GUI launches Python with a restricted PATH."""
    found = shutil.which("ffprobe")
    if found:
        return found
    if sys.platform == "darwin":
        for bin_dir in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
            candidate = os.path.join(bin_dir, "ffprobe")
            if os.path.exists(candidate):
                return candidate
    return "ffprobe"


def _get_video_duration_seconds(path):
    """Read container duration with ffprobe; return 0 when unavailable."""
    try:
        result = subprocess.run(
            [
                _find_ffprobe(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _within_wechat_duration_limit(path):
    """Reject only unreadable durations; WeChat no longer caps uploads at 10 minutes."""
    duration = _get_video_duration_seconds(path)
    if duration <= 0:
        print(f">>> ❌ 无法读取视频时长，已停止上传: {path}")
        return False
    return True


def _autosub_llm_default():
    """读 autosub settings.json，返回 (model, provider_str)。"""
    candidates = [
        os.environ.get("AUTOSUB_SETTINGS"),
        "/Users/shanfu/cc/Library/Tools/autosub/settings.json",
    ]
    for cand in filter(None, candidates):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                st = json.load(f)
            model = (st.get("llm_model") or "").strip()
            vendor = (st.get("llm_vendor") or "").strip()
            _VENDOR_TO_PROVIDER = {
                "Google (Vertex AI)": "vertex", "Google (Gemini API)": "gemini",
                "OpenAI": "openai", "Anthropic": "anthropic",
                "DeepSeek": "deepseek", "Alibaba (Bailian)": "dashscope",
                "DashScope (Alibaba Cloud)": "dashscope",
                "Zhipu AI": "zhipu", "Moonshot (Kimi)": "moonshot",
                "SiliconFlow": "siliconflow", "MiniMax": "minimax",
                "Google Vertex AI": "vertex", "Google Gemini": "gemini",
                "Silicon Flow": "siliconflow", "Zhipu (GLM)": "zhipu",
            }
            prov = _VENDOR_TO_PROVIDER.get(vendor)
            if model and prov:
                return model, prov
        except Exception:
            continue
    return "glm-5.2", "dashscope"


def _read_subtitle_text(video_path, max_chars=6000):
    """读取与视频同目录的字幕纯文本，优先双语/中文字幕。返回 (text, source)。"""
    base_name = os.path.splitext(video_path)[0]
    orig_base = base_name.replace("_hardsub", "")
    vdir = os.path.dirname(video_path) or "."
    candidates = []
    for bn in (base_name, orig_base):
        for ext in (".bi.srt", ".cn.srt", ".zh.srt", ".srt", ".txt"):
            p = bn + ext
            if os.path.exists(p) and p not in candidates:
                candidates.append(p)
    try:
        dir_files = os.listdir(vdir)
    except Exception:
        dir_files = []
    for fn in sorted(dir_files):
        if fn.endswith((".bi.srt", ".cn.srt", ".zh.srt", ".srt")):
            p = os.path.join(vdir, fn)
            if p not in candidates:
                candidates.append(p)

    for sub_path in candidates:
        try:
            with open(sub_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            continue
        if not raw.strip():
            continue
        is_bilingual = sub_path.endswith(".bi.srt")
        lines = []
        text_lines_in_block = 0
        for ln in raw.splitlines():
            stripped = ln.strip()
            if re.match(r"^\d+$", stripped):
                text_lines_in_block = 0
                continue
            if re.match(r"^[\d:.,\-\s>]+$", stripped) and "-->" in stripped:
                continue
            if not stripped:
                text_lines_in_block = 0
                continue
            if is_bilingual:
                text_lines_in_block += 1
                if text_lines_in_block % 2 == 0:
                    continue
            lines.append(stripped)
        text = " ".join(lines).strip()
        if text:
            return text[:max_chars], sub_path
    return None, None


def _summarize_via_llm(subtitle_text, title, source_label, llm_provider="dashscope", llm_model="glm-5.2"):
    """用 LLM 根据字幕生成视频号描述。失败返回 None。"""
    if not _get_llm_client or not _LLMProvider:
        return None
    try:
        client = _get_llm_client()
        prompt = (
            "你是一位微信视频号视频简介撰写助手。根据下面给定的视频字幕内容，"
            "写一段简洁、有吸引力的中文视频描述（80-150 字），概括核心话题与亮点，"
            "语气自然平实，适合视频号传播，"
            "不要加标题、不要用 Markdown、不要加 emoji、不要署名。\n\n"
            f"视频标题：{title}\n\n"
            f"字幕内容（来自 {source_label}）：\n{subtitle_text}\n\n"
            "请直接输出描述正文："
        )
        try:
            prov = _LLMProvider(llm_provider)
        except Exception:
            prov = _LLMProvider.DASHSCOPE
        out = client.generate_content(prompt, provider=prov, model_name=llm_model, fallback=False)
        if out and out.strip():
            return out.strip()
    except Exception as e:
        print(f">>> LLM 生成描述失败: {e}")
    return None


def _generate_tags_via_llm(subtitle_text, title, source_label, llm_provider="dashscope", llm_model="glm-5.2"):
    """用 LLM 根据字幕生成标签。失败返回 None。"""
    if not _get_llm_client or not _LLMProvider:
        return None
    try:
        client = _get_llm_client()
        prompt = (
            "你是一位微信视频号标签专家。根据下面给定的视频字幕内容，"
            "生成 3-5 个适合微信视频号的中文或英文标签（关键词），用逗号分隔，只输出标签本身，不要编号、不要解释。\n\n"
            f"视频标题：{title}\n\n"
            f"字幕内容（来自 {source_label}）：\n{subtitle_text}\n\n"
            "请直接输出标签（逗号分隔）："
        )
        try:
            prov = _LLMProvider(llm_provider)
        except Exception:
            prov = _LLMProvider.DASHSCOPE
        out = client.generate_content(prompt, provider=prov, model_name=llm_model, fallback=False)
        if out and out.strip():
            tags = [t.strip() for t in re.split(r'[,，、\n]', out.strip()) if t.strip()]
            if tags:
                return tags[:10]
    except Exception as e:
        print(f">>> LLM 生成标签失败: {e}")
    return None


def _fetch_metadata_description(video_path):
    """从原视频元数据提取描述（yt-dlp 的 *.description 或 *.info.json）。"""
    base_name = os.path.splitext(video_path)[0]
    desc_path = base_name + ".description"
    if os.path.exists(desc_path):
        try:
            with open(desc_path, "r", encoding="utf-8") as f:
                d = f.read().strip()
            if d:
                return d
        except Exception:
            pass
    for p in [base_name + ".info.json"]:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                info = json.load(f)
            d = (info.get("description") or "").strip()
            if d:
                return d
        except Exception:
            continue
    return None


def _load_cached_upload_metadata(video_path):
    """Read metadata generated by the batch engine, if present."""
    metadata_path = os.path.join(
        os.path.dirname(video_path) or ".",
        "upload_metadata.json",
    )
    if not os.path.isfile(metadata_path):
        return {}
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _merge_upload_metadata(video_path, patch):
    """Persist extra keys, such as the draft box reference, next to the video."""
    metadata_path = os.path.join(
        os.path.dirname(video_path) or ".",
        "upload_metadata.json",
    )
    data = _load_cached_upload_metadata(video_path)
    data.update(patch)
    try:
        with open(metadata_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        print(f">>> 写入 upload_metadata.json 失败: {exc}")
        return False


async def _record_draft_reference(video_path, page, desc):
    """Remember which draft box entry holds this video, so repairs stay exact."""
    try:
        if not await _open_draft_list(page):
            return None
        entries = await _draft_entries_all_pages(page)
        candidates = _repair_draft_candidates(entries, desc)
        if len(candidates) != 1:
            print(f">>> 草稿箱匹配到 {len(candidates)} 条同描述草稿，未记录草稿引用")
            return None
        draft_time = str(candidates[0].get("time") or "").strip()
        if not draft_time:
            return None
        if _merge_upload_metadata(video_path, {"wechat_draft_time": draft_time}):
            print(f">>> 已记录草稿引用: {draft_time}")
            # Reading the list leaves the page on the draft box. The caller
            # continues on the edit form, so reopen the matched draft.
            if not await _open_draft_entry_ref(page, candidates[0]):
                print(">>> 记录草稿引用后未能回到草稿编辑页")
                return None
            if not await _wait_cover_editor_ready(page, timeout=600):
                print(">>> 记录草稿引用后封面编辑器未就绪")
                return None
            return draft_time
    except Exception as exc:
        print(f">>> 记录草稿引用失败: {exc}")
    return None


def build_description(video_path, title, description=None, llm_provider="dashscope", llm_model="glm-5.2"):
    """生成视频号描述。优先级：显式传入 > 原视频元数据 > 字幕 LLM 总结 > 标题。"""
    if description and description.strip():
        return description.strip()
    meta_desc = _fetch_metadata_description(video_path)
    if meta_desc:
        print(">>> 描述来源：原视频元数据")
        return meta_desc
    sub_text, sub_src = _read_subtitle_text(video_path)
    if sub_text:
        summary = _summarize_via_llm(sub_text, title, os.path.basename(sub_src), llm_provider, llm_model)
        if summary:
            print(f">>> 描述来源：根据字幕内容总结（{os.path.basename(sub_src)}）")
            return summary
    return f"{title}"


def _normalize_wechat_title(title):
    """Apply WeChat's short-title punctuation and length rules."""
    cleaned = str(title).replace(",", " ").replace("，", " ")
    cleaned = re.sub(
        r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff《》：:+?%℃ ]",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or WECHAT_TITLE_FALLBACK)[:WECHAT_TITLE_MAX_CHARS]


def build_title(video_path, original_title, llm_provider="dashscope", llm_model="glm-5.2"):
    """Generate a WeChat title exclusively from subtitle content.

    The caller-provided title is logged only as context; it is never sent to
    the LLM or used as a fallback. This keeps GUI/Launcher integrations from
    accidentally reintroducing filename-derived titles.
    """
    if not _SUBTITLE_TITLE_AVAILABLE:
        raise SubtitleTitleError("subtitle_title module is unavailable")
    client = _get_llm_client() if _get_llm_client and _LLMProvider else None
    grounded = build_subtitle_grounded_title(
        video_path,
        llm_client=client,
        model_name=llm_model,
        max_title_chars=WECHAT_TITLE_MAX_CHARS,
        min_title_chars=8,
        platform_hint="微信短标题字段最多 16 个字符，且不能依赖后续截断。",
        title_style="plain",
    )
    title = _normalize_wechat_title(grounded.title)
    subtitle_text, _ = read_full_subtitle_text(video_path, grounded.subtitle_path)
    validate_title_from_subtitle(
        title,
        subtitle_text or "",
        grounded.evidence,
        min_chars=5,
        max_chars=WECHAT_TITLE_MAX_CHARS,
    )
    print(f">>> 标题来源：{grounded.source}（{os.path.basename(grounded.subtitle_path)}）")
    print(f"    原标题: {original_title}")
    print(f"    新标题: {title}")
    for fragment in grounded.evidence:
        print(f"    字幕证据: {fragment}")
    return title


def build_cover_title(
    video_path,
    llm_provider="dashscope",
    llm_model="glm-5.2",
    attribution=None,
):
    """Generate the longer editorial headline rendered on cover images."""
    if not _SUBTITLE_TITLE_AVAILABLE:
        raise SubtitleTitleError("subtitle_title module is unavailable")
    client = _get_llm_client() if _get_llm_client and _LLMProvider else None
    grounded = build_subtitle_grounded_title(
        video_path,
        llm_client=client,
        model_name=llm_model,
        max_title_chars=48,
        min_title_chars=12,
        platform_hint=(
            "微信封面标题可以比表单标题更长；优先使用字幕中已有的强钩子"
            "（热点词、身份标签、反差冲突、比喻或原话金句），没有强钩子再输出完整判断句。"
        ),
        title_style="cover_headline",
        attribution=attribution,
    )
    print(f">>> 封面标题来源：{grounded.source}（{os.path.basename(grounded.subtitle_path)}）")
    print(f"    封面标题: {grounded.title}")
    for fragment in grounded.evidence:
        print(f"    字幕证据: {fragment}")
    return grounded.title


# ============================================================================ #
#  Playwright 上传核心
# ============================================================================ #

async def _ensure_browser(playwright, headless=False):
    """启动持久化 browser context。返回 (browser_context, page)。"""
    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
    profile_lock = _BrowserProfileLock()
    await profile_lock.acquire()
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=BROWSER_DATA_DIR,
            headless=headless,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-proxy-server",
            ] + (
                [f"--host-resolver-rules={rules}"]
                if (rules := os.environ.get("WECHAT_HOST_RESOLVER_RULES"))
                else []
            ),
        )
        # 拦截可能的自动化检测
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        if not context.pages:
            page = await context.new_page()
        else:
            page = context.pages[0]
    except Exception:
        await profile_lock.release()
        raise
    return context, page, profile_lock


async def _wait_login(page, timeout=120):
    """Wait for QR login, then prove it by opening the create form."""
    print(">>> 请在浏览器中扫码登录微信视频号创作者平台...")
    print(f">>> 等待登录完成（最多 {timeout} 秒）...")
    start = time.time()
    while time.time() - start < timeout:
        url = page.url
        if "login" not in url.lower() and await _check_logged_in(page):
            await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
            if await _wait_for_upload_input(page, timeout=10):
                print(">>> 登录成功！")
                return True
            # A stale home page can still contain an avatar. Return to the
            # login route so the QR code remains visible for the user.
            await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)
            continue
        try:
            if await page.query_selector('text="发布"'):
                await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                if await _wait_for_upload_input(page, timeout=10):
                    print(">>> 登录成功！")
                    return True
                await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)
                continue
        except Exception:
            pass
        await asyncio.sleep(2)
    print(">>> 登录超时")
    return False


async def _check_logged_in(page):
    """Quick UI check; callers still verify the create form when it matters."""
    url = page.url
    if "login" in url.lower():
        return False
    try:
        # 登录后通常有头像或发布按钮
        for sel in ['text="发布动态"', 'text="发表内容"', '[class*="avatar"]', '[class*="user-info"]']:
            if await page.query_selector(sel):
                return True
    except Exception:
        pass
    return False


async def _wait_for_upload_input(page, timeout=15):
    """Wait for the create page's file input, a practical logged-in signal."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            if "login" in page.url.lower():
                return False
            candidate = page.locator('input[type="file"]').first
            if await candidate.count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def _ensure_logged_in(page, allow_qr=True, timeout=45):
    """Verify login by opening the create form, recovering only when needed."""
    await page.goto(WECHAT_CREATE_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)
    if await _wait_for_upload_input(page, timeout=timeout):
        return True

    error_path = os.path.join(SKILL_DIR, "assets", "page_error.html")
    try:
        with open(error_path, "w", encoding="utf-8") as handle:
            handle.write(await page.content())
    except Exception:
        pass

    login_expired = "login" in page.url.lower()
    if not allow_qr:
        if login_expired:
            print(">>> 微信登录态不可用；无头上传已停止。")
            print(">>> 请先执行一次可见登录命令，扫码后再重试上传：")
        else:
            print(">>> 微信发布页加载失败或上传入口未出现；登录态可能仍有效。")
            print(">>> 请检查网络后重试；若连续失败，再执行可见登录命令：")
        print(f">>> {sys.executable} -u {os.path.abspath(__file__)} --login")
        return False
    if login_expired:
        print(">>> 微信登录态已失效，请在浏览器中重新扫码...")
    else:
        print(">>> 发布页未出现上传入口，转入登录页复核登录态...")
    await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    if not await _wait_login(page, timeout=600):
        print(">>> 微信重新登录失败")
        return False
    return True


async def _do_login(headless=False):
    """首次登录流程：打开浏览器，等待扫码。"""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        context, page, profile_lock = await _ensure_browser(p, headless=False)  # 登录必须 headless=False
        try:
            print(f">>> 打开微信视频号创作者平台: {WECHAT_LOGIN_URL}")
            await page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            if await _ensure_logged_in(page):
                print(">>> 已经是登录状态，无需重新扫码")
                return True
            return False
        finally:
            try:
                await context.close()
            finally:
                await profile_lock.release()


# 多组容错选择器
UPLOAD_INPUT_SELECTORS = [
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
    '[class*="upload"] input[type="file"]',
    '[class*="video-input"] input',
]
TITLE_SELECTORS = [
    '[placeholder*="标题"]',
    '[placeholder*="标题"] textarea',
    '[class*="title"] [contenteditable]',
    '[class*="title"] input',
    '[class*="title"] textarea',
    'textarea[placeholder*="标题"]',
]
DESC_SELECTORS = [
    '[placeholder*="描述"]',
    '[placeholder*="简介"]',
    '[placeholder*="说点什么"]',
    '[class*="desc"] [contenteditable]',
    '[class*="desc"] textarea',
    '[class*="description"] textarea',
    'textarea[placeholder*="描述"]',
    'div[contenteditable][data-placeholder*="描述"]',
    'div[contenteditable][data-placeholder*="说"]',
]
TAG_INPUT_SELECTORS = [
    '[placeholder*="标签"]',
    '[placeholder*="话题"]',
    '[class*="tag"] input',
    '[class*="tag"] [contenteditable]',
]
PUBLISH_SELECTORS = [
    'button:has-text("发布")',
    'button:has-text("发表")',
    'button:has-text("发布动态")',
    '[class*="publish"] button',
    'button[class*="primary"]:has-text("发")',
]
SAVE_DRAFT_SELECTORS = [
    'button:has-text("存草稿")',
    'button:has-text("保存草稿")',
    '[class*="draft"] button',
]
UPLOAD_PROGRESS_SELECTORS = [
    '[class*="progress"]',
    '[class*="upload-status"]',
    '[class*="uploading"]',
]
UPLOAD_DONE_INDICATORS = [
    'text="上传完成"',
    '[class*="upload-success"]',
    '[class*="upload-complete"]',
]

# 进度百分比连续这么久不变就判定上传卡死。正常上传每几秒就会跳一个百分点，
# 而卡死时页面仍在轮询自己的接口，看起来"还活着"，只有进度不再前进。
UPLOAD_STALL_SECONDS = float(os.environ.get("WECHAT_UPLOAD_STALL_SECONDS", "900"))
# Large videos can sit at 99% while WeChat transcodes/processes them on the
# server. This phase is not network upload progress and needs a longer budget.
UPLOAD_PROCESSING_STALL_SECONDS = float(
    os.environ.get("WECHAT_UPLOAD_PROCESSING_STALL_SECONDS", "3600")
)
# 文件已选择但页面始终不给进度时，也不能占用串行上传槽位到 10000 秒。
UPLOAD_NO_PROGRESS_SECONDS = float(
    os.environ.get("WECHAT_UPLOAD_NO_PROGRESS_SECONDS", "300")
)
UPLOAD_EVALUATE_TIMEOUT_SECONDS = float(
    os.environ.get("WECHAT_UPLOAD_EVALUATE_TIMEOUT_SECONDS", "10")
)


async def _try_selectors(page, selectors, description="element"):
    """依次尝试多个选择器，返回第一个找到的 element 或 None。"""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                return el
        except Exception:
            continue
    print(f">>> 未找到 {description}（尝试了 {len(selectors)} 个选择器）")
    return None


async def _set_text(page, selectors, text, description="字段"):
    """在 contenteditable 或 textarea/input 中设置文本。"""
    el = await _try_selectors(page, selectors, description)
    if not el:
        return False
    try:
        await el.fill("")
        await el.fill(text)
        print(f">>> 已填写{description}")
        return True
    except Exception as e:
        print(f">>> 填写{description}失败: {e}")
        return False


async def _add_tags(page, tags):
    """逐个添加标签。"""
    if not tags:
        return
    tag_input = await _try_selectors(page, TAG_INPUT_SELECTORS, "标签输入框")
    if not tag_input:
        print(">>> 未找到标签输入框，跳过标签")
        return
    for tag in tags:
        try:
            await tag_input.click()
            await asyncio.sleep(0.2)
            await page.keyboard.type(tag, delay=10)
            await asyncio.sleep(0.3)
            # 尝试回车确认
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f">>> 添加标签 '{tag}' 失败: {e}")
    print(f">>> 已添加 {len(tags)} 个标签")


async def _select_wechat_location_none(page):
    """Set the location field to WeChat's explicit no-location option."""
    try:
        display = page.locator(".post-position-wrap .position-display").first
        if await display.count() == 0:
            print(">>> 未找到微信位置设置")
            return False
        await display.click()
        option = page.locator(
            ".post-position-wrap .option-item",
            has_text="不显示位置",
        ).first
        # WeChat loads nearby-location options asynchronously after the
        # display control is clicked. A fixed 0.3s wait can run before the
        # "no location" row is attached.
        for _ in range(20):
            if await option.count() > 0:
                break
            await asyncio.sleep(0.25)
        if await option.count() == 0:
            print(">>> 未找到微信“不显示位置”选项")
            return False
        await option.click()
        print(">>> 微信位置已设置为不显示")
        return True
    except Exception as exc:
        print(f">>> 设置微信位置失败: {exc}")
        return False


async def _select_wechat_mark_tag_none(page):
    """Set the video mark tag to the platform's no-annotation option."""
    try:
        display = page.locator(
            ".post-with-mark-tag .mark-tag-select .select-display"
        ).first
        if await display.count() == 0:
            print(">>> 未找到微信视频标注设置")
            return False
        await display.click()
        await asyncio.sleep(0.3)
        option = page.locator(
            ".post-with-mark-tag .mark-tag-option",
            has_text="无需标注",
        ).first
        if await option.count() == 0:
            print(">>> 未找到微信“无需标注”选项")
            return False
        await option.click()
        print(">>> 微信视频标注已设置为无需标注")
        return True
    except Exception as exc:
        print(f">>> 设置微信视频标注失败: {exc}")
        return False


async def _confirm_wechat_original_dialog(page):
    """Confirm an original-rights prompt if checking the checkbox opens one."""
    for _ in range(10):
        try:
            dialog = page.locator(
                ".post-with-link .weui-desktop-dialog__wrp:visible"
            ).first
            if await dialog.count() == 0:
                return True

            agreement = dialog.locator('input[type="checkbox"]').first
            if await agreement.count() > 0 and not await agreement.is_checked():
                wrapper = dialog.locator(".ant-checkbox-wrapper").first
                if await wrapper.count() > 0:
                    await wrapper.click()
                else:
                    await agreement.click()
                await asyncio.sleep(0.2)

            buttons = dialog.locator("button")
            button = None
            for index in range(await buttons.count()):
                candidate = buttons.nth(index)
                if (await candidate.inner_text()).strip() == "声明原创":
                    button = candidate
                    break

            if button is not None and await button.is_enabled():
                if await button.is_visible():
                    await button.click()
                    await asyncio.sleep(0.5)
                    if await dialog.count() == 0:
                        print(">>> 已确认微信原创声明")
                        return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


async def _select_wechat_original(page):
    """Enable the original-content marker and confirm any rights dialog."""
    try:
        checkbox = page.locator(
            ".declare-original-checkbox input[type='checkbox']"
        ).first
        if await checkbox.count() == 0:
            print(">>> 未找到微信声明原创开关")
            return False
        if not await checkbox.is_checked():
            wrapper = page.locator(
                ".declare-original-checkbox .ant-checkbox-wrapper"
            ).first
            if await wrapper.count() > 0:
                await wrapper.click()
            else:
                await checkbox.click()
            await asyncio.sleep(0.3)
        if not await checkbox.is_checked():
            await checkbox.click()
            await asyncio.sleep(0.3)
        if not await checkbox.is_checked():
            print(">>> 微信声明原创开关未能选中")
            return False
        if not await _confirm_wechat_original_dialog(page):
            return False
        print(">>> 微信已选择原创标志")
        return True
    except Exception as exc:
        print(f">>> 设置微信原创标志失败: {exc}")
        return False


async def _set_required_publish_settings(page):
    """Apply the three mandatory WeChat publish settings."""
    location_ok = await _select_wechat_location_none(page)
    mark_ok = await _select_wechat_mark_tag_none(page)
    original_ok = await _select_wechat_original(page)
    if not (location_ok and mark_ok and original_ok):
        return False
    print(">>> 微信位置、视频标注、原创标志均已设置")
    return True


async def _read_required_publish_settings(page):
    """Read the three mandatory settings without changing them."""
    state = {"location": False, "mark": False, "original": False}
    try:
        location = page.locator(
            ".post-position-wrap .position-display"
        ).first
        if await location.count() > 0:
            state["location"] = (
                "不显示位置" in await location.inner_text()
            )
    except Exception:
        pass
    try:
        mark = page.locator(
            ".post-with-mark-tag .mark-tag-select .select-display"
        ).first
        if await mark.count() > 0:
            state["mark"] = "无需标注" in await mark.inner_text()
    except Exception:
        pass
    try:
        checkbox = page.locator(
            ".declare-original-checkbox input[type='checkbox']"
        ).first
        if await checkbox.count() > 0:
            state["original"] = await checkbox.is_checked()
    except Exception:
        pass
    return state


async def _verify_required_publish_settings(page):
    """Confirm the saved draft still carries the three mandatory settings."""
    state = await _read_required_publish_settings(page)
    print(
        ">>> 微信发布设置回读: "
        f"位置={'不显示' if state['location'] else '未确认'}, "
        f"标注={'无需标注' if state['mark'] else '未确认'}, "
        f"原创={'已选' if state['original'] else '未确认'}"
    )
    return all(state.values())


async def _ensure_required_publish_settings(page):
    """Reapply the mandatory settings only when the saved state is incomplete."""
    if await _verify_required_publish_settings(page):
        return True
    print(">>> 微信发布设置不完整，自动补设")
    return await _set_required_publish_settings(page)


async def _set_wechat_collection(page, collection_name: str) -> bool:
    """Select or create a WeChat Channels collection (添加到合集)."""
    if not collection_name or not collection_name.strip():
        return True
    target = collection_name.strip()[:10]
    try:
        album_wrap = page.locator(".post-album-wrap").first
        if await album_wrap.count() == 0:
            print(">>> 表单中未找到'添加到合集'入口")
            return False

        display_text = album_wrap.locator(".display-text").first
        current = (await display_text.inner_text()).strip() if await display_text.count() > 0 else ""
        if target in current:
            print(f">>> 当前视频已关联微信合集: {target}")
            return True

        # Click dropdown trigger to reveal options
        await album_wrap.locator(".post-album-display-wrap").first.click()
        await asyncio.sleep(0.5)

        # Check existing options
        options = album_wrap.locator(".option-list-wrap .option-item")
        matched = None
        for i in range(await options.count()):
            opt = options.nth(i)
            name_el = opt.locator(".name").first
            if await name_el.count() > 0 and (await name_el.inner_text()).strip() == target:
                matched = opt
                break

        if matched is not None:
            await matched.click()
            await asyncio.sleep(0.5)
            print(f">>> 已选择已有合集: {target}")
            return True

        # Create new collection
        create_link = album_wrap.locator(".create a").first
        if await create_link.count() == 0:
            print(f">>> 未找到'创建新合集'按钮，无法创建: {target}")
            await album_wrap.locator(".post-album-display-wrap").first.click()
            return False

        await create_link.click()
        await asyncio.sleep(0.8)

        # Fill collection input
        dialog_input = page.locator(".create-dialog-wrap input.weui-desktop-form__input").first
        await dialog_input.wait_for(state="visible", timeout=5000)
        await dialog_input.fill(target)
        await asyncio.sleep(0.3)

        # Click create button
        create_btn = page.locator(
            ".create-dialog-wrap"
        ).locator("xpath=ancestor::div[contains(@class, 'weui-desktop-dialog')]").locator(".weui-desktop-btn_primary").first
        if await create_btn.count() == 0:
            create_btn = page.locator("button:has-text('创建')").first
        await create_btn.click()
        await asyncio.sleep(1.0)

        # Dismiss "我知道了" dialog
        know_btn = page.locator(".create-dialog-success-wrap button, button:has-text('我知道了')").first
        if await know_btn.count() > 0:
            try:
                await know_btn.click(timeout=3000)
                await asyncio.sleep(0.5)
            except Exception:
                pass

        print(f">>> 成功新建并关联微信合集: {target}")
        return True
    except Exception as exc:
        print(f">>> 设置微信合集失败 ({target}): {exc}")
        return False


async def _read_upload_progress(page):
    """Read upload progress from progress nodes, ARIA state, or page text."""
    try:
        raw_progress = await asyncio.wait_for(
            page.evaluate(
                """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const text = root.innerText || document.body?.innerText || '';
                const uploadError = ['上传失败', '网络出错，请重新上传'].find(
                    (message) => text.includes(message)
                );
                if (uploadError) {
                    return {
                        pct: null,
                        source: 'upload-error',
                        detail: uploadError,
                        error: uploadError,
                    };
                }
                const nodes = Array.from(root.querySelectorAll(
                    '[class*="progress"], [class*="upload-status"], [class*="uploading"], [role="progressbar"]'
                ));
                const candidates = [];
                const isVisible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return Boolean(
                        rect
                        && rect.width > 0
                        && rect.height > 0
                        && style
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                    );
                };
                for (const node of nodes) {
                    let pct = null;
                    let source = '';
                    let detail = '';
                    const textValues = [
                        node.getAttribute('aria-valuetext'),
                        node.innerText,
                        node.textContent,
                    ].filter(Boolean).map((value) => String(value).trim());
                    for (const value of textValues) {
                        const match = value.match(/(\\d{1,3}(?:\\.\\d+)?)\\s*%/);
                        if (match) {
                            pct = Math.min(100, Math.round(parseFloat(match[1])));
                            source = 'progress-node';
                            detail = value.slice(0, 160);
                            break;
                        }
                    }

                    if (pct === null) {
                        const ariaNow = Number(node.getAttribute('aria-valuenow'));
                        if (Number.isFinite(ariaNow) && ariaNow >= 0) {
                            const ariaMax = Number(node.getAttribute('aria-valuemax')) || 100;
                            if (ariaMax > 0) {
                                pct = Math.min(100, Math.round((ariaNow / ariaMax) * 100));
                                source = 'aria-progressbar';
                                detail = `${ariaNow}/${ariaMax}`;
                            }
                        }
                    }

                    if (pct === null) {
                        const width = node.style && node.style.width;
                        const widthMatch = width && String(width).match(/^(\\d{1,3}(?:\\.\\d+)?)%$/);
                        if (widthMatch) {
                            pct = Math.min(100, Math.round(parseFloat(widthMatch[1])));
                            source = 'progress-width';
                            detail = String(width);
                        }
                    }

                    if (pct !== null) {
                        candidates.push({
                            pct,
                            source,
                            detail,
                            visible: isVisible(node),
                        });
                    }
                }

                const lines = text.split('\\n').map((line) => line.trim()).filter(Boolean);
                const progressLine = lines.find(
                    (line) => /\\d{1,3}(?:\\.\\d+)?\\s*%/.test(line)
                        && /(上传|进度|转码|传输|完成)/.test(line)
                );
                if (progressLine) {
                    const match = progressLine.match(/(\\d{1,3}(?:\\.\\d+)?)\\s*%/);
                    candidates.push({
                        pct: Math.min(100, Math.round(parseFloat(match[1]))),
                        source: 'page-text',
                        detail: progressLine.slice(0, 160),
                        visible: true,
                    });
                }
                return {
                    candidates,
                    pct: null,
                    source: 'no-progress',
                    detail: lines.slice(0, 20).join(' | ').slice(0, 500),
                };
            }"""
            ),
            timeout=UPLOAD_EVALUATE_TIMEOUT_SECONDS,
        )
        return _select_upload_progress(raw_progress)
    except asyncio.TimeoutError:
        return {
            "pct": None,
            "source": "evaluate-timeout",
            "detail": f"page.evaluate exceeded {UPLOAD_EVALUATE_TIMEOUT_SECONDS}s",
        }
    except Exception as e:
        return {"pct": None, "source": "evaluate-error", "detail": str(e)[:200]}


def _select_upload_progress(raw_progress):
    """Choose the most useful progress candidate returned by the page."""
    if not isinstance(raw_progress, dict):
        return {"pct": None, "source": "evaluate-error", "detail": "invalid progress result"}
    if raw_progress.get("error"):
        return raw_progress

    candidates = raw_progress.get("candidates")
    if not isinstance(candidates, list):
        return raw_progress

    valid = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        pct = candidate.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        if not math.isfinite(pct) or pct < 0 or pct > 100:
            continue
        valid.append(candidate)
    if not valid:
        return raw_progress

    visible = [candidate for candidate in valid if candidate.get("visible")]
    pool = visible or valid
    best = max(pool, key=lambda candidate: candidate["pct"])
    return {
        "pct": best["pct"],
        "source": best.get("source") or "progress-node",
        "detail": str(best.get("detail") or ""),
    }


def _upload_progress_stage_label(progress, previous_pct=-1):
    """Label the ARIA bar that follows a near-complete upload as processing."""
    source = str(progress.get("source") or "")
    if source == "aria-progressbar" and previous_pct >= 90:
        return "页面处理进度"
    return "视频上传进度"


async def _wait_upload_complete(page, timeout=10000):
    """等待视频上传完成。"""
    print(f">>> 等待视频上传完成（最多 {timeout} 秒）...")
    start = time.time()
    last_pct = -1
    last_stage = ""
    last_progress_at = start
    last_diagnostic_at = 0.0
    while time.time() - start < timeout:
        # 检测上传完成指示器
        for sel in UPLOAD_DONE_INDICATORS:
            try:
                if await page.locator(sel).count() > 0:
                    print(f"\n>>> 上传完成！")
                    return True
            except Exception:
                continue
        progress = await _read_upload_progress(page)
        pct = progress.get("pct")
        upload_error = progress.get("error")
        if upload_error:
            print(f"\n>>> 页面显示上传错误: {upload_error}")
            return False
        if pct is not None:
            if pct != last_pct:
                stage = _upload_progress_stage_label(progress, last_pct)
                print(f"\r>>> {stage}: {pct}% ({progress.get('source')})", end="", flush=True)
                last_pct = pct
                last_stage = stage
                last_progress_at = time.time()
            if pct >= 100:
                print()
                return True
        elif time.time() - last_diagnostic_at >= 60:
            detail = re.sub(r"\s+", " ", str(progress.get("detail") or "")).strip()
            print(f"\n>>> 视频上传进度未识别；页面文本片段: {detail[:300]}")
            last_diagnostic_at = time.time()
            if time.time() - last_progress_at >= UPLOAD_NO_PROGRESS_SECONDS:
                print(
                    f"\n>>> {UPLOAD_NO_PROGRESS_SECONDS} 秒内未识别到上传进度，判定为上传未启动或网络断流"
                )
                return False
        # 快速上传可能在第一次轮询前完成，进度条已经消失。
        # 此时用“保存草稿/发表按钮恢复可用”兜底，但至少等待 45 秒。
        if time.time() - last_progress_at >= 45:
            try:
                for text in ("保存草稿", "发表"):
                    btn = page.locator(f'button:has-text("{text}")').first
                    if await btn.count() == 0:
                        continue
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    class_name = await btn.get_attribute("class") or ""
                    if not disabled and aria_disabled != "true" and "disabled" not in class_name:
                        print(f"\n>>> {text}按钮已可点击，且进度条已结束，上传应已完成")
                        return True
            except Exception:
                pass
        stall_limit = (
            UPLOAD_PROCESSING_STALL_SECONDS
            if last_stage == "页面处理进度"
            else UPLOAD_STALL_SECONDS
        )
        if last_pct >= 0 and time.time() - last_progress_at >= stall_limit:
            print(
                f"\n>>> 上传进度在 {stall_limit} 秒内停在 {last_pct}%，判定为卡死，放弃等待"
            )
            return False
        for error_text in ("上传失败", "网络出错，请重新上传"):
            try:
                error = page.locator(f'text="{error_text}"').first
                if await error.count() > 0 and await error.is_visible():
                    print(f"\n>>> 页面显示上传错误: {error_text}")
                    return False
            except Exception:
                pass
        await asyncio.sleep(2)
    print("\n>>> 上传等待超时")
    return False

async def _wujie_text(page):
    """读取 Wujie 微前端在 shadow DOM 中的可见文本。"""
    try:
        return await page.evaluate(
            "() => document.querySelector('wujie-app')?.shadowRoot?.querySelector('html')?.innerText || ''"
        )
    except Exception:
        return ""


def _normalize_draft_text(text):
    """Normalize user-visible draft text for exact, whitespace-insensitive matching."""
    return re.sub(r"\s+", "", str(text or ""))


def _draft_signature(entry):
    """Build a conservative identity for a draft visible on the first page."""
    return (
        _normalize_draft_text(entry.get("title")),
        _normalize_draft_text(entry.get("time")),
    )


def _new_draft_entries(entries, expected_desc, known_drafts=None):
    """Return only exact-description drafts that were not present before upload."""
    expected = _normalize_draft_text(expected_desc)
    known = {tuple(item) for item in (known_drafts or set())}
    return [
        entry
        for entry in entries or []
        if _normalize_draft_text(entry.get("title")) == expected
        and _draft_signature(entry) not in known
    ]


def _dedupe_draft_entries(entries):
    """Drop duplicate page reads while preserving distinct list positions."""
    seen = set()
    result = []
    for entry in entries or []:
        # A stale page re-read repeats the same position and the same draft,
        # so the page number must not keep such a duplicate alive.
        identity = (entry.get("index"), _draft_signature(entry))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(entry)
    return result


def _known_draft_has_description(known_drafts, desc):
    """Whether the pre-upload draft snapshot already contains this description."""
    expected = _normalize_draft_text(desc)
    return any(
        tuple(item)[0] == expected
        for item in (known_drafts or set())
        if isinstance(item, (tuple, list)) and len(item) >= 1
    )


async def _click_visible_draft_link(page):
    links = page.locator("a", has_text="草稿箱")
    count = await links.count()
    for index in range(count):
        link = links.nth(index)
        try:
            if await link.is_visible():
                box = await link.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    await link.click()
                    return True
        except Exception:
            continue
    return False


async def _click_menu_draft_link(page):
    """Open the draft list from the menu, including the folded sidebar."""
    try:
        folded = await page.evaluate(
            "() => Boolean(document.querySelector('.app-body.fold-sidebar'))"
        )
        if folded:
            await page.evaluate(
                "() => document.querySelector('.finder-ui-desktop-menu__header')?.click()"
            )
            await asyncio.sleep(1)

        content_opened = await page.evaluate(
            """() => {
                const link = Array.from(
                    document.querySelectorAll('.finder-ui-desktop-menu__link')
                ).find((item) => (item.innerText || item.textContent || '').includes('内容管理'));
                if (!link) return false;
                link.click();
                return true;
            }"""
        )
        if not content_opened:
            return False
        await asyncio.sleep(1)

        return await page.evaluate(
            """() => {
                const link = Array.from(
                    document.querySelectorAll('.finder-ui-desktop-menu__link')
                ).find((item) => (item.innerText || item.textContent || '').includes('草稿箱'));
                if (!link) return false;
                link.click();
                return true;
            }"""
        )
    except Exception:
        return False


async def _wait_draft_list_ready(page, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        if "/post/draftListManager" in page.url:
            list_start = time.time()
            while time.time() - list_start < 60:
                entries = await _draft_list_entries(page)
                if entries:
                    return True
                text = await _wujie_text(page)
                if "暂无" in text and "草稿" in text:
                    return True
                normalized_text = _normalize_draft_text(text)
                if re.search(r"草稿箱[（(]0[）)]", normalized_text):
                    return True
                await asyncio.sleep(1)
            print(f">>> 草稿箱列表未加载完成: {page.url}")
            return False
        entries = await _draft_list_entries(page)
        if entries:
            return True
        await asyncio.sleep(1)
    print(f">>> 草稿箱页面未就绪: {page.url}")
    return False


async def _open_draft_list(page):
    """Open the draft list through the menu, with a direct-route fallback."""
    try:
        await page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector(
                ".finder-ui-desktop-menu__link",
                state="attached",
                timeout=20000,
            )
        except Exception:
            pass
        opened = await _click_menu_draft_link(page)
        if not opened:
            opened = await _click_visible_draft_link(page)

        if opened and await _wait_draft_list_ready(page):
            return True

        # The collapsed icon menu can hide both link labels. Use the known
        # route as a last resort, then still require real draft entries.
        print(">>> 可见菜单未打开草稿箱，尝试直达草稿箱路由...")
        await page.goto(WECHAT_DRAFT_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        if await _wait_draft_list_ready(page):
            return True

        # The Wujie shell can leave the direct route blank after a save action.
        # One reload is safer than declaring an already-saved draft unverified.
        print(">>> 草稿箱直达路由未加载，刷新后重试...")
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        return await _wait_draft_list_ready(page)
    except Exception as e:
        print(f">>> 打开草稿箱失败: {e}")
        return False


async def _draft_list_entries(page):
    """Read the currently visible draft entries from the Wujie shadow DOM."""
    # Draft items are attached before they are painted, and innerText of an
    # unpainted node is empty. Reading too early therefore yields rows with no
    # title, which later looks like "the draft disappeared".
    entries = []
    for _ in range(20):
        try:
            entries = await page.evaluate(
                """() => {
                    const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                    return Array.from(root.querySelectorAll('.post-draft-item')).map((item, index) => ({
                        index,
                        title: item.querySelector('.post-title')?.innerText || item.innerText,
                        time: item.querySelector('.post-time')?.innerText || '',
                        text: item.innerText,
                    }));
                }"""
            )
        except Exception:
            return []
        if any(_normalize_draft_text(item.get("title")) for item in entries):
            return entries
        await asyncio.sleep(0.5)
    return entries


async def _current_draft_page_number(page):
    """Return the draft list's current page number, or None when unknown.

    The pagination lives inside the Wujie shadow root, so a plain locator read
    silently failed and defaulted to 1. That made _goto_draft_page(1) believe it
    was already on page 1 and skip the click, leaving the list on the last page
    it had visited.
    """
    try:
        return await page.evaluate(
            """() => {
                const roots = [
                    document.querySelector('wujie-app')?.shadowRoot?.querySelector('html'),
                    document,
                ].filter(Boolean);
                let current = null;
                for (const root of roots) {
                    current = root.querySelector('.weui-desktop-pagination__num_current');
                    if (current) break;
                }
                if (!current) return null;
                const value = parseInt((current.innerText || '').trim(), 10);
                return Number.isFinite(value) ? value : null;
            }"""
        )
    except Exception:
        return None


async def _click_draft_page_number(page, page_number):
    """Click a pagination number inside the Wujie shadow root."""
    try:
        return bool(
            await page.evaluate(
                """(wanted) => {
                    const roots = [
                        document.querySelector('wujie-app')?.shadowRoot?.querySelector('html'),
                        document,
                    ].filter(Boolean);
                    let target = null;
                    for (const root of roots) {
                        target = Array.from(
                            root.querySelectorAll('.weui-desktop-pagination__num')
                        ).find((el) => (el.innerText || '').trim() === String(wanted));
                        if (target) break;
                    }
                    if (!target) return false;
                    target.click();
                    return true;
                }""",
                page_number,
            )
        )
    except Exception:
        return False


async def _goto_draft_page(page, page_number):
    """Move the draft list to an explicit page."""
    for _ in range(20):
        current = await _current_draft_page_number(page)
        if current == page_number:
            return True
        if current is None:
            # No pagination rendered: a single-page list is already page 1.
            return page_number == 1
        if not await _click_draft_page_number(page, page_number):
            return False
        await asyncio.sleep(0.5)
    return await _current_draft_page_number(page) == page_number


async def _goto_first_draft_page(page):
    """Return to page 1 even when the pagination indicator cannot be read.

    Clicking 上一页 until it is disabled converges on the first page without
    depending on the current-page element, and is a no-op once there.
    """
    await _goto_draft_page(page, 1)
    for _ in range(20):
        try:
            prev_button = page.locator(
                ".weui-desktop-pagination__nav a",
                has_text="上一页",
            ).first
            if await prev_button.count() == 0:
                return True
            disabled = (
                await prev_button.get_attribute("disabled")
                or "disabled" in (await prev_button.get_attribute("class") or "")
            )
        except Exception:
            return True
        if disabled:
            return True
        await prev_button.click()
        await asyncio.sleep(0.8)
    return True


async def _draft_entries_all_pages(page):
    """Read every draft entry, not only the first page."""
    if not hasattr(page, "locator"):
        return await _draft_list_entries(page)
    # Always start from page 1 so the collected local indices are stable.
    await _goto_first_draft_page(page)

    entries = []
    page_number = 0
    while True:
        page_number += 1
        current_entries = await _draft_list_entries(page)
        for entry in current_entries:
            entry["page"] = page_number
        entries.extend(current_entries)
        page_key = tuple(_draft_signature(item) for item in current_entries)

        next_button = page.locator(
            ".weui-desktop-pagination__nav a",
            has_text="下一页",
        ).first
        if await next_button.count() == 0:
            break
        disabled = (
            await next_button.get_attribute("disabled")
            or "disabled" in (await next_button.get_attribute("class") or "")
        )
        if disabled:
            break
        await next_button.click()
        # The page indicator flips before the list re-renders, so require the
        # rows themselves to change; otherwise a stale page is collected twice.
        advanced = False
        for _ in range(20):
            await asyncio.sleep(0.5)
            probe = await _draft_list_entries(page)
            if tuple(_draft_signature(item) for item in probe) != page_key:
                advanced = True
                break
        if not advanced:
            break

    await _goto_first_draft_page(page)
    return _dedupe_draft_entries(entries)


async def _open_draft_entry_ref(page, entry):
    """Open a draft entry, reloading the list when pagination is unreliable.

    Driving the pagination widget can leave the list parked on the wrong page,
    and a stale page reads as "the draft disappeared". A fresh draft-list
    navigation always lands on page 1, so retry from there instead of scanning.
    """
    page_number = int(entry.get("page") or 1)
    for attempt in range(3):
        if attempt:
            print(">>> 重新加载草稿箱后重试打开草稿...")
            if not await _open_draft_list(page):
                return False
        if page_number > 1 and not await _goto_draft_page(page, page_number):
            print(f">>> 无法切换到草稿箱第 {page_number} 页")
            continue
        if await _open_draft_entry_by_signature(page, entry):
            return True
    print(">>> 草稿箱中未找到唯一匹配的草稿签名")
    return False


async def _wait_draft_edit_open(page):
    """Wait for the draft edit form after an entry has been clicked."""
    for _ in range(30):
        if await page.locator(".vertical-cover-wrap").count() > 0:
            return True
        if await page.locator(".horizon-cover-wrap").count() > 0:
            return True
        if await page.locator('input[type="file"]').count() > 0:
            return True
        await asyncio.sleep(1)
    print(">>> 草稿编辑页未打开")
    return False


async def _open_draft_entry_by_signature(page, entry):
    """Open the unique list item matching a previously read draft signature."""
    expected_title = _normalize_draft_text(entry.get("title"))
    expected_time = _normalize_draft_text(entry.get("time"))
    if not expected_title and not expected_time:
        print(">>> 草稿引用缺少标题和时间，无法安全打开")
        return False
    current_entries = await _draft_list_entries(page)
    matches = [
        item
        for item in current_entries
        if _normalize_draft_text(item.get("title")) == expected_title
        and _normalize_draft_text(item.get("time")) == expected_time
    ]
    if not matches and expected_title:
        # Saving a draft refreshes its displayed timestamp, so a stale time
        # must not hide an otherwise unique title match.
        title_only = [
            item
            for item in current_entries
            if _normalize_draft_text(item.get("title")) == expected_title
        ]
        if len(title_only) == 1:
            print(">>> 草稿时间已刷新，按唯一标题匹配打开")
            return await _open_draft_entry(page, title_only[0]["index"])
    if len(matches) == 1:
        return await _open_draft_entry(page, matches[0]["index"])
    if len(matches) > 1:
        print(f">>> 草稿箱按标题和时间匹配到 {len(matches)} 条草稿，拒绝猜测")
        return False
    print(
        ">>> 草稿签名未命中；期望时间="
        f"{expected_time or '(空)'} 期望标题前40字={expected_title[:40] or '(空)'}"
    )
    for item in current_entries[:5]:
        print(
            "    实际条目: 时间="
            f"{_normalize_draft_text(item.get('time')) or '(空)'} "
            f"标题前40字={_normalize_draft_text(item.get('title'))[:40] or '(空)'}"
        )
    try:
        result = await page.evaluate(
            """(expected) => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const normalize = (value) => String(value || '').replace(/\\s+/g, '');
                const items = Array.from(root.querySelectorAll('.post-draft-item'));
                const matches = items.filter((item) => {
                    const title = normalize(
                        item.querySelector('.post-title')?.innerText || item.innerText
                    );
                    const time = normalize(item.querySelector('.post-time')?.innerText || '');
                    return title === expected.title && time === expected.time;
                });
                if (matches.length !== 1) {
                    return matches.length;
                }
                const button = matches[0].querySelector('.opr-item');
                if (!button) {
                    return -2;
                }
                button.click();
                return -1;
            }""",
            {"title": expected_title, "time": expected_time},
        )
        if result == -1:
            return await _wait_draft_edit_open(page)
        if result == -2:
            print(">>> 匹配的草稿条目缺少编辑入口")
        else:
            print(f">>> 草稿箱按标题和时间匹配到 {result} 条草稿，拒绝猜测")
        return False
    except Exception as exc:
        print(f">>> 按草稿签名打开编辑页失败: {exc}")
        return False


async def _open_draft_entry(page, index):
    """Open one draft by its current list index and wait for the edit form."""
    try:
        clicked = await page.evaluate(
            """(index) => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const item = Array.from(root.querySelectorAll('.post-draft-item'))[index];
                if (!item) return false;
                const button = item.querySelector('.opr-item');
                if (!button) return false;
                button.click();
                return true;
            }""",
            index,
        )
        if not clicked:
            print(f">>> 草稿条目 {index} 缺少编辑入口")
            return False
        return await _wait_draft_edit_open(page)
    except Exception as e:
        print(f">>> 打开草稿编辑页失败: {e}")
        return False


async def _snapshot_drafts(page):
    """Capture visible draft signatures before creating a new upload."""
    if not await _open_draft_list(page):
        return None
    entries = await _draft_entries_all_pages(page)
    return {_draft_signature(entry) for entry in entries}


async def _verify_draft_saved(
    page,
    title,
    desc=None,
    expected_covers=None,
    known_drafts=None,
    timeout=40,
):
    """Confirm a saved draft, including both uploaded cover cards."""
    print(">>> 正在到草稿箱核验保存结果...")
    if not await _open_draft_list(page):
        print(">>> 无法打开草稿箱")
        return False
    expected_desc = _normalize_draft_text(desc or title)
    start = time.time()
    while time.time() - start < timeout:
        entries = await _draft_entries_all_pages(page)
        candidates = _new_draft_entries(entries, expected_desc, known_drafts)
        if candidates:
            if len(candidates) != 1:
                print(f">>> 草稿箱匹配到 {len(candidates)} 条本次新增草稿，拒绝猜测")
                return False
            print(">>> 草稿箱已确认保存本次新增的视频")
            if not await _open_draft_entry_ref(page, candidates[0]):
                print(">>> 保存后无法重新打开这条草稿")
                return False
            if expected_covers:
                if not await _wait_cover_editor_ready(page):
                    print(">>> 保存后的草稿封面编辑器未就绪")
                    return False
            if not await _verify_draft_form_fields(page, title, desc):
                return False
            if expected_covers:
                covers_ok = await _verify_wechat_cover_previews(
                    page, expected_covers
                )
                if not covers_ok:
                    return False
            return True
        await asyncio.sleep(2)
    print(f">>> 草稿箱未找到本次新增且描述精确匹配的草稿: {expected_desc[:80]}")
    return False


async def _read_form_text(page, selectors):
    """Read the first populated form field matching the selectors."""
    for selector in selectors:
        try:
            field = page.locator(selector).first
            if await field.count() == 0:
                continue
            value = await field.evaluate(
                "element => element.value ?? element.innerText ?? element.textContent ?? ''"
            )
            value = str(value or "").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


async def _verify_draft_form_fields(page, title, desc):
    """Confirm the reopened draft still has the requested form title and description."""
    actual_title = await _read_form_text(page, TITLE_SELECTORS)
    actual_desc = await _read_form_text(page, DESC_SELECTORS)
    title_ok = _normalize_draft_text(actual_title) == _normalize_draft_text(title)
    desc_ok = not desc or _normalize_draft_text(actual_desc) == _normalize_draft_text(desc)
    if not title_ok:
        print(f">>> 草稿表单标题不匹配: 期望={title!r} 实际={actual_title!r}")
    if not desc_ok:
        print(f">>> 草稿表单描述不匹配: 期望={desc!r} 实际={actual_desc!r}")
    if title_ok and desc_ok:
        print(">>> 草稿表单标题与描述均已回读确认")
    return title_ok and desc_ok


async def _click_final_action(page, is_public):
    """Click only the action requested by the caller; never guess publish."""
    if is_public:
        selectors, action = PUBLISH_SELECTORS, "发布"
    else:
        selectors, action = SAVE_DRAFT_SELECTORS, "存草稿"
    await _confirm_wechat_original_dialog(page)
    btn = await _try_selectors(page, selectors, f"{action}按钮")
    if not btn:
        raise RuntimeError(f"找不到{action}按钮")
    print(f">>> 点击{action}按钮...")
    try:
        await btn.click()
    except Exception as click_error:
        print(f">>> {action}按钮被弹层遮挡，改用DOM点击: {click_error}")
        clicked = await btn.evaluate(
            "element => { if (element.disabled) return false; element.click(); return true; }"
        )
        if not clicked:
            raise
    return action


async def _wait_publish_resources_ready(page, timeout=600):
    """Wait until generated covers/cards finish and the form has no title error."""
    print(f">>> 等待封面与分享卡片生成完成（最多 {timeout} 秒）...")
    start = time.time()
    last_generating = -1
    while time.time() - start < timeout:
        text = await _wujie_text(page)
        generating = text.count("生成中")
        if generating != last_generating:
            print(f">>> 待生成资源: {generating}")
            last_generating = generating
        if "标题超过16字限制" in text or "标题不能超过" in text:
            raise RuntimeError("微信短标题超过 16 字限制")
        if "标题包含特殊字符" in text:
            raise RuntimeError("微信短标题包含平台不支持的标点")
        if generating == 0:
            print(">>> 封面与分享卡片生成完成")
            return True
        await asyncio.sleep(2)
    print(">>> 封面或分享卡片生成超时")
    return False


async def _save_draft_after_resource_timeout(page, title, desc, known_drafts):
    """Preserve an already-uploaded video when generated resources are slow."""
    print(">>> 平台资源生成超时，先保存草稿保留已上传视频...")
    action = await _click_final_action(page, False)
    await asyncio.sleep(5)
    print(f">>> 超时恢复点击后页面: {page.url}")
    if not await _verify_draft_saved(
        page,
        title,
        desc,
        expected_covers=None,
        known_drafts=known_drafts,
    ):
        raise RuntimeError("平台资源生成超时后保存草稿失败")
    print(f">>> {action}已作为超时恢复保存，继续补齐封面")
    return True


async def _wait_cover_editor_ready(page, timeout=180):
    """Wait for WeChat's generated preview before opening a cover editor."""
    start = time.time()
    last_generating = -1
    last_processing = None
    last_nodes_ready = None
    ready_since = None
    while time.time() - start < timeout:
        text = await _wujie_text(page)
        cover_text = await _cover_editor_text(page)
        combined_text = f"{text}\n{cover_text}"
        generating = combined_text.count("生成中")
        processing = "视频处理中" in combined_text
        nodes_ready = await _cover_editor_nodes_ready(page)
        if generating != last_generating:
            print(f">>> 封面编辑器待生成资源: {generating}")
            last_generating = generating
        if processing != last_processing:
            if processing:
                print(">>> 封面编辑器仍在处理视频，等待可编辑状态...")
            last_processing = processing
        if nodes_ready != last_nodes_ready:
            if nodes_ready:
                print(">>> 封面编辑器节点已挂载")
            else:
                print(">>> 等待封面编辑器节点挂载...")
            last_nodes_ready = nodes_ready
        is_ready = (
            nodes_ready
            and generating == 0
            and "预览图生成中" not in combined_text
            and "视频处理中" not in combined_text
        )
        if is_ready:
            ready_since = ready_since or time.time()
            # WeChat can briefly remove the badge before the popover is usable.
            # Require a stable ready state so an early text snapshot cannot pass.
            if time.time() - ready_since >= 3:
                print(">>> 封面编辑器已可编辑")
                return True
        else:
            ready_since = None
        await asyncio.sleep(1)
    print(">>> 封面编辑器资源生成超时")
    return False


async def _cover_editor_nodes_ready(page):
    """Require both real cover cards and their edit buttons to be attached."""
    selectors = (
        ".vertical-cover-wrap",
        ".vertical-cover-wrap .edit-btn",
        ".horizon-cover-wrap",
        ".horizon-cover-wrap .edit-btn",
    )
    try:
        for selector in selectors:
            if await page.locator(selector).count() == 0:
                return False
        return True
    except Exception:
        return False


async def _save_cover_editor_debug(page):
    """Capture the page state when the cover editor cannot be opened."""
    debug_path = os.path.join(SKILL_DIR, "assets", "cover_editor_failure.png")
    try:
        await page.screenshot(path=debug_path, full_page=True)
        print(f">>> 已保存封面编辑失败截图: {debug_path}")
    except Exception as screenshot_error:
        print(f">>> 保存封面编辑失败截图失败: {screenshot_error}")
    try:
        page_text = await _wujie_text(page)
        print(f">>> 封面编辑失败页面文本片段: {page_text[:1200]}")
    except Exception:
        pass


async def _cover_editor_text(page):
    """Read cover-card text, including WeChat's hidden readiness tooltip."""
    try:
        return await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                return Array.from(
                    root.querySelectorAll('.vertical-cover-wrap, .horizon-cover-wrap')
                ).map((element) => element.textContent || '').join('\\n');
            }"""
        )
    except Exception:
        return ""


async def _click_visible_button(page, label, timeout=5000):
    buttons = page.locator("button", has_text=label)
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        count = await buttons.count()
        for index in range(count):
            button = buttons.nth(index)
            try:
                if await button.is_visible():
                    disabled = await button.get_attribute("disabled")
                    aria_disabled = await button.get_attribute("aria-disabled")
                    class_name = await button.get_attribute("class") or ""
                    if disabled or aria_disabled == "true" or "disabled" in class_name:
                        continue
                    await button.click()
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


async def _click_visible_text(page, label, timeout=5000):
    """Click a visible text control, including WeChat's non-button actions."""
    candidates = page.get_by_text(label, exact=True)
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        count = await candidates.count()
        for index in range(count):
            candidate = candidates.nth(index)
            try:
                if await candidate.is_visible():
                    await candidate.click()
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


# WeChat keeps the cover dialog's confirm button disabled while the uploaded
# image is still being processed, and renders a full-screen mask on top of it.
# Both states outlast a short poll on a slow route, so the click is driven from
# inside the Wujie shadow root and reports why it is not clickable yet.
_COVER_DIALOG_CONFIRM_JS = """
(label) => {
    const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const mask = Array.from(root.querySelectorAll('.setting-cover-mask')).some(visible);
    // Only the cover editor's own dialogs may be confirmed. The page keeps many
    // unrelated hidden dialogs, and the innermost open one is the last visible
    // wrapper in document order (the crop dialog nests inside the cover dialog).
    const wrappers = Array.from(
        root.querySelectorAll('.edit-cover-dialog-container .weui-desktop-dialog__wrp')
    ).filter(visible);
    if (wrappers.length === 0) {
        return {title: '', mask, clicked: false, reason: 'no-cover-dialog', buttons: [], allButtons: []};
    }
    const scope = wrappers[wrappers.length - 1];
    const titleEl = scope.querySelector('.weui-desktop-dialog__title');
    const title = titleEl ? (titleEl.textContent || '').trim() : '';
    const describe = (btn) => {
        const rect = btn.getBoundingClientRect();
        return {
            text: (btn.textContent || '').trim(),
            className: btn.className || '',
            disabled: btn.disabled === true
                || btn.getAttribute('aria-disabled') === 'true'
                || /disabled/.test(btn.className || ''),
            visible: visible(btn),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
        };
    };
    const buttons = Array.from(scope.querySelectorAll('button'));
    const matched = buttons
        .map((btn) => ({btn, info: describe(btn)}))
        .filter((item) => item.info.text === label);
    const state = {
        title,
        mask,
        clicked: false,
        reason: 'not-found',
        buttons: matched.map((item) => item.info),
        allButtons: buttons.map(describe).filter((info) => info.text).slice(0, 12),
    };
    if (matched.length === 0) return state;
    const enabled = matched.filter((item) => item.info.visible && !item.info.disabled);
    if (enabled.length === 0) {
        state.reason = matched.some((item) => item.info.disabled) ? 'disabled' : 'hidden';
        return state;
    }
    if (mask) {
        state.reason = 'masked';
        return state;
    }
    const primary = enabled.find((item) => /weui-desktop-btn_primary/.test(item.info.className));
    const target = primary || enabled[enabled.length - 1];
    target.btn.click();
    state.clicked = true;
    state.reason = 'clicked';
    return state;
}
"""


async def _cover_dialog_confirm_state(page, label="确认"):
    """Probe (and optionally click) the active cover dialog's confirm button."""
    try:
        return await page.evaluate(_COVER_DIALOG_CONFIRM_JS, label)
    except Exception as exc:
        return {"clicked": False, "reason": f"evaluate-error: {exc}", "buttons": []}


async def _save_cover_dialog_debug(page, tag="confirm"):
    """Capture the cover dialog DOM so a failed confirm can be diagnosed."""
    assets_dir = os.path.join(SKILL_DIR, "assets")
    try:
        os.makedirs(assets_dir, exist_ok=True)
    except Exception:
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(assets_dir, f"cover_dialog_{tag}_{stamp}.html")
    png_path = os.path.join(assets_dir, f"cover_dialog_{tag}_{stamp}.png")
    try:
        html = await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                return root.innerHTML;
            }"""
        )
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(html or "")
        print(f">>> 已保存封面弹层 DOM: {html_path}")
    except Exception as exc:
        print(f">>> 保存封面弹层 DOM 失败: {exc}")
    try:
        await page.screenshot(path=png_path, full_page=False)
        print(f">>> 已保存封面弹层截图: {png_path}")
    except Exception:
        pass


def _cover_dialog_canvas_selector(orientation):
    """Return the crop canvas selector for the requested cover orientation."""
    if orientation == "portrait":
        return ".cover-croppie.vertical canvas.cr-image"
    if orientation == "share":
        return ".cover-croppie.normal canvas.cr-image, .cover-croppie.horizon canvas.cr-image"
    return ".croppie-container canvas.cr-image"


def _expected_cover_ratio(orientation):
    """Width/height ratio WeChat's cropper binds for each cover slot."""
    if orientation == "portrait":
        return 3 / 4
    if orientation == "share":
        return 4 / 3
    return None


async def _cover_dialog_canvas_size(page, orientation):
    """Bound bitmap size of the crop canvas in the active cover dialog.

    WeChat binds the uploaded cover to croppie only after it reaches their CDN;
    until then the canvas still holds the video frame. Headless screenshots do
    not reliably capture canvas pixels, so the bound bitmap's aspect ratio is
    the dependable signal that the crop source is our cover.
    """
    try:
        return await page.evaluate(
            """(payload) => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const wrappers = Array.from(
                    root.querySelectorAll('.edit-cover-dialog-container .weui-desktop-dialog__wrp')
                ).filter(visible);
                const scope = wrappers.length ? wrappers[wrappers.length - 1] : null;
                if (!scope) return null;
                const bound = (item) => item.width > 0 && item.height > 0 && visible(item);
                let candidates = Array.from(scope.querySelectorAll(payload.canvasSelector)).filter(bound);
                if (!candidates.length) {
                    candidates = Array.from(
                        scope.querySelectorAll('.croppie-container canvas.cr-image')
                    ).filter(bound);
                }
                if (!candidates.length) return null;
                const matchesExpected = (item) => {
                    const ratio = item.width / item.height;
                    if (payload.expectedRatio && Math.abs(ratio - payload.expectedRatio) <= 0.05) {
                        return true;
                    }
                    if (payload.orientation === 'share' && Math.abs(ratio - 1.0) <= 0.05) {
                        return true;
                    }
                    return false;
                };
                const canvas = candidates.find(matchesExpected) || candidates[0];
                return {width: canvas.width, height: canvas.height};
            }""",
            {
                "canvasSelector": _cover_dialog_canvas_selector(orientation),
                "expectedRatio": _expected_cover_ratio(orientation),
                "orientation": orientation,
            },
        )
    except Exception:
        return None


async def _wait_cover_dialog_canvas(page, orientation, seconds=180):
    """Wait until croppie binds a cover with the expected aspect ratio.

    Clicking the uploaded thumbnail is deliberately avoided: a real click on
    that node re-opens the file picker and resets the uploader, which destroys
    the cover that was already bound.
    """
    expected = _expected_cover_ratio(orientation)
    if expected is None:
        return True
    deadline = time.time() + seconds
    last_size = None
    while time.time() < deadline:
        size = await _cover_dialog_canvas_size(page, orientation)
        if isinstance(size, dict) and size.get("width") and size.get("height"):
            current = (size["width"], size["height"])
            if current != last_size:
                last_size = current
                print(
                    f">>> 封面裁剪画布: {current[0]}x{current[1]}"
                    f" (比例 {current[0] / current[1]:.3f}，期望 {expected:.3f})"
                )
            ratio = current[0] / current[1]
            if abs(ratio - expected) <= 0.05:
                return True
            if orientation == "share" and abs(ratio - 1.0) <= 0.05:
                return True
        await asyncio.sleep(1)
    return False


async def _close_cover_dialog(page):
    """Dismiss an open cover dialog so a retry starts from a clean form."""
    try:
        closed = await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const wrappers = Array.from(
                    root.querySelectorAll('.edit-cover-dialog-container .weui-desktop-dialog__wrp')
                ).filter(visible);
                for (const wrp of wrappers.reverse()) {
                    const cancel = Array.from(wrp.querySelectorAll('button'))
                        .find((btn) => (btn.textContent || '').trim() === '取消');
                    const target = cancel || wrp.querySelector('.weui-desktop-dialog__close-btn');
                    if (target) {
                        target.click();
                        return true;
                    }
                }
                return false;
            }"""
        )
    except Exception:
        return False
    if closed:
        await asyncio.sleep(2)
    return bool(closed)


async def _cover_dialog_open(page):
    """Whether any cover-editor dialog is still on screen."""
    try:
        return await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                return Array.from(
                    root.querySelectorAll('.edit-cover-dialog-container .weui-desktop-dialog__wrp')
                ).some(visible);
            }"""
        )
    except Exception:
        return False


async def _cover_dialog_text(page):
    """Visible text of the open cover-editor dialog, for failure diagnosis."""
    try:
        return await page.evaluate(
            """() => {
                const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const wrappers = Array.from(
                    root.querySelectorAll('.edit-cover-dialog-container .weui-desktop-dialog__wrp')
                ).filter(visible);
                const scope = wrappers.length ? wrappers[wrappers.length - 1] : null;
                return scope ? (scope.innerText || '') : '';
            }"""
        )
    except Exception:
        return ""


async def _click_cover_dialog_confirm(page, orientation=None, timeout=240000, stop_if_closed=False):
    """Click the active cover dialog's confirm button, waiting out WeChat processing."""
    deadline = time.time() + timeout / 1000
    last_signature = None
    last_note = 0.0
    while time.time() < deadline:
        state = None
        for label in ("确认", "确定"):
            state = await _cover_dialog_confirm_state(page, label)
            if state.get("clicked"):
                print(f">>> 已点击封面弹层按钮: {label}（弹层: {state.get('title') or '未知'}）")
                return True
            if stop_if_closed and state.get("reason") == "no-cover-dialog":
                print(">>> 封面弹层已在等待确认期间关闭")
                return True
            if state.get("reason") in ("disabled", "masked", "hidden"):
                break
        if stop_if_closed and (state or {}).get("reason") == "no-cover-dialog":
            print(">>> 封面弹层已在等待确认期间关闭")
            return True
        signature = (
            (state or {}).get("title"),
            (state or {}).get("reason"),
            tuple(
                (item.get("text"), item.get("disabled"), item.get("visible"))
                for item in (state or {}).get("buttons", [])
            ),
        )
        now = time.time()
        if signature != last_signature or now - last_note >= 30:
            last_signature = signature
            last_note = now
            detail = ", ".join(
                f"{item.get('text')}{'(禁用)' if item.get('disabled') else ''}"
                f"{'' if item.get('visible') else '(不可见)'}"
                for item in (state or {}).get("buttons", [])
            ) or "无匹配按钮"
            print(
                f">>> 等待封面弹层确认可点击: 弹层={(state or {}).get('title') or '未知'} "
                f"原因={(state or {}).get('reason')} 遮罩={(state or {}).get('mask')} 按钮=[{detail}]"
            )
        await asyncio.sleep(1)
    await _save_cover_dialog_debug(page, "confirm_timeout")
    return False


async def _cover_wrap_images(page, wrap_selector):
    """Read the cover-card preview images inside the Wujie shadow DOM."""
    return await page.evaluate(
        """(selector) => {
            const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
            return Array.from(root.querySelectorAll(selector + ' img')).map((img) => ({
                src: img.src,
                naturalWidth: img.naturalWidth,
                naturalHeight: img.naturalHeight,
            }));
        }""",
        wrap_selector,
    )


def _cover_preview_matches_orientation(images, orientation):
    """Check whether a loaded preview has the expected card aspect ratio."""
    expected_ratio = 3 / 4 if orientation == "portrait" else 4 / 3
    for image in images or []:
        width = int(image.get("naturalWidth") or 0)
        height = int(image.get("naturalHeight") or 0)
        if width > 0 and height > 0 and abs(width / height - expected_ratio) <= 0.03:
            return True
    return False


def _matching_cover_preview(images, orientation):
    """Return the first loaded preview matching the requested card ratio."""
    expected_ratio = 3 / 4 if orientation == "portrait" else 4 / 3
    for image in images or []:
        width = int(image.get("naturalWidth") or 0)
        height = int(image.get("naturalHeight") or 0)
        if width and height and abs(width / height - expected_ratio) <= 0.03:
            return image
    return None


async def _current_cover_preview(page, wrap_selector, orientation):
    images = await _cover_wrap_images(page, wrap_selector)
    return _matching_cover_preview(images, orientation)


def _validate_cover_file(cover_path, orientation, expected_title):
    """Reject stale, wrong-shape, or wrong-title files before WeChat upload."""
    expected_size = (1080, 1440) if orientation == "portrait" else (1080, 810)
    try:
        with Image.open(cover_path) as image:
            if image.size != expected_size:
                raise ValueError(f"尺寸应为 {expected_size[0]}x{expected_size[1]}")
        if not cover_style_matches(cover_path, expected_title, orientation):
            raise ValueError("封面缓存标记与当前封面标题不一致")
    except Exception as e:
        print(f">>> 封面文件校验失败: {cover_path} ({e})")
        return False
    return True


async def _wait_cover_preview_changed(
    page,
    wrap_selector,
    orientation,
    before_sources,
    timeout=20,
    expected_path=None,
):
    """Wait until the outer form card reflects the uploaded cover."""
    start = time.time()
    last_state = None
    while time.time() - start < timeout:
        images = await _cover_wrap_images(page, wrap_selector)
        state = tuple(
            (item.get("src"), item.get("naturalWidth"), item.get("naturalHeight"))
            for item in images
        )
        if state != last_state:
            print(f">>> 封面卡片预览状态: {state}")
            last_state = state
        loaded = [
            item for item in images
            if item.get("naturalWidth") and item.get("naturalHeight")
        ]
        changed = any(item.get("src") not in before_sources for item in loaded)
        if changed and _cover_preview_matches_orientation(loaded, orientation):
            if expected_path:
                content_matches = await _verify_cover_preview_matches(
                    page,
                    {"local_path": os.path.abspath(expected_path)},
                    orientation,
                )
                if not content_matches:
                    print(">>> 封面卡片预览已变化，但像素内容未匹配上传封面")
                    await asyncio.sleep(0.5)
                    continue
            print(">>> 封面卡片预览已切换为上传封面")
            return True
        await asyncio.sleep(0.5)
    print(">>> 封面卡片预览未切换为上传封面")
    return False


async def _active_cover_file_input(page, orientation, timeout=10000):
    """Return the file input belonging to the currently visible cover dialog."""
    title = "编辑个人主页卡片" if orientation == "portrait" else "编辑分享卡片"
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        try:
            index = await page.evaluate(
                """(title) => {
                    const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                    const inputs = Array.from(root.querySelectorAll('.edit-cover-dialog input[type="file"]'));
                    return inputs.findIndex((input) => {
                        const dialog = input.closest('.edit-cover-dialog');
                        const wrapper = dialog?.querySelector('.weui-desktop-dialog__wrp');
                        const dialogTitle = dialog?.querySelector('.weui-desktop-dialog__title')?.innerText || '';
                        return Boolean(
                            dialog
                            && wrapper
                            && wrapper.style.display !== 'none'
                            && dialogTitle.includes(title)
                        );
                    });
                }""",
                title,
            )
        except Exception:
            index = -1
        if isinstance(index, int) and index >= 0:
            inputs = page.locator('.edit-cover-dialog input[type="file"]')
            if await inputs.count() > index:
                return inputs.nth(index)
        await asyncio.sleep(0.2)
    return None


async def _click_cover_recommend_direct_edit(page, timeout=3):
    """Open the real cover editor when WeChat shows its recommendation popover."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            clicked = await page.evaluate(
                """() => {
                    const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const buttons = Array.from(
                        root.querySelectorAll('.img-recommend-wrap .btn-directly-edit button')
                    ).filter(visible);
                    if (!buttons.length) return false;
                    buttons[0].click();
                    return true;
                }"""
            )
        except Exception:
            clicked = False
        if clicked:
            return True
        await asyncio.sleep(0.2)
    return False


async def _upload_wechat_cover_card(page, cover_path, orientation, expected_title):
    """Upload one exact-ratio cover card in WeChat's cover editor."""
    wrap_selector = ".vertical-cover-wrap" if orientation == "portrait" else ".horizon-cover-wrap"
    edit_selector = f"{wrap_selector} .edit-btn"
    try:
        if not _validate_cover_file(cover_path, orientation, expected_title):
            return False
        if not await _wait_cover_editor_ready(page):
            print(">>> 封面编辑器未就绪，无法上传封面")
            await _save_cover_editor_debug(page)
            return False
        wrap = page.locator(wrap_selector).first
        edit_btn = page.locator(edit_selector).first
        try:
            await wrap.wait_for(state="attached", timeout=5000)
            await edit_btn.wait_for(state="attached", timeout=5000)
        except Exception:
            print(f">>> 未找到{'个人主页' if orientation == 'portrait' else '分享'}封面编辑入口")
            await _save_cover_editor_debug(page)
            return False
        before_images = await _cover_wrap_images(page, wrap_selector)
        before_sources = {item.get("src") for item in before_images}

        try:
            await edit_btn.evaluate("element => element.click()")
            await _click_cover_recommend_direct_edit(page)
            image_input = await _active_cover_file_input(page, orientation)
            if image_input is None:
                print(f">>> 未打开{'个人主页' if orientation == 'portrait' else '分享'}封面编辑弹层")
                await _save_cover_editor_debug(page)
                return False
            await image_input.wait_for(state="attached", timeout=10000)
        except Exception:
            print(">>> 封面编辑弹层未显示上传封面入口")
            await _save_cover_editor_debug(page)
            return False
        await image_input.set_input_files(cover_path)

        # WeChat renders a local preview before enabling the crop confirmation.
        dialog_ready = False
        start = time.time()
        while time.time() - start < 15:
            dialog_images = await page.evaluate(
                """() => {
                    const root = document.querySelector('wujie-app')?.shadowRoot?.querySelector('html') || document;
                    // Scope to the open cover dialog: the form behind it also
                    // carries 3:4 / 4:3 previews that would satisfy this gate
                    // before our image has actually rendered in the dialog.
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const wrappers = Array.from(
                        root.querySelectorAll('.edit-cover-dialog-container .weui-desktop-dialog__wrp')
                    ).filter(visible);
                    const scope = wrappers.length ? wrappers[wrappers.length - 1] : root;
                    return Array.from(scope.querySelectorAll('.preview-image')).map((img) => ({
                        naturalWidth: img.naturalWidth,
                        naturalHeight: img.naturalHeight,
                    }));
                }"""
            )
            if _cover_preview_matches_orientation(dialog_images, orientation):
                dialog_ready = True
                break
            await asyncio.sleep(0.5)
        if not dialog_ready:
            print(">>> 封面编辑弹层未显示上传图片预览")
            return False

        # WeChat only switches the crop source to our image once it finishes
        # reaching its CDN; until then the canvas crops the video frame and
        # 确认 would save that wrong source.
        canvas_ok = await _wait_cover_dialog_canvas(page, orientation, 180)
        if not canvas_ok:
            print(">>> 裁剪画布仍未切换到上传封面，关闭弹层重开一次")
            await _close_cover_dialog(page)
            try:
                await edit_btn.evaluate("element => element.click()")
                await _click_cover_recommend_direct_edit(page)
                image_input = await _active_cover_file_input(page, orientation)
                if image_input is None:
                    print(">>> 重开封面编辑弹层失败")
                    return False
                await image_input.wait_for(state="attached", timeout=10000)
                await image_input.set_input_files(cover_path)
            except Exception:
                print(">>> 重开封面编辑弹层失败")
                return False
            canvas_ok = await _wait_cover_dialog_canvas(page, orientation, 180)
        if not canvas_ok:
            print(">>> 裁剪画布始终未切换到上传封面，放弃本次封面设置以保留现有封面")
            await _save_cover_dialog_debug(page, f"canvas_mismatch_{orientation}")
            await _close_cover_dialog(page)
            return False

        confirmed = await _click_cover_dialog_confirm(page, orientation, timeout=240000)
        if not confirmed:
            # No page-wide fallback: clicking an unrelated dialog's 确定 can
            # silently change the form instead of the cover.
            print(">>> 封面编辑弹层的确认按钮始终不可点击，已保存弹层 DOM 与截图")
            return False

        # WeChat ignores the confirm while it is still pushing our image to its
        # CDN: the dialog stays open with no error and the card keeps its old
        # frame. Keep re-clicking until the dialog closes instead of giving up
        # after a fixed number of attempts.
        still_open_deadline = time.time() + 300
        captured_debug = False
        clicks = 0
        # Give WeChat a moment to close the dialog after a successful confirm.
        await asyncio.sleep(8)
        while await _cover_dialog_open(page):
            if time.time() > still_open_deadline:
                print(">>> 封面弹层 300 秒后仍未关闭，放弃本次封面设置")
                break
            clicks += 1
            dialog_text = (await _cover_dialog_text(page)).replace("\n", " ").strip()
            print(
                f">>> 确认点击 {clicks} 次后弹层仍未关闭，弹层文本: {dialog_text[:240] or '(空)'}"
            )
            if not captured_debug:
                await _save_cover_dialog_debug(page, f"still_open_{orientation}")
                captured_debug = True
            if not await _click_cover_dialog_confirm(page, orientation, timeout=30000, stop_if_closed=True):
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(10)

        if not await _wait_cover_preview_changed(
            page,
            wrap_selector,
            orientation,
            before_sources,
            timeout=180,
            expected_path=cover_path,
        ):
            await _save_cover_dialog_debug(page, f"preview_unchanged_{orientation}")
            # Some flows show a second crop confirmation after the first save.
            if await _click_cover_dialog_confirm(page, orientation, timeout=60000):
                if not await _wait_cover_preview_changed(
                    page,
                    wrap_selector,
                    orientation,
                    before_sources,
                    timeout=120,
                    expected_path=cover_path,
                ):
                    return False
            else:
                return False
        preview = await _current_cover_preview(page, wrap_selector, orientation)
        if not preview:
            print(f">>> {'个人主页' if orientation == 'portrait' else '分享'}封面预览缺失")
            return False
        print(
            f">>> 已上传{'个人主页' if orientation == 'portrait' else '分享'}封面: "
            f"{preview.get('naturalWidth')}x{preview.get('naturalHeight')}"
        )
        # Let the "cover updated" toast clear before another cover editor opens.
        await asyncio.sleep(2)
        preview = dict(preview or {})
        preview["local_path"] = os.path.abspath(cover_path)
        return preview
    except Exception as e:
        print(f">>> 上传{'个人主页' if orientation == 'portrait' else '分享'}封面失败: {e}")
        return False


async def _set_wechat_cover(
    page, portrait_cover=None, share_cover=None, expected_title=None, finish=False
):
    """Set generated covers on the create form or an existing cover-edit page."""
    if not portrait_cover and not share_cover:
        return False
    previews = {}
    if portrait_cover:
        preview = await _upload_wechat_cover_card(
            page, portrait_cover, "portrait", expected_title
        )
        if not preview:
            return False
        previews["portrait"] = preview
    if share_cover:
        preview = await _upload_wechat_cover_card(
            page, share_cover, "share", expected_title
        )
        if not preview:
            return False
        previews["share"] = preview
    if finish and previews:
        if not await _click_visible_button(page, "完成", timeout=10000):
            print(">>> 未找到封面编辑页完成按钮")
            return False
        await asyncio.sleep(3)
    return previews


def _decode_data_url(data_url):
    try:
        metadata, encoded = str(data_url).split(",", 1)
        if "base64" not in metadata:
            return None
        return base64.b64decode(encoded, validate=False)
    except Exception:
        return None


async def _preview_image_bytes(page, src):
    """Read a preview image without relying on a stable CDN URL."""
    if not src:
        return None
    if str(src).startswith("data:"):
        return _decode_data_url(src)
    if str(src).startswith("blob:"):
        try:
            data_url = await page.evaluate(
                """async (src) => {
                    const blob = await fetch(src).blob();
                    return await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    });
                }""",
                src,
            )
            return _decode_data_url(data_url)
        except Exception as e:
            print(f">>> 读取封面预览 blob 失败: {e}")
            return None
    try:
        response = await page.request.get(src)
        if response.ok:
            return await response.body()
        print(f">>> 读取封面预览失败: HTTP {response.status}")
    except Exception as e:
        print(f">>> 读取封面预览失败: {e}")
    return None


def _cover_preview_similarity(local_path, preview_bytes):
    """Return a normalized RMS pixel difference; lower means more similar."""
    try:
        with Image.open(local_path) as local_image, Image.open(io.BytesIO(preview_bytes)) as preview_image:
            local = ImageOps.fit(local_image.convert("RGB"), (128, 128))
            preview = ImageOps.fit(preview_image.convert("RGB"), (128, 128))
            difference = ImageChops.difference(local, preview)
            histogram = difference.histogram()
            total = 0
            sum_squares = 0
            for channel in range(3):
                channel_histogram = histogram[channel * 256:(channel + 1) * 256]
                total += sum(channel_histogram)
                sum_squares += sum(
                    value * value * count
                    for value, count in enumerate(channel_histogram)
                )
            return (sum_squares / total) ** 0.5
    except Exception as e:
        print(f">>> 比较封面预览失败: {e}")
        return None


async def _verify_cover_preview_matches(page, expected, orientation):
    if not expected or not expected.get("local_path"):
        return False
    label = "个人主页" if orientation == "portrait" else "分享"
    wrap_selector = ".vertical-cover-wrap" if orientation == "portrait" else ".horizon-cover-wrap"
    images = await _cover_wrap_images(page, wrap_selector)
    current = _matching_cover_preview(images, orientation)
    if not current:
        loaded = [
            img for img in (images or [])
            if int(img.get("naturalWidth") or 0) > 0 and int(img.get("naturalHeight") or 0) > 0
        ]
        if loaded:
            dims = ", ".join(f"{img.get('naturalWidth')}x{img.get('naturalHeight')}" for img in loaded)
            print(f">>> 回读时{label}封面比例不匹配（疑似仍是视频原帧）: {dims}")
            return False
        print(f">>> 回读时{label}封面预览缺失")
        return None
    if current.get("src") and current.get("src") == expected.get("src"):
        print(f">>> 回读{label}封面 URL 一致")
        return True

    preview_bytes = await _preview_image_bytes(page, current.get("src"))
    if not preview_bytes:
        return False
    score = _cover_preview_similarity(expected.get("local_path"), preview_bytes)
    if score is None:
        return False
    print(
        f">>> 回读{label}封面相似度: "
        f"{score:.2f} (阈值 20.00)"
    )
    return score <= 20.0


async def _verify_wechat_cover_previews(page, expected_previews):
    """Confirm saved cover cards, treating lazy-loaded previews as warnings."""
    if not expected_previews:
        return False
    missing_previews = []
    for orientation, label in (("portrait", "个人主页"), ("share", "分享")):
        expected = expected_previews.get(orientation)
        if not expected:
            print(f">>> 缺少{label}封面回读基准")
            return False
        result = await _verify_cover_preview_matches(page, expected, orientation)
        if result is None:
            missing_previews.append(label)
            print(f">>> {label}封面预览未回读，继续以草稿箱和表单回读为准")
            continue
        if result is False:
            print(f">>> {label}封面回读校验失败")
            return False
    if missing_previews:
        print(f">>> 封面预览缺失: {', '.join(missing_previews)}；草稿已保存，不判失败")
    else:
        print(">>> 横竖双封面均已回读确认")
    return True


async def _open_draft_by_text(page, text):
    """Reopen the one saved draft whose full description matches exactly."""
    if not text:
        return False
    if not await _open_draft_list(page):
        return False
    needle = _normalize_draft_text(text)
    for _ in range(30):
        entries = await _draft_entries_all_pages(page)
        candidates = [
            entry
            for entry in entries
            if _normalize_draft_text(entry.get("title")) == needle
        ]
        if len(candidates) > 1:
            print(f">>> 草稿箱匹配到 {len(candidates)} 条同描述草稿，拒绝猜测")
            return False
        if len(candidates) == 1:
            return await _open_draft_entry_ref(page, candidates[0])
        await asyncio.sleep(1)
    print(">>> 草稿箱未找到描述精确匹配的草稿")
    return False


def _repair_draft_candidates(entries, desc, draft_time=None):
    """Return exact-description drafts, optionally narrowed by visible time."""
    expected = _normalize_draft_text(desc)
    candidates = [
        entry
        for entry in entries or []
        if _normalize_draft_text(entry.get("title")) == expected
    ]
    if draft_time:
        candidates = [
            entry
            for entry in candidates
            if str(draft_time) in str(entry.get("time") or "")
        ]
    return candidates


def _select_repair_draft_entry(entries, desc, draft_time=None):
    """Select one repair target, refusing to guess among duplicate drafts."""
    candidates = _repair_draft_candidates(entries, desc, draft_time)
    if len(candidates) != 1:
        return None
    return candidates[0]


async def _set_wechat_covers_same_page(
    page, portrait_cover, share_cover, expected_title, max_attempts=2
):
    """Set both cover cards on the create form before saving one draft."""
    previews = {}
    for orientation, cover_path in (
        ("portrait", portrait_cover),
        ("share", share_cover),
    ):
        preview = None
        for attempt in range(1, max_attempts + 1):
            preview = await _upload_wechat_cover_card(
                page, cover_path, orientation, expected_title
            )
            if preview:
                previews[orientation] = preview
                break
            if attempt < max_attempts:
                print(
                    f">>> {'分享' if orientation == 'share' else '个人主页'}"
                    f"封面设置失败，准备重试 ({attempt}/{max_attempts})"
                )
        if not preview:
            print(
                f">>> {'分享' if orientation == 'share' else '个人主页'}"
                "封面未能确认设置"
            )
    return previews or False


async def _set_wechat_covers_for_upload(
    page, portrait_cover, share_cover, expected_title
):
    """Set both cover cards before a draft can be considered complete."""
    if not portrait_cover or not share_cover:
        raise RuntimeError("微信上传需要横竖两张封面，封面生成结果不完整")
    previews = await _set_wechat_covers_same_page(
        page, portrait_cover, share_cover, expected_title
    )
    incomplete = not previews or not previews.get("portrait") or not previews.get("share")
    if incomplete:
        raise RuntimeError("微信横竖封面未完全设置成功，已停止保存草稿")
    return previews or {}


async def _reopen_matching_draft(page, desc, known_drafts=None, timeout=600):
    """Open the draft list and reopen the unique matching draft by description."""
    if not await _open_draft_list(page):
        print(">>> 无法打开草稿箱")
        return False
    entries = await _draft_entries_all_pages(page)
    target = None
    if known_drafts is not None:
        new_matches = _new_draft_entries(entries, desc, known_drafts)
        if len(new_matches) == 1:
            target = new_matches[0]
    if target is None:
        target = _select_repair_draft_entry(entries, desc)
    if not target:
        print(">>> 未能在草稿箱唯一匹配到本次草稿，跳过盲目重开")
        return False
    if not await _open_draft_entry_ref(page, target):
        return False
    return await _wait_cover_editor_ready(page, timeout=timeout)


async def _repair_existing_draft_missing_covers(
    page,
    title,
    desc,
    portrait_cover,
    share_cover,
    cover_headline,
    known_drafts=None,
):
    """Inspect a unique existing matching draft and backfill any missing/mismatched covers."""
    entries = await _draft_entries_all_pages(page)
    if not entries and await _open_draft_list(page):
        entries = await _draft_entries_all_pages(page)
    target = _select_repair_draft_entry(entries, desc)
    if not target:
        candidates = _repair_draft_candidates(entries, desc)
        if len(candidates) > 1:
            print(
                ">>> 草稿箱存在多条同描述草稿；跳过重复上传，"
                "如需补封面请使用 --repair-covers --draft-time"
            )
        else:
            print(
                ">>> 草稿箱已存在同描述草稿；跳过重复上传，"
                "如需补封面请使用 --repair-covers"
            )
        return True
    if not await _open_draft_entry_ref(page, target):
        return False
    if not await _wait_cover_editor_ready(page, timeout=600):
        print(">>> 已有草稿封面编辑器未就绪，无法补修封面")
        return False

    previews = {}
    missing = []
    for orient, path in (("portrait", portrait_cover), ("share", share_cover)):
        if not path:
            continue
        expected_info = {"local_path": os.path.abspath(path)}
        result = await _verify_cover_preview_matches(page, expected_info, orient)
        if result is False:
            missing.append((orient, path))
        else:
            previews[orient] = expected_info

    if not missing:
        print(">>> 草稿箱已存在同描述草稿且横竖封面均完整，跳过重复上传")
        return True

    print(f">>> 已有草稿缺失封面（{', '.join(o for o, _ in missing)}），开始自动补修...")
    for idx, (orient, path) in enumerate(missing):
        if idx > 0 and hasattr(page, "locator") and not await _cover_editor_nodes_ready(page):
            await _reopen_matching_draft(page, desc, known_drafts=None, timeout=600)
        preview = await _upload_wechat_cover_card(page, path, orient, cover_headline)
        if not preview:
            return False
        previews[orient] = preview
        action = await _click_final_action(page, False)
        await asyncio.sleep(6)
        is_last = idx == len(missing) - 1
        expected_covers = previews if is_last else None
        if not await _verify_draft_saved(
            page,
            title,
            desc,
            expected_covers=expected_covers,
            known_drafts=None,
        ):
            print(f">>> {action}后补修封面核验失败")
            return False
    print(">>> 已有草稿缺失封面已自动补修完成！")
    return True


async def _set_wechat_covers_sequentially(
    page,
    portrait_cover,
    share_cover,
    expected_title,
    title,
    desc,
    known_drafts,
    video_path=None,
):
    """Set one cover, save, reopen, then set the other cover.

    WeChat's current editor reliably accepts only one uploaded cover before it
    needs a draft save/reopen. Keeping both uploads on one page makes the
    second confirm button disappear and turns a finished upload into an
    ambiguous failure.
    """
    previews = {}
    for orientation, cover_path in (
        ("portrait", portrait_cover),
        ("share", share_cover),
    ):
        label = "个人主页" if orientation == "portrait" else "分享"
        if orientation == "share" and hasattr(page, "locator") and not await _cover_editor_nodes_ready(page):
            print(">>> 正在从草稿箱重新打开草稿以设置分享卡片封面...")
            await _reopen_matching_draft(page, desc, known_drafts=known_drafts, timeout=600)

        preview = None
        for attempt in range(1, 4):
            preview = await _upload_wechat_cover_card(
                page, cover_path, orientation, expected_title
            )
            if preview:
                break
            if attempt < 3:
                # Right after the video finishes processing, WeChat's pending
                # auto cover generation can overwrite a custom cover that was
                # just confirmed. Wait the race out, dismiss the dialog and
                # apply the cover again instead of failing the whole upload.
                print(
                    f">>> {label}封面未生效，关闭弹层等待 45 秒后重试 ({attempt}/3)"
                )
                await _close_cover_dialog(page)
                await asyncio.sleep(45)
        if not preview:
            if orientation == "share" and previews.get("portrait"):
                # The draft already holds the video and the portrait cover. A
                # share card we cannot pixel-verify is a cosmetic difference,
                # not a lost upload: save the draft and record success with a
                # warning instead of reporting the whole upload as failed.
                print(
                    f">>> {label}封面未能像素校验通过；草稿已含视频与竖封面，"
                    "先存草稿保住本次上传"
                )
                action = await _click_final_action(page, False)
                await asyncio.sleep(6)
                if not await _verify_draft_saved(
                    page, title, desc, known_drafts=known_drafts
                ):
                    raise RuntimeError(f"{action}后草稿箱核验失败")
                print(
                    ">>> 警告：分享卡片可能仍是微信自动封面，请在草稿箱目视确认；"
                    "本次上传按成功记录"
                )
                previews[orientation] = {"local_path": cover_path, "unverified": True}
                continue
            raise RuntimeError(f"微信{label}封面未能确认设置")
        previews[orientation] = preview

        action = await _click_final_action(page, False)
        await asyncio.sleep(6)
        expected_covers = previews if orientation == "share" else None
        if not await _verify_draft_saved(
            page,
            title,
            desc,
            expected_covers=expected_covers,
            known_drafts=known_drafts,
        ):
            raise RuntimeError(f"{action}后草稿箱核验失败")
        if orientation == "portrait" and video_path:
            # The draft now exists even if the share cover later fails, so pin
            # its box entry for an exact --repair-covers run.
            await _record_draft_reference(video_path, page, desc)
    return previews


def _generate_wechat_covers(video_path, title, cover_title=None):
    """Generate 3:4 and 4:3 covers, reusing outputs when possible.

    ``cover_title`` is an explicit override for the text rendered on the cover
    images only. It intentionally does not change the WeChat form title.
    """
    if not _COVER_UTILS_AVAILABLE:
        print(">>> ⚠️ 封面生成模块不可用，本次不自动设置微信封面")
        return None, None
    effective_cover_title = str(cover_title or "").strip() or title
    try:
        portrait_path, share_path = generate_platform_covers(
            video_path,
            "wechat",
            effective_cover_title,
        )
        return portrait_path, share_path
    except Exception as e:
        print(f">>> ⚠️ 微信封面生成失败: {e}")
        return None, None


async def _wait_published(page, timeout=30):
    """等待发布完成（检测成功提示或页面跳转）。"""
    print(">>> 等待发布完成...")
    start = time.time()
    while time.time() - start < timeout:
        for indicator in ['text="发布成功"', 'text="已发布"', '[class*="success"]', '[class*="publish-success"]']:
            try:
                if await page.locator(indicator).count() > 0:
                    print(">>> 发布成功！")
                    return True
            except Exception:
                continue
        # 检测页面跳转
        if "/platform/post/manage" in page.url or "/platform/home" in page.url:
            print(">>> 页面已跳转，发布可能成功")
            return True
        await asyncio.sleep(1)
    print(">>> 未检测到明确的成功提示（可能已发布，建议人工确认）")
    return True  # 宽松返回，让批量流程继续


async def upload_one_video(video_path, title, desc=None, tags=None, is_public=False,
                           llm_provider="dashscope", llm_model="glm-5.2", headless=False,
                           max_retries=2, auto_title=True, cover_title=None, attribution=None,
                           collection=None):
    """上传单个视频到微信视频号。返回 True/False。"""
    if not os.path.exists(video_path):
        print(f">>> 视频文件不存在: {video_path}")
        return False
    if not _within_wechat_duration_limit(video_path):
        return False

    cached_metadata = _load_cached_upload_metadata(video_path)
    if desc is None:
        cached_description = str(cached_metadata.get("description") or "").strip()
        if cached_description:
            desc = cached_description
            print(">>> 描述来源：upload_metadata.json")
    cached_wechat_title = str(cached_metadata.get("wechat_title") or "").strip()
    if auto_title and cached_wechat_title:
        title = _normalize_wechat_title(cached_wechat_title)
        auto_title = False
        print(">>> 标题来源：upload_metadata.json")
    if not cover_title:
        cover_title = str(cached_metadata.get("cover_title") or "").strip()
        if cover_title:
            print(">>> 封面标题来源：upload_metadata.json")

    original_title = str(title or "").strip()
    if auto_title:
        try:
            title = build_title(
                video_path,
                original_title,
                llm_provider=llm_provider,
                llm_model=llm_model,
            )
        except SubtitleTitleError as e:
            print(f">>> ❌ 自动标题失败：{e}")
            print(">>> 已停止上传。请先生成/翻译字幕，或显式使用 --no_auto_title。")
            return False
    else:
        title = _normalize_wechat_title(original_title)
        if original_title != title:
            print(f">>> 标题已按微信短标题规则规范化: {title}")

    # 生成元数据
    desc = build_description(video_path, title, desc, llm_provider, llm_model)
    if not tags:
        sub_text, sub_src = _read_subtitle_text(video_path)
        if sub_text:
            gen_tags = _generate_tags_via_llm(sub_text, title, os.path.basename(sub_src), llm_provider, llm_model)
            if gen_tags:
                tags = gen_tags
                print(f">>> 标签来源：根据字幕生成（{os.path.basename(sub_src)}）")
    if not tags:
        tags = ["人工智能", "AI", "科技"]
        print(">>> 标签来源：默认标签")
    cover_title = str(cover_title or "").strip()
    if not cover_title:
        try:
            cover_title = build_cover_title(
                video_path,
                llm_provider=llm_provider,
                llm_model=llm_model,
                attribution=attribution,
            )
        except SubtitleTitleError as e:
            print(f">>> ❌ 自动封面标题失败：{e}")
            print(">>> 已停止上传。封面标题不能来自表单标题或文件名。")
            return False
    cover_headline = cover_title
    portrait_cover, share_cover = _generate_wechat_covers(
        video_path, title, cover_title=cover_title
    )
    if not portrait_cover or not share_cover:
        print(">>> ❌ 微信横竖封面生成失败，已停止上传，避免保存无标题封面")
        return False

    print(f"\n{'=' * 60}")
    print(f"📹 视频号上传: {title}")
    print(f"   文件: {video_path}")
    print(f"   描述: {desc[:60]}...")
    print(f"   标签: {', '.join(tags)}")
    print(f"   封面标题: {cover_headline}")
    print(f"   可见性: {'公开' if is_public else '仅自己可见'}")
    print(f"{'=' * 60}")

    from playwright.async_api import async_playwright
    last_error = None
    file_selected = False
    ambiguous_draft_checked = False
    ambiguous_draft_verified = False
    for attempt in range(1, max_retries + 1):
        try:
            async with async_playwright() as p:
                context, page, profile_lock = await _ensure_browser(p, headless=headless)
                try:
                    await _goto_wechat_page(page, WECHAT_HOME_URL)
                    await asyncio.sleep(2)
                    if not await _ensure_logged_in(page, allow_qr=not headless):
                        return False
                    await _goto_wechat_page(page, WECHAT_HOME_URL)
                    await asyncio.sleep(2)
                    known_drafts = await _snapshot_drafts(page)
                    if known_drafts is None:
                        print(">>> 草稿箱读取失败，先复查微信登录态...")
                        if not await _ensure_logged_in(page):
                            return False
                        await _goto_wechat_page(page, WECHAT_HOME_URL)
                        await asyncio.sleep(2)
                        known_drafts = await _snapshot_drafts(page)
                    if known_drafts is None:
                        raise RuntimeError("上传前无法读取草稿箱，已停止以避免修改旧草稿")
                    if _known_draft_has_description(known_drafts, desc):
                        repaired = await _repair_existing_draft_missing_covers(
                            page,
                            title,
                            desc,
                            portrait_cover,
                            share_cover,
                            cover_headline,
                            known_drafts=known_drafts,
                        )
                        if not repaired:
                            return False
                        marker = os.path.join(os.path.dirname(video_path), ".wechat_uploaded")
                        with open(marker, "w", encoding="utf-8") as handle:
                            handle.write("1")
                        return True

                    def _log_wechat_response(response):
                        # Normal page polling returns many 200/201 responses.
                        # Keep the upload log readable and report failures only.
                        if response.status < 400:
                            return
                        if "channels.weixin.qq.com" not in response.url.lower():
                            return
                        print(
                            f">>> ⚠️ 微信接口异常: "
                            f"{response.status} {response.url[:220]}"
                        )
                    page.on("response", _log_wechat_response)

                    def _log_wechat_request_failed(request):
                        failure = request.failure
                        if _is_noisy_wechat_request_failure(request.url, failure):
                            return
                        print(
                            f">>> ⚠️ 微信网络请求失败: "
                            f"{request.method} {request.url[:220]} {failure or 'unknown'}"
                        )

                    page.on("requestfailed", _log_wechat_request_failed)

                    # 1. 直接打开发布页。首页按钮在 Wujie 微前端中加载时机不稳定，
                    # 直达 /post/create 后顶层文档即可看到 file input。
                    print(f">>> 打开发布页面 (尝试 {attempt}/{max_retries})...")
                    await _goto_wechat_page(page, WECHAT_CREATE_URL)
                    await asyncio.sleep(3)

                    # 登录态失效时会跳回登录页；已登录时发布页会加载 file input。
                    print(">>> 等待页面加载...")
                    file_input = None
                    for _ in range(20):
                        if "login" in page.url.lower():
                            break
                        try:
                            candidate = page.locator('input[type="file"]').first
                            if await candidate.count() > 0:
                                file_input = candidate
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)

                    if file_input is None and "login" in page.url.lower():
                        if headless:
                            print(">>> 需要扫码登录；无头模式不会弹出二维码窗口。")
                            print(f">>> 请先运行: {sys.executable} -u {os.path.abspath(__file__)} --login")
                            return False
                        print(">>> 需要扫码登录，请在弹出的浏览器中扫码...")
                        ok = await _wait_login(page, timeout=600)
                        if not ok:
                            return False
                        await asyncio.sleep(3)
                        await _goto_wechat_page(page, WECHAT_CREATE_URL)
                        await asyncio.sleep(5)
                        for _ in range(20):
                            candidate = page.locator('input[type="file"]').first
                            if await candidate.count() > 0:
                                file_input = candidate
                                break
                            await asyncio.sleep(1)

                    if file_input is None:
                        error_path = os.path.join(SKILL_DIR, "assets", "page_error.html")
                        with open(error_path, "w", encoding="utf-8") as f:
                            f.write(await page.content())
                        raise RuntimeError("找不到视频上传入口，微信页面结构可能已更新")

                    # 2. 上传视频文件
                    print(f">>> 选择视频文件: {os.path.basename(video_path)}")
                    await file_input.set_input_files(video_path)
                    file_selected = True
                    print(">>> 视频文件已选择，等待上传...")

                    # 3. 等待上传完成
                    await asyncio.sleep(3)  # 等待上传开始
                    ok = await _wait_upload_complete(page, timeout=10000)
                    if not ok:
                        raise RuntimeError("视频上传超时或失败")

                    # 4. 填写标题
                    await asyncio.sleep(2)
                    await _set_text(page, TITLE_SELECTORS, title, "标题")

                    # 5. 填写描述
                    await _set_text(page, DESC_SELECTORS, desc, "描述")

                    # 6. 添加标签
                    await _add_tags(page, tags)

                    # 7. 设置微信必选发布项，再处理可见性和封面。
                    if not await _set_required_publish_settings(page):
                        raise RuntimeError("微信位置、视频标注或原创标志未设置成功")

                    if collection and str(collection).strip():
                        await _set_wechat_collection(page, str(collection).strip())

                    # 8. 设置可见性（公开 vs 仅自己可见）
                    resources_ready = await _wait_publish_resources_ready(page)
                    if not resources_ready:
                        if is_public:
                            raise RuntimeError("封面或分享卡片生成超时")
                        await _save_draft_after_resource_timeout(
                            page, title, desc, known_drafts
                        )
                        if not await _wait_cover_editor_ready(page, timeout=1800):
                            raise RuntimeError("超时恢复后的封面编辑器未就绪")

                    if not is_public:
                        cover_previews = await _set_wechat_covers_sequentially(
                            page,
                            portrait_cover,
                            share_cover,
                            cover_headline,
                            title,
                            desc,
                            known_drafts,
                            video_path=video_path,
                        )
                        if isinstance(cover_previews, dict) and cover_previews.get("share", {}).get("unverified"):
                            print(">>> ⚠️ 警告：分享卡片封面未确认生效，请用 --repair-covers 补修或目视确认")
                        if not await _verify_required_publish_settings(page):
                            if not await _set_required_publish_settings(page):
                                raise RuntimeError(
                                    "微信位置、视频标注或原创标志未能在保存后保持"
                                )
                            action = await _click_final_action(page, False)
                            await asyncio.sleep(6)
                            if not await _verify_required_publish_settings(page):
                                raise RuntimeError(
                                    f"{action}后发布设置仍未保持"
                                )
                        marker = os.path.join(os.path.dirname(video_path), ".wechat_uploaded")
                        with open(marker, "w") as f:
                            f.write("1")
                        print(">>> 存草稿完成！")
                        return True

                    cover_previews = await _set_wechat_covers_for_upload(
                        page,
                        portrait_cover,
                        share_cover,
                        cover_headline,
                    )
                    # 尝试找到"公开"选项
                    try:
                        public_opt = await _try_selectors(page, [
                            'text="公开"',
                            '[class*="public"]',
                            'label:has-text("公开")',
                        ], "公开选项")
                        if public_opt:
                            await public_opt.click()
                            print(">>> 已设置为公开")
                    except Exception:
                        print(">>> 未找到公开选项（默认可能已是公开）")

                    # 9. 发布
                    await asyncio.sleep(1)
                    action = await _click_final_action(page, True)
                    await asyncio.sleep(5)
                    print(f">>> 点击后页面: {page.url}")
                    after_text = await _wujie_text(page)
                    print(f">>> 点击后页面文本片段: {after_text[:800]}")
                    debug_path = os.path.join(SKILL_DIR, "assets", "debug_after_save.png")
                    try:
                        await page.screenshot(path=debug_path, full_page=True)
                        print(f">>> 已保存调试截图: {debug_path}")
                    except Exception as e:
                        print(f">>> 保存调试截图失败: {e}")

                    # 10. 等待发布结果。
                    await _wait_published(page, timeout=30)

                    # 11. 标记完成
                    marker = os.path.join(os.path.dirname(video_path), ".wechat_uploaded")
                    with open(marker, "w") as f:
                        f.write("1")

                    print(f">>> {action}完成！")
                    return True

                finally:
                    # The old flow closed this context before the outer except
                    # could inspect the draft box, turning every ambiguous
                    # upload into a manual-check failure. Verify while the page
                    # is still available.
                    if file_selected and page and not page.is_closed():
                        active_exception = sys.exc_info()[1]
                        if active_exception is not None:
                            ambiguous_draft_checked = True
                            if await _draft_exists_for(page, desc):
                                marker = os.path.join(
                                    os.path.dirname(video_path), ".wechat_uploaded"
                                )
                                with open(marker, "w", encoding="utf-8") as handle:
                                    handle.write("1")
                                ambiguous_draft_verified = True
                                print(
                                    ">>> 异常后草稿箱已确认存在本次草稿；"
                                    "上传按成功记录，封面差异请用 --repair-covers "
                                    "或目视核对"
                                )
                            else:
                                print(
                                    ">>> 异常后草稿箱未找到本次草稿；"
                                    "本次上传未保存成功"
                                )
                    try:
                        await context.close()
                    finally:
                        await profile_lock.release()

        except Exception as e:
            last_error = e
            print(f">>> 尝试 {attempt} 失败: {e}")
            if file_selected:
                print(">>> 文件已提交给微信，为避免重复上传，本次不再自动重试")
                if ambiguous_draft_verified:
                    return True
                # The video is in WeChat's hands. If the draft box confirms a
                # draft for this description, the upload itself succeeded and
                # only a cover detail is off: record success instead of
                # reporting a failure the user has to disprove by hand.
                page_is_open = False
                try:
                    page_is_open = not page.is_closed()
                except Exception:
                    page_is_open = False
                if page_is_open and await _draft_exists_for(page, desc):
                    marker = os.path.join(os.path.dirname(video_path), ".wechat_uploaded")
                    with open(marker, "w", encoding="utf-8") as handle:
                        handle.write("1")
                    print(
                        ">>> 草稿箱已确认存在本次草稿；上传按成功记录，"
                        "封面差异请用 --repair-covers 或目视核对"
                    )
                    return True
                if page_is_open:
                    print(
                        ">>> 草稿箱未找到本次草稿；请用 --repair-covers 或重跑，"
                        "不要盲目重复上传"
                    )
                elif not ambiguous_draft_checked:
                    print(
                        ">>> 草稿箱核验未执行：浏览器上下文已关闭。"
                        "请先在微信草稿箱人工确认，避免重复上传"
                    )
                return False
            if attempt < max_retries:
                print(f">>> 等待 5 秒后重试...")
                await asyncio.sleep(5)

    print(f">>> 上传最终失败: {last_error}")
    return False


async def _draft_exists_for(page, desc):
    """Whether the draft box already holds a draft for this description."""
    try:
        if not await _open_draft_list(page):
            return False
        entries = await _draft_entries_all_pages(page)
        return len(_repair_draft_candidates(entries, desc)) >= 1
    except Exception:
        return False


async def _list_draft_box(headless=False):
    """Read-only inventory of the WeChat draft box, for reconciling with tasks."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        context, page, profile_lock = await _ensure_browser(p, headless=headless)
        try:
            await _goto_wechat_page(page, WECHAT_HOME_URL)
            await asyncio.sleep(2)
            if not await _ensure_logged_in(page, allow_qr=not headless):
                return False
            if not await _open_draft_list(page):
                print(">>> 无法打开草稿箱")
                return False
            entries = await _draft_entries_all_pages(page)
            print(f"\n=== 草稿箱共 {len(entries)} 条 ===")
            for entry in entries:
                title = (entry.get("title") or "").replace("\n", " ")[:64]
                print(f"[{entry.get('index')}] {entry.get('time')} | {title}")
            return True
        finally:
            try:
                await context.close()
            finally:
                await profile_lock.release()


async def repair_existing_draft(
    video_path,
    title=None,
    desc=None,
    cover_title=None,
    draft_time=None,
    headless=False,
):
    """Repair covers on an already-uploaded draft without submitting the video again."""
    if not os.path.exists(video_path):
        print(f">>> 视频文件不存在: {video_path}")
        return False

    cached_metadata = _load_cached_upload_metadata(video_path)
    title = str(title or cached_metadata.get("wechat_title") or "").strip()
    desc = str(desc or cached_metadata.get("description") or "").strip()
    cover_title = str(cover_title or cached_metadata.get("cover_title") or "").strip()
    # A previous upload records which draft box entry it wrote, so a repair can
    # target it exactly even when older duplicate drafts still exist.
    draft_time = str(draft_time or cached_metadata.get("wechat_draft_time") or "").strip() or None
    title = _normalize_wechat_title(title)
    if not title or not desc or not cover_title:
        print(">>> 修复草稿需要标题、描述和封面标题；请传入或提供 upload_metadata.json")
        return False

    portrait_cover, share_cover = _generate_wechat_covers(
        video_path, title, cover_title=cover_title
    )
    if not portrait_cover or not share_cover:
        print(">>> 微信横竖封面生成失败，已停止修复草稿")
        return False

    print(f"\n{'=' * 60}")
    print("🛠 视频号草稿封面修复")
    print(f"   文件: {video_path}")
    print(f"   标题: {title}")
    print(f"   草稿时间: {draft_time or '自动匹配唯一描述'}")
    print(f"   封面标题: {cover_title}")
    print(f"{'=' * 60}")

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        context, page, profile_lock = await _ensure_browser(p, headless=headless)
        try:
            await _goto_wechat_page(page, WECHAT_HOME_URL)
            await asyncio.sleep(2)
            if not await _ensure_logged_in(page, allow_qr=not headless):
                return False
            if not await _open_draft_list(page):
                print(">>> 无法打开草稿箱")
                return False

            entries = await _draft_entries_all_pages(page)
            known_drafts = {_draft_signature(entry) for entry in entries}
            target = _select_repair_draft_entry(entries, desc, draft_time)
            if not target:
                candidates = _repair_draft_candidates(entries, desc)
                print(
                    f">>> 描述匹配到 {len(candidates)} 条草稿，"
                    "请用 --draft-time 指定其中一条: "
                    + "; ".join(
                        f"{entry.get('time')} (index={entry.get('index')})"
                        for entry in candidates
                    )
                )
                return False

            print(f">>> 打开待修复草稿: {target.get('time')} (index={target.get('index')})")
            if not await _open_draft_entry_ref(page, target):
                return False
            if not await _set_required_publish_settings(page):
                print(">>> 待修复草稿的位置、视频标注或原创标志未设置成功")
                return False
            if not await _wait_cover_editor_ready(page, timeout=1800):
                print(">>> 待修复草稿的封面编辑器未就绪")
                await _save_cover_editor_debug(page)
                return False

            cover_previews = {}
            action = "存草稿"
            for orientation, cover_path in (
                ("portrait", portrait_cover),
                ("share", share_cover),
            ):
                preview = await _upload_wechat_cover_card(
                    page, cover_path, orientation, cover_title
                )
                if not preview:
                    print(f">>> {'个人主页' if orientation == 'portrait' else '分享'}封面未能确认设置")
                    return False
                cover_previews[orientation] = preview

                action = await _click_final_action(page, False)
                await asyncio.sleep(6)
                print(f">>> 修复保存后页面: {page.url}")
                if not await _open_draft_list(page):
                    print(">>> 修复后无法打开草稿箱核验")
                    return False
                entries = await _draft_entries_all_pages(page)
                updated_entries = [
                    entry
                    for entry in _repair_draft_candidates(entries, desc)
                    if _draft_signature(entry) not in known_drafts
                ]
                if len(updated_entries) == 1:
                    target = updated_entries[0]
                else:
                    target = _select_repair_draft_entry(entries, desc, draft_time)
                if not target:
                    print(">>> 修复后无法唯一确定这条草稿，未写入成功标记")
                    return False
                print(f">>> 重新打开草稿以设置下一张封面: {target.get('time')} (index={target.get('index')})")
                if not await _open_draft_entry_ref(page, target):
                    return False
                if not await _wait_cover_editor_ready(page, timeout=1800):
                    print(">>> 修复后的草稿封面编辑器未就绪")
                    await _save_cover_editor_debug(page)
                    return False

            print(f">>> 核验修复后的草稿: {target.get('time')} (index={target.get('index')})")
            # The last loop iteration already reopened this draft, so the editor
            # is normally on screen. Only walk back through the draft list when
            # the current page is not the draft's edit form.
            if not await _cover_editor_nodes_ready(page):
                if not await _open_draft_list(page):
                    print(">>> 核验前无法打开草稿箱")
                    return False
                entries = await _draft_entries_all_pages(page)
                refreshed = _select_repair_draft_entry(entries, desc, draft_time)
                if refreshed:
                    target = refreshed
                if not await _open_draft_entry_ref(page, target):
                    return False
            if not await _wait_cover_editor_ready(page, timeout=1800):
                print(">>> 修复后的草稿封面编辑器未就绪")
                await _save_cover_editor_debug(page)
                return False
            if not await _verify_draft_form_fields(page, title, desc):
                return False
            if not await _verify_wechat_cover_previews(page, cover_previews):
                return False
            if not await _verify_required_publish_settings(page):
                if not await _set_required_publish_settings(page):
                    print(">>> 修复后的位置、视频标注或原创标志未设置成功")
                    return False
                action = await _click_final_action(page, False)
                await asyncio.sleep(6)
                if not await _verify_required_publish_settings(page):
                    print(">>> 修复后的发布设置仍未保持")
                    return False

            marker = os.path.join(os.path.dirname(video_path), ".wechat_uploaded")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("1")
            print(f">>> {action}完成，草稿封面已修复！")
            return True
        finally:
            try:
                await context.close()
            finally:
                await profile_lock.release()


# ============================================================================ #
#  批量上传
# ============================================================================ #

def derive_title(video_path, project_title=None):
    """从视频路径推导标题。"""
    parent = os.path.basename(os.path.dirname(video_path))
    title = re.sub(r'\s*\[[a-zA-Z0-9_-]+\]\s*$', '', parent)
    title = re.sub(r'[_\s]+', ' ', title).strip()
    if not title:
        title = os.path.splitext(os.path.basename(video_path))[0].replace('_', ' ')
    if project_title:
        title = f"{project_title} - {title}"
    return title[:60]  # 视频号标题限制较短


def scan_videos(paths, pattern="*hardsub*.mp4"):
    """扫描路径下的视频文件。"""
    results = []
    for root_path in paths:
        if os.path.isfile(root_path) and root_path.lower().endswith((".mp4", ".mkv", ".webm")):
            results.append(os.path.abspath(root_path))
            continue
        if not os.path.isdir(root_path):
            print(f">>> 跳过不存在的路径: {root_path}")
            continue
        matched = glob.glob(os.path.join(root_path, "**", pattern), recursive=True)
        if not matched:
            matched = glob.glob(os.path.join(root_path, "**", "*.mp4"), recursive=True)
        results.extend(sorted(set(os.path.abspath(m) for m in matched)))
    return results


def load_failed_queue():
    if os.path.exists(QUEUE_PATH):
        try:
            with open(QUEUE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_failed_queue(queue):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


async def run_batch(videos, args):
    total = len(videos)
    failed = []
    succeeded = []

    print(f"\n{'=' * 60}")
    print(f"📹 视频号批量上传: 共 {total} 个视频")
    print(f"{'=' * 60}\n")

    for i, vpath in enumerate(videos, 1):
        # 跳过已上传的
        marker = os.path.join(os.path.dirname(vpath), ".wechat_uploaded")
        if os.path.exists(marker):
            print(f"\n[{i}/{total}] 跳过已上传: {derive_title(vpath)}")
            succeeded.append(vpath)
            continue

        title = derive_title(vpath, args.project_title) if args.no_auto_title else "字幕自动标题"
        print(f"\n[{i}/{total}] 上传: {title}")

        try:
            ok = await upload_one_video(
                vpath, title,
                desc=args.desc,
                tags=[t.strip() for t in re.split(r'[,，、]', args.tags) if t.strip()] if args.tags else None,
                is_public=args.public,
                llm_provider=args.llm_provider,
                llm_model=args.llm_model,
                headless=args.headless,
                auto_title=not args.no_auto_title,
                attribution=args.attribution,
            )
            if ok:
                succeeded.append(vpath)
            else:
                failed.append({"path": vpath, "title": title, "error": "upload returned False"})
        except Exception as e:
            failed.append({"path": vpath, "title": title, "error": str(e)})

        if i < total:
            await asyncio.sleep(args.interval)

    if failed:
        save_failed_queue(failed)
        print(f"\n>>> {len(failed)} 个失败，已记录到 {QUEUE_PATH}")
        print(f">>> 补传: python wechat_upload.py --retry-failed")

    print(f"\n>>> 汇总: 成功 {len(succeeded)} / 失败 {len(failed)} / 总计 {total}")
    return len(failed) == 0


# ============================================================================ #
#  CLI
# ============================================================================ #

def main():
    parser = argparse.ArgumentParser(description="微信视频号自动上传工具")
    parser.add_argument("path", nargs="?", help="视频文件路径")
    parser.add_argument("title", nargs="?", help="视频标题；默认从字幕自动提炼")
    parser.add_argument("--desc", type=str, default=None, help="视频描述（留空自动生成）")
    parser.add_argument("--tags", type=str, default=None, help="标签（逗号分隔）")
    parser.add_argument(
        "--cover-title",
        default=None,
        help="单视频封面画面标题（留空则单独从字幕提炼；不影响表单标题）",
    )
    parser.add_argument(
        "--attribution",
        default=None,
        help="可选封面署名前缀，例如“Palantir CEO卡普”；不参与字幕事实校验",
    )
    parser.add_argument("--public", action="store_true", help="公开发布（默认存草稿/仅自己可见）")
    parser.add_argument("--login", action="store_true", help="首次扫码登录")
    parser.add_argument("--batch", type=str, default=None, help="批量上传目录")
    parser.add_argument("--pattern", default="*hardsub*.mp4", help="批量上传文件匹配模式")
    parser.add_argument("--project-title", default=None, help="项目标题前缀")
    parser.add_argument("--retry-failed", action="store_true", help="补传失败队列")
    parser.add_argument(
        "--repair-covers",
        action="store_true",
        help="修复已保存草稿的横竖封面；不会重新上传视频",
    )
    parser.add_argument(
        "--list-drafts",
        action="store_true",
        help="只读列出草稿箱条目，用于核对任务与草稿是否一致",
    )
    parser.add_argument(
        "--draft-time",
        default=None,
        help="修复草稿时用草稿箱显示时间区分同描述草稿，例如 23:36",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="微信视频号合集名称（不超过10字，如不存在则自动新建并关联）",
    )
    parser.add_argument("--headless", action="store_true", help="无头模式（不显示浏览器窗口）")
    parser.add_argument("--no_auto_title", action="store_true", help="禁用字幕自动标题，使用传入/文件名标题")
    parser.add_argument("--interval", type=float, default=10.0, help="批量上传间隔秒数")
    _def_model, _def_prov = _autosub_llm_default()
    parser.add_argument("--llm-model", default=_def_model, help="LLM 模型")
    parser.add_argument("--llm-provider", default=_def_prov, help="LLM 通道")
    args = parser.parse_args()

    if args.login:
        ok = asyncio.run(_do_login())
        sys.exit(0 if ok else 1)

    if args.retry_failed:
        failed = load_failed_queue()
        if not failed:
            print("没有需要补传的失败记录。")
            return
        videos = [item["path"] for item in failed if os.path.exists(item.get("path", ""))]
        if not videos:
            print("失败队列中的文件已不存在。")
            return
        print(f">>> 补传模式: {len(videos)} 个视频")
        ok = asyncio.run(run_batch(videos, args))
        sys.exit(0 if ok else 1)

    if args.list_drafts:
        ok = asyncio.run(_list_draft_box(headless=args.headless))
        sys.exit(0 if ok else 1)

    if args.repair_covers:
        if not args.path:
            parser.error("--repair-covers 需要视频文件路径")
        ok = asyncio.run(repair_existing_draft(
            args.path,
            title=args.title,
            desc=args.desc,
            cover_title=args.cover_title,
            draft_time=args.draft_time,
            headless=args.headless,
        ))
        sys.exit(0 if ok else 1)

    if args.batch:
        if args.cover_title:
            parser.error("--cover-title only supports single-video uploads")
        videos = scan_videos([args.batch], args.pattern)
        if not videos:
            print("未找到视频文件。")
            return
        ok = asyncio.run(run_batch(videos, args))
        sys.exit(0 if ok else 1)

    if not args.path:
        parser.error("请提供视频文件路径，或使用 --login / --batch")
    if not args.title and args.no_auto_title:
        parser.error("使用 --no_auto_title 时必须提供视频标题")

    tags = [t.strip() for t in re.split(r'[,，、]', args.tags) if t.strip()] if args.tags else None
    ok = asyncio.run(upload_one_video(
        args.path, args.title or "",
        desc=args.desc, tags=tags,
        is_public=args.public,
        llm_provider=args.llm_provider, llm_model=args.llm_model,
        headless=args.headless,
        auto_title=not args.no_auto_title,
        cover_title=args.cover_title,
        attribution=args.attribution,
        collection=args.collection,
    ))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
