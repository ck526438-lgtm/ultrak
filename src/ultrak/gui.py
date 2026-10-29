"""Tkinter front-end. Standard library only, so there is nothing to pip install."""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .batch import prepare, run_batch, summarize
from .planner import total_frames
from .report import poster
from .utils import IS_WINDOWS, UltraKError, human_bytes, human_duration

BG = "#15171c"
PANEL = "#1d2128"
FG = "#e6e9ef"
ACCENT = "#3fa7ff"
MUTED = "#8b94a3"

MODES_LABEL = {
    "auto": "Auto honnete (recommande)",
    "4k": "4K forcee (3840x2160)",
    "qhd": "QHD (2560x1440)",
    "1080": "Full HD (1920x1080)",
}
OPTION_LABEL = {
    "preset": ["veryfast", "faster", "fast", "medium", "slow", "slower"],
    "denoise": ["auto", "none", "light", "medium", "strong"],
    "sharpen": ["auto", "none", "soft", "pro", "strong"],
    "grade": ["none", "natural", "punch"],
    "audio": ["copy", "aac", "none"],
}


class App:
    def __init__(self, root, hw):
        self.hw = hw
        self.root = root
        self.files = []
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.last_states = []
        self._build()
        root.after(120, self._poll)
        if hw is None:
            self.analyze_btn.configure(state="disabled")
            self.start_btn.configure(state="disabled")
            self.set_status("detection du materiel...")
            self._run_job(self._detect_worker, buttons=False)
        else:
            self.set_status(hw.summary())
            self._maybe_demo()

    def _maybe_demo(self):
        """ULTRAK_DEMO=1: analyse tout de suite, used by tools/shot_gui.py for the README."""
        if os.environ.get("ULTRAK_DEMO") and self.files:
            self.root.after(400, self.analyze)

    def _detect_worker(self):
        """Probe the machine (and fetch ffmpeg if needed) without freezing the window."""
        from .hardware import detect
        from .installer import install
        try:
            self.hw = detect()
        except UltraKError:
            self.emit("ffmpeg introuvable -> installation (~190 Mo, une seule fois)")
            try:
                self.hw = detect(install(log=self.emit))
            except UltraKError as exc:
                self.emit(f"installation rateee: {exc}")
                self.messages.put(("hw_failed", None))
                return
        self.messages.put(("hw", self.hw))

    def _build(self):
        self.root.title(window_title())
        self.root.geometry("1020x780")
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Head.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 13, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("Go.TButton", foreground="#0c0d10", background=ACCENT,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG)
        style.configure("Green.Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT)

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(top, text="UltraK", style="Head.TLabel").pack(side="left")
        ttk.Label(top, text="100% local - aucune upload, aucun compte, aucune licence",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(4, 0))

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=14)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        ttk.Label(left, text="Videos a traiter").pack(anchor="w")
        self.listbox = tk.Listbox(left, bg=PANEL, fg=FG, selectbackground=ACCENT,
                                  highlightthickness=0, font=("Consolas", 9), relief="flat")
        self.listbox.pack(fill="both", expand=True, pady=6)

        row = ttk.Frame(left)
        row.pack(fill="x")
        for text, command in (("Ajouter", self.choose_files), ("Retirer", self.remove_selected),
                              ("Tout vider", self.clear_files)):
            ttk.Button(row, text=text, command=command).pack(side="left", expand=True, fill="x", padx=2)

        right = ttk.Frame(body, width=330)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        form = ttk.Frame(right, style="Panel.TFrame", padding=10)
        form.pack(fill="x", pady=(24, 0))

        self.mode = self._combo(form, "Qualite cible", list(MODES_LABEL.values()), 0)
        self.preset = self._combo(form, "Preset x264", OPTION_LABEL["preset"], 3)
        self.denoise = self._combo(form, "Denoise", OPTION_LABEL["denoise"], 0)
        self.sharpen = self._combo(form, "Netimete", OPTION_LABEL["sharpen"], 0)
        self.grade = self._combo(form, "Etalonnage", OPTION_LABEL["grade"], 1)
        self.audio = self._combo(form, "Audio", OPTION_LABEL["audio"], 0)
        numbers = ttk.Frame(form, style="Panel.TFrame")
        numbers.pack(fill="x", pady=(4, 0))
        self.crf = self._spin(numbers, "CRF 16-23", 18, 12, 28)
        self.fps = self._spin(numbers, "FPS (0=src)", 0, 0, 120, increment=5)
        self.budget = self._spin(numbers, "Budget min", 0, 0, 600)

        self.preview_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Tester 4 s avant de lancer (ETA reel)",
                        variable=self.preview_var, style="TLabel").pack(anchor="w", pady=(6, 0))
        self.poster_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Generer l'image avant/apres", variable=self.poster_var,
                        style="TLabel").pack(anchor="w")

        out_row = ttk.Frame(form)
        out_row.pack(fill="x", pady=(8, 0))
        ttk.Label(out_row, text="Sortie:").pack(side="left")
        self.out_dir = tk.StringVar(value="")
        ttk.Entry(out_row, textvariable=self.out_dir, width=22).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(out_row, text="...", command=self.pick_out).pack(side="right")

        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=14, pady=10)
        self.analyze_btn = ttk.Button(actions, text="Analyser", command=self.analyze)
        self.analyze_btn.pack(side="left", padx=(0, 6))
        self.start_btn = ttk.Button(actions, text="Lancer le rendu", style="Go.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(actions, text="Annuler", command=self.cancel_render,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="Ouvrir la sortie", command=self.open_out).pack(side="right")

        bottom = ttk.Frame(self.root, style="Panel.TFrame", padding=8)
        bottom.pack(fill="both", padx=14, pady=(0, 12))
        self.bar = ttk.Progressbar(bottom, style="Green.Horizontal.TProgressbar", maximum=100)
        self.bar.pack(fill="x")
        self.stats = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.stats.pack(anchor="w", pady=(4, 2))
        self.log = tk.Text(bottom, height=9, bg="#12141a", fg=MUTED, relief="flat",
                           font=("Consolas", 9), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

    @staticmethod
    def _version():
        from . import __version__
        return __version__

    def _combo(self, parent, label, values, index):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(4, 0))
        var = tk.StringVar(value=values[index])
        box = ttk.Combobox(parent, textvariable=var, values=values, state="readonly")
        box.pack(fill="x")
        return var

    def _spin(self, parent, label, default, low, high, increment=1):
        col = ttk.Frame(parent)
        col.pack(side="left", expand=True, fill="x", padx=2)
        ttk.Label(col, text=label).pack(anchor="w")
        var = tk.IntVar(value=default)
        ttk.Spinbox(col, from_=low, to=high, textvariable=var, increment=increment).pack(fill="x")
        return var

    def add_files(self, paths):
        for path in paths:
            path = os.path.abspath(path)
            if path not in self.files:
                self.files.append(path)
                self.listbox.insert("end", os.path.basename(path))

    def choose_files(self):
        paths = filedialog.askopenfilenames(
            title="Choisis tes videos",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.mpg *.mpeg *.ts *.flv"),
                       ("Tous les fichiers", "*.*")])
        if paths:
            self.add_files(paths)

    def remove_selected(self):
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
            del self.files[index]

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files = []

    def pick_out(self):
        path = filedialog.askdirectory(title="Dossier de sortie")
        if path:
            self.out_dir.set(path)

    def open_out(self):
        folder = self.out_dir.get() or (os.path.dirname(self.files[0]) if self.files else os.getcwd())
        if IS_WINDOWS:
            os.startfile(folder)  # noqa: E010
        else:
            subprocess.Popen(["xdg-open", folder])

    def options(self) -> dict:
        mode_key = next(k for k, v in MODES_LABEL.items() if v == self.mode.get())
        return dict(mode=mode_key, preset=self.preset.get(), crf=int(self.crf.get()),
                    fps=float(self.fps.get()), denoise=self.denoise.get(),
                    sharpen=self.sharpen.get(), grade=self.grade.get(), audio=self.audio.get(),
                    budget_minutes=float(self.budget.get()),
                    use_preview=bool(self.preview_var.get()),
                    out_dir=self.out_dir.get())

    def analyze(self):
        if not self._ready():
            return
        if not self.files:
            messagebox.showwarning("UltraK", "Ajoute d'abord une video.")
            return
        self._run_job(self._analyze_worker, buttons=False)
        self.set_status("analyse en cours...")

    def _analyze_worker(self):
        states = []
        for path in self.files:
            opts = dict(self.options(), use_preview=False)  # l'analyse ne rend rien
            state = prepare(path, self.hw.ffmpeg, self.hw, log=self.emit, **opts)
            states.append(state)
            frames = total_frames(state.job.src, state.job.plan)
            self.emit(f"  ETA estime: {human_duration(state.job.eta_for(frames))}")
        self.last_states = states
        self.set_status(f"analyse terminee: {len(states)} fichier(s)")
        self.messages.put(("done", None))

    def _ready(self) -> bool:
        if self.hw is not None:
            return True
        messagebox.showwarning("UltraK", "Detection de la machine pas terminee (ou ffmpeg "
                                         "introuvable). Attends un peu, ou lance "
                                         "`python ultrak.py install-ffmpeg`.")
        return False

    def start(self):
        if not self._ready():
            return
        if not self.files:
            messagebox.showwarning("UltraK", "Ajoute d'abord une video.")
            return
        self.cancel.clear()
        self._run_job(self._render_worker, buttons=True)

    def _render_worker(self):
        states = []
        try:
            for path in self.files:
                states.append(prepare(path, self.hw.ffmpeg, self.hw, log=self.emit,
                                      **self.options()))
            self.last_states = states
            run_batch(states, self.hw.ffmpeg, self.hw, on_progress=self._on_progress,
                      cancel=self.cancel, log=self.emit)
            self.emit(summarize(states))
            self.set_status("rendu termine" if not self.cancel.is_set() else "rendu annule")
            if self.poster_var.get():
                self._make_posters(states)
        except UltraKError as exc:
            self.emit(f"erreur: {exc}")
        self.messages.put(("done", None))

    def _make_posters(self, states):
        for state in states:
            if state.status != "done":
                continue
            path = os.path.splitext(state.job.dst)[0] + "_compare.png"
            try:
                poster(self.hw, state.job.src.path, state.job.dst, path,
                       at=min(3.0, (state.job.src.duration or 6) / 3))
                self.emit(f"image avant/apres: {path}")
            except UltraKError as exc:
                self.emit(f"image avant/apres impossible: {exc}")

    def _on_progress(self, state, progress):
        self.messages.put(("progress", (progress.percent, progress.speed,
                                        progress.eta_seconds, progress.frames)))

    def _run_job(self, target, buttons):
        self.worker = threading.Thread(target=self._guarded, args=(target,), daemon=True)
        self.worker.start()
        if buttons:
            self.start_btn.configure(state="disabled")
            self.analyze_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")

    def _guarded(self, target):
        started = time.perf_counter()
        try:
            target()
        except Exception as exc:
            self.emit(f"erreur inattendue: {type(exc).__name__}: {exc}")
            self.messages.put(("done", None))
        self.emit(f"({time.perf_counter() - started:.1f}s)")

    def cancel_render(self):
        self.cancel.set()
        self.emit("annulation demandee, l'encodage en cours s'arrete...")

    def emit(self, text: str):
        self.messages.put(("log", str(text)))

    def set_status(self, text: str):
        self.messages.put(("status", text))

    def _poll(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "progress":
                    percent, speed, eta, frames = payload
                    self.bar["value"] = percent
                    self.stats.configure(
                        text=f"{percent:5.1f}%   {speed:.2f} fps   reste "
                             f"{human_duration(eta)}   {frames} images rendues")
                elif kind == "status":
                    self.stats.configure(text=payload)
                elif kind == "hw":
                    self.set_status(payload.summary())
                    self.analyze_btn.configure(state="normal")
                    self.start_btn.configure(state="normal")
                    self._maybe_demo()
                elif kind == "hw_failed":
                    self.set_status("ffmpeg indisponible - voir le journal")
                    self.start_btn.configure(state="normal")
                    self.analyze_btn.configure(state="normal")
                elif kind == "done":
                    self.start_btn.configure(state="normal")
                    self.analyze_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _append(self, line: str):
        self.log.configure(state="normal")
        for part in str(line).splitlines():
            self.log.insert("end", part + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv",
             ".mpg", ".mpeg", ".ts", ".flv")


def window_title() -> str:
    from . import __version__
    return f"UltraK {__version__} - video moyenne -> 4K exploitable"


def launch(hw=None, files=None):
    root = tk.Tk()
    app = App(root, hw)
    if files:
        app.add_files([f for f in files if f.lower().endswith(VIDEO_EXT)])
    root.protocol("WM_DELETE_WINDOW", lambda: (app.cancel.set(), root.destroy()))
    root.mainloop()
    return 0
