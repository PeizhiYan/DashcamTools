import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading  # NEW
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

# --------- Utilities ---------

def check_ffmpeg():
    """Return True if ffmpeg is available on PATH (ffprobe comes with it)."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

def natural_keys(text):
    def atoi(tok):
        return int(tok) if tok.isdigit() else tok.lower()
    return [atoi(c) for c in re.split(r'(\d+)', text)]

def build_concat_listfile(files):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for f in files:
            p = Path(f).resolve().as_posix().replace("'", r"'\''")
            tmp.write(f"file '{p}'\n")
    finally:
        tmp.close()
    return tmp.name

def has_audio_stream(sample_file):
    try:
        cmd = ["ffprobe","-v","error","-select_streams","a","-show_entries","stream=codec_type","-of","csv=p=0",str(sample_file)]
        out = subprocess.run(cmd, capture_output=True, text=True)
        return "audio" in out.stdout.lower()
    except Exception:
        return True

def get_duration_seconds(path):
    try:
        cmd = ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)]
        out = subprocess.run(cmd, capture_output=True, text=True)
        return max(0.0, float(out.stdout.strip()))
    except Exception:
        return 0.0

def hms_from_seconds(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}" if h>0 else f"{m:02d}:{s:05.2f}"

def _build_atempo_chain(factor):
    steps = []
    if factor <= 0: return steps
    while factor < 0.5:
        steps.append(0.5); factor /= 0.5
    while factor > 2.0:
        steps.append(2.0); factor /= 2.0
    if abs(factor - 1.0) > 1e-6:
        steps.append(factor)
    return steps

def run_ffmpeg_concat(
    listfile, user_fps, out_path,
    log_callback=None, progress_callback=None,
    speed_mode=True, base_output_fps=60,
    keep_audio=True, audio_present=True
):
    """
    Reads -progress from stdout and calls progress_callback(seconds_done).
    Runs in the background thread; callbacks should be thread-safe.
    """
    common = [
        "ffmpeg","-hide_banner","-y",
        "-f","concat","-safe","0","-i",listfile,
        "-pix_fmt","yuv420p","-c:v","libx264","-preset","slow","-crf","22",
        "-movflags","+faststart","-progress","pipe:1","-nostats"
    ]

    if speed_mode:
        speed = float(base_output_fps)/float(user_fps)
        vfilter = f"setpts=PTS/{speed:g}"
        cmd = common + ["-vf", vfilter, "-r", f"{base_output_fps}"]
        if keep_audio and audio_present:
            steps = _build_atempo_chain(speed)
            if steps:
                cmd += ["-filter:a", ",".join(f"atempo={s:.6g}" for s in steps), "-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += [out_path]
    else:
        cmd = common + ["-vf", f"fps={user_fps}", "-r", f"{user_fps}"]
        if keep_audio and audio_present:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += [out_path]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, bufsize=1, encoding="utf-8", errors="replace"
    )

    for raw in proc.stdout:
        line = (raw or "").strip()
        if not line: continue
        if line.startswith("out_time_ms="):
            try:
                us = int(line.split("=",1)[1].strip())
                secs = us/1_000_000.0
                if progress_callback: progress_callback(secs)
            except Exception: pass
            continue
        if line.startswith("out_time="):
            try:
                t = line.split("=",1)[1].strip()
                hh,mm,ss = t.split(":")
                secs = int(hh)*3600 + int(mm)*60 + float(ss)
                if progress_callback: progress_callback(secs)
            except Exception: pass
            continue
        if line.startswith("progress="):
            continue
        if log_callback: log_callback(line)

    return proc.wait()

# --------- GUI ---------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dashcam MP4 Concatenator")
        self.geometry("820x640")

        self.folder_var = tk.StringVar()
        self.outfile_var = tk.StringVar()
        self.fps_var = tk.StringVar(value="60")
        self.speed_mode_var = tk.BooleanVar(value=True)
        self.keep_audio_var = tk.BooleanVar(value=True)
        self.base_out_fps = 60
        self.total_duration = 0.0

        row = 0
        tk.Label(self, text="Folder with MP4 clips:").grid(row=row, column=0, sticky="w", padx=10, pady=(12,4))
        tk.Entry(self, textvariable=self.folder_var, width=70).grid(row=row, column=1, padx=6, pady=(12,4), sticky="we")
        tk.Button(self, text="Browse…", command=self.pick_folder).grid(row=row, column=2, padx=10, pady=(12,4))
        row += 1
        tk.Label(self, text="Output video file:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        tk.Entry(self, textvariable=self.outfile_var, width=70).grid(row=row, column=1, padx=6, pady=4, sticky="we")
        tk.Button(self, text="Save as…", command=self.pick_outfile).grid(row=row, column=2, padx=10, pady=4)
        row += 1
        tk.Label(self, text="FPS field:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        tk.Entry(self, textvariable=self.fps_var, width=12).grid(row=row, column=1, sticky="w", padx=6, pady=4)
        tk.Label(self, text="Enter e.g. 20 for 3× fast-forward (output stays 60 fps in speed mode).").grid(row=row, column=1, sticky="w", padx=120, pady=4)
        row += 1
        tk.Checkbutton(self, text="Keep output at 60 fps and adjust speed (fast-forward/slow-mo)", variable=self.speed_mode_var).grid(row=row, column=1, sticky="w", padx=6, pady=(0,6))
        row += 1
        tk.Checkbutton(self, text="Keep audio", variable=self.keep_audio_var).grid(row=row, column=1, sticky="w", padx=6, pady=(0,10))
        row += 1
        self.start_btn = tk.Button(self, text="Concatenate", command=self.start, width=16)
        self.start_btn.grid(row=row, column=0, padx=10, pady=10, sticky="w")
        self.quit_btn = tk.Button(self, text="Quit", command=self.destroy, width=10)
        self.quit_btn.grid(row=row, column=2, padx=10, pady=10, sticky="e")
        row += 1
        tk.Label(self, text="Progress:").grid(row=row, column=0, sticky="w", padx=10)
        self.prog = ttk.Progressbar(self, orient="horizontal", mode="determinate", length=400)
        self.prog.grid(row=row, column=1, sticky="we", padx=6)
        self.prog_label = tk.Label(self, text="0%")
        self.prog_label.grid(row=row, column=2, sticky="w", padx=6)
        row += 1
        tk.Label(self, text="Log:").grid(row=row, column=0, sticky="w", padx=10)
        row += 1
        self.log = tk.Text(self, height=18, wrap="word")
        self.log.grid(row=row, column=0, columnspan=3, padx=10, pady=(0,10), sticky="nsew")
        self.scroll = tk.Scrollbar(self, command=self.log.yview)
        self.scroll.grid(row=row, column=3, sticky="ns", pady=(0,10))
        self.log["yscrollcommand"] = self.scroll.set
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(row, weight=1)

        if not check_ffmpeg():
            messagebox.showwarning(
                "ffmpeg/ffprobe not found",
                "ffmpeg (and ffprobe) are not on your PATH.\n\nInstall ffmpeg and try again.\n"
                "On Windows: choco install ffmpeg\nOn macOS: brew install ffmpeg\nOn Linux: sudo apt install ffmpeg"
            )

    def pick_folder(self):
        folder = filedialog.askdirectory(title="Select folder with MP4 clips")
        if folder:
            self.folder_var.set(folder)
            suggested = Path(folder) / "concatenated_output.mp4"
            if not self.outfile_var.get():
                self.outfile_var.set(str(suggested))

    def pick_outfile(self):
        path = filedialog.asksaveasfilename(
            title="Save output video as",
            defaultextension=".mp4",
            initialfile="concatenated_output.mp4",
            filetypes=[("MP4 video","*.mp4"),("All files","*.*")]
        )
        if path:
            self.outfile_var.set(path)

    def append_log(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _set_progress_total(self, seconds_total):
        self.total_duration = max(0.0, float(seconds_total))
        if self.total_duration > 0:
            self.prog.config(mode="determinate", maximum=100.0, value=0.0)
            self.prog_label.config(text=f"0% (0:00 / {hms_from_seconds(self.total_duration)})")
        else:
            self.prog.config(mode="indeterminate")
            self.prog.start(50)
            self.prog_label.config(text="Working…")

    def _on_progress_seconds(self, seconds_done):
        if self.total_duration > 0:
            pct = min(100.0, (seconds_done / self.total_duration) * 100.0)
            self.prog["value"] = pct
            self.prog_label.config(text=f"{pct:0.1f}% ({hms_from_seconds(seconds_done)} / {hms_from_seconds(self.total_duration)})")

    def start(self):
        folder = self.folder_var.get().strip()
        outfile = self.outfile_var.get().strip()
        fps_str = self.fps_var.get().strip() or "60"
        speed_mode = self.speed_mode_var.get()
        keep_audio = self.keep_audio_var.get()

        if not folder:
            messagebox.showerror("Missing folder","Please select the folder containing your MP4 clips."); return
        if not Path(folder).is_dir():
            messagebox.showerror("Invalid folder","That path is not a folder."); return
        if not outfile:
            messagebox.showerror("Missing output","Please choose an output file path."); return
        try:
            user_fps = float(fps_str); assert user_fps > 0
        except Exception:
            messagebox.showerror("Invalid FPS","Please enter a positive number (e.g., 60 or 20)."); return
        if not check_ffmpeg():
            messagebox.showerror("ffmpeg not found","ffmpeg/ffprobe are not on your PATH. Please install and try again."); return

        files = sorted(
            [str(p) for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower()==".mp4"],
            key=lambda p: natural_keys(Path(p).name)
        )
        if not files:
            messagebox.showerror("No MP4 files","No .mp4/.MP4 files found in the selected folder."); return

        # Total duration for progress (adjust for speed mode)
        self.append_log("Scanning durations for progress…")
        total_sec = sum(get_duration_seconds(f) for f in files)
        if speed_mode:
            speed = self.base_out_fps / user_fps
            effective_total = total_sec / speed if speed > 0 else total_sec
        else:
            effective_total = total_sec
        self._set_progress_total(effective_total)

        audio_present = has_audio_stream(files[0])

        self.append_log(f"Found {len(files)} MP4 files.")
        self.append_log("Creating ffmpeg concat list…")
        listfile = build_concat_listfile(files)

        # Disable buttons during work
        self.start_btn.config(state="disabled")
        self.quit_btn.config(state="disabled")

        if speed_mode:
            if (self.base_out_fps / user_fps) >= 1.0:
                self.append_log(f"Speed mode ON: fast-forward by {(self.base_out_fps/user_fps):.3g}×, output {self.base_out_fps} fps.")
            else:
                self.append_log(f"Speed mode ON: slow-motion by {(user_fps/self.base_out_fps):.3g}×, output {self.base_out_fps} fps.")
        else:
            self.append_log(f"Re-encode mode: output frame rate = {user_fps:g} fps.")
        self.append_log(f"Audio: {'kept' if keep_audio and audio_present else 'disabled'}")
        self.append_log(f"Output: {outfile}")
        self.append_log("Running ffmpeg…")

        def after_done(returncode, listfile_path):
            if self.total_duration <= 0:
                self.prog.stop()
            else:
                self.prog["value"] = 100.0
                self.prog_label.config(text=f"100% ({hms_from_seconds(self.total_duration)} / {hms_from_seconds(self.total_duration)})")
            try: os.remove(listfile_path)
            except Exception: pass
            self.start_btn.config(state="normal")
            self.quit_btn.config(state="normal")
            if returncode == 0:
                self.append_log("✅ Done!")
                messagebox.showinfo("Success", f"Finished!\nSaved to:\n{outfile}")
            else:
                self.append_log(f"❌ ffmpeg exited with code {returncode}. See log above.")
                messagebox.showerror("Failed","ffmpeg reported an error. Check the log for details.")

        def worker():
            try:
                rc = run_ffmpeg_concat(
                    listfile=listfile,
                    user_fps=user_fps,
                    out_path=outfile,
                    # IMPORTANT: marshal callbacks to Tk thread
                    log_callback=lambda line: self.after(0, self.append_log, line),
                    progress_callback=lambda s: self.after(0, self._on_progress_seconds, s),
                    speed_mode=speed_mode,
                    base_output_fps=self.base_out_fps,
                    keep_audio=keep_audio,
                    audio_present=audio_present
                )
            except Exception as e:
                rc = 1
                self.after(0, self.append_log, f"Error: {e}")
            # Finish on the Tk thread
            self.after(0, after_done, rc, listfile)

        # Start background thread (don’t block Tk mainloop)
        threading.Thread(target=worker, daemon=True).start()

# --------- Entry point ---------

if __name__ == "__main__":
    if getattr(sys, 'frozen', False):
        os.environ["PATH"] = os.environ.get("PATH","")
    app = App()
    app.mainloop()
