import os
import time
import threading
import queue
import hashlib
import shutil
import logging
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "FiveM MLO Cleaner"
APP_TAGLINE = "(Futuristic Dark Edition)"
CREATED_BY = "created by Leutnant"

DEFAULT_KEEP_EXTS = {
    ".ymap", ".ytyp",
    ".ydr", ".ytd", ".yft", ".ycd", ".ynv", ".ypt",
    ".awc", ".gxt2",
    ".meta",
    ".dat10", ".dat54", ".dat151",
}

def is_occlusion_name(p: Path) -> bool:
    n = p.name.lower()
    return ("occl" in n) or ("occlusion" in n)

def sha1_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def iter_files(root: Path):
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            yield Path(dirpath) / fn

def write_fxmanifest(dest_root: Path, resource_name: str):
    content = f"""fx_version 'cerulean'
game 'gta5'

author '{resource_name}'
description 'Auto-cleaned MLO resource (ymap/ytyp/models/textures only)'
version '1.0.0'

this_is_a_map 'yes'

files {{
  'stream/*'
}}
"""
    (dest_root / "fxmanifest.lua").write_text(content, encoding="utf-8")

def should_keep_file(p: Path, keep_exts: set[str], include_ybn: bool):
    """
    Returns (keep: bool, reason: str)
    """
    if p.is_dir():
        return False, "is_directory"

    ext = p.suffix.lower()

    if is_occlusion_name(p):
        return False, "occlusion_file_name"

    if ext == ".ybn" and not include_ybn:
        return False, "ybn_disabled"

    if ext in keep_exts or (include_ybn and ext == ".ybn"):
        return True, "kept"
    return False, f"extension_not_allowed:{ext or '<none>'}"

class CleanerWorker:
    def __init__(self, log_q: queue.Queue, stop_event: threading.Event, logger: logging.Logger):
        self.log_q = log_q
        self.stop_event = stop_event
        self.logger = logger

    def ui_log(self, msg: str):
        self.log_q.put(msg)

    def clean_one(
        self,
        src: Path,
        out_dir: Path,
        keep_exts: set[str],
        include_ybn: bool,
        make_fxmanifest: bool,
        flatten_stream: bool,
        dedupe: bool,
        progress_cb,
        progress_range: tuple[int, int] | None = None,  # (start, end) for multi-run global progress
    ):
        src = src.resolve()
        if not src.exists() or not src.is_dir():
            raise FileNotFoundError(f"Source folder not found: {src}")

        resource_name = src.name
        dest_root = out_dir / f"{resource_name}_fivem_clean"
        stream_dir = dest_root / "stream"
        stream_dir.mkdir(parents=True, exist_ok=True)

        if make_fxmanifest:
            write_fxmanifest(dest_root, resource_name=resource_name)

        files = list(iter_files(src))
        total = max(1, len(files))

        copied = 0
        skipped = 0
        errors = 0

        seen_hashes: dict[str, Path] = {}

        self.ui_log(f"\n=== START: {resource_name} ===")
        self.ui_log(f"Scanning: {src}")
        self.ui_log(f"Output:  {dest_root}")
        self.logger.info("START %s | src=%s out=%s", resource_name, src, dest_root)
        self.logger.info("Options: include_ybn=%s make_fxmanifest=%s flatten_stream=%s dedupe=%s",
                         include_ybn, make_fxmanifest, flatten_stream, dedupe)

        for i, f in enumerate(files, start=1):
            if self.stop_event.is_set():
                self.ui_log("!! STOP requested. Aborting current job.")
                self.logger.warning("STOP requested. Aborting job: %s", resource_name)
                break

            keep, reason = should_keep_file(f, keep_exts, include_ybn)
            if not keep:
                skipped += 1
                self.logger.debug("SKIP: %s | reason=%s", f, reason)
                self._update_progress(progress_cb, i, total, progress_range)
                continue

            try:
                if flatten_stream:
                    dest_path = stream_dir / f.name
                else:
                    rel = f.relative_to(src)
                    dest_path = stream_dir / rel

                dest_path.parent.mkdir(parents=True, exist_ok=True)

                if dedupe:
                    h = sha1_file(f)
                    if h in seen_hashes:
                        skipped += 1
                        self.ui_log(f"[DEDUPED] {resource_name}: {f.name} (same as {seen_hashes[h].name})")
                        self.logger.info("DEDUPED: %s | same_as=%s", f, seen_hashes[h])
                        self._update_progress(progress_cb, i, total, progress_range)
                        continue
                    seen_hashes[h] = dest_path

                if dest_path.exists():
                    try:
                        if sha1_file(dest_path) == sha1_file(f):
                            skipped += 1
                            self.ui_log(f"[SKIP-SAME] {resource_name}: {f.name}")
                            self.logger.info("SKIP-SAME: %s already identical", f)
                            self._update_progress(progress_cb, i, total, progress_range)
                            continue
                    except Exception as e:
                        self.logger.warning("Hash compare failed for %s: %s", f, e)

                    stem, ext = dest_path.stem, dest_path.suffix
                    n = 2
                    while True:
                        alt = dest_path.with_name(f"{stem}_{n}{ext}")
                        if not alt.exists():
                            dest_path = alt
                            break
                        n += 1
                    self.logger.info("NAME-CONFLICT: %s -> %s", f, dest_path)

                shutil.copy2(f, dest_path)
                copied += 1
                self.ui_log(f"[COPY] {resource_name}: {f.name} -> {dest_path.relative_to(dest_root)}")
                self.logger.info("COPY: %s -> %s", f, dest_path)

            except Exception as e:
                errors += 1
                self.ui_log(f"[ERROR] {resource_name}: {f.name} | {e}")
                self.logger.exception("ERROR copying %s: %s", f, e)

            self._update_progress(progress_cb, i, total, progress_range)

        self.ui_log(f"=== DONE: {resource_name} | Copied: {copied} | Skipped: {skipped} | Errors: {errors} ===")
        self.logger.info("DONE %s | copied=%d skipped=%d errors=%d", resource_name, copied, skipped, errors)
        return copied, skipped, errors, dest_root

    def _update_progress(self, progress_cb, i, total, progress_range):
        if not progress_range:
            progress_cb(i, total)
            return
        start, end = progress_range
        # Map [0..total] to [start..end]
        frac = i / total
        val = int(start + (end - start) * frac)
        progress_cb(val, 100)

# ----------------- Futuristic Fire Progress Bar -----------------

class FireBar(ttk.Frame):
    def __init__(self, master, width=760, height=24, **kwargs):
        super().__init__(master, **kwargs)
        self.w = width
        self.h = height
        self.value = 0
        self.maximum = 100
        self._t = 0.0

        self.canvas = tk.Canvas(self, width=self.w, height=self.h, highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", expand=True)

        self._bg = "#12131a"
        self._frame = "#00ffd5"
        self._glow = "#8a2be2"
        self._text = "#e8e8ff"

        self._draw_static()
        self.after(40, self._animate)

    def _draw_static(self):
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, self.w, self.h, outline=self._glow, width=2)
        self.canvas.create_rectangle(2, 2, self.w-2, self.h-2, outline=self._frame, width=2)
        self.canvas.create_rectangle(4, 4, self.w-4, self.h-4, outline="", fill=self._bg, tags=("bg",))
        self.canvas.create_text(self.w-10, self.h/2, text="0%", fill=self._text, anchor="e",
                                font=("Segoe UI", 9, "bold"), tags=("pct",))
        self.canvas.create_text(10, self.h/2, text="🔥", fill=self._text, anchor="w",
                                font=("Segoe UI Emoji", 11), tags=("flame",))

    def set(self, value, maximum=None):
        if maximum is not None and maximum > 0:
            self.maximum = maximum
        self.value = max(0, min(value, self.maximum))

    def _animate(self):
        self._t += 0.12
        self._render()
        self.after(40, self._animate)

    def _render(self):
        import math
        self.canvas.delete("fill")
        self.canvas.delete("embers")

        inner_w = self.w - 8
        inner_h = self.h - 8
        x0, y0 = 4, 4

        frac = 0 if self.maximum <= 0 else (self.value / self.maximum)
        fill_w = int(inner_w * frac)

        stripes = 18
        if fill_w > 0:
            for s in range(stripes):
                sx0 = x0 + int(fill_w * s / stripes)
                sx1 = x0 + int(fill_w * (s+1) / stripes)
                flick = int(1 + 2 * (0.5 + 0.5 * (1 + math.sin(self._t + s*0.6))))
                if s < stripes*0.55:
                    col = "#ff3b2f"
                elif s < stripes*0.85:
                    col = "#ffb300"
                else:
                    col = "#fff2a6"
                self.canvas.create_rectangle(sx0, y0+flick, sx1, y0+inner_h-flick,
                                             outline="", fill=col, tags=("fill",))

            end_x = x0 + fill_w
            for k in range(10):
                dy = int((k * 7 + int(8 * math.sin(self._t + k))) % inner_h)
                self.canvas.create_oval(end_x-6, y0+dy, end_x-2, y0+dy+4,
                                        outline="", fill="#ff6a00", tags=("embers",))

        pct = int(frac * 100)
        self.canvas.itemconfigure("pct", text=f"{pct}%")

        flame_x = x0 + max(10, min(fill_w, inner_w-10))
        self.canvas.coords("flame", flame_x, self.h/2)

# ----------------- App -----------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_TAGLINE}")
        self.geometry("1040x720")
        self.minsize(980, 650)

        self.bg = "#0d0f14"
        self.panel = "#12131a"
        self.panel2 = "#171a22"
        self.text = "#e8e8ff"
        self.muted = "#aab0c0"
        self.neon = "#00ffd5"
        self.neon2 = "#8a2be2"
        self.err = "#ff3b2f"

        self.configure(bg=self.bg)

        # If icon.png exists at runtime, set window icon (optional)
        try:
            icon_png = Path(getattr(sys, "_MEIPASS", Path.cwd())) / "icon.png"
            if icon_png.exists():
                self._icon_img = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

        self.log_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread = None

        self.out_var = tk.StringVar(value=str((Path.cwd() / "out_clean").resolve()))

        self.include_ybn = tk.BooleanVar(value=False)
        self.make_fxmanifest = tk.BooleanVar(value=True)
        self.flatten_stream = tk.BooleanVar(value=True)
        self.dedupe = tk.BooleanVar(value=False)

        self.sources: list[Path] = []

        self.logger = None
        self.logfile_path = None

        self._style_ttk()
        self._build_ui()
        self.after(80, self._drain_log_queue)

    def _style_ttk(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background=self.bg)
        style.configure("TLabel", background=self.bg, foreground=self.text, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=self.bg, foreground=self.neon, font=("Segoe UI", 14, "bold"))
        style.configure("Sub.TLabel", background=self.bg, foreground=self.muted, font=("Segoe UI", 9))
        style.configure("RightTag.TLabel", background=self.bg, foreground=self.neon2, font=("Segoe UI", 9, "bold"))

        style.configure("TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Neon.TButton", font=("Segoe UI", 10, "bold"),
                        foreground=self.bg, background=self.neon)
        style.map("Neon.TButton",
                  background=[("active", self.neon2)],
                  foreground=[("active", self.text)])

        style.configure("Danger.TButton", font=("Segoe UI", 10, "bold"),
                        foreground=self.text, background=self.err)

        style.configure("TEntry", fieldbackground=self.panel2, background=self.panel2, foreground=self.text)
        style.configure("TLabelframe", background=self.panel, foreground=self.neon)
        style.configure("TLabelframe.Label", background=self.panel, foreground=self.neon, font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        pad = {"padx": 12, "pady": 10}

        header = ttk.Frame(self)
        header.pack(fill="x", **pad)

        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)

        ttk.Label(left, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Queue multiple MLO folders, clean for FiveM, and log every decision.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        ttk.Label(header, text=CREATED_BY, style="RightTag.TLabel").pack(side="right", anchor="ne")

        # Sources panel
        srcf = ttk.Labelframe(self, text="MLO folders (queue)", style="TLabelframe")
        srcf.pack(fill="both", expand=False, **pad)

        row = ttk.Frame(srcf)
        row.pack(fill="x", padx=10, pady=10)

        ttk.Button(row, text="Add MLO Folder…", command=self.add_source, style="Neon.TButton").pack(side="left")
        ttk.Button(row, text="Remove Selected", command=self.remove_selected, style="Danger.TButton").pack(side="left", padx=10)
        ttk.Button(row, text="Clear", command=self.clear_sources).pack(side="left", padx=10)

        self.listbox = tk.Listbox(
            srcf, height=6,
            bg=self.panel2, fg=self.text,
            selectbackground=self.neon2,
            highlightthickness=0, relief="flat",
            font=("Consolas", 10)
        )
        self.listbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Output + options
        paths = ttk.Labelframe(self, text="Output & Options", style="TLabelframe")
        paths.pack(fill="x", **pad)

        ttk.Label(paths, text="Output folder:").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        ttk.Entry(paths, textvariable=self.out_var, width=80).grid(row=0, column=1, sticky="we", padx=10, pady=8)
        ttk.Button(paths, text="Browse…", command=self.pick_out, style="Neon.TButton").grid(row=0, column=2, padx=10, pady=8)

        opt_row = ttk.Frame(paths)
        opt_row.grid(row=1, column=0, columnspan=3, sticky="we", padx=10, pady=(0, 10))

        ttk.Checkbutton(opt_row, text="Keep .ybn collision (NOT recommended)", variable=self.include_ybn).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opt_row, text="Create fxmanifest.lua", variable=self.make_fxmanifest).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opt_row, text="Flatten into stream/", variable=self.flatten_stream).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opt_row, text="Dedupe identical files (SHA1)", variable=self.dedupe).pack(side="left")

        paths.columnconfigure(1, weight=1)

        # Actions + firebar
        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)

        self.start_btn = ttk.Button(actions, text="Start Queue", command=self.start, style="Neon.TButton")
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, style="Danger.TButton", state="disabled")
        self.stop_btn.pack(side="left", padx=10)

        ttk.Button(actions, text="Open Output Folder", command=self.open_output).pack(side="left", padx=10)

        self.firebar = FireBar(actions, width=520, height=24)
        self.firebar.pack(side="right", padx=10, fill="x", expand=True)

        # Log
        logf = ttk.Labelframe(self, text="Live Log (also saved to logfile)", style="TLabelframe")
        logf.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(
            logf,
            height=18,
            wrap="none",
            bg=self.panel2,
            fg=self.text,
            insertbackground=self.neon,
            relief="flat",
            font=("Consolas", 10),
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)

        yscroll = ttk.Scrollbar(self.log_text, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")

    def add_source(self):
        p = filedialog.askdirectory(title="Select MLO resource folder")
        if not p:
            return
        path = Path(p).resolve()
        if path in self.sources:
            messagebox.showinfo("Already added", "That folder is already in the queue.")
            return
        self.sources.append(path)
        self.listbox.insert("end", str(path))

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        # remove from end to start
        for idx in reversed(sel):
            self.listbox.delete(idx)
            del self.sources[idx]

    def clear_sources(self):
        self.sources.clear()
        self.listbox.delete(0, "end")

    def pick_out(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.out_var.set(p)

    def open_output(self):
        out = Path(self.out_var.get()).resolve()
        out.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(out)  # Windows
        except Exception:
            messagebox.showinfo("Output", f"Output folder: {out}")

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(80, self._drain_log_queue)

    def _progress(self, current: int, total: int):
        if total <= 0:
            self.firebar.set(0, 100)
            return
        self.firebar.set(current, total)

    def _setup_logger(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.logfile_path = out_dir / f"mlo_cleaner_{ts}.log"

        logger = logging.getLogger(f"mlo_cleaner_{ts}")
        logger.setLevel(logging.DEBUG)

        fh = logging.FileHandler(self.logfile_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        fh.setFormatter(fmt)

        logger.handlers.clear()
        logger.addHandler(fh)

        self.logger = logger
        self.log_q.put(f"[LOGFILE] {self.logfile_path}")

    def start(self):
        if not self.sources:
            messagebox.showerror("No MLO folders", "Add at least one MLO folder to the queue.")
            return

        out = self.out_var.get().strip()
        if not out:
            messagebox.showerror("Missing", "Please select an Output folder.")
            return

        out_p = Path(out).resolve()
        out_p.mkdir(parents=True, exist_ok=True)
        self._setup_logger(out_p)

        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.firebar.set(0, 100)

        self.log_q.put("Starting queue…")

        def run_queue():
            try:
                worker = CleanerWorker(self.log_q, self.stop_event, self.logger)

                n = len(self.sources)
                for idx, src in enumerate(self.sources, start=1):
                    if self.stop_event.is_set():
                        self.log_q.put("Queue stopped.")
                        break

                    # global progress chunk per job
                    start = int(((idx - 1) / n) * 100)
                    end = int((idx / n) * 100)

                    self.log_q.put(f"\n>>> Job {idx}/{n}: {src.name}")
                    worker.clean_one(
                        src=src,
                        out_dir=out_p,
                        keep_exts=set(DEFAULT_KEEP_EXTS),
                        include_ybn=self.include_ybn.get(),
                        make_fxmanifest=self.make_fxmanifest.get(),
                        flatten_stream=self.flatten_stream.get(),
                        dedupe=self.dedupe.get(),
                        progress_cb=self._progress,
                        progress_range=(start, end),
                    )

                self.log_q.put("\nAll done.")
            except Exception as e:
                self.log_q.put(f"FATAL ERROR: {e}")
                if self.logger:
                    self.logger.exception("Fatal error: %s", e)
            finally:
                self.start_btn.config(state="normal")
                self.stop_btn.config(state="disabled")

        self.worker_thread = threading.Thread(target=run_queue, daemon=True)
        self.worker_thread.start()

    def stop(self):
        self.stop_event.set()
        self.log_q.put("Stop requested…")

if __name__ == "__main__":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    App().mainloop()
