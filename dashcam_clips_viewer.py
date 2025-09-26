import os
import re
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

# 3rd-party
import cv2
from PIL import Image, ImageTk

# ---------- Helpers ----------

def natural_keys(text):
    """Natural sort so file2 < file10."""
    def atoi(tok):
        return int(tok) if tok.isdigit() else tok.lower()
    return [atoi(c) for c in re.split(r'(\d+)', text)]

def clamp(val, lo, hi):
    return max(lo, min(hi, val))

# ---------- Player App ----------

class FolderVideoPlayer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Folder Video Player (Mini)")

        # --- state ---
        self.files = []                 # list of dicts per clip
        self.total_frames = 0
        self.total_seconds = 0.0
        self.cap = None                 # current cv2.VideoCapture
        self.cur_file_idx = -1
        self.cur_global_frame = 0
        self.playing = False
        self.seeking = False            # when user drags the slider
        self.max_display_width = 960    # simple fit; adjust as you like
        self.loop_var = tk.BooleanVar(value=True)

        # playback speed
        self.play_speed = 1.0
        self.speed_choices = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]

        # --- UI layout ---
        self._build_ui()

        # Bind keys
        self.bind("<space>", lambda e: self.toggle_play())
        self.bind("<Left>", lambda e: self.step(-1))
        self.bind("<Right>", lambda e: self.step(+1))
        self.bind("[", lambda e: self._nudge_speed(-1))
        self.bind("]", lambda e: self._nudge_speed(+1))

        # Start idle loop (timer driving playback)
        self.after(10, self._timer_tick)

    # ---------- UI ----------

    def _build_ui(self):
        # Top row: folder chooser
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="Folder:").pack(side="left")
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(top, textvariable=self.folder_var, width=60)
        self.folder_entry.pack(side="left", padx=6)
        ttk.Button(top, text="Browse…", command=self.choose_folder).pack(side="left", padx=4)

        # Middle row: controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=8, pady=4)

        self.play_btn = ttk.Button(ctrl, text="Play", command=self.toggle_play, width=8)
        self.play_btn.pack(side="left")

        ttk.Button(ctrl, text="⟨⟨ -1", command=lambda: self.step(-1), width=6).pack(side="left", padx=4)
        ttk.Button(ctrl, text="+1 ⟩⟩", command=lambda: self.step(+1), width=6).pack(side="left", padx=2)

        ttk.Checkbutton(ctrl, text="Loop", variable=self.loop_var).pack(side="left", padx=10)

        # Speed selector
        sp = ttk.Frame(ctrl)
        sp.pack(side="left", padx=12)
        ttk.Label(sp, text="Speed").pack(side="left", padx=(0,4))
        self.speed_var = tk.StringVar(value="1.0×")
        self.speed_combo = ttk.Combobox(
            sp,
            values=[f"{s}×" for s in self.speed_choices],
            state="readonly",
            width=6,
            textvariable=self.speed_var
        )
        self.speed_combo.pack(side="left")
        self.speed_combo.bind("<<ComboboxSelected>>", self._on_speed_change)

        self.info_var = tk.StringVar(value="Open a folder to start")
        ttk.Label(ctrl, textvariable=self.info_var).pack(side="right")

        # Slider
        srow = ttk.Frame(self)
        srow.pack(fill="x", padx=8, pady=(0, 4))
        self.slider = ttk.Scale(srow, from_=0, to=0, orient="horizontal", command=self._on_slider_move)
        self.slider.pack(fill="x", expand=True)
        # Detect drag start/end for precise seek
        self.slider.bind("<ButtonPress-1>", self._on_slider_press)
        self.slider.bind("<ButtonRelease-1>", self._on_slider_release)

        # Video area
        self.video_label = ttk.Label(self, anchor="center", background="#000")
        self.video_label.pack(fill="both", expand=True, padx=8, pady=8)

        # Footer
        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).pack(fill="x", padx=8, pady=(0,6))

    # ---------- Playback speed ----------

    def _set_speed(self, speed: float):
        speed = float(speed)
        speed = clamp(speed, min(self.speed_choices), max(self.speed_choices))
        self.play_speed = speed
        self.speed_var.set(f"{speed}×")
        self.status_var.set(self._status_text())  # refresh footer

    def _nudge_speed(self, step: int):
        # step is +/-1 into the discrete choices list
        idx = min(range(len(self.speed_choices)),
                  key=lambda i: abs(self.speed_choices[i] - self.play_speed))
        idx = clamp(idx + step, 0, len(self.speed_choices)-1)
        self._set_speed(self.speed_choices[idx])

    def _on_speed_change(self, _):
        try:
            s = float(self.speed_var.get().rstrip("×"))
        except Exception:
            s = 1.0
        self._set_speed(s)

    # ---------- Folder & indexing ----------

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Select folder with MP4 clips")
        if not folder:
            return
        self.folder_var.set(folder)
        self._load_folder(Path(folder))

    def _load_folder(self, folder: Path):
        # Close previous capture
        if self.cap is not None:
            self.cap.release()
            self.cap = None

        # Find .mp4 / .MP4
        paths = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"]
        if not paths:
            messagebox.showerror("No videos", "No .mp4/.MP4 files found in that folder.")
            return

        paths.sort(key=lambda p: natural_keys(p.name))

        # Build metadata index
        files = []
        total_frames = 0
        total_secs = 0.0
        start_frame = 0
        start_time = 0.0

        for p in paths:
            cap = cv2.VideoCapture(str(p))
            if not cap.isOpened():
                cap.release()
                continue
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            if fps <= 1e-3:
                fps = 30.0  # fallback
            nframes = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cap.release()

            dur = (nframes / fps) if nframes > 0 else 0.0
            files.append(dict(
                path=str(p),
                fps=fps,
                frames=nframes,
                width=width,
                height=height,
                start_frame=start_frame,
                start_time=start_time
            ))
            start_frame += nframes
            start_time += dur
            total_frames += nframes
            total_secs += dur

        if total_frames == 0:
            messagebox.showerror("No frames", "Could not read frames from any file.")
            return

        self.files = files
        self.total_frames = total_frames
        self.total_seconds = total_secs

        # Reset playhead to start
        self.cur_file_idx = -1
        self.cur_global_frame = 0
        self._open_capture_for_global_frame(0)
        self._display_current_frame()  # show first frame

        # Configure slider
        self.slider.config(from_=0, to=max(0, self.total_frames - 1))
        self.slider.set(0)

        self.info_var.set(f"{len(self.files)} clips | {self.total_frames} frames | {self._fmt_time(self.total_seconds)} total")
        self.status_var.set(self._status_text())

    # ---------- Mapping & capture ----------

    def _global_to_file_local(self, gframe: int):
        """Return (file_idx, local_frame) for a global frame index."""
        gframe = clamp(gframe, 0, max(0, self.total_frames - 1))
        # Binary search would be faster; linear is fine for dozens/hundreds of clips
        for i, f in enumerate(self.files):
            start = f["start_frame"]
            end = start + f["frames"]  # exclusive
            if gframe < end:
                return i, gframe - start
        # Fallback to last frame
        last = len(self.files) - 1
        return last, max(0, self.files[last]["frames"] - 1)

    def _open_capture_for_file(self, idx: int):
        if idx == self.cur_file_idx:
            return
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.files[idx]["path"])
        self.cur_file_idx = idx

    def _open_capture_for_global_frame(self, gframe: int):
        idx, local = self._global_to_file_local(gframe)
        self._open_capture_for_file(idx)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, float(local))

    # ---------- Playback core ----------

    def toggle_play(self):
        self.playing = not self.playing
        self.play_btn.config(text="Pause" if self.playing else "Play")

    def step(self, delta: int):
        if not self.files:
            return
        self.playing = False
        self.play_btn.config(text="Play")
        new_frame = clamp(self.cur_global_frame + delta, 0, self.total_frames - 1)
        self.seek_to(new_frame)

    def seek_to(self, gframe: int):
        if not self.files:
            return
        self.cur_global_frame = clamp(gframe, 0, self.total_frames - 1)
        self._open_capture_for_global_frame(self.cur_global_frame)
        self._display_current_frame()
        if not self.seeking:
            self.slider.set(self.cur_global_frame)
        self.status_var.set(self._status_text())

    def _timer_tick(self):
        """Runs periodically; if playing, read next frame and schedule based on fps and speed."""
        delay = 33  # default ~30 fps
        if self.files and self.playing:
            # Ensure capture points to correct clip/position
            file_idx, local_frame = self._global_to_file_local(self.cur_global_frame)
            self._open_capture_for_file(file_idx)
            # If our cap position drifted, snap it (cheap check)
            cap_pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
            if abs(cap_pos - local_frame) > 1:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, float(local_frame))
            ok, frame = self.cap.read()
            if not ok:
                # End of current file -> advance to next
                if file_idx + 1 < len(self.files):
                    self._open_capture_for_file(file_idx + 1)
                    self.cur_global_frame = self.files[file_idx + 1]["start_frame"]
                    ok, frame = self.cap.read()
                else:
                    # Reached the very end
                    if self.loop_var.get():
                        self.seek_to(0)
                        ok, frame = self.cap.read()
                    else:
                        self.playing = False
                        self.play_btn.config(text="Play")
                        self.after(delay, self._timer_tick)
                        return

            if ok:
                self._show_frame(frame)
                self.cur_global_frame = clamp(self.cur_global_frame + 1, 0, self.total_frames - 1)
                if not self.seeking:
                    self.slider.set(self.cur_global_frame)
                self.status_var.set(self._status_text())

            # Next delay according to current clip fps AND speed
            fps = self.files[self.cur_file_idx]["fps"] if self.files else 30.0
            eff_fps = max(1.0, fps * max(0.01, float(self.play_speed)))
            delay = int(max(1, round(1000.0 / eff_fps)))

        self.after(delay, self._timer_tick)

    # ---------- Display ----------

    def _show_frame(self, bgr):
        # Convert to RGB and scale to fit width
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        if w > self.max_display_width:
            scale = self.max_display_width / float(w)
            new_size = (int(w * scale), int(h * scale))
            rgb = cv2.resize(rgb, new_size, interpolation=cv2.INTER_AREA)
        img = Image.fromarray(rgb)
        self._tk_img = ImageTk.PhotoImage(img)  # keep a ref
        self.video_label.configure(image=self._tk_img)

    def _display_current_frame(self):
        # Read the current frame without advancing global frame
        idx, local = self._global_to_file_local(self.cur_global_frame)
        self._open_capture_for_file(idx)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, float(local))
        ok, frame = self.cap.read()
        if ok:
            # reading advanced by 1; keep global pointer consistent with what we show
            self._show_frame(frame)
        else:
            # Try a second time (some containers need a nudge)
            ok2, frame2 = self.cap.read()
            if ok2:
                self._show_frame(frame2)

    # ---------- Slider callbacks ----------

    def _on_slider_press(self, _):
        self.seeking = True

    def _on_slider_release(self, _):
        # Seek to slider position
        target = int(self.slider.get())
        self.seeking = False
        self.seek_to(target)

    def _on_slider_move(self, _val):
        # While dragging, just update the status text (cheap).
        if self.seeking and self.files:
            target = int(float(_val))
            file_idx, local = self._global_to_file_local(target)
            # Approximate time: start_time + local/fps
            t = self.files[file_idx]["start_time"] + (local / max(1.0, self.files[file_idx]["fps"]))
            self.status_var.set(self._status_text(frame=target, time_sec=t))

    # ---------- Status formatting ----------

    def _fmt_time(self, sec: float) -> str:
        sec = max(0.0, float(sec))
        h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}" if h > 0 else f"{m:02d}:{s:05.2f}"

    def _status_text(self, frame=None, time_sec=None) -> str:
        if not self.files:
            return ""
        if frame is None:
            frame = self.cur_global_frame
        if time_sec is None:
            i, local = self._global_to_file_local(frame)
            time_sec = self.files[i]["start_time"] + local / max(1.0, self.files[i]["fps"])
        return (
            f"Frame {frame+1:,} / {self.total_frames:,}   |   "
            f"{self._fmt_time(time_sec)} / {self._fmt_time(self.total_seconds)}   |   "
            f"{self.play_speed}×"
        )

# ---------- main ----------

if __name__ == "__main__":
    app = FolderVideoPlayer()
    # Optional: start maximized on Windows
    try:
        app.state('zoomed')
    except Exception:
        pass
    app.mainloop()
