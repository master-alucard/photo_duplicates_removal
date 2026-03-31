"""
main.py — Image Deduper v2 GUI
Complete rewrite: Quick/Advanced modes, ⓘ info popups, phase-aware progress,
pre-scan estimate, pause/resume, in-app report viewer.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ── dependency check ─────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageTk
    import imagehash
except ImportError as _e:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Missing dependencies",
        "Please install requirements first:\n\n"
        "  pip install Pillow imagehash piexif\n\n"
        f"Error: {_e}"
    )
    sys.exit(1)

try:
    import rawpy  # type: ignore
    _RAWPY_AVAILABLE = True
except ImportError:
    _RAWPY_AVAILABLE = False

from config import Settings, DEFAULTS, load_settings, save_settings
from info_texts import INFO_TEXTS
from progress_tracker import PhaseTracker
from scanner import collect_images, find_groups, IMAGE_EXTENSIONS
from mover import move_groups, ops_log_path
from reporter import generate_report
from report_viewer import ReportViewer


# ── constants ────────────────────────────────────────────────────────────────

SETTINGS_PATH = Path(__file__).parent / "settings.json"
PHASE_NAMES = ["Discovery", "Hashing", "Comparing", "Metadata", "Moving", "Report"]

_ACCENT = "#1a73e8"
_BG = "#f0f2f5"
_CARD_BG = "#ffffff"


# ── app icon ─────────────────────────────────────────────────────────────────

def _make_icon(size: int = 64) -> ImageTk.PhotoImage:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill=(26, 115, 232, 255))
    m = size // 6
    draw.rounded_rectangle(
        [m, m + 2, size - m - 1, size - m - 2],
        radius=max(2, size // 12),
        outline=(255, 255, 255, 230), width=max(1, size // 20)
    )
    pts = [m + 3, size - m - 3, size // 2 - 2, m + 10, size - m - 3, size - m - 3]
    draw.polygon(pts, fill=(255, 255, 255, 190))
    r = size // 9
    cx, cy = size - m - r - 2, m + r + 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 215, 0, 255))
    return ImageTk.PhotoImage(img)


# ── info popup ───────────────────────────────────────────────────────────────

def show_info(parent: tk.Widget, key: str) -> None:
    title, text = INFO_TEXTS.get(key, ("Help", "No help available."))
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("460x280")
    win.grab_set()
    win.resizable(False, False)
    txt = tk.Text(win, wrap=tk.WORD, padx=12, pady=12, relief=tk.FLAT,
                  bg=win.cget("bg"), font=("Segoe UI", 9))
    txt.insert("1.0", text)
    txt.config(state=tk.DISABLED)
    txt.pack(fill=tk.BOTH, expand=True)
    ttk.Button(win, text="Close", command=win.destroy).pack(pady=8)


# ── UI helpers ────────────────────────────────────────────────────────────────

def _section(parent: tk.Widget, title: str) -> ttk.LabelFrame:
    f = ttk.LabelFrame(parent, text=title, padding=(10, 6, 10, 8))
    f.pack(fill=tk.X, pady=(0, 6))
    return f


def _info_btn(parent: tk.Widget, key: str) -> ttk.Button:
    return ttk.Button(
        parent, text="\u24d8", width=2,
        command=lambda k=key: show_info(parent.winfo_toplevel(), k)
    )


def _row(parent: tk.Widget) -> tk.Frame:
    r = ttk.Frame(parent)
    r.pack(fill=tk.X, pady=2)
    return r


def _label(parent: tk.Widget, text: str, width: int = 26) -> ttk.Label:
    lbl = ttk.Label(parent, text=text, width=width, anchor=tk.W)
    lbl.pack(side=tk.LEFT)
    return lbl


def _first_sentence(text: str) -> str:
    """Extract the first sentence from a multi-line help string."""
    line = text.split("\n")[0].strip()
    if "." in line:
        return line[: line.index(".") + 1]
    return line[:100]


# ── main application ──────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Image Deduper v2")
        self.root.geometry("750x820")
        self.root.resizable(True, True)
        self.root.minsize(700, 600)

        try:
            self._icon = _make_icon(64)
            root.wm_iconphoto(True, self._icon)
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        # Load settings
        self.settings = load_settings(SETTINGS_PATH)

        # Scan state
        self.report_path: Path | None = None
        self.scan_groups: list = []
        self.scan_records: list = []
        self._stop_flag: list[bool] = [False]
        self._pause_flag: list[bool] = [False]
        self._paused_state = None   # ScanState if paused mid-scan
        self._save_after_id = None  # debounce timer id

        # Tracker
        self._tracker: PhaseTracker | None = None

        self._build_ui()
        self._check_resume_state()
        self._schedule_estimate_update()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self.root, bg=_ACCENT)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="Image Deduper v2",
            font=("Segoe UI", 15, "bold"), bg=_ACCENT, fg="white"
        ).pack(side=tk.LEFT, padx=20, pady=11)
        tk.Label(
            hdr, text="Find & remove duplicate preview images",
            font=("Segoe UI", 9), bg=_ACCENT, fg="#b3cfff"
        ).pack(side=tk.LEFT)

        # Mode toggle
        mode_bar = tk.Frame(self.root, bg="#e8eaed")
        mode_bar.pack(fill=tk.X)
        tk.Label(mode_bar, text="Mode:", bg="#e8eaed", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(12, 4), pady=6)
        self._mode_var = tk.StringVar(value=self.settings.mode)
        for mode_val, mode_lbl in (("quick", "Quick"), ("advanced", "Advanced")):
            rb = tk.Radiobutton(
                mode_bar, text=mode_lbl, variable=self._mode_var, value=mode_val,
                bg="#e8eaed", font=("Segoe UI", 9, "bold"),
                indicatoron=False, width=10, relief=tk.GROOVE,
                command=self._on_mode_change,
                selectcolor=_ACCENT, fg="white" if self._mode_var.get() == mode_val else "#333",
            )
            rb.pack(side=tk.LEFT, padx=2, pady=4)
        self._mode_btns = mode_bar

        # Scrollable body
        scroll_container = tk.Frame(self.root)
        scroll_container.pack(fill=tk.BOTH, expand=True)

        self._body_canvas = tk.Canvas(scroll_container, bg=_BG, highlightthickness=0)
        _sb = ttk.Scrollbar(scroll_container, orient=tk.VERTICAL, command=self._body_canvas.yview)
        self._body_canvas.configure(yscrollcommand=_sb.set)
        _sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._body_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._body = ttk.Frame(self._body_canvas, padding=(14, 6, 14, 6))
        self._body_window = self._body_canvas.create_window((0, 0), window=self._body, anchor=tk.NW)
        self._body.bind("<Configure>", lambda e: self._body_canvas.configure(
            scrollregion=self._body_canvas.bbox("all")))
        self._body_canvas.bind("<Configure>", lambda e: self._body_canvas.itemconfig(
            self._body_window, width=e.width))
        # Scroll anywhere on the page — bind_all catches wheel on every child widget
        def _on_mousewheel(event):
            self._body_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.root.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Folders ──────────────────────────────────────────────────────
        folders = _section(self._body, "Folders")
        self.src_var = self._folder_row(folders, "Source folder:", "src_folder")
        self.out_var = self._folder_row(folders, "Output folder:", "out_folder")

        # ── Advanced-only sections (hidden in quick mode) ─────────────────
        self._advanced_frames: list[tk.Widget] = []

        # Detection
        det = _section(self._body, "Detection")
        self._advanced_frames.append(det)

        self.thresh_var = tk.DoubleVar(value=self.settings.threshold)
        self._slider_row(det, "Similarity Threshold", self.thresh_var,
                         1, 30, 8, 12, 12, "threshold", 1,
                         lambda v: str(int(round(v))))

        self.ratio_var = tk.DoubleVar(value=self.settings.preview_ratio)
        self._slider_row(det, "Preview Size Ratio (per-dimension)", self.ratio_var,
                         0.50, 0.99, 0.85, 0.95, 0.90, "preview_ratio", 0.01,
                         lambda v: f"{v:.2f}")

        self.series_tol_var = tk.DoubleVar(value=self.settings.series_tolerance_pct)
        self._slider_row(det, "Series Dimension Tolerance %", self.series_tol_var,
                         0.0, 10.0, 0.0, 2.0, 0.0, "series_tolerance_pct", 0.1,
                         lambda v: f"{v:.1f}%")

        self.series_thresh_var = tk.DoubleVar(value=self.settings.series_threshold_factor)
        self._slider_row(det, "Series Grouping Leniency", self.series_thresh_var,
                         1.0, 5.0, 1.5, 2.5, 2.0, "series_threshold_factor", 0.1,
                         lambda v: f"{v:.1f}\u00d7")

        self.ar_tol_var = tk.DoubleVar(value=self.settings.ar_tolerance_pct)
        self._slider_row(det, "Aspect Ratio Tolerance %", self.ar_tol_var,
                         0.0, 20.0, 3.0, 8.0, 5.0, "ar_tolerance_pct", 0.5,
                         lambda v: f"{v:.1f}%")

        # Dark protection group
        r = _row(det)
        self.dark_var = tk.BooleanVar(value=self.settings.dark_protection)
        ttk.Checkbutton(r, text="Dark Image Protection", variable=self.dark_var).pack(side=tk.LEFT)
        _info_btn(r, "dark_protection").pack(side=tk.LEFT, padx=2)
        _, _dark_desc = INFO_TEXTS.get("dark_protection", ("", ""))
        if _dark_desc:
            ttk.Label(r, text=_first_sentence(_dark_desc),
                      foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)
        self.dark_var.trace_add("write", self._on_setting_change)

        self.dark_thresh_var = tk.DoubleVar(value=self.settings.dark_threshold)
        self._slider_row(det, "  Dark Image Threshold (brightness 0–255)", self.dark_thresh_var,
                         0.0, 128.0, 30.0, 50.0, 40.0, "dark_threshold", 1.0,
                         lambda v: f"{int(round(v))}")

        self.dark_factor_var = tk.DoubleVar(value=self.settings.dark_tighten_factor)
        self._slider_row(det, "  Dark Tighten Factor", self.dark_factor_var,
                         0.1, 1.0, 0.4, 0.6, 0.5, "dark_tighten_factor", 0.05,
                         lambda v: f"{v:.2f}")

        # Dual hash / histogram toggles
        r = _row(det)
        self.dual_hash_var = tk.BooleanVar(value=self.settings.use_dual_hash)
        ttk.Checkbutton(r, text="Dual Hash (dHash)", variable=self.dual_hash_var).pack(side=tk.LEFT)
        _info_btn(r, "use_dual_hash").pack(side=tk.LEFT, padx=2)
        self.dual_hash_var.trace_add("write", self._on_setting_change)

        r = _row(det)
        self.hist_var = tk.BooleanVar(value=self.settings.use_histogram)
        ttk.Checkbutton(r, text="Histogram Intersection Guard", variable=self.hist_var).pack(side=tk.LEFT)
        _info_btn(r, "use_histogram").pack(side=tk.LEFT, padx=2)
        self.hist_var.trace_add("write", self._on_setting_change)

        self.hist_sim_var = tk.DoubleVar(value=self.settings.hist_min_similarity)
        self._slider_row(det, "  Minimum Histogram Similarity", self.hist_sim_var,
                         0.0, 1.0, 0.65, 0.80, 0.70, "hist_min_similarity", 0.05,
                         lambda v: f"{v:.2f}")

        self.brightness_diff_var = tk.DoubleVar(value=self.settings.brightness_max_diff)
        self._slider_row(det, "Max Brightness Difference (0–255)", self.brightness_diff_var,
                         0.0, 200.0, 40.0, 80.0, 60.0, "brightness_max_diff", 5.0,
                         lambda v: f"{int(round(v))}")

        # Ambiguous detection
        r = _row(det)
        self.ambig_var = tk.BooleanVar(value=self.settings.ambiguous_detection)
        ttk.Checkbutton(r, text="Ambiguous Match Detection", variable=self.ambig_var).pack(side=tk.LEFT)
        _info_btn(r, "ambiguous_detection").pack(side=tk.LEFT, padx=2)
        _, _ambig_desc = INFO_TEXTS.get("ambiguous_detection", ("", ""))
        if _ambig_desc:
            ttk.Label(r, text=_first_sentence(_ambig_desc),
                      foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)
        self.ambig_var.trace_add("write", self._on_setting_change)

        self.ambig_factor_var = tk.DoubleVar(value=self.settings.ambiguous_threshold_factor)
        self._slider_row(det, "  Ambiguous Threshold Factor", self.ambig_factor_var,
                         1.0, 3.0, 1.3, 2.0, 1.5, "ambiguous_threshold_factor", 0.1,
                         lambda v: f"{v:.1f}\u00d7")

        # Keep Strategy
        keep = _section(self._body, "Keep Strategy")
        self._advanced_frames.append(keep)

        r = _row(keep)
        _label(r, "Prefer to keep:")
        self.strategy_var = tk.StringVar(value=self.settings.keep_strategy)
        ttk.Radiobutton(r, text="Largest resolution", variable=self.strategy_var, value="pixels").pack(side=tk.LEFT)
        ttk.Radiobutton(r, text="Oldest file date", variable=self.strategy_var, value="oldest").pack(side=tk.LEFT, padx=6)
        _info_btn(r, "keep_strategy").pack(side=tk.LEFT, padx=2)
        self.strategy_var.trace_add("write", self._on_setting_change)

        r = _row(keep)
        self.all_formats_var = tk.BooleanVar(value=self.settings.keep_all_formats)
        ttk.Checkbutton(r, text="Keep all formats (best per extension)", variable=self.all_formats_var).pack(side=tk.LEFT)
        _info_btn(r, "keep_all_formats").pack(side=tk.LEFT, padx=2)
        self.all_formats_var.trace_add("write", self._on_setting_change)

        r = _row(keep)
        self.prefer_meta_var = tk.BooleanVar(value=self.settings.prefer_rich_metadata)
        ttk.Checkbutton(r, text="Prefer image with richer EXIF metadata", variable=self.prefer_meta_var).pack(side=tk.LEFT)
        _info_btn(r, "prefer_rich_metadata").pack(side=tk.LEFT, padx=2)
        self.prefer_meta_var.trace_add("write", self._on_setting_change)

        # Metadata
        meta_sec = _section(self._body, "Metadata")
        self._advanced_frames.append(meta_sec)

        r = _row(meta_sec)
        self.collect_meta_var = tk.BooleanVar(value=self.settings.collect_metadata)
        ttk.Checkbutton(r, text="Collect EXIF metadata", variable=self.collect_meta_var).pack(side=tk.LEFT)
        _info_btn(r, "collect_metadata").pack(side=tk.LEFT, padx=2)
        self.collect_meta_var.trace_add("write", self._on_setting_change)

        r = _row(meta_sec)
        self.export_csv_var = tk.BooleanVar(value=self.settings.export_csv)
        ttk.Checkbutton(r, text="Export metadata CSV", variable=self.export_csv_var).pack(side=tk.LEFT)
        _info_btn(r, "export_csv").pack(side=tk.LEFT, padx=2)
        self.export_csv_var.trace_add("write", self._on_setting_change)

        r = _row(meta_sec)
        self.ext_report_var = tk.BooleanVar(value=self.settings.extended_report)
        ttk.Checkbutton(r, text="Extended report (EXIF per image)", variable=self.ext_report_var).pack(side=tk.LEFT)
        _info_btn(r, "extended_report").pack(side=tk.LEFT, padx=2)
        self.ext_report_var.trace_add("write", self._on_setting_change)

        r = _row(meta_sec)
        self.sort_fname_var = tk.BooleanVar(value=self.settings.sort_by_filename_date)
        ttk.Checkbutton(r, text="Sort by filename date", variable=self.sort_fname_var).pack(side=tk.LEFT)
        _info_btn(r, "sort_by_filename_date").pack(side=tk.LEFT, padx=2)
        self.sort_fname_var.trace_add("write", self._on_setting_change)

        r = _row(meta_sec)
        self.sort_exif_var = tk.BooleanVar(value=self.settings.sort_by_exif_date)
        ttk.Checkbutton(r, text="Sort by EXIF date", variable=self.sort_exif_var).pack(side=tk.LEFT)
        _info_btn(r, "sort_by_exif_date").pack(side=tk.LEFT, padx=2)
        self.sort_exif_var.trace_add("write", self._on_setting_change)

        # RAW
        raw_sec = _section(self._body, "RAW Files")
        self._advanced_frames.append(raw_sec)

        r = _row(raw_sec)
        self.rawpy_var = tk.BooleanVar(value=self.settings.use_rawpy)
        rawpy_cb = ttk.Checkbutton(
            r, text="Use rawpy for RAW files (CR2, NEF, ARW...)",
            variable=self.rawpy_var,
            state=tk.NORMAL if _RAWPY_AVAILABLE else tk.DISABLED
        )
        rawpy_cb.pack(side=tk.LEFT)
        _info_btn(r, "use_rawpy").pack(side=tk.LEFT, padx=2)
        if not _RAWPY_AVAILABLE:
            ttk.Label(r, text="not installed", foreground="#e03").pack(side=tk.LEFT, padx=4)
            ttk.Button(r, text="Install rawpy",
                       command=self._install_rawpy).pack(side=tk.LEFT, padx=2)
        self.rawpy_var.trace_add("write", self._on_setting_change)

        # Filters
        filt = _section(self._body, "Filters")
        self._advanced_frames.append(filt)

        self.mindim_var = tk.DoubleVar(value=self.settings.min_dimension)
        self._slider_row(filt, "Minimum Dimension Filter (px)", self.mindim_var,
                         0, 2000, 100, 300, 0, "min_dimension", 50,
                         lambda v: f"{int(round(v))} px" if v > 0 else "off")

        r = _row(filt)
        self.recursive_var = tk.BooleanVar(value=self.settings.recursive)
        ttk.Checkbutton(r, text="Scan subfolders recursively", variable=self.recursive_var).pack(side=tk.LEFT)
        _info_btn(r, "recursive").pack(side=tk.LEFT, padx=2)
        self.recursive_var.trace_add("write", self._on_setting_change)

        r = _row(filt)
        _label(r, "Skip folder names:")
        self.skip_names_var = tk.StringVar(value=self.settings.skip_names)
        ttk.Entry(r, textvariable=self.skip_names_var, width=36).pack(side=tk.LEFT)
        _info_btn(r, "skip_names").pack(side=tk.LEFT, padx=2)
        self.skip_names_var.trace_add("write", self._on_setting_change)

        # Actions (dry run + organize — visible in both modes)
        act = _section(self._body, "Actions")

        r = _row(act)
        self.dry_var = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(r, text="Dry Run", variable=self.dry_var).pack(side=tk.LEFT)
        _info_btn(r, "dry_run").pack(side=tk.LEFT, padx=2)
        ttk.Label(r,
                  text="Scan & report only \u2014 no files moved. "
                       "Click \u201cAccept & Move\u201d afterwards to apply.",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)
        self.dry_var.trace_add("write", self._on_setting_change)

        r = _row(act)
        self.org_date_var = tk.BooleanVar(value=self.settings.organize_by_date)
        ttk.Checkbutton(r, text="Organize by Date", variable=self.org_date_var).pack(side=tk.LEFT)
        _info_btn(r, "organize_by_date").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="Create date subfolders in results/ and trash/",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)
        self.org_date_var.trace_add("write", self._on_setting_change)

        r = _row(act)
        ttk.Label(r, text="  Date format:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self.date_fmt_var = tk.StringVar(value=self.settings.date_folder_format)
        _FMT_CHOICES = ["%Y-%m", "%Y/%m", "%Y-%m-%d", "%Y"]
        fmt_cb = ttk.Combobox(r, textvariable=self.date_fmt_var,
                              values=_FMT_CHOICES, width=14, state="readonly")
        fmt_cb.pack(side=tk.LEFT)
        _info_btn(r, "date_folder_format").pack(side=tk.LEFT, padx=2)
        self._date_fmt_example = tk.StringVar()
        ttk.Label(r, textvariable=self._date_fmt_example,
                  foreground="#555", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)
        self.date_fmt_var.trace_add("write", self._on_date_fmt_change)
        self._on_date_fmt_change()

        # Pre-scan estimate (always visible)
        self._estimate_frame = ttk.Frame(self._body)
        self._estimate_frame.pack(fill=tk.X, pady=(2, 4))
        self._estimate_var = tk.StringVar(value="Select a source folder to see estimate.")
        ttk.Label(self._estimate_frame, textvariable=self._estimate_var,
                  foreground="#555", font=("Segoe UI", 8, "italic")).pack(anchor=tk.W)

        # Resume notice (hidden until a paused state is found)
        self._resume_frame = ttk.Frame(self._body)
        self._resume_frame.pack(fill=tk.X, pady=(2, 2))
        self._resume_var = tk.StringVar(value="")
        self._resume_lbl = ttk.Label(
            self._resume_frame, textvariable=self._resume_var,
            foreground="#7c3aed", font=("Segoe UI", 8, "bold")
        )
        self._resume_lbl.pack(side=tk.LEFT)
        self._resume_btn = ttk.Button(self._resume_frame, text="Resume", command=self._resume_scan)
        self._discard_btn = ttk.Button(self._resume_frame, text="Discard", command=self._discard_resume)

        # Progress panel
        self._prog_frame = ttk.LabelFrame(self._body, text="Progress", padding=(8, 4, 8, 6))
        self._prog_frame.pack(fill=tk.X, pady=(4, 4))

        self._phase_label_var = tk.StringVar(value="Ready.")
        ttk.Label(self._prog_frame, textvariable=self._phase_label_var,
                  font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)

        self._progress_bar = ttk.Progressbar(self._prog_frame, mode="determinate", maximum=100)
        self._progress_bar.pack(fill=tk.X, pady=(4, 2))

        self._eta_var = tk.StringVar(value="")
        ttk.Label(self._prog_frame, textvariable=self._eta_var, foreground="#555",
                  font=("Segoe UI", 8)).pack(anchor=tk.W)

        # Details toggle
        self._details_var = tk.BooleanVar(value=self.settings.details_visible)
        toggle_btn = ttk.Checkbutton(
            self._prog_frame, text="Show phase details",
            variable=self._details_var,
            command=self._toggle_details
        )
        toggle_btn.pack(anchor=tk.W, pady=(2, 0))

        self._detail_text = tk.Text(
            self._prog_frame, height=7, state=tk.DISABLED,
            font=("Consolas", 8), bg="#f8f8f8", relief=tk.FLAT
        )

        self._bottom_toolbar()
        self._apply_mode()

    def _bottom_toolbar(self) -> None:
        """Fixed bottom toolbar with all action buttons."""
        bar = tk.Frame(self.root, bg="#e8eaed", pady=6)
        bar.pack(fill=tk.X, side=tk.BOTTOM)

        # Left: reset defaults
        ttk.Button(bar, text="Reset Defaults", command=self._reset_defaults).pack(side=tk.LEFT, padx=8)

        # Right: scan controls
        self.scan_btn = ttk.Button(bar, text="Start Scan", command=self._start_scan)
        self.scan_btn.pack(side=tk.RIGHT, padx=4)

        self.pause_btn = ttk.Button(bar, text="Pause", command=self._pause_scan, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.RIGHT, padx=4)

        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop_scan, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=4)

        # Post-scan buttons (initially hidden, enabled when scan completes)
        self.accept_btn = ttk.Button(
            bar, text="Accept & Move",
            command=self._accept_and_move, state=tk.DISABLED
        )
        self.accept_btn.pack(side=tk.RIGHT, padx=4)

        self.browser_report_btn = ttk.Button(
            bar, text="Browser Report",
            command=self._open_browser_report, state=tk.DISABLED
        )
        self.browser_report_btn.pack(side=tk.RIGHT, padx=4)

        self.inapp_report_btn = ttk.Button(
            bar, text="Review In-App",
            command=self._open_inapp_report, state=tk.DISABLED
        )
        self.inapp_report_btn.pack(side=tk.RIGHT, padx=4)

        self.revert_all_btn = ttk.Button(
            bar, text="Revert All",
            command=self._revert_all, state=tk.DISABLED
        )
        self.revert_all_btn.pack(side=tk.RIGHT, padx=4)

    # ── mode management ───────────────────────────────────────────────────

    def _apply_mode(self) -> None:
        mode = self._mode_var.get()
        for frame in self._advanced_frames:
            if mode == "advanced":
                frame.pack(fill=tk.X, pady=(0, 6))
            else:
                frame.pack_forget()

    def _on_mode_change(self) -> None:
        self.settings.mode = self._mode_var.get()
        self._apply_mode()
        self._schedule_settings_save()

    # ── folder row helper ─────────────────────────────────────────────────

    def _folder_row(self, parent: tk.Widget, label: str, setting_key: str) -> tk.StringVar:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=3)
        ttk.Label(frame, text=label, width=16, anchor=tk.W).pack(side=tk.LEFT)
        var = tk.StringVar(value=getattr(self.settings, setting_key, ""))
        ttk.Entry(frame, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(frame, text="Browse...", command=lambda v=var, k=setting_key: self._browse(v, k)).pack(side=tk.RIGHT)
        var.trace_add("write", self._on_folder_change)
        return var

    # ── slider row helper ─────────────────────────────────────────────────

    def _slider_row(
        self,
        parent: tk.Widget,
        label: str,
        var: tk.Variable,
        min_v: float,
        max_v: float,
        rec_lo: float,
        rec_hi: float,
        default: float,
        key: str,
        step: float,
        fmt,          # callable: float -> str
    ) -> None:
        """
        Create a complete slider block:
          Label (bold)
          [slider ━━━━◉━━━━━━] [value] [↺ reset] [ⓘ info]
          marks canvas: min  ←rec: lo–hi→  max
          Short description text
        """
        outer = ttk.Frame(parent)
        outer.pack(fill=tk.X, pady=(6, 2))

        # ── title ──
        ttk.Label(outer, text=label, font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)

        # ── control row ──
        ctrl = ttk.Frame(outer)
        ctrl.pack(fill=tk.X)

        scale = ttk.Scale(ctrl, from_=min_v, to=max_v, variable=var, orient=tk.HORIZONTAL)
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        disp_var = tk.StringVar()

        def _update_disp(*_):
            try:
                disp_var.set(fmt(var.get()))
            except Exception:
                disp_var.set("?")

        var.trace_add("write", _update_disp)
        _update_disp()

        ttk.Label(ctrl, textvariable=disp_var, width=8, anchor=tk.E,
                  font=("Consolas", 9)).pack(side=tk.LEFT)

        def _reset():
            var.set(default)
            self._on_setting_change()

        ttk.Button(ctrl, text="\u21ba", width=3, command=_reset).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl, text="\u24d8", width=2,
                   command=lambda k=key: show_info(self.root, k)).pack(side=tk.LEFT, padx=2)

        # Jump to clicked position, then snap to nearest step
        def _jump_to_click(event):
            try:
                w = scale.winfo_width()
                PAD = 8
                track_w = max(w - 2 * PAD, 1)
                frac = max(0.0, min(1.0, (event.x - PAD) / track_w))
                raw = min_v + frac * (max_v - min_v)
                snapped = round(round(raw / step) * step, 10)
                var.set(max(min_v, min(max_v, snapped)))
            except Exception:
                pass
            return "break"  # prevent default step-by-one behaviour

        scale.bind("<Button-1>", _jump_to_click)

        def _snap_on_release(*_):
            try:
                v = var.get()
                rounded = round(round(v / step) * step, 10)
                if abs(rounded - v) > step * 0.001:
                    var.set(rounded)
            except Exception:
                pass

        scale.bind("<ButtonRelease-1>", _snap_on_release)
        var.trace_add("write", lambda *_: self._on_setting_change())

        # ── marks canvas ──
        canvas_h = 20
        try:
            _cbg = parent.cget("bg")
        except Exception:
            _cbg = _BG
        marks_c = tk.Canvas(outer, height=canvas_h, highlightthickness=0, bg=_cbg)
        marks_c.pack(fill=tk.X, pady=(1, 0))

        def _draw_marks(event=None):
            marks_c.delete("all")
            scale.update_idletasks()
            scale_x = scale.winfo_x()
            scale_w = scale.winfo_width()
            if scale_w < 20:
                return
            PAD = 8  # approximate ttk.Scale track inset
            t_start = scale_x + PAD
            t_end = scale_x + scale_w - PAD
            t_w = t_end - t_start
            if t_w <= 0:
                return

            def px(v: float) -> float:
                return t_start + (v - min_v) / (max_v - min_v) * t_w

            # Baseline
            marks_c.create_line(t_start, canvas_h // 2,
                                 t_end, canvas_h // 2, fill="#ddd", width=1)
            # Recommended range band
            marks_c.create_rectangle(
                px(rec_lo), canvas_h // 2 - 3,
                px(rec_hi), canvas_h // 2 + 3,
                fill="#b3cfff", outline="", tags="rec"
            )
            # Ticks and labels
            for v, color, is_edge in [
                (min_v,  "#999",     True),
                (rec_lo, "#1a73e8",  False),
                (rec_hi, "#1a73e8",  False),
                (max_v,  "#999",     True),
            ]:
                x = px(v)
                marks_c.create_line(x, 2, x, canvas_h - 4, fill=color, width=1)
                text = fmt(v)
                anchor = tk.NW if is_edge and v == min_v else (tk.NE if is_edge else tk.N)
                marks_c.create_text(x, canvas_h - 2, text=text,
                                    anchor=tk.S, fill=color, font=("Segoe UI", 7))

        marks_c.bind("<Configure>", _draw_marks)
        outer.after(80, _draw_marks)

        # ── description ──
        _, detail = INFO_TEXTS.get(key, ("", ""))
        if detail:
            desc = _first_sentence(detail)
            ttk.Label(outer, text=desc, foreground="#666",
                      font=("Segoe UI", 8), wraplength=560,
                      justify=tk.LEFT).pack(anchor=tk.W, pady=(1, 0))

    def _browse(self, var: tk.StringVar, key: str) -> None:
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            var.set(folder)

    def _on_folder_change(self, *_) -> None:
        self._on_setting_change()
        # Trigger estimate update after 2s
        if hasattr(self, "_estimate_after_id"):
            self.root.after_cancel(self._estimate_after_id)
        self._estimate_after_id = self.root.after(2000, self._update_estimate)

    # ── settings persistence ──────────────────────────────────────────────

    def _on_date_fmt_change(self, *_) -> None:
        import datetime
        fmt = self.date_fmt_var.get()
        try:
            example = datetime.datetime(2024, 3, 15).strftime(fmt)
            self._date_fmt_example.set(f"e.g. {example}/")
        except Exception:
            self._date_fmt_example.set("(invalid format)")
        self._on_setting_change()

    def _on_setting_change(self, *_) -> None:
        self._schedule_settings_save()

    def _schedule_settings_save(self) -> None:
        if self._save_after_id is not None:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(500, self._save_settings_now)

    def _save_settings_now(self) -> None:
        self._save_after_id = None
        self._collect_settings()
        save_settings(self.settings, SETTINGS_PATH)

    def _collect_settings(self) -> None:
        """Read all UI vars into self.settings."""
        s = self.settings
        s.mode = self._mode_var.get()
        s.src_folder = self.src_var.get()
        s.out_folder = self.out_var.get()
        s.threshold = self._safe_int(self.thresh_var, 12)
        s.preview_ratio = self._safe_float(self.ratio_var, 0.90)
        s.series_tolerance_pct = self._safe_float(self.series_tol_var, 0.0)
        s.series_threshold_factor = self._safe_float(self.series_thresh_var, 2.0)
        s.ar_tolerance_pct = self._safe_float(self.ar_tol_var, 5.0)
        s.dark_protection = self.dark_var.get()
        s.dark_threshold = self._safe_float(self.dark_thresh_var, 40.0)
        s.dark_tighten_factor = self._safe_float(self.dark_factor_var, 0.5)
        s.use_dual_hash = self.dual_hash_var.get()
        s.use_histogram = self.hist_var.get()
        s.hist_min_similarity = self._safe_float(self.hist_sim_var, 0.70)
        s.brightness_max_diff = self._safe_float(self.brightness_diff_var, 60.0)
        s.ambiguous_detection = self.ambig_var.get()
        s.ambiguous_threshold_factor = self._safe_float(self.ambig_factor_var, 1.5)
        s.use_rawpy = self.rawpy_var.get()
        s.keep_strategy = self.strategy_var.get()
        s.keep_all_formats = self.all_formats_var.get()
        s.prefer_rich_metadata = self.prefer_meta_var.get()
        s.collect_metadata = self.collect_meta_var.get()
        s.export_csv = self.export_csv_var.get()
        s.extended_report = self.ext_report_var.get()
        s.sort_by_filename_date = self.sort_fname_var.get()
        s.sort_by_exif_date = self.sort_exif_var.get()
        s.min_dimension = self._safe_int(self.mindim_var, 0)
        s.recursive = self.recursive_var.get()
        s.skip_names = self.skip_names_var.get()
        s.dry_run = self.dry_var.get()
        s.organize_by_date = self.org_date_var.get()
        s.date_folder_format = self.date_fmt_var.get() or "%Y-%m"
        s.details_visible = self._details_var.get()

    def _reset_defaults(self) -> None:
        d = DEFAULTS
        self.thresh_var.set(d.threshold)
        self.ratio_var.set(d.preview_ratio)
        self.series_tol_var.set(d.series_tolerance_pct)
        self.series_thresh_var.set(d.series_threshold_factor)
        self.ar_tol_var.set(d.ar_tolerance_pct)
        self.dark_var.set(d.dark_protection)
        self.dark_thresh_var.set(d.dark_threshold)
        self.dark_factor_var.set(d.dark_tighten_factor)
        self.dual_hash_var.set(d.use_dual_hash)
        self.hist_var.set(d.use_histogram)
        self.hist_sim_var.set(d.hist_min_similarity)
        self.brightness_diff_var.set(d.brightness_max_diff)
        self.ambig_var.set(d.ambiguous_detection)
        self.ambig_factor_var.set(d.ambiguous_threshold_factor)
        self.rawpy_var.set(d.use_rawpy)
        self.strategy_var.set(d.keep_strategy)
        self.all_formats_var.set(d.keep_all_formats)
        self.prefer_meta_var.set(d.prefer_rich_metadata)
        self.collect_meta_var.set(d.collect_metadata)
        self.export_csv_var.set(d.export_csv)
        self.ext_report_var.set(d.extended_report)
        self.sort_fname_var.set(d.sort_by_filename_date)
        self.sort_exif_var.set(d.sort_by_exif_date)
        self.mindim_var.set(d.min_dimension)
        self.recursive_var.set(d.recursive)
        self.skip_names_var.set(d.skip_names)
        self.dry_var.set(d.dry_run)
        self.org_date_var.set(d.organize_by_date)
        self.date_fmt_var.set(d.date_folder_format)
        self._schedule_settings_save()

    @staticmethod
    def _safe_int(var: tk.Variable, default: int) -> int:
        try:
            return int(float(var.get()))
        except Exception:
            return default

    @staticmethod
    def _safe_float(var: tk.Variable, default: float) -> float:
        try:
            return float(var.get())
        except Exception:
            return default

    # ── pre-scan estimate ─────────────────────────────────────────────────

    def _schedule_estimate_update(self) -> None:
        self.root.after(3000, self._update_estimate)

    def _update_estimate(self) -> None:
        src = self.src_var.get().strip()
        if not src or not Path(src).is_dir():
            return

        def _count() -> None:
            try:
                count = 0
                recursive = self.recursive_var.get() if hasattr(self, "recursive_var") else True
                skip_names_raw = self.skip_names_var.get() if hasattr(self, "skip_names_var") else ""
                skip_names_set = {s.strip() for s in skip_names_raw.split(",") if s.strip()}
                src_path = Path(src)

                if recursive:
                    for root, dirs, files in os.walk(src_path):
                        dirs[:] = [d for d in dirs if d not in skip_names_set]
                        for f in files:
                            if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                                count += 1
                else:
                    for f in os.listdir(src_path):
                        if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                            count += 1

                # Time estimate
                hash_time = count * 0.3
                compare_time = count * (count - 1) / 2 * 0.0000005
                extras = 0.0
                try:
                    if self.collect_meta_var.get():
                        extras += hash_time * 0.15
                    if self.rawpy_var.get():
                        extras += hash_time * 0.30
                    if self.dual_hash_var.get():
                        extras += hash_time * 0.05
                except Exception:
                    pass

                total_s = hash_time + compare_time + extras
                if total_s < 60:
                    time_str = f"~{int(total_s)}s"
                elif total_s < 3600:
                    total_s_int = int(total_s)
                    time_str = f"~{total_s_int // 60}m {total_s_int % 60}s"
                else:
                    hrs = int(total_s) // 3600
                    mins = (int(total_s) % 3600) // 60
                    time_str = f"~{hrs}h {mins}m"

                msg = f"Estimated time: {time_str}  \u00b7  {count} images found"
                self.root.after(0, lambda m=msg: self._estimate_var.set(m))
            except Exception:
                pass

        threading.Thread(target=_count, daemon=True).start()

    # ── resume / discard paused state ─────────────────────────────────────

    def _check_resume_state(self) -> None:
        out = self.settings.out_folder
        if not out:
            return
        from scan_state import state_path, load_state
        sp = state_path(Path(out))
        st = load_state(sp)
        if st is None:
            return
        import datetime
        ts = ""
        try:
            mtime = sp.stat().st_mtime
            ts = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        self._paused_state = st
        self._resume_var.set(f"Paused scan found from {ts}.")
        self._resume_lbl.pack(side=tk.LEFT)
        self._resume_btn.pack(side=tk.LEFT, padx=4)
        self._discard_btn.pack(side=tk.LEFT, padx=4)

    def _resume_scan(self) -> None:
        self._resume_lbl.pack_forget()
        self._resume_btn.pack_forget()
        self._discard_btn.pack_forget()
        self._start_scan(resume_state=self._paused_state)

    def _discard_resume(self) -> None:
        self._resume_lbl.pack_forget()
        self._resume_btn.pack_forget()
        self._discard_btn.pack_forget()
        self._paused_state = None
        out = self.settings.out_folder
        if out:
            from scan_state import state_path
            sp = state_path(Path(out))
            if sp.exists():
                sp.unlink()

    # ── scan control ──────────────────────────────────────────────────────

    def _start_scan(self, resume_state=None) -> None:
        self._collect_settings()
        src = self.settings.src_folder.strip()
        out = self.settings.out_folder.strip()
        if not src:
            messagebox.showerror("Error", "Please select a source folder.", parent=self.root)
            return
        if not out:
            messagebox.showerror("Error", "Please select an output folder.", parent=self.root)
            return
        src_path, out_path = Path(src), Path(out)
        if not src_path.is_dir():
            messagebox.showerror("Error", "Source folder does not exist.", parent=self.root)
            return

        self._stop_flag[0] = False
        self._pause_flag[0] = False

        self.scan_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.pause_btn.config(state=tk.NORMAL)
        self.accept_btn.config(state=tk.DISABLED)
        self.browser_report_btn.config(state=tk.DISABLED)
        self.inapp_report_btn.config(state=tk.DISABLED)
        self.revert_all_btn.config(state=tk.DISABLED)
        self.report_path = None
        self.scan_groups = []
        self.scan_records = []

        # Set up phase tracker
        self._tracker = PhaseTracker(PHASE_NAMES)
        self._tracker.start_phase("Discovery", 1)

        self._phase_label_var.set("Phase 1/6: Discovering images...")
        self._progress_bar["value"] = 0
        self._progress_bar["mode"] = "indeterminate"
        self._progress_bar.start(12)

        threading.Thread(
            target=self._worker,
            args=(src_path, out_path, self.settings, resume_state),
            daemon=True,
        ).start()

    def _pause_scan(self) -> None:
        self._pause_flag[0] = True
        self.pause_btn.config(state=tk.DISABLED)
        self._phase_label_var.set("Pausing...")

    def _stop_scan(self) -> None:
        self._stop_flag[0] = True
        self.stop_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.DISABLED)
        self._phase_label_var.set("Stopping...")

    # ── progress callback ─────────────────────────────────────────────────

    def _progress_cb(self, msg: str, done: int, total: int, phase_name: str) -> None:
        """Called from worker thread. Posts updates to main thread via after()."""
        def _update() -> None:
            if self._tracker is None:
                return
            # If this is a new phase, start it
            if self._tracker.current_phase_name != phase_name:
                self._tracker.finish_phase()
                phase_num = PHASE_NAMES.index(phase_name) + 1 if phase_name in PHASE_NAMES else "?"
                self._tracker.start_phase(phase_name, max(total, 1))
                self._phase_label_var.set(f"Phase {phase_num}/{len(PHASE_NAMES)}: {phase_name}...")
                self._progress_bar.stop()
                self._progress_bar["mode"] = "determinate"

            if total > 0:
                self._tracker.update(done)

            pct = self._tracker.total_pct
            self._progress_bar["value"] = pct
            eta = self._tracker.format_eta()
            self._eta_var.set(f"{pct:.0f}%  \u00b7  {eta} remaining  \u00b7  {msg[:80]}")
            self._update_detail_log()

        self.root.after(0, _update)

    def _update_detail_log(self) -> None:
        if not self._details_var.get() or self._tracker is None:
            return
        summaries = self._tracker.phase_summaries
        lines = []
        for s in summaries:
            if s["status"] == "done":
                icon = "✓"
                info = f"{s['total_units']} units  {s['duration_s']:.1f}s"
            elif s["status"] == "active":
                icon = "→"
                info = f"{s['done_units']}/{s['total_units']}  ongoing"
            else:
                icon = "○"
                info = "waiting"
            lines.append(f"{icon} {s['name']:<14} {info}")

        text = "\n".join(lines)
        self._detail_text.config(state=tk.NORMAL)
        self._detail_text.delete("1.0", tk.END)
        self._detail_text.insert("1.0", text)
        self._detail_text.config(state=tk.DISABLED)

    def _toggle_details(self) -> None:
        if self._details_var.get():
            self._detail_text.pack(fill=tk.X, pady=(4, 0))
        else:
            self._detail_text.pack_forget()
        self._save_settings_now()

    # ── worker thread ─────────────────────────────────────────────────────

    def _worker(
        self,
        src: Path,
        out: Path,
        settings: Settings,
        resume_state=None,
    ) -> None:
        def cb(msg: str, done: int, total: int, phase: str) -> None:
            self._progress_cb(msg, done, total, phase)

        try:
            out.mkdir(parents=True, exist_ok=True)
            skip_paths = {
                (out / "results").resolve(),
                (out / "trash").resolve(),
                out.resolve(),
            }

            # ── Phase: Hashing ─────────────────────────────────────────
            if resume_state and resume_state.phase == "comparing":
                # Restore records from state
                from scan_state import deserialize_record
                records = [deserialize_record(r) for r in resume_state.records]
                cb(f"Restored {len(records)} records from paused state.", 0, 0, "Hashing")
            else:
                cb("Discovering images...", 0, 1, "Discovery")
                records = collect_images(
                    src, skip_paths, settings,
                    progress_cb=cb,
                    stop_flag=self._stop_flag,
                    pause_flag=self._pause_flag,
                )

            if self._stop_flag[0]:
                self.root.after(0, lambda: self._on_done("Stopped by user.", success=False))
                return

            if self._pause_flag[0]:
                self._save_pause_state(records, out, settings, compare_i=0, union_parent=[])
                self.root.after(0, lambda: self._on_done("Scan paused.", success=False, paused=True))
                return

            self.scan_records = records

            # ── Phase: Comparing ───────────────────────────────────────
            groups, partial_state = find_groups(
                records, settings,
                progress_cb=cb,
                stop_flag=self._stop_flag,
                pause_flag=self._pause_flag,
                resume_state=resume_state,
            )

            if self._stop_flag[0]:
                self.root.after(0, lambda: self._on_done("Stopped by user.", success=False))
                return

            if self._pause_flag[0] and partial_state is not None:
                # Save paused compare state
                from scan_state import save_state, state_path, serialize_record
                from dataclasses import asdict
                import copy
                partial_state.source_folder = str(src)
                partial_state.output_folder = str(out)
                partial_state.settings_snapshot = asdict(settings)
                if not partial_state.records:
                    partial_state.records = [serialize_record(r) for r in records]
                save_state(partial_state, state_path(out))
                self.root.after(0, lambda: self._on_done("Scan paused.", success=False, paused=True))
                return

            # ── Phase: Metadata ────────────────────────────────────────
            if settings.collect_metadata and groups:
                cb("Saving metadata...", 0, 1, "Metadata")
                from metadata import save_metadata_json, export_metadata_csv
                save_metadata_json(groups, out)
                if settings.export_csv:
                    export_metadata_csv(groups, out)

            # ── Phase: Moving ──────────────────────────────────────────
            if not settings.dry_run and groups:
                cb("Moving files...", 0, len(groups), "Moving")
                move_groups(groups, out, dry_run=False, settings=settings)
            elif settings.dry_run:
                # Still assign group_ids for report even in dry run
                pass

            # ── Phase: Report ──────────────────────────────────────────
            cb("Generating report...", 0, 1, "Report")
            report = generate_report(groups, out, src, len(records), settings)
            self.report_path = report
            self.scan_groups = groups

            # Remove saved state on successful completion
            from scan_state import state_path as _sp
            _sp_file = _sp(out)
            if _sp_file.exists():
                _sp_file.unlink()

            n_orig = sum(len(g.originals) for g in groups)
            n_prev = sum(len(g.previews) for g in groups)
            dry_note = " (DRY RUN)" if settings.dry_run else ""
            msg = (
                f"Done{dry_note}. {len(records)} scanned \u2014 "
                f"{len(groups)} groups, {n_orig} kept, {n_prev} previews."
            )
            self.root.after(0, lambda: self._on_done(msg, success=True, dry_run=settings.dry_run))

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self.root.after(0, lambda e=exc, t=tb: self._on_error(str(e), t))

    def _save_pause_state(
        self, records: list, out: Path, settings: Settings,
        compare_i: int, union_parent: list
    ) -> None:
        from scan_state import ScanState, save_state, state_path, serialize_record
        from dataclasses import asdict
        state = ScanState(
            source_folder=str(self.src_var.get()),
            output_folder=str(out),
            settings_snapshot=asdict(settings),
            phase="hashing",
            records=[serialize_record(r) for r in records],
            compare_i=compare_i,
            union_parent=union_parent,
        )
        save_state(state, state_path(out))

    # ── done / error callbacks ────────────────────────────────────────────

    def _on_done(
        self, msg: str, success: bool = True,
        dry_run: bool = False, paused: bool = False
    ) -> None:
        self._progress_bar.stop()
        self._progress_bar["mode"] = "determinate"
        self._progress_bar["value"] = 100 if success and not paused else self._progress_bar["value"]

        self.scan_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.DISABLED)

        self._phase_label_var.set(msg)
        self._eta_var.set("")

        if success:
            self.browser_report_btn.config(state=tk.NORMAL)
            self.inapp_report_btn.config(state=tk.NORMAL)
            if dry_run:
                self.accept_btn.config(state=tk.NORMAL)
                # Make it obvious what to do next
                self._phase_label_var.set(
                    msg + "  \u2192  Review report, then click \u201cAccept & Move\u201d to apply."
                )
            else:
                out = self.settings.out_folder.strip()
                if out:
                    log_path = ops_log_path(Path(out))
                    if log_path.exists():
                        self.revert_all_btn.config(state=tk.NORMAL)
            # Auto-open report in browser
            if self.report_path:
                webbrowser.open(self.report_path.as_uri())

        if paused:
            self._check_resume_state()

    def _on_error(self, msg: str, tb: str = "") -> None:
        self._progress_bar.stop()
        self._phase_label_var.set("Error — see dialog.")
        self.scan_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.DISABLED)
        detail = f"{msg}\n\n{tb}" if tb else msg
        messagebox.showerror("Error", detail, parent=self.root)

    # ── post-scan actions ─────────────────────────────────────────────────

    def _accept_and_move(self) -> None:
        if not self.scan_groups:
            messagebox.showwarning("Accept & Move", "No groups to move.", parent=self.root)
            return
        out = self.settings.out_folder.strip()
        if not out:
            return
        self.accept_btn.config(state=tk.DISABLED)
        self._phase_label_var.set("Moving files...")

        def _do_move() -> None:
            try:
                moved_orig, moved_prev = move_groups(
                    self.scan_groups, Path(out), dry_run=False,
                    settings=self.settings
                )
                # Re-generate report with updated paths
                report = generate_report(
                    self.scan_groups, Path(out),
                    Path(self.settings.src_folder),
                    len(self.scan_records), self.settings
                )
                self.report_path = report
                msg = f"Moved {moved_orig} originals + {moved_prev} previews."
                self.root.after(0, lambda: self._phase_label_var.set(msg))
                self.root.after(0, lambda: self.revert_all_btn.config(state=tk.NORMAL))
                if report:
                    self.root.after(0, lambda: webbrowser.open(report.as_uri()))
            except Exception as exc:
                self.root.after(0, lambda e=exc: messagebox.showerror("Error", str(e), parent=self.root))

        threading.Thread(target=_do_move, daemon=True).start()

    def _open_browser_report(self) -> None:
        if self.report_path and self.report_path.exists():
            webbrowser.open(self.report_path.as_uri())

    def _open_inapp_report(self) -> None:
        if not self.scan_groups:
            messagebox.showinfo("Review", "No groups to review.", parent=self.root)
            return
        out = self.settings.out_folder.strip()
        log_path = ops_log_path(Path(out)) if out else None

        def _apply_cb(groups: list) -> None:
            move_groups(groups, Path(out), dry_run=False, settings=self.settings)
            report = generate_report(
                self.scan_groups, Path(out),
                Path(self.settings.src_folder),
                len(self.scan_records), self.settings
            )
            self.report_path = report

        viewer = ReportViewer(
            self.root,
            self.scan_groups,
            ops_log_path=log_path,
            on_apply_cb=_apply_cb,
        )
        viewer.grab_set()

    def _revert_all(self) -> None:
        out = self.settings.out_folder.strip()
        if not out:
            return
        log_path = ops_log_path(Path(out))
        if not log_path.exists():
            messagebox.showinfo("Revert", "No operations log found.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Revert All",
            "Move all files back to their original locations?\nThis cannot be undone.",
            parent=self.root
        ):
            return

        def _do() -> None:
            from mover import revert_operations
            reverted, errors = revert_operations(log_path)
            msg = f"Reverted {reverted} files."
            if errors:
                msg += f" ({errors} errors)"
            self.root.after(0, lambda: self._phase_label_var.set(msg))

        threading.Thread(target=_do, daemon=True).start()

    # ── rawpy installer ───────────────────────────────────────────────────

    def _install_rawpy(self) -> None:
        """Run 'pip install rawpy' in a background thread and report the result."""
        import subprocess
        win = tk.Toplevel(self.root)
        win.title("Installing rawpy...")
        win.geometry("480x220")
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text="Installing rawpy via pip...",
                  font=("Segoe UI", 10, "bold")).pack(pady=(18, 6))
        log = tk.Text(win, height=6, state=tk.DISABLED,
                      font=("Consolas", 8), relief=tk.FLAT, bg="#f4f4f4")
        log.pack(fill=tk.BOTH, expand=True, padx=12)
        close_btn = ttk.Button(win, text="Close", state=tk.DISABLED,
                               command=win.destroy)
        close_btn.pack(pady=8)

        def _append(text: str) -> None:
            log.config(state=tk.NORMAL)
            log.insert(tk.END, text)
            log.see(tk.END)
            log.config(state=tk.DISABLED)

        def _run() -> None:
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "rawpy"],
                    capture_output=True, text=True
                )
                out = (proc.stdout + proc.stderr).strip()
                success = proc.returncode == 0
            except Exception as exc:
                out = str(exc)
                success = False

            def _done() -> None:
                _append(out + "\n")
                if success:
                    _append("\nrawpy installed! Restart the app to enable RAW support.\n")
                else:
                    _append("\nInstallation failed. Check your Python/pip setup.\n")
                close_btn.config(state=tk.NORMAL)

            win.after(0, _done)

        threading.Thread(target=_run, daemon=True).start()


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
