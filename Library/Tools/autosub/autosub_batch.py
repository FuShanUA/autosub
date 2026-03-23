import os
import sys
import argparse
import subprocess
import glob
import time
import re
import shutil
import queue
import threading
import psutil
from datetime import datetime
from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.console import Group
from rich.live import Live
from rich.text import Text

# Global pause event for pausing batch scheduling
global_pause = threading.Event()
global_pause.set()
global_abort = False
global_ui_log = "[grey50]等候指令中...[/grey50]"

active_processes = {}  # title -> pid
paused_videos = set()  # set of titles globally paused

video_ids = {}         # title -> char ID
next_id_idx = 0
ID_CHARS = "1234567890abcdefghijklmnopqrstuvwxyz"

registry = [] # Global list of projects dicts
registry_lock = threading.RLock()

def get_video_id(title):
    global next_id_idx
    with registry_lock:
        if title not in video_ids:
            video_ids[title] = ID_CHARS[next_id_idx % len(ID_CHARS)]
            next_id_idx += 1
        return video_ids[title]

def suspend_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True): child.suspend()
        parent.suspend()
    except Exception: pass

def resume_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True): child.resume()
        parent.resume()
    except Exception: pass

# Setup path and import autosub
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.append(CURRENT_DIR)

try:
    import autosub
    from autosub import get_video_duration, VDOWN_CMD, TRANSCRIBER_CMD, SMART_TRANSLATE_CMD, SUBTRANSLATOR_CMD, SRT2ASS_CMD, BURNSUB_CMD
except Exception as e:
    print(f"Failed to import autosub: {e}")
    sys.exit(1)

def sanitize_filename(name):
    clean = re.sub(r'[\\/*?:"<>|]', '_', name)
    clean = clean.strip().strip('.')
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:100]

def log_event(workdir, level, msg):
    log_path = os.path.join(workdir, "batch_workflow.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{level}] {msg}\n")

# --- Dynamic UI Architecture ---
class StatefulBarColumn(BarColumn):
    def render(self, task):
        # Override styles dynamically depending on task flags
        state = task.fields.get('ui_state', 'unstarted')
        if state == 'skipped':
            self.complete_style = "grey50"
            self.finished_style = "grey50"
        elif state == 'failed':
            self.complete_style = "bright_red"
            self.finished_style = "bright_red"
        elif state == 'completed':
            self.complete_style = "green"
            self.finished_style = "green"
        elif state == 'active':
            self.complete_style = "bright_blue"
            self.finished_style = "green"
        else: # unstarted
            self.complete_style = "grey15"
            self.finished_style = "grey15"
        return super().render(task)

def create_progress():
    return Progress(
        TextColumn("{task.description}", justify="left"),
        StatefulBarColumn(bar_width=40, style="grey15", pulse_style="grey15"),
        TextColumn("[{task.fields[pct_color]}]{task.percentage:>5.2f}%", justify="right"),
        TextColumn("用时:"), TimeElapsedColumn(),
        TextColumn("剩余:"), TimeRemainingColumn(),
    )

prog_dl = create_progress()
prog_tr = create_progress()
prog_tl = create_progress()
prog_mg = create_progress()
prog_bn = create_progress()

stage_visibility = {"dl": False, "tr": False, "tl": False, "mg": False, "bn": False}
stage_locks = {k: threading.Lock() for k in stage_visibility}
tasks_dl = {}; tasks_tr = {}; tasks_tl = {}; tasks_mg = {}; tasks_bn = {}

def trigger_stage(stage_key, prog_instance, task_map, stage_prefix):
    with stage_locks[stage_key]:
        if not stage_visibility[stage_key]:
            with registry_lock:
                for proj in registry:
                    if stage_key == "dl" and not proj.get("is_url") and not proj.get("is_local"): continue
                    vid = get_video_id(proj['title'])
                    desc = f"[grey50][ID:{vid}] {stage_prefix}: {proj['title']}[/grey50]"
                    tid = prog_instance.add_task(desc, total=100.0, start=False, vid=vid, title=proj['title'], ui_state="unstarted", pct_color="grey50")
                    task_map[proj['title']] = tid
            stage_visibility[stage_key] = True

class DynamicPipelineGroup:
    def __rich__(self):
        items = []
        if stage_visibility["dl"]: items.extend([Text("【 第1阶段：视频下载 】", style="bold yellow"), prog_dl, Text("")])
        if stage_visibility["tr"]: items.extend([Text("【 第2阶段：语音转录 】", style="bold yellow"), prog_tr, Text("")])
        if stage_visibility["tl"]: items.extend([Text("【 第3阶段：智能翻译 】", style="bold yellow"), prog_tl, Text("")])
        if stage_visibility["mg"]: items.extend([Text("【 第4阶段：字幕合并 】", style="bold yellow"), prog_mg, Text("")])
        if stage_visibility["bn"]: items.extend([Text("【 第5阶段：字幕烧录 】", style="bold yellow"), prog_bn, Text("")])
        if not items: items = [Text("🚀 队列初始化中，等候分配...", style="bold magenta")]
        
        items.append(Text(""))
        items.append(Text("─" * 80, style="grey50"))
        items.append(Text("💡 键盘快捷菜单:  [ P ] = 全局挂起/恢复    [ K ] = 强杀所有子进程并终止    [ 0-9, a-z ] = 单挂某个视频", style="bold cyan"))
        items.append(Text.from_markup(global_ui_log))
        
        return Group(*items)
# -------------------------------

def run_cmd_with_progress(cmd, progress, task_id, step_name, workdir, title):
    log_event(workdir, "START", f"Starting {step_name}: {' '.join(cmd)}")
    try:
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            encoding='utf-8', 
            errors='replace', 
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        active_processes[title] = process.pid
        if title in paused_videos: suspend_process_tree(process.pid)
        
        last_percent = 0.0
        for line in process.stdout:
            msg = line.strip()
            percent = None
            if "Progress:" in msg:
                m = re.search(r'Progress:\s*([\d\.]+)%', msg)
                if m: percent = float(m.group(1))
            elif "[download]" in msg and "%" in msg:
                m = re.search(r'\[download\]\s*([\d\.]+)%', msg)
                if m: percent = float(m.group(1))
            
            if percent is not None and percent > last_percent:
                progress.update(task_id, completed=percent)
                last_percent = percent
                
        process.wait()
        if process.returncode == 0:
            log_event(workdir, "SUCCESS", f"Completed {step_name} at 100.00%")
            progress.update(task_id, completed=100.0)
            if title in active_processes: del active_processes[title]
            return True
        else:
            log_event(workdir, "FAIL", f"{step_name} failed with exit code {process.returncode}")
            if title in active_processes: del active_processes[title]
            return False
    except Exception as e:
        log_event(workdir, "FAIL", f"Error running {step_name}: {e}")
        if title in active_processes: del active_processes[title]
        return False

def downloader_worker(download_queue, transcribe_queue, args, output_dir):
    while not download_queue.empty():
        if global_abort: break
        global_pause.wait()
        try:
            item = download_queue.get(timeout=1)
            url = item['url']
            initial_title = item['title']
            
            trigger_stage("dl", prog_dl, tasks_dl, "下载")
            vid = get_video_id(initial_title)
            
            if url.startswith("local://"):
                video_path = url[8:]
                safe_title = initial_title
                workdir = os.path.dirname(video_path)
                tid = tasks_dl[safe_title]
                prog_dl.start_task(tid)
                prog_dl.update(tid, description=f"[grey50][ID:{vid}] 下载: {safe_title} (本地)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                transcribe_queue.put({"video_path": video_path, "workdir": workdir, "title": safe_title})
                download_queue.task_done()
                continue
            
            # Start resolving real title
            title = autosub.get_video_title(url, args.cookies)
            safe_title = sanitize_filename(title) if title else initial_title
            
            # Update registry and ID mapping if title resolved
            if safe_title != initial_title:
                with registry_lock:
                    for p in registry:
                        if p['title'] == initial_title: p['title'] = safe_title
                    video_ids[safe_title] = video_ids.pop(initial_title)
                # Update task mapping
                if initial_title in tasks_dl:
                    tasks_dl[safe_title] = tasks_dl.pop(initial_title)
            
            workdir = os.path.join(output_dir, safe_title)
            os.makedirs(workdir, exist_ok=True)
            
            tid = tasks_dl[safe_title]
            
            # Skip logic for downloader
            vids = []
            if os.path.exists(workdir):
                vids = [f for f in glob.glob(os.path.join(workdir, "*")) if os.path.splitext(f)[1].lower() in ['.mp4', '.mkv', '.webm', '.ts', '.mov', '.avi'] and not f.endswith('.part') and '_hardsub' not in f.lower()]
            if vids:
                log_event(workdir, "SKIP", f"Video exists: {os.path.basename(vids[0])}")
                video_path = max(vids, key=os.path.getmtime)
                prog_dl.start_task(tid)
                prog_dl.update(tid, description=f"[grey50][ID:{vid}] 下载: {safe_title} (已有)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                transcribe_queue.put({"video_path": video_path, "workdir": workdir, "title": safe_title})
                download_queue.task_done()
                continue
            
            prog_dl.start_task(tid)
            prog_dl.update(tid, description=f"[bold blue][ID:{vid}] 下载: {safe_title}[/bold blue]", ui_state="active", pct_color="bright_blue")
            
            cmd = list(VDOWN_CMD) + [url, args.cookies or "", workdir]
            success = run_cmd_with_progress(cmd, prog_dl, tid, "Download", workdir, safe_title)
            
            if success:
                prog_dl.update(tid, description=f"[bold green][ID:{vid}] 下载: {safe_title}[/bold green]", ui_state="completed", pct_color="green", completed=100.0)
                vids = [f for f in glob.glob(os.path.join(workdir, "*")) if os.path.splitext(f)[1].lower() in ['.mp4', '.mkv', '.webm', '.ts', '.mov', '.avi'] and not f.endswith('.part') and '_hardsub' not in f.lower()]
                if vids:
                    video_path = max(vids, key=os.path.getmtime)
                    transcribe_queue.put({"video_path": video_path, "workdir": workdir, "title": safe_title})
            else:
                prog_dl.update(tid, description=f"[bold red][ID:{vid}] 下载: {safe_title} (失败)[/bold red]", ui_state="failed", pct_color="bright_red")
                
            download_queue.task_done()
        except queue.Empty:
            break

def transcriber_worker(transcribe_queue, translate_queue, args, state):
    while not (state['download_done'] and transcribe_queue.empty()):
        if global_abort: break
        global_pause.wait()
        try:
            item = transcribe_queue.get(timeout=1)
            video_path = item["video_path"]
            workdir = item["workdir"]
            title = item["title"]
            base = os.path.splitext(os.path.basename(video_path))[0]
            
            trigger_stage("tr", prog_tr, tasks_tr, "转录")
            vid = get_video_id(title)
            tid = tasks_tr.get(title)
            if tid is None: # fallback
                tid = prog_tr.add_task(f"[grey50][ID:{vid}] 转录: {title}[/grey50]", total=100.0, start=False, title=title, vid=vid, ui_state="unstarted", pct_color="grey50")
                tasks_tr[title] = tid
            
            expected_srt = os.path.join(workdir, base + ".srt")
            expected_en = os.path.join(workdir, base + ".en.srt")
            
            src_srt = None
            if os.path.exists(expected_srt) and os.path.getsize(expected_srt) > 500: src_srt = expected_srt
            elif os.path.exists(expected_en) and os.path.getsize(expected_en) > 500: src_srt = expected_en
            
            if src_srt:
                log_event(workdir, "SKIP", f"Transcription exists: {os.path.basename(src_srt)}")
                item["src_srt"] = src_srt
                prog_tr.start_task(tid)
                prog_tr.update(tid, description=f"[grey50][ID:{vid}] 转录: {title} (跳过)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                translate_queue.put(item)
            else:
                dur = autosub.get_video_duration(video_path)
                dur_str = f"{int(dur)}s"
                
                prog_tr.start_task(tid)
                prog_tr.update(tid, description=f"[bold blue][ID:{vid}] 转录: {title} [{dur_str}][/bold blue]", ui_state="active", pct_color="bright_blue")
                
                cmd = list(TRANSCRIBER_CMD) + [video_path, "--model", args.model, "--output", workdir, "--no-gui"]
                success = run_cmd_with_progress(cmd, prog_tr, tid, "Transcribe", workdir, title)
                
                if success:
                    res = os.path.join(workdir, os.path.splitext(os.path.basename(video_path))[0] + ".srt")
                    if os.path.exists(res):
                        en_res = res.replace(".srt", ".en.srt")
                        if not os.path.exists(en_res): shutil.copy2(res, en_res)
                        item["src_srt"] = res
                        prog_tr.update(tid, description=f"[bold green][ID:{vid}] 转录: {title} [{dur_str}][/bold green]", ui_state="completed", pct_color="green", completed=100.0)
                        translate_queue.put(item)
                else:
                    prog_tr.update(tid, description=f"[bold red][ID:{vid}] 转录: {title} (失败)[/bold red]", ui_state="failed", pct_color="bright_red")
                    
            transcribe_queue.task_done()
        except queue.Empty:
            continue

def translator_worker(translate_queue, burn_queue, args, state):
    while not (state['transcribe_done'] and translate_queue.empty()):
        if global_abort: break
        global_pause.wait()
        try:
            item = translate_queue.get(timeout=1)
            src_srt = item["src_srt"]
            workdir = item["workdir"]
            title = item["title"]
            base = os.path.splitext(os.path.basename(item["video_path"]))[0]
            
            trigger_stage("tl", prog_tl, tasks_tl, "翻译")
            vid = get_video_id(title)
            tid = tasks_tl.get(title)
            if tid is None:
                tid = prog_tl.add_task(f"[grey50][ID:{vid}] 翻译: {title}[/grey50]", total=100.0, start=False, title=title, vid=vid, ui_state="unstarted", pct_color="grey50")
                tasks_tl[title] = tid
            
            expected_cn = os.path.join(workdir, base + ".cn.srt")
            expected_zh = os.path.join(workdir, base + ".zh.srt")
            
            zh_srt = None
            if os.path.exists(expected_cn) and os.path.getsize(expected_cn) > 500: zh_srt = expected_cn
            elif os.path.exists(expected_zh) and os.path.getsize(expected_zh) > 500: zh_srt = expected_zh
            
            if zh_srt:
                log_event(workdir, "SKIP", f"Translation exists: {os.path.basename(zh_srt)}")
                item["zh_srt"] = zh_srt
                prog_tl.start_task(tid)
                prog_tl.update(tid, description=f"[grey50][ID:{vid}] 翻译: {title} (跳过)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                burn_queue.put(item)
            else:
                prog_tl.start_task(tid)
                prog_tl.update(tid, description=f"[bold blue][ID:{vid}] 翻译: {title}[/bold blue]", ui_state="active", pct_color="bright_blue")
                
                cmd = [sys.executable, SMART_TRANSLATE_CMD[1], src_srt, "--style", args.style, "--model", args.llm_model, "--trans-mode", args.trans_mode]
                success = run_cmd_with_progress(cmd, prog_tl, tid, "Translate", workdir, title)
                
                if success:
                    zh_srt_path = expected_cn if os.path.exists(expected_cn) else expected_zh
                    if os.path.exists(zh_srt_path):
                        item["zh_srt"] = zh_srt_path
                        prog_tl.update(tid, description=f"[bold green][ID:{vid}] 翻译: {title}[/bold green]", ui_state="completed", pct_color="green", completed=100.0)
                        burn_queue.put(item)
                else:
                    prog_tl.update(tid, description=f"[bold red][ID:{vid}] 翻译: {title} (失败)[/bold red]", ui_state="failed", pct_color="bright_red")
            
            translate_queue.task_done()
        except queue.Empty:
            continue

def burner_worker(burn_queue, args, state):
    while not (state['translate_done'] and burn_queue.empty()):
        if global_abort: break
        global_pause.wait()
        try:
            item = burn_queue.get(timeout=1)
            src_srt = item["src_srt"]
            zh_srt = item["zh_srt"]
            video_path = item["video_path"]
            workdir = item["workdir"]
            title = item["title"]
            
            video_ext = os.path.splitext(video_path)[1]
            out_video = os.path.join(workdir, os.path.splitext(os.path.basename(video_path))[0] + "_hardsub" + video_ext)
            
            trigger_stage("mg", prog_mg, tasks_mg, "合并")
            trigger_stage("bn", prog_bn, tasks_bn, "烧录")
            vid = get_video_id(title)
            
            tid_mg = tasks_mg.get(title)
            tid_bn = tasks_bn.get(title)
            
            if os.path.exists(out_video) and os.path.getsize(out_video) > 1024 * 1024:
                log_event(workdir, "SKIP", f"Hardsub exists: {os.path.basename(out_video)}")
                prog_mg.start_task(tid_mg)
                prog_mg.update(tid_mg, description=f"[grey50][ID:{vid}] 合并: {title} (跳过)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                prog_bn.start_task(tid_bn)
                prog_bn.update(tid_bn, description=f"[grey50][ID:{vid}] 烧录: {title} (跳过)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                burn_queue.task_done()
                continue
            
            # Merge & Format (.ass generation logically belongs here)
            prog_mg.start_task(tid_mg)
            
            bi_path = src_srt[:-7] + ".bi.srt" if src_srt.lower().endswith(".en.srt") else src_srt.replace(".srt", ".bi.srt")
            final_srt_candidate = bi_path if (os.path.exists(bi_path) and os.path.getsize(bi_path) > 100) else zh_srt
            ass_path_candidate = os.path.splitext(final_srt_candidate)[0] + ".ass"
            
            needs_bi = not (os.path.exists(bi_path) and os.path.getsize(bi_path) > 100)
            needs_ass = not os.path.exists(ass_path_candidate)
            
            if needs_bi or needs_ass:
                prog_mg.update(tid_mg, description=f"[bold blue][ID:{vid}] 合并: {title}[/bold blue]", ui_state="active", pct_color="bright_blue")
                
                if needs_bi:
                    cmd_merge = list(SUBTRANSLATOR_CMD) + ["merge", src_srt, "--translated-file", zh_srt]
                    run_cmd_with_progress(cmd_merge, prog_mg, tid_mg, "Merge", workdir, title)
                
                final_srt = bi_path if os.path.exists(bi_path) else zh_srt
                ass_path = os.path.splitext(final_srt)[0] + ".ass"
                
                if not os.path.exists(ass_path):
                    width, height = autosub.get_video_dimensions(video_path)
                    cmd_ass = list(SRT2ASS_CMD) + [final_srt, ass_path, "--layout", args.layout, "--main-lang", args.main_lang, "--cn-font", args.cn_font, "--en-font", args.en_font, "--cn-size", args.cn_size, "--en-size", args.en_size, "--cn-color", args.cn_color, "--en-color", args.en_color, "--width", str(width), "--height", str(height)]
                    if args.no_bg_box: cmd_ass.append("--no-bg-box")
                    subprocess.run(cmd_ass, check=False)
                
                prog_mg.update(tid_mg, description=f"[bold green][ID:{vid}] 合并: {title}[/bold green]", ui_state="completed", pct_color="green", completed=100.0)
            else:
                log_event(workdir, "SKIP", f"Bilingual and ASS styles exist: {os.path.basename(bi_path)}")
                prog_mg.update(tid_mg, description=f"[grey50][ID:{vid}] 合并: {title} (跳过)[/grey50]", ui_state="skipped", pct_color="grey50", completed=100.0)
                final_srt = final_srt_candidate
                ass_path = ass_path_candidate
            
            # Burn
            prog_bn.start_task(tid_bn)
            prog_bn.update(tid_bn, description=f"[bold blue][ID:{vid}] 烧录: {title}[/bold blue]", ui_state="active", pct_color="bright_blue")
            
            # If the final _hardsub output already exists and we didn't skip it, generate a versioned filename
            def get_versioned_filename(filepath):
                if not os.path.exists(filepath): return filepath
                base, ext = os.path.splitext(filepath)
                match = re.search(r'_v(\d+)$', base)
                if match:
                    version = int(match.group(1)); base = base[:match.start()]
                else: version = 0
                while True:
                    version += 1
                    new_path = f"{base}_v{version}{ext}"
                    if not os.path.exists(new_path): return new_path

            safe_out_video = get_versioned_filename(out_video)
            
            cmd_burn = list(BURNSUB_CMD) + [video_path, ass_path, safe_out_video, "--headless"]
            success = run_cmd_with_progress(cmd_burn, prog_bn, tid_bn, "Burn", workdir, title)
            
            if success:
                prog_bn.update(tid_bn, description=f"[bold green][ID:{vid}] 烧录: {title}[/bold green]", ui_state="completed", pct_color="green", completed=100.0)
            else:
                prog_bn.update(tid_bn, description=f"[bold red][ID:{vid}] 烧录: {title} (失败)[/bold red]", ui_state="failed", pct_color="bright_red")
            
            burn_queue.task_done()
        except queue.Empty:
            continue

def run_batch(args):
    raw_urls = []
    if args.batch_urls:
        for u in args.batch_urls:
            if os.path.isfile(u):
                with open(u, 'r', encoding='utf-8') as f:
                    raw_urls.extend([line.strip() for line in f if line.strip() and not line.startswith("#")])
            else:
                raw_urls.append(u)
    
    unique_urls = []
    seen = set()
    for u in raw_urls:
        if u not in seen:
            unique_urls.append(u)
            seen.add(u)
    urls = unique_urls
    
    output_dir = args.output_dir or autosub.BASE_OUTPUT_DIR
    if not os.path.isabs(output_dir):
        output_dir = os.path.abspath(os.path.join(autosub.PROJECT_ROOT, output_dir))
    
    download_queue = queue.Queue()
    transcribe_queue = queue.Queue()
    translate_queue = queue.Queue()
    burn_queue = queue.Queue()
    
    count_temp = 0
    for u in urls:
        count_temp += 1
        tname = f"URL_{count_temp}"
        registry.append({"title": tname, "is_url": True, "url": u})
        download_queue.put({'url': u, 'title': tname})
        
    if args.batch_dir and os.path.isdir(args.batch_dir):
        for entry in os.listdir(args.batch_dir):
            workdir = os.path.join(args.batch_dir, entry)
            if os.path.isdir(workdir):
                vids = [f for f in glob.glob(os.path.join(workdir, "*")) if os.path.splitext(f)[1].lower() in ['.mp4', '.mkv', '.webm', '.ts', '.mov', '.avi'] and '_hardsub' not in f.lower()]
                if vids:
                    video_path = max(vids, key=os.path.getmtime)
                    registry.append({"title": entry, "is_url": False, "is_local": True})
                    download_queue.put({'url': f"local://{video_path}", 'title': entry})
                    
    state = {'download_done': False, 'transcribe_done': False, 'translate_done': False}
    workers = min(max(int(args.workers), 1), 10)
    translate_workers = max(1, int(args.max_api_calls) // 10)
    
    print(f"🚀 启动分布式动态批处理框架 (Workers={workers}, Translate Workers={translate_workers})")
    
    def key_listener(live_instance):
        global global_abort, global_ui_log
        import msvcrt
        while not global_abort:
            if msvcrt.kbhit():
                k = msvcrt.getch()
                k_lower = k.lower()
                if k_lower == b'p':
                    if global_pause.is_set(): 
                        global_pause.clear()
                        with registry_lock:
                            for title, pid in list(active_processes.items()):
                                if title not in paused_videos:
                                    suspend_process_tree(pid)
                                    paused_videos.add(title)
                        global_ui_log = "⏸️ [bold red]已强制挂起全局队列分配及所有进行中的任务...[/bold red]"
                    else: 
                        global_pause.set()
                        with registry_lock:
                            for title, pid in list(active_processes.items()):
                                if title in paused_videos:
                                    resume_process_tree(pid)
                                    paused_videos.remove(title)
                        global_ui_log = "▶️ [bold green]恢复全局队列分配及所有进行中的任务...[/bold green]"
                    # Update all UI tasks dynamically
                    for prog in [prog_dl, prog_tr, prog_tl, prog_mg, prog_bn]:
                        for task in prog.tasks:
                            title_val = getattr(task, "fields", {}).get('title')
                            if title_val:
                                ui_state = getattr(task, "fields", {}).get('ui_state')
                                base_desc = task.description.replace('[PAUSED] ', '')
                                new_desc = f"[PAUSED] {base_desc}" if (title_val in paused_videos and ui_state == 'active') else base_desc
                                prog.update(task.id, description=new_desc)
                elif k_lower == b'k':
                    global_abort = True
                    global_pause.set()
                    with registry_lock:
                        for title, pid in list(active_processes.items()):
                            try:
                                parent = psutil.Process(pid)
                                for child in parent.children(recursive=True): child.kill()
                                parent.kill()
                            except: pass
                    global_ui_log = "🛑 [bold red]紧急操作：已精准截杀本次衍生的所有 ffmpeg/python 子进程，并停止流水线！[/bold red]"
                else:
                    try:
                        char = k_lower.decode('utf-8')
                        found_title = None
                        with registry_lock:
                            for title, vid in video_ids.items():
                                if str(vid) == char:
                                    found_title = title
                                    break
                        if found_title:
                            if found_title in paused_videos:
                                paused_videos.remove(found_title)
                                if found_title in active_processes: resume_process_tree(active_processes[found_title])
                                global_ui_log = f"▶️ [cyan]已恢复 '{found_title}' 的并发子进程。[/cyan]"
                            else:
                                paused_videos.add(found_title)
                                if found_title in active_processes: suspend_process_tree(active_processes[found_title])
                                global_ui_log = f"⏸️ [yellow]已原生冻结 '{found_title}' 的并发子进程！[/yellow]"
                            
                            for prog in [prog_dl, prog_tr, prog_tl, prog_mg, prog_bn]:
                                for task in prog.tasks:
                                    if getattr(task, "fields", {}).get('title') == found_title:
                                        ui_state = getattr(task, "fields", {}).get('ui_state')
                                        base_desc = task.description.replace('[PAUSED] ', '')
                                        new_desc = f"[PAUSED] {base_desc}" if (found_title in paused_videos and ui_state == 'active') else base_desc
                                        prog.update(task.id, description=new_desc)
                    except: pass
            time.sleep(0.1)

    # Clear terminal to prevent initialization artifacts from showing duplicate headers
    time.sleep(0.5)
    os.system("cls" if os.name == "nt" else "clear")

    with Live(DynamicPipelineGroup(), refresh_per_second=10, screen=True) as live:
        threading.Thread(target=key_listener, args=(live,), daemon=True).start()
        
        dl_threads = []
        if not download_queue.empty():
            for _ in range(workers):
                t = threading.Thread(target=downloader_worker, args=(download_queue, transcribe_queue, args, output_dir))
                t.start()
                dl_threads.append(t)
                
        tr_threads = []
        for _ in range(workers):
            t = threading.Thread(target=transcriber_worker, args=(transcribe_queue, translate_queue, args, state))
            t.start()
            tr_threads.append(t)
            
        tl_threads = []
        for _ in range(translate_workers):
            t = threading.Thread(target=translator_worker, args=(translate_queue, burn_queue, args, state))
            t.start()
            tl_threads.append(t)
            
        bn_threads = []
        for _ in range(workers):
            t = threading.Thread(target=burner_worker, args=(burn_queue, args, state))
            t.start()
            bn_threads.append(t)

        for t in dl_threads: t.join()
        state['download_done'] = True
        
        for t in tr_threads: t.join()
        state['transcribe_done'] = True
        
        for t in tl_threads: t.join()
        state['translate_done'] = True
        
        for t in bn_threads: t.join()

    if global_abort:
        print("\n⚠️ 批量处理已被强制终止！已产出的字幕或文件将予以保留，发生中断的压制/下载在下次启动时需重新开始。")
    else:
        print("\n🎉 批量处理全部完成！")
