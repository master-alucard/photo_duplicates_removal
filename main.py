"""
main.py — Image Deduper GUI
Tab-based UI: Scan, Results (dynamic), History, Settings.
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import sys
import threading
import time
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
    _frozen = getattr(sys, "frozen", False)
    if _frozen:
        _msg = (
            "A required component could not be loaded.\n\n"
            f"Error: {_e}\n\n"
            "This is usually caused by Windows security blocking an app file.\n\n"
            "Try these steps:\n"
            "  1. Right-click ImageDeduper.exe → Properties → click Unblock → OK\n"
            "  2. Re-run the installer as Administrator\n"
            "  3. Temporarily disable Windows Defender / App Control and retry\n\n"
            "If the problem persists, please report it at:\n"
            "  https://github.com/master-alucard/photo_duplicates_removal/issues"
        )
    else:
        _msg = (
            "Please install requirements first:\n\n"
            "  pip install Pillow imagehash piexif\n\n"
            f"Error: {_e}"
        )
    messagebox.showerror("Startup Error", _msg)
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
from about_tab import build_about_tab
from library_tab import build_library_tab
import error_handler


# ── constants ────────────────────────────────────────────────────────────────

SETTINGS_PATH = Path(__file__).parent / "settings.json"
HISTORY_PATH  = Path(__file__).parent / "scan_history.json"
PHASE_NAMES   = ["Discovery", "Hashing", "Comparing", "Metadata", "Moving", "Report"]
_CUSTOM_PHASES = ["Main folder", "Check folder", "Comparing", "Report"]

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


# Dark protection: maps strength 1-10 → (dark_threshold, dark_tighten_factor)
_DARK_STRENGTH_MAP: list[tuple[float, float]] = [
    (10,  0.85),   # 1 — very mild
    (20,  0.75),   # 2
    (30,  0.65),   # 3
    (35,  0.57),   # 4
    (40,  0.50),   # 5 — default
    (50,  0.42),   # 6
    (60,  0.35),   # 7
    (70,  0.27),   # 8
    (85,  0.20),   # 9
    (100, 0.15),   # 10 — maximum
]

def _dark_strength_to_params(strength: float) -> tuple[float, float]:
    i = max(0, min(len(_DARK_STRENGTH_MAP) - 1, int(round(strength)) - 1))
    return _DARK_STRENGTH_MAP[i]

def _dark_params_to_strength(threshold: float, factor: float) -> float:
    best = min(range(len(_DARK_STRENGTH_MAP)),
               key=lambda i: abs(_DARK_STRENGTH_MAP[i][0] - threshold)
                             + abs(_DARK_STRENGTH_MAP[i][1] - factor) * 100)
    return float(best + 1)


# ── Quality tiers for Quick mode slider ───────────────────────────────────────
# (use_dual_hash, use_histogram, dark_protection, guard_speedup_factor, accuracy_label)
# guard_speedup_factor: speed multiplier from skipping guards only (relative to all-guards-on)
# total estimated speedup = n_threads × guard_speedup_factor
# 4 distinct guard states distributed evenly across 10 positions:
#   1–3  → all guards on  (quality zone)
#   4–6  → dark off       (balanced zone, default=5)
#   7–8  → dark+dHash off (fast zone)
#   9–10 → all off        (fastest, minor accuracy cost)
_QUALITY_TIERS: list[tuple[bool, bool, bool, float, str]] = [
    (True,  True,  True,  1.00, "100%"),   # 1 — Max quality
    (True,  True,  True,  1.00, "100%"),   # 2
    (True,  True,  True,  1.00, "100%"),   # 3
    (True,  True,  False, 1.35, "100%"),   # 4 — dark protection off
    (True,  True,  False, 1.35, "100%"),   # 5 — default
    (True,  True,  False, 1.35, "100%"),   # 6
    (False, True,  False, 1.38, "100%"),   # 7 — dHash guard also off
    (False, True,  False, 1.38, "100%"),   # 8
    (False, False, False, 1.50, "~99%"),   # 9 — all guards off
    (False, False, False, 1.50, "~98%"),   # 10 — Max speed
]

def _quality_to_params(level: int) -> tuple[bool, bool, bool]:
    """Return (use_dual_hash, use_histogram, dark_protection) for quality level 1-10."""
    t = _QUALITY_TIERS[max(0, min(9, level - 1))]
    return t[0], t[1], t[2]


def _set_interactive_state(widget: tk.Widget, state: str) -> None:
    """Recursively set state on all interactive children of a widget."""
    _interactive = (
        ttk.Entry, ttk.Button, ttk.Combobox, ttk.Scale,
        ttk.Checkbutton, ttk.Radiobutton, tk.Radiobutton, tk.Button, tk.Entry,
    )
    if isinstance(widget, _interactive):
        try:
            widget.configure(state=state)
        except tk.TclError:
            pass
    for child in widget.winfo_children():
        _set_interactive_state(child, state)


def _unique_dest(folder: Path, name: str) -> Path:
    """Return a non-colliding path inside folder for the given filename."""
    p = folder / name
    if not p.exists():
        return p
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        p = folder / f"{stem}_{i}{suffix}"
        if not p.exists():
            return p
        i += 1


def _fmt_duration(seconds: float) -> str:
    """Format a number of seconds as a human-readable duration string."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _set_sleep_prevention(enable: bool) -> None:
    """Ask Windows to keep the system awake while scanning (no-op on non-Windows)."""
    try:
        ES_CONTINUOUS      = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


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

def _find_ico() -> "Path | None":
    """Return the path to assets/app.ico, handling both source and PyInstaller bundle."""
    # PyInstaller frozen: assets live next to the EXE
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).parent / "assets" / "app.ico"
        if p.exists():
            return p
    # Running from source
    for candidate in [
        Path(__file__).parent / "assets" / "app.ico",
        Path(__file__).with_name("assets") / "app.ico",
    ]:
        if candidate.exists():
            return candidate
    return None


def _make_icon(size: int = 64) -> ImageTk.PhotoImage:
    _ico = _find_ico()
    if _ico:
        try:
            img = Image.open(_ico).convert("RGBA").resize((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            pass
    # Fallback: generate icon programmatically
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
        self.root.title("Image Deduper")
        self.root.geometry("1160x800")
        self.root.resizable(True, True)
        self.root.minsize(700, 520)

        # Refresh UI after the PC wakes from sleep (canvas can go blank)
        self.root.bind("<FocusIn>", self._on_focus_in)

        try:
            _ico = _find_ico()
            if _ico:
                root.iconbitmap(str(_ico))   # Windows: sets taskbar + title bar icon
            self._icon = _make_icon(64)
            root.wm_iconphoto(True, self._icon)  # fallback / Linux / macOS
        except Exception:
            pass

        self.settings = load_settings(SETTINGS_PATH)
        error_handler.set_settings(self.settings)
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
        self._is_paused: bool = False
        self._save_after_id = None
        self._tracker: PhaseTracker | None = None
        self._last_scan_info: dict = {}
        self._last_heartbeat: float = time.monotonic()

        # Custom scan state
        self._custom_stop_flag:  list[bool] = [False]
        self._custom_pause_flag: list[bool] = [False]
        self._custom_paused_state = None
        self._custom_is_paused: bool = False
        self._custom_groups:     list = []
        self._custom_broken:     list = []
        self._custom_report_path: Path | None = None
        self._scanning = False   # prevents concurrent scans

        self._build_ui()
        self._check_resume_state()
        self._check_custom_resume_state()
        self._check_last_results()
        self._schedule_estimate_update()
        self._heartbeat_tick()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self.root, bg=_ACCENT)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Image Deduper",
                 font=("Segoe UI", 15, "bold"), bg=_ACCENT, fg="white").pack(
            side=tk.LEFT, padx=20, pady=12)
        tk.Label(hdr, text="Find & remove duplicate images",
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
        self._tab_custom   = ttk.Frame(self._nb)
        self._tab_history  = ttk.Frame(self._nb)
        self._tab_library  = ttk.Frame(self._nb)
        self._tab_settings = ttk.Frame(self._nb)
        self._tab_about    = ttk.Frame(self._nb)

        self._nb.add(self._tab_scan,     text="  Scan  ")
        self._nb.add(self._tab_custom,   text="  Compare Scan  ")
        self._nb.add(self._tab_history,  text="  History  ")
        self._nb.add(self._tab_library,  text="  Library  ")
        self._nb.add(self._tab_settings, text="  Settings  ")
        self._nb.add(self._tab_about,    text="  About  ")

        # Init all shared vars before building tabs
        self._init_setting_vars()

        self._build_scan_tab()
        self._build_results_tab_content()
        self._build_custom_scan_tab()
        self._build_history_tab()
        self._build_library_tab()
        self._build_settings_tab()
        self._build_about_tab()

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
        self.dark_strength_var  = tk.DoubleVar(value=_dark_params_to_strength(s.dark_threshold, s.dark_tighten_factor))
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
        self.meta_csv_var       = tk.BooleanVar(value=s.collect_metadata and s.export_csv)
        self.ext_report_var     = tk.BooleanVar(value=s.extended_report)
        _dsort = "exif" if s.sort_by_exif_date else ("filename" if s.sort_by_filename_date else "mtime")
        self.date_sort_var      = tk.StringVar(value=_dsort)
        self.mindim_var         = tk.DoubleVar(value=s.min_dimension)
        self.recursive_var      = tk.BooleanVar(value=s.recursive)
        self.skip_names_var     = tk.StringVar(value=s.skip_names)
        self.quick_scan_speed_var = tk.IntVar(value=s.scan_speed)
        import os as _os
        _default_threads = max(1, _os.cpu_count() or 1) if s.scan_threads == 0 else max(1, s.scan_threads)
        self.scan_threads_var = tk.StringVar(value=str(_default_threads))
        self.scan_threads_var.trace_add("write", self._on_setting_change)
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

        # Auto-update
        self.auto_update_var = tk.BooleanVar(value=s.auto_update)
        self.auto_update_var.trace_add("write", self._on_setting_change)

        self._calib_info_var = tk.StringVar(value=self._calib_info_text())
        self.developer_mode_var = tk.BooleanVar(value=s.developer_mode)
        self.developer_mode_var.trace_add("write", self._on_setting_change)

        # Library source mode — "browse" or "library" per folder picker
        self.src_mode_var        = tk.StringVar(value="browse")
        self.trust_lib_src_var   = tk.BooleanVar(value=False)
        self.main_mode_var       = tk.StringVar(value="browse")
        self.trust_lib_main_var  = tk.BooleanVar(value=False)
        self.check_mode_var      = tk.StringVar(value="browse")
        self.trust_lib_check_var = tk.BooleanVar(value=False)

        # Custom scan folder vars
        s2 = self.settings
        self._custom_main_var    = tk.StringVar(value=s2.custom_main_folder)
        self._custom_check_var   = tk.StringVar(value=s2.custom_check_folder)
        self._custom_out_var     = tk.StringVar(value=s2.custom_out_folder or s2.out_folder)
        self._custom_phase_label = tk.StringVar(value="Ready.")
        self._custom_eta_var     = tk.StringVar(value="")
        self._custom_details_var = tk.BooleanVar(value=s2.details_visible)
        self._custom_tracker: PhaseTracker | None = None
        self._custom_estimate_var = tk.StringVar(value="Select folders to see estimate.")
        self._custom_dry_var     = tk.BooleanVar(value=s2.dry_run)
        self._custom_dry_var.trace_add("write", self._on_setting_change)

        # Add traces
        for var in (
            self.thresh_var, self.ratio_var, self.series_tol_var, self.series_thresh_var,
            self.ar_tol_var, self.dark_strength_var, self.hist_sim_var,
            self.brightness_diff_var, self.ambig_factor_var, self.mindim_var,
        ):
            var.trace_add("write", lambda *_: self._on_setting_change())

        for var in (
            self.dark_var, self.dual_hash_var, self.hist_var, self.ambig_var,
            self.disable_series_var, self.rawpy_var, self.all_formats_var,
            self.prefer_meta_var, self.meta_csv_var,
            self.ext_report_var,
            self.recursive_var, self.dry_var, self.org_date_var,
        ):
            var.trace_add("write", self._on_setting_change)
        self.date_sort_var.trace_add("write", self._on_setting_change)

        self.strategy_var.trace_add("write", self._on_setting_change)
        self.skip_names_var.trace_add("write", self._on_setting_change)
        self._date_sep_var.trace_add("write", self._on_date_sep_change)
        self._date_order_var.trace_add("write", self._on_date_fmt_change)

    # ── Scan tab ──────────────────────────────────────────────────────────

    def _build_scan_tab(self) -> None:
        tab = self._tab_scan

        # Scrollable body (outer reference saved so we can hide it when showing results)
        self._scan_form_outer, body = _scrollable_frame(tab)

        # Folders
        self._scan_folders_section = _section(body, "Folders")
        self.src_var = self._lib_folder_row(
            self._scan_folders_section, "Source folder:", "src_folder",
            self.src_mode_var, self.trust_lib_src_var,
            browse_cmd=lambda v: self._browse(v, "src_folder"),
            change_cb=self._on_folder_change,
        )
        self.out_var = self._folder_row(self._scan_folders_section, "Output folder:", "out_folder")

        # Mode toggle
        self._scan_mode_card = ttk.LabelFrame(body, text="Mode", padding=(10, 6, 10, 8))
        self._scan_mode_card.pack(fill=tk.X, pady=(0, 6))
        mode_row = ttk.Frame(self._scan_mode_card)
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

        # Quick Speed card (quick mode only)
        self._quick_speed_frame = ttk.LabelFrame(body, text="Scan Speed", padding=(10, 8, 10, 10))
        _qs_row = ttk.Frame(self._quick_speed_frame)
        _qs_row.pack(fill=tk.X)
        ttk.Label(_qs_row, text="Quality", foreground="#666",
                  font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        self._quick_spd_slider = ttk.Scale(
            _qs_row, from_=1, to=10, orient=tk.HORIZONTAL, length=220,
            variable=self.quick_scan_speed_var,
        )
        self._quick_spd_slider.pack(side=tk.LEFT)
        ttk.Label(_qs_row, text="Speed", foreground="#666",
                  font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(6, 16))
        self._quick_spd_info = ttk.Label(_qs_row, text="", foreground=_ACCENT,
                                         font=("Segoe UI", 9, "bold"))
        self._quick_spd_info.pack(side=tk.LEFT)

        # Dev-mode parameter detail row (shown below slider)
        self._quick_spd_dev_lbl = ttk.Label(
            self._quick_speed_frame, text="", foreground="#888",
            font=("Segoe UI", 7), justify=tk.LEFT,
        )

        def _quick_speed_param_text(level: int) -> str:
            udh, uhi, udp = _quality_to_params(level)
            try:
                n_thr = int(self.scan_threads_var.get())
            except Exception:
                n_thr = 1
            parts = [f"threads: {n_thr}"]
            if not udp:
                parts.append("dark protection: off")
            if not udh:
                parts.append("dHash guard: off")
            if not uhi:
                parts.append("histogram guard: off")
            return "  ·  ".join(parts)

        def _update_quick_speed(*_):
            level = max(1, min(10, int(round(self.quick_scan_speed_var.get()))))
            tier = _QUALITY_TIERS[level - 1]
            guard_x = tier[3]
            acc = tier[4]
            if guard_x == 1.0:
                spd_text = "1×  —  100% accuracy"
                fg = _ACCENT
            elif acc == "100%":
                spd_text = f"~{guard_x:.2g}×  —  100% accuracy"
                fg = _ACCENT
            else:
                spd_text = f"~{guard_x:.2g}×  —  accuracy {acc}"
                fg = _M_AMBER
            self._quick_spd_info.configure(text=spd_text, foreground=fg)
            # Dev-mode detail label (immediate, lightweight)
            if self.developer_mode_var.get():
                self._quick_spd_dev_lbl.configure(text=_quick_speed_param_text(level))
                self._quick_spd_dev_lbl.pack(anchor=tk.W, padx=4, pady=(2, 0))
            else:
                self._quick_spd_dev_lbl.pack_forget()
            # Debounced estimate update — only fires after sliding stops
            self._schedule_estimate_update(delay_ms=400)

        # Refresh when dev mode or thread count changes
        self.developer_mode_var.trace_add("write", lambda *_: _update_quick_speed())
        self.quick_scan_speed_var.trace_add("write", _update_quick_speed)
        self.scan_threads_var.trace_add("write", _update_quick_speed)
        _update_quick_speed()

        _qs_fmt_row = ttk.Frame(self._quick_speed_frame)
        _qs_fmt_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(_qs_fmt_row, text="Keep all formats (keep best copy per file extension)",
                        variable=self.all_formats_var).pack(side=tk.LEFT)
        _info_btn(_qs_fmt_row, "keep_all_formats").pack(side=tk.LEFT, padx=2)

        # Compact key settings (advanced mode only)
        self._compact_adv_frame = ttk.LabelFrame(body, text="Key Settings", padding=(10, 6, 10, 8))
        _crows = [ttk.Frame(self._compact_adv_frame) for _ in range(4)]
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

        ttk.Checkbutton(_crows[3], text="Keep all formats (keep best copy per file extension)",
                        variable=self.all_formats_var).pack(side=tk.LEFT)
        _info_btn(_crows[3], "keep_all_formats").pack(side=tk.LEFT, padx=2)

        # Actions
        act = _section(body, "Actions")

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
        self._scan_btn_bar = tk.Frame(tab, bg=_ACCENT_TINT, pady=7)
        btn_bar = self._scan_btn_bar
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

        # Inline results frame — shown after scan completes, replaces the form
        self._scan_inline_result_frame = tk.Frame(tab, bg=_BG)
        # (not packed until scan done)

    # ── Scan inline results ───────────────────────────────────────────────

    def _build_results_tab_content(self) -> None:
        tab = self._scan_inline_result_frame

        # Container shown when viewer is NOT active (summary + buttons)
        self._results_summary_frame = tk.Frame(tab, bg=_BG)
        self._results_summary_frame.pack(fill=tk.BOTH, expand=True)
        sf = self._results_summary_frame

        # Placeholder shown before first scan
        self._results_placeholder = tk.Label(
            sf, text="Run a scan to review results here.",
            font=("Segoe UI", 11), bg=_BG, fg="#9E9E9E",
        )
        self._results_placeholder.pack(expand=True)

        # Success card (hidden until scan completes; contents rebuilt dynamically)
        self._results_info_card = tk.Frame(sf, bg=_CARD_BG, bd=0, relief=tk.FLAT,
                                           highlightbackground=_M_DIVIDER,
                                           highlightthickness=1)

        # Action buttons row (separate so _on_done can enable/disable each independently)
        self._results_btn_row = tk.Frame(sf, bg=_BG)
        btn_row = self._results_btn_row

        self.inapp_report_btn = _mat_btn(btn_row, "📋  View Report",
                                         self._open_inapp_report, _ACCENT, font_size=10)
        self.inapp_report_btn.pack(side=tk.LEFT, padx=(0, 6))
        _mat_disable(self.inapp_report_btn)

        self.browser_report_btn = _mat_btn(btn_row, "🌐  HTML Report",
                                           self._open_browser_report, "#546E7A")
        self.browser_report_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self.browser_report_btn)

        self.accept_btn = _mat_btn(btn_row, "✓  Accept & Move",
                                   self._accept_and_move, _M_SUCCESS)
        self.accept_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self.accept_btn)

        self.revert_all_btn = _mat_btn(btn_row, "⟲  Revert All", self._revert_all, _M_WARNING)
        self.revert_all_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self.revert_all_btn)

        # Divider
        self._results_divider = tk.Frame(sf, height=1, bg=_M_DIVIDER)

        # Start New Scan
        self._results_new_frame = tk.Frame(sf, bg=_BG)
        _mat_btn(self._results_new_frame, "   +  Start New Scan   ",
                 self._new_scan_prompt, _ACCENT, font_size=11).pack()
        ttk.Label(self._results_new_frame,
                  text="Clears current results and starts a new scan.",
                  foreground="#888", font=("Segoe UI", 8)).pack(pady=(6, 0))

        # Container for the embedded ReportViewer (packed on demand)
        self._results_viewer_host = tk.Frame(tab, bg=_BG)

    def _embed_report_viewer(
        self,
        groups: list,
        solo_originals: list,
        broken_files: list,
        out: str,
        apply_cb=None,
    ) -> None:
        """Embed ReportViewer as a Frame inside the Results tab."""
        # Clear any existing viewer
        for w in self._results_viewer_host.winfo_children():
            w.destroy()

        # Hide summary, show viewer host
        self._results_summary_frame.pack_forget()
        self._results_viewer_host.pack(fill=tk.BOTH, expand=True)

        log_path = ops_log_path(Path(out)) if out else None

        def _on_close():
            self._results_viewer_host.pack_forget()
            for w in self._results_viewer_host.winfo_children():
                w.destroy()
            self._results_summary_frame.pack(fill=tk.BOTH, expand=True)

        viewer = ReportViewer(
            self._results_viewer_host,
            groups,
            ops_log_path=log_path,
            on_apply_cb=apply_cb,
            solo_originals=solo_originals,
            broken_files=broken_files,
            settings=self.settings,
            on_close_cb=_on_close,
        )
        viewer.pack(fill=tk.BOTH, expand=True)

    def _show_results_tab(self) -> None:
        """Show inline scan results within the Scan tab (replaces old separate Results tab)."""
        self._scan_form_outer.pack_forget()
        self._prog_frame.pack_forget()
        self._scan_btn_bar.pack_forget()
        self._scan_inline_result_frame.pack(fill=tk.BOTH, expand=True)

    def _hide_results_tab(self) -> None:
        """Restore the scan form by hiding the inline results view."""
        self._scan_inline_result_frame.pack_forget()
        # Clear any embedded viewer so it's fresh next time
        for w in self._results_viewer_host.winfo_children():
            w.destroy()
        self._results_viewer_host.pack_forget()
        self._results_summary_frame.pack(fill=tk.BOTH, expand=True)
        # Restore scan form panels in original pack order
        self._scan_form_outer.pack(fill=tk.BOTH, expand=True)
        self._prog_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self._scan_btn_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _update_results_tab_ui(self, extra: "dict | None" = None) -> None:
        """Rebuild the success card and show/enable action buttons on the Results tab."""
        if extra:
            self._last_scan_info.update(extra)
        i = self._last_scan_info
        if not i:
            return

        ts         = i.get("date", "")
        src        = i.get("src_folder", "–")
        files      = i.get("total_files", 0)
        n_groups   = i.get("groups", 0)
        n_dupes    = i.get("duplicates", 0)
        n_solo     = i.get("n_solo", 0)
        n_ambig    = i.get("n_ambiguous", 0)
        space_b    = i.get("space_saved", 0)
        applied    = i.get("applied", False)
        dur_s      = i.get("duration_s", 0.0)

        # Space label
        space_mb = space_b / (1024 * 1024) if space_b else 0.0
        if space_mb >= 1024:
            space_lbl = f"{space_mb / 1024:.1f} GB"
        elif space_mb >= 1:
            space_lbl = f"{space_mb:.1f} MB"
        elif space_b > 0:
            space_lbl = f"{space_b // 1024} KB"
        else:
            space_lbl = "–"

        # ── Rebuild card contents ─────────────────────────────────────────
        for w in self._results_info_card.winfo_children():
            w.destroy()

        # Green top bar
        bar_col = _M_SUCCESS if n_dupes > 0 else _ACCENT
        tk.Frame(self._results_info_card, height=4, bg=bar_col).pack(fill=tk.X)

        # Header row
        hdr = tk.Frame(self._results_info_card, bg=_CARD_BG)
        hdr.pack(fill=tk.X, padx=16, pady=(12, 4))
        title = "✅  Scan Complete" + ("  ·  ✓ Applied" if applied else "")
        tk.Label(hdr, text=title,
                 font=("Segoe UI", 12, "bold"), bg=_CARD_BG, fg=bar_col).pack(side=tk.LEFT)
        if ts:
            tk.Label(hdr, text=ts, font=("Segoe UI", 8),
                     bg=_CARD_BG, fg="#9E9E9E").pack(side=tk.RIGHT, pady=(2, 0))

        # Source path
        tk.Label(self._results_info_card,
                 text=f"📁  {src}", font=("Segoe UI", 9),
                 bg=_CARD_BG, fg=_M_TEXT2, anchor=tk.W,
                 wraplength=900).pack(fill=tk.X, padx=16, pady=(0, 10))

        tk.Frame(self._results_info_card, height=1, bg=_M_DIVIDER).pack(
            fill=tk.X, padx=16, pady=(0, 10))

        # Stat cells row
        stats_row = tk.Frame(self._results_info_card, bg=_CARD_BG)
        stats_row.pack(fill=tk.X, padx=16, pady=(0, 10))

        def _stat_cell(parent, value, label, fg=_M_TEXT1):
            cell = tk.Frame(parent, bg=_CARD_BG)
            cell.pack(side=tk.LEFT, padx=(0, 28))
            vstr = f"{value:,}" if isinstance(value, int) else str(value)
            tk.Label(cell, text=vstr, font=("Segoe UI", 20, "bold"),
                     bg=_CARD_BG, fg=fg).pack(anchor=tk.W)
            tk.Label(cell, text=label, font=("Segoe UI", 8),
                     bg=_CARD_BG, fg="#9E9E9E").pack(anchor=tk.W)

        _stat_cell(stats_row, files,   "files scanned")
        _stat_cell(stats_row, n_groups, "dup groups",
                   _M_ERROR if n_groups > 0 else _M_TEXT1)
        _stat_cell(stats_row, n_dupes,  "duplicates",
                   _M_ERROR if n_dupes > 0 else _M_TEXT1)
        _stat_cell(stats_row, space_lbl, "space to free", _M_SUCCESS if space_b > 0 else _M_TEXT1)
        if dur_s:
            _stat_cell(stats_row, _fmt_duration(dur_s), "scan time")

        # Summary line
        parts: list[str] = []
        if n_solo:
            parts.append(f"✓  {n_solo:,} safe originals")
        if n_ambig:
            parts.append(f"⚠  {n_ambig} need review")
        if n_groups == 0:
            parts.append("No duplicates found — your collection looks clean!")
        if parts:
            tk.Label(self._results_info_card,
                     text="   ·   ".join(parts),
                     font=("Segoe UI", 9), bg=_CARD_BG,
                     fg="#9E9E9E", anchor=tk.W).pack(
                         fill=tk.X, padx=16, pady=(0, 14))

        # Show card and buttons (hide placeholder)
        self._results_placeholder.pack_forget()
        self._results_info_card.pack(fill=tk.X, padx=16, pady=(16, 8))
        self._results_btn_row.pack(fill=tk.X, padx=16, pady=6)
        self._results_divider.pack(fill=tk.X, padx=16, pady=14)
        self._results_new_frame.pack(pady=10)

    # ── History tab ───────────────────────────────────────────────────────

    def _build_history_tab(self) -> None:
        tab = self._tab_history

        header = tk.Frame(tab, bg=_BG)
        header.pack(fill=tk.X, padx=8, pady=(10, 4))
        tk.Label(header, text="Scan History", font=("Segoe UI", 10, "bold"),
                 bg=_BG, fg=_M_TEXT1).pack(side=tk.LEFT)

        cols = ("date", "duration", "src", "files", "groups", "dups", "dup_pct", "dry_run", "applied")
        self._hist_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="browse", height=16)

        col_cfg = [
            ("date",     "Date",      130, "w"),
            ("duration", "Duration",   70, "center"),
            ("src",      "Source",    210, "w"),
            ("files",    "Files",      60, "center"),
            ("groups",   "Groups",     60, "center"),
            ("dups",     "Dups",       60, "center"),
            ("dup_pct",  "Dup %",      60, "center"),
            ("dry_run",  "Dry Run",    65, "center"),
            ("applied",  "Applied",    65, "center"),
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
            _dur = entry.get("duration_s", 0.0)
            self._hist_tree.insert("", "end", values=(
                entry.get("date", ""),
                _fmt_duration(_dur) if _dur else "–",
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
        duration_s: float = 0.0,
    ) -> None:
        dup_pct = duplicates / total_files * 100 if total_files > 0 else 0.0
        entry = {
            "date":        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "duration_s":  round(duration_s, 1),
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

        # ── Detection ────────────────────────────────────────────────────
        det = _section(body, "Detection")

        self._slider_row(det, "Similarity Threshold", self.thresh_var,
                         1, 30, 2, 12, 2, "threshold", 1,
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

        # ── Parallel threads ──────────────────────────────────────────────
        import os as _os
        _max_thr = max(1, _os.cpu_count() or 1)
        _thr_opts: list[str] = []
        _n = 1
        while _n < _max_thr:
            _thr_opts.append(str(_n))
            _n *= 2
        _thr_opts.append(str(_max_thr))

        thr_r = _row(det)
        _label(thr_r, "Parallel threads:")
        self._threads_combo = ttk.Combobox(
            thr_r, values=_thr_opts, textvariable=self.scan_threads_var,
            width=6, state="readonly",
        )
        self._threads_combo.pack(side=tk.LEFT)
        ttk.Label(thr_r, text=f"(max: {_max_thr} on this machine)",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)

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

        self._calib_info_lbl = ttk.Label(calib_sec, textvariable=self._calib_info_var,
                                         foreground=_M_SUCCESS if self.settings.calibrated_threshold > 0 else "#999",
                                         font=("Segoe UI", 8, "bold"))
        self._calib_info_lbl.pack(anchor=tk.W, pady=(2, 2))

        ttk.Label(calib_sec,
                  text="Calibration finds the best threshold and ratio for your specific photo library.",
                  foreground="#666", font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(0, 2))

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
        self._slider_row(hash_sec, "  Protection Strength  (1 = mild → 10 = max)",
                         self.dark_strength_var,
                         1.0, 10.0, 4.0, 6.0, 5.0, "dark_protection_strength", 1.0,
                         lambda v: str(int(round(v))))

        r = _row(hash_sec)
        _label(r, "Verification guards:")
        ttk.Checkbutton(r, text="dHash", variable=self.dual_hash_var).pack(side=tk.LEFT, padx=(4, 0))
        _info_btn(r, "use_dual_hash").pack(side=tk.LEFT, padx=(2, 8))
        ttk.Checkbutton(r, text="Histogram", variable=self.hist_var).pack(side=tk.LEFT)
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
        ttk.Checkbutton(r, text="Export metadata CSV  (collects & saves EXIF on each scan)",
                        variable=self.meta_csv_var).pack(side=tk.LEFT)
        _info_btn(r, "export_csv").pack(side=tk.LEFT, padx=2)

        r = _row(meta_sec)
        ttk.Checkbutton(r, text="Extended report (EXIF per image)",
                        variable=self.ext_report_var).pack(side=tk.LEFT)
        _info_btn(r, "extended_report").pack(side=tk.LEFT, padx=2)

        r = _row(meta_sec)
        _label(r, "Date source (oldest strategy):")
        ttk.Radiobutton(r, text="File date", variable=self.date_sort_var,
                        value="mtime").pack(side=tk.LEFT, padx=(4, 0))
        ttk.Radiobutton(r, text="Filename", variable=self.date_sort_var,
                        value="filename").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(r, text="EXIF", variable=self.date_sort_var,
                        value="exif").pack(side=tk.LEFT, padx=(6, 0))
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

        # ── Developer ─────────────────────────────────────────────────────
        dev_sec = _section(body, "Developer")
        dev_card = tk.Frame(dev_sec, bg="#FFF8E1", padx=12, pady=10,
                            highlightbackground="#FFD54F", highlightthickness=1)
        dev_card.pack(fill=tk.X)
        tk.Label(dev_card, text="🛠  Developer Mode",
                 font=("Segoe UI", 9, "bold"),
                 bg="#FFF8E1", fg="#E65100").pack(anchor=tk.W)
        tk.Label(
            dev_card,
            text=(
                "When ON — all errors show the full technical details and traceback.\n"
                "When OFF (default) — errors show a simple, plain-language message."
            ),
            font=("Segoe UI", 8), bg="#FFF8E1", fg="#795548",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 8))
        ttk.Checkbutton(
            dev_card,
            text="Enable Developer Mode  (show full error details)",
            variable=self.developer_mode_var,
        ).pack(anchor=tk.W)

    # ── Compare Scan tab ──────────────────────────────────────────────────

    def _build_custom_scan_tab(self) -> None:
        """Cross-folder duplicate finder: main folder (read-only) vs check folder."""
        tab = self._tab_custom
        self._custom_form_outer, body = _scrollable_frame(tab)

        # ── Info banner ───────────────────────────────────────────────────
        banner = tk.Frame(body, bg="#E8F5E9", bd=0)
        banner.pack(fill=tk.X, pady=(0, 8))
        tk.Frame(banner, height=3, bg=_M_SUCCESS).pack(fill=tk.X)
        tk.Label(
            banner,
            text=(
                "Compare Scan compares a reference folder against a second folder.\n"
                "Files in the Main folder are never moved or deleted.\n"
                "Only duplicates found in the Check folder are moved to trash."
            ),
            bg="#E8F5E9", fg="#1B5E20",
            font=("Segoe UI", 8), justify=tk.LEFT, padx=12, pady=8,
        ).pack(anchor=tk.W)

        # ── Folders ───────────────────────────────────────────────────────
        self._custom_folders_section = _section(body, "Folders")

        def _cust_folder_row(parent, label, var, key):
            f = ttk.Frame(parent)
            f.pack(fill=tk.X, pady=3)
            ttk.Label(f, text=label, width=22, anchor=tk.W).pack(side=tk.LEFT)
            ttk.Entry(f, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            ttk.Button(f, text="Browse…",
                       command=lambda v=var: self._browse_custom(v)).pack(side=tk.RIGHT)
            var.trace_add("write", self._on_custom_folder_change)

        self._lib_folder_row(
            self._custom_folders_section, "Main folder (reference):", "custom_main_folder",
            self.main_mode_var, self.trust_lib_main_var,
            browse_cmd=lambda v: self._browse_custom(v),
            change_cb=self._on_custom_folder_change,
            label_width=22, path_var=self._custom_main_var,
        )
        self._lib_folder_row(
            self._custom_folders_section, "Check folder (find dups):", "custom_check_folder",
            self.check_mode_var, self.trust_lib_check_var,
            browse_cmd=lambda v: self._browse_custom(v),
            change_cb=self._on_custom_folder_change,
            label_width=22, path_var=self._custom_check_var,
        )
        _cust_folder_row(self._custom_folders_section, "Output / trash folder:", self._custom_out_var, "custom_out_folder")

        # ── Key Settings (identical to Scan → Advanced "Key Settings") ──
        self._custom_ks_frame = ttk.LabelFrame(body, text="Key Settings", padding=(10, 6, 10, 8))
        ks = self._custom_ks_frame
        ks.pack(fill=tk.X, pady=(0, 6))
        _ksrows = [ttk.Frame(ks) for _ in range(4)]
        for kr in _ksrows:
            kr.pack(fill=tk.X, pady=2)

        # Row 0: Ambiguous + Disable Series (side by side)
        ttk.Checkbutton(_ksrows[0], text="Ambiguous Match Detection",
                        variable=self.ambig_var).pack(side=tk.LEFT)
        _info_btn(_ksrows[0], "ambiguous_detection").pack(side=tk.LEFT, padx=2)
        ttk.Label(_ksrows[0], text="  ", width=3).pack(side=tk.LEFT)
        ttk.Checkbutton(_ksrows[0], text="Disable Series Detection",
                        variable=self.disable_series_var).pack(side=tk.LEFT)

        # Row 1: Recursive + Rawpy (side by side)
        ttk.Checkbutton(_ksrows[1], text="Scan subfolders recursively",
                        variable=self.recursive_var).pack(side=tk.LEFT)
        _info_btn(_ksrows[1], "recursive").pack(side=tk.LEFT, padx=2)
        ttk.Label(_ksrows[1], text="  ", width=3).pack(side=tk.LEFT)
        self._compact_rawpy_cb2 = ttk.Checkbutton(
            _ksrows[1], text="Use rawpy for RAW files", variable=self.rawpy_var,
            state=tk.NORMAL if _RAWPY_AVAILABLE else tk.DISABLED,
        )
        self._compact_rawpy_cb2.pack(side=tk.LEFT)
        if not _RAWPY_AVAILABLE:
            ttk.Label(_ksrows[1], text="(not installed)", foreground="#e03",
                      font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=2)

        # Row 2: Keep strategy
        ttk.Label(_ksrows[2], text="Prefer to keep:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Radiobutton(_ksrows[2], text="Largest resolution",
                        variable=self.strategy_var, value="pixels").pack(side=tk.LEFT)
        ttk.Radiobutton(_ksrows[2], text="Oldest file date",
                        variable=self.strategy_var, value="oldest").pack(side=tk.LEFT, padx=6)
        _info_btn(_ksrows[2], "keep_strategy").pack(side=tk.LEFT, padx=2)

        # Row 3: Keep all formats
        ttk.Checkbutton(_ksrows[3], text="Keep all formats (keep best copy per file extension)",
                        variable=self.all_formats_var).pack(side=tk.LEFT)
        _info_btn(_ksrows[3], "keep_all_formats").pack(side=tk.LEFT, padx=2)

        # ── Actions ───────────────────────────────────────────────────────
        act = _section(body, "Actions")



        r = _row(act)
        ttk.Checkbutton(r, text="Organize by Date", variable=self.org_date_var).pack(side=tk.LEFT)
        _info_btn(r, "organize_by_date").pack(side=tk.LEFT, padx=2)
        ttk.Label(r, text="Create date subfolders in results/ and trash/",
                  foreground="#666", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=8)

        # Date format
        r = _row(act)
        ttk.Label(r, text="  Date order:", width=12, anchor=tk.W).pack(side=tk.LEFT)
        init_order_idx2, init_sep2 = self._guess_order_sep(self.settings.date_folder_format)
        self._custom_date_order_cb = ttk.Combobox(r, textvariable=self._date_order_var,
                                                   width=14, state="readonly")
        self._custom_date_order_cb.pack(side=tk.LEFT)
        ttk.Label(r, text="  Separator:").pack(side=tk.LEFT, padx=(8, 0))
        self._custom_date_sep_cb = ttk.Combobox(r, textvariable=self._date_sep_var,
                                                 values=self._DATE_SEPARATORS, width=4, state="readonly")
        self._custom_date_sep_cb.pack(side=tk.LEFT, padx=(2, 0))
        _info_btn(r, "date_folder_format").pack(side=tk.LEFT, padx=4)
        ttk.Label(r, textvariable=self._date_fmt_example,
                  foreground="#555", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)
        self._refresh_date_order_choices(init_sep2, init_order_idx2)

        # Estimate
        self._custom_estimate_frame = ttk.Frame(body)
        self._custom_estimate_frame.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(self._custom_estimate_frame, textvariable=self._custom_estimate_var,
                  foreground="#555", font=("Segoe UI", 8, "italic")).pack(anchor=tk.W)

        # Resume notice (Compare Scan)
        self._custom_resume_frame = ttk.Frame(body)
        self._custom_resume_frame.pack(fill=tk.X, pady=(2, 2))
        self._custom_resume_var = tk.StringVar()
        self._custom_resume_lbl = ttk.Label(
            self._custom_resume_frame, textvariable=self._custom_resume_var,
            foreground="#7c3aed", font=("Segoe UI", 8, "bold"))
        self._custom_resume_btn = ttk.Button(
            self._custom_resume_frame, text="Resume",
            command=self._resume_custom_scan)
        self._custom_discard_btn = ttk.Button(
            self._custom_resume_frame, text="Discard",
            command=self._discard_custom_resume)

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
        ttk.Checkbutton(
            self._custom_prog_frame, text="Show phase details",
            variable=self._custom_details_var, command=self._toggle_custom_details,
        ).pack(anchor=tk.W, pady=(2, 0))
        self._custom_detail_text = tk.Text(
            self._custom_prog_frame, height=5, state=tk.DISABLED,
            font=("Consolas", 8), bg="#f8f8f8", relief=tk.FLAT,
        )

        # ── Button bar (fixed very bottom) ───────────────────────────────���
        self._custom_btn_bar = tk.Frame(tab, bg=_ACCENT_TINT, pady=7)
        c_btn_bar = self._custom_btn_bar
        c_btn_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(c_btn_bar, height=1, bg=_M_DIVIDER).place(relx=0, rely=0, relwidth=1)

        _GR = "#757575"

        self._custom_idle_frame = tk.Frame(c_btn_bar, bg=_ACCENT_TINT)
        self._custom_idle_frame.pack(fill=tk.X, padx=4)

        # Left side: Reset Defaults + Last Calibration
        _mat_btn(self._custom_idle_frame, "Reset Defaults",
                 self._reset_defaults, _GR).pack(side=tk.LEFT, padx=(4, 4))

        self._custom_last_calib_btn = _mat_btn(
            self._custom_idle_frame, "↩ Last Calibration",
            self._apply_last_calibration, _ACCENT)
        self._custom_last_calib_btn.pack(side=tk.LEFT, padx=4)
        if self.settings.calibrated_threshold == 0:
            _mat_disable(self._custom_last_calib_btn)

        # Right side: Start + Accept + Review + Browser
        self._custom_scan_btn = _mat_btn(
            self._custom_idle_frame, "▶  Start Compare Scan",
            self._start_custom_scan, _M_SUCCESS)
        self._custom_scan_btn.pack(side=tk.RIGHT, padx=(4, 8))

        self._custom_accept_btn = _mat_btn(
            self._custom_idle_frame, "✓  Accept & Move",
            self._custom_accept_and_move, _M_SUCCESS)
        self._custom_accept_btn.pack(side=tk.RIGHT, padx=4)
        _mat_disable(self._custom_accept_btn)

        self._custom_inapp_btn = _mat_btn(
            self._custom_idle_frame, "Review In-App",
            self._custom_open_inapp_report, _ACCENT)
        self._custom_inapp_btn.pack(side=tk.RIGHT, padx=4)
        _mat_disable(self._custom_inapp_btn)

        self._custom_browser_btn = _mat_btn(
            self._custom_idle_frame, "Browser Report",
            self._custom_open_browser_report, "#757575")
        self._custom_browser_btn.pack(side=tk.RIGHT, padx=4)
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

        # Inline results frame — shown after compare scan completes, replaces the form
        self._custom_inline_result_frame = tk.Frame(tab, bg=_BG)
        self._build_custom_inline_results()

    # ── Compare Scan inline results ─────────────────���─────────────────────

    def _build_custom_inline_results(self) -> None:
        """Build the inline results view for the Compare Scan tab."""
        tab = self._custom_inline_result_frame

        # Summary container (visible when viewer is NOT active)
        self._custom_results_summary_frame = tk.Frame(tab, bg=_BG)
        self._custom_results_summary_frame.pack(fill=tk.BOTH, expand=True)
        sf = self._custom_results_summary_frame

        # Stats card (rebuilt dynamically after each scan)
        self._custom_results_info_card = tk.Frame(
            sf, bg=_CARD_BG, bd=0, relief=tk.FLAT,
            highlightbackground=_M_DIVIDER, highlightthickness=1,
        )

        # Action buttons row
        self._custom_results_btn_row = tk.Frame(sf, bg=_BG)
        cbr = self._custom_results_btn_row

        self._cr_inapp_btn = _mat_btn(cbr, "📋  View Report",
                                      self._custom_open_inapp_report, _ACCENT, font_size=10)
        self._cr_inapp_btn.pack(side=tk.LEFT, padx=(0, 6))
        _mat_disable(self._cr_inapp_btn)

        self._cr_browser_btn = _mat_btn(cbr, "🌐  HTML Report",
                                        self._custom_open_browser_report, "#546E7A")
        self._cr_browser_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self._cr_browser_btn)

        self._cr_accept_btn = _mat_btn(cbr, "✓  Accept & Move",
                                       self._custom_accept_and_move, _M_SUCCESS)
        self._cr_accept_btn.pack(side=tk.LEFT, padx=4)
        _mat_disable(self._cr_accept_btn)

        # Divider
        self._custom_results_divider = tk.Frame(sf, height=1, bg=_M_DIVIDER)

        # Start New Compare Scan
        self._custom_results_new_frame = tk.Frame(sf, bg=_BG)
        _mat_btn(self._custom_results_new_frame, "   +  Start New Compare Scan   ",
                 self._new_custom_scan, _ACCENT, font_size=11).pack()
        ttk.Label(self._custom_results_new_frame,
                  text="Clears current results and starts a new compare scan.",
                  foreground="#888", font=("Segoe UI", 8)).pack(pady=(6, 0))

        # Container for embedded ReportViewer (shown on demand)
        self._custom_results_viewer_host = tk.Frame(tab, bg=_BG)

    def _update_custom_results_ui(
        self, n_main: int, n_check: int, n_cross: int,
        n_dups: int, dry_run: bool, src_folder: str,
        duration_s: float = 0.0,
    ) -> None:
        """Rebuild the compare-scan stats card and show action buttons."""
        for w in self._custom_results_info_card.winfo_children():
            w.destroy()

        bar_col = _M_SUCCESS if n_dups > 0 else _ACCENT
        tk.Frame(self._custom_results_info_card, height=4, bg=bar_col).pack(fill=tk.X)

        hdr = tk.Frame(self._custom_results_info_card, bg=_CARD_BG)
        hdr.pack(fill=tk.X, padx=16, pady=(12, 4))
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        tk.Label(hdr, text="✅  Compare Scan Complete",
                 font=("Segoe UI", 12, "bold"), bg=_CARD_BG, fg=bar_col).pack(side=tk.LEFT)
        tk.Label(hdr, text=ts, font=("Segoe UI", 8),
                 bg=_CARD_BG, fg="#9E9E9E").pack(side=tk.RIGHT, pady=(2, 0))

        tk.Label(self._custom_results_info_card,
                 text=f"📁  {src_folder}", font=("Segoe UI", 9),
                 bg=_CARD_BG, fg=_M_TEXT2, anchor=tk.W,
                 wraplength=900).pack(fill=tk.X, padx=16, pady=(0, 10))

        tk.Frame(self._custom_results_info_card, height=1, bg=_M_DIVIDER).pack(
            fill=tk.X, padx=16, pady=(0, 10))

        stats_row = tk.Frame(self._custom_results_info_card, bg=_CARD_BG)
        stats_row.pack(fill=tk.X, padx=16, pady=(0, 10))

        def _stat_cell(parent, value, label, fg=_M_TEXT1):
            cell = tk.Frame(parent, bg=_CARD_BG)
            cell.pack(side=tk.LEFT, padx=(0, 28))
            vstr = f"{value:,}" if isinstance(value, int) else str(value)
            tk.Label(cell, text=vstr, font=("Segoe UI", 20, "bold"),
                     bg=_CARD_BG, fg=fg).pack(anchor=tk.W)
            tk.Label(cell, text=label, font=("Segoe UI", 8),
                     bg=_CARD_BG, fg="#9E9E9E").pack(anchor=tk.W)

        _stat_cell(stats_row, n_main,  "main files")
        _stat_cell(stats_row, n_check, "check files")
        _stat_cell(stats_row, n_cross, "matched groups",
                   _M_ERROR if n_cross > 0 else _M_TEXT1)
        _stat_cell(stats_row, n_dups,  "duplicates",
                   _M_ERROR if n_dups > 0 else _M_TEXT1)
        if duration_s:
            _stat_cell(stats_row, _fmt_duration(duration_s), "scan time")

        if n_cross == 0:
            tk.Label(self._custom_results_info_card,
                     text="No cross-folder duplicates found — folders look clean!",
                     font=("Segoe UI", 9), bg=_CARD_BG,
                     fg="#9E9E9E", anchor=tk.W).pack(fill=tk.X, padx=16, pady=(0, 14))

        # Show card, buttons, divider, new-scan button
        self._custom_results_info_card.pack(fill=tk.X, padx=16, pady=(16, 8))
        self._custom_results_btn_row.pack(fill=tk.X, padx=16, pady=6)
        self._custom_results_divider.pack(fill=tk.X, padx=16, pady=14)
        self._custom_results_new_frame.pack(pady=10)

        # Enable appropriate buttons
        _mat_enable(self._cr_inapp_btn)
        _mat_enable(self._cr_browser_btn)
        if dry_run and self._custom_groups:
            _mat_enable(self._cr_accept_btn)

    def _show_custom_results_in_tab(self) -> None:
        """Hide the compare-scan form and show inline results."""
        self._custom_form_outer.pack_forget()
        self._custom_prog_frame.pack_forget()
        self._custom_btn_bar.pack_forget()
        self._custom_inline_result_frame.pack(fill=tk.BOTH, expand=True)

    def _hide_custom_results_in_tab(self) -> None:
        """Restore the compare-scan form by hiding inline results."""
        self._custom_inline_result_frame.pack_forget()
        for w in self._custom_results_viewer_host.winfo_children():
            w.destroy()
        self._custom_results_viewer_host.pack_forget()
        self._custom_results_summary_frame.pack(fill=tk.BOTH, expand=True)
        # Restore form panels in original pack order
        self._custom_form_outer.pack(fill=tk.BOTH, expand=True)
        self._custom_prog_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self._custom_btn_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _new_custom_scan(self) -> None:
        """Reset compare scan state and restore the form."""
        if not messagebox.askyesno(
            "Start New Compare Scan",
            "Clear the current results and start a new compare scan?",
            parent=self.root,
        ):
            return
        self._custom_groups      = []
        self._custom_broken      = []
        self._custom_report_path = None
        _mat_disable(self._cr_accept_btn)
        _mat_disable(self._cr_browser_btn)
        _mat_disable(self._cr_inapp_btn)
        self._custom_phase_label.set("Ready.")
        self._hide_custom_results_in_tab()

    # ── custom scan folder helpers ───────────────────────────��─────────────

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

    def _start_custom_scan(self, resume_state=None) -> None:
        if self._scanning:
            error_handler.show_warning(self.root, "Scan In Progress",
                "A scan is already running.\nPlease wait for it to finish.")
            return

        main  = self._custom_main_var.get().strip()
        check = self._custom_check_var.get().strip()
        out   = self._custom_out_var.get().strip()

        if not main:
            error_handler.show_warning(self.root, "Missing Folder",
                "Please select the Main (reference) folder before starting.")
            return
        if not check:
            error_handler.show_warning(self.root, "Missing Folder",
                "Please select the Check folder before starting.")
            return
        if not out:
            error_handler.show_warning(self.root, "Missing Folder",
                "Please select an Output folder before starting.")
            return

        main_path, check_path, out_path = Path(main), Path(check), Path(out)
        if not main_path.is_dir():
            error_handler.show_error(self.root, "Folder Not Found",
                "The Main folder could not be found.\nCheck that the path is correct and the drive is connected.",
                detail=f"Path: {main}")
            return
        if not check_path.is_dir():
            error_handler.show_error(self.root, "Folder Not Found",
                "The Check folder could not be found.\nCheck that the path is correct and the drive is connected.",
                detail=f"Path: {check}")
            return
        if main_path.resolve() == check_path.resolve():
            error_handler.show_warning(self.root, "Same Folder",
                "The Main and Check folders must be different.\nPlease select two separate folders.")
            return

        self._collect_settings()
        self._scanning = True
        self._custom_stop_flag[0]  = False
        self._custom_pause_flag[0] = False
        self._custom_is_paused     = False
        self._custom_groups        = []
        self._custom_broken        = []
        self._custom_report_path   = None
        self._lock_settings()

        # Swap button frames
        self._custom_idle_frame.pack_forget()
        self._custom_active_frame.pack(fill=tk.X, padx=4)
        # Ensure pause button is in correct state
        self._custom_pause_btn.configure(
            text="⏸  Pause", command=self._pause_custom_scan)
        _mat_enable(self._custom_pause_btn)
        _mat_enable(self._custom_stop_btn)
        _mat_disable(self._custom_accept_btn)
        _mat_disable(self._custom_inapp_btn)
        _mat_disable(self._custom_browser_btn)

        self._custom_phase_label.set("Initialising…")
        self._custom_progress_bar["value"] = 0
        self._custom_progress_bar["mode"]  = "indeterminate"
        self._custom_progress_bar.start(12)
        self._custom_tracker = PhaseTracker(_CUSTOM_PHASES)
        self._custom_tracker.start_phase(_CUSTOM_PHASES[0], 1)

        _set_sleep_prevention(True)
        self._custom_scan_start_time = time.perf_counter()
        _use_lib_main  = self.main_mode_var.get() == "library"
        _trust_main    = self.trust_lib_main_var.get() if _use_lib_main else False
        _use_lib_check = self.check_mode_var.get() == "library"
        _trust_check   = self.trust_lib_check_var.get() if _use_lib_check else False
        threading.Thread(
            target=self._custom_worker,
            args=(main_path, check_path, out_path, self.settings),
            kwargs={
                "use_lib_main":  _use_lib_main,  "trust_main":  _trust_main,
                "use_lib_check": _use_lib_check, "trust_check": _trust_check,
                "resume_state":  resume_state,
            },
            daemon=True,
        ).start()

    def _pause_custom_scan(self) -> None:
        self._custom_pause_flag[0] = True
        _mat_disable(self._custom_pause_btn)
        self._custom_phase_label.set("Pausing…")

    def _resume_custom_in_place(self) -> None:
        """Resume a paused Compare Scan without going back to the idle state."""
        self._custom_is_paused = False
        self._custom_pause_btn.configure(
            text="⏸  Pause", command=self._pause_custom_scan)
        _mat_enable(self._custom_stop_btn)
        self._start_custom_scan(resume_state=self._custom_paused_state)

    def _stop_custom_scan(self) -> None:
        if self._custom_is_paused:
            # Scan already paused; stopping discards the paused state
            if not messagebox.askyesno("Discard Paused Scan",
                                       "Discard the paused compare scan?",
                                       parent=self.root):
                return
            self._custom_is_paused = False
            self._custom_paused_state = None
            self._unlock_settings()
            self._custom_active_frame.pack_forget()
            self._custom_idle_frame.pack(fill=tk.X, padx=4)
            self._custom_phase_label.set("Paused scan discarded.")
            self._custom_pause_btn.configure(
                text="⏸  Pause", command=self._pause_custom_scan)
            # Delete on-disk state
            out = self._custom_out_var.get().strip()
            if out:
                from scan_state import delete_custom_state
                delete_custom_state(Path(out))
            return
        if not messagebox.askyesno("Stop Scan", "Stop the custom scan?", parent=self.root):
            return
        self._custom_stop_flag[0] = True
        _mat_disable(self._custom_stop_btn)
        _mat_disable(self._custom_pause_btn)
        self._custom_phase_label.set("Stopping…")

    # ── custom worker ─────────────────────────────────────────────────────

    def _custom_worker(
        self, main_path: Path, check_path: Path, out_path: Path, settings: Settings,
        use_lib_main: bool = False, trust_main: bool = False,
        use_lib_check: bool = False, trust_check: bool = False,
        resume_state=None,
    ) -> None:
        _PHASES = ["Main folder", "Check folder", "Comparing", "Report"]

        def cb(msg, done, total, phase):
            self._custom_progress_cb(msg, done, total, phase)

        def _load_lib_cache(folder: Path, use: bool):
            """Load cached hashes for *folder* from the library."""
            try:
                from library import Library, get_library_dir
                _lib = Library.load(get_library_dir())
                return _lib.load_cache_merged(str(folder.resolve())), _lib
            except Exception:
                return None, None

        def _inject_records_into_cache(records_list, lib_cache):
            """Inject already-hashed records into the in-memory cache."""
            if lib_cache is None:
                lib_cache = {}
            try:
                from library import FileRecord as _FR
                for _r in records_list:
                    try:
                        lib_cache[str(_r.path.resolve())] = _FR.from_image_record(_r)
                    except Exception:
                        pass
            except ImportError:
                pass
            return lib_cache

        def _writeback_to_library(folder: Path, records: list) -> None:
            """Write scan results back to the library (best-effort)."""
            if not records:
                return
            try:
                from library import (Library, get_library_dir, FileRecord,
                                     FolderEntry, get_drive_info,
                                     compute_folder_fingerprint)
                from datetime import datetime as _dt
                _lib_wb = Library.load(get_library_dir())
                _wb_cache: dict = {}
                for _r in records:
                    try:
                        _st = _r.path.stat()
                        _wb_cache[str(_r.path)] = FileRecord.from_image_record(
                            _r, st_mtime=_st.st_mtime)
                    except Exception:
                        _wb_cache[str(_r.path)] = FileRecord.from_image_record(_r)
                _folder_str = str(folder.resolve())
                _lib_wb.save_cache(_folder_str, _wb_cache)
                _di = get_drive_info(folder)
                _lib_wb.set_folder(FolderEntry(
                    path               = _folder_str,
                    drive_type         = _di.drive_type,
                    volume_serial      = _di.volume_serial,
                    folder_fingerprint = compute_folder_fingerprint(folder),
                    last_updated       = _dt.now().isoformat(),
                    file_count         = len(_wb_cache),
                ))
            except Exception:
                pass   # library write-back is best-effort

        def _save_custom_pause(phase, main_records, check_records,
                               compare_i=0, union_parent=None):
            """Persist the custom scan state so it can be resumed."""
            from scan_state import (CustomScanState, save_custom_state,
                                    custom_state_path, serialize_record)
            from dataclasses import asdict
            st = CustomScanState(
                main_folder=str(main_path),
                check_folder=str(check_path),
                output_folder=str(out_path),
                settings_snapshot=asdict(settings),
                phase=phase,
                main_records=[serialize_record(r) for r in main_records],
                check_records=[serialize_record(r) for r in check_records],
                compare_i=compare_i,
                union_parent=list(union_parent) if union_parent else [],
            )
            save_custom_state(st, custom_state_path(out_path))
            self._custom_paused_state = st

        try:
            out_path.mkdir(parents=True, exist_ok=True)
            skip_paths = {
                (out_path / "results").resolve(),
                (out_path / "trash").resolve(),
                out_path.resolve(),
            }

            main_records = []
            check_records = []
            main_failed: list = []
            check_failed: list = []
            _compare_resume = None     # ScanState for resuming mid-compare

            # ── Determine starting point from resume_state ────────────────
            _skip_main  = False
            _skip_check = False
            if resume_state is not None:
                from scan_state import deserialize_record
                rp = resume_state.phase
                if rp in ("main_done", "check_hashing", "check_done", "comparing"):
                    main_records = [deserialize_record(r) for r in resume_state.main_records]
                    _skip_main = True
                    cb(f"Restored {len(main_records)} main records.", 0, 0, "Main folder")
                if rp in ("check_done", "comparing"):
                    check_records = [deserialize_record(r) for r in resume_state.check_records]
                    _skip_check = True
                    cb(f"Restored {len(check_records)} check records.", 0, 0, "Check folder")
                if rp == "comparing" and resume_state.compare_i > 0:
                    # Build a ScanState for find_groups resume
                    from scan_state import ScanState
                    _compare_resume = ScanState(
                        phase="comparing",
                        compare_i=resume_state.compare_i,
                        union_parent=resume_state.union_parent,
                    )
                if rp == "main_hashing":
                    # Partially hashed main folder — inject into cache
                    _partial_main = [deserialize_record(r) for r in resume_state.main_records]
                    cb(f"Resuming main folder — {len(_partial_main)} already hashed.", 0, 0, "Main folder")
                    _main_cache, _ = _load_lib_cache(main_path, use_lib_main)
                    _main_cache = _inject_records_into_cache(_partial_main, _main_cache)
                    main_records = collect_images(
                        main_path, skip_paths, settings,
                        progress_cb=cb,
                        stop_flag=self._custom_stop_flag,
                        pause_flag=self._custom_pause_flag,
                        failed_paths=main_failed,
                        library_cache=_main_cache,
                        trust_library=True,
                    )
                    _writeback_to_library(main_path, main_records)
                    _skip_main = True   # already done
                if rp == "check_hashing":
                    # Partially hashed check folder — inject into cache
                    _partial_check = [deserialize_record(r) for r in resume_state.check_records]
                    cb(f"Resuming check folder — {len(_partial_check)} already hashed.", 0, 0, "Check folder")
                    _check_cache, _ = _load_lib_cache(check_path, use_lib_check)
                    _check_cache = _inject_records_into_cache(_partial_check, _check_cache)
                    check_records = collect_images(
                        check_path, skip_paths, settings,
                        progress_cb=cb,
                        stop_flag=self._custom_stop_flag,
                        pause_flag=self._custom_pause_flag,
                        failed_paths=check_failed,
                        library_cache=_check_cache,
                        trust_library=True,
                    )
                    _writeback_to_library(check_path, check_records)
                    _skip_check = True

            # ── Phase 1 — hash main folder ────────────────────────────────
            if not _skip_main:
                cb("Scanning main folder…", 0, 1, "Main folder")
                _main_cache, _ = _load_lib_cache(main_path, use_lib_main)
                main_records = collect_images(
                    main_path, skip_paths, settings,
                    progress_cb=cb,
                    stop_flag=self._custom_stop_flag,
                    pause_flag=self._custom_pause_flag,
                    failed_paths=main_failed,
                    library_cache=_main_cache,
                    trust_library=trust_main and use_lib_main,
                )
                _writeback_to_library(main_path, main_records)

            if self._custom_stop_flag[0]:
                self.root.after(0, lambda: self._on_custom_done("Stopped.", success=False))
                return
            if self._custom_pause_flag[0]:
                _save_custom_pause("main_hashing", main_records, [])
                self.root.after(0, lambda: self._on_custom_done(
                    "Compare Scan paused (main folder).", success=False, paused=True))
                return

            # ── Phase 2 — hash check folder ───────────────────────────────
            if not _skip_check:
                cb("Scanning check folder…", 0, 1, "Check folder")
                _check_cache, _ = _load_lib_cache(check_path, use_lib_check)
                check_records = collect_images(
                    check_path, skip_paths, settings,
                    progress_cb=cb,
                    stop_flag=self._custom_stop_flag,
                    pause_flag=self._custom_pause_flag,
                    failed_paths=check_failed,
                    library_cache=_check_cache,
                    trust_library=trust_check and use_lib_check,
                )
                _writeback_to_library(check_path, check_records)

            if self._custom_stop_flag[0]:
                self.root.after(0, lambda: self._on_custom_done("Stopped.", success=False))
                return
            if self._custom_pause_flag[0]:
                _save_custom_pause("check_hashing", main_records, check_records)
                self.root.after(0, lambda: self._on_custom_done(
                    "Compare Scan paused (check folder).", success=False, paused=True))
                return

            all_records = main_records + check_records
            self._custom_broken = main_failed + check_failed

            # ── Phase 3 — find groups across both folders ─────────────────
            cb("Comparing images…", 0, 1, "Comparing")
            all_groups, partial_state = find_groups(
                all_records, settings,
                progress_cb=cb,
                stop_flag=self._custom_stop_flag,
                pause_flag=self._custom_pause_flag,
                resume_state=_compare_resume,
            )

            if self._custom_stop_flag[0]:
                self.root.after(0, lambda: self._on_custom_done("Stopped.", success=False))
                return
            if self._custom_pause_flag[0] and partial_state is not None:
                _save_custom_pause(
                    "comparing", main_records, check_records,
                    compare_i=partial_state.compare_i,
                    union_parent=partial_state.union_parent,
                )
                self.root.after(0, lambda: self._on_custom_done(
                    "Compare Scan paused (comparing).", success=False, paused=True))
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

            # Clean up any paused state file on successful completion
            from scan_state import delete_custom_state
            delete_custom_state(out_path)

            dry = settings.dry_run
            msg = (
                f"Done. {len(main_records)} main + {len(check_records)} check images scanned.  "
                f"{len(cross_groups)} cross-folder matches ({n_cross} dups from check folder)"
                + (f",  {len(within_check_groups)} within-check groups ({n_inner} dups)" if n_inner else "")
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
        def _update() -> None:
            tracker = self._custom_tracker
            if tracker is None:
                return
            self._check_sleep_gap(tracker)

            # Map scanner phase names to custom phases.
            # collect_images reports "Hashing"; find_groups reports "Comparing".
            # The worker sets the high-level phase via cb() before each call.
            mapped = phase_name
            if phase_name == "Hashing":
                # Keep the current custom phase ("Main folder" or "Check folder")
                mapped = tracker.current_phase_name or _CUSTOM_PHASES[0]
            elif phase_name not in _CUSTOM_PHASES:
                mapped = tracker.current_phase_name or _CUSTOM_PHASES[0]

            if tracker.current_phase_name != mapped:
                tracker.finish_phase()
                tracker.start_phase(mapped, max(total, 1))

            if total > 0:
                tracker.update(done)

            pct = tracker.total_pct
            eta = tracker.format_eta()
            phase_num = _CUSTOM_PHASES.index(mapped) + 1 if mapped in _CUSTOM_PHASES else "?"

            self._custom_progress_bar.stop()
            self._custom_progress_bar["mode"]  = "determinate"
            self._custom_progress_bar["value"] = pct

            self._custom_phase_label.set(
                f"Phase {phase_num}/{len(_CUSTOM_PHASES)}: {mapped}…"
            )
            self._custom_eta_var.set(
                f"{pct:.0f}%  \u00b7  {eta} remaining  \u00b7  {msg[:80]}"
            )
            self._update_custom_detail_log()

        self.root.after(0, _update)

    def _update_custom_detail_log(self) -> None:
        if not self._custom_details_var.get() or self._custom_tracker is None:
            return
        summaries = self._custom_tracker.phase_summaries
        lines = []
        for s in summaries:
            if s["status"] == "done":
                icon = "\u2713"
                info = f"{s['total_units']} units  {s['duration_s']:.1f}s"
            elif s["status"] == "active":
                icon = "\u2192"
                info = f"{s['done_units']}/{s['total_units']}  ongoing"
            else:
                icon = "\u25cb"
                info = "waiting"
            lines.append(f"{icon} {s['name']:<14} {info}")
        text = "\n".join(lines)
        self._custom_detail_text.config(state=tk.NORMAL)
        self._custom_detail_text.delete("1.0", tk.END)
        self._custom_detail_text.insert("1.0", text)
        self._custom_detail_text.config(state=tk.DISABLED)

    def _toggle_custom_details(self) -> None:
        if self._custom_details_var.get():
            self._custom_detail_text.pack(fill=tk.X, pady=(4, 0))
        else:
            self._custom_detail_text.pack_forget()
        self._save_settings_now()

    def _on_custom_done(
        self, msg: str, success: bool = True,
        dry_run: bool = True, paused: bool = False,
        n_main: int = 0, n_check: int = 0,
        n_cross: int = 0, n_dups: int = 0, src_folder: str = "",
    ) -> None:
        _set_sleep_prevention(False)
        self._custom_progress_bar.stop()
        self._custom_progress_bar["mode"]  = "determinate"
        self._custom_progress_bar["value"] = 100 if (success and not paused) else self._custom_progress_bar["value"]
        self._scanning = False

        if paused:
            # Keep the active frame visible; flip Pause → Resume
            self._custom_is_paused = True
            self._custom_pause_btn.configure(
                text="▶  Resume", command=self._resume_custom_in_place,
            )
            _mat_enable(self._custom_pause_btn)
            _mat_enable(self._custom_stop_btn)   # Stop = Discard while paused
        else:
            self._unlock_settings()
            # Restore idle frame
            self._custom_active_frame.pack_forget()
            self._custom_idle_frame.pack(fill=tk.X, padx=4)
            # Reset pause button for next scan
            self._custom_pause_btn.configure(
                text="⏸  Pause", command=self._pause_custom_scan)

        self._custom_phase_label.set(msg)
        self._custom_eta_var.set("")

        if success:
            # Refresh the Library tab so newly-cached hashes appear there.
            if hasattr(self, "_library_ctrl"):
                self._library_ctrl.reload()

        if success and self._custom_groups:
            _elapsed_s = time.perf_counter() - getattr(self, "_custom_scan_start_time",
                                                        time.perf_counter())
            # Log to scan history (with note that it's a custom scan)
            dup_pct = n_dups / max(n_main + n_check, 1) * 100
            entry = {
                "date":        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "duration_s":  round(_elapsed_s, 1),
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

            # Show inline results inside the Compare Scan tab
            self._update_custom_results_ui(n_main, n_check, n_cross, n_dups, dry_run,
                                           src_folder, duration_s=_elapsed_s)
            self._show_custom_results_in_tab()
            self._nb.select(self._tab_custom)

    def _on_custom_error(self, msg: str, tb: str = "") -> None:
        _set_sleep_prevention(False)
        self._custom_progress_bar.stop()
        self._custom_phase_label.set("Scan failed — see error message.")
        self._scanning = False
        self._unlock_settings()
        self._custom_active_frame.pack_forget()
        self._custom_idle_frame.pack(fill=tk.X, padx=4)
        self._custom_pause_btn.configure(
            text="⏸  Pause", command=self._pause_custom_scan)
        user_msg, detail = error_handler.format_scan_error(Exception(msg), tb)
        error_handler.show_error(self.root, "Scan Failed", user_msg, detail=detail)

    # ── custom scan post-scan actions ──────────────────────────────────────

    def _custom_open_inapp_report(self) -> None:
        if not self._custom_groups:
            messagebox.showinfo("Review", "No custom scan results to review.", parent=self.root)
            return
        out = self._custom_out_var.get().strip()

        def _apply_cb(_paths_trashed: list) -> None:
            # File moving is handled inside ReportViewer; update history here.
            if self._scan_history:
                for e in reversed(self._scan_history):
                    if "[Custom]" in e.get("src_folder", ""):
                        e["applied"] = True
                        break
                self._save_scan_history()
                self._refresh_history_view()

        # Ensure inline results frame is visible in the Compare Scan tab
        if not self._custom_inline_result_frame.winfo_ismapped():
            self._show_custom_results_in_tab()
        self._nb.select(self._tab_custom)

        # Embed viewer inside the compare-scan inline results area
        for w in self._custom_results_viewer_host.winfo_children():
            w.destroy()
        self._custom_results_summary_frame.pack_forget()
        self._custom_results_viewer_host.pack(fill=tk.BOTH, expand=True)

        log_path = ops_log_path(Path(out)) if out else None

        def _on_close():
            self._custom_results_viewer_host.pack_forget()
            for w in self._custom_results_viewer_host.winfo_children():
                w.destroy()
            self._custom_results_summary_frame.pack(fill=tk.BOTH, expand=True)

        viewer = ReportViewer(
            self._custom_results_viewer_host,
            self._custom_groups,
            ops_log_path=log_path,
            on_apply_cb=_apply_cb,
            solo_originals=[],
            broken_files=self._custom_broken,
            settings=self.settings,
            on_close_cb=_on_close,
        )
        viewer.pack(fill=tk.BOTH, expand=True)

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
        _mat_disable(self._cr_accept_btn)
        self._custom_phase_label.set("Moving files…")

        def _do() -> None:
            try:
                moved_prev, err_count = move_groups(
                    self._custom_groups, Path(out),
                    dry_run=False, settings=self.settings,
                )
                report = generate_report(
                    self._custom_groups, Path(out),
                    Path(self._custom_main_var.get()),
                    0, self.settings,
                )
                self._custom_report_path = report
                err_note = f"  ({err_count} errors)" if err_count else ""
                msg = f"Moved {moved_prev} duplicates from check folder to trash{err_note}."
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
                import traceback as _tb; _detail = _tb.format_exc()
                self.root.after(0, lambda e=exc, d=_detail: error_handler.show_error(
                    self.root, "Move Failed",
                    "Could not move the files.\nCheck folder permissions and available disk space.",
                    detail=d))

        threading.Thread(target=_do, daemon=True).start()

    # ── mode management ───────────────────────────────────────────────────

    def _apply_mode(self) -> None:
        if self._mode_var.get() == "advanced":
            self._quick_speed_frame.pack_forget()
            self._compact_adv_frame.pack(fill=tk.X, pady=(0, 4))
        else:
            self._compact_adv_frame.pack_forget()
            self._quick_speed_frame.pack(fill=tk.X, pady=(0, 6))

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

    def _toggle_custom_show_all(self) -> None:
        self._custom_all_settings_visible = not self._custom_all_settings_visible
        if self._custom_all_settings_visible:
            self._custom_show_all_btn.pack_forget()
            for f in self._custom_advanced_frames:
                f.pack(fill=tk.X, pady=(0, 6))
            self._custom_show_all_btn.pack(anchor=tk.W, padx=2, pady=(0, 4))
            self._custom_show_all_btn.configure(text="▲  Hide Advanced Settings")
        else:
            for f in self._custom_advanced_frames:
                f.pack_forget()
            self._custom_show_all_btn.configure(text="▼  Show Advanced Settings")

    def _on_mode_change(self) -> None:
        self.settings.mode = self._mode_var.get()
        self._apply_mode()
        self._schedule_settings_save()

    # ── folder row helper ─────────────────────────────────────────────────

    def _lib_folder_row(
        self,
        parent: tk.Widget,
        label: str,
        setting_key: str,
        mode_var: tk.StringVar,
        trust_var: tk.BooleanVar,
        *,
        browse_cmd=None,
        change_cb=None,
        label_width: int = 16,
        path_var: "tk.StringVar | None" = None,
    ) -> tk.StringVar:
        """Folder picker row with Browse / Library mode toggle.

        In Browse mode: a text Entry + Browse button (identical to _folder_row).
        In Library mode: a Combobox listing all tracked Library folders, a drive
        status badge, and a "Skip file change check" checkbox.

        Always returns (or accepts) the StringVar that holds the active path — so
        existing code that reads the var is unchanged regardless of mode.
        """
        if path_var is None:
            path_var = tk.StringVar(value=getattr(self.settings, setting_key, ""))

        # ── Mode toggle row ───────────────────────────────────────────────
        hdr = ttk.Frame(parent)
        hdr.pack(fill=tk.X, pady=(3, 0))
        ttk.Label(hdr, text=label, width=label_width, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Radiobutton(hdr, text="Browse",  variable=mode_var, value="browse",
                        command=lambda: _on_mode()).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Radiobutton(hdr, text="Library", variable=mode_var, value="library",
                        command=lambda: _on_mode()).pack(side=tk.LEFT)

        # Container keeps the correct pack position in parent regardless of
        # which child rows are visible.  Without this, pack_forget + re-pack
        # would append lib_row/trust_row after the Output folder row.
        container = ttk.Frame(parent)
        container.pack(fill=tk.X)

        # ── Browse row ────────────────────────────────────────────────────
        browse_row = ttk.Frame(container)
        ttk.Label(browse_row, width=label_width).pack(side=tk.LEFT)
        ttk.Entry(browse_row, textvariable=path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        if browse_cmd is None:
            browse_cmd = lambda v=path_var: self._browse(v, setting_key)  # noqa: E731
        ttk.Button(browse_row, text="Browse…",
                   command=lambda: browse_cmd(path_var)).pack(side=tk.RIGHT)

        # ── Library row ───────────────────────────────────────────────────
        lib_row = ttk.Frame(container)
        ttk.Label(lib_row, width=label_width).pack(side=tk.LEFT)
        lib_path_entry = ttk.Entry(lib_row, textvariable=path_var, state="readonly")
        lib_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        badge_lbl = ttk.Label(lib_row, text="", width=10, anchor=tk.W)
        badge_lbl.pack(side=tk.LEFT, padx=(0, 4))

        def _open_lib_picker() -> None:
            from library_tab import LibFolderPickerDialog
            dlg = LibFolderPickerDialog(self.root)
            if dlg.result:
                path_var.set(dlg.result)
                _update_badge(dlg.result)

        ttk.Button(lib_row, text="Pick…", command=_open_lib_picker).pack(side=tk.RIGHT)

        # ── Trust row (shown only in Library mode) ────────────────────────
        trust_row = ttk.Frame(container)
        ttk.Label(trust_row, width=label_width).pack(side=tk.LEFT)
        ttk.Checkbutton(
            trust_row,
            text="Skip file change check  (trust cached hashes — fastest)",
            variable=trust_var,
        ).pack(side=tk.LEFT)

        # ── Helpers ───────────────────────────────────────────────────────

        def _update_badge(selected: str) -> None:
            if not selected:
                badge_lbl.configure(text="", foreground=_M_TEXT2)
            elif Path(selected).exists():
                badge_lbl.configure(text="✓  OK", foreground=_M_SUCCESS)
            else:
                badge_lbl.configure(text="✗  Missing", foreground=_M_ERROR)

        def _on_mode() -> None:
            # Hide all children first, then show only what's needed.
            # Operating within container keeps pack position stable in parent.
            for child in (browse_row, lib_row, trust_row):
                child.pack_forget()
            if mode_var.get() == "library":
                _update_badge(path_var.get())
                lib_row.pack(fill=tk.X, pady=2)
                trust_row.pack(fill=tk.X, pady=(0, 4))
            else:
                trust_var.set(False)
                browse_row.pack(fill=tk.X, pady=2)

        _on_mode()   # apply initial state

        if change_cb:
            path_var.trace_add("write", lambda *_: change_cb())
        return path_var

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

        # ── Canvas slider: green recommended band drawn behind track & thumb ──
        CANVAS_H = 26
        PAD = 10
        try:
            _cbg = parent.cget("bg")
        except Exception:
            _cbg = _BG

        scale_c = tk.Canvas(ctrl, height=CANVAS_H, highlightthickness=0,
                            bg=_cbg, cursor="hand2")
        scale_c.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        def _eff_rec() -> tuple:
            """Return (rec_lo, rec_hi), updated from calibration when available."""
            if key == "threshold":
                cv = getattr(self.settings, "calibrated_threshold", 0)
                if cv > 0:
                    return (max(float(min_v), cv - 1.0), min(float(max_v), cv + 1.0))
            elif key == "preview_ratio":
                cv = getattr(self.settings, "calibrated_preview_ratio", 0.0)
                if cv > 0:
                    return (max(float(min_v), cv - 0.02), min(float(max_v), cv + 0.02))
            return (rec_lo, rec_hi)

        def _px(v: float, w: int) -> float:
            return PAD + (v - min_v) / max(float(max_v - min_v), 1e-9) * max(w - 2 * PAD, 1)

        def _draw(*_):
            scale_c.delete("all")
            w = scale_c.winfo_width()
            if w < 20:
                return
            cy = CANVAS_H // 2

            rl, rh = _eff_rec()

            # 1 — green recommended band (drawn first → visually behind everything)
            scale_c.create_rectangle(
                _px(rl, w), 1, _px(rh, w), CANVAS_H - 1,
                fill="#c8e6c9", outline="",
            )
            # 2 — track line
            scale_c.create_line(
                PAD, cy, w - PAD, cy,
                fill="#bdbdbd", width=2, capstyle=tk.ROUND,
            )
            # 3 — thumb knob (drawn last → on top)
            tx = _px(var.get(), w)
            r = 7
            scale_c.create_oval(
                tx - r, cy - r, tx + r, cy + r,
                fill="#1565C0", outline="#FFFFFF", width=2,
            )

        def _on_input(event):
            try:
                t_w = max(scale_c.winfo_width() - 2 * PAD, 1)
                frac = max(0.0, min(1.0, (event.x - PAD) / t_w))
                raw  = min_v + frac * (max_v - min_v)
                snap = round(round(raw / step) * step, 10)
                var.set(max(min_v, min(max_v, snap)))
            except Exception:
                pass

        def _snap(*_):
            try:
                v = var.get()
                r = round(round(v / step) * step, 10)
                if abs(r - v) > step * 0.001:
                    var.set(r)
            except Exception:
                pass

        scale_c.bind("<Button-1>",       _on_input)
        scale_c.bind("<B1-Motion>",      _on_input)
        scale_c.bind("<ButtonRelease-1>", _snap)
        scale_c.bind("<Configure>",       _draw)
        var.trace_add("write", _draw)
        scale_c.after(50, _draw)

        # Save draw-fn reference for calibration-triggered redraws
        if key == "threshold":
            self._thresh_slider_draw = _draw
        elif key == "preview_ratio":
            self._ratio_slider_draw  = _draw
        # ─────────────────────────────────────────────────────────────────────

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
        self._schedule_estimate_update(delay_ms=2000)

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

    def _calib_info_text(self) -> str:
        t = self.settings.calibrated_threshold
        r = self.settings.calibrated_preview_ratio
        if t > 0:
            return f"Last calibration: threshold = {t}  ·  ratio = {r:.2f}"
        return "No calibration run yet."

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
        _dth, _dtf = _dark_strength_to_params(self._safe_float(self.dark_strength_var, 5.0))
        s.dark_threshold           = _dth
        s.dark_tighten_factor      = _dtf
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
        s.collect_metadata         = self.meta_csv_var.get()
        s.export_csv               = self.meta_csv_var.get()
        s.extended_report          = self.ext_report_var.get()
        _ds = self.date_sort_var.get()
        s.sort_by_filename_date    = (_ds == "filename")
        s.sort_by_exif_date        = (_ds == "exif")
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
        s.auto_update              = self.auto_update_var.get()
        s.developer_mode           = self.developer_mode_var.get()
        # Threads: always from the Settings dropdown
        try:
            s.scan_threads = max(1, int(self.scan_threads_var.get()))
        except (ValueError, tk.TclError):
            import os as _os
            s.scan_threads = max(1, _os.cpu_count() or 1)
        if s.mode == "quick":
            # Quick mode: quality guards driven by the quality slider
            speed = int(self.quick_scan_speed_var.get())
            s.scan_speed = speed
            _udh, _uhi, _udp = _quality_to_params(speed)
            s.use_dual_hash   = _udh
            s.use_histogram   = _uhi
            s.dark_protection = _udp
        else:
            # Advanced mode: guards come from individual checkboxes in Settings
            s.use_dual_hash   = self.dual_hash_var.get()
            s.use_histogram   = self.hist_var.get()
            s.dark_protection = self.dark_var.get()

    def _build_library_tab(self) -> None:
        """Populate the Library tab using the library_tab module."""
        build_library_tab(self._tab_library, self)

    def _build_about_tab(self) -> None:
        """Populate the About tab using the about_tab module."""
        build_about_tab(self._tab_about, self)

    def _reset_defaults(self) -> None:
        d = DEFAULTS
        self.thresh_var.set(d.threshold)
        self.ratio_var.set(d.preview_ratio)
        self.series_tol_var.set(d.series_tolerance_pct)
        self.series_thresh_var.set(d.series_threshold_factor)
        self.ar_tol_var.set(d.ar_tolerance_pct)
        self.dark_var.set(d.dark_protection)
        self.dark_strength_var.set(_dark_params_to_strength(d.dark_threshold, d.dark_tighten_factor))
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
        self.meta_csv_var.set(d.collect_metadata and d.export_csv)
        self.ext_report_var.set(d.extended_report)
        _ds_default = "exif" if d.sort_by_exif_date else ("filename" if d.sort_by_filename_date else "mtime")
        self.date_sort_var.set(_ds_default)
        self.mindim_var.set(d.min_dimension)
        self.recursive_var.set(d.recursive)
        self.skip_names_var.set(d.skip_names)
        self.dry_var.set(d.dry_run)
        import os as _os
        self.scan_threads_var.set(str(max(1, _os.cpu_count() or 1)))
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
            if hasattr(self, "_custom_last_calib_btn"):
                _mat_enable(self._custom_last_calib_btn)
            self._calib_info_var.set(self._calib_info_text())
            try:
                fg = _M_SUCCESS if self.settings.calibrated_threshold > 0 else "#999"
                self._calib_info_lbl.configure(foreground=fg)
            except Exception:
                pass
            # Redraw threshold and ratio slider canvases so their green
            # recommended bands immediately reflect the new calibrated values.
            for attr in ("_thresh_slider_draw", "_ratio_slider_draw"):
                fn = getattr(self, attr, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
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

    def _schedule_estimate_update(self, delay_ms: int = 600) -> None:
        if getattr(self, "_estimate_after_id", None) is not None:
            try:
                self.root.after_cancel(self._estimate_after_id)
            except Exception:
                pass
        self._estimate_after_id = self.root.after(delay_ms, self._update_estimate)

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

                # Derive active thread count and quality params
                try:
                    _thr = max(1, int(self.scan_threads_var.get()))
                except Exception:
                    _thr = 1
                try:
                    mode = self._mode_var.get()
                    if mode == "quick":
                        _udh, _uhi, _udp = _quality_to_params(int(self.quick_scan_speed_var.get()))
                    else:
                        _udh = self.dual_hash_var.get()
                        _uhi = self.hist_var.get()
                        _udp = self.dark_var.get()
                except Exception:
                    _udh, _uhi, _udp = True, True, True

                # Base hash time: ~0.06s/image per-thread at full speed
                # (measured: 848 images in ~50s sequential = 0.059s each)
                per_image_s = 0.059
                if not _udp:
                    per_image_s *= 0.65   # skip brightness computation
                if not _udh:
                    per_image_s *= 0.97   # dHash is cheap, minor saving
                hash_time = count * per_image_s / max(1, _thr)

                compare_time = count * (count - 1) / 2 * 0.0000005
                extras       = 0.0
                try:
                    if self.meta_csv_var.get():
                        extras += hash_time * 0.15
                    if self.rawpy_var.get():
                        extras += hash_time * 0.30
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

    # ── Compare Scan resume / discard ─────────────────────────────────────

    def _check_custom_resume_state(self) -> None:
        """Show resume notice on the Compare Scan tab if a paused state exists."""
        out = self._custom_out_var.get().strip()
        if not out:
            return
        from scan_state import custom_state_path, load_custom_state
        sp = custom_state_path(Path(out))
        st = load_custom_state(sp)
        if st is None:
            return
        ts = ""
        try:
            ts = datetime.datetime.fromtimestamp(
                sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        self._custom_paused_state = st
        phase_label = st.phase.replace("_", " ").title() if st.phase else "unknown"
        self._custom_resume_var.set(
            f"Paused compare scan found from {ts}  ·  Phase: {phase_label}")
        self._custom_resume_lbl.pack(side=tk.LEFT)
        self._custom_resume_btn.pack(side=tk.LEFT, padx=4)
        self._custom_discard_btn.pack(side=tk.LEFT, padx=4)
        # Pre-fill folder fields from the saved state
        if st.main_folder:
            self._custom_main_var.set(st.main_folder)
        if st.check_folder:
            self._custom_check_var.set(st.check_folder)

    def _resume_custom_scan(self) -> None:
        """Resume a saved Compare Scan from the resume notice."""
        self._custom_resume_lbl.pack_forget()
        self._custom_resume_btn.pack_forget()
        self._custom_discard_btn.pack_forget()
        self._start_custom_scan(resume_state=self._custom_paused_state)

    def _discard_custom_resume(self) -> None:
        self._custom_resume_lbl.pack_forget()
        self._custom_resume_btn.pack_forget()
        self._custom_discard_btn.pack_forget()
        self._custom_paused_state = None
        out = self._custom_out_var.get().strip()
        if out:
            from scan_state import delete_custom_state
            delete_custom_state(Path(out))

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

        n_groups    = len(self.scan_groups)
        n_prev      = sum(len(g.previews) for g in self.scan_groups)
        n_ambiguous = sum(1 for g in self.scan_groups if getattr(g, "is_ambiguous", False))
        n_solo      = len(self._solo_originals)
        space_saved = sum(r.file_size for g in self.scan_groups for r in g.previews)

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
            "n_solo":      n_solo,
            "n_ambiguous": n_ambiguous,
            "space_saved": space_saved,
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
            error_handler.show_warning(self.root, "Scan In Progress",
                "A scan is already running.\nPlease wait for it to finish.")
            return
        self._collect_settings()
        src = self.settings.src_folder.strip()
        out = self.settings.out_folder.strip()
        if not src:
            error_handler.show_warning(self.root, "Missing Folder",
                "Please select a source folder before starting the scan.")
            return
        if not out:
            error_handler.show_warning(self.root, "Missing Folder",
                "Please select an output folder before starting the scan.")
            return
        src_path, out_path = Path(src), Path(out)
        if not src_path.is_dir():
            error_handler.show_error(self.root, "Folder Not Found",
                "The source folder could not be found.\nCheck that the path is correct and the drive is connected.",
                detail=f"Path: {src}")
            return

        self._scanning      = True
        self._stop_flag[0]  = False
        self._pause_flag[0] = False
        self._is_paused     = False
        self._lock_settings()

        # Swap button frames (safe to call even if active frame is already visible)
        self._scan_idle_frame.pack_forget()
        self._scan_active_frame.pack(fill=tk.X, padx=4)
        # Ensure pause button is in correct state
        self.pause_btn.configure(text="⏸  Pause", command=self._pause_scan)
        _mat_enable(self.pause_btn)
        _mat_enable(self.stop_btn)

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

        _set_sleep_prevention(True)
        self._scan_start_time = time.perf_counter()
        _use_lib  = self.src_mode_var.get() == "library"
        _trust    = self.trust_lib_src_var.get() if _use_lib else False
        threading.Thread(
            target=self._worker,
            args=(src_path, out_path, self.settings, resume_state),
            kwargs={"use_library": _use_lib, "trust_library": _trust},
            daemon=True,
        ).start()

    def _pause_scan(self) -> None:
        self._pause_flag[0] = True
        self.pause_btn.configure(state=tk.DISABLED)
        self._phase_label_var.set("Pausing…")

    def _resume_in_place(self) -> None:
        """Resume a paused scan without going back to the idle state."""
        self._is_paused = False
        self.pause_btn.configure(text="⏸  Pause", command=self._pause_scan)
        _mat_enable(self.stop_btn)
        self._start_scan(resume_state=self._paused_state)

    def _stop_scan(self) -> None:
        if self._is_paused:
            # Scan already stopped; stopping just discards the paused state
            if not messagebox.askyesno("Discard Paused Scan",
                                       "Discard the paused scan?", parent=self.root):
                return
            self._is_paused = False
            self._paused_state = None
            self._unlock_settings()
            self._scan_active_frame.pack_forget()
            self._scan_idle_frame.pack(fill=tk.X, padx=4)
            self._phase_label_var.set("Paused scan discarded.")
            # Reset pause button for next scan
            self.pause_btn.configure(text="⏸  Pause", command=self._pause_scan)
            return
        if not messagebox.askyesno("Stop Scan", "Stop the current scan?", parent=self.root):
            return
        self._stop_flag[0] = True
        _mat_disable(self.stop_btn)
        _mat_disable(self.pause_btn)
        self._phase_label_var.set("Stopping…")

    # ── new scan prompt ───────────────────────────────────────────────────

    def _on_focus_in(self, event: tk.Event) -> None:
        """Force a UI refresh when the window regains focus (e.g. after sleep)."""
        if event.widget is self.root:
            self.root.after(150, self.root.update_idletasks)

    def _heartbeat_tick(self) -> None:
        """Periodic heartbeat (every 3 s) to detect sleep/hibernate gaps.

        If a gap > 10 s is detected, all active trackers are notified so
        ETA calculations exclude the sleep duration.
        """
        now = time.monotonic()
        gap = now - self._last_heartbeat
        if gap > 10.0:
            for tracker in (self._tracker, getattr(self, '_custom_tracker', None)):
                if tracker is not None:
                    tracker.notify_gap(gap - 1.0)
        self._last_heartbeat = now
        self.root.after(3000, self._heartbeat_tick)

    def bring_to_front(self) -> None:
        """Raise this window to the front and give it focus.

        Called by SingleInstance when a second launch is detected.
        """
        self.root.deiconify()   # restore if minimised
        self.root.lift()
        self.root.focus_force()

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

    # ── progress callback ─────────────────────────────────────────────────

    def _check_sleep_gap(self, tracker) -> None:
        """Detect time gaps > 10 s (system sleep/hibernate) and compensate."""
        now = time.monotonic()
        gap = now - self._last_heartbeat
        if gap > 10.0 and tracker is not None:
            tracker.notify_gap(gap - 1.0)   # keep 1 s margin
        self._last_heartbeat = now

    def _progress_cb(self, msg: str, done: int, total: int, phase_name: str) -> None:
        def _update() -> None:
            if self._tracker is None:
                return
            self._check_sleep_gap(self._tracker)
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

    def _worker(
        self, src: Path, out: Path, settings: Settings, resume_state=None,
        use_library: bool = False, trust_library: bool = False,
    ) -> None:
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
            elif resume_state and resume_state.phase == "hashing" and resume_state.records:
                # ── Resume hashing: inject saved records into cache ────────
                from scan_state import deserialize_record
                _already = [deserialize_record(r) for r in resume_state.records]
                cb(f"Resuming — {len(_already)} files already hashed.", 0, 0, "Hashing")
                failed: list = []

                # Build a merged cache: library + already-hashed records
                _lib_cache = None
                try:
                    from library import Library, get_library_dir
                    _lib = Library.load(get_library_dir())
                    _lib_cache = _lib.load_cache_merged(str(src.resolve()))
                except Exception:
                    _lib_cache = None
                if _lib_cache is None:
                    _lib_cache = {}
                # Add saved records into the in-memory cache so they won't
                # be re-hashed.  Trust them — they were just computed.
                try:
                    from library import FileRecord as _FR
                    for _r in _already:
                        try:
                            _lib_cache[str(_r.path.resolve())] = _FR.from_image_record(_r)
                        except Exception:
                            pass
                except ImportError:
                    pass

                records = collect_images(
                    src, skip_paths, settings,
                    progress_cb=cb,
                    stop_flag=self._stop_flag,
                    pause_flag=self._pause_flag,
                    failed_paths=failed,
                    library_cache=_lib_cache,
                    trust_library=True,      # trust injected records
                )
            else:
                cb("Discovering images…", 0, 1, "Discovery")
                failed: list = []

                # ── Library cache injection ───────────────────────────────
                # Always load cached hashes for this folder if available.
                # Staleness is verified per-file (mtime + size) unless the
                # user explicitly enabled "Trust library" in Library mode.
                _lib_cache = None
                try:
                    from library import Library, get_library_dir
                    _lib = Library.load(get_library_dir())
                    _lib_cache = _lib.load_cache_merged(str(src.resolve()))
                except Exception:
                    _lib_cache = None
                # trust_library (skip staleness check) only applies when the
                # user explicitly chose Library mode *and* enabled that option.
                _effective_trust = trust_library and use_library

                records = collect_images(
                    src, skip_paths, settings,
                    progress_cb=cb,
                    stop_flag=self._stop_flag,
                    pause_flag=self._pause_flag,
                    failed_paths=failed,
                    library_cache=_lib_cache,
                    trust_library=_effective_trust,
                )
                # Write scan results back to the library cache so future
                # scans skip re-hashing unchanged files (staleness check via
                # mtime+size guards against serving stale hashes).
                if records:
                    try:
                        from library import (Library, get_library_dir, FileRecord,
                                             FolderEntry, get_drive_info,
                                             compute_folder_fingerprint)
                        from datetime import datetime as _dt
                        _lib_wb = Library.load(get_library_dir())
                        _wb_cache: dict = {}
                        for _r in records:
                            try:
                                _st = _r.path.stat()
                                _wb_cache[str(_r.path)] = FileRecord.from_image_record(
                                    _r, st_mtime=_st.st_mtime)
                            except Exception:
                                _wb_cache[str(_r.path)] = FileRecord.from_image_record(_r)
                        _src_str = str(src.resolve())
                        _lib_wb.save_cache(_src_str, _wb_cache)
                        _di = get_drive_info(src)
                        _lib_wb.set_folder(FolderEntry(
                            path               = _src_str,
                            drive_type         = _di.drive_type,
                            volume_serial      = _di.volume_serial,
                            folder_fingerprint = compute_folder_fingerprint(src),
                            last_updated       = _dt.now().isoformat(),
                            file_count         = len(_wb_cache),
                        ))
                    except Exception:
                        pass   # library write-back is best-effort
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

            # Move broken/unreadable files to trash/broken/
            broken = getattr(self, "_broken_files", [])
            if not settings.dry_run and broken:
                broken_dir = out / "trash" / "broken"
                broken_dir.mkdir(parents=True, exist_ok=True)
                for bp in broken:
                    try:
                        bp = Path(bp) if not isinstance(bp, Path) else bp
                        if bp.exists():
                            import shutil as _sh
                            _sh.move(str(bp), str(_unique_dest(broken_dir, bp.name)))
                    except Exception:
                        pass

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

            n_orig      = sum(len(g.originals) for g in groups)
            n_prev      = sum(len(g.previews)  for g in groups)
            n_ambiguous = sum(1 for g in groups if getattr(g, "is_ambiguous", False))
            n_solo      = len(solo_originals)
            space_saved = sum(r.file_size for g in groups for r in g.previews)
            msg = (
                f"Done. {len(records):,} scanned — "
                f"{len(groups)} groups, {n_orig} kept, {n_prev} duplicates."
            )
            self.root.after(0, lambda: self._on_done(
                msg, success=True, dry_run=settings.dry_run,
                total_scanned=len(records), n_groups=len(groups),
                n_prev=n_prev, src_folder=str(src),
                n_solo=n_solo, n_ambiguous=n_ambiguous, space_saved=space_saved,
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

    # ── settings lock during scan ─────────────────────────────────────────

    def _lock_settings(self) -> None:
        """Disable all settings controls while a scan is running."""
        for frame in (
            self._scan_folders_section,
            self._scan_mode_card,
            self._quick_speed_frame,
            self._compact_adv_frame,
            self._custom_folders_section,
            self._custom_ks_frame,
        ):
            try:
                _set_interactive_state(frame, tk.DISABLED)
            except Exception:
                pass
        try:
            self._nb.tab(self._tab_settings, state="disabled")
        except Exception:
            pass

    def _unlock_settings(self) -> None:
        """Re-enable all settings controls after scan completes or is cancelled."""
        for frame in (
            self._scan_folders_section,
            self._scan_mode_card,
            self._quick_speed_frame,
            self._compact_adv_frame,
            self._custom_folders_section,
            self._custom_ks_frame,
        ):
            try:
                _set_interactive_state(frame, tk.NORMAL)
            except Exception:
                pass
        try:
            self._nb.tab(self._tab_settings, state="normal")
        except Exception:
            pass
        # Restore rawpy buttons if library not installed
        if not _RAWPY_AVAILABLE:
            for cb in (
                getattr(self, "_compact_rawpy_cb", None),
                getattr(self, "_compact_rawpy_cb2", None),
            ):
                if cb is not None:
                    try:
                        cb.configure(state=tk.DISABLED)
                    except Exception:
                        pass

    # ── done / error callbacks ────────────────────────────────────────────

    def _on_done(
        self, msg: str, success: bool = True,
        dry_run: bool = False, paused: bool = False,
        total_scanned: int = 0, n_groups: int = 0,
        n_prev: int = 0, src_folder: str = "",
        n_solo: int = 0, n_ambiguous: int = 0, space_saved: int = 0,
    ) -> None:
        _set_sleep_prevention(False)
        self._progress_bar.stop()
        self._progress_bar["mode"]  = "determinate"
        self._progress_bar["value"] = 100 if (success and not paused) else self._progress_bar["value"]
        self._scanning = False

        if paused:
            # Keep the active frame visible; flip Pause → Resume
            self._is_paused = True
            self.pause_btn.configure(
                text="▶  Resume", command=self._resume_in_place,
                state=tk.NORMAL,
            )
            _mat_enable(self.pause_btn)
            _mat_enable(self.stop_btn)    # Stop = Discard while paused
        else:
            self._unlock_settings()
            # Restore idle button frame
            self._scan_active_frame.pack_forget()
            self._scan_idle_frame.pack(fill=tk.X, padx=4)
            # Reset pause button for next scan
            self.pause_btn.configure(text="⏸  Pause", command=self._pause_scan)

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

            # Refresh the Library tab so newly-cached hashes appear there.
            if hasattr(self, "_library_ctrl"):
                self._library_ctrl.reload()

            # Compute elapsed scan time.
            _elapsed_s = time.perf_counter() - getattr(self, "_scan_start_time",
                                                        time.perf_counter())

            # Log to history and show Results tab
            self._log_scan_history(
                total_files=total_scanned,
                groups=n_groups,
                duplicates=n_prev,
                dry_run=dry_run,
                src_folder=src_folder,
                applied=not dry_run,
                duration_s=_elapsed_s,
            )
            self._update_results_tab_ui({
                "n_solo":      n_solo,
                "n_ambiguous": n_ambiguous,
                "space_saved": space_saved,
                "duration_s":  _elapsed_s,
            })
            self._show_results_tab()
            self._nb.select(self._tab_scan)

    def _on_error(self, msg: str, tb: str = "") -> None:
        _set_sleep_prevention(False)
        self._progress_bar.stop()
        self._phase_label_var.set("Scan failed — see error message.")
        self._scanning = False
        self._unlock_settings()
        self._scan_active_frame.pack_forget()
        self._scan_idle_frame.pack(fill=tk.X, padx=4)
        user_msg, detail = error_handler.format_scan_error(Exception(msg), tb)
        error_handler.show_error(self.root, "Scan Failed", user_msg, detail=detail)

    # ── post-scan actions ─────────────────────────────────────────────────

    def _accept_and_move(self) -> None:
        if not self.scan_groups:
            error_handler.show_warning(self.root, "Nothing to Move",
                "No duplicate groups found. Run a scan first.")
            return
        n_dups = sum(len(g.previews) for g in self.scan_groups
                     if not getattr(g, "is_ambiguous", False))
        if n_dups == 0:
            error_handler.show_info(self.root, "Nothing to Move",
                "All groups are flagged for manual review (ambiguous).\n"
                "No files to move automatically.")
            return
        if not messagebox.askyesno(
            "Accept & Move",
            f"Move {n_dups} duplicate file(s) to trash?\n"
            "Originals stay in place. This action can be reverted via 'Revert All'.",
            parent=self.root,
        ):
            return
        out = self.settings.out_folder.strip()
        if not out:
            error_handler.show_warning(self.root, "No Output Folder",
                "Output folder is not set.\n\n"
                "Go to the Scan tab, set an output folder, and re-run the scan.")
            return
        _mat_disable(self.accept_btn)
        self._phase_label_var.set("Moving files…")

        def _do_move() -> None:
            try:
                moved_prev, err_count = move_groups(
                    self.scan_groups, Path(out), dry_run=False, settings=self.settings
                )
                report = generate_report(
                    self.scan_groups, Path(out),
                    Path(self.settings.src_folder),
                    len(self.scan_records), self.settings,
                )
                self.report_path = report
                trash_dir = Path(out) / "trash"
                err_note = f"  ({err_count} errors)" if err_count else ""
                msg = f"Moved {moved_prev} duplicate(s) to trash{err_note}."
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

                # Show success/failure feedback on the Results tab (visible to user)
                def _show_done(n=moved_prev, errs=err_count, td=trash_dir, rpt=report) -> None:
                    if n == 0:
                        detail = (
                            "Possible reasons:\n"
                            "• Files may have already been moved\n"
                            "• Files not found at their scanned paths\n"
                            "• Permission denied on source or trash folder\n\n"
                            f"Trash folder: {td}\n"
                            "Check operations_log.json in the output folder for details."
                        )
                        messagebox.showwarning(
                            "Nothing Moved",
                            f"No files were moved ({errs} error(s)).\n\n{detail}",
                            parent=self.root,
                        )
                    else:
                        body = f"✓  {n} duplicate(s) moved to trash."
                        if errs:
                            body += f"\n⚠  {errs} file(s) could not be moved (see operations_log.json)."
                        body += f"\n\nTrash folder:\n{td}\n\nOpen updated HTML report?"
                        if messagebox.askyesno("Move Complete", body, parent=self.root):
                            if rpt:
                                webbrowser.open(rpt.as_uri())
                self.root.after(0, _show_done)
            except Exception as exc:
                import traceback as _tb; _detail = _tb.format_exc()
                self.root.after(0, lambda e=exc, d=_detail: error_handler.show_error(
                    self.root, "Move Failed",
                    "Could not move the files.\nCheck folder permissions and available disk space.",
                    detail=d))

        threading.Thread(target=_do_move, daemon=True).start()

    def _open_browser_report(self) -> None:
        if self.report_path and self.report_path.exists():
            webbrowser.open(self.report_path.as_uri())

    def _open_inapp_report(self) -> None:
        if not self.scan_groups and not self.scan_records:
            error_handler.show_info(self.root, "No Results",
                "No scan results yet. Run a scan first, then come back to review.")
            return
        out = self.settings.out_folder.strip()

        def _apply_cb(_paths_trashed: list) -> None:
            if out:
                report = generate_report(
                    self.scan_groups, Path(out),
                    Path(self.settings.src_folder),
                    len(self.scan_records), self.settings,
                )
                self.report_path = report
                from scan_state import delete_results
                delete_results(Path(out))

        self._show_results_tab()
        self._nb.select(self._tab_scan)
        self._embed_report_viewer(
            self.scan_groups, self._solo_originals, self._broken_files, out, _apply_cb
        )

    def _revert_all(self) -> None:
        out = self.settings.out_folder.strip()
        if not out:
            return
        log_path = ops_log_path(Path(out))
        if not log_path.exists():
            error_handler.show_info(self.root, "Nothing to Revert",
                "No files have been moved yet — nothing to revert.")
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
    from single_instance import SingleInstance
    si = SingleInstance()
    if si.is_secondary():
        si.signal_and_exit()   # focus the first window, then exit immediately

    root = tk.Tk()
    app  = App(root)
    si.start_listener(root, app.bring_to_front)
    root.mainloop()
    si.cleanup()


if __name__ == "__main__":
    main()
