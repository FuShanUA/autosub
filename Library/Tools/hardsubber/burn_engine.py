import sys
import ctypes
import subprocess
import os
import shutil
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import threading
import re
import time
import datetime

# Config
# Config
# Updated to user-provided path
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

FFMPEG_PATH = get_ffmpeg_path()
if FFMPEG_PATH != "ffmpeg":
    print(f"📦 Found FFmpeg at: {FFMPEG_PATH}")

def get_optimized_encoder(ffmpeg_path):
    """Detects available hardware encoders by running a robust dry-run test."""
    try:
        # Run a 0.1-second dummy encode to test if NVENC actually works on the GPU.
        # This prevents returning 'nvenc' on machines that list it but lack CUDA drivers/DLLs.
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        test_cmd = [
            ffmpeg_path,
            "-v", "error", 
            "-f", "lavfi", 
            "-i", "nullsrc=s=128x128:d=0.1", 
            "-c:v", "h264_nvenc", 
            "-f", "null", 
            "-"
        ]
        
        result = subprocess.run(test_cmd, capture_output=True, startupinfo=startupinfo)
        
        if result.returncode == 0:
            print("🚀 Hardware Acceleration (NVENC) Enabled & Verified!")
            return "h264_nvenc", ["-preset", "p4", "-rc", "constqp", "-qp", "23"]
    except Exception as e:
        pass
        
    print("ℹ️ Using CPU encoding (libx264, preset=veryfast) for maximum stability.")
    return "libx264", ["-preset", "veryfast", "-crf", "23", "-threads", "0"]

def parse_time_str(time_str):
    """Converts HH:MM:SS.mm to seconds."""
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0.0

def format_seconds(seconds):
    """Converts seconds to HH:MM:SS."""
    return str(datetime.timedelta(seconds=int(seconds)))


def validate_ass(ass_path):
    """
    Validates the ASS file for common timing errors.
    Returns: (is_valid, messages)
    is_valid: False if critical errors found (like Start > End).
    messages: List of warning/error strings.
    """
    errors = []
    warnings = []
    
    try:
        with open(ass_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        for i, line in enumerate(lines):
            if line.startswith("Dialogue:"):
                # Format: Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
                parts = line.split(",", 9)
                if len(parts) < 10:
                    continue
                    
                start_str = parts[1]
                end_str = parts[2]
                
                try:
                    start_sec = parse_time_str(start_str)
                    end_sec = parse_time_str(end_str)
                except:
                    continue
                
                if start_sec > end_sec:
                    errors.append(f"Line {i+1}: Start ({start_str}) > End ({end_str})")
                elif (end_sec - start_sec) > 60.0:
                    warnings.append(f"Line {i+1}: Duration > 60s ({end_sec - start_sec:.2f}s)")
                    
    except Exception as e:
        return False, [f"Failed to read/parse ASS file: {e}"]

    if errors:
        return False, errors + warnings
    return True, warnings

class BurnProgressApp:
    def __init__(self, root, video_path, ass_path, output_path, headless=False):
        self.root = root
        self.headless = headless
        
        self.video_path = video_path
        self.ass_path = ass_path
        self.output_path = output_path
        self.total_duration_sec = 0.0
        self.finished = False

        if not self.headless:
            self.root.title("Hardsub Burning Progress")
            self.root.geometry("600x300") # Increased size for potential error msg
            self.root.bind('<space>', self.on_space)

            # UI Elements
            main_frame = ttk.Frame(root, padding="20")
            main_frame.pack(fill=tk.BOTH, expand=True)

            ttk.Label(main_frame, text="Burning Subtitles...", font=("Helvetica", 12, "bold")).pack(pady=(0, 10))
            
            info_frame = ttk.Frame(main_frame)
            info_frame.pack(fill=tk.X, pady=5)
            
            ttk.Label(info_frame, text=f"Input: {os.path.basename(video_path)}").pack(anchor="w")
            ttk.Label(info_frame, text=f"Output: {os.path.basename(output_path)}").pack(anchor="w")

            # Progress Bar
            self.progress_var = tk.DoubleVar()
            self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
            self.progress_bar.pack(fill=tk.X, pady=20)

            # Stats Labels
            stats_frame = ttk.Frame(main_frame)
            stats_frame.pack(fill=tk.X)
            
            self.lbl_total = ttk.Label(stats_frame, text="Total Duration: Calculating...")
            self.lbl_total.grid(row=0, column=0, sticky="w", padx=5)
            
            self.lbl_elapsed = ttk.Label(stats_frame, text="Elapsed: 00:00:00")
            self.lbl_elapsed.grid(row=0, column=1, sticky="w", padx=5)
            
            self.lbl_remaining = ttk.Label(stats_frame, text="Remaining: Calculating...")
            self.lbl_remaining.grid(row=0, column=2, sticky="w", padx=5)

            # Status
            self.status_label = ttk.Label(main_frame, text="Starting...", foreground="blue")
            self.status_label.pack(pady=10)

            # Close Button (Initially Hidden)
            self.btn_close = ttk.Button(main_frame, text="Close (Space)", command=self.close_app)
        else:
            # Headless initialization
            print(f"🔥 Burning Subtitles: {os.path.basename(video_path)}")
            print(f"   Output: {os.path.basename(output_path)}")
            self.start_time = time.time()
            self.progress_var = None # No GUI var

        # Validate ASS
        self.validate_and_start()

    def validate_and_start(self):
        valid, msgs = validate_ass(self.ass_path)
        
        if not valid:
             # Critical errors
             if self.headless:
                  print(f"Error: Critical ASS errors found: {msgs}")
                  sys.exit(1)
             
             msg_text = "\n".join(msgs[:10])
             if len(msgs) > 10: msg_text += "\n..."
             messagebox.showerror("Subtitle Validation Error", f"Critical errors found in ASS file:\n{msg_text}")
             self.status_label.config(text="Validation Failed.", foreground="red")
             self.btn_close.pack(pady=10)
             return 

        if msgs:
             # Warnings
             if self.headless:
                  print(f"Warning: ASS validation warnings: {msgs}")
                  # Continue in headless
             else:
                  msg_text = "\n".join(msgs[:10])
                  if len(msgs) > 10: msg_text += "\n..."
                  if not messagebox.askyesno("Subtitle Validation Warning", f"Warnings found in ASS file:\n{msg_text}\n\nContinue burning?"):
                      self.status_label.config(text="Cancelled by user.", foreground="orange")
                      self.btn_close.pack(pady=10)
                      return
        
        # Start Process
        self.start_process()

    def start_process(self):
        self.is_running = True
        self.start_time = time.time()
        # Actually for headless we want blocking usually? Or threaded?
        # If we use Thread, we must join it or wait.
        # But existing logic uses Thread for GUI.
        
        if self.headless:
             self.run_ffmpeg()
        else:
             self.thread = threading.Thread(target=self.run_ffmpeg)
             self.thread.daemon = True
             self.thread.start()
             self.update_timer()

    def update_timer(self):
        if self.is_running:
            elapsed = time.time() - self.start_time
            if not self.headless:
                self.lbl_elapsed.config(text=f"Elapsed: {format_seconds(elapsed)}")
                self.root.after(1000, self.update_timer)

    def on_space(self, event):
        if self.finished:
            self.close_app()

    def close_app(self):
        self.root.destroy()
        sys.exit(0)

    def flash_window(self):
        """Flashes the window in the taskbar."""
        try:
            # FLASHW_ALL | FLASHW_TIMERNOFG = 3 | 12 = 14 (approx logic)
            # Actually standard FlashWindow is simpler?
            # FlashWindowEx structure is complex in ctypes without defs.
            # Simple approach: FlashWindow(hwnd, TRUE)
            
            # The correct handle is:
            # We need the HWND of the Tk window.
            # winfo_id might return it. 
            hwnd = self.root.winfo_id() 
            # Or use GetForegroundWindow if we are active? No.
            # ctypes.windll.user32.FlashWindow(hwnd, True) 
            
            # Better trick:
            # Use 'wm_frame' to get the outer window handle
            hwnd = int(self.root.wm_frame(), 16)
            ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception as e:
            print(f"Flash Error: {e}")

    def run_ffmpeg(self):
        out_abs_path = os.path.abspath(self.output_path)
        work_dir = os.path.dirname(out_abs_path)
        os.makedirs(work_dir, exist_ok=True)

        if os.path.exists(out_abs_path):
            try:
                os.remove(out_abs_path)
            except:
                self.update_status("Error: Output locked.", "red")
                return

        # Always make a temporary short file name to avoid FFmpeg filter parsing errors! 
        # (FFmpeg's -vf ass=... breaks on spaces, colons, and unicode strings).
        import uuid
        uid = uuid.uuid4().hex[:8]
        temp_ass_name = f"tmp_sub_{uid}.ass"
        temp_ass_path = os.path.join(work_dir, temp_ass_name)
        
        try:
            shutil.copy2(os.path.abspath(self.ass_path), temp_ass_path)
        except Exception as e:
            self.update_status(f"Error copying subtitle: {str(e)[:50]}...", "red")
            return

        encoder_name, encoder_opts = get_optimized_encoder(FFMPEG_PATH)

        cmd = [
            FFMPEG_PATH,
            "-y",
            "-i", os.path.abspath(self.video_path), 
            "-vf", f"ass={temp_ass_name}", # Safe, short, relative filename!
            "-c:a", "copy",
            "-c:v", encoder_name
        ]
        cmd.extend(encoder_opts)
        
        cmd.extend([
            "-sn", # Disable subtitle recording (prevent ghosting from input softsubs)
            os.path.abspath(self.output_path)
        ])

        self.temp_ass_path = temp_ass_path # Save for later cleanup
        self.update_status("Running FFmpeg...", "blue")
        
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        try:
            self.process = subprocess.Popen(
                cmd, 
                cwd=work_dir,
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True, 
                encoding='utf-8', 
                errors='ignore',
                startupinfo=startupinfo
            )
        except Exception as e:
            self.update_status(f"Launch Error: {e}", "red")
            if hasattr(self, 'temp_ass_path') and os.path.exists(self.temp_ass_path):
                try: os.remove(self.temp_ass_path)
                except: pass
            return

        time_pattern = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")
        duration_pattern = re.compile(r"Duration: (\d{2}:\d{2}:\d{2}\.\d{2})")

        error_log = []
        while True:
            line = self.process.stdout.readline()
            if not line and self.process.poll() is not None:
                break
            if line:
                error_log.append(line.strip())
                # Keep error log relatively small
                if len(error_log) > 50:
                    error_log.pop(0)

                if self.total_duration_sec == 0.0:
                    dur_match = duration_pattern.search(line)
                    if dur_match:
                        self.total_duration_sec = parse_time_str(dur_match.group(1))
                        if not self.headless:
                            self.root.after(0, lambda: self.lbl_total.config(text=f"Total: {format_seconds(self.total_duration_sec)}"))
                        else:
                            print(f"   Total Duration: {format_seconds(self.total_duration_sec)}")

                time_match = time_pattern.search(line)
                if time_match and self.total_duration_sec > 0:
                    current_time_sec = parse_time_str(time_match.group(1))
                    percentage = (current_time_sec / self.total_duration_sec) * 100
                    
                    elapsed = time.time() - self.start_time
                    if percentage > 0:
                        eta_sec = (elapsed / percentage) * 100 - elapsed
                        eta_str = format_seconds(eta_sec)
                    else:
                        eta_str = "Calculating..."

                    if not self.headless:
                        self.root.after(0, lambda p=percentage, e=eta_str: self.update_progress(p, e))
                    else:
                        # Headless Mode - Print Progress to Stdout
                        print(f"Progress: {percentage:.1f}% (ETA: {eta_str})")
                        sys.stdout.flush()

        ret_code = self.process.poll()
        self.is_running = False

        if hasattr(self, 'temp_ass_path') and os.path.exists(self.temp_ass_path):
            try:
                os.remove(self.temp_ass_path)
            except:
                pass

        if ret_code == 0:
            self.finished = True
            self.update_status("Burning Completed! Press SPACE to close.", "green")
            if not self.headless:
                self.root.after(0, self.show_completion_ui)
        else:
            self.update_status(f"Error Code: {ret_code}", "red")
            if self.headless:
                print("\n".join(error_log)) # Dump ffmpeg errors!
            if not self.headless:
                self.root.after(0, lambda: messagebox.showerror("FFmpeg Error", f"FFmpeg exited with code {ret_code}.\nCheck console output if available."))

    def show_completion_ui(self):
        """Updates UI for completion: Shows Close button and Flashes window."""
        if self.root:
             self.btn_close.pack(pady=10)
             self.btn_close.focus_set()
             self.flash_window()

    def update_progress(self, percentage, eta):
        if not self.headless and self.progress_var:
             self.progress_var.set(percentage)
             self.lbl_remaining.config(text=f"Remaining: {eta}")

    def update_status(self, text, color):
        if self.root:
             self.root.after(0, lambda: self.status_label.config(text=text, foreground=color))

if __name__ == "__main__":
    if len(sys.argv) < 4:
        # Fallback for testing/debugging info
        print("Usage: python burn_engine.py <video> <ass> <output>")
        print("Missing arguments. Opening dummy window.")
        # sys.exit(1) # Commented out to allow import testing or dev
    
    video = sys.argv[1] if len(sys.argv) > 1 else "video.mp4"
    ass = sys.argv[2] if len(sys.argv) > 2 else "subs.ass"
    out = sys.argv[3] if len(sys.argv) > 3 else "out.mp4"

    if "--headless" in sys.argv:
         # Headless mode: No GUI
         app = BurnProgressApp(None, video, ass, out, headless=True)
         # In headless mode, start_process calls run_ffmpeg synchronously
         if not app.finished:
             sys.exit(1)
    else:
         root = tk.Tk()
         app = BurnProgressApp(root, video, ass, out)
         root.mainloop()
