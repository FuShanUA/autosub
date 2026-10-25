import sys
import os

# Add common Mac Homebrew paths to PATH to ensure ffmpeg is found by MLX Whisper and standard engines
for path in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if os.path.exists(path) and path not in os.environ.get("PATH", ""):
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")

import json
import time
import ctypes
import re
import subprocess
import argparse
import io
import traceback

try:
    import wordninja
except ImportError:
    wordninja = None

try:
    from faster_whisper import WhisperModel # type: ignore
except ImportError:
    import site
    from importlib import reload
    reload(site)
    from faster_whisper import WhisperModel # type: ignore

# Try to import MLX support
try:
    from mlx_engine import transcribe_mlx, is_apple_silicon, HAS_MLX
except ImportError:
    HAS_MLX = False

try:
    from opencc import OpenCC
    _T2S_CONVERTER = OpenCC("t2s")
except ImportError:
    _T2S_CONVERTER = None

# Try to import pyannote.audio for speaker diarization
HAS_PYANNOTE = False
try:
    from pyannote.audio import Pipeline as PyannotePipeline
    HAS_PYANNOTE = True
except ImportError:
    pass

# Force UTF-8 for stdout/stderr
if sys.platform == "win32":
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

import multiprocessing
multiprocessing.freeze_support()

# Configuration
STD_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "faster-whisper")
DEFAULT_MODEL_SIZE = "large-v2"
# Anchor RESULT_ROOT to the repository structure
CURRENT_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Library/Tools/transcriber -> Library/Tools -> Library -> Root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(CURRENT_SCRIPT_DIR)))
RESULT_ROOT = os.path.join(PROJECT_ROOT, "Projects")
LOCAL_MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DOCS_MODELS = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub", "Models")

class SegmentChunk:
    def __init__(self, start, end, text, id):
        self.start = start
        self.end = end
        self.text = text
        self.id = id

from typing import Any

def get_v(obj: Any, key: str, default: Any = 0) -> Any:
    if hasattr(obj, key):
        res = getattr(obj, key, default)
        return res if (res is not None) else default
    if isinstance(obj, dict):
        res = obj.get(key, default)
        return res if (res is not None) else default
    return default

CHUNK_PROFILES = {
    'formal': {'max_chars': 80, 'max_duration': 8.0, 'gap_threshold': 2.5, 'min_context': 45, 'min_words': 5, 'min_yield_chars': 20},
    'spoken': {'max_chars': 80, 'max_duration': 8.0, 'gap_threshold': 1.5, 'min_context': 45, 'min_words': 5, 'min_yield_chars': 20},
}

def detect_content_type(segments_list):
    if not segments_list: return 'spoken'
    durations = [get_v(s, 'end') - get_v(s, 'start') for s in segments_list if get_v(s, 'end') > get_v(s, 'start')]
    gaps = []
    for i in range(1, len(segments_list)):
        g = get_v(segments_list[i], 'start') - get_v(segments_list[i-1], 'end')
        if g >= 0: gaps.append(g)
    avg_dur = sum(durations) / len(durations) if durations else 0
    avg_gap = sum(gaps) / len(gaps) if gaps else 0
    style = 'formal' if (avg_dur > 4.5 or avg_gap > 0.6) else 'spoken'
    # print(f"📊 Pacing: avg_seg={avg_dur:.1f}s, avg_gap={avg_gap:.2f}s → style='{style}'")
    return style

# Languages allowed for multilang detection. Short audio segments
# misdetected as Welsh/Portuguese/etc. cause Whisper to hallucinate
# gibberish in those languages. Restricting to the actual languages
# present in the video prevents this.
ALLOWED_LANGS = {'ja', 'en', 'zh'}

ASR_FRAGMENT_FIXES = {
    "a iaging": "AI Agent",
    "a i agent fde": "AI Agent FDE",
    "a i agent": "AI Agent",
    "a i reception list": "AI receptionist",
    "voicea i": "voice AI",
    "usinga i": "using AI",
    "agent iccore": "agentic core",
    "lmwork flow": "LLM workflow",
    "kuberne tesinfra": "Kubernetes infra",
    "accent ure mackenzie": "Accenture、McKinsey",
    "ceo c to": "CEO、CTO",
    "low - h anging fruit": "low-hanging fruit",
    "codex pie": "Codex CLI",
    "cha it pc": "ChatGPT",
    "a to a": "A2A",
    "lesson learn": "lessons learned",
    "makehard decision": "make hard decisions",
    "makehard decisions": "make hard decisions",
    "gpc 3. 5 devin 7": "GPT 3.5 Devin 7",
    "Bu ild business": "build business",
    "F DE": "FDE",
    "F DPM": "FDPM",
    "AI 18 F DE": "AI FDE",
    "For ward Dep loy ed Product Engineer": "Forward Deployed Product Engineer",
    "Open AI": "OpenAI",
    "S OP IC": "Anthropic",
    "S ARP IC": "Anthropic",
    "Pal ant ir": "Palantir",
    "P ent ier": "Palantir",
    "Pan 恩切": "Palantir",
    "Go ogle": "Google",
    "Mac ken zie": "McKinsey",
    "Qu esta": "Cresta",
    "Qu est": "Cresta",
    "C resta": "Cresta",
    "Cry stal": "Cresta",
    "V in cent": "Vincent",
    "V in ston": "Vincent",
    "W inson": "Winston",
    "W in ston": "Winston",
    "W inter": "Winston",
    "Ge off": "Jeff",
    "J oey": "Joey",
    "Jo ey": "Joey",
    "J ob": "Job",
    "J ob in": "Job",
    "Ge orge": "George",
    "V ent us": "Ventus",
    "A 16 Z": "a16z",
    "A IR": "AIR",
    "S now fl ake": "Snowflake",
    "Sp unk": "Splunk",
    "I BM": "IBM",
    "Az ure": "Azure",
    "A WS": "AWS",
    "Bad lock": "Bedrock",
    "A st ral Pack": "Mistral",
    "Ti mi": "Mistral",
    "S kill": "Skill",
    "T ouch DP": "GPT",
    "F ound er": "Founder",
    "F ound er Mode": "Founder Mode",
    "F und ing engineer": "Founding Engineer",
    "En ter prise": "Enterprise",
    "S aaS": "SaaS",
    "S AP": "SAP",
    "S ales force": "Salesforce",
    "S ales now": "ServiceNow",
    "sales now": "ServiceNow",
    "A gent": "Agent",
    "A gents": "Agents",
    "ag ent": "agent",
    "ag ents": "agents",
    "Del ta": "Delta",
    "Al pha": "Alpha",
    "E cho": "Echo",
    "H ead": "Head",
    "Co - F ound er": "Co-Founder",
    "H IP AA": "HIPAA",
    "ret rie val": "retrieval",
    "em bed ding": "embedding",
    "mem ory": "memory",
    "cont ext engineering": "context engineering",
    "gard er ial": "guardrail",
    "T TS": "TTS",
    "C II": "CLI",
    "C UI": "CLI",
    "W APT": "FDE",
    "t oken": "token",
    "ann ounce": "announce",
    "te am": "team",
    "B ridge the G ap": "bridge the gap",
    "D ata Engineering": "Data Engineering",
    "Sec urity": "Security",
    "Mag ical": "Magical",
    "prof essional service": "professional service",
    "prom pt": "prompt",
    "aut om ation": "automation",
    "ch all eng ing": "challenging",
    "L ucky": "Lucky",
    "mic ro management": "micro management",
    "c oding": "coding",
    "v end or": "vendor",
    "Web inar": "Webinar",
    "co - host": "co-host",
    "F ocus On": "Focus On",
    "engine er": "engineer",
    "man ager": "manager",
    "Man ager": "Manager",
    "tim eline": "timeline",
    "del iver": "deliver",
    "S ize": "Size",
    "off er": "offer",
    "OpenAI And ro id": "OpenAI Android",
    "Ty pe Script": "TypeScript",
    "mile stone": "milestone",
    "depend ency": "dependency",
    "AI TeamM ates": "AI Teammates",
    "know ledge base": "knowledge base",
    "guard rail": "guardrail",
    "U AT": "UAT",
    "D ata Sci ent ist": "Data Scientist",
    "F ly And": "Frontend",
    "Fly And": "Frontend",
    "custom er": "customer",
    "stake holder": "stakeholder",
    "me et": "meet",
    "S olution": "Solution",
    "Un it est": "Unit test",
    "C it izenTest": "CitizenTest",
    "special ty care": "specialty care",
    "Vict or": "Victor",
    "agent ic": "agentic",
    "h arness": "harness",
    "wardDe ploy Product Manager": "Forward Deploy Product Manager",
    "out come": "outcome",
    "Pay out": "Payout",
    "C - level": "C-level",
    "bud get": "budget",
    "up sell": "upsell",
    "data break": "Databricks",
    "platform wise": "platform-wise",
    "inst ruction": "instruction",
    "Engine er": "Engineer",
    "G it Hub": "GitHub",
    "For ceTech": "Force Tech",
    "s ession": "session",
    "C TO": "CTO",
    "CEO C TO": "CEO CTO",
    "weekly meet up": "weekly meetup",
    "CIUD engineer": "CI/CD engineer",
    "out compete": "outcompete",
    "trade off": "trade-off",
    "矽谷": "硅谷",
}

COMMON_LATIN_TOKENS = {
    "ai", "agent", "agents", "api", "aws", "azure", "bedrock", "big",
    "care", "case", "ceo", "cloud", "code", "codex", "cto", "data",
    "demo", "engineer", "engineering", "fde", "fdpm", "focus", "founder",
    "full", "health", "late", "management", "micro", "mode", "native",
    "openai", "palantir", "research", "rest", "skill", "soft", "splunk",
    "stack", "stage", "team", "test", "use", "ventus", "on", "and",
    "the", "of", "to", "in", "for", "with", "as", "at", "by", "or",
    "be", "is", "are", "it", "its", "this", "that", "from", "not",
    "we", "you", "they", "our", "out", "up", "off", "over", "under",
    "all", "any", "can", "will", "just", "now", "new", "one", "two",
    "get", "got", "let", "may", "say", "see", "she", "he", "his",
    "her", "them", "their", "there", "then", "than", "when", "where",
    "why", "how", "who", "what", "which", "while", "about", "after",
    "before", "again", "once", "here", "some", "such", "only", "own",
    "same", "too", "very", "should", "because", "into", "through",
    "during", "above", "below", "between", "other", "most", "more",
    "few", "both", "each", "been", "were", "have", "has", "had",
    "does", "did", "doing", "a", "an", "i", "me", "my", "so", "if",
    "no", "yes", "ok",
    "offer", "gap", "onsite", "swift", "gpu", "mcp", "uat", "kpi",
    "compete", "scientist",
}

PRESERVE_LATIN_RUNS = {
    "AI Agent FDE",
    "AI receptionist",
    "AI architect",
    "AI builders",
    "AI engine",
    "AI transformation",
    "enterprise AI",
    "grounded AI",
    "voice AI",
    "using AI",
    "agentic core",
    "LLM workflow",
    "Kubernetes infra",
    "low-hanging fruit",
    "Codex CLI",
    "ChatGPT",
    "A2A",
    "lessons learned",
    "make hard decisions",
    "AI native",
    "AI agent",
    "AI Agent",
    "AI agents",
    "AI FDE",
    "AI Research",
    "AI Engineer",
    "AI coding",
    "AI coding AI",
    "AI Teammates",
    "build business",
    "top down",
    "web coding",
}

WORD_NINJA_DISPLAYS = {
    "anthropic": "Anthropic",
    "vincent": "Vincent",
    "ventusai": "Ventus AI",
    "cofounder": "Co-Founder",
    "palantir": "Palantir",
    "rollingai": "Rolling AI",
    "openai": "OpenAI",
    "openaianthropic": "OpenAI Anthropic",
    "fde": "FDE",
    "mbb": "MBB",
    "vce": "VC",
    "sop": "SOP",
    "saas": "SaaS",
    "btc": "BTC",
    "mbti": "MBTI",
    "enfp": "ENFP",
    "p2p": "P2P",
    "agentharness": "Agent Harness",
    "businessbuilder": "Business Builder",
    "forwarddeployed": "Forward Deployed",
    "applicationai": "application AI",
    "koji": "Koji",
    "platinum": "Platinum",
    "onboarding": "onboarding",
    "input": "input",
    "bottomup": "bottom up",
    "shadow": "shadow",
    "agent": "agent",
    "engineering": "engineering",
    "taste": "taste",
    "feeling": "feeling",
    "calculate": "calculate",
    "lead": "lead",
    "sense": "sense",
    "judgment": "judgment",
    "upside": "upside",
    "exactly": "exactly",
    "problemsolver": "problem solver",
    "token": "token",
    "enabler": "enabler",
    "battle": "battle",
    "resultasaservice": "result as a service",
    "mercer": "Mercer",
    "essential": "essential",
    "owner": "owner",
    "software": "software",
    "infra": "infra",
    "gpc35devin7": "GPT 3.5 Devin 7",
    "paperwork": "paperwork",
}


PRESERVE_ENGLISH_TOKENS = {
    "palantir", "tribeca", "staples", "meritocracy", "fellowship",
    "westport", "connecticut", "sahana", "crawford", "ezra", "schwartz",
    "isaac", "elia", "ohio", "penn", "bay", "york",
}

ENGLISH_SPACING_FIXES = (
    (r"\bpalant ir\b", "Palantir"),
    (r"\bise it\b", "is it"),
    (r"\bis e it\b", "is it"),
    (r"\bihope were main colleagues\b", "I hope we remain colleagues"),
    (r"\bidea 2 action\b", "idea to action"),
    (r"\biwould beat\b", "I would be at"),
    (r"\bbetakinga\b", "be taking a"),
    (r"\bbetaking\b", "be taking"),
    (r"\bbeatpenn\b", "be at Penn"),
    (r"\bisawagroup\b", "I saw a group"),
    (r"\blikennorth\b", "like north"),
    (r"\bahalf\b", "a half"),
    (r"\bgoto\b", "go to"),
    (r"\bhighschool\b", "high school"),
    (r"\bknowhow\b", "know how"),
    (r"\bnoone\b", "no one"),
    (r"\bthankyou\b", "thank you"),
    (r"\bthusfar\b", "thus far"),
    (r"\bwhata i\b", "what AI"),
    (r"\bwhata\b", "what a"),
    (r"\blikeboom\b", "like boom"),
    (r"\bfailquick\b", "fail quick"),
    (r"\bpalantirandi\b", "Palantir and I"),
    (r"\bpalantiris\b", "Palantir is"),
)


_SPEAKER_LABEL_NAME_WORD = (
    r"(?!And\b|But\b|The\b|Well\b|Okay\b|So\b|Yeah\b|Wait\b|"
    r"What(?:'s)?\b|You\b|It(?:'s)?\b|I\b|Mrs\.?\b|Mr\.?\b|Ms\.?\b|Dr\.?\b)"
    r"[A-Z][a-z][A-Za-z'’-]*"
)
_SPEAKER_LABEL_RE = re.compile(
    r"(?P<label>"
    rf"{_SPEAKER_LABEL_NAME_WORD}(?:\s+{_SPEAKER_LABEL_NAME_WORD}){{1,2}}"
    r")"
    r"(?P<suffix>(?:,\s*(?:Jr\.?|Sr\.?|II|III))?)"
    r"(?P<sep>\s*:\s*|\s+)",
)


def _remove_speaker_labels(value: str, speaker_names=None) -> str:
    """Remove Whisper's inserted speaker labels from English transcript text."""
    value = str(value or "")
    if not value:
        return value

    pieces = []
    cursor = 0
    for match in _SPEAKER_LABEL_RE.finditer(value):
        remainder_start = match.end()
        remainder = value[remainder_start:]
        separator = match.group("sep") or ""
        if ":" not in separator and not (remainder and remainder[0].isupper()):
            continue
        pieces.append(value[cursor:match.start("label")])
        cursor = remainder_start

    if cursor != 0:
        pieces.append(value[cursor:])
        value = re.sub(r"\s+", " ", "".join(pieces)).strip()

    if speaker_names:
        for name in speaker_names:
            value = re.sub(
                rf"(?<=,)\s*{re.escape(name)}\s*,\s*",
                " ",
                value,
            )
            value = re.sub(rf"^\s*{re.escape(name)}\s*,\s*", "", value)
    return value


def _repeated_speaker_label_first_names(segments_list):
    """Find first names from repeated pseudo speaker labels.

    Whisper sometimes inserts a full pseudo label in several segments and later
    uses only its first name as a vocative. Repeated full labels provide enough
    evidence to remove that later vocative without hard-coding a name.
    """
    label_counts = {}
    for segment in segments_list:
        value = str(get_v(segment, "text", "") or "")
        for match in _SPEAKER_LABEL_RE.finditer(value):
            remainder = value[match.end():]
            separator = match.group("sep") or ""
            if ":" not in separator and not (remainder and remainder[0].isupper()):
                continue
            label = match.group("label")
            label_counts[label] = label_counts.get(label, 0) + 1

    return {
        label.split()[0]
        for label, count in label_counts.items()
        if count >= 2
    }


def _restore_english_spacing(value: str) -> str:
    """Restore spaces lost by Whisper in English word tokens."""
    if not value:
        return value

    for pattern, replacement in ENGLISH_SPACING_FIXES:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)

    value = re.sub(
        r"(?i)\b([a-z]+(?:'|’)(?:s|re|ve|ll|d|m|t))([a-z]{2,})\b",
        r"\1 \2",
        value,
    )
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)

    if wordninja is None:
        return value

    repaired_tokens = []
    for raw_token in value.split():
        match = re.match(r"^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$", raw_token)
        if not match:
            repaired_tokens.append(raw_token)
            continue
        prefix, core, suffix = match.groups()
        compact = re.sub(r"[^A-Za-z0-9]", "", core)
        if (
            not compact
            or compact.lower() in PRESERVE_ENGLISH_TOKENS
            or "'" in core
            or "’" in core
        ):
            repaired_tokens.append(raw_token)
            continue

        split = wordninja.split(compact)
        should_split = len(split) > 1 and (
            compact[0].islower()
            or (compact[:1] == "I" and split[0].lower() == "i")
        )
        if not should_split:
            repaired_tokens.append(raw_token)
            continue

        if compact[:1] == "I":
            split[0] = "I"
        elif compact[:1].isupper() and split:
            split[0] = split[0].capitalize()
        repaired_tokens.append(prefix + " ".join(split) + suffix)

    value = " ".join(repaired_tokens)
    value = re.sub(r"(?<![A-Za-z])a i(?![A-Za-z])", "AI", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<![A-Za-z])i(?![A-Za-z])", "I", value)
    return value


def _contains_kana(text: str) -> bool:
    return any(0x3040 <= ord(char) <= 0x30FF for char in text)


def _contains_hangul(text: str) -> bool:
    return any(0xAC00 <= ord(char) <= 0xD7AF for char in text)


def _contains_cjk_ideograph(text: str) -> bool:
    return any(0x4E00 <= ord(char) <= 0x9FFF for char in text)


def detect_primary_language(segments_list):
    """Return the dominant transcript language from text and explicit labels."""
    if not segments_list:
        return "en"

    explicit = {}
    text = ""
    for segment in segments_list:
        segment_text = str(get_v(segment, "text", "") or "")
        text += segment_text + " "
        lang = get_v(segment, "lang", None)
        if lang:
            explicit[lang] = explicit.get(lang, 0) + max(1.0, len(segment_text))

    if explicit:
        return max(explicit, key=explicit.get)
    if _contains_hangul(text):
        return "ko"
    if _contains_kana(text):
        return "ja"
    cjk_count = sum(1 for char in text if 0x4E00 <= ord(char) <= 0x9FFF)
    latin_count = sum(1 for char in text if char.isascii() and char.isalpha())
    return "zh" if cjk_count > latin_count else "en"


def normalize_transcript_text(
    text,
    simplify_chinese=True,
    normalize_latin_runs=True,
    speaker_names=None,
):
    """Normalize ASR output for Chinese subtitles and mixed-language terms."""
    value = str(text or "").strip()
    if not value:
        return value

    if not simplify_chinese:
        value = _remove_speaker_labels(value, speaker_names=speaker_names)

    if simplify_chinese and _T2S_CONVERTER is not None and _contains_cjk_ideograph(value) and not _contains_kana(value):
        value = _T2S_CONVERTER.convert(value)

    value = re.sub(r"\s+", " ", value)

    for wrong, correct in sorted(ASR_FRAGMENT_FIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if wrong in value:
            value = value.replace(wrong, correct)

    value = value.replace(" - ", "-")

    value = re.sub(
        r"(?<![A-Za-z])a i(?![A-Za-z])",
        "AI",
        value,
        flags=re.IGNORECASE,
    )

    def _normalize_latin_run(match):
        run = match.group(0)
        if run in PRESERVE_LATIN_RUNS:
            return run
        if wordninja is None:
            return run
        compact = re.sub(r"[^A-Za-z0-9]", "", run).lower()
        if compact in WORD_NINJA_DISPLAYS:
            return WORD_NINJA_DISPLAYS[compact]
        split = wordninja.split(compact)
        if [token.lower() for token in run.split()] == split:
            return run
        return " ".join(split)

    if normalize_latin_runs:
        value = re.sub(
            r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]*(?: [A-Za-z0-9][A-Za-z0-9-]*)+(?![A-Za-z0-9])",
            _normalize_latin_run,
            value,
        )

    if not normalize_latin_runs:
        return _restore_english_spacing(value).strip()

    def _is_fragment(token):
        clean = re.sub(r"[^A-Za-z0-9]", "", token)
        if not clean or clean.isdigit():
            return False
        if clean.lower() in COMMON_LATIN_TOKENS:
            return False
        return len(clean) <= 5

    def _merge_latin_run(match):
        if match.group(0) in PRESERVE_LATIN_RUNS:
            return match.group(0)
        tokens = match.group(0).split(" ")
        merged = []
        index = 0

        def _should_merge(left, right):
            left_clean = re.sub(r"[^A-Za-z0-9]", "", left)
            right_clean = re.sub(r"[^A-Za-z0-9]", "", right)
            if not left_clean or not right_clean:
                return False
            return (
                (_is_fragment(left) and _is_fragment(right))
                or (len(left_clean) == 1 and _is_fragment(right))
                or (len(right_clean) == 1 and _is_fragment(left))
            )

        while index < len(tokens):
            token = tokens[index]
            if index + 1 < len(tokens) and _should_merge(token, tokens[index + 1]):
                combined = token + tokens[index + 1]
                index += 1
                while index + 1 < len(tokens) and _should_merge(combined, tokens[index + 1]):
                    combined += tokens[index + 1]
                    index += 1
                merged.append(combined)
                index += 1
                continue
            merged.append(token)
            index += 1
        return " ".join(merged)

    value = re.sub(
        r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]*(?: [A-Za-z][A-Za-z0-9-]*)+(?![A-Za-z0-9])",
        _merge_latin_run,
        value,
    )

    if not simplify_chinese:
        value = _restore_english_spacing(value)

    cjk = r"[\u3400-\u4DBF\u4E00-\u9FFF\u3000-\u303F\uFF00-\uFFEF]"
    latin = r"[A-Za-z0-9]"
    value = re.sub(rf"({cjk})\s+({latin})", r"\1\2", value)
    value = re.sub(rf"({latin})\s+({cjk})", r"\1\2", value)
    value = re.sub(rf"({cjk})\s+({cjk})", r"\1\2", value)
    return value.strip()


def _realignment_tokens(text):
    """Split normalized text at CJK/Latin boundaries while keeping punctuation."""
    tokens = []
    current = ""
    previous_kind = None

    def _kind(char):
        if 0x3400 <= ord(char) <= 0x4DBF or 0x4E00 <= ord(char) <= 0x9FFF:
            return "cjk"
        if char.isascii() and (char.isalnum() or char in "-_"):
            return "latin"
        return "other"

    for char in str(text or ""):
        if char.isspace():
            if current:
                tokens.append(current)
                current = ""
                previous_kind = None
            continue

        kind = _kind(char)
        if (
            current
            and previous_kind in ("cjk", "latin")
            and kind in ("cjk", "latin")
            and kind != previous_kind
        ):
            tokens.append(current)
            current = ""

        current += char
        if kind in ("cjk", "latin"):
            previous_kind = kind

    if current:
        tokens.append(current)
    combined = []
    index = 0
    while index < len(tokens):
        if (
            index + 1 < len(tokens)
            and tokens[index].casefold() == "ai"
            and tokens[index + 1].casefold() == "agent"
        ):
            combined.append(tokens[index] + " " + tokens[index + 1])
            index += 2
            continue
        combined.append(tokens[index])
        index += 1
    return combined


def _realign_segment_words(segment):
    """Restore display words after Whisper splits mixed-language terms.

    Faster-whisper can emit ``J + ove`` or ``C + rest + a`` as word tokens while
    keeping the correct spelling in segment.text. Chunking later joins those
    word tokens with spaces, producing fragments such as ``J ove``. Align the
    raw word timestamps to normalized display tokens so timing is preserved
    without inheriting the tokenizer's subword boundaries.
    """
    text = str(get_v(segment, "text", "") or "").strip()
    words = segment.get("words", []) if isinstance(segment, dict) else None
    if not text or not words:
        return

    raw_compact = "".join(
        re.sub(r"\s+", "", str(get_v(word, "word", "") or "")).casefold()
        for word in words
    )
    text_compact = re.sub(r"\s+", "", text).casefold()
    display_tokens = _realignment_tokens(text)
    if not raw_compact or not display_tokens or raw_compact != text_compact:
        segment_start = get_v(segment, "start", 0)
        segment_end = get_v(segment, "end", segment_start)
        duration = max(0.0, segment_end - segment_start)
        total_len = sum(len(re.sub(r"\s+", "", token)) for token in display_tokens)
        if total_len <= 0:
            return

        synthetic = []
        consumed = 0
        for token in display_tokens:
            token_len = len(re.sub(r"\s+", "", token))
            start = segment_start + duration * (consumed / total_len)
            end = segment_start + duration * ((consumed + token_len) / total_len)
            synthetic.append({"start": start, "end": end, "word": token})
            consumed += token_len
        segment["words"] = synthetic
        return

    realigned = []
    raw_index = 0

    for token in display_tokens:
        token_len = len(re.sub(r"\s+", "", token))
        consumed = 0
        group = []

        while raw_index < len(words) and consumed < token_len:
            raw_word = words[raw_index]
            raw_text = str(get_v(raw_word, "word", "") or "")
            raw_len = len(re.sub(r"\s+", "", raw_text))
            if raw_len == 0:
                raw_index += 1
                continue

            group.append(raw_word)
            consumed += raw_len
            raw_index += 1

        if not group:
            continue

        first = dict(group[0])
        first["word"] = token
        first["start"] = get_v(group[0], "start")
        first["end"] = get_v(group[-1], "end")
        realigned.append(first)

    if realigned:
        segment["words"] = realigned


def normalize_segments_for_output(segments_list):
    language = detect_primary_language(segments_list)
    simplify = language == "zh"
    speaker_names = _repeated_speaker_label_first_names(segments_list)
    for segment in segments_list:
        segment["text"] = normalize_transcript_text(
            segment.get("text", ""),
            simplify_chinese=simplify,
            normalize_latin_runs=(language != "en"),
            speaker_names=speaker_names,
        )
        _realign_segment_words(segment)
    return language, segments_list


def normalize_chunk_boundaries(chunks):
    """Fix ASR terms that were split across adjacent subtitle blocks."""
    for i in range(len(chunks) - 1):
        tail = str(get_v(chunks[i], "text", "") or "")
        head = str(get_v(chunks[i + 1], "text", "") or "")
        if not tail or not head:
            continue
        combined = tail + " " + head
        boundary = len(tail)
        for wrong, correct in sorted(ASR_FRAGMENT_FIXES.items(), key=lambda item: len(item[0]), reverse=True):
            start = combined.find(wrong)
            if start < 0:
                continue
            end = start + len(wrong)
            if not (start < boundary and end > boundary):
                continue
            prefix = combined[:start]
            suffix = combined[end:]
            chunks[i].text = (prefix + correct).strip()
            if suffix.strip():
                chunks[i + 1].text = suffix.strip()
            else:
                chunks[i].end = chunks[i + 1].end
                del chunks[i + 1]
            break
    return chunks

def _clamp_language(lang):
    """Clamp detected language to the allowed set. Falls back to 'en' for
    anything exotic (Welsh, Portuguese, etc.) to prevent hallucinations."""
    if lang in ALLOWED_LANGS:
        return lang
    return 'en'

def chunk_segments(segments_list, content_type=None):
    if content_type is None: content_type = detect_content_type(segments_list)
    p = CHUNK_PROFILES[content_type]
    max_chars, max_duration, gap_threshold = p['max_chars'], p['max_duration'], p['gap_threshold']
    min_context, min_words, min_yield_chars = p['min_context'], p['min_words'], p['min_yield_chars']

    chunk_id = 1
    def word_streamer(segs):
        for seg in segs:
            words = get_v(seg, 'words', None)
            if words:
                for w in words: yield w
            else:
                # Segment has no word-level timestamps (e.g. from checkpoint resume).
                # Yield a synthetic word from the full segment text so it isn't silently dropped.
                text = get_v(seg, 'text', '')
                if text and text.strip():
                    yield {'word': text, 'start': get_v(seg, 'start'), 'end': get_v(seg, 'end')}

    all_words = list(word_streamer(segments_list))
    if not all_words:
        # Fallback to segments if no word-level timestamps are available
        for i, s in enumerate(segments_list):
            yield SegmentChunk(get_v(s, 'start'), get_v(s, 'end'), get_v(s, 'text', ""), i+1)
        return

    # Build a per-word language map for boundary detection.
    # In multilang mode, each segment carries a 'lang' field. We use this
    # to break subtitle blocks at language boundaries (ja->en or en->ja),
    # so that Japanese and English text don't end up in the same block.
    seg_langs = []
    for seg in segments_list:
        lang = get_v(seg, 'lang', None)
        if lang:
            seg_langs.append(lang)
        else:
            text = get_v(seg, 'text', '')
            has_kana = any(0x3040 <= ord(c) <= 0x30FF for c in text)
            has_cjk = any(0x4E00 <= ord(c) <= 0x9FFF for c in text)
            seg_langs.append('ja' if (has_kana or has_cjk) else 'en')
    word_langs = []
    seg_idx = 0
    for seg in segments_list:
        words = get_v(seg, 'words', None)
        count = len(words) if words else (1 if get_v(seg, 'text', '').strip() else 0)
        lang = seg_langs[seg_idx] if seg_idx < len(seg_langs) else 'en'
        for _ in range(count):
            word_langs.append(lang)
        seg_idx += 1
    while len(word_langs) < len(all_words):
        word_langs.append('en')
    word_langs = word_langs[:len(all_words)]

    def smart_join(t1, t2):
        if not t1: return t2
        # Don't insert a space between CJK characters. faster-whisper with
        # word_timestamps=True splits Japanese/Chinese into single-char tokens,
        # and inserting spaces produces garbage like "F DE モ デ ル".
        if t1.endswith(' ') or t2.startswith(' '):
            return t1 + t2
        last_char = t1[-1] if t1 else ''
        first_char = t2[0] if t2 else ''
        def _is_cjk(ch):
            return bool(ch) and (0x3000 <= ord(ch) <= 0x9FFF or 0xFF00 <= ord(ch) <= 0xFFEF)
        if _is_cjk(last_char) or _is_cjk(first_char):
            return t1 + t2
        # Only join runs of single-letter tokens. A lone English article or
        # pronoun ("is a", "and I") must keep its space.
        if (
            len(t1.strip()) == 1
            and len(t2.strip()) == 1
            and t1.strip().isalpha()
            and t2.strip().isalpha()
        ):
            return t1 + t2
        return t1 + ' ' + t2

    i = 0
    while i < len(all_words):
        current_chunk_words = [all_words[i]]
        current_start = get_v(all_words[i], 'start')
        current_text = get_v(all_words[i], 'word', "") or get_v(all_words[i], 'text', "")
        i += 1
        while i < len(all_words):
            word = all_words[i]
            word_text = get_v(word, 'word', "") or get_v(word, 'text', "")
            gap = get_v(word, 'start') - get_v(all_words[i-1], 'end')
            would_exceed_chars = len(current_text) + len(word_text.strip()) > max_chars
            would_exceed_duration = (get_v(word, 'end') - current_start) > max_duration
            is_large_gap = gap > gap_threshold
            is_sentence_end = word_text.strip()[-1] in '.?!。？！' if word_text.strip() else False

            if would_exceed_chars and not would_exceed_duration:
                found_near_end = False
                if is_sentence_end and len(current_text) + len(word_text.strip()) < max_chars * 1.4:
                    found_near_end = True
                if not found_near_end:
                    for j in range(1, 6):
                        if i + j < len(all_words):
                            w_peek_text = get_v(all_words[i+j], 'word', "") or get_v(all_words[i+j], 'text', "")
                            if w_peek_text.strip() and w_peek_text.strip()[-1] in '.?!。？！' and len(current_text) < max_chars * 1.4:
                                found_near_end = True; break
                if found_near_end: would_exceed_chars = False

            # Detect language boundary: break when the language changes
            # (ja->en or en->ja), but only if the current block has enough
            # text to not be a tiny fragment. Use a lower threshold (5 chars)
            # than min_yield_chars (20) to allow breaks at language boundaries
            # while still avoiding single-word fragments.
            cur_lang = word_langs[i-1] if i-1 < len(word_langs) else 'en'
            next_lang = word_langs[i] if i < len(word_langs) else 'en'
            is_lang_boundary = (cur_lang != next_lang)

            should_break = (would_exceed_chars or would_exceed_duration or (is_large_gap and len(current_chunk_words) >= min_words) or (is_sentence_end and len(current_text) > min_context) or is_lang_boundary)
            # Use a lower min threshold for language boundary breaks
            min_check = 5 if is_lang_boundary else min_yield_chars
            if should_break and len(current_text.strip()) >= min_check:
                if is_sentence_end and not (would_exceed_chars or would_exceed_duration):
                    current_chunk_words.append(word); current_text = smart_join(current_text, word_text); i += 1
                break
            current_chunk_words.append(word); current_text = smart_join(current_text, word_text); i += 1
        yield SegmentChunk(current_start, get_v(current_chunk_words[-1], 'end'), current_text.strip(), chunk_id)
        chunk_id += 1

def get_ffmpeg_path():
    import shutil
    path = shutil.which("ffmpeg")
    if path: return path
    for fb in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe", r"D:\Program Files\CapCut\7.7.0.3143\ffmpeg.exe"]:
        if os.path.exists(fb): return fb
    return "ffmpeg"

FFMPEG_EXE = get_ffmpeg_path()

def get_duration(file_path):
    cmd = [FFMPEG_EXE, "-i", file_path, "-hide_banner"]
    try:
        startupinfo = None
        if hasattr(subprocess, 'STARTUPINFO'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
        
        kwargs = {}
        if startupinfo:
            kwargs['startupinfo'] = startupinfo
            
        result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore', **kwargs)
        for line in result.stderr.split('\n'):
            if "Duration" in line:
                time_str = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = time_str.split(':')
                return float(h) * 3600 + float(m) * 60 + float(s)
    except: pass
    return 0


def _extract_audio_chunk(file_path, start, duration):
    """Extract a real WAV chunk for long-video transcription.

    ``clip_timestamps`` can make faster-whisper silently skip audio inside a
    clip range. Extracting the exact range with ffmpeg first avoids that
    failure mode while still keeping transcription resumable.
    """
    import tempfile

    handle, wav_path = tempfile.mkstemp(suffix=".wav", prefix="autosub_chunk_")
    os.close(handle)
    cmd = [
        FFMPEG_EXE,
        "-y",
        "-ss",
        f"{float(start):.3f}",
        "-t",
        f"{float(duration):.3f}",
        "-i",
        file_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        wav_path,
    ]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=max(60, int(duration) + 30),
        )
        return wav_path
    except (subprocess.SubprocessError, OSError):
        try:
            os.remove(wav_path)
        except OSError:
            pass
        return None


def show_notification(title, message):
    try: 
        if hasattr(ctypes, 'windll'):
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1)
    except: pass

def get_project_folder(file_path):
    base = os.path.splitext(os.path.basename(file_path))[0]
    clean = re.sub(r'[^a-zA-Z0-9]', '_', base)
    folder_name = "_".join([p for p in clean.split('_') if p][:5]) or "Untitled_Project"
    return os.path.join(RESULT_ROOT, folder_name)

def serialize_segment(s, lang=None):
    """Converts a faster-whisper segment into a serializable dict."""
    res = {
        'start': getattr(s, 'start', 0),
        'end': getattr(s, 'end', 0),
        'text': getattr(s, 'text', ""),
        'words': []
    }
    if lang:
        res['lang'] = lang
    words = getattr(s, 'words', None)
    if words:
        for w in words:
            res['words'].append({'start': getattr(w, 'start', 0), 'end': getattr(w, 'end', 0), 'word': getattr(w, 'word', ""), 'probability': getattr(w, 'probability', 0)})
    return res

def save_checkpoint(path, segments):
    """Saves transcription state atomically using a temporary file."""
    temp_path = path + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump({'segments': segments}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as e:
        print(f"⚠️ Failed to save checkpoint: {e}")

def _multilang_transcribe(model, file_path, total_duration, clip_start, kwargs):
    """Multi-language transcription using VAD + Tiny + sliding window.

    Uses faster-whisper's VAD to split audio into speech segments, detects
    language per segment with the Tiny model, refines language boundaries with
    a sliding window, and transcribes each language group with the main model.
    Long groups (>30s) are split at internal VAD pauses to prevent Whisper
    auto-detect from drifting to the wrong language on long segments.
    """
    yield from _multilang_transcribe_sliding_window(model, file_path, total_duration, clip_start, kwargs)

def _multilang_transcribe_sliding_window(model, file_path, total_duration, clip_start, kwargs):
    """Multi-language transcription: detects language per VAD segment and transcribes each with the correct language."""
    # Use tiny model for fast language detection; large-v2 for actual transcription.
    # tiny's detect_language is ~100x faster than large-v2's (far fewer params).
    print(f"🌐 Loading tiny model for fast language detection...", flush=True)
    detect_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    print(f"🌐 Tiny model loaded. Detecting languages per segment...", flush=True)
    from faster_whisper.audio import decode_audio
    audio = decode_audio(file_path)
    if clip_start > 0:
        audio = audio[int(clip_start * 16000):]
    try:
        from faster_whisper.vad import get_vad_model, VadOptions
        # max_speech_duration_s: split long segments at silence points,
        # naturally approximating speaker turns for language detection.
        vad_opts = VadOptions(min_silence_duration_ms=200, speech_pad_ms=200, max_speech_duration_s=10)
        from faster_whisper.vad import get_speech_timestamps
        vad_segments = get_speech_timestamps(audio, vad_opts)
    except Exception as e:
        print(f"⚠️ VAD failed ({e}), falling back to single-pass transcription", flush=True)
        return model.transcribe(file_path, **kwargs)
    speech_segs = []
    for seg in vad_segments:
        start = seg.get("start", 0) / 16000 + clip_start
        end = seg.get("end", 0) / 16000 + clip_start
        if end - start >= 1.0:
            speech_segs.append((start, end))
    # VAD with max_speech_duration_s already splits at silence points,
    # approximating speaker turns. No merging or fixed-window splitting needed.
    print(f"🌐 Found {len(speech_segs)} speech segments (VAD auto-split at pauses). Detecting language...", flush=True)
    all_segs = []
    for i, (seg_start, seg_end) in enumerate(speech_segs):
        seg_audio = audio[int((seg_start - clip_start) * 16000):int((seg_end - clip_start) * 16000)]
        if len(seg_audio) < 16000:
            continue
        try:
            lang, prob, _ = detect_model.detect_language(audio=seg_audio)
            lang = _clamp_language(lang)
        except Exception:
            lang, prob = "auto", 0.0
        print(f"   [{i+1}/{len(speech_segs)}] {seg_start:.1f}-{seg_end:.1f}s -> {lang} ({prob:.2f})", flush=True)
        all_segs.append((seg_start, seg_end, lang, prob))
    if not all_segs:
        print(f"⚠️ No speech segments found, falling back to single-pass", flush=True)
        return model.transcribe(file_path, **kwargs)
    # Refine language boundaries: when a segment's language differs from the
    # next segment's, the boundary may be imprecise (VAD didn't detect a pause
    # at the actual language switch point). Use a sliding-window Tiny model
    # scan to find the real switch point and split the segment there.
    LANG_CONF_THRESHOLD = 0.80
    refined_segs = []
    for idx in range(len(all_segs)):
        seg_start, seg_end, lang, prob = all_segs[idx]
        # Only refine if this segment is long enough and the next segment
        # has a different language (i.e. there's a language boundary here)
        next_lang = all_segs[idx + 1][2] if idx + 1 < len(all_segs) else None
        if (next_lang and next_lang != lang and seg_end - seg_start >= 3.0
                and lang in ('ja', 'en', 'zh') and next_lang in ('ja', 'en', 'zh')):
            # Sliding window from front to back: 2s windows with 1s step
            # to find the approximate switch point, then 1s windows with
            # 0.5s step to refine. Scan only the last 6s of the segment.
            sample_rate = 16000
            def _detect_lang(t_start, t_end):
                w_start = int((t_start - clip_start) * sample_rate)
                w_end = int((t_end - clip_start) * sample_rate)
                w_audio = audio[w_start:w_end]
                if len(w_audio) < sample_rate // 2:
                    return None, 0
                try:
                    wl, wp, _ = detect_model.detect_language(audio=w_audio)
                    return _clamp_language(wl), wp
                except Exception:
                    return None, 0

            # Pass 1: 2s windows, 1s step, from scan_start forward
            switch_point = None
            scan_start = max(seg_start, seg_end - 6.0)
            coarse_point = None
            t = scan_start
            while t < seg_end - 2.0:
                wl, wp = _detect_lang(t, t + 2.0)
                if wl == next_lang and wp >= 0.60:
                    coarse_point = t
                    break
                t += 1.0
            if coarse_point is not None:
                # Pass 2: 1s windows, 0.5s step, refine around coarse_point
                fine_start = max(seg_start, coarse_point - 2.0)
                t = fine_start
                while t < coarse_point + 2.0 and t < seg_end - 1.0:
                    wl, wp = _detect_lang(t, t + 1.0)
                    if wl == next_lang and wp >= 0.55:
                        switch_point = t
                        break
                    t += 0.5
                if switch_point is None:
                    switch_point = coarse_point
            if switch_point is not None and switch_point > seg_start + 0.5:
                # Split: [seg_start, switch_point] stays as current lang,
                # [switch_point, seg_end] becomes next lang
                refined_segs.append((seg_start, switch_point, lang, prob))
                refined_segs.append((switch_point, seg_end, next_lang, 0.90))
                print(f"   🔀 Refined boundary: {seg_start:.1f}-{switch_point:.1f}s ({lang}) + {switch_point:.1f}-{seg_end:.1f}s ({next_lang})", flush=True)
            else:
                refined_segs.append((seg_start, seg_end, lang, prob))
        else:
            refined_segs.append((seg_start, seg_end, lang, prob))

    # Group consecutive same-language segments
    all_segs = refined_segs
    groups = []
    cur_lang = all_segs[0][2]
    cur_start, cur_end = all_segs[0][0], all_segs[0][1]
    cur_min_prob = all_segs[0][3]
    for seg_start, seg_end, lang, prob in all_segs[1:]:
        if lang == cur_lang:
            cur_end = seg_end
            cur_min_prob = min(cur_min_prob, prob)
        else:
            groups.append((cur_start, cur_end, cur_lang, cur_min_prob))
            cur_start, cur_end, cur_lang, cur_min_prob = seg_start, seg_end, lang, prob
    groups.append((cur_start, cur_end, cur_lang, cur_min_prob))
    print(f"🌐 Grouped into {len(groups)} language segments. Transcribing with main model...", flush=True)
    info = type('Info', (), {'language': 'multilang', 'language_probability': 1.0})()
    import tempfile
    for i, (g_start, g_end, g_lang, g_min_prob) in enumerate(groups):
        overall_pct = g_start / total_duration * 100 if total_duration > 0 else 0
        print(f"Progress: {min(99.0, overall_pct):.1f}% (Transcribing {g_start:.0f}-{g_end:.0f}s as {g_lang})", flush=True)
        # Extract this group's audio as a temp WAV using ffmpeg, then
        # transcribe the extracted file directly. This is more reliable
        # than clip_timestamps, which can cause Whisper to silently skip
        # portions of the audio within the clip range.
        g_duration = g_end - g_start

        # For long groups (>30s), split at internal VAD pauses to prevent
        # Whisper auto-detect from drifting to the wrong language.
        MAX_GROUP_DURATION = 30.0
        if g_duration > MAX_GROUP_DURATION:
            try:
                from faster_whisper.vad import get_speech_timestamps, VadOptions
                group_audio = audio[int((g_start - clip_start) * 16000):int((g_end - clip_start) * 16000)]
                vad_opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=100, max_speech_duration_s=MAX_GROUP_DURATION)
                sub_segs = get_speech_timestamps(group_audio, vad_opts)
                if len(sub_segs) > 1:
                    print(f"   📏 Split long group {g_start:.0f}-{g_end:.0f}s ({g_duration:.0f}s) into {len(sub_segs)} sub-segments", flush=True)
                    # Process each sub-segment
                    for sub in sub_segs:
                        sub_start = sub.get("start", 0) / 16000 + g_start
                        sub_end = sub.get("end", 0) / 16000 + g_start
                        sub_dur = sub_end - sub_start
                        if sub_dur < 0.3:
                            continue
                        sub_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=tempfile.gettempdir())
                        sub_wav.close()
                        try:
                            import subprocess as _sp
                            _sp.run([FFMPEG_EXE, '-y', '-ss', str(sub_start), '-t', str(sub_dur),
                                    '-i', file_path, '-vn', '-ac', '1', '-ar', '16000',
                                    '-f', 'wav', sub_wav.name], capture_output=True, timeout=60)
                        except:
                            sub_wav = None
                        sub_kwargs = dict(kwargs)
                        sub_kwargs.pop("vad_filter", None)
                        sub_kwargs.pop("clip_timestamps", None)
                        sub_kwargs.pop("language", None)
                        sub_kwargs["vad_filter"] = False
                        if g_lang and g_lang != "auto" and g_min_prob >= 0.80:
                            sub_kwargs["language"] = g_lang
                        try:
                            t_input = sub_wav.name if sub_wav and os.path.exists(sub_wav.name) else file_path
                            if sub_wav and os.path.exists(sub_wav.name):
                                sub_kwargs.pop("clip_timestamps", None)
                            else:
                                sub_kwargs["clip_timestamps"] = [float(sub_start), float(sub_end)]
                            seg_iter, info_local = model.transcribe(t_input, **sub_kwargs)
                            seg_list = list(seg_iter)
                            sub_lang = getattr(info_local, 'language', g_lang)
                            # MLX fallback
                            if seg_list and sub_dur > 3.0:
                                last_end = getattr(seg_list[-1], 'end', 0)
                                coverage = last_end / sub_dur if sub_dur > 0 else 1.0
                                if coverage < 0.5 and HAS_MLX:
                                    print(f"⚠️ Low coverage ({coverage:.0%}) for sub-segment {sub_start:.0f}-{sub_end:.0f}s. MLX retry...", flush=True)
                                    try:
                                        mlx_segs = transcribe_mlx(sub_wav.name if sub_wav and os.path.exists(sub_wav.name) else file_path,
                                            model_name="mlx-community/whisper-large-v2-mlx", language=None)
                                        if mlx_segs:
                                            mlx_list = list(mlx_segs)
                                            if mlx_list and sub_wav and os.path.exists(sub_wav.name):
                                                mlx_cov = getattr(mlx_list[-1], 'end', 0) / sub_dur if sub_dur > 0 else 1.0
                                                if mlx_cov < 0.5:
                                                    try:
                                                        mlx_en = transcribe_mlx(sub_wav.name, model_name="mlx-community/whisper-large-v2-mlx", language='en')
                                                        if mlx_en:
                                                            en_list = list(mlx_en)
                                                            en_cov = getattr(en_list[-1], 'end', 0) / sub_dur if sub_dur > 0 and en_list else 0
                                                            if en_cov > mlx_cov:
                                                                mlx_list = en_list
                                                                print(f"   English retry: {en_cov:.0%} (better)", flush=True)
                                                    except: pass
                                            seg_list = mlx_list
                                    except: pass
                            for s in seg_list:
                                s_start_orig = getattr(s, 'start', 0) + sub_start
                                s_end_orig = getattr(s, 'end', 0) + sub_start
                                try: s.start = s_start_orig
                                except: pass
                                try: s.end = s_end_orig
                                except: pass
                                words = getattr(s, 'words', None)
                                if words:
                                    for w in words:
                                        try: w.start = getattr(w, 'start', 0) + sub_start
                                        except: pass
                                        try: w.end = getattr(w, 'end', 0) + sub_start
                                        except: pass
                                try: s._lang = sub_lang
                                except: pass
                                if total_duration and total_duration > 0:
                                    p = round((s_end_orig / total_duration) * 100, 1)
                                    print(f"Progress: {min(99.0, p)}% ({s_end_orig:.0f}/{total_duration:.0f}s | {sub_lang} @ {sub_start:.0f}-{sub_end:.0f}s)", flush=True)
                                yield s
                        except Exception as e:
                            print(f"⚠️ Failed to transcribe sub-segment {sub_start:.0f}-{sub_end:.0f}s: {e}", flush=True)
                        finally:
                            if sub_wav and os.path.exists(sub_wav.name):
                                try: os.unlink(sub_wav.name)
                                except: pass
                    continue  # Skip the normal transcription below for this group
            except Exception as e:
                print(f"⚠️ VAD split failed, using full group: {e}", flush=True)

        tmp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=tempfile.gettempdir())
        tmp_wav.close()
        try:
            import subprocess as _sp
            _extract_cmd = [FFMPEG_EXE, '-y', '-ss', str(g_start), '-t', str(g_duration),
                            '-i', file_path, '-vn', '-ac', '1', '-ar', '16000',
                            '-f', 'wav', tmp_wav.name]
            _sp.run(_extract_cmd, capture_output=True, timeout=60)
        except Exception as e:
            print(f"⚠️ ffmpeg extract failed for {g_start:.0f}-{g_end:.0f}s: {e}", flush=True)
            tmp_wav = None

        group_kwargs = dict(kwargs)
        group_kwargs.pop("vad_filter", None)
        group_kwargs.pop("clip_timestamps", None)
        group_kwargs.pop("language", None)
        group_kwargs["vad_filter"] = False
        LANG_CONFIDENCE_THRESHOLD = 0.80
        if g_lang and g_lang != "auto" and g_min_prob >= LANG_CONFIDENCE_THRESHOLD:
            group_kwargs["language"] = g_lang
            print(f"   Language: {g_lang} (confidence >= {g_min_prob:.2f})", flush=True)
        else:
            print(f"   Language: auto (min confidence {g_min_prob:.2f} < {LANG_CONFIDENCE_THRESHOLD})", flush=True)
        try:
            transcribe_input = tmp_wav.name if tmp_wav and os.path.exists(tmp_wav.name) else file_path
            if tmp_wav and os.path.exists(tmp_wav.name):
                group_kwargs.pop("clip_timestamps", None)
            else:
                group_kwargs["clip_timestamps"] = [float(g_start), float(g_end)]
            seg_iter, _ = model.transcribe(transcribe_input, **group_kwargs)
            seg_list = list(seg_iter)
            # Check if transcription covered the full group duration.
            # If the last segment ends much earlier than the group end,
            # faster-whisper may have silently dropped speech. Fall back
            # to MLX engine which handles these cases better.
            if seg_list and g_duration > 3.0:
                last_end = getattr(seg_list[-1], 'end', 0)
                # If using extracted WAV, timestamps are relative to 0
                if tmp_wav and os.path.exists(tmp_wav.name):
                    coverage = last_end / g_duration if g_duration > 0 else 1.0
                else:
                    coverage = (last_end - g_start) / g_duration if g_duration > 0 else 1.0
                if coverage < 0.5 and HAS_MLX:
                    print(f"⚠️ Low coverage ({coverage:.0%}) for {g_start:.0f}-{g_end:.0f}s. Retrying with MLX engine...", flush=True)
                    try:
                        mlx_input = tmp_wav.name if tmp_wav and os.path.exists(tmp_wav.name) else file_path
                        mlx_kwargs = {"language": g_lang} if g_lang and g_lang != "auto" else {}
                        mlx_segs = transcribe_mlx(mlx_input, model_name="mlx-community/whisper-large-v2-mlx",
                                                  language=g_lang if g_lang and g_lang != "auto" else None)
                        if mlx_segs:
                            # Check if MLX auto-detect also had low coverage.
                            # If so, try English as fallback (English Whisper
                            # correctly transcribes English speech, while
                            # Japanese Whisper mistranslates it).
                            mlx_list = list(mlx_segs)
                            if mlx_list and tmp_wav and os.path.exists(tmp_wav.name):
                                mlx_last_end = getattr(mlx_list[-1], 'end', 0)
                                mlx_coverage = mlx_last_end / g_duration if g_duration > 0 else 1.0
                                if mlx_coverage < 0.5 and g_lang != 'en':
                                    print(f"   MLX auto-detect also low ({mlx_coverage:.0%}). Retrying with English...", flush=True)
                                    try:
                                        mlx_en = transcribe_mlx(tmp_wav.name,
                                            model_name="mlx-community/whisper-large-v2-mlx",
                                            language='en')
                                        if mlx_en:
                                            en_list = list(mlx_en)
                                            en_last_end = getattr(en_list[-1], 'end', 0) if en_list else 0
                                            en_coverage = en_last_end / g_duration if g_duration > 0 else 0
                                            if en_coverage > mlx_coverage:
                                                mlx_list = en_list
                                                print(f"   English retry: {en_coverage:.0%} coverage (better)", flush=True)
                                    except Exception as en_err:
                                        print(f"   English retry failed: {en_err}", flush=True)
                            seg_list = mlx_list
                            # Note: timestamp offset is applied later in the
                            # for s in seg_list: loop, so no need to offset here
                    except Exception as mlx_err:
                        print(f"⚠️ MLX retry also failed: {mlx_err}", flush=True)
            for s in seg_list:
                # Offset timestamps back to the original audio timeline
                s_start_orig = getattr(s, 'start', 0) + g_start
                s_end_orig = getattr(s, 'end', 0) + g_start
                try:
                    s.start = s_start_orig
                except Exception:
                    pass
                try:
                    s.end = s_end_orig
                except Exception:
                    pass
                # Also offset word timestamps
                words = getattr(s, 'words', None)
                if words:
                    for w in words:
                        try:
                            w.start = getattr(w, 'start', 0) + g_start
                        except Exception:
                            pass
                        try:
                            w.end = getattr(w, 'end', 0) + g_start
                        except Exception:
                            pass
                # Attach the detected language
                try:
                    s._lang = g_lang
                except Exception:
                    pass
                if total_duration and total_duration > 0:
                    p = round((s_end_orig / total_duration) * 100, 1)
                    print(f"Progress: {min(99.0, p)}% ({s_end_orig:.0f}/{total_duration:.0f}s | {g_lang} @ {g_start:.0f}-{g_end:.0f}s)", flush=True)
                yield s
        except Exception as e:
            print(f"⚠️ Failed to transcribe {g_start:.0f}-{g_end:.0f}s ({g_lang}): {e}", flush=True)
        finally:
            if tmp_wav and os.path.exists(tmp_wav.name):
                try:
                    os.unlink(tmp_wav.name)
                except Exception:
                    pass
    print(f"🌐 Multi-language transcription complete.", flush=True)

def main():
    print(f"🚀 Transcriber engine started (PID: {os.getpid()})")
    print(f"Progress: 0.1% (Initializing engine...)", flush=True)
    print(f"DEBUG: sys.argv = {sys.argv}")
    parser = argparse.ArgumentParser(description="Standalone Faster-Whisper Transcriber")
    parser.add_argument("file", help="Path to video/audio file")
    parser.add_argument("--device", default=None, help="Device (cuda/cpu)")
    parser.add_argument("--model", default=DEFAULT_MODEL_SIZE, help="Whisper model name/path")
    parser.add_argument("--output", default=None, help="Output directory for SRT")
    parser.add_argument("--no-gui", action="store_true", help="Run without UI progress")
    parser.add_argument("--quick", action="store_true", help="Experimental: Quick Transcribe (YouTube Subs + LLM)")
    parser.add_argument("--language", default=None, help="Source language code (e.g. ja, en, zh). Auto-detect if omitted.")
    parser.add_argument("--multilang", action="store_true", help="Enable per-segment language detection for mixed-language videos")
    parser.add_argument("--rebuild-only", action="store_true", help="Rebuild SRT from transcribe_state.json without transcribing")
    args = parser.parse_args()

    file_path = os.path.abspath(args.file)
    output_dir = os.path.abspath(args.output) if args.output else get_project_folder(file_path)
    raw_model_name = args.model
    if raw_model_name.startswith("faster-whisper-"): raw_model_name = raw_model_name.replace("faster-whisper-", "", 1)

    os.makedirs(output_dir, exist_ok=True)
    lock_file = os.path.join(output_dir, ".transcribe.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                old_pid = int(f.read().strip())
            
            running = False
            if sys.platform == "win32":
                import psutil
                running = psutil.pid_exists(old_pid)
            else:
                try:
                    os.kill(old_pid, 0)
                    running = True
                except OSError:
                    running = False
                    
            if running:
                print(f"⚠️ Project folder locked by PID {old_pid}. Exiting.")
                sys.exit(0)
        except Exception as e:
            pass

    with open(lock_file, "w") as f: f.write(str(os.getpid()))

    try:
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}"); sys.exit(1)

        print(f"🎙️ File: {os.path.basename(file_path)}\n📂 Output: {output_dir}\n🧠 Model: {raw_model_name}")

        # --- Quick Mode (YouTube + LLM) ---
        if args.quick:
            print("⚡ Mode: Quick Transcribe (Experimental)")
            quick_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_transcribe.py")
            cmd = [sys.executable, quick_script, file_path, "--output", output_dir]
            # Try to pass cookies if we can find them
            cookies = r"D:\Downloads\cookies.txt"
            if os.path.exists(cookies):
                cmd.extend(["--cookies", cookies])

            try:
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == "win32" else 0
                subprocess.run(cmd, check=True, creationflags=creationflags)
                print("✅ Quick Transcribe finished.")
                if os.path.exists(lock_file): os.remove(lock_file)
                sys.exit(0)
            except Exception as e:
                print(f"❌ Quick Transcribe failed: {e}. Falling back to normal transcription.")

        total_duration = get_duration(file_path)

        # UI
        root, progress_var, lbl_time = None, None, None
        if not args.no_gui:
            try:
                import tkinter as tk
                from tkinter import ttk
                root = tk.Tk(); root.title("Transcribing...")
                root.geometry('400x150')
                tk.Label(root, text=f"Processing: {os.path.basename(file_path)}", wraplength=380).pack(pady=10)
                progress_var = tk.DoubleVar()
                ttk.Progressbar(root, variable=progress_var, maximum=100, length=350).pack(pady=10)
                lbl_time = tk.Label(root, text="Initializing..."); lbl_time.pack()
                root.update()
            except: root = None

        # Model Load
        if args.device:
            device = args.device
        else:
            if sys.platform == "win32":
                try:
                    device = "cuda" if ctypes.windll.kernel32.GetModuleHandleW("nvcuda.dll") else "cpu"
                except:
                    device = "cpu"
            else:
                # For Mac/Linux, stick to CPU for maximum compatibility unless specifically requested
                device = "cpu"
        search_paths = [LOCAL_MODELS, DOCS_MODELS, STD_CACHE]
        best_root = next((d for d in search_paths if d and os.path.exists(d)), None)
        print(f"Progress: 0.8% (Analyzing audio...)", flush=True)

        # Checkpoint
        checkpoint_path = os.path.join(output_dir, "transcribe_state.json")
        segments_list, clip_start = [], 0
        if os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, 'r', encoding='utf-8') as f:
                    segments_list = json.load(f).get('segments', [])
                    if segments_list:
                        clip_start = segments_list[-1].get('end', 0)
                        if clip_start is None: clip_start = 0
                        print(f"⏩ Resuming from {clip_start:.2f}s")
                        # 立即向 TUI 汇报当前真实进展，避免用户误以为从头开始
                        if total_duration > 0:
                            p = round(min(99.9, (clip_start / total_duration) * 100), 1)
                            print(f"Progress: {p}% (Resuming from {clip_start:.0f}s...)", flush=True)
            except Exception as e:
                print(f"⚠️ State file corrupted or unreadable ({e}). Starting fresh.")
                segments_list, clip_start = [], 0

        kwargs: dict[str, Any] = {"beam_size": 5, "vad_filter": True, "word_timestamps": True}
        if clip_start > 0: kwargs["clip_timestamps"] = [float(clip_start), float(total_duration)]
        if args.language:
            kwargs["language"] = args.language
            print(f"🌐 Language override: {args.language}")
        if args.multilang:
            kwargs.pop("language", None)
            kwargs["condition_on_previous_text"] = False
            print(f"🌐 Multi-language detection enabled (per-segment auto-detect, condition_on_previous_text=False)")

        # --- High-level Skip: If already finished, don't even start the engine ---
        if args.rebuild_only:
            print("♻️ Rebuilding SRT from the existing checkpoint.", flush=True)
            segments = []
            skip_loop_progress = True
        elif total_duration > 0 and clip_start >= total_duration - 1.0:
            print(f"✅ Transcription already completed ({clip_start:.1f}s / {total_duration:.1f}s). Skipping to output.", flush=True)
            segments = []
        else:
            # --- Transcription Engine Selection ---
            skip_loop_progress = False
            # Standard Path: faster-whisper (CPU)
            print(f"Progress: 0.2% (Loading model {raw_model_name} on {device}...)", flush=True)
            model = WhisperModel(raw_model_name, device=device, compute_type="auto", download_root=best_root)
            print(f"Progress: 0.5% (Model loaded)", flush=True)
            if args.multilang:
                segments = _multilang_transcribe(model, file_path, total_duration, clip_start, kwargs)
                skip_loop_progress = True
                # Consume the generator fully (multilang yields segments)
                segments = list(segments)
                for s in segments:
                    seg_lang = getattr(s, '_lang', None)
                    segments_list.append(serialize_segment(s, lang=seg_lang))
            elif total_duration > 300:
                # --- Chunked transcription for long videos ---
                # model.transcribe() on a long video submits all chunks at once
                # to ctranslate2's thread pool. The generator won't yield the
                # first segment until the entire batch is processed, which can
                # take 20+ minutes on CPU with no progress or checkpoint.
                # Splitting into 5-minute chunks with per-segment checkpointing
                # ensures progress is visible and resumable.
                CHUNK_SEC = 300  # 5 minutes per chunk
                segments = []
                # Resume from the checkpoint instead of submitting the whole
                # remaining audio to faster-whisper as one uncheckpointed batch.
                chunk_start = float(clip_start)
                while chunk_start < total_duration:
                    chunk_end = min(chunk_start + CHUNK_SEC, total_duration)
                    chunk_duration = chunk_end - chunk_start
                    wav_path = _extract_audio_chunk(
                        file_path,
                        chunk_start,
                        chunk_duration,
                    )
                    if not wav_path:
                        raise RuntimeError(
                            f"ffmpeg failed to extract audio chunk "
                            f"{chunk_start:.0f}-{chunk_end:.0f}s"
                        )
                    pct = round((chunk_start / total_duration) * 100, 1)
                    print(f"Progress: {pct}% (Transcribing {chunk_start:.0f}-{chunk_end:.0f}s)", flush=True)
                    chunk_kwargs = dict(kwargs)
                    chunk_kwargs.pop("clip_timestamps", None)
                    chunk_kwargs.pop("vad_filter", None)
                    chunk_kwargs["vad_filter"] = True
                    # Each WAV chunk already starts fresh. Carrying prior text
                    # inside a long chunk can inject unrelated speaker names.
                    chunk_kwargs["condition_on_previous_text"] = False
                    seg_iter, info = model.transcribe(wav_path, **chunk_kwargs)
                    # Iterate generator one segment at a time so each segment
                    # is checkpointed immediately (enables resume + progress).
                    for s in seg_iter:
                        s.start += chunk_start
                        s.end += chunk_start
                        for word in s.words or []:
                            word.start += chunk_start
                            word.end += chunk_start
                        segments.append(s)
                        segments_list.append(serialize_segment(s))
                        s_end = getattr(s, 'end', 0) or 0
                        if total_duration > 0:
                            p = round((s_end / total_duration) * 100, 1)
                            print(f"Progress: {p}% ({s_end:.0f}/{total_duration:.0f}s)", flush=True)
                        if len(segments_list) % 2 == 0:
                            save_checkpoint(checkpoint_path, segments_list)
                    save_checkpoint(checkpoint_path, segments_list)
                    os.remove(wav_path)
                    chunk_start = chunk_end
                skip_loop_progress = True
            else:
                segments, info = model.transcribe(file_path, **kwargs)
        detected_style = None

        # Chunked and multilang paths already appended to segments_list
        # during their loops. Skip re-append to avoid duplicating segments.
        if not skip_loop_progress:
            for s in segments:
                seg_lang = getattr(s, '_lang', None)
                segments_list.append(serialize_segment(s, lang=seg_lang))

        # Sort segments by start time and fix overlaps.
        segments_list.sort(key=lambda x: x.get('start', 0))
        for i in range(1, len(segments_list)):
            prev_end = segments_list[i-1].get('end', 0)
            curr_start = segments_list[i].get('start', 0)
            if curr_start < prev_end:
                segments_list[i]['start'] = prev_end
                if segments_list[i].get('end', 0) <= prev_end:
                    segments_list[i]['end'] = prev_end + 0.1
                for w in segments_list[i].get('words', []):
                    if w.get('start', 0) < prev_end:
                        w['start'] = prev_end
            if len(segments_list) % 2 == 0:
                save_checkpoint(checkpoint_path, segments_list)

            s_end = segments_list[i].get('end', 0)
            if s_end is None: s_end = 0

            if not detected_style and (s_end > 60 or len(segments_list) > 20):
                detected_style = detect_content_type(segments_list)

            if total_duration and total_duration > 0:
                p = round((s_end / total_duration) * 100, 1)
                if root and progress_var:
                    try:
                        progress_var.set(p); root.update()
                    except: pass
                elif not skip_loop_progress:
                    print(f"Progress: {p}% ({s_end:.0f}/{total_duration:.0f}s)", flush=True)

        save_checkpoint(checkpoint_path, segments_list)
        if skip_loop_progress:
            print(f"Progress: 100.0% (Transcription finished successfully)", flush=True)
        if not detected_style: detected_style = detect_content_type(segments_list)

        primary_language, segments_list = normalize_segments_for_output(segments_list)
        save_checkpoint(checkpoint_path, segments_list)
        print(f"🌐 Primary transcript language: {primary_language}")

        srt_path = os.path.join(output_dir, os.path.splitext(os.path.basename(file_path))[0] + ".srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            output_chunks = normalize_chunk_boundaries(
                list(chunk_segments(segments_list, detected_style))
            )
            for i, segment in enumerate(output_chunks):
                def fmt(t):
                    if t is None: t = 0
                    h, m, s, ms = int(t//3600), int((t%3600)//60), int(t%60), int((t%1)*1000)
                    return f"{h:02}:{m:02}:{s:02},{ms:03}"
                f.write(f"{i+1}\n{fmt(get_v(segment,'start'))} --> {fmt(get_v(segment,'end'))}\n{get_v(segment,'text','')}\n\n")

        print(f"✅ Saved: {srt_path}")
        if root: root.destroy()
        if os.path.exists(lock_file): os.remove(lock_file)

    except Exception as e:
        print(f"❌ Error: {e}")
        traceback.print_exc()
        if os.path.exists(lock_file):
            try: os.remove(lock_file)
            except: pass
        sys.exit(1)

if __name__ == "__main__":
    main()
