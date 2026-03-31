"""
main.py — Image Deduper v2 GUI
Tab-based UI: Scan, Results (dynamic), History, Settings.
"""
from __future__ import annotations

import datetime
import json
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
HISTORY_PATH  = Path(__file__).parent / "scan_history.json"
PHASE_NAMES   = ["Discovery", "Hashing", "Comparing", "Metadata", "Moving", "Report"]

_ACCENT         = "#1565C0"   # Blue 800
_ACCENT_DARK    = "#0D47A1"   # Blue 900
_ACCENT_TINT    = "#E3F2FD"   # Blue 50
_BG             = "#F5F5F5"   # Grey 100
_CARD_BG        = "#FFFFFF"   # Surface white
_M_SUCCESS      = "#2E7D32"   # Green 800
_M_ERROR        = "#C62828"   # Red 800
_M_WARNING      = "#E65100"   # Deep Orange 900
_M_AMBER        = "#F57F17"   # Amber 900
_M_DIVIDER      = "#E0E0E0"   # Grey 300
_M_TEXT1        = "#212121"   # Grey 900
_M_TEXT2        = "#616161"   # Grey 700
_MAT_DISABLED   = "#BDBDBD"   # Grey 400


def _darken_color(hex_color: str) -> str:
    try:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
        f = 0.85
        return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"
    except Exception:
        return hex_color


def _mat_btn(parent, text, command, bg, fg="#FFFFFF", font_size=9, **kw) -> tk.Button:
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=_darken_color(bg), activeforeground=fg,
        relief=tk.FLAT, bd=0, padx=12, pady=5,
        font=("Segoe UI", font_size, "bold"), cursor="hand2", **kw,
    )
    btn._mat_bg = bg

    def _enter(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=_darken_color(btn._mat_bg))

    def _leave(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=btn._mat_bg)

    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)
    return btn


def _mat_enable(btn: tk.Button) -> None:
    btn.configure(state=tk.NORMAL, bg=btn._mat_bg, cursor="hand2")


def _mat_disable(btn: tk.Button) -> None:
    btn.configure(state=tk.DISABLED, bg=_MAT_DISABLED, cursor="")


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
    win.geometry("480x280")
    win.grab_set()
    win.resizable(False, False)
    win.configure(bg=_CARD_BG)
    tk.Label(win, text=title, font=("Segoe UI", 11, "bold"),
             bg=_CARD_BG, fg=_M_TEXT1).pack(anchor=tk.W, padx=16, pady=(14, 4))
    tk.Frame(win, height=1, bg=_M_DIVIDER).pack(fill=tk.X, padx=16, pady=(0, 8))
    txt = tk.Text(win, wrap=tk.WORD, padx=14, pady=8, relief=tk.FLAT,
                  bg=_CARD_BG, fg=_M_TEXT2, font=("Segoe UI", 9))
    txt.insert("1.0", text)
    txt.config(state=tk.DISABLED)
    txt.pack(fill=tk.BOTH, expand=True, padx=4)
    _mat_btn(win, "Close", win.destroy, _ACCENT).pack(pady=10)


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
    line = text.split("\n")[0].strip()
    if "." in line:
        return line[: line.index(".") + 1]
    return line[:100]


def _scrollable_frame(parent: tk.Widget):
    """Return (outer_frame, body_frame) where body is inside a scrollable canvas."""
    outer = tk.Frame(parent)
    outer.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(outer, bg=_BG, highlightthickness=0)
    sb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    body = ttk.Frame(canvas, padding=(14, 8, 14, 8))
    bw = canvas.create_window((0, 0), window=body, anchor=tk.NW)
    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(bw, width=e.width))

    def _on_mw(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", _on_mw))
    canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

    return outer, body


# ── main application ──────────────────────────────────────────────────────────

class App:
    # Date format helpers
    _DATE_ORDER_TEMPLATES = [
        "%Y{s}%m{s}%d",
        "%d{s}%m{s}%Y",
        "%m{s}%d{s}%Y",
        "%Y{s}%m",
        "%Y",
    ]
    _DATE_SEPARATORS = ["-", "/", ".", "_", " "]

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Image Deduper v2")
        self.root.geometry("860x640")
        self.root.resizable(True, True)
        self.root.minsize(700, 520)

        try:
            self._icon = _make_icon(64)
            root.wm_iconphoto(True, self._icon)
        except Exception:
            pass

        self.settings = load_settings(SETTINGS_PATH)
        self._scan_history: list[dict] = self._load_scan_history()

        # Scan state
        self.report_path: Path | None = None
        self.scan_groups: list = []
        self.scan_records: list = []
        self._broken_files: list = []
        self._solo_originals: list = []
        self._stop_flag: list[bool] = [False]
        self._pause_flag: list[bool] = [False]
        self._paused_state = None
        self._save_after_id = None
        self._tracker: PhaseTracker | None = None
        self._last_scan_info: dict = {}
        self._results_tab_visible        = False

        # Custom scan state
        self._custom_stop_flag:  list[bool] = [False]
        self._custom_pause_flag: list[bool] = [False]
        self._custom_groups:     list = []
        self._custom_broken:     list = []
        self._custom_report_path: Path | None = None
        self._scanning = False   # prevents concurrent scans

        self._build_ui()
        self._check_resume_state()
        self._check_last_results()
        self._schedule_estimate_update()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self.root, bg=_ACCENT)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Image Deduper v2",
                 font=("Segoe UI", 15, "bold"), bg=_ACCENT, fg="white").pack(
            side=tk.LEFT, padx=20, pady=12)
        tk.Label(hdr, text="Find & remove duplicate preview images",
                 font=("Segoe UI", 9), bg=_ACCENT, fg="#90CAF9").pack(side=tk.LEFT)

        # Notebook style — white selected tab with blue text works reliably on Windows
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        try:
            style.configure("App.TNotebook", background=_BG)
            style.configure("App.TNotebook.Tab",
                            font=("Segoe UI", 9, "bold"), padding=[14, 6])
            style.map("App.TNotebook.Tab",
                      background=[("selected", _CARD_BG), ("!selected", _ACCENT_TINT)],
                      foreground=[("selected", _ACCENT), ("!selected", _M_TEXT2)])
        except Exception:
            pass

        self._nb = ttk.Notebook(self.root, style="App.TNotebook")
        self._nb.pack(fill=tk.BOTH, expand=True)

        self._tab_scan     = ttk.Frame(self._nb)
        self._tab_results  = ttk.Frame(self._nb)
        self._tab_custom   = ttk.Frame(self._nb)
        self._tab_history  = ttk.Frame(self._nb)
        self._tab_settings = ttk.Frame(self._nb)

        self._nb.add(self._tab_scan,     text="  Scan  ")
        self._nb.add(self._tab_custom,   text="  Custom Scan  ")
        self._nb.add(self._tab_history,  text="  History  ")
        self._nb.add(self._tab_settings, text="  Settings  ")

        # Results tab inserted dynamically at position 1 after a scan completes
        self._results_tab_visible = False

        # Init all shared vars before building tabs
        self._init_setting_vars()

        self._build_scan_tab()
        self._build_results_tab_content()
        self._build_custom_scan_tab()
        self._build_history_tab()
        self._build_settings_tab()

    def _init_setting_vars(self) -> None:
        """Create all tkinter data vars from current settings. Called once before building tabs."""
        s = self.settings
        self._mode_var          = tk.StringVar(value=s.mode)
        self.thresh_var         = tk.DoubleVar(value=s.threshold)
        self.ratio_var          = tk.DoubleVar(value=s.preview_ratio)
        self.series_tol_var     = tk.DoubleVar(value=s.series_tolerance_pct)
        self.series_thresh_var  = tk.DoubleVar(value=s.series_threshold_factor)
        self.ar_tol_var         = tk.DoubleVar(value=s.ar_tolerance_pct)
        self.dark_var           = tk.BooleanVar(value=s.dark_protection)
        self.dark_thresh_var    = tk.DoubleVar(value=s.dark_threshold)
        self.dark_factor_var    = tk.DoubleVar(value=s.dark_tighten_factor)
        self.dual_hash_var      = tk.BooleanVar(value=s.use_dual_hash)
        self.hist_var           = tk.BooleanVar(value=s.use_histogram)
        self.hist_sim_var       = tk.DoubleVar(value=s.hist_min_similarity)
        self.brightness_diff_var = tk.DoubleVar(value=s.brightness_max_diff)
        self.ambig_var          = tk.BooleanVar(value=s.ambiguous_detection)
        self.ambig_factor_var   = tk.DoubleVar(value=s.ambiguous_threshold_factor)
        self.disable_series_var = tk.BooleanVar(value=s.disable_series_detection)
        self.rawpy_var          = tk.BooleanVar(value=s.use_rawpy)
        self.strategy_var       = tk.StringVar(value=s.keep_strategy)
        self.all_formats_var    = tk.BooleanVar(value=s.keep_all_formats)
        self.prefer_meta_var    = tk.BooleanVar(value=s.prefer_rich_metadata)
        self.collect_meta_var   = tk.BooleanVar(value=s.collect_metadata)
        self.export_csv_var     = tk.BooleanVar(value=s.export_csv)
        self.ext_report_var     = tk.BooleanVar(value=s.extended_report)
        self.sort_fname_var     = tk.BooleanVar(value=s.sort_by_filename_date)
        self.sort_exif_var      = tk.BooleanVar(value=s.sort_by_exif_date)
        self.mindim_var         = tk.DoubleVar(value=s.min_dimension)
        self.recursive_var      = tk.BooleanVar(value=s.recursive)
        self.skip_names_var     = tk.StringVar(value=s.skip_names)
        self.dry_var            = tk.BooleanVar(value=s.dry_run)
        self.org_date_var       = tk.BooleanVar(value=s.organize_by_date)
        self._details_var       = tk.BooleanVar(value=s.details_visible)
        self._phase_label_var   = tk.StringVar(value="Ready.")
        self._eta_var           = tk.StringVar(value="")
        self._estimate_var      = tk.StringVar(value="Select a source folder to see estimate.")
        self._resume_var        = tk.StringVar(value="")
        self._results_info_var  = tk.StringVar(value="")

        # Date format vars
        init_order_idx, init_sep = self._guess_order_sep(s.date_folder_format)
        self._date_fmt_var_hidden = tk.StringVar(value=s.date_folder_format)
        self.date_fmt_var         = self._date_fmt_var_hidden
        self._date_order_var      = tk.StringVar()
        self._date_order_idx_val  = init_order_idx
        self._date_sep_var        = tk.StringVar(value=init_sep)
        self._date_fmt_example    = tk.StringVar()

        # Custom scan folder vars
        s2 = self.settings
        self._custom_main_var    = tk.StringVar(value=s2.custom_main_folder)
        self._custom_check_var   = tk.StringVar(value=s2.custom_check_folder)
        self._custom_out_var     = tk.StringVar(value=s2.custom_out_folder or s2.out_folder)
        self._custom_phase_label = tk.StringVar(value="Ready.")
        self._custom_eta_var     = tk.StringVar(value="")
        self._custom_estimate_var = tk.StringVar(value="Select folders to see estimate.")
        self._custom_dry_var     = tk.BooleanVar(value=s2.dry_run)
        self._custom_dry_var.trace_add("write", self._on_setting_change)

        # Add traces
        for var in (
            self.thresh_var, self.ratio_var, self.series_tol_var, self.series_thresh_var,
            self.ar_tol_var, self.dark_thresh_var, self.dark_factor_var, self.hist_sim_var,
            self.brightness_diff_var, self.ambig_factor_var, self.mindim_var,
        ):
            var.trace_add("write", lambda *_: self._on_setting_change())

        for var in (
            self.dark_var, self.dual_hash_var, self.hist_var, self.ambig_var,
            self.disable_series_var, self.rawpy_var, self.all_formats_var,
            self.prefer_meta_var, self.collect_meta_var, self.export_csv_var,
            self.ext_report_var, self.sort_fname_var, self.sort_exif_var,
            self.recursive_var, self.dry_var, self.org_date_var,
        ):
            var.trace_add("write", self._on_setting_change)

        self.strategy_var.trace_add("write", self._on_setting_change)
        self.skip_names_var.trace_add("write", self._on_setting_change)
        self._date_sep_var.trace_add("write", self._on_date_sep_change)
        self._date_order_var.trace_add("write", self._on_date_fmt_change)

    # ── Scan tab ──────────────────────────────────────────────────────────

    def _build_scan_tab(self) -> None:
        tab = self._tab_scan

        # Scrollable body
        _, body = _scrollable_frame(tab)

        # Folders
        folders = _section(body, "Folders")
        self.src_var = self._folder_row(folders, "Source folder:", "src_folder")
        self.out_var = self._folder_row(folders, "Output folder:", "out_folder")

        # Mode toggle
        mode_card = ttk.LabelFrame(body, text="Mode", padding=(10, 6, 10, 8))
        mode_card.pack(fill=tk.X, pady=(0, 6))
        mode_row = ttk.Frame(mode_card)
        mode_row.pack(fill=tk.X)
        for val, lbl in (("quick", "Quick"), ("advanced", "Advanced")):
            rb = tk.Radiobutton(
                mode_row, text=lbl, variable=self._mode_var, value=val,
                bg=_ACCENT_TINT, font=("Segoe UI", 9, "bold"),
                indicatoron=False, width=12, relief=tk.FLAT,
                command=self._on_mode_change,
                selectcolor=_ACCENT,
            )
            rb.pack(side=tk.LEFT, padx=2)

        # Compact key settings (advanced mode only)
        self._compact_adv_frame = ttk.LabelFrame(body, text="Key Settings", padding=(10, 6, 10, 8))
        _crows = [ttk.Frame(self._compact_adv_frame) for _ in range(3)]
        for cr in _crows:
            cr.pack(fill=tk.X, pady=2)

        ttk.Checkbutton(_crows[0], text="Ambiguous Match Detection",
                        variable=self.ambig_var).pack(side=tk.LEFT)
        _info_btn(_crows[0], "ambiguous_detection").pack(side=tk.LEFT, padx=2)
        ttk.Label(_crows[0], text="  ", width=3).pack(side=tk.LEFT)
        ttk.Checkbutton(_crows[0], text="Disable Series Detection",
                        variable=self.disable_series_var).pack(side=tk.LEFT)

        ttk.Checkbutton(_crows[1], text="Scan subfolders recursively",
                        variable=self.recursive_var).pack(side=tk.LEFT)
        _info_btn(_crows[1], "recursive").pack(side=tk.LEFT, padx=2)
        ttk.Label(_crows[1], text="  ", width=3).pack(side=tk.LEFT)
        self._compact_rawpy_cb = ttk.Checkbutton(
            _crows[1], text="Use rawpy for RAW files", variable=self.rawpy_var,
            state=tk.NORMAL if _RAWPY_AVAILABLE else tk.DISABLED,
        )
        self._compact_rawpy_cb.pack(side=tk.LEFT)
        if not _RAWPY_AVAILABLE:
            ttk.Label(_crows[1], text="(not installed)", foreground="#e03",
                      font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=2)

        ttk.Label(_crows[2], text="Prefer to keep:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Radiobutton(_crows[2], text="Largest resolution",
                        variable=self.strategy_var, value="pixels").pack(side=tk.LEFT)
        ttk.Radiobutton(_crows[2], text="Oldest file date",
                        variable=self.strategy_var, value="oldest").pack(side=tk.LEFT, padx=6)
        _info_btn(_crows[2], "keep_strategy").pack(side=tk.LEFT, padx=2)

        # Actions
        act = _section(body, "Actions")

        r = _row(act)
        ttk.Checkbutton(r, text="Dry Run (recommended)", variable=self.dry_var).pack(side=tk.LEFT)
        _info_btn(r, "dry_run").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="Scan & report only — no files moved.",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)

        r = _row(act)
        ttk.Checkbutton(r, text="Organize by Date", variable=self.org_date_var).pack(side=tk.LEFT)
        _info_btn(r, "organize_by_date").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="Create date subfolders in results/ and trash/",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)

        # Date format
        r = _row(act)
        ttk.Label(r, text="  Date order:", width=12, anchor=tk.W).pack(side=tk.LEFT)
        init_order_idx, init_sep = self._guess_order_sep(self.settings.date_folder_format)
        self._date_order_cb = ttk.Combobox(r, textvariable=self._date_order_var,
                                           width=14, state="readonly")
        self._date_order_cb.pack(side=tk.LEFT)
        ttk.Label(r, text="  Separator:").pack(side=tk.LEFT, padx=(8, 0))
        self._date_sep_cb = ttk.Combobox(r, textvariable=self._date_sep_var,
                                         values=self._DATE_SEPARATORS, width=4, state="readonly")
        self._date_sep_cb.pack(side=tk.LEFT, padx=(2, 0))
        _info_btn(r, "date_folder_format").pack(side=tk.LEFT, padx=4)
        ttk.Label(r, textvariable=self._date_fmt_example,
                  foreground="#555", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)
        self._refresh_date_order_choices(init_sep, init_order_idx)

        # Estimate
        self._estimate_frame = ttk.Frame(body)
        self._estimate_frame.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(self._estimate_frame, textvariable=self._estimate_var,
                  foreground="#555", font=("Segoe UI", 8, "italic")).pack(anchor=tk.W)

        # Resume notice
        self._resume_frame = ttk.Frame(body)
        self._resume_frame.pack(fill=tk.X, pady=(2, 2))
        self._resume_lbl = ttk.Label(
            self._resume_frame, textvariable=self._resume_var,
            foreground="#7c3aed", font=("Segoe UI", 8, "bold"))
        self._resume_lbl.pack(side=tk.LEFT)
        self._resume_btn  = ttk.Button(self._resume_frame, text="Resume",  command=self._resume_scan)
        self._discard_btn = ttk.Button(self._resume_frame, text="Discard", command=self._discard_resume)

        # Progress panel (fixed, bottom of tab)
        self._prog_frame = ttk.LabelFrame(tab, text="Progress", padding=(8, 4, 8, 6))
        self._prog_frame.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Label(self._prog_frame, textvariable=self._phase_label_var,
                  font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        self._progress_bar = ttk.Progressbar(self._prog_frame, mode="determinate", maximum=100)
        self._progress_bar.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(self._prog_frame, textvariable=self._eta_var,
                  foreground="#555", font=("Segoe UI", 8)).pack(anchor=tk.W)
        ttk.Checkbutton(
            self._prog_frame, text="Show phase details",
            variable=self._details_var, command=self._toggle_details,
        ).pack(anchor=tk.W, pady=(2, 0))
        self._detail_text = tk.Text(
            self._prog_frame, height=7, state=tk.DISABLED,
            font=("Consolas", 8), bg="#f8f8f8", relief=tk.FLAT,
        )

        # Button bar (fixed, very bottom of tab)
        btn_bar = tk.Frame(tab, bg=_ACCENT_TINT, pady=7)
        btn_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(btn_bar, height=1, bg=_M_DIVIDER).place(relx=0, rely=0, relwidth=1)

        _GR = "#757575"

        # Idle frame: shown when not scanning
        self._scan_idle_frame = tk.Frame(btn_bar, bg=_ACCENT_TINT)
        self._scan_idle_frame.pack(fill=tk.X, padx=4)

        _mat_btn(self._scan_idle_frame, "Reset Defaults",
                 self._reset_defaults, _GR).pack(side=tk.LEFT, padx=(4, 4))

        self._scan_last_calib_btn = _mat_btn(
            self._scan_idle_frame, "↩ Last Calibration",
            self._apply_last_calibration, _ACCENT)
        self._scan_last_calib_btn.pack(side=tk.LEFT, padx=4)
        if self.settings.calibrated_threshold == 0:
            _mat_disable(self._scan_last_calib_btn)

        self.scan_btn = _mat_btn(self._scan_idle_frame, "▶  Start Scan",
                                 self._start_scan, _M_SUCCESS)
        self.scan_btn.pack(side=tk.RIGHT, padx=(4, 8))

        # Active frame: shown while scanning
        self._scan_active_frame = tk.Frame(btn_bar, bg=_ACCENT_TINT)
        # Not packed initially

        self.stop_btn = _mat_btn(self._scan_active_frame, "■  Stop",
                                 self._stop_scan, _M_ERROR)
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 4))

        self.pause_btn = _mat_btn(self._scan_active_frame, "⏸  Pause",
                                  self._pause_scan, _M_AMBER)
        self.pause_btn.pack(side=tk.LEFT, padx=4)

        self._apply_mode()

    # ── Results tab ───────────────────────────────────────────────────────

    def _build_results_tab_content(self) -> None:
        tab = self._tab_results

        # Info card
        info_card = tk.Frame(tab, bg=_CARD_BG, bd=1, relief=tk.FLAT)
        info_card.pack(fill=tk.X, padx=16, pady=(16, 8))
        tk.Frame(info_card, height=3, bg=_ACCENT).pack(fill=tk.X)
        tk.Label(
            info_card, textvariable=self._results_info_var,
            bg=_CARD_BG, fg=_M_TEXT1, font=("Segoe UI", 9),
            justify=tk.LEFT, wraplength=700, padx=14, pady=10,
            anchor=tk.W,
        ).pack(fill=tk.X)

        # Action buttons row
        btn_row = tk.Frame(tab, bg=_BG)
        btn_row.pack(fill=tk.X, padx=16, pady=6)

        self.revert_all_btn = _mat_btn(btn_row, "⟲  Revert All", self._revert_all, _M_WARNING)
        self.revert_all_btn.pack(side=tk.LEFT, padx=(0, 6))
        _mat_disable(self.revert_all_btn)

        self.inapp_report_btn = _mat_btn(btn_row, "Review In-App",
                                         self._open_inapp_report, _ACCENT)
        self.inapp_report_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self.inapp_report_btn)

        self.browser_report_btn = _mat_btn(btn_row, "Browser Report",
                                           self._open_browser_report, "#757575")
        self.browser_report_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self.browser_report_btn)

        self.accept_btn = _mat_btn(btn_row, "✓  Accept & Move",
                                   self._accept_and_move, _M_SUCCESS)
        self.accept_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self.accept_btn)

        # Divider
        tk.Frame(tab, height=1, bg=_M_DIVIDER).pack(fill=tk.X, padx=16, pady=14)

        # Start New Scan
        new_frame = tk.Frame(tab, bg=_BG)
        new_frame.pack(pady=10)
        _mat_btn(new_frame, "   +  Start New Scan   ",
                 self._new_scan_prompt, _ACCENT, font_size=11).pack()
        ttk.Label(new_frame, text="Clears current results and returns to the Scan tab.",
                  foreground="#888", font=("Segoe UI", 8)).pack(pady=(6, 0))

    def _show_results_tab(self) -> None:
        if not self._results_tab_visible:
            self._nb.insert(1, self._tab_results, text="  Results  ")
            self._results_tab_visible = True

    def _hide_results_tab(self) -> None:
        if self._results_tab_visible:
            self._nb.forget(self._tab_results)
            self._results_tab_visible = False

    def _update_results_tab_ui(self, info: "dict | None" = None) -> None:
        """Refresh the info card text and button states on the Results tab."""
        if info:
            self._last_scan_info = info
        i = self._last_scan_info
        if not i:
            self._results_info_var.set("No scan results available.")
            return

        ts       = i.get("date", "")
        src      = i.get("src_folder", "")
        files    = i.get("total_files", 0)
        groups   = i.get("groups", 0)
        dups     = i.get("duplicates", 0)
        dup_pct  = i.get("dup_pct", 0.0)
        dry_run  = i.get("dry_run", True)
        applied  = i.get("applied", False)

        dry_tag  = "  [DRY RUN — files not moved]" if dry_run else ""
        appl_tag = "  ✓ Results applied." if applied else ""

        lines = [
            f"Scan:   {ts}{dry_tag}{appl_tag}",
            f"Source: {src}",
            f"Files scanned: {files}   ·   Groups: {groups}   ·   "
            f"Duplicates: {dups}   ({dup_pct:.1f}%)",
        ]
        self._results_info_var.set("\n".join(lines))

    # ── History tab ───────────────────────────────────────────────────────

    def _build_history_tab(self) -> None:
        tab = self._tab_history

        header = tk.Frame(tab, bg=_BG)
        header.pack(fill=tk.X, padx=8, pady=(10, 4))
        tk.Label(header, text="Scan History", font=("Segoe UI", 10, "bold"),
                 bg=_BG, fg=_M_TEXT1).pack(side=tk.LEFT)

        cols = ("date", "src", "files", "groups", "dups", "dup_pct", "dry_run", "applied")
        self._hist_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="browse", height=16)

        col_cfg = [
            ("date",    "Date",      130, "w"),
            ("src",     "Source",    220, "w"),
            ("files",   "Files",      60, "center"),
            ("groups",  "Groups",     60, "center"),
            ("dups",    "Dups",       60, "center"),
            ("dup_pct", "Dup %",      60, "center"),
            ("dry_run", "Dry Run",    65, "center"),
            ("applied", "Applied",    65, "center"),
        ]
        for cid, head, w, anch in col_cfg:
            self._hist_tree.heading(cid, text=head)
            self._hist_tree.column(cid, width=w, anchor=anch)

        vsb = ttk.Scrollbar(tab, orient="vertical", command=self._hist_tree.yview)
        self._hist_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)
        self._hist_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        btn_bar = tk.Frame(tab, bg=_BG)
        btn_bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        _mat_btn(btn_bar, "Clear History", self._clear_history, "#757575").pack(side=tk.LEFT)

        self._refresh_history_view()

    def _refresh_history_view(self) -> None:
        if not hasattr(self, "_hist_tree"):
            return
        for item in self._hist_tree.get_children():
            self._hist_tree.delete(item)
        for entry in reversed(self._scan_history):
            self._hist_tree.insert("", "end", values=(
                entry.get("date", ""),
                entry.get("src_folder", ""),
                entry.get("total_files", ""),
                entry.get("groups", ""),
                entry.get("duplicates", ""),
                f"{entry.get('dup_pct', 0):.1f}%",
                "Yes" if entry.get("dry_run") else "No",
                "Yes" if entry.get("applied") else "No",
            ))

    def _load_scan_history(self) -> list[dict]:
        try:
            if HISTORY_PATH.exists():
                return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_scan_history(self) -> None:
        try:
            HISTORY_PATH.write_text(
                json.dumps(self._scan_history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _log_scan_history(
        self, total_files: int, groups: int, duplicates: int,
        dry_run: bool, src_folder: str, applied: bool = False,
    ) -> None:
        dup_pct = duplicates / total_files * 100 if total_files > 0 else 0.0
        entry = {
            "date":        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "src_folder":  src_folder,
            "total_files": total_files,
            "groups":      groups,
            "duplicates":  duplicates,
            "dup_pct":     round(dup_pct, 2),
            "dry_run":     dry_run,
            "applied":     applied,
        }
        self._scan_history.append(entry)
        self._save_scan_history()
        self._refresh_history_view()
        self._last_scan_info = entry

    def _clear_history(self) -> None:
        if not messagebox.askyesno("Clear History",
                                   "Remove all history entries?", parent=self.root):
            return
        self._scan_history.clear()
        self._save_scan_history()
        self._refresh_history_view()

    # ── Settings tab ──────────────────────────────────────────────────────

    def _build_settings_tab(self) -> None:
        tab = self._tab_settings
        _, body = _scrollable_frame(tab)

        # Calibration
        calib_sec = _section(body, "Calibration")
        calib_btn_row = ttk.Frame(calib_sec)
        calib_btn_row.pack(fill=tk.X, pady=(2, 4))

        _mat_btn(calib_btn_row, "⚙  Calibrate Detection Settings…",
                 self._open_calibration, _ACCENT, font_size=10).pack(side=tk.LEFT)

        self._calib_apply_btn = _mat_btn(
            calib_btn_row, "↩  Apply Last Calibration",
            self._apply_last_calibration, _ACCENT)
        self._calib_apply_btn.pack(side=tk.LEFT, padx=10)
        if self.settings.calibrated_threshold == 0:
            _mat_disable(self._calib_apply_btn)

        ttk.Label(calib_sec,
                  text="Calibration finds the best threshold and ratio for your specific photo library.",
                  foreground="#666", font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(0, 2))

        # ── Detection ────────────────────────────────────────────────────
        det = _section(body, "Detection")

        self._slider_row(det, "Similarity Threshold", self.thresh_var,
                         1, 30, 8, 12, 12, "threshold", 1,
                         lambda v: str(int(round(v))))

        self._slider_row(det, "Preview Size Ratio (per-dimension)", self.ratio_var,
                         0.50, 0.99, 0.85, 0.95, 0.90, "preview_ratio", 0.01,
                         lambda v: f"{v:.2f}")

        r = _row(det)
        ttk.Checkbutton(r, text="Ambiguous Match Detection",
                        variable=self.ambig_var).pack(side=tk.LEFT)
        _info_btn(r, "ambiguous_detection").pack(side=tk.LEFT, padx=2)
        self._slider_row(det, "  Ambiguous Threshold Factor", self.ambig_factor_var,
                         1.0, 3.0, 1.3, 2.0, 1.5, "ambiguous_threshold_factor", 0.1,
                         lambda v: f"{v:.1f}\u00d7")

        r = _row(det)
        self.disable_series_cb = ttk.Checkbutton(
            r, text="Disable Series Detection", variable=self.disable_series_var)
        self.disable_series_cb.pack(side=tk.LEFT)
        ttk.Label(r, text="Treat all same-size duplicates normally instead of keeping burst shots",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)

        # ── Keep Strategy ────────────────────────────────────────────────
        keep = _section(body, "Keep Strategy")

        r = _row(keep)
        _label(r, "Prefer to keep:")
        ttk.Radiobutton(r, text="Largest resolution",
                        variable=self.strategy_var, value="pixels").pack(side=tk.LEFT)
        ttk.Radiobutton(r, text="Oldest file date",
                        variable=self.strategy_var, value="oldest").pack(side=tk.LEFT, padx=6)
        _info_btn(r, "keep_strategy").pack(side=tk.LEFT, padx=2)

        r = _row(keep)
        ttk.Checkbutton(r, text="Keep all formats (best per extension)",
                        variable=self.all_formats_var).pack(side=tk.LEFT)
        _info_btn(r, "keep_all_formats").pack(side=tk.LEFT, padx=2)

        r = _row(keep)
        ttk.Checkbutton(r, text="Prefer image with richer EXIF metadata",
                        variable=self.prefer_meta_var).pack(side=tk.LEFT)
        _info_btn(r, "prefer_rich_metadata").pack(side=tk.LEFT, padx=2)

        # ── Filters ──────────────────────────────────────────────────────
        filt = _section(body, "Filters")

        r = _row(filt)
        ttk.Checkbutton(r, text="Scan subfolders recursively",
                        variable=self.recursive_var).pack(side=tk.LEFT)
        _info_btn(r, "recursive").pack(side=tk.LEFT, padx=2)

        r = _row(filt)
        _label(r, "Skip folder names:")
        ttk.Entry(r, textvariable=self.skip_names_var, width=36).pack(side=tk.LEFT)
        _info_btn(r, "skip_names").pack(side=tk.LEFT, padx=2)

        self._slider_row(filt, "Minimum Dimension Filter (px)", self.mindim_var,
                         0, 2000, 100, 300, 0, "min_dimension", 50,
                         lambda v: f"{int(round(v))} px" if v > 0 else "off")

        # ── Show Advanced toggle ──────────────────────────────────────────
        self._advanced_frames: list[tk.Widget] = []
        self._all_settings_visible = False

        self._show_all_btn = ttk.Button(
            body, text="▼  Show Advanced Settings",
            command=self._toggle_show_all,
        )
        self._show_all_btn.pack(anchor=tk.W, padx=2, pady=(4, 6))

        def _adv(title: str) -> ttk.LabelFrame:
            f = ttk.LabelFrame(body, text=title, padding=(10, 6, 10, 8))
            self._advanced_frames.append(f)
            return f

        # Advanced: Series detection
        series_sec = _adv("Series Detection")

        self._slider_row(series_sec, "Series Dimension Tolerance %", self.series_tol_var,
                         0.0, 10.0, 0.0, 2.0, 0.0, "series_tolerance_pct", 0.1,
                         lambda v: f"{v:.1f}%")
        self._slider_row(series_sec, "Series Grouping Leniency", self.series_thresh_var,
                         1.0, 5.0, 1.5, 2.5, 2.0, "series_threshold_factor", 0.1,
                         lambda v: f"{v:.1f}\u00d7")

        # Advanced: Hash options
        hash_sec = _adv("Hash & Match Options")

        self._slider_row(hash_sec, "Aspect Ratio Tolerance %", self.ar_tol_var,
                         0.0, 20.0, 3.0, 8.0, 5.0, "ar_tolerance_pct", 0.5,
                         lambda v: f"{v:.1f}%")

        r = _row(hash_sec)
        ttk.Checkbutton(r, text="Dark Image Protection",
                        variable=self.dark_var).pack(side=tk.LEFT)
        _info_btn(r, "dark_protection").pack(side=tk.LEFT, padx=2)
        self._slider_row(hash_sec, "  Dark Image Threshold (brightness 0–255)",
                         self.dark_thresh_var,
                         0.0, 128.0, 30.0, 50.0, 40.0, "dark_threshold", 1.0,
                         lambda v: f"{int(round(v))}")
        self._slider_row(hash_sec, "  Dark Tighten Factor", self.dark_factor_var,
                         0.1, 1.0, 0.4, 0.6, 0.5, "dark_tighten_factor", 0.05,
                         lambda v: f"{v:.2f}")

        r = _row(hash_sec)
        ttk.Checkbutton(r, text="Dual Hash (dHash)",
                        variable=self.dual_hash_var).pack(side=tk.LEFT)
        _info_btn(r, "use_dual_hash").pack(side=tk.LEFT, padx=2)

        r = _row(hash_sec)
        ttk.Checkbutton(r, text="Histogram Intersection Guard",
                        variable=self.hist_var).pack(side=tk.LEFT)
        _info_btn(r, "use_histogram").pack(side=tk.LEFT, padx=2)
        self._slider_row(hash_sec, "  Minimum Histogram Similarity", self.hist_sim_var,
                         0.0, 1.0, 0.65, 0.80, 0.70, "hist_min_similarity", 0.05,
                         lambda v: f"{v:.2f}")
        self._slider_row(hash_sec, "Max Brightness Difference (0–255)",
                         self.brightness_diff_var,
                         0.0, 200.0, 40.0, 80.0, 60.0, "brightness_max_diff", 5.0,
                         lambda v: f"{int(round(v))}")

        # Advanced: Metadata
        meta_sec = _adv("Metadata")

        r = _row(meta_sec)
        ttk.Checkbutton(r, text="Collect EXIF metadata",
                        variable=self.collect_meta_var).pack(side=tk.LEFT)
        _info_btn(r, "collect_metadata").pack(side=tk.LEFT, padx=2)

        r = _row(meta_sec)
        ttk.Checkbutton(r, text="Export metadata CSV",
                        variable=self.export_csv_var).pack(side=tk.LEFT)
        _info_btn(r, "export_csv").pack(side=tk.LEFT, padx=2)

        r = _row(meta_sec)
        ttk.Checkbutton(r, text="Extended report (EXIF per image)",
                        variable=self.ext_report_var).pack(side=tk.LEFT)
        _info_btn(r, "extended_report").pack(side=tk.LEFT, padx=2)

        r = _row(meta_sec)
        ttk.Checkbutton(r, text="Sort by filename date",
                        variable=self.sort_fname_var).pack(side=tk.LEFT)
        _info_btn(r, "sort_by_filename_date").pack(side=tk.LEFT, padx=2)

        r = _row(meta_sec)
        ttk.Checkbutton(r, text="Sort by EXIF date",
                        variable=self.sort_exif_var).pack(side=tk.LEFT)
        _info_btn(r, "sort_by_exif_date").pack(side=tk.LEFT, padx=2)

        # Advanced: RAW Files
        raw_sec = _adv("RAW Files")

        r = _row(raw_sec)
        rawpy_cb = ttk.Checkbutton(
            r, text="Use rawpy for RAW files (CR2, NEF, ARW…)",
            variable=self.rawpy_var,
            state=tk.NORMAL if _RAWPY_AVAILABLE else tk.DISABLED,
        )
        rawpy_cb.pack(side=tk.LEFT)
        _info_btn(r, "use_rawpy").pack(side=tk.LEFT, padx=2)
        if not _RAWPY_AVAILABLE:
            ttk.Label(r, text="not installed", foreground="#e03").pack(side=tk.LEFT, padx=4)
            ttk.Button(r, text="Install rawpy",
                       command=self._install_rawpy).pack(side=tk.LEFT, padx=2)

    # ── Custom Scan tab ───────────────────────────────────────────────────

    def _build_custom_scan_tab(self) -> None:
        """Cross-folder duplicate finder: main folder (read-only) vs check folder."""
        tab = self._tab_custom
        _, body = _scrollable_frame(tab)

        # ── Info banner ───────────────────────────────────────────────────
        banner = tk.Frame(body, bg="#E8F5E9", bd=0)
        banner.pack(fill=tk.X, pady=(0, 8))
        tk.Frame(banner, height=3, bg=_M_SUCCESS).pack(fill=tk.X)
        tk.Label(
            banner,
            text=(
                "Custom Scan compares a reference folder against a second folder.\n"
                "Files in the Main folder are never moved or deleted.\n"
                "Only duplicates found in the Check folder are moved to trash."
            ),
            bg="#E8F5E9", fg="#1B5E20",
            font=("Segoe UI", 8), justify=tk.LEFT, padx=12, pady=8,
        ).pack(anchor=tk.W)

        # ── Folders ───────────────────────────────────────────────────────
        folders = _section(body, "Folders")

        def _cust_folder_row(parent, label, var, key):
            f = ttk.Frame(parent)
            f.pack(fill=tk.X, pady=3)
            ttk.Label(f, text=label, width=22, anchor=tk.W).pack(side=tk.LEFT)
            ttk.Entry(f, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            ttk.Button(f, text="Browse…",
                       command=lambda v=var: self._browse_custom(v)).pack(side=tk.RIGHT)
            var.trace_add("write", self._on_custom_folder_change)

        _cust_folder_row(folders, "Main folder (reference):",   self._custom_main_var,  "custom_main_folder")
        _cust_folder_row(folders, "Check folder (find dups):",  self._custom_check_var, "custom_check_folder")
        _cust_folder_row(folders, "Output / trash folder:",     self._custom_out_var,   "custom_out_folder")

        # ── Detection Settings ────────────────────────────────────────────
        det = _section(body, "Detection Settings")

        # Threshold and ratio share the same vars as Settings tab
        self._slider_row(det, "Similarity Threshold", self.thresh_var,
                         1, 30, 8, 12, 12, "threshold", 1,
                         lambda v: str(int(round(v))))
        self._slider_row(det, "Preview Size Ratio (per-dimension)", self.ratio_var,
                         0.50, 0.99, 0.85, 0.95, 0.90, "preview_ratio", 0.01,
                         lambda v: f"{v:.2f}")

        r = _row(det)
        ttk.Checkbutton(r, text="Ambiguous Match Detection",
                        variable=self.ambig_var).pack(side=tk.LEFT)
        _info_btn(r, "ambiguous_detection").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="  ", width=3).pack(side=tk.LEFT)
        ttk.Checkbutton(r, text="Disable Series Detection",
                        variable=self.disable_series_var).pack(side=tk.LEFT)

        r = _row(det)
        ttk.Checkbutton(r, text="Scan subfolders recursively",
                        variable=self.recursive_var).pack(side=tk.LEFT)
        _info_btn(r, "recursive").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="  ", width=3).pack(side=tk.LEFT)
        self._compact_rawpy_cb2 = ttk.Checkbutton(
            r, text="Use rawpy for RAW files", variable=self.rawpy_var,
            state=tk.NORMAL if _RAWPY_AVAILABLE else tk.DISABLED,
        )
        self._compact_rawpy_cb2.pack(side=tk.LEFT)

        r = _row(det)
        _label(r, "Skip folder names:")
        ttk.Entry(r, textvariable=self.skip_names_var, width=36).pack(side=tk.LEFT)
        _info_btn(r, "skip_names").pack(side=tk.LEFT, padx=2)

        self._slider_row(det, "Minimum Dimension Filter (px)", self.mindim_var,
                         0, 2000, 100, 300, 0, "min_dimension", 50,
                         lambda v: f"{int(round(v))} px" if v > 0 else "off")

        # ── Keep & Match options ──────────────────────────────────────────
        km = _section(body, "Keep & Match")

        r = _row(km)
        _label(r, "Prefer to keep:")
        ttk.Radiobutton(r, text="Largest resolution",
                        variable=self.strategy_var, value="pixels").pack(side=tk.LEFT)
        ttk.Radiobutton(r, text="Oldest file date",
                        variable=self.strategy_var, value="oldest").pack(side=tk.LEFT, padx=6)
        _info_btn(r, "keep_strategy").pack(side=tk.LEFT, padx=2)

        r = _row(km)
        ttk.Checkbutton(r, text="Dark Image Protection",
                        variable=self.dark_var).pack(side=tk.LEFT)
        _info_btn(r, "dark_protection").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="  ", width=3).pack(side=tk.LEFT)
        ttk.Checkbutton(r, text="Dual Hash (dHash)",
                        variable=self.dual_hash_var).pack(side=tk.LEFT)
        _info_btn(r, "use_dual_hash").pack(side=tk.LEFT, padx=2)

        r = _row(km)
        ttk.Checkbutton(r, text="Histogram Intersection Guard",
                        variable=self.hist_var).pack(side=tk.LEFT)
        _info_btn(r, "use_histogram").pack(side=tk.LEFT, padx=2)

        # ── Actions ───────────────────────────────────────────────────────
        act = _section(body, "Actions")

        r = _row(act)
        ttk.Checkbutton(r, text="Dry Run (recommended)",
                        variable=self._custom_dry_var).pack(side=tk.LEFT)
        _info_btn(r, "dry_run").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="Report only — no files moved. Use 'Accept & Move' after reviewing.",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)

        # Estimate
        self._custom_estimate_frame = ttk.Frame(body)
        self._custom_estimate_frame.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(self._custom_estimate_frame, textvariable=self._custom_estimate_var,
                  foreground="#555", font=("Segoe UI", 8, "italic")).pack(anchor=tk.W)

        # ── Progress panel (fixed bottom of tab) ──────────────────────────
        self._custom_prog_frame = ttk.LabelFrame(tab, text="Progress", padding=(8, 4, 8, 6))
        self._custom_prog_frame.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Label(self._custom_prog_frame, textvariable=self._custom_phase_label,
                  font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        self._custom_progress_bar = ttk.Progressbar(
            self._custom_prog_frame, mode="determinate", maximum=100)
        self._custom_progress_bar.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(self._custom_prog_frame, textvariable=self._custom_eta_var,
                  foreground="#555", font=("Segoe UI", 8)).pack(anchor=tk.W)

        # ── Button bar (fixed very bottom) ────────────────────────────────
        c_btn_bar = tk.Frame(tab, bg=_ACCENT_TINT, pady=7)
        c_btn_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(c_btn_bar, height=1, bg=_M_DIVIDER).place(relx=0, rely=0, relwidth=1)

        self._custom_idle_frame = tk.Frame(c_btn_bar, bg=_ACCENT_TINT)
        self._custom_idle_frame.pack(fill=tk.X, padx=4)

        self._custom_scan_btn = _mat_btn(
            self._custom_idle_frame, "▶  Start Custom Scan",
            self._start_custom_scan, _M_SUCCESS)
        self._custom_scan_btn.pack(side=tk.RIGHT, padx=(4, 8))

        self._custom_accept_btn = _mat_btn(
            self._custom_idle_frame, "✓  Accept & Move",
            self._custom_accept_and_move, _M_SUCCESS)
        self._custom_accept_btn.pack(side=tk.LEFT, padx=(4, 4))
        _mat_disable(self._custom_accept_btn)

        self._custom_inapp_btn = _mat_btn(
            self._custom_idle_frame, "Review In-App",
            self._custom_open_inapp_report, _ACCENT)
        self._custom_inapp_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self._custom_inapp_btn)

        self._custom_browser_btn = _mat_btn(
            self._custom_idle_frame, "Browser Report",
            self._custom_open_browser_report, "#757575")
        self._custom_browser_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self._custom_browser_btn)

        self._custom_active_frame = tk.Frame(c_btn_bar, bg=_ACCENT_TINT)
        # Not packed initially

        self._custom_stop_btn = _mat_btn(
            self._custom_active_frame, "■  Stop",
            self._stop_custom_scan, _M_ERROR)
        self._custom_stop_btn.pack(side=tk.LEFT, padx=(8, 4))

        self._custom_pause_btn = _mat_btn(
            self._custom_active_frame, "⏸  Pause",
            self._pause_custom_scan, _M_AMBER)
        self._custom_pause_btn.pack(side=tk.LEFT, padx=4)

    # ── custom scan folder helpers ─────────────────────────────────────────

    def _browse_custom(self, var: tk.StringVar) -> None:
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            var.set(folder)

    def _on_custom_folder_change(self, *_) -> None:
        self._on_setting_change()
        if hasattr(self, "_custom_estimate_after_id"):
            self.root.after_cancel(self._custom_estimate_after_id)
        self._custom_estimate_after_id = self.root.after(2000, self._update_custom_estimate)

    def _update_custom_estimate(self) -> None:
        main  = self._custom_main_var.get().strip()
        check = self._custom_check_var.get().strip()
        if not main and not check:
            return

        def _count() -> None:
            total = 0
            try:
                recursive      = self.recursive_var.get()
                skip_names_set = {s.strip() for s in self.skip_names_var.get().split(",") if s.strip()}
                for folder in (main, check):
                    if not folder or not Path(folder).is_dir():
                        continue
                    if recursive:
                        for root_d, dirs, files in os.walk(folder):
                            dirs[:] = [d for d in dirs if d not in skip_names_set]
                            for f in files:
                                if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                                    total += 1
                    else:
                        for f in os.listdir(folder):
                            if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                                total += 1

                hash_time    = total * 0.3
                compare_time = total * (total - 1) / 2 * 0.0000005
                total_s      = hash_time + compare_time
                if total_s < 60:
                    time_str = f"~{int(total_s)}s"
                elif total_s < 3600:
                    si = int(total_s)
                    time_str = f"~{si // 60}m {si % 60}s"
                else:
                    time_str = f"~{int(total_s) // 3600}h"

                msg = f"Estimated time: {time_str}  ·  {total} images total across both folders"
                self.root.after(0, lambda m=msg: self._custom_estimate_var.set(m))
            except Exception:
                pass

        threading.Thread(target=_count, daemon=True).start()

    # ── custom scan control ────────────────────────────────────────────────

    def _start_custom_scan(self) -> None:
        if self._scanning:
            messagebox.showwarning("Busy", "A scan is already running.", parent=self.root)
            return

        main  = self._custom_main_var.get().strip()
        check = self._custom_check_var.get().strip()
        out   = self._custom_out_var.get().strip()

        if not main:
            messagebox.showerror("Error", "Please select the Main (reference) folder.", parent=self.root)
            return
        if not check:
            messagebox.showerror("Error", "Please select the Check folder.", parent=self.root)
            return
        if not out:
            messagebox.showerror("Error", "Please select an Output folder.", parent=self.root)
            return

        main_path, check_path, out_path = Path(main), Path(check), Path(out)
        if not main_path.is_dir():
            messagebox.showerror("Error", "Main folder does not exist.", parent=self.root)
            return
        if not check_path.is_dir():
            messagebox.showerror("Error", "Check folder does not exist.", parent=self.root)
            return
        if main_path.resolve() == check_path.resolve():
            messagebox.showerror("Error", "Main and Check folders must be different.", parent=self.root)
            return

        self._collect_settings()
        self._scanning = True
        self._custom_stop_flag[0]  = False
        self._custom_pause_flag[0] = False
        self._custom_groups        = []
        self._custom_broken        = []
        self._custom_report_path   = None

        # Swap button frames
        self._custom_idle_frame.pack_forget()
        self._custom_active_frame.pack(fill=tk.X, padx=4)
        _mat_disable(self._custom_accept_btn)
        _mat_disable(self._custom_inapp_btn)
        _mat_disable(self._custom_browser_btn)

        self._custom_phase_label.set("Initialising…")
        self._custom_progress_bar["value"] = 0
        self._custom_progress_bar["mode"]  = "indeterminate"
        self._custom_progress_bar.start(12)

        threading.Thread(
            target=self._custom_worker,
            args=(main_path, check_path, out_path, self.settings),
            daemon=True,
        ).start()

    def _pause_custom_scan(self) -> None:
        self._custom_pause_flag[0] = True
        _mat_disable(self._custom_pause_btn)
        self._custom_phase_label.set("Pausing…")

    def _stop_custom_scan(self) -> None:
        if not messagebox.askyesno("Stop Scan", "Stop the custom scan?", parent=self.root):
            return
        self._custom_stop_flag[0] = True
        _mat_disable(self._custom_stop_btn)
        _mat_disable(self._custom_pause_btn)
        self._custom_phase_label.set("Stopping…")

    # ── custom worker ─────────────────────────────────────────────────────

    def _custom_worker(
        self, main_path: Path, check_path: Path, out_path: Path, settings: Settings
    ) -> None:
        _PHASES = ["Main folder", "Check folder", "Comparing", "Report"]

        def cb(msg, done, total, phase):
            self._custom_progress_cb(msg, done, total, phase)

        try:
            out_path.mkdir(parents=True, exist_ok=True)
            skip_paths = {
                (out_path / "results").resolve(),
                (out_path / "trash").resolve(),
                out_path.resolve(),
            }

            # Phase 1 — hash main folder
            cb("Scanning main folder…", 0, 1, "Main folder")
            main_failed: list = []
            main_records = collect_images(
                main_path, skip_paths, settings,
                progress_cb=cb,
                stop_flag=self._custom_stop_flag,
                pause_flag=self._custom_pause_flag,
                failed_paths=main_failed,
            )

            if self._custom_stop_flag[0]:
                self.root.after(0, lambda: self._on_custom_done("Stopped.", success=False))
                return

            # Phase 2 — hash check folder
            cb("Scanning check folder…", 0, 1, "Check folder")
            check_failed: list = []
            check_records = collect_images(
                check_path, skip_paths, settings,
                progress_cb=cb,
                stop_flag=self._custom_stop_flag,
                pause_flag=self._custom_pause_flag,
                failed_paths=check_failed,
            )

            if self._custom_stop_flag[0]:
                self.root.after(0, lambda: self._on_custom_done("Stopped.", success=False))
                return

            all_records = main_records + check_records
            self._custom_broken = main_failed + check_failed

            # Phase 3 — find groups across both folders
            cb("Comparing images…", 0, 1, "Comparing")
            all_groups, _ = find_groups(
                all_records, settings,
                progress_cb=cb,
                stop_flag=self._custom_stop_flag,
                pause_flag=self._custom_pause_flag,
            )

            if self._custom_stop_flag[0]:
                self.root.after(0, lambda: self._on_custom_done("Stopped.", success=False))
                return

            # Reclassify: main = originals (never moved), check = duplicates (candidates for trash)
            main_res  = main_path.resolve()
            check_res = check_path.resolve()

            def _in_folder(p: Path, folder: Path) -> bool:
                try:
                    p.resolve().relative_to(folder)
                    return True
                except ValueError:
                    return False

            cross_groups = []
            within_check_groups = []
            for g in all_groups:
                all_members = g.originals + g.previews
                from_main  = [r for r in all_members if _in_folder(r.path, main_res)]
                from_check = [r for r in all_members if _in_folder(r.path, check_res)]

                if from_main and from_check:
                    # Cross-folder match: main files = originals, check files = duplicates
                    g.originals = from_main
                    g.previews  = from_check
                    cross_groups.append(g)
                elif not from_main and len(from_check) > 1:
                    # Within-check duplicates — keep the best, mark rest as previews
                    g.originals = from_check[:1]
                    g.previews  = from_check[1:]
                    within_check_groups.append(g)

            combined_groups = cross_groups + within_check_groups
            n_cross = sum(len(g.previews) for g in cross_groups)
            n_inner = sum(len(g.previews) for g in within_check_groups)
            n_total = n_cross + n_inner

            # Phase 4 — report
            cb("Generating report…", 0, 1, "Report")
            report = generate_report(
                combined_groups, out_path, main_path, len(all_records), settings)
            self._custom_groups      = combined_groups
            self._custom_report_path = report

            dry = settings.dry_run
            msg = (
                f"Done. {len(main_records)} main + {len(check_records)} check images scanned.  "
                f"{len(cross_groups)} cross-folder matches ({n_cross} dups from check folder)"
                + (f",  {len(within_check_groups)} within-check groups ({n_inner} dups)" if n_inner else "")
                + ("  [DRY RUN]" if dry else "")
                + "."
            )
            self.root.after(0, lambda: self._on_custom_done(
                msg, success=True, dry_run=dry,
                n_main=len(main_records), n_check=len(check_records),
                n_cross=len(cross_groups), n_dups=n_total,
                src_folder=str(main_path),
            ))

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self.root.after(0, lambda e=exc, t=tb: self._on_custom_error(str(e), t))

    def _custom_progress_cb(
        self, msg: str, done: int, total: int, phase_name: str
    ) -> None:
        _PHASES = ["Main folder", "Check folder", "Comparing", "Report"]

        def _update() -> None:
            pct_per_phase = 100 / len(_PHASES)
            try:
                phase_idx = _PHASES.index(phase_name)
            except ValueError:
                phase_idx = 0
            base_pct = phase_idx * pct_per_phase
            inner    = (done / max(total, 1)) * pct_per_phase if total > 0 else 0
            pct      = min(100, base_pct + inner)

            self._custom_progress_bar.stop()
            self._custom_progress_bar["mode"]  = "determinate"
            self._custom_progress_bar["value"] = pct
            self._custom_phase_label.set(
                f"[{phase_name}] {msg[:90]}"
            )
            self._custom_eta_var.set(f"{pct:.0f}%")

        self.root.after(0, _update)

    def _on_custom_done(
        self, msg: str, success: bool = True,
        dry_run: bool = True, n_main: int = 0, n_check: int = 0,
        n_cross: int = 0, n_dups: int = 0, src_folder: str = "",
    ) -> None:
        self._custom_progress_bar.stop()
        self._custom_progress_bar["mode"]  = "determinate"
        self._custom_progress_bar["value"] = 100 if success else self._custom_progress_bar["value"]
        self._scanning = False

        # Restore idle frame
        self._custom_active_frame.pack_forget()
        self._custom_idle_frame.pack(fill=tk.X, padx=4)

        self._custom_phase_label.set(msg)
        self._custom_eta_var.set("")

        if success and self._custom_groups:
            _mat_enable(self._custom_inapp_btn)
            _mat_enable(self._custom_browser_btn)
            if dry_run:
                _mat_enable(self._custom_accept_btn)

            # Log to scan history (with note that it's a custom scan)
            dup_pct = n_dups / max(n_main + n_check, 1) * 100
            entry = {
                "date":        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "src_folder":  f"[Custom] {src_folder}",
                "total_files": n_main + n_check,
                "groups":      n_cross,
                "duplicates":  n_dups,
                "dup_pct":     round(dup_pct, 2),
                "dry_run":     dry_run,
                "applied":     not dry_run,
            }
            self._scan_history.append(entry)
            self._save_scan_history()
            self._refresh_history_view()

            # Auto-open in-app report
            self.root.after(300, self._custom_open_inapp_report)

    def _on_custom_error(self, msg: str, tb: str = "") -> None:
        self._custom_progress_bar.stop()
        self._custom_phase_label.set("Error — see dialog.")
        self._scanning = False
        self._custom_active_frame.pack_forget()
        self._custom_idle_frame.pack(fill=tk.X, padx=4)
        messagebox.showerror("Custom Scan Error", f"{msg}\n\n{tb}" if tb else msg,
                             parent=self.root)

    # ── custom scan post-scan actions ──────────────────────────────────────

    def _custom_open_inapp_report(self) -> None:
        if not self._custom_groups:
            messagebox.showinfo("Review", "No custom scan results to review.", parent=self.root)
            return
        out = self._custom_out_var.get().strip()

        def _apply_cb(groups: list) -> None:
            move_groups(groups, Path(out), dry_run=False, settings=self.settings)
            # Update history applied flag
            if self._scan_history:
                for e in reversed(self._scan_history):
                    if "[Custom]" in e.get("src_folder", ""):
                        e["applied"] = True
                        break
                self._save_scan_history()
                self._refresh_history_view()

        viewer = ReportViewer(
            self.root, self._custom_groups,
            ops_log_path=None,
            on_apply_cb=_apply_cb,
            solo_originals=[],
            broken_files=self._custom_broken,
            settings=self.settings,
        )
        viewer.grab_set()

    def _custom_open_browser_report(self) -> None:
        if self._custom_report_path and self._custom_report_path.exists():
            webbrowser.open(self._custom_report_path.as_uri())

    def _custom_accept_and_move(self) -> None:
        if not self._custom_groups:
            messagebox.showwarning("Accept & Move", "No groups to move.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Accept & Move",
            "Move duplicate files from the Check folder to trash?\n"
            "Main folder files will NOT be touched.",
            parent=self.root,
        ):
            return
        out = self._custom_out_var.get().strip()
        if not out:
            return
        _mat_disable(self._custom_accept_btn)
        self._custom_phase_label.set("Moving files…")

        def _do() -> None:
            try:
                moved_orig, moved_prev = move_groups(
                    self._custom_groups, Path(out),
                    dry_run=False, settings=self.settings,
                )
                report = generate_report(
                    self._custom_groups, Path(out),
                    Path(self._custom_main_var.get()),
                    0, self.settings,
                )
                self._custom_report_path = report
                msg = f"Moved {moved_prev} duplicates from check folder to trash."
                self.root.after(0, lambda: self._custom_phase_label.set(msg))
                if self._scan_history:
                    for e in reversed(self._scan_history):
                        if "[Custom]" in e.get("src_folder", ""):
                            e["applied"] = True
                            break
                    self._save_scan_history()
                    self.root.after(0, self._refresh_history_view)
                if report:
                    self.root.after(0, lambda: webbrowser.open(report.as_uri()))
            except Exception as exc:
                self.root.after(0, lambda e=exc: messagebox.showerror(
                    "Error", str(e), parent=self.root))

        threading.Thread(target=_do, daemon=True).start()

    # ── mode management ───────────────────────────────────────────────────

    def _apply_mode(self) -> None:
        if self._mode_var.get() == "advanced":
            self._compact_adv_frame.pack(fill=tk.X, pady=(0, 4))
        else:
            self._compact_adv_frame.pack_forget()

    def _toggle_show_all(self) -> None:
        self._all_settings_visible = not self._all_settings_visible
        if self._all_settings_visible:
            self._show_all_btn.pack_forget()
            for f in self._advanced_frames:
                f.pack(fill=tk.X, pady=(0, 6))
            self._show_all_btn.pack(anchor=tk.W, padx=2, pady=(0, 4))
            self._show_all_btn.configure(text="▲  Hide Advanced Settings")
        else:
            for f in self._advanced_frames:
                f.pack_forget()
            self._show_all_btn.configure(text="▼  Show Advanced Settings")

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
        ttk.Button(frame, text="Browse…",
                   command=lambda v=var, k=setting_key: self._browse(v, k)).pack(side=tk.RIGHT)
        var.trace_add("write", self._on_folder_change)
        return var

    # ── slider row helper ─────────────────────────────────────────────────

    def _slider_row(
        self, parent, label, var, min_v, max_v,
        rec_lo, rec_hi, default, key, step, fmt,
    ) -> None:
        outer = ttk.Frame(parent)
        outer.pack(fill=tk.X, pady=(6, 2))

        ttk.Label(outer, text=label, font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)

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
            return "break"

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

        # Marks canvas
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
            PAD = 8
            t_start = scale_x + PAD
            t_end   = scale_x + scale_w - PAD
            t_w     = t_end - t_start
            if t_w <= 0:
                return

            def px(v: float) -> float:
                return t_start + (v - min_v) / (max_v - min_v) * t_w

            marks_c.create_line(t_start, canvas_h // 2, t_end, canvas_h // 2,
                                fill="#ddd", width=1)
            marks_c.create_rectangle(
                px(rec_lo), canvas_h // 2 - 3,
                px(rec_hi), canvas_h // 2 + 3,
                fill="#b3cfff", outline="",
            )
            for v, color, is_edge in [
                (min_v,  "#999", True),
                (rec_lo, "#1a73e8", False),
                (rec_hi, "#1a73e8", False),
                (max_v,  "#999", True),
            ]:
                x = px(v)
                marks_c.create_line(x, 2, x, canvas_h - 4, fill=color, width=1)
                marks_c.create_text(x, canvas_h - 2, text=fmt(v),
                                    anchor=tk.S, fill=color, font=("Segoe UI", 7))

        marks_c.bind("<Configure>", _draw_marks)
        outer.after(80, _draw_marks)

        _, detail = INFO_TEXTS.get(key, ("", ""))
        if detail:
            ttk.Label(outer, text=_first_sentence(detail), foreground="#666",
                      font=("Segoe UI", 8), wraplength=560,
                      justify=tk.LEFT).pack(anchor=tk.W, pady=(1, 0))

    def _browse(self, var: tk.StringVar, key: str) -> None:
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            var.set(folder)

    def _on_folder_change(self, *_) -> None:
        self._on_setting_change()
        if hasattr(self, "_estimate_after_id"):
            self.root.after_cancel(self._estimate_after_id)
        self._estimate_after_id = self.root.after(2000, self._update_estimate)

    # ── date format helpers ───────────────────────────────────────────────

    def _date_labels(self, sep: str) -> list[str]:
        vis = sep if sep != " " else "·"
        result = []
        for tmpl in self._DATE_ORDER_TEMPLATES:
            lbl = (tmpl.replace("{s}", vis)
                       .replace("%Y", "YYYY").replace("%m", "MM").replace("%d", "DD"))
            result.append(lbl)
        return result

    def _fmt_from_order_sep(self, idx: int, sep: str) -> str:
        return self._DATE_ORDER_TEMPLATES[idx].replace("{s}", sep)

    def _guess_order_sep(self, fmt: str) -> tuple[int, str]:
        for sep in self._DATE_SEPARATORS:
            for i, tmpl in enumerate(self._DATE_ORDER_TEMPLATES):
                if tmpl.replace("{s}", sep) == fmt:
                    return i, sep
        return 0, "-"

    def _refresh_date_order_choices(self, sep: str, select_idx: int) -> None:
        labels = self._date_labels(sep)
        self._date_order_cb["values"] = labels
        if 0 <= select_idx < len(labels):
            self._date_order_var.set(labels[select_idx])
            self._date_order_idx_val = select_idx

    def _on_date_sep_change(self, *_) -> None:
        sep = self._date_sep_var.get()
        cur_label = self._date_order_var.get()
        old_labels = self._date_labels("-" if sep != "-" else "/")
        try:
            idx = old_labels.index(cur_label)
        except ValueError:
            idx = self._date_order_idx_val
        self._refresh_date_order_choices(sep, idx)
        self._recompute_date_fmt()

    def _on_date_fmt_change(self, *_) -> None:
        self._recompute_date_fmt()

    def _recompute_date_fmt(self, *_) -> None:
        sep = self._date_sep_var.get()
        labels = self._date_labels(sep)
        cur_label = self._date_order_var.get()
        try:
            idx = labels.index(cur_label)
        except ValueError:
            idx = 0
        self._date_order_idx_val = idx
        fmt = self._fmt_from_order_sep(idx, sep)
        self.date_fmt_var.set(fmt)
        try:
            example = datetime.datetime(2024, 3, 15).strftime(fmt)
            self._date_fmt_example.set(f"e.g. {example}/")
        except Exception:
            self._date_fmt_example.set("(invalid)")
        self._on_setting_change()

    # ── settings persistence ──────────────────────────────────────────────

    def _apply_last_calibration(self) -> None:
        if self.settings.calibrated_threshold > 0:
            self.thresh_var.set(self.settings.calibrated_threshold)
            self.ratio_var.set(self.settings.calibrated_preview_ratio)
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
        s = self.settings
        s.mode                     = self._mode_var.get()
        s.src_folder               = self.src_var.get()
        s.out_folder               = self.out_var.get()
        s.threshold                = self._safe_int(self.thresh_var, 12)
        s.preview_ratio            = self._safe_float(self.ratio_var, 0.90)
        s.series_tolerance_pct     = self._safe_float(self.series_tol_var, 0.0)
        s.series_threshold_factor  = self._safe_float(self.series_thresh_var, 1.0)
        s.ar_tolerance_pct         = self._safe_float(self.ar_tol_var, 5.0)
        s.dark_protection          = self.dark_var.get()
        s.dark_threshold           = self._safe_float(self.dark_thresh_var, 40.0)
        s.dark_tighten_factor      = self._safe_float(self.dark_factor_var, 0.5)
        s.use_dual_hash            = self.dual_hash_var.get()
        s.use_histogram            = self.hist_var.get()
        s.hist_min_similarity      = self._safe_float(self.hist_sim_var, 0.70)
        s.brightness_max_diff      = self._safe_float(self.brightness_diff_var, 60.0)
        s.ambiguous_detection      = self.ambig_var.get()
        s.ambiguous_threshold_factor = self._safe_float(self.ambig_factor_var, 1.5)
        s.disable_series_detection = self.disable_series_var.get()
        s.use_rawpy                = self.rawpy_var.get()
        s.keep_strategy            = self.strategy_var.get()
        s.keep_all_formats         = self.all_formats_var.get()
        s.prefer_rich_metadata     = self.prefer_meta_var.get()
        s.collect_metadata         = self.collect_meta_var.get()
        s.export_csv               = self.export_csv_var.get()
        s.extended_report          = self.ext_report_var.get()
        s.sort_by_filename_date    = self.sort_fname_var.get()
        s.sort_by_exif_date        = self.sort_exif_var.get()
        s.min_dimension            = self._safe_int(self.mindim_var, 0)
        s.recursive                = self.recursive_var.get()
        s.skip_names               = self.skip_names_var.get()
        s.dry_run                  = self.dry_var.get()
        s.organize_by_date         = self.org_date_var.get()
        s.date_folder_format       = self.date_fmt_var.get() or "%Y-%m-%d"
        s.details_visible          = self._details_var.get()
        s.dry_run                  = self._custom_dry_var.get()  # shared dry-run flag
        s.custom_main_folder       = self._custom_main_var.get()
        s.custom_check_folder      = self._custom_check_var.get()
        s.custom_out_folder        = self._custom_out_var.get()

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
        self.disable_series_var.set(d.disable_series_detection)
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
        new_fmt = d.date_folder_format
        idx, sep = self._guess_order_sep(new_fmt)
        self._date_sep_var.set(sep)
        self._refresh_date_order_choices(sep, idx)
        self.date_fmt_var.set(new_fmt)
        self._schedule_settings_save()

    def _open_calibration(self) -> None:
        self._collect_settings()
        from calibration_window import CalibrationWindow

        def _apply(threshold: int, preview_ratio: float) -> None:
            self.thresh_var.set(threshold)
            self.ratio_var.set(preview_ratio)
            self._on_setting_change()

        def _folder_saved(folder: str) -> None:
            self.settings.calib_folder = folder
            self._schedule_settings_save()

        def _calib_applied(threshold: int, preview_ratio: float) -> None:
            self.settings.calibrated_threshold = threshold
            self.settings.calibrated_preview_ratio = preview_ratio
            _mat_enable(self._calib_apply_btn)
            _mat_enable(self._scan_last_calib_btn)
            self._schedule_settings_save()

        CalibrationWindow(
            self.root, self.settings,
            apply_cb=_apply,
            folder_cb=_folder_saved,
            calibration_applied_cb=_calib_applied,
        )

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
                recursive      = self.recursive_var.get()
                skip_names_set = {s.strip() for s in self.skip_names_var.get().split(",") if s.strip()}
                src_path       = Path(src)

                if recursive:
                    for root_dir, dirs, files in os.walk(src_path):
                        dirs[:] = [d for d in dirs if d not in skip_names_set]
                        for f in files:
                            if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                                count += 1
                else:
                    for f in os.listdir(src_path):
                        if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                            count += 1

                hash_time    = count * 0.3
                compare_time = count * (count - 1) / 2 * 0.0000005
                extras       = 0.0
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
                    s_int = int(total_s)
                    time_str = f"~{s_int // 60}m {s_int % 60}s"
                else:
                    hrs  = int(total_s) // 3600
                    mins = (int(total_s) % 3600) // 60
                    time_str = f"~{hrs}h {mins}m"

                msg = f"Estimated time: {time_str}  ·  {count} images found"
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
        ts = ""
        try:
            ts = datetime.datetime.fromtimestamp(sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
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

    # ── last-results restore ──────────────────────────────────────────────

    def _check_last_results(self) -> None:
        out = self.settings.out_folder.strip()
        if not out:
            return
        from scan_state import load_results, results_path
        rp = results_path(Path(out))
        result = load_results(Path(out))
        if result is None:
            return

        self.scan_groups     = result["groups"]
        self._solo_originals = result["solo_originals"]
        self._broken_files   = result["broken_files"]
        self.scan_records = (
            [r for g in self.scan_groups for r in g.originals + g.previews]
            + self._solo_originals
        )

        html = result.get("report_html", "")
        if html and Path(html).exists():
            self.report_path = Path(html)

        ts = ""
        try:
            ts = datetime.datetime.fromtimestamp(rp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        n_groups = len(self.scan_groups)
        n_prev   = sum(len(g.previews) for g in self.scan_groups)

        _mat_enable(self.browser_report_btn)
        _mat_enable(self.inapp_report_btn)
        if result.get("dry_run", True):
            _mat_enable(self.accept_btn)

        self._last_scan_info = {
            "date":        ts,
            "src_folder":  result.get("src_folder", ""),
            "total_files": result.get("total_scanned", 0),
            "groups":      n_groups,
            "duplicates":  n_prev,
            "dup_pct":     n_prev / max(result.get("total_scanned", 1), 1) * 100,
            "dry_run":     result.get("dry_run", True),
            "applied":     False,
        }
        self._show_results_tab()
        self._update_results_tab_ui()
        self._phase_label_var.set(
            f"Previous scan ({ts})  ·  {n_groups} groups, {n_prev} duplicates.  "
            "Switch to Results tab to review."
        )

    # ── scan control ──────────────────────────────────────────────────────

    def _start_scan(self, resume_state=None) -> None:
        if self._scanning:
            messagebox.showwarning("Busy", "A scan is already running.", parent=self.root)
            return
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

        self._scanning      = True
        self._stop_flag[0]  = False
        self._pause_flag[0] = False

        # Swap button frames
        self._scan_idle_frame.pack_forget()
        self._scan_active_frame.pack(fill=tk.X, padx=4)

        self.report_path     = None
        self.scan_groups     = []
        self.scan_records    = []
        self._broken_files   = []
        self._solo_originals = []

        # Switch to Scan tab so user sees progress
        self._nb.select(self._tab_scan)

        self._tracker = PhaseTracker(PHASE_NAMES)
        self._tracker.start_phase("Discovery", 1)
        self._phase_label_var.set("Phase 1/6: Discovering images…")
        self._progress_bar["value"] = 0
        self._progress_bar["mode"]  = "indeterminate"
        self._progress_bar.start(12)

        threading.Thread(
            target=self._worker,
            args=(src_path, out_path, self.settings, resume_state),
            daemon=True,
        ).start()

    def _pause_scan(self) -> None:
        self._pause_flag[0] = True
        _mat_disable(self.pause_btn)
        self._phase_label_var.set("Pausing…")

    def _stop_scan(self) -> None:
        if not messagebox.askyesno("Stop Scan", "Stop the current scan?", parent=self.root):
            return
        self._stop_flag[0] = True
        _mat_disable(self.stop_btn)
        _mat_disable(self.pause_btn)
        self._phase_label_var.set("Stopping…")

    # ── new scan prompt ───────────────────────────────────────────────────

    def _new_scan_prompt(self) -> None:
        if not messagebox.askyesno(
            "Start New Scan",
            "Clear the current results and start a new scan?",
            parent=self.root,
        ):
            return
        self._hide_results_tab()
        self.scan_groups     = []
        self.scan_records    = []
        self._broken_files   = []
        self._solo_originals = []
        self.report_path     = None
        self._last_scan_info = {}
        _mat_disable(self.accept_btn)
        _mat_disable(self.browser_report_btn)
        _mat_disable(self.inapp_report_btn)
        _mat_disable(self.revert_all_btn)
        self._phase_label_var.set("Ready.")
        self._nb.select(self._tab_scan)

    # ── progress callback ─────────────────────────────────────────────────

    def _progress_cb(self, msg: str, done: int, total: int, phase_name: str) -> None:
        def _update() -> None:
            if self._tracker is None:
                return
            if self._tracker.current_phase_name != phase_name:
                self._tracker.finish_phase()
                phase_num = PHASE_NAMES.index(phase_name) + 1 if phase_name in PHASE_NAMES else "?"
                self._tracker.start_phase(phase_name, max(total, 1))
                self._phase_label_var.set(f"Phase {phase_num}/{len(PHASE_NAMES)}: {phase_name}…")
                self._progress_bar.stop()
                self._progress_bar["mode"] = "determinate"

            if total > 0:
                self._tracker.update(done)

            pct = self._tracker.total_pct
            self._progress_bar["value"] = pct
            eta = self._tracker.format_eta()
            self._eta_var.set(f"{pct:.0f}%  ·  {eta} remaining  ·  {msg[:80]}")
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

    def _worker(self, src: Path, out: Path, settings: Settings, resume_state=None) -> None:
        def cb(msg, done, total, phase):
            self._progress_cb(msg, done, total, phase)

        try:
            out.mkdir(parents=True, exist_ok=True)
            skip_paths = {
                (out / "results").resolve(),
                (out / "trash").resolve(),
                out.resolve(),
            }

            if resume_state and resume_state.phase == "comparing":
                from scan_state import deserialize_record
                records = [deserialize_record(r) for r in resume_state.records]
                cb(f"Restored {len(records)} records from paused state.", 0, 0, "Hashing")
            else:
                cb("Discovering images…", 0, 1, "Discovery")
                failed: list = []
                records = collect_images(
                    src, skip_paths, settings,
                    progress_cb=cb,
                    stop_flag=self._stop_flag,
                    pause_flag=self._pause_flag,
                    failed_paths=failed,
                )
                self._broken_files = failed

            if self._stop_flag[0]:
                self.root.after(0, lambda: self._on_done("Stopped by user.", success=False))
                return

            if self._pause_flag[0]:
                self._save_pause_state(records, out, settings, compare_i=0, union_parent=[])
                self.root.after(0, lambda: self._on_done("Scan paused.", success=False, paused=True))
                return

            self.scan_records = records

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
                from scan_state import save_state, state_path, serialize_record
                from dataclasses import asdict
                partial_state.source_folder      = str(src)
                partial_state.output_folder      = str(out)
                partial_state.settings_snapshot  = asdict(settings)
                if not partial_state.records:
                    partial_state.records = [serialize_record(r) for r in records]
                save_state(partial_state, state_path(out))
                self.root.after(0, lambda: self._on_done("Scan paused.", success=False, paused=True))
                return

            if settings.collect_metadata and groups:
                cb("Saving metadata…", 0, 1, "Metadata")
                from metadata import save_metadata_json, export_metadata_csv
                save_metadata_json(groups, out)
                if settings.export_csv:
                    export_metadata_csv(groups, out)

            if not settings.dry_run and groups:
                cb("Moving files…", 0, len(groups), "Moving")
                move_groups(groups, out, dry_run=False, settings=settings)

            cb("Generating report…", 0, 1, "Report")
            report = generate_report(groups, out, src, len(records), settings)
            self.report_path = report
            self.scan_groups = groups

            grouped_paths = {
                r.path.resolve()
                for g in groups for r in g.originals + g.previews
            }
            solo_originals     = [r for r in records if r.path.resolve() not in grouped_paths]
            self._solo_originals = solo_originals

            from scan_state import save_results, state_path as _sp
            save_results(
                groups=groups,
                solo_originals=solo_originals,
                broken_files=getattr(self, "_broken_files", []),
                total_scanned=len(records),
                output_folder=out,
                src_folder=str(src),
                dry_run=settings.dry_run,
                report_html=str(report) if report else "",
            )

            _sp_file = _sp(out)
            if _sp_file.exists():
                _sp_file.unlink()

            n_orig = sum(len(g.originals) for g in groups)
            n_prev = sum(len(g.previews)  for g in groups)
            dry_note = " (DRY RUN)" if settings.dry_run else ""
            msg = (
                f"Done{dry_note}. {len(records)} scanned — "
                f"{len(groups)} groups, {n_orig} kept, {n_prev} duplicates."
            )
            self.root.after(0, lambda: self._on_done(
                msg, success=True, dry_run=settings.dry_run,
                total_scanned=len(records), n_groups=len(groups),
                n_prev=n_prev, src_folder=str(src),
            ))

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self.root.after(0, lambda e=exc, t=tb: self._on_error(str(e), t))

    def _save_pause_state(self, records, out, settings, compare_i, union_parent) -> None:
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
        dry_run: bool = False, paused: bool = False,
        total_scanned: int = 0, n_groups: int = 0,
        n_prev: int = 0, src_folder: str = "",
    ) -> None:
        self._progress_bar.stop()
        self._progress_bar["mode"]  = "determinate"
        self._progress_bar["value"] = 100 if (success and not paused) else self._progress_bar["value"]
        self._scanning = False

        # Restore idle button frame
        self._scan_active_frame.pack_forget()
        self._scan_idle_frame.pack(fill=tk.X, padx=4)

        self._phase_label_var.set(msg)
        self._eta_var.set("")

        if success:
            _mat_enable(self.browser_report_btn)
            _mat_enable(self.inapp_report_btn)
            if dry_run:
                _mat_enable(self.accept_btn)
            else:
                out = self.settings.out_folder.strip()
                if out and ops_log_path(Path(out)).exists():
                    _mat_enable(self.revert_all_btn)

            # Log to history and show Results tab
            self._log_scan_history(
                total_files=total_scanned,
                groups=n_groups,
                duplicates=n_prev,
                dry_run=dry_run,
                src_folder=src_folder,
                applied=not dry_run,
            )
            self._update_results_tab_ui()
            self._show_results_tab()
            self._nb.select(self._tab_results)

            # Auto-open in-app report
            self.root.after(300, self._open_inapp_report)

        if paused:
            self._check_resume_state()

    def _on_error(self, msg: str, tb: str = "") -> None:
        self._progress_bar.stop()
        self._phase_label_var.set("Error — see dialog.")
        self._scanning = False
        self._scan_active_frame.pack_forget()
        self._scan_idle_frame.pack(fill=tk.X, padx=4)
        detail = f"{msg}\n\n{tb}" if tb else msg
        messagebox.showerror("Error", detail, parent=self.root)

    # ── post-scan actions ─────────────────────────────────────────────────

    def _accept_and_move(self) -> None:
        if not self.scan_groups:
            messagebox.showwarning("Accept & Move", "No groups to move.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Accept & Move",
            "Move all duplicate files to the output folder?\n"
            "Originals stay in place. This action can be reverted via 'Revert All'.",
            parent=self.root,
        ):
            return
        out = self.settings.out_folder.strip()
        if not out:
            return
        _mat_disable(self.accept_btn)
        self._phase_label_var.set("Moving files…")

        def _do_move() -> None:
            try:
                moved_orig, moved_prev = move_groups(
                    self.scan_groups, Path(out), dry_run=False, settings=self.settings
                )
                report = generate_report(
                    self.scan_groups, Path(out),
                    Path(self.settings.src_folder),
                    len(self.scan_records), self.settings,
                )
                self.report_path = report
                msg = f"Moved {moved_orig} originals + {moved_prev} duplicates."
                self.root.after(0, lambda: self._phase_label_var.set(msg))
                self.root.after(0, lambda: _mat_enable(self.revert_all_btn))
                from scan_state import delete_results
                delete_results(Path(out))
                # Update history entry applied flag
                if self._scan_history:
                    self._scan_history[-1]["applied"] = True
                    self._save_scan_history()
                    self._refresh_history_view()
                if self._last_scan_info:
                    self._last_scan_info["applied"] = True
                    self.root.after(0, self._update_results_tab_ui)
                if report:
                    self.root.after(0, lambda: webbrowser.open(report.as_uri()))
            except Exception as exc:
                self.root.after(0, lambda e=exc: messagebox.showerror(
                    "Error", str(e), parent=self.root))

        threading.Thread(target=_do_move, daemon=True).start()

    def _open_browser_report(self) -> None:
        if self.report_path and self.report_path.exists():
            webbrowser.open(self.report_path.as_uri())

    def _open_inapp_report(self) -> None:
        if not self.scan_groups and not self.scan_records:
            messagebox.showinfo("Review", "No scan results to review.", parent=self.root)
            return
        out      = self.settings.out_folder.strip()
        log_path = ops_log_path(Path(out)) if out else None

        def _apply_cb(groups: list) -> None:
            move_groups(groups, Path(out), dry_run=False, settings=self.settings)
            report = generate_report(
                self.scan_groups, Path(out),
                Path(self.settings.src_folder),
                len(self.scan_records), self.settings,
            )
            self.report_path = report
            if out:
                from scan_state import delete_results
                delete_results(Path(out))

        viewer = ReportViewer(
            self.root, self.scan_groups,
            ops_log_path=log_path,
            on_apply_cb=_apply_cb,
            solo_originals=self._solo_originals,
            broken_files=self._broken_files,
            settings=self.settings,
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
            parent=self.root,
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
        import subprocess
        win = tk.Toplevel(self.root)
        win.title("Installing rawpy…")
        win.geometry("480x220")
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text="Installing rawpy via pip…",
                  font=("Segoe UI", 10, "bold")).pack(pady=(18, 6))
        log = tk.Text(win, height=6, state=tk.DISABLED,
                      font=("Consolas", 8), relief=tk.FLAT, bg="#f4f4f4")
        log.pack(fill=tk.BOTH, expand=True, padx=12)
        close_btn = ttk.Button(win, text="Close", state=tk.DISABLED, command=win.destroy)
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
                    capture_output=True, text=True,
                )
                out  = (proc.stdout + proc.stderr).strip()
                success = proc.returncode == 0
            except Exception as exc:
                out     = str(exc)
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
