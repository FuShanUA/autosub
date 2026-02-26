
import sys
import os
import json
import time
import ctypes
import re
import subprocess
from faster_whisper import WhisperModel
import io

# Force UTF-8 for stdout/stderr to handle emojis in logs on Windows
if sys.platform == "win32":
    # Fix for HuggingFace/Faster-Whisper cache on Windows: Disable symlinks which cause WinError 448
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
    
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

import multiprocessing

# Windows compatibility for packaged apps
multiprocessing.freeze_support()

# Configuration
STD_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "faster-whisper")

if getattr(sys, 'frozen', False):
    # Packaged installer logic
    DOCS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AutoSub")
    RESULT_ROOT = os.path.join(DOCS_DIR, "Projects")
    LOCAL_MODELS = os.path.join(os.path.dirname(sys.executable), "models")
    DOCS_MODELS = os.path.join(DOCS_DIR, "Models")

    if not os.path.exists(LOCAL_MODELS):
        BUNDLE_DIR = sys._MEIPASS
        LOCAL_MODELS = os.path.join(BUNDLE_DIR, "Library", "Tools", "transcriber", "models")
else:
    # Development/Raw script logic
    RESULT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    LOCAL_MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    DOCS_MODELS = None # Disable Documents fallback in dev mode

class SegmentChunk:
    def __init__(self, start, end, text, id):
        self.start = start
        self.end = end
        self.text = text
        self.id = id

# --- Chunking Profiles ---
# Profiles tuned for speech 'pacing' rather than just content type.
# 'formal': Keynotes/Webinars — Measured, planned, deliberate pauses.
# 'spoken': Interviews/Podcasts — Conversational, informal flow, fewer 'dead' pauses.

CHUNK_PROFILES = {
    'formal': {
        'max_chars': 80,        # Slightly more headroom for natural phrasing
        'max_duration': 8.0,
        'gap_threshold': 2.5,   
        'min_context': 45,      # Start looking for punctuation early
        'min_words': 5,
        'min_yield_chars': 20,
    },
    'spoken': {
        'max_chars': 80,
        'max_duration': 8.0,
        'gap_threshold': 1.5,   
        'min_context': 45,
        'min_words': 5,
        'min_yield_chars': 20,
    },
}

def detect_content_type(segments_list):
    """
    Analyzes pacing to distinguish formal planned speech from casual oral flow.
    """
    if not segments_list:
        return 'spoken'

    durations = [s.end - s.start for s in segments_list if s.end > s.start]
    gaps = []
    for i in range(1, len(segments_list)):
        g = segments_list[i].start - segments_list[i-1].end
        if g >= 0:
            gaps.append(g)

    avg_dur = sum(durations) / len(durations) if durations else 0
    avg_gap = sum(gaps) / len(gaps) if gaps else 0

    # Decision: Formal keys/webinars tend to have significantly longer segments 
    # even with dense starts. Lowered thresholds to be more inclusive of formal styles.
    is_formal = avg_dur > 4.5 or avg_gap > 0.6

    style = 'formal' if is_formal else 'spoken'
    print(f"📊 Speech pacing analysis: avg_seg={avg_dur:.1f}s, avg_gap={avg_gap:.2f}s → style='{style}'")
    return style


def chunk_segments(segments_list, content_type=None):
    """
    Groups Whisper word-level timestamps into subtitle chunks.
    Automatically selects chunking profile based on content type if not specified.
    Uses look-ahead to avoid splitting the last few words of a sentence into the 
    next segment (Sentence Completion Stretch).
    """
    if content_type is None:
        content_type = detect_content_type(segments_list)

    p = CHUNK_PROFILES[content_type]
    max_chars     = p['max_chars']
    max_duration  = p['max_duration']
    gap_threshold = p['gap_threshold']
    min_context   = p['min_context']
    min_words     = p['min_words']
    min_yield_chars = p['min_yield_chars']

    chunk_id = 1
    
    def word_streamer(segs):
        for seg in segs:
            if hasattr(seg, 'words') and seg.words:
                for w in seg.words:
                    yield w

    all_words = list(word_streamer(segments_list))
    if not all_words:
        return

    def smart_join(t1, t2):
        if not t1: return t2
        if t1.endswith(' ') or t2.startswith(' '):
            return t1 + t2
        return t1 + ' ' + t2

    i = 0
    while i < len(all_words):
        current_chunk_words = [all_words[i]]
        current_start = all_words[i].start
        current_text = all_words[i].word
        
        current_idx = i
        i += 1 # Advance to next word for accumulation loop
        
        while i < len(all_words):
            word = all_words[i]
            word_text = word.word
            
            gap = word.start - all_words[i-1].end
            
            # Basic flags
            would_exceed_chars = len(current_text) + len(word_text.strip()) > max_chars
            would_exceed_duration = (word.end - current_start) > max_duration
            is_large_gap = gap > gap_threshold
            is_sentence_end = word_text.strip()[-1] in '.?!。？！' if word_text.strip() else False
            has_min_words = len(current_chunk_words) >= min_words
            has_min_context = len(current_text) > min_context
            
            # --- THE "STRETCH" LOGIC ---
            # If we hit char limit, peek ahead to see if a sentence ends very soon.
            # If so, keep going to finish the sentence in ONE block.
            if would_exceed_chars and not would_exceed_duration:
                found_near_end = False
                
                # First check: Does the CURRENT word finish the sentence?
                if is_sentence_end:
                    if len(current_text) + len(word_text.strip()) < max_chars * 1.4:
                        found_near_end = True
                
                # Second check: Peek up to 5 words ahead
                if not found_near_end:
                    for j in range(1, 6):
                        if i + j < len(all_words):
                            w_peek = all_words[i+j]
                            if w_peek.word.strip()[-1] in '.?!。？！':
                                # Sentence ends within next 5 words! 
                                if len(current_text) < max_chars * 1.4:
                                    found_near_end = True
                                    break
                
                if found_near_end:
                    would_exceed_chars = False # Defer the break

            should_break = (
                would_exceed_chars or
                would_exceed_duration or
                (is_large_gap and has_min_words) or
                (is_sentence_end and has_min_context and has_min_words)
            )

            if should_break:
                # If chunk is too micro, merge this word in anyway unless hard limit
                if len(current_text.strip()) < min_yield_chars and not (would_exceed_chars or would_exceed_duration):
                    pass # Don't break, keep accumulating
                else:
                    # BREAK HERE
                    # The current word 'word' belongs to the NEXT chunk (unless specifically included)
                    # No, in Whisper the logic is simpler: if should_break is triggered by 'word', 
                    # 'word' is usually excluded from the current chunk.
                    # EXCEPT if is_sentence_end triggered it, we want the punctuation word IN the chunk.
                    
                    if is_sentence_end and (should_break and not would_exceed_chars and not would_exceed_duration):
                        # Include the punctuation word
                        current_chunk_words.append(word)
                        current_text = smart_join(current_text, word_text)
                        i += 1
                    
                    break
            
            # Accumulate
            current_chunk_words.append(word)
            current_text = smart_join(current_text, word_text)
            i += 1
        
        # Yield the completed chunk
        end_time = current_chunk_words[-1].end
        yield SegmentChunk(current_start, end_time, current_text.strip(), chunk_id)
        chunk_id += 1

# Models are stored in the user's .cache folder by default (~/.cache/faster-whisper)
# This allows the installer to be smaller as weights are downloaded on first run.
# You can also place a "models" folder inside the app directory for offline use.
DEFAULT_MODEL_SIZE = "large-v2" # Default size if no path is provided

# Check for a local "models" folder for portability
LOCAL_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
if os.path.exists(LOCAL_MODEL_DIR):
    # If there are subfolders in models/, we could pick one, but for now we trust the --model arg
    pass


# --- Robust FFmpeg Detection ---
def get_ffmpeg_path():
    import shutil
    import glob
    # 1. Check PATH
    path = shutil.which("ffmpeg")
    if path: return path
    
    # 2. Check WinGet Gyan FFmpeg (User Specific)
    user_home = os.path.expanduser("~")
    winget_base = os.path.join(user_home, "AppData", "Local", "Microsoft", "Winget", "Packages")
    if os.path.exists(winget_base):
        for d in os.listdir(winget_base):
            if "Gyan.FFmpeg" in d:
                for bin_dir in glob.glob(os.path.join(winget_base, d, "**/bin"), recursive=True):
                    tool_path = os.path.join(bin_dir, "ffmpeg.exe")
                    if os.path.exists(tool_path): return tool_path

    # 3. Check common hardcoded paths
    fallbacks = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"D:\Program Files\CapCut\7.7.0.3143\ffmpeg.exe",
    ]
    for fb in fallbacks:
        if os.path.exists(fb): return fb
        
    return "ffmpeg" # Default fallback

FFMPEG_EXE = get_ffmpeg_path()

def get_duration(file_path):
    if not os.path.exists(file_path): return 0
    # Try using detected ffmpeg
    cmd = [FFMPEG_EXE, "-i", file_path, "-hide_banner"]
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore', startupinfo=startupinfo)
        for line in result.stderr.split('\n'):
            if "Duration" in line:
                try:
                    time_str = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = time_str.split(':')
                    return float(h) * 3600 + float(m) * 60 + float(s)
                except: pass
    except: pass
    return 0

def estimate_processing_time(duration):
    return duration / 10.0 if duration else 0

def show_notification(title, message):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1) 
    except: pass

def get_project_folder(file_path):
    base = os.path.splitext(os.path.basename(file_path))[0]
    clean = re.sub(r'[^a-zA-Z0-9]', '_', base)
    clean = re.sub(r'_+', '_', clean)
    parts = [p for p in clean.split('_') if p]
    folder_name = "_".join(parts[:5])
    if not folder_name: folder_name = "Untitled_Project"
    if not os.path.exists(RESULT_ROOT):
        try: os.makedirs(RESULT_ROOT)
        except: pass
    return os.path.join(RESULT_ROOT, folder_name)

def main():
    if len(sys.argv) < 3:
        print("Usage: python transcribe_engine.py <mode> <file_path> [--model model_name]")
        sys.exit(1)
        
    mode = sys.argv[1]
    file_path = sys.argv[2]
    
    selected_model = DEFAULT_MODEL_SIZE
    custom_output_dir = None
    
    # Parse args manually since we aren't using argparse yet
    if "--model" in sys.argv:
        try:
            idx = sys.argv.index("--model")
            if idx + 1 < len(sys.argv):
                selected_model = sys.argv[idx + 1]
        except: pass
        
    if "--output" in sys.argv:
        try:
            idx = sys.argv.index("--output")
            if idx + 1 < len(sys.argv):
                custom_output_dir = sys.argv[idx + 1]
        except: pass
            
    if mode == "estimate":
        dur = get_duration(file_path)
        est = estimate_processing_time(dur)
        print(json.dumps({"duration": dur, "estimated_seconds": est}))
        
    elif mode == "run":
        if not os.path.exists(file_path):
            print(f"Error: File {file_path} not found.")
            sys.exit(1)

        if custom_output_dir:
            project_dir = custom_output_dir
        else:
            project_dir = get_project_folder(file_path)
            
        if not os.path.exists(project_dir):
            try: os.makedirs(project_dir)
            except: 
                if not custom_output_dir: # Only fallback if not custom
                    project_dir = os.path.dirname(os.path.abspath(file_path))
        
        output_dir = project_dir
        
        # Strip "faster-whisper-" prefix if present, as library expects just the size or path
        raw_model_name = selected_model
        if raw_model_name.startswith("faster-whisper-"):
            raw_model_name = raw_model_name.replace("faster-whisper-", "", 1)
            
        print(f"File: {os.path.basename(file_path)}")
        print(f"Output: {output_dir}")
        print(f"Model: {raw_model_name}")
        print(f"Starting transcription...")
        
        # Get duration for progress calculation
        total_duration = get_duration(file_path)
        
        # Setup specific UI for progress
        no_gui = "--no-gui" in sys.argv
        root = None
        if not no_gui:
            try:
                import tkinter as tk
                from tkinter import ttk
                
                root = tk.Tk()
                root.title("Transcribing...")
                # Center the window
                w = 400
                h = 150
                ws = root.winfo_screenwidth()
                hs = root.winfo_screenheight()
                x = (ws/2) - (w/2)
                y = (hs/2) - (h/2)
                root.geometry('%dx%d+%d+%d' % (w, h, x, y))
                root.resizable(False, False)
                
                # Label
                lbl_status = tk.Label(root, text=f"Processing: {os.path.basename(file_path)}", wraplength=380)
                lbl_status.pack(pady=10)
                
                # Progress bar
                progress_var = tk.DoubleVar()
                pbar = ttk.Progressbar(root, variable=progress_var, maximum=100, length=350, mode='determinate')
                pbar.pack(pady=10)
                
                # Time Label
                lbl_time = tk.Label(root, text="Initializing...")
                lbl_time.pack(pady=5)
                
                root.update()
            except Exception as e:
                print(f"UI Error: {e}")
                root = None
        else:
            print("No-GUI mode enabled.")

        start_time = time.time()
        
        try:
            # Use CUDA if available, else CPU
            device = "cuda" if ctypes.windll.kernel32.GetModuleHandleW("nvcuda.dll") else "cpu"
            print(f"Device: {device}")

            # --- Smart Model Discovery ---
            model_found_root = None
            def get_search_dirs():
                dirs = []
                if LOCAL_MODELS: dirs.append(LOCAL_MODELS)
                if DOCS_MODELS: dirs.append(DOCS_MODELS)
                
                # Standard locations & Environment variables
                for env in ["HF_HOME", "HUGGINGFACE_HUB_CACHE", "AUTOSUB_MODELS"]:
                    val = os.environ.get(env)
                    if val: dirs.append(val)
                
                dirs.append(STD_CACHE)
                
                # Proactive discovery for custom system setups (like SystemMoves)
                for drive in ['D', 'E', 'F', 'G', 'C']:
                    alt = f"{drive}:\\SystemMoves\\faster-whisper"
                    if os.path.exists(alt) and alt not in dirs:
                        dirs.append(alt)
                
                return [d for d in dirs if d is not None and os.path.exists(d)]

            search_dirs = get_search_dirs()
            model_folder_name = f"models--Systran--faster-whisper-{raw_model_name}"
            
            # Target identification
            actual_model_path_or_name = raw_model_name
            best_download_root = search_dirs[0] if search_dirs else None

            for d in search_dirs:
                # 1. Exact HF Hub folder structure
                if os.path.exists(os.path.join(d, model_folder_name)):
                    best_download_root = d
                    print(f"✅ Found model cache in: {d}")
                    break
                
                # 2. Direct size folder (manual download)
                direct_path = os.path.join(d, raw_model_name)
                if os.path.exists(os.path.join(direct_path, "model.bin")):
                    actual_model_path_or_name = direct_path
                    best_download_root = d
                    print(f"✅ Found direct model folder in: {d}")
                    break

            try:
                print(f"Attempting to load model '{raw_model_name}' on {device}...")
                model = WhisperModel(actual_model_path_or_name, device=device, compute_type="auto", download_root=best_download_root)
            except Exception as e:
                if device == "cuda":
                    print(f"⚠️ CUDA initialization failed, falling back to CPU: {e}")
                    model = WhisperModel(actual_model_path_or_name, device="cpu", compute_type="int8", download_root=best_download_root)
                else:
                    print(f"❌ Error loading model: {e}")
                    sys.exit(1)

            segments, info = model.transcribe(file_path, beam_size=5, vad_filter=True, initial_prompt="Claude Code, Anthropic, AI Agent", word_timestamps=True)
            
            print("Detected language '%s' with probability %f" % (info.language, info.language_probability))

            # Perform transcription with a real-time progress indicator and early pacing detection
            print(f"🎙️ Transcribing & Analyzing Pacing (using {device})...")
            segments_list = []
            detected_style = None
            
            duration = info.duration
            last_p = -1
            
            for s in segments:
                segments_list.append(s)
                
                # --- Early Pacing Detection ---
                # We don't need the whole video. 60s or 20 segments is enough to decide.
                if not detected_style and (s.end > 60 or len(segments_list) > 20):
                    detected_style = detect_content_type(segments_list)
                    print(f"✨ Style locked: '{detected_style}' (base on first {s.end:.0f}s)")
                
                if duration > 0:
                    p = int((s.end / duration) * 100)
                    if p > last_p and p % 5 == 0:
                        print(f"   Progress: {p}% ({s.end:.0f}/{duration:.0f}s)")
                        last_p = p

            # Final safety check if video is extremely short
            if not detected_style:
                detected_style = detect_content_type(segments_list)

            print(f"✅ Transcription complete. {len(segments_list)} segments collected.")

            srt_path = os.path.join(output_dir, os.path.splitext(os.path.basename(file_path))[0] + ".srt")
            
            with open(srt_path, "w", encoding="utf-8") as f:
                # Pass the early detected style to chunk_segments
                for segment in chunk_segments(segments_list, content_type=detected_style):
                    # Format timestamp
                    start = segment.start
                    end = segment.end
                    text = segment.text.strip()
                    
                    def fmt(t):
                        hours = int(t // 3600)
                        minutes = int((t % 3600) // 60)
                        seconds = int(t % 60)
                        milliseconds = int((t - int(t)) * 1000)
                        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
                    
                    print(f"[{fmt(start)} --> {fmt(end)}] {text}")
                    f.write(f"{segment.id}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")
                    
                    # Update Progress
                    pct = (end / total_duration) * 100 if total_duration > 0 else 0
                    progress_msg = f"Progress: {pct:.1f}% ({fmt(end)} / {fmt(total_duration)})"
                    print(progress_msg, flush=True)
                    
                    if root and total_duration > 0:
                        try:
                            progress_var.set(pct)
                            lbl_time.config(text=progress_msg)
                            root.update()
                        except:
                            root = None # Stop trying to update if UI is closed/failed
            
            if root:
                root.destroy()
                
            elapsed = time.time() - start_time
            msg = f"Done!\nProject: {os.path.basename(project_dir)}\nTime: {elapsed:.2f}s"
            print(msg)
            if not no_gui:
                show_notification("Transcriber Complete", msg)
                
        except Exception as e:
            print(f"Execution Error: {e}")
            if not no_gui:
                show_notification("Transcriber Error", str(e))
            sys.exit(1)

if __name__ == "__main__":
    main()
