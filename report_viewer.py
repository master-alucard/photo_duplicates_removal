"""
report_viewer.py — In-app report viewer with Material Design, per-group
                   confirmation, calibration-from-review, and revert support.
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional

# Limit simultaneous thumbnail-decode threads so we don't spawn hundreds at once
_THUMB_SEMAPHORE = threading.Semaphore(12)

import tkinter as tk
from tkinter import messagebox, ttk

try:
    from PIL import Image as PILImage, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

from scanner import DuplicateGroup, ImageRecord
import error_handler


class _TrueStub:
    """Lightweight stand-in for a tk.BooleanVar that always returns True."""
    __slots__ = ()
    @staticmethod
    def get() -> bool:
        return True

_TRUE_STUB = _TrueStub()


def _make_checkbox_pair(
    size: int = 22,
    check_fill: str = "#2E7D32",
    check_outline: str = "#1B5E20",
    box_outline: str = "#B0B0B0",
    box_fill: str = "#FFFFFF",
) -> "tuple[ImageTk.PhotoImage, ImageTk.PhotoImage]":
    """Render a crisp unchecked/checked checkbox pair at 4× and downscale."""
    from PIL import ImageDraw
    S = size * 4  # supersample factor
    r = int(S * 0.14)  # corner radius

    # ── unchecked ────────────────────────────────────────────────────────
    img = PILImage.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [3, 3, S - 4, S - 4], radius=r,
        outline=box_outline, width=max(3, S // 12), fill=box_fill,
    )
    unchecked = ImageTk.PhotoImage(
        img.resize((size, size), PILImage.LANCZOS))

    # ── checked (green box + white ✓) ────────────────────────────────────
    img = PILImage.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [3, 3, S - 4, S - 4], radius=r,
        fill=check_fill, outline=check_outline, width=max(2, S // 16),
    )
    # ✓  checkmark: left leg from (22%, 52%) → (40%, 72%), right leg → (78%, 24%)
    lw = max(4, S // 8)
    pts = [
        (int(S * 0.22), int(S * 0.52)),
        (int(S * 0.40), int(S * 0.72)),
        (int(S * 0.78), int(S * 0.24)),
    ]
    d.line(pts, fill="white", width=lw, joint="curve")
    # Round the endpoints
    hr = lw // 2
    for px, py in (pts[0], pts[-1]):
        d.ellipse([px - hr, py - hr, px + hr, py + hr], fill="white")
    checked = ImageTk.PhotoImage(
        img.resize((size, size), PILImage.LANCZOS))

    return unchecked, checked


# ── Material Design colour palette (light defaults, overwritten by _apply_theme) ──

_M_BG           = "#F2F4F7"
_M_SURFACE      = "#FFFFFF"
_M_PRIMARY      = "#1565C0"
_M_PRIMARY_DARK = "#0D47A1"
_M_PRIMARY_TINT = "#E8EFF9"
_M_SUCCESS      = "#2E7D32"
_M_SUCCESS_TINT = "#E8F5E9"
_M_ERROR        = "#C62828"
_M_ERROR_TINT   = "#FFEBEE"
_M_WARNING      = "#E65100"
_M_WARNING_TINT = "#FFF3E0"
_M_PURPLE       = "#6A1B9A"
_M_DIVIDER      = "#DDE1E6"
_M_TEXT1        = "#1B1B1F"
_M_TEXT2        = "#49454F"
_M_TEXT3        = "#79747E"
_M_SOLO_TINT    = "#E1F5FE"
_M_SOLO_BORDER  = "#0288D1"
_M_BROKEN_TINT  = "#FCE4EC"
_M_BROKEN_BDR   = "#AD1457"
_M_MANUAL      = "#5C6BC0"
_M_MANUAL_TINT = "#E8EAF6"
_M_MANUAL_HDR  = "#EDE7F6"
_M_MANUAL_DARK = "#4527A0"


def _apply_theme(dark: bool = False) -> None:
    """Overwrite module colours from the theme palette."""
    global _M_BG, _M_SURFACE, _M_PRIMARY, _M_PRIMARY_DARK, _M_PRIMARY_TINT
    global _M_SUCCESS, _M_SUCCESS_TINT, _M_ERROR, _M_ERROR_TINT
    global _M_WARNING, _M_WARNING_TINT, _M_PURPLE
    global _M_DIVIDER, _M_TEXT1, _M_TEXT2, _M_TEXT3
    global _M_SOLO_TINT, _M_SOLO_BORDER, _M_BROKEN_TINT, _M_BROKEN_BDR
    global _M_MANUAL, _M_MANUAL_TINT, _M_MANUAL_HDR, _M_MANUAL_DARK
    global _RV_REVERT_BG, _RV_CALIB_BG, _RV_SELECT_BG, _RV_SELECT_FG
    global _RV_HEADER_STATS, _RV_HEADER_BG, _RV_BTN_PRIMARY, _RV_BTN_SUCCESS
    global _CARD_BG, _ORIG_BG, _PREV_BG, _HEADER_BG, _SERIES_COLOR
    global _SOLO_BG, _BROKEN_BG
    import theme as _t
    p = _t.get_palette(dark)
    _M_BG           = p["BG"]
    _M_SURFACE      = p["CARD_BG"]
    _M_PRIMARY      = p["ACCENT"]
    _M_PRIMARY_DARK = p["ACCENT_DARK"]
    _M_PRIMARY_TINT = p["PRIMARY_TINT"]
    _M_SUCCESS      = p["SUCCESS"]
    _M_SUCCESS_TINT = p["SUCCESS_TINT"]
    _M_ERROR        = p["ERROR"]
    _M_ERROR_TINT   = p["ERROR_TINT"]
    _M_WARNING      = p["WARNING"]
    _M_WARNING_TINT = p["WARNING_TINT"]
    _M_DIVIDER      = p["DIVIDER"]
    _M_TEXT1        = p["TEXT1"]
    _M_TEXT2        = p["TEXT2"]
    _M_TEXT3        = p["TEXT3"]
    # Report-specific semantic colours — keep readable in both themes
    if dark:
        _M_PURPLE       = "#CE93D8"   # Purple 200
        _M_SOLO_TINT    = "#1A2A33"   # Dark teal tint
        _M_SOLO_BORDER  = "#4FC3F7"   # Light Blue 300
        _M_BROKEN_TINT  = "#2E1A22"   # Dark pink tint
        _M_BROKEN_BDR   = "#F48FB1"   # Pink 200
        _M_MANUAL       = "#9FA8DA"   # Indigo 200
        _M_MANUAL_TINT  = "#1A1D33"   # Dark indigo tint
        _M_MANUAL_HDR   = "#1E1A2E"   # Dark purple tint
        _M_MANUAL_DARK  = "#B39DDB"   # Deep Purple 200
    else:
        _M_PURPLE       = "#6A1B9A"
        _M_SOLO_TINT    = "#E1F5FE"
        _M_SOLO_BORDER  = "#0288D1"
        _M_BROKEN_TINT  = "#FCE4EC"
        _M_BROKEN_BDR   = "#AD1457"
        _M_MANUAL       = "#5C6BC0"
        _M_MANUAL_TINT  = "#E8EAF6"
        _M_MANUAL_HDR   = "#EDE7F6"
        _M_MANUAL_DARK  = "#4527A0"
    _RV_REVERT_BG    = p["RV_REVERT_BG"]
    _RV_CALIB_BG     = p["RV_CALIB_BG"]
    _RV_SELECT_BG    = p["RV_SELECT_BG"]
    _RV_SELECT_FG    = p["RV_SELECT_FG"]
    _RV_HEADER_STATS = p["RV_HEADER_STATS"]
    _RV_HEADER_BG    = p["HEADER_BG"]
    _RV_BTN_PRIMARY  = p["BTN_PRIMARY"]
    _RV_BTN_SUCCESS  = p["BTN_SUCCESS"]
    # Legacy aliases
    _CARD_BG      = _M_SURFACE
    _ORIG_BG      = _M_SUCCESS_TINT
    _PREV_BG      = _M_ERROR_TINT
    _HEADER_BG    = _M_PRIMARY
    _SERIES_COLOR = _M_PURPLE
    _SOLO_BG      = _M_SOLO_TINT
    _BROKEN_BG    = _M_BROKEN_TINT


_RV_REVERT_BG    = "#455A64"
_RV_CALIB_BG     = "#5C6BC0"
_RV_SELECT_BG    = "#FFFFFF"
_RV_SELECT_FG    = "#1565C0"
_RV_HEADER_STATS = "#BBDEFB"
_RV_HEADER_BG    = "#1565C0"
_RV_BTN_PRIMARY  = "#1565C0"
_RV_BTN_SUCCESS  = "#2E7D32"

_THUMB_SIZE = 156

# Keep legacy aliases used elsewhere
_CARD_BG    = _M_SURFACE
_ORIG_BG    = _M_SUCCESS_TINT
_PREV_BG    = _M_ERROR_TINT
_HEADER_BG  = _M_PRIMARY
_SERIES_COLOR = _M_PURPLE
_SOLO_BG    = _M_SOLO_TINT
_BROKEN_BG  = _M_BROKEN_TINT


# ── helper: info popup ────────────────────────────────────────────────────────

_INFO_TEXTS = {
    "same_image": (
        "Same Image — confirm duplicate",
        "Press this button when all images in this group are confirmed to be the same photo "
        "(original + compressed / resized copies).\n\n"
        "Confirming helps the Calibrate from Review feature learn what correct matches look like. "
        "Confirmed groups are shown with a green border.",
    ),
    "wrong_group": (
        "Wrong Group — incorrect match",
        "Press this button when the app wrongly grouped photos that are actually DIFFERENT scenes "
        "or different photos.\n\n"
        "Marking a group as wrong helps the Calibrate from Review feature avoid similar mistakes. "
        "Wrong groups are shown with a red border.",
    ),
    "different_image": (
        "Different image",
        "Uncheck this image to mark it as a DIFFERENT photo — it does not actually belong to this "
        "duplicate group.\n\n"
        "Unchecked images will be treated as independent originals when you use "
        "'Calibrate from Review'.",
    ),
    "calibrate_review": (
        "Calibrate from Review",
        "Uses your manual review choices to automatically improve the detection settings.\n\n"
        "Before clicking this button:\n"
        "  ✓  Check groups where all images are real duplicates\n"
        "  ✗  Uncheck groups where photos were wrongly matched\n"
        "  ✗  Uncheck individual images that don't belong in a group\n\n"
        "The app will create a calibration dataset from your choices and run the 3-round "
        "calibration search to find the best threshold and ratio for your library.",
    ),
}

def _show_info(parent: tk.Widget, key: str) -> None:
    title, text = _INFO_TEXTS.get(key, ("Help", "No help text available."))
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("480x260")
    win.grab_set()
    win.resizable(False, False)
    win.configure(bg=_M_SURFACE)
    tk.Label(win, text=title, font=("Segoe UI", 11, "bold"),
             bg=_M_SURFACE, fg=_M_TEXT1).pack(anchor=tk.W, padx=16, pady=(14, 4))
    tk.Frame(win, height=1, bg=_M_DIVIDER).pack(fill=tk.X, padx=16, pady=(0, 8))
    txt = tk.Text(win, wrap=tk.WORD, padx=14, pady=8, relief=tk.FLAT,
                  bg=_M_SURFACE, fg=_M_TEXT2, font=("Segoe UI", 9))
    txt.insert("1.0", text)
    txt.config(state=tk.DISABLED)
    txt.pack(fill=tk.BOTH, expand=True, padx=4)
    _mat_btn(win, "Close", win.destroy, _RV_BTN_PRIMARY).pack(pady=10)


def _mat_btn(
    parent: tk.Widget,
    text: str,
    command: Callable,
    bg: str,
    fg: str = "#FFFFFF",
    font_size: int = 9,
    **kw,
) -> tk.Button:
    """Flat Material-style button."""
    # Strip state from kw so we can apply it properly via _mat_disable
    initial_disabled = kw.pop("state", None) == tk.DISABLED

    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=_darken_color(bg), activeforeground=fg,
        relief=tk.FLAT, bd=0, padx=12, pady=5,
        font=("Segoe UI", font_size, "bold"),
        cursor="hand2", **kw,
    )
    btn._mat_bg = bg
    btn._mat_fg = fg

    def _enter(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=_darken_color(btn._mat_bg))

    def _leave(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=btn._mat_bg)

    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)

    if initial_disabled:
        _mat_disable(btn)
    return btn


def _mat_enable(btn: tk.Button) -> None:
    btn.configure(state=tk.NORMAL, bg=btn._mat_bg, fg=btn._mat_fg,
                  activebackground=_darken_color(btn._mat_bg),
                  activeforeground=btn._mat_fg, cursor="hand2")


def _mat_disable(btn: tk.Button) -> None:
    btn.configure(state=tk.DISABLED, bg=_M_DIVIDER, fg=_M_TEXT3, cursor="")


def _darken_color(hex_color: str) -> str:
    """Return a slightly darker shade of a hex color."""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        factor = 0.85
        r, g, b = int(r * factor), int(g * factor), int(b * factor)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


def _info_btn(parent: tk.Widget, key: str, bg: str = _M_SURFACE) -> tk.Button:
    """Small ⓘ info button (Material-style, same bg as parent)."""
    return tk.Button(
        parent, text=" ⓘ ", command=lambda k=key: _show_info(parent.winfo_toplevel(), k),
        bg=bg, fg=_M_PRIMARY, activebackground=bg, activeforeground=_M_PRIMARY_DARK,
        relief=tk.FLAT, bd=0, padx=2, pady=2,
        font=("Segoe UI", 9), cursor="hand2",
    )


# ── main viewer ───────────────────────────────────────────────────────────────

class ReportViewer(tk.Frame):
    """Material-style in-app report viewer with confirmation, calibration-from-review."""

    def __init__(
        self,
        parent: tk.Widget,
        groups: List[DuplicateGroup],
        ops_log_path: Optional[Path] = None,
        on_apply_cb: Optional[Callable] = None,
        solo_originals: Optional[List[ImageRecord]] = None,
        broken_files: Optional[List[Path]] = None,
        settings=None,
        on_close_cb: Optional[Callable] = None,
    ) -> None:
        super().__init__(parent, bg=_M_BG)

        self._groups          = groups
        self._ops_log_path    = ops_log_path
        self._on_apply_cb     = on_apply_cb
        self._solo_originals  = solo_originals or []
        self._broken_files    = broken_files or []
        self._settings        = settings
        self._on_close_cb     = on_close_cb

        # Photo reference storage (prevent GC)
        self._photo_refs: list = []
        self._placeholder_cache: dict[tuple, ImageTk.PhotoImage] = {}  # (size, bg) → photo

        # Per-group: include checkbox, status, border-frame ref
        # (pre-created for ALL groups so page changes don't lose state)
        self._group_vars: dict[int, tk.BooleanVar] = {}
        self._group_status: dict[int, str] = {}       # "confirmed" | "wrong" | ""
        self._group_border_frames: dict[int, tk.Frame] = {}  # only current page
        self._group_frames: dict[int, tk.Frame] = {}  # outer frame per group card
        self._group_calib_containers: dict[int, tk.Frame] = {}  # calibration button area per group
        self._group_calib_badges: dict[int, tk.Label] = {}     # "In calibration" badge per group
        self._active_traces: list[tuple[tk.BooleanVar, str]] = []  # (var, trace_id) for cleanup

        # Applied state — paths successfully moved to trash (populated after apply)
        self._trashed_paths: set[Path] = set()

        # Manual trash selection (from originals / solo images)
        self._manual_trash_selected: set[Path] = set()
        self._manual_trashed_items: list[dict] = []   # {"original": Path, "trash": Path, "rec": Optional[ImageRecord]}
        self._manual_trash_tile_frames: dict[Path, tk.Frame] = {}  # selectable tile refs

        # Per-image: belongs-to-group checkbox  (group_idx, role, img_idx) → BoolVar
        self._image_vars: dict[tuple, tk.BooleanVar] = {}

        # Solo original checkboxes
        self._solo_vars: dict[int, tk.BooleanVar] = {}

        # Groups flagged for false-positive calibration
        self._fp_calib_groups: set[int] = set()

        # Manual calibration groups (list of path lists, created by user in the review)
        self._manual_calib_groups: list[list[Path]] = []
        self._manual_group_vars: dict[int, tk.BooleanVar] = {}    # per-manual-group include checkbox
        self._manual_used_paths: set[Path] = set()                 # paths assigned to manual groups or unsorted
        self._manual_selected_paths: set[Path] = set()             # currently selected tile paths
        self._manual_tile_frames: dict[Path, tk.Frame] = {}        # path → tile frame
        self._unsorted_paths: list[Path] = []                      # images removed from manual groups
        self._solo_visible_paths: list[Path] = []                  # ordered visible solo paths (for shift-select)
        self._last_solo_click_path: Optional[Path] = None          # anchor for shift-click range

        # Pagination
        self._current_page: int = 0
        self._page_size: int = (settings.report_page_size
                                if settings and hasattr(settings, "report_page_size")
                                else 100)

        # Deferred thumbnail loading
        self._pending_thumbs: list[tuple] = []  # (path, label, max_px, grayscale) queued during build
        self._thumb_batch_id: int = 0           # bumped on page change to cancel stale loads

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._setup_style()
        self._create_checkbox_images()

        # ── Header bar ────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=_RV_HEADER_BG)
        hdr.pack(fill=tk.X)
        self._header_frame = hdr  # saved for revert-frame packing

        if self._on_close_cb:
            _mat_btn(hdr, "◀  Back to Results", self._on_close_cb,
                     _RV_HEADER_BG, fg=_RV_HEADER_STATS, font_size=9,
                     ).pack(side=tk.LEFT, padx=(12, 0), pady=8)

        tk.Label(
            hdr, text="Review Scan Results",
            font=("Segoe UI", 14, "bold"), bg=_RV_HEADER_BG, fg="#FFFFFF",
        ).pack(side=tk.LEFT, padx=18, pady=12)

        n_groups   = len(self._groups)
        n_previews = sum(len(g.previews) for g in self._groups)
        n_series   = sum(1 for g in self._groups if g.is_series)
        parts = [f"{n_groups} groups", f"{n_previews} previews"]
        if n_series:
            parts.append(f"{n_series} series")
        if self._solo_originals:
            parts.append(f"{len(self._solo_originals)} unique")
        if self._broken_files:
            parts.append(f"{len(self._broken_files)} broken")
        tk.Label(
            hdr, text="  ·  ".join(parts),
            font=("Segoe UI", 9), bg=_RV_HEADER_BG, fg=_RV_HEADER_STATS,
        ).pack(side=tk.LEFT, padx=4)

        # Right side of header: status, revert, apply
        self._status_lbl = tk.Label(hdr, text="", bg=_RV_HEADER_BG,
                                    fg=_RV_HEADER_STATS, font=("Segoe UI", 9))
        self._status_lbl.pack(side=tk.RIGHT, padx=12)

        # Revert buttons — always created, revealed only after first successful apply
        self._revert_frame = tk.Frame(hdr, bg=_RV_HEADER_BG)
        self._revert_selected_btn = _mat_btn(
            self._revert_frame, "↩  Revert Selected", self._on_revert_selected, _RV_REVERT_BG)
        self._revert_selected_btn.pack(side=tk.LEFT, padx=4)
        self._revert_all_btn = _mat_btn(
            self._revert_frame, "↩  Revert All", self._on_revert_all, _RV_REVERT_BG)
        self._revert_all_btn.pack(side=tk.LEFT, padx=4)
        # Do NOT pack _revert_frame yet — only shown after _on_apply_done succeeds

        self._apply_btn = _mat_btn(hdr, "📦  Move Duplicates", self._on_apply, _RV_BTN_SUCCESS)
        self._apply_btn.pack(side=tk.RIGHT, padx=(4, 12), pady=8)

        # ── Pagination nav bar ────────────────────────────────────────────
        nav = tk.Frame(self, bg=_M_SURFACE,
                       highlightthickness=0)
        nav.pack(fill=tk.X)
        self._nav_bar_frame = nav   # saved for show/hide during inline panels

        # Prev / Next buttons (left side)
        self._prev_btn = _mat_btn(nav, "◀  Prev", self._prev_page,
                                  _RV_BTN_PRIMARY, font_size=8, state=tk.DISABLED)
        self._prev_btn.pack(side=tk.LEFT, padx=(10, 2), pady=4)
        self._next_btn = _mat_btn(nav, "Next  ▶", self._next_page,
                                  _RV_BTN_PRIMARY, font_size=8)
        self._next_btn.pack(side=tk.LEFT, padx=(2, 10), pady=4)

        self._page_info_var = tk.StringVar(value="")
        tk.Label(nav, textvariable=self._page_info_var,
                 bg=_M_SURFACE, fg=_M_TEXT2,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=14)

        # Jump-to-unique button (only shown when there are solo originals)
        if self._solo_originals:
            self._unique_btn = _mat_btn(
                nav,
                f"★  Unique  ({len(self._solo_originals)})",
                lambda: self._render_page(self._unique_page_index()),
                _RV_BTN_PRIMARY, font_size=8,
            )
            self._unique_btn.pack(side=tk.LEFT, padx=(0, 4), pady=4)

        # Calibrate False Positives button (hidden until groups are flagged)
        self._fp_calib_btn = _mat_btn(nav, "🔧  Calibrate False Positives",
                                       self._run_fp_calibration, _RV_CALIB_BG, font_size=8)
        # Don't pack yet — shown by _update_fp_calib_btn when groups are flagged

        # Per-page size selector (right side — dropdown at far right, label to its left)
        self._page_size_var = tk.StringVar(value=str(self._page_size))
        ps_cb = ttk.Combobox(nav, textvariable=self._page_size_var,
                             values=["10", "20", "50", "100"],
                             width=5, state="readonly")
        ps_cb.pack(side=tk.RIGHT, padx=(0, 12))
        tk.Label(nav, text="Groups per page:",
                 bg=_M_SURFACE, fg=_M_TEXT2,
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=(0, 4))
        self._page_size_var.trace_add("write", self._on_page_size_change)

        # Select All / Select None (right side of nav, before page-size selector)
        _mat_btn(nav, "Select None", self._select_none, _RV_SELECT_BG, fg=_RV_SELECT_FG,
                 ).pack(side=tk.RIGHT, padx=6, pady=4)
        _mat_btn(nav, "Select All", self._select_all, _RV_SELECT_BG, fg=_RV_SELECT_FG,
                 ).pack(side=tk.RIGHT, padx=2, pady=4)

        # ── Scrollable canvas ─────────────────────────────────────────────
        container = tk.Frame(self, bg=_M_BG)
        container.pack(fill=tk.BOTH, expand=True)
        self._canvas_container = container   # saved for show/hide during inline panels

        self._canvas = tk.Canvas(container, bg=_M_BG, highlightthickness=0,
                                yscrollincrement=1)   # 1-pixel granularity for smooth scroll
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL,
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._inner_frame = tk.Frame(self._canvas, bg=_M_BG)
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._inner_frame, anchor=tk.NW)

        self._inner_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Bind mousewheel directly on the canvas and container — no bind_all so
        # the global mouse input is never intercepted (fixes sensitivity on Windows).
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._canvas_container.bind("<MouseWheel>", self._on_mousewheel)
        self._inner_frame.bind("<MouseWheel>", self._on_mousewheel)

        # Init solo vars; group vars are created lazily per page by _ensure_group_vars
        self._init_vars()
        # Render the first page
        self._render_page(0)

    def _setup_style(self) -> None:
        style = ttk.Style()
        try:
            style.configure("TScrollbar", troughcolor=_M_BG, background=_M_PRIMARY)
        except Exception:
            pass

    def _get_placeholder(self, size: int, bg: str) -> "ImageTk.PhotoImage":
        """Return a cached solid-colour placeholder image of the given pixel size.
        Cached separately from _photo_refs so page changes don't destroy them."""
        key = (size, bg)
        ph = self._placeholder_cache.get(key)
        if ph is not None:
            return ph
        if _PIL_AVAILABLE:
            try:
                r = int(bg[1:3], 16)
                g = int(bg[3:5], 16)
                b = int(bg[5:7], 16)
            except Exception:
                r, g, b = 240, 240, 240
            img = PILImage.new("RGB", (size, size), (r, g, b))
            ph = ImageTk.PhotoImage(img)
        else:
            ph = tk.PhotoImage(width=size, height=size)
        self._placeholder_cache[key] = ph
        return ph

    def _create_checkbox_images(self):
        """Create 22x22 checkbox images rendered at 4× for crisp anti-aliasing."""
        if not _PIL_AVAILABLE:
            self._cb_checked = self._cb_unchecked = None
            return
        self._cb_unchecked, self._cb_checked = _make_checkbox_pair(22)

    # ── group card ────────────────────────────────────────────────────────────

    def _build_group_card(self, idx: int, group: DuplicateGroup) -> None:
        self._ensure_group_vars(idx)
        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)
        self._group_frames[idx] = outer

        # ── Calculate applied state for this group ───────────────────────
        prev_paths = [rec.path for rec in group.previews]
        n_prev     = len(prev_paths)
        n_trashed  = sum(1 for p in prev_paths if p in self._trashed_paths)
        # Only count previews whose checkbox is checked (the ones selected for trash)
        n_checked  = sum(
            1 for img_idx in range(n_prev)
            if (self._image_vars.get((idx, "prev", img_idx)) or _TRUE_STUB).get()
        )
        if n_trashed > 0 and n_trashed >= n_checked and n_checked > 0:
            apply_state = "full"    # all selected previews trashed
        elif n_trashed > 0:
            apply_state = "partial" # some selected previews trashed
        else:
            apply_state = "none"

        # Card with left-colour border
        if idx in self._fp_calib_groups:
            border_color = _M_WARNING
        elif apply_state == "full":
            border_color = _M_SUCCESS
        elif apply_state == "partial":
            border_color = _M_WARNING
        else:
            border_color = _M_PRIMARY

        card_wrap = tk.Frame(outer, bg=_M_SURFACE,
                             highlightthickness=0)
        card_wrap.pack(fill=tk.X)

        left_border = tk.Frame(card_wrap, width=4, bg=border_color)
        left_border.pack(side=tk.LEFT, fill=tk.Y)
        self._group_border_frames[idx] = left_border

        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Header ──────────────────────────────────────────────────────
        head = tk.Frame(card, bg=_M_PRIMARY_TINT, pady=0)
        head.pack(fill=tk.X)

        g_var = self._group_vars[idx]  # pre-created by _init_vars; preserves state across pages
        _cb_kw = {}
        if self._cb_unchecked and self._cb_checked:
            _cb_kw = dict(image=self._cb_unchecked, selectimage=self._cb_checked,
                          indicatoron=False, bd=0, relief=tk.FLAT, selectcolor=_M_PRIMARY_TINT)
        else:
            _cb_kw = dict(selectcolor=_M_SUCCESS)
        tk.Checkbutton(
            head, variable=g_var, bg=_M_PRIMARY_TINT,
            command=lambda i=idx: self._on_group_toggle(i),
            activebackground=_M_PRIMARY_TINT,
            **_cb_kw,
        ).pack(side=tk.LEFT, padx=(10, 0), pady=8)

        n_orig = len(group.originals)
        lbl = (
            f"Group #{idx + 1}  ·  {n_orig} original{'s' if n_orig != 1 else ''}"
            f"  ·  {n_prev} preview{'s' if n_prev != 1 else ''}"
        )
        tk.Label(
            head, text=lbl,
            font=("Segoe UI", 9, "bold"), bg=_M_PRIMARY_TINT, fg=_M_PRIMARY,
        ).pack(side=tk.LEFT, padx=8, pady=8)

        if group.is_series:
            tk.Label(
                head, text=" SERIES — all kept ",
                font=("Segoe UI", 8, "bold"), bg=_M_PURPLE, fg="#FFFFFF",
                padx=6, pady=2,
            ).pack(side=tk.LEFT, padx=4)

        # Applied state badge
        if apply_state == "full":
            tk.Label(
                head, text=f" ✓ {n_trashed} trashed ",
                font=("Segoe UI", 8, "bold"), bg=_M_SUCCESS, fg="#FFFFFF",
                padx=6, pady=2,
            ).pack(side=tk.LEFT, padx=4)
        elif apply_state == "partial":
            tk.Label(
                head, text=f" ⚠ {n_trashed}/{n_checked} trashed ",
                font=("Segoe UI", 8, "bold"), bg=_M_WARNING, fg="#FFFFFF",
                padx=6, pady=2,
            ).pack(side=tk.LEFT, padx=4)

        # "In calibration" badge — always created, shown/hidden dynamically
        calib_badge = tk.Label(head, text=" 📋 In calibration ",
                               font=("Segoe UI", 8, "bold"), bg=_M_WARNING, fg="#FFFFFF",
                               padx=6, pady=2)
        if idx in self._fp_calib_groups:
            calib_badge.pack(side=tk.LEFT, padx=4)
        self._group_calib_badges[idx] = calib_badge

        # Calibration toggle container — always created, populated by helper
        calib_btn_frame = tk.Frame(head, bg=_M_PRIMARY_TINT)
        calib_btn_frame.pack(side=tk.RIGHT, padx=8, pady=6)
        self._group_calib_containers[idx] = calib_btn_frame
        self._populate_calib_area(idx)

        # ── Body: originals | separator | previews ───────────────────────
        body = tk.Frame(card, bg=_M_SURFACE)
        body.pack(fill=tk.X)

        # Originals column — label changes after apply
        orig_col = tk.Frame(body, bg=_M_SUCCESS_TINT, padx=10, pady=8)
        orig_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        orig_lbl_text = "Originals — kept in place ✓" if apply_state != "none" else "Originals — kept in place"
        tk.Label(
            orig_col, text=orig_lbl_text,
            font=("Segoe UI", 8, "bold"), bg=_M_SUCCESS_TINT, fg=_M_SUCCESS,
        ).pack(anchor=tk.W, pady=(0, 6))

        orig_grid = tk.Frame(orig_col, bg=_M_SUCCESS_TINT)
        orig_grid.pack(fill=tk.X)
        for img_idx, rec in enumerate(group.originals):
            key = (idx, "orig", img_idx)
            v = self._image_vars[key]  # pre-created by _init_vars
            tile = self._build_image_tile(orig_grid, rec, v, img_idx % 3, img_idx // 3,
                                          bg=_M_SUCCESS_TINT, show_checkbox=False, group_idx=idx)

        tk.Frame(body, width=1, bg=_M_DIVIDER).pack(side=tk.LEFT, fill=tk.Y)

        # Previews column — label and tile state change after apply
        prev_col_bg = "#EEEEEE" if apply_state == "full" else _M_ERROR_TINT
        prev_col = tk.Frame(body, bg=prev_col_bg, padx=10, pady=8)
        prev_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        if apply_state == "full":
            prev_lbl_text = f"Duplicates trashed ✓  →  trash/"
            prev_lbl_fg   = _M_SUCCESS
        elif apply_state == "partial":
            prev_lbl_text = f"Duplicates — {n_trashed}/{n_checked} trashed  →  trash/"
            prev_lbl_fg   = _M_WARNING
        else:
            prev_lbl_text = "Duplicates to trash  →  trash/"
            prev_lbl_fg   = _M_ERROR
        tk.Label(
            prev_col, text=prev_lbl_text,
            font=("Segoe UI", 8, "bold"), bg=prev_col_bg, fg=prev_lbl_fg,
        ).pack(anchor=tk.W, pady=(0, 6))

        prev_grid = tk.Frame(prev_col, bg=prev_col_bg)
        prev_grid.pack(fill=tk.X)
        for img_idx, rec in enumerate(group.previews):
            key = (idx, "prev", img_idx)
            v = self._image_vars[key]  # pre-created by _init_vars
            trashed = rec.path in self._trashed_paths
            tile_bg = "#E0E0E0" if trashed else prev_col_bg
            self._build_image_tile(prev_grid, rec, v, img_idx % 4, img_idx // 4,
                                   bg=tile_bg, max_thumb=120, trashed=trashed, group_idx=idx)

    # ── solo & broken sections ────────────────────────────────────────────────

    def _build_solo_section(self) -> None:
        visible = [r for r in self._solo_originals if r.path not in self._manual_used_paths]
        if not visible:
            return

        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE, highlightthickness=0)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=4, bg=_M_SOLO_BORDER).pack(side=tk.LEFT, fill=tk.Y)
        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head = tk.Frame(card, bg=_M_SOLO_TINT)
        head.pack(fill=tk.X)
        tk.Label(
            head,
            text=f"Unique images — no duplicates found  ({len(visible)} files)",
            font=("Segoe UI", 9, "bold"), bg=_M_SOLO_TINT, fg=_M_SOLO_BORDER,
        ).pack(side=tk.LEFT, padx=12, pady=8)
        tk.Label(
            head, text="Check images to copy to results/",
            font=("Segoe UI", 8), bg=_M_SOLO_TINT, fg=_M_TEXT3,
        ).pack(side=tk.LEFT, padx=2)
        # "All"/"None" acts only on the visible (non-manual-grouped) subset
        visible_idxs = [i for i, r in enumerate(self._solo_originals)
                        if r.path not in self._manual_used_paths]
        _mat_btn(head, "None",
                 lambda vi=visible_idxs: [self._solo_vars[i].set(False) for i in vi],
                 _M_SOLO_TINT, fg=_M_SOLO_BORDER, font_size=8
                 ).pack(side=tk.RIGHT, padx=(0, 8), pady=4)
        _mat_btn(head, "All",
                 lambda vi=visible_idxs: [self._solo_vars[i].set(True) for i in vi],
                 _M_SOLO_TINT, fg=_M_SOLO_BORDER, font_size=8
                 ).pack(side=tk.RIGHT, padx=4, pady=4)

        grid_frame = tk.Frame(card, bg=_M_SOLO_TINT, padx=10, pady=8)
        grid_frame.pack(fill=tk.X)
        # Rebuild tile frame dict and ordered path list for shift-click selection
        self._manual_tile_frames = {}
        self._solo_visible_paths = []
        col = 0
        row = 0
        _trashed_originals = {item["original"] for item in self._manual_trashed_items}
        for img_idx, rec in enumerate(self._solo_originals):
            if rec.path in self._manual_used_paths:
                continue
            self._solo_visible_paths.append(rec.path)
            v = self._solo_vars[img_idx]
            tile = self._build_image_tile(grid_frame, rec, v, col, row, bg=_M_SOLO_TINT)
            self._manual_tile_frames[rec.path] = tile
            if rec.path not in _trashed_originals:
                self._manual_trash_tile_frames[rec.path] = tile
                self._bind_trash_select(tile, rec.path)
            col += 1
            if col >= 5:
                col = 0
                row += 1
        # Reset anchor if it left the visible set
        if self._last_solo_click_path not in self._solo_visible_paths:
            self._last_solo_click_path = None

    def _build_broken_section(self) -> None:
        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE, highlightthickness=0)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=4, bg=_M_BROKEN_BDR).pack(side=tk.LEFT, fill=tk.Y)
        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head = tk.Frame(card, bg=_M_BROKEN_TINT)
        head.pack(fill=tk.X)
        tk.Label(
            head,
            text=f"Unreadable / broken files  ({len(self._broken_files)} files)",
            font=("Segoe UI", 9, "bold"), bg=_M_BROKEN_TINT, fg=_M_BROKEN_BDR,
        ).pack(side=tk.LEFT, padx=12, pady=8)
        tk.Label(
            head, text="Could not be opened — check for corruption or unsupported format",
            font=("Segoe UI", 8), bg=_M_BROKEN_TINT, fg=_M_TEXT3,
        ).pack(side=tk.LEFT, padx=4)

        lst = tk.Frame(card, bg=_M_BROKEN_TINT, padx=12, pady=8)
        lst.pack(fill=tk.X)
        for p in self._broken_files:
            tk.Label(
                lst, text=str(p), font=("Consolas", 8),
                bg=_M_BROKEN_TINT, fg=_M_TEXT2, anchor=tk.W,
            ).pack(fill=tk.X, pady=1)

    # ── image tile ────────────────────────────────────────────────────────────

    def _build_image_tile(
        self,
        parent: tk.Frame,
        rec: ImageRecord,
        var: tk.BooleanVar,
        col: int, row: int,
        bg: str,
        max_thumb: int = _THUMB_SIZE,
        trashed: bool = False,
        show_checkbox: bool = True,
        group_idx: int = -1,
    ) -> tk.Frame:
        tile = tk.Frame(parent, bg=bg, padx=4, pady=4)
        tile.grid(row=row, column=col, padx=4, pady=4, sticky=tk.NW)

        # Thumbnail with placeholder image (fixed pixel size prevents layout reflow)
        _placeholder = self._get_placeholder(max_thumb, bg)
        thumb_lbl = tk.Label(tile, bg=bg, image=_placeholder)
        thumb_lbl.pack()
        self._load_thumbnail_async(rec.path, thumb_lbl, max_thumb, grayscale=trashed)

        if trashed:
            # ── Trashed tile: show badge, gray out, disable checkbox ─────
            tk.Label(
                tile, text="🗑  Trashed",
                font=("Segoe UI", 8, "bold"), bg=bg, fg="#757575",
            ).pack(pady=(2, 0))

            fname = rec.path.name
            tk.Label(
                tile, text=fname,
                font=("Segoe UI", 8), bg=bg, fg="#757575",
                wraplength=max_thumb,
            ).pack()
            tk.Label(
                tile, text=f"{rec.width}×{rec.height}  {rec.size_label()}",
                font=("Segoe UI", 8), bg=bg, fg="#9E9E9E",
            ).pack()
        else:
            # ── Normal tile ───────────────────────────────────────────────
            fname = rec.path.name

            if show_checkbox:
                cb_frame = tk.Frame(tile, bg=bg)
                cb_frame.pack(fill=tk.X)

                _cb_kw = {}
                if self._cb_unchecked and self._cb_checked:
                    _cb_kw = dict(image=self._cb_unchecked, selectimage=self._cb_checked,
                                  indicatoron=False, bd=0, relief=tk.FLAT, selectcolor=bg)
                else:
                    _cb_kw = dict(selectcolor=_M_SUCCESS)

                def _on_image_cb(gi=group_idx):
                    if gi >= 0:
                        self._populate_calib_area(gi)

                cb = tk.Checkbutton(
                    cb_frame, variable=var,
                    bg=bg, activebackground=bg,
                    command=_on_image_cb,
                    **_cb_kw,
                )
                cb.pack(side=tk.LEFT)

                tk.Label(
                    cb_frame, text=fname,
                    font=("Segoe UI", 8), bg=bg, fg=_M_TEXT1,
                    wraplength=max_thumb,
                ).pack(side=tk.LEFT)

                _info_btn(cb_frame, "different_image", bg=bg).pack(side=tk.LEFT, padx=0)

                # "Different image" badge (shown when unchecked)
                diff_badge = tk.Label(
                    tile, text="≠ different image",
                    font=("Segoe UI", 8, "italic"), bg=bg, fg=_M_WARNING,
                )
                def _toggle_badge(*_):
                    try:
                        if not diff_badge.winfo_exists():
                            return
                    except Exception:
                        return
                    if var.get():
                        diff_badge.pack_forget()
                    else:
                        diff_badge.pack(pady=(0, 2))
                _tid = var.trace_add("write", _toggle_badge)
                self._active_traces.append((var, _tid))
            else:
                tk.Label(
                    tile, text=fname,
                    font=("Segoe UI", 8, "bold"), bg=bg, fg=_M_TEXT1,
                    wraplength=max_thumb,
                ).pack()

            tk.Label(
                tile, text=f"{rec.width}×{rec.height}  {rec.size_label()}",
                font=("Segoe UI", 8), bg=bg, fg=_M_TEXT2,
            ).pack()
            tk.Label(
                tile, text=rec.date_label(),
                font=("Segoe UI", 8), bg=bg, fg=_M_TEXT3,
            ).pack()

        return tile

    def _bind_tile_select(self, widget: tk.Widget, path: Path) -> None:
        """Recursively bind <Button-1> for manual-group selection, skipping Checkbutton."""
        if not isinstance(widget, tk.Checkbutton):
            widget.bind("<Button-1>", lambda e, p=path: self._manual_toggle_shift(e, p))
        for child in widget.winfo_children():
            self._bind_tile_select(child, path)

    def _load_thumbnail_async(self, path: Path, label: tk.Label, max_px: int,
                              grayscale: bool = False) -> None:
        """Queue a thumbnail for deferred loading (batched after page renders)."""
        self._pending_thumbs.append((path, label, max_px, grayscale))

    def _flush_pending_thumbs(self) -> None:
        """Spawn thumbnail-loading threads in staggered batches to avoid flooding."""
        if not self._pending_thumbs:
            return
        batch_id = self._thumb_batch_id
        pending = list(self._pending_thumbs)
        self._pending_thumbs.clear()
        self._drain_thumb_batch(pending, 0, batch_id)

    _THUMB_BATCH = 12   # thumbnails to start loading per tick

    def _drain_thumb_batch(self, items: list, offset: int, batch_id: int) -> None:
        """Start the next slice of thumbnail threads, then schedule the rest."""
        if batch_id != self._thumb_batch_id:
            return  # page changed — cancel
        end = min(offset + self._THUMB_BATCH, len(items))
        for i in range(offset, end):
            path, label, max_px, grayscale = items[i]
            self._spawn_thumb_thread(path, label, max_px, grayscale, batch_id)
        if end < len(items):
            try:
                self._canvas.after(16, lambda: self._drain_thumb_batch(items, end, batch_id))
            except Exception:
                pass

    def _spawn_thumb_thread(self, path: Path, label: tk.Label, max_px: int,
                            grayscale: bool, batch_id: int) -> None:
        """Start a single thumbnail-loading thread."""
        def _load() -> None:
            if not _PIL_AVAILABLE:
                return
            if batch_id != self._thumb_batch_id:
                return
            try:
                if not label.winfo_exists():
                    return
            except Exception:
                return
            with _THUMB_SEMAPHORE:
                if batch_id != self._thumb_batch_id:
                    return
                try:
                    with PILImage.open(path) as img:
                        img.thumbnail((max_px, max_px), PILImage.LANCZOS)
                        if grayscale:
                            img = img.convert("L").convert("RGB")
                        elif img.mode not in ("RGB", "RGBA"):
                            img = img.convert("RGB")
                        photo = ImageTk.PhotoImage(img)
                        self._photo_refs.append(photo)
                        try:
                            if label.winfo_exists():
                                def _set_img(p=photo, lbl=label):
                                    try:
                                        if lbl.winfo_exists():
                                            lbl.configure(image=p)
                                    except Exception:
                                        pass
                                label.after(0, _set_img)
                        except Exception:
                            pass
                except Exception:
                    try:
                        if label.winfo_exists():
                            def _set_err(lbl=label):
                                try:
                                    if lbl.winfo_exists():
                                        lbl.configure(text="[no preview]", fg=_M_TEXT3)
                                except Exception:
                                    pass
                            label.after(0, _set_err)
                    except Exception:
                        pass

        threading.Thread(target=_load, daemon=True).start()

    # ── group confirmation ────────────────────────────────────────────────────

    def _group_has_deselected(self, idx: int) -> bool:
        """Check if any preview image in the group is deselected."""
        if idx >= len(self._groups):
            return False
        grp = self._groups[idx]
        for img_idx in range(len(grp.previews)):
            key = (idx, "prev", img_idx)
            v = self._image_vars.get(key)
            if v is not None and not v.get():
                return True
        return False

    def _populate_calib_area(self, idx: int) -> None:
        """Fill the calibration button container for a single group (no page redraw)."""
        container = self._group_calib_containers.get(idx)
        if container is None or not container.winfo_exists():
            return
        # Clear existing content
        for w in container.winfo_children():
            w.destroy()

        has_deselected = self._group_has_deselected(idx)
        if has_deselected or idx in self._fp_calib_groups:
            if idx in self._fp_calib_groups:
                _mat_btn(container, "✗  Remove from Calibration",
                         lambda i=idx: self._toggle_fp_calib(i),
                         bg=_M_WARNING, fg="#FFFFFF", font_size=8,
                         ).pack(side=tk.RIGHT, padx=4)
            else:
                _mat_btn(container, "+  Add to Calibration",
                         lambda i=idx: self._toggle_fp_calib(i),
                         bg=_M_WARNING_TINT, fg=_M_WARNING, font_size=8,
                         ).pack(side=tk.RIGHT, padx=4)

    def _update_group_calib_area(self, idx: int) -> None:
        """Update just the calibration button + badge + border colour for one group."""
        self._populate_calib_area(idx)
        # Toggle "In calibration" badge
        badge = self._group_calib_badges.get(idx)
        if badge is not None:
            try:
                if not badge.winfo_exists():
                    badge = None
            except Exception:
                badge = None
        if badge is not None:
            if idx in self._fp_calib_groups:
                if not badge.winfo_ismapped():
                    badge.pack(side=tk.LEFT, padx=4)
            else:
                badge.pack_forget()
        # Update left-border colour to reflect calibration state
        border = self._group_border_frames.get(idx)
        if border and border.winfo_exists():
            if idx in self._fp_calib_groups:
                border.configure(bg=_M_WARNING)
            else:
                border.configure(bg=_M_PRIMARY)

    def _toggle_fp_calib(self, idx: int) -> None:
        """Toggle a group's membership in the false-positive calibration set."""
        if idx in self._fp_calib_groups:
            self._fp_calib_groups.discard(idx)
        else:
            self._fp_calib_groups.add(idx)
        self._update_fp_calib_btn()
        self._update_group_calib_area(idx)

    def _update_fp_calib_btn(self) -> None:
        """Show/hide the 'Calibrate False Positives' button in nav bar."""
        if self._fp_calib_groups:
            if not self._fp_calib_btn.winfo_ismapped():
                self._fp_calib_btn.pack(side=tk.LEFT, padx=(10, 0), pady=4)
        else:
            self._fp_calib_btn.pack_forget()

    def _refresh_page_keep_scroll(self) -> None:
        """Re-render the current page while preserving scroll position."""
        scroll_pos = self._canvas.yview()[0]
        self._render_page(self._current_page)
        self._canvas.after(10, lambda: self._canvas.yview_moveto(scroll_pos))

    # ── selection helpers ─────────────────────────────────────────────────────

    def _select_all(self) -> None:
        # Ensure vars exist for ALL groups (not just visited pages)
        for idx in range(len(self._groups)):
            self._ensure_group_vars(idx)
        for v in self._group_vars.values():
            v.set(True)
        for v in self._image_vars.values():
            v.set(True)
        # Update calibration buttons for visible groups only
        for idx in list(self._group_calib_containers):
            self._populate_calib_area(idx)

    def _select_none(self) -> None:
        # Ensure vars exist for ALL groups (not just visited pages)
        for idx in range(len(self._groups)):
            self._ensure_group_vars(idx)
        for v in self._group_vars.values():
            v.set(False)
        for v in self._image_vars.values():
            v.set(False)
        # Update calibration buttons for visible groups only
        for idx in list(self._group_calib_containers):
            self._populate_calib_area(idx)

    def _on_group_toggle(self, group_idx: int) -> None:
        checked = self._group_vars[group_idx].get()
        for key, var in self._image_vars.items():
            if key[0] == group_idx:
                var.set(checked)
        # Update only the calibration button for this group (no full redraw)
        self._update_group_calib_area(group_idx)

    # ── apply / revert ────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        """Collect checked-preview paths and move them to trash. Originals stay."""
        # Collect only previews from checked groups whose per-image checkbox is also checked.
        # Groups the user never viewed still have default state (all checked).
        paths_to_trash: list[Path] = []
        for idx, grp in enumerate(self._groups):
            g_var = self._group_vars.get(idx)
            # Uninitialized group → default is checked (include all previews)
            if g_var is not None and not g_var.get():
                continue
            for img_idx, rec in enumerate(grp.previews):
                key = (idx, "prev", img_idx)
                v = self._image_vars.get(key)
                # Uninitialized image var → default is checked
                if v is not None and not v.get():
                    continue
                paths_to_trash.append(rec.path)

        if not paths_to_trash:
            self._show_results_panel(0, 0, [],
                note="No duplicates selected for trashing.\n"
                     "Check at least one group to move its duplicates to trash.")
            return

        # Determine trash directory
        out = (self._settings.out_folder.strip() if self._settings else "") or ""
        if out:
            trash_dir = Path(out) / "trash"
        else:
            trash_dir = paths_to_trash[0].parent / "trash"

        dry = False  # Move Duplicates always performs the real move

        self._apply_btn.configure(state=tk.DISABLED)
        self._status_lbl.configure(text="Moving to trash…")
        self.update_idletasks()

        def _do() -> None:
            from mover import trash_files
            try:
                moved, errors = trash_files(paths_to_trash, trash_dir, dry_run=dry)
            except Exception as exc:
                moved, errors = 0, [str(exc)]
            kept = len(paths_to_trash) - moved - len(errors)
            # Record which paths were actually moved (or dry-run "moved")
            error_names = {e.split(":")[0] for e in errors}
            newly_trashed = {
                p for p in paths_to_trash
                if p.name not in error_names
            }
            # Notify main.py (e.g. for report generation / history update)
            if self._on_apply_cb:
                try:
                    self._on_apply_cb(paths_to_trash)
                except Exception:
                    pass
            self.after(0, lambda t=newly_trashed: self._on_apply_done(
                t, moved, kept, errors, trash_dir, dry))

        threading.Thread(target=_do, daemon=True).start()

    def _on_apply_done(
        self,
        newly_trashed: set,
        moved: int,
        kept: int,
        errors: list,
        trash_dir: Optional[Path],
        dry: bool,
    ) -> None:
        """Store applied state, re-render the review with status badges, then show results."""
        self._trashed_paths.update(newly_trashed)
        # Update ops log path so revert works against the log just written
        if trash_dir and not dry and moved > 0:
            from mover import ops_log_path as _ops_log_path_fn
            self._ops_log_path = _ops_log_path_fn(trash_dir.parent)
        # Hide Move Duplicates btn, show Revert btn
        if moved > 0 and not dry:
            self._apply_btn.pack_forget()
            self._show_revert_buttons()
        else:
            self._apply_btn.configure(state=tk.NORMAL)
        # Re-render so cards immediately reflect trashed state
        self._render_page(self._current_page)
        self._show_results_panel(moved, kept, errors, trash_dir=trash_dir, dry=dry,
                                 trashed_paths=list(newly_trashed))

    def _show_revert_buttons(self) -> None:
        """Reveal the revert buttons in the header bar (idempotent)."""
        if hasattr(self, "_revert_frame") and not self._revert_frame.winfo_ismapped():
            self._revert_frame.pack(side=tk.RIGHT, padx=(0, 4), pady=8)

    def _show_results_panel(
        self,
        moved: int,
        kept: int,
        errors: list,
        trash_dir: Optional[Path] = None,
        dry: bool = False,
        note: str = "",
        trashed_paths: Optional[list] = None,
    ) -> None:
        """Hide the canvas/nav and show an inline results card with a Back button."""
        self._status_lbl.configure(text="")

        # Hide scrollable area
        self._nav_bar_frame.pack_forget()
        self._canvas_container.pack_forget()

        self._results_frame = tk.Frame(self, bg=_M_BG)
        self._results_frame.pack(fill=tk.BOTH, expand=True)

        # ── Back bar ──────────────────────────────────────────────────────
        back_bar = tk.Frame(self._results_frame, bg=_M_SURFACE,
                            highlightthickness=0)
        back_bar.pack(fill=tk.X)
        _mat_btn(back_bar, "◀  Back to Review", self._restore_review,
                 "#455A64").pack(side=tk.LEFT, padx=10, pady=6)

        # ── Results card ──────────────────────────────────────────────────
        scroll_host = tk.Canvas(self._results_frame, bg=_M_BG, highlightthickness=0)
        scroll_host.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(scroll_host, bg=_M_BG)
        scroll_host.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>",
                   lambda _: scroll_host.configure(scrollregion=scroll_host.bbox("all")))

        card_wrap = tk.Frame(inner, bg=_M_BG)
        card_wrap.pack(fill=tk.X, padx=30, pady=24)

        card = tk.Frame(card_wrap, bg=_M_SURFACE,
                        highlightthickness=0,
                        padx=24, pady=20)
        card.pack(fill=tk.X)

        if note:
            title = note
            title_color = _M_TEXT2
        elif dry:
            title = "Dry Run — no files were moved"
            title_color = _M_WARNING
        elif errors and moved == 0:
            title = "Failed — no files moved"
            title_color = _M_ERROR
        else:
            title = "Done — duplicates moved to trash"
            title_color = _M_SUCCESS

        tk.Label(card, text=title,
                 font=("Segoe UI", 13, "bold"),
                 bg=_M_SURFACE, fg=title_color).pack(anchor=tk.W, pady=(0, 12))
        tk.Frame(card, height=1, bg=_M_DIVIDER).pack(fill=tk.X, pady=(0, 14))

        def _stat_row(label: str, value: str, fg: str = _M_TEXT1) -> None:
            row = tk.Frame(card, bg=_M_SURFACE)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, font=("Segoe UI", 9),
                     bg=_M_SURFACE, fg=_M_TEXT2, width=26, anchor=tk.W).pack(side=tk.LEFT)
            tk.Label(row, text=value, font=("Segoe UI", 9, "bold"),
                     bg=_M_SURFACE, fg=fg).pack(side=tk.LEFT)

        action = "Would move" if dry else "Moved to trash"
        _stat_row(f"{action}:", f"{moved} file{'s' if moved != 1 else ''}",
                  fg=_M_SUCCESS if moved > 0 else _M_TEXT2)
        if trash_dir:
            _stat_row("Trash folder:", str(trash_dir), fg=_M_TEXT2)
        if kept > 0:
            _stat_row("Kept (unchecked):", f"{kept} file{'s' if kept != 1 else ''}")
        if errors:
            _stat_row("Errors:", f"{len(errors)}", fg=_M_ERROR)
            tk.Frame(card, height=1, bg=_M_DIVIDER).pack(fill=tk.X, pady=(10, 6))
            for msg in errors[:15]:
                tk.Label(card, text=f"  • {msg}",
                         font=("Segoe UI", 8), bg=_M_SURFACE, fg=_M_ERROR,
                         anchor=tk.W).pack(fill=tk.X)

        # ── Expandable file list ───────────────────────────────────────────
        file_list = trashed_paths or []
        if file_list:
            tk.Frame(card, height=1, bg=_M_DIVIDER).pack(fill=tk.X, pady=(14, 8))
            list_frame = tk.Frame(card, bg=_M_SURFACE)
            _list_visible = [False]

            def _toggle_list():
                if _list_visible[0]:
                    for w in list_frame.winfo_children():
                        w.destroy()
                    list_frame.pack_forget()
                    toggle_btn.configure(text=f"▼  Show {len(file_list)} files moved")
                    _list_visible[0] = False
                else:
                    for p in file_list:
                        tk.Label(
                            list_frame,
                            text=f"  • {Path(p).name}",
                            font=("Consolas", 8), bg=_M_SURFACE, fg=_M_TEXT2,
                            anchor=tk.W,
                        ).pack(fill=tk.X, pady=1)
                    list_frame.pack(fill=tk.X, pady=(4, 0))
                    toggle_btn.configure(text=f"▲  Hide file list")
                    _list_visible[0] = True

            toggle_btn = _mat_btn(
                card,
                f"▼  Show {len(file_list)} files moved",
                _toggle_list,
                bg=_M_PRIMARY_TINT, fg=_M_PRIMARY, font_size=8,
            )
            toggle_btn.pack(anchor=tk.W)

    def _restore_review(self) -> None:
        """Destroy any inline panel and restore the scrollable review canvas."""
        for attr in ("_results_frame", "_calib_inline_frame"):
            frame = getattr(self, attr, None)
            if frame and frame.winfo_exists():
                frame.destroy()
        self._nav_bar_frame.pack(fill=tk.X)
        self._canvas_container.pack(fill=tk.BOTH, expand=True)
        self._render_page(self._current_page)
        # Re-enable apply button
        try:
            self._apply_btn.configure(state=tk.NORMAL)
        except Exception:
            pass

    def _on_revert_selected(self) -> None:
        if not self._ops_log_path:
            return
        # If the results panel is open, "selected" means all trashed groups from this apply
        results_open = (
            hasattr(self, "_results_frame") and self._results_frame.winfo_exists()
        )
        if results_open:
            # Revert groups that had any paths trashed in this session
            selected = [
                grp.group_id
                for grp in self._groups
                if any(rec.path in self._trashed_paths for rec in grp.previews)
            ]
        else:
            selected = [
                self._groups[idx].group_id
                for idx, var in self._group_vars.items() if var.get()
            ]
        if not selected:
            error_handler.show_warning(
                self, "Revert",
                user_msg="No groups are selected. Tick at least one group to revert.",
            )
            return
        self._do_revert(selected)

    def _on_revert_all(self) -> None:
        if not self._ops_log_path:
            return
        if not messagebox.askyesno("Revert All",
                                   "Move all files back to their original locations?",
                                   parent=self):
            return
        self._do_revert(None)

    def _do_revert(self, group_ids) -> None:
        self._status_lbl.config(text="Reverting…")
        self.update_idletasks()

        def _worker() -> None:
            from mover import revert_operations
            reverted, errors = revert_operations(self._ops_log_path, group_ids)
            msg = f"Reverted {reverted} file{'s' if reverted != 1 else ''}."
            if errors:
                msg += f" ({errors} error{'s' if errors != 1 else ''})"
            self.after(0, lambda: self._on_revert_done(msg, group_ids))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_revert_done(self, msg: str, group_ids) -> None:
        """After revert: clear trashed state for the reverted paths and re-render."""
        if group_ids is None:
            # Revert all — clear everything
            self._trashed_paths.clear()
        else:
            # Revert selected groups — remove their preview paths from trashed set
            reverted_ids = set(group_ids)
            for idx, grp in enumerate(self._groups):
                if getattr(grp, "group_id", None) in reverted_ids:
                    for rec in grp.previews:
                        self._trashed_paths.discard(rec.path)
        self._status_lbl.config(text=msg)
        # Re-show Move Duplicates button
        try:
            if not self._apply_btn.winfo_ismapped():
                self._apply_btn.pack(side=tk.RIGHT, padx=(4, 12), pady=8)
            self._apply_btn.configure(state=tk.NORMAL)
        except Exception:
            pass
        # If we're on the results panel, go back to review; otherwise just re-render
        results_open = (
            hasattr(self, "_results_frame")
            and self._results_frame.winfo_exists()
        )
        if results_open:
            self._restore_review()
        else:
            self._render_page(self._current_page)

    # ── calibrate from review ─────────────────────────────────────────────────

    def _show_calibration_info(self) -> None:
        """Build calibration data and show the calibration panel inline."""
        has_checked   = any(v.get() for v in self._group_vars.values())
        has_unchecked = any(not v.get() for v in self._group_vars.values())
        has_manual    = any(v.get() for v in self._manual_group_vars.values())
        has_solo      = any(v.get() for v in self._solo_vars.values())
        if not has_checked and not has_unchecked and not has_manual and not has_solo:
            error_handler.show_info(
                self, "Nothing to calibrate",
                user_msg=(
                    "No groups or images are selected for calibration.\n\n"
                    "Check some scan groups, manual groups, or unique images first."
                ),
            )
            return

        # Create temp calibration folder
        base = Path(tempfile.mkdtemp(prefix="deduper_review_calib_"))
        groups_dir  = base / "groups"
        neg_dir     = base / "negatives"
        singles_dir = base / "singles"
        groups_dir.mkdir()
        neg_dir.mkdir()
        singles_dir.mkdir()

        errors: list[str] = []

        def _link(src: Path, dst: Path) -> None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except (OSError, NotImplementedError, PermissionError):
                try:
                    shutil.copy2(src, dst)
                except Exception as e:
                    errors.append(f"{src.name}: {e}")

        for idx, g_var in self._group_vars.items():
            if idx >= len(self._groups):
                continue
            grp = self._groups[idx]
            all_recs = grp.originals + grp.previews

            if g_var.get():
                confirmed_paths: list[Path] = []
                skipped_paths:   list[Path] = []
                for img_idx, rec in enumerate(grp.originals):
                    key = (idx, "orig", img_idx)
                    if self._image_vars.get(key, _TRUE_STUB).get():
                        confirmed_paths.append(rec.path)
                    else:
                        skipped_paths.append(rec.path)
                for img_idx, rec in enumerate(grp.previews):
                    key = (idx, "prev", img_idx)
                    if self._image_vars.get(key, _TRUE_STUB).get():
                        confirmed_paths.append(rec.path)
                    else:
                        skipped_paths.append(rec.path)

                if len(confirmed_paths) >= 2:
                    gdir = groups_dir / f"g{idx:04d}"
                    gdir.mkdir(exist_ok=True)
                    for p in confirmed_paths:
                        _link(p, gdir / p.name)
                elif len(confirmed_paths) == 1:
                    _link(confirmed_paths[0], singles_dir / confirmed_paths[0].name)
                for p in skipped_paths:
                    _link(p, singles_dir / p.name)
            else:
                ndir = neg_dir / f"n{idx:04d}"
                ndir.mkdir(exist_ok=True)
                for rec in all_recs:
                    _link(rec.path, ndir / rec.path.name)

        for gi, paths in enumerate(self._manual_calib_groups):
            if not self._manual_group_vars.get(gi, _TRUE_STUB).get():
                continue
            if len(paths) < 2:
                continue
            gdir = groups_dir / f"manual_{gi:04d}"
            gdir.mkdir(exist_ok=True)
            for p in paths:
                _link(p, gdir / p.name)

        for img_idx, rec in enumerate(self._solo_originals):
            if self._solo_vars.get(img_idx, tk.BooleanVar(value=False)).get():
                _link(rec.path, singles_dir / rec.path.name)

        if errors:
            error_handler.show_warning(
                self, "Some files skipped",
                user_msg=f"{len(errors)} file(s) could not be prepared for calibration.",
                detail="\n".join(errors[:10]),
            )

        n_groups = sum(1 for d in groups_dir.iterdir() if d.is_dir())
        n_negs   = sum(1 for d in neg_dir.iterdir() if d.is_dir())
        if n_groups == 0 and n_negs == 0:
            error_handler.show_info(
                self, "Not enough data",
                user_msg=(
                    "Need at least one confirmed group or one wrong-match group to calibrate.\n\n"
                    "Check some groups as correct or uncheck wrongly matched groups first."
                ),
            )
            shutil.rmtree(base, ignore_errors=True)
            return

        # Build inline calibration panel
        from calibration_window import CalibrationPanel
        from config import Settings

        calib_settings = copy.deepcopy(self._settings) if self._settings else Settings()
        calib_settings.calib_folder = str(base)

        def _apply_cb(threshold: int, preview_ratio: float) -> None:
            pass  # main.py handles settings persistence via calibration_applied_cb

        # Hide scrollable area, show inline panel
        self._nav_bar_frame.pack_forget()
        self._canvas_container.pack_forget()

        self._calib_inline_frame = tk.Frame(self, bg=_M_BG)
        self._calib_inline_frame.pack(fill=tk.BOTH, expand=True)

        # Back bar
        back_bar = tk.Frame(self._calib_inline_frame, bg=_M_SURFACE,
                            highlightthickness=0)
        back_bar.pack(fill=tk.X)
        _mat_btn(back_bar, "◀  Back to Review", self._restore_review,
                 "#455A64").pack(side=tk.LEFT, padx=10, pady=6)
        tk.Label(back_bar,
                 text=f"Dataset: {n_groups} group folder{'s' if n_groups != 1 else ''}",
                 font=("Segoe UI", 8), bg=_M_SURFACE, fg=_M_TEXT2,
                 ).pack(side=tk.LEFT, padx=12)

        # Embed calibration panel (starts on "Run" tab since data is pre-loaded)
        panel = CalibrationPanel(
            self._calib_inline_frame,
            calib_settings,
            apply_cb=_apply_cb,
            start_on_run_tab=True,
        )
        panel.pack(fill=tk.BOTH, expand=True)

    # ── false-positive calibration popup ────────────────────────────────────

    def _run_fp_calibration(self) -> None:
        """Open false-positive calibration popup."""
        if not self._fp_calib_groups:
            return

        win = tk.Toplevel(self)
        win.title("Calibrate False Positives")
        win.geometry("500x400")
        win.resizable(False, False)
        win.configure(bg=_M_BG)
        win.transient(self.winfo_toplevel())
        win.grab_set()

        # Summary
        n_groups = len(self._fp_calib_groups)
        n_fp = 0
        for idx in self._fp_calib_groups:
            if idx >= len(self._groups):
                continue
            grp = self._groups[idx]
            for img_idx in range(len(grp.previews)):
                key = (idx, "prev", img_idx)
                if not self._image_vars.get(key, _TRUE_STUB).get():
                    n_fp += 1

        tk.Label(win, text="False-Positive Calibration",
                 font=("Segoe UI", 14, "bold"), bg=_M_BG, fg=_M_TEXT1,
                 ).pack(padx=20, pady=(20, 4))
        tk.Label(win, text=f"{n_groups} group{'s' if n_groups != 1 else ''}  \u00b7  {n_fp} false-positive image{'s' if n_fp != 1 else ''}",
                 font=("Segoe UI", 9), bg=_M_BG, fg=_M_TEXT2,
                 ).pack(padx=20, pady=(0, 16))

        tk.Frame(win, height=1, bg=_M_DIVIDER).pack(fill=tk.X, padx=20)

        # Description
        tk.Label(win, text=(
            "This will compute pHash distances between false-positive images\n"
            "and the originals, then suggest a tighter threshold that avoids\n"
            "these wrong matches.\n\n"
            "Selected images = correctly matched duplicates\n"
            "Deselected images = false positives (not real duplicates)"
        ), font=("Segoe UI", 9), bg=_M_BG, fg=_M_TEXT2,
                 justify=tk.LEFT, anchor=tk.W,
                 ).pack(padx=20, pady=16, anchor=tk.W)

        # Progress area
        progress_frame = tk.Frame(win, bg=_M_BG)
        progress_frame.pack(fill=tk.X, padx=20, pady=(0, 8))
        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(progress_frame, variable=progress_var, maximum=100)
        progress_bar.pack(fill=tk.X, pady=4)
        progress_lbl = tk.Label(progress_frame, text="Ready", bg=_M_BG, fg=_M_TEXT2,
                                font=("Segoe UI", 9))
        progress_lbl.pack()

        # Result area (hidden until calibration done)
        result_frame = tk.Frame(win, bg=_M_BG)

        # Buttons
        btn_frame = tk.Frame(win, bg=_M_BG)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=20, pady=16)

        cancel_btn = _mat_btn(btn_frame, "Cancel", win.destroy, "#455A64")
        cancel_btn.pack(side=tk.RIGHT, padx=4)

        interrupted = [False]

        def _do_calibrate():
            # Disable start, show interrupt
            start_btn.pack_forget()
            cancel_btn.pack_forget()
            interrupt_btn = _mat_btn(btn_frame, "Interrupt", lambda: interrupted.__setitem__(0, True), _M_ERROR)
            interrupt_btn.pack(side=tk.RIGHT, padx=4)

            current_threshold = self._settings.threshold if self._settings else 2

            def _worker():
                fp_distances = []
                total_pairs = 0

                # Count total work
                for idx in self._fp_calib_groups:
                    if idx >= len(self._groups):
                        continue
                    grp = self._groups[idx]
                    n_orig = len(grp.originals)
                    n_fp_in_grp = sum(
                        1 for i in range(len(grp.previews))
                        if not self._image_vars.get((idx, "prev", i), _TRUE_STUB).get()
                    )
                    total_pairs += n_orig * n_fp_in_grp

                if total_pairs == 0:
                    self.after(0, lambda: _show_result(win, result_frame, progress_lbl, btn_frame,
                                                        None, current_threshold, interrupted))
                    return

                done = 0
                for idx in self._fp_calib_groups:
                    if interrupted[0]:
                        break
                    if idx >= len(self._groups):
                        continue
                    grp = self._groups[idx]
                    for img_idx, rec in enumerate(grp.previews):
                        if interrupted[0]:
                            break
                        key = (idx, "prev", img_idx)
                        if self._image_vars.get(key, _TRUE_STUB).get():
                            continue  # This is a correctly matched image, skip
                        # This is a false positive — compute distance to originals
                        for orig in grp.originals:
                            if interrupted[0]:
                                break
                            try:
                                dist = rec.phash - orig.phash
                                fp_distances.append(dist)
                            except Exception:
                                pass
                            done += 1
                            pct = done / max(total_pairs, 1) * 100
                            self.after(0, lambda p=pct, d=done, t=total_pairs:
                                       _update_progress(progress_var, progress_lbl, p, d, t))

                if interrupted[0]:
                    self.after(0, lambda: _on_interrupted(win, progress_lbl, btn_frame))
                    return

                min_fp = min(fp_distances) if fp_distances else None
                self.after(0, lambda: _show_result(win, result_frame, progress_lbl, btn_frame,
                                                    min_fp, current_threshold, interrupted))

            threading.Thread(target=_worker, daemon=True).start()

        def _update_progress(pvar, plbl, pct, done, total):
            pvar.set(pct)
            plbl.configure(text=f"Computing distances... {done}/{total} pairs")

        def _on_interrupted(w, plbl, bf):
            plbl.configure(text="Calibration interrupted \u2014 no changes applied.")
            for child in bf.winfo_children():
                child.destroy()
            _mat_btn(bf, "Close", w.destroy, "#455A64").pack(side=tk.RIGHT, padx=4)

        def _show_result(w, rf, plbl, bf, min_fp_dist, current_thr, interrupted_flag):
            progress_var.set(100)

            for child in bf.winfo_children():
                child.destroy()

            rf.pack(fill=tk.X, padx=20, pady=8)

            if min_fp_dist is None:
                plbl.configure(text="No false-positive pairs found.")
                tk.Label(rf, text="Could not compute distances. Check that groups have originals.",
                         font=("Segoe UI", 9), bg=_M_BG, fg=_M_TEXT2).pack(anchor=tk.W)
                _mat_btn(bf, "Close", w.destroy, "#455A64").pack(side=tk.RIGHT, padx=4)
                return

            suggested = max(1, min_fp_dist - 1)
            plbl.configure(text="Calibration complete.")

            tk.Label(rf, text=f"Minimum false-positive pHash distance:  {min_fp_dist}",
                     font=("Segoe UI", 10, "bold"), bg=_M_BG, fg=_M_TEXT1).pack(anchor=tk.W, pady=2)
            tk.Label(rf, text=f"Current threshold:  {current_thr}",
                     font=("Segoe UI", 9), bg=_M_BG, fg=_M_TEXT2).pack(anchor=tk.W)

            if suggested < current_thr:
                tk.Label(rf, text=f"Recommended threshold:  {suggested}",
                         font=("Segoe UI", 10, "bold"), bg=_M_BG, fg=_M_SUCCESS).pack(anchor=tk.W, pady=4)
                tk.Label(rf, text="Lowering the threshold will prevent these false-positive matches.",
                         font=("Segoe UI", 9), bg=_M_BG, fg=_M_TEXT2).pack(anchor=tk.W)

                def _apply_and_close():
                    if self._settings:
                        self._settings.threshold = suggested
                        try:
                            from pathlib import Path as _P
                            from config import save_settings
                            save_settings(self._settings, _P(__file__).parent / "settings.json")
                        except Exception:
                            pass
                    self._fp_calib_groups.clear()
                    self._update_fp_calib_btn()
                    w.destroy()
                    self._refresh_page_keep_scroll()

                _mat_btn(bf, "Apply & Close", _apply_and_close, _RV_BTN_SUCCESS).pack(side=tk.RIGHT, padx=4)
            else:
                tk.Label(rf, text="Current threshold is already optimal for these images.",
                         font=("Segoe UI", 10, "bold"), bg=_M_BG, fg=_M_PRIMARY).pack(anchor=tk.W, pady=4)

            _mat_btn(bf, "Close", w.destroy, "#455A64").pack(side=tk.RIGHT, padx=4)

        start_btn = _mat_btn(btn_frame, "Start Calibration", _do_calibrate, _RV_BTN_PRIMARY)
        start_btn.pack(side=tk.RIGHT, padx=4)

    # ── manual calibration groups section ────────────────────────────────────

    def _build_manual_group_card(self, mg_idx: int) -> None:
        """Render a proper group card for a manually-created calibration group."""
        paths = self._manual_calib_groups[mg_idx]
        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE, highlightthickness=0)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=4, bg=_M_MANUAL).pack(side=tk.LEFT, fill=tk.Y)
        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Header
        head = tk.Frame(card, bg=_M_MANUAL_TINT, pady=0)
        head.pack(fill=tk.X)

        g_var = self._manual_group_vars.get(mg_idx)
        if g_var is None:
            g_var = tk.BooleanVar(value=True)
            self._manual_group_vars[mg_idx] = g_var

        _mcb_kw = {}
        if self._cb_unchecked and self._cb_checked:
            _mcb_kw = dict(image=self._cb_unchecked, selectimage=self._cb_checked,
                           indicatoron=False, bd=0, relief=tk.FLAT, selectcolor=_M_MANUAL_TINT)
        else:
            _mcb_kw = dict(selectcolor=_M_SUCCESS)
        tk.Checkbutton(
            head, variable=g_var, bg=_M_MANUAL_TINT,
            activebackground=_M_MANUAL_TINT,
            **_mcb_kw,
        ).pack(side=tk.LEFT, padx=(10, 0), pady=8)

        tk.Label(
            head,
            text=f"Manual Group #{mg_idx + 1}  ·  {len(paths)} image{'s' if len(paths) != 1 else ''}",
            font=("Segoe UI", 9, "bold"), bg=_M_MANUAL_TINT, fg=_M_MANUAL,
        ).pack(side=tk.LEFT, padx=8, pady=8)

        tk.Label(
            head, text=" MANUAL ",
            font=("Segoe UI", 8, "bold"), bg=_M_MANUAL, fg="#FFFFFF",
            padx=6, pady=2,
        ).pack(side=tk.LEFT, padx=4)

        tk.Label(
            head, text="↗ calibration only",
            font=("Segoe UI", 8), bg=_M_MANUAL_TINT, fg=_M_TEXT2,
        ).pack(side=tk.LEFT, padx=4)

        _mat_btn(
            head, "✕  Remove Group",
            lambda i=mg_idx: self._manual_remove_group(i),
            bg=_M_ERROR_TINT, fg=_M_ERROR, font_size=8,
        ).pack(side=tk.RIGHT, padx=8, pady=6)

        # Image tiles
        img_frame = tk.Frame(card, bg=_M_MANUAL_TINT, padx=10, pady=8)
        img_frame.pack(fill=tk.X)

        COLS = 4
        for i, path in enumerate(paths):
            rec = self._path_to_record(path)
            if rec is not None:
                dummy_var = tk.BooleanVar(value=True)
                tile = self._build_image_tile(img_frame, rec, dummy_var, i % COLS, i // COLS,
                                              bg=_M_MANUAL_TINT)
            else:
                tile = tk.Frame(img_frame, bg=_M_MANUAL_TINT, padx=4, pady=4)
                tile.grid(row=i // COLS, column=i % COLS, padx=4, pady=4, sticky=tk.NW)
                lbl = tk.Label(tile, bg=_M_MANUAL_TINT,
                              image=self._get_placeholder(_THUMB_SIZE, _M_MANUAL_TINT))
                lbl.pack()
                self._load_thumbnail_async(path, lbl, _THUMB_SIZE)
                tk.Label(
                    tile,
                    text=(path.name[:15] + "…" if len(path.name) > 16 else path.name),
                    font=("Segoe UI", 8), bg=_M_MANUAL_TINT, fg=_M_TEXT2,
                ).pack()
            # Per-image remove button → sends image to Unsorted
            _mat_btn(tile, "↩ Unsorted",
                     lambda p=path, gi=mg_idx: self._remove_image_from_manual_group(gi, p),
                     bg=_M_WARNING_TINT, fg=_M_WARNING, font_size=7).pack(pady=(2, 0))

    def _update_manual_sel_count(self) -> None:
        n = len(self._manual_selected_paths)
        if hasattr(self, "_manual_sel_lbl") and self._manual_sel_lbl.winfo_exists():
            self._manual_sel_lbl.configure(
                text=f"{n} selected",
                fg=_M_MANUAL if n > 0 else _M_TEXT3,
            )

    def _manual_toggle(self, path: Path) -> None:
        cell = self._manual_tile_frames.get(path)
        if cell is None or not cell.winfo_exists():
            return
        if path in self._manual_selected_paths:
            self._manual_selected_paths.discard(path)
            cell.configure(highlightbackground=cell.cget("bg"), highlightthickness=0)
        else:
            self._manual_selected_paths.add(path)
            cell.configure(highlightbackground=_M_MANUAL, highlightthickness=3)
        self._update_manual_sel_count()

    def _manual_toggle_shift(self, event: tk.Event, path: Path) -> None:
        """Handle click with optional Shift for range selection in the solo section."""
        shift_held = bool(event.state & 0x0001)
        if shift_held and self._last_solo_click_path is not None and self._solo_visible_paths:
            try:
                i1 = self._solo_visible_paths.index(self._last_solo_click_path)
                i2 = self._solo_visible_paths.index(path)
            except ValueError:
                # Fallback to normal toggle if path not in visible list
                self._manual_toggle(path)
                self._last_solo_click_path = path
                return
            lo, hi = min(i1, i2), max(i1, i2)
            for p in self._solo_visible_paths[lo:hi + 1]:
                if p not in self._manual_selected_paths:
                    self._manual_selected_paths.add(p)
                    cell = self._manual_tile_frames.get(p)
                    if cell and cell.winfo_exists():
                        cell.configure(highlightbackground=_M_MANUAL, highlightthickness=3)
            self._update_manual_sel_count()
        else:
            self._manual_toggle(path)
            self._last_solo_click_path = path

    def _remove_image_from_manual_group(self, mg_idx: int, path: Path) -> None:
        """Remove a single image from a manual group; it goes to the Unsorted pool."""
        if mg_idx >= len(self._manual_calib_groups):
            return
        group = self._manual_calib_groups[mg_idx]
        if path not in group:
            return
        group.remove(path)
        self._unsorted_paths.append(path)
        # path stays in _manual_used_paths (now it's in unsorted, not solo)
        if len(group) < 2:
            # Group too small — dissolve it, send remaining to unsorted too
            for p in list(group):
                self._unsorted_paths.append(p)
            self._manual_calib_groups.pop(mg_idx)
        self._reinit_manual_vars()
        self._render_page(self._current_page)

    def _return_unsorted_to_solo(self, path: Path) -> None:
        """Return an unsorted image back to the solo section."""
        if path in self._unsorted_paths:
            self._unsorted_paths.remove(path)
            self._manual_used_paths.discard(path)
            self._render_page(self._current_page)

    def _build_unsorted_section(self) -> None:
        """Card showing images removed from manual groups — staging area before re-grouping."""
        if not self._unsorted_paths:
            return

        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE, highlightthickness=0)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=4, bg=_M_WARNING).pack(side=tk.LEFT, fill=tk.Y)
        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head = tk.Frame(card, bg=_M_WARNING_TINT, pady=0)
        head.pack(fill=tk.X)
        tk.Label(
            head,
            text=f"Unsorted  ({len(self._unsorted_paths)} image{'s' if len(self._unsorted_paths) != 1 else ''})",
            font=("Segoe UI", 9, "bold"), bg=_M_WARNING_TINT, fg=_M_WARNING,
        ).pack(side=tk.LEFT, padx=12, pady=8)
        tk.Label(
            head, text="Images removed from manual groups — return to Unique images to re-group",
            font=("Segoe UI", 8), bg=_M_WARNING_TINT, fg=_M_TEXT2,
        ).pack(side=tk.LEFT, padx=4)

        grid_frame = tk.Frame(card, bg=_M_WARNING_TINT, padx=10, pady=8)
        grid_frame.pack(fill=tk.X)

        COLS = 5
        for i, path in enumerate(self._unsorted_paths):
            rec = self._path_to_record(path)
            cell = tk.Frame(grid_frame, bg=_M_WARNING_TINT, padx=4, pady=4)
            cell.grid(row=i // COLS, column=i % COLS, padx=4, pady=4, sticky=tk.NW)

            lbl = tk.Label(cell, bg=_M_WARNING_TINT,
                          image=self._get_placeholder(_THUMB_SIZE, _M_WARNING_TINT))
            lbl.pack()
            self._load_thumbnail_async(path, lbl, _THUMB_SIZE)

            if rec is not None:
                tk.Label(cell, text=f"{rec.width}×{rec.height}  {rec.size_label()}",
                         font=("Segoe UI", 8), bg=_M_WARNING_TINT, fg=_M_TEXT2).pack()

            fname = path.name
            tk.Label(
                cell,
                text=(fname[:14] + "…" if len(fname) > 15 else fname),
                font=("Segoe UI", 8), bg=_M_WARNING_TINT, fg=_M_TEXT1,
            ).pack()

            _mat_btn(cell, "↩ Return to Unique",
                     lambda p=path: self._return_unsorted_to_solo(p),
                     bg=_M_SOLO_TINT, fg=_M_SOLO_BORDER, font_size=7).pack(pady=(3, 0))

    def _manual_create_group(self) -> None:
        if len(self._manual_selected_paths) < 2:
            if hasattr(self, "_manual_sel_lbl") and self._manual_sel_lbl.winfo_exists():
                self._manual_sel_lbl.configure(
                    text="Select at least 2 images first", fg=_M_ERROR)
            return
        paths = list(self._manual_selected_paths)
        self._manual_calib_groups.append(paths)
        self._manual_used_paths.update(paths)
        self._manual_selected_paths.clear()
        self._reinit_manual_vars()
        self._render_page(self._current_page)

    def _manual_remove_group(self, idx: int) -> None:
        if 0 <= idx < len(self._manual_calib_groups):
            paths = self._manual_calib_groups.pop(idx)
            # Send all images in the removed group to Unsorted
            still_in_other_groups = {p for g in self._manual_calib_groups for p in g}
            for p in paths:
                if p not in still_in_other_groups and p not in self._unsorted_paths:
                    self._unsorted_paths.append(p)
                    # Keep in _manual_used_paths since it's now in unsorted
            self._reinit_manual_vars()
            self._render_page(self._current_page)

    # ── manual trash selection ────────────────────────────────────────────────

    def _bind_trash_select(self, widget: tk.Widget, path: Path) -> None:
        """Recursively bind <Button-1> for manual trash selection, skipping Checkbutton."""
        if not isinstance(widget, tk.Checkbutton):
            widget.bind("<Button-1>", lambda e, p=path: self._manual_trash_toggle_shift(e, p))
        for child in widget.winfo_children():
            self._bind_trash_select(child, path)

    def _manual_trash_toggle_shift(self, event: tk.Event, path: Path) -> None:
        """Handle click with optional Shift for range selection."""
        shift_held = bool(event.state & 0x0001)
        if shift_held and self._last_solo_click_path is not None and self._solo_visible_paths:
            try:
                i1 = self._solo_visible_paths.index(self._last_solo_click_path)
                i2 = self._solo_visible_paths.index(path)
            except ValueError:
                self._manual_trash_toggle(path)
                self._last_solo_click_path = path
                return
            lo, hi = min(i1, i2), max(i1, i2)
            for p in self._solo_visible_paths[lo:hi + 1]:
                if p not in self._manual_trash_selected:
                    self._manual_trash_selected.add(p)
                    cell = self._manual_trash_tile_frames.get(p)
                    if cell and cell.winfo_exists():
                        cell.configure(highlightbackground=_M_ERROR, highlightthickness=3)
            self._update_trash_sel_count()
        else:
            self._manual_trash_toggle(path)
            self._last_solo_click_path = path

    def _manual_trash_toggle(self, path: Path) -> None:
        """Toggle a path's selection state for manual trash."""
        cell = self._manual_trash_tile_frames.get(path)
        if path in self._manual_trash_selected:
            self._manual_trash_selected.discard(path)
            if cell and cell.winfo_exists():
                cell.configure(highlightthickness=0)
        else:
            self._manual_trash_selected.add(path)
            if cell and cell.winfo_exists():
                cell.configure(highlightbackground=_M_ERROR, highlightthickness=3)
        self._update_trash_sel_count()

    def _update_trash_sel_count(self) -> None:
        n = len(self._manual_trash_selected)
        if hasattr(self, "_trash_sel_count_lbl") and self._trash_sel_count_lbl.winfo_exists():
            self._trash_sel_count_lbl.configure(
                text=f"{n} selected",
                fg=_M_ERROR if n > 0 else _M_TEXT3,
            )
        if hasattr(self, "_trash_sel_btn") and self._trash_sel_btn.winfo_exists():
            self._trash_sel_btn.configure(state=tk.NORMAL if n > 0 else tk.DISABLED)

    def _on_trash_selected(self) -> None:
        """Move manually-selected images (from originals/solo) to trash."""
        if not self._manual_trash_selected:
            return
        paths = list(self._manual_trash_selected)

        out = (self._settings.out_folder.strip() if self._settings else "") or ""
        trash_dir = Path(out) / "trash" if out else paths[0].parent / "trash"
        dry = bool(self._settings.dry_run) if self._settings else False

        self._trash_sel_btn.configure(state=tk.DISABLED)
        self._status_lbl.configure(text="Moving selected to trash…")
        self.update_idletasks()

        def _do() -> None:
            from mover import trash_files
            try:
                moved, errors = trash_files(paths, trash_dir, dry_run=dry)
            except Exception as exc:
                moved, errors = 0, [str(exc)]
            # Build items list for the duplicates card
            items: list[dict] = []
            error_names = {e.split(":")[0] for e in errors}
            for p in paths:
                if p.name not in error_names:
                    tp = trash_dir / p.name
                    rec = self._path_to_record(p)
                    items.append({"original": p, "trash": tp, "rec": rec})
            self.after(0, lambda: self._on_trash_selected_done(items, moved, errors, trash_dir, dry))

        threading.Thread(target=_do, daemon=True).start()

    def _on_trash_selected_done(
        self,
        items: list[dict],
        moved: int,
        errors: list,
        trash_dir: Path,
        dry: bool,
    ) -> None:
        self._manual_trashed_items.extend(items)
        newly = {it["original"] for it in items}
        self._manual_trash_selected -= newly
        self._trashed_paths.update(newly)
        if trash_dir and not dry and moved > 0:
            from mover import ops_log_path as _ops_log_path_fn
            self._ops_log_path = _ops_log_path_fn(trash_dir.parent)
            self._show_revert_buttons()
        msg = f"Moved {moved} file{'s' if moved != 1 else ''} to trash."
        if errors:
            msg += f"  ({len(errors)} error{'s' if len(errors) != 1 else ''})"
        if dry:
            msg = f"Dry run: would move {moved} file{'s' if moved != 1 else ''}."
        self._status_lbl.configure(text=msg)
        self._render_page(self._current_page)
        self._update_trash_sel_count()

    def _on_revert_manual_item(self, item: dict) -> None:
        """Move a single manually-trashed file back to its original location."""
        original: Path = item["original"]
        trash_path: Path = item["trash"]
        if not trash_path.exists():
            error_handler.show_warning(
                self, "Revert",
                user_msg=f"'{trash_path.name}' could not be found in the trash folder.\nIt may have already been moved or deleted.",
                detail=f"Expected path: {trash_path}",
            )
            return
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trash_path), str(original))
            if item in self._manual_trashed_items:
                self._manual_trashed_items.remove(item)
            self._trashed_paths.discard(original)
            self._status_lbl.configure(text=f"Reverted: {original.name}")
            self._render_page(self._current_page)
        except Exception as exc:
            error_handler.show_error(
                self, "Revert Failed",
                user_msg="Could not move the file back to its original location.\nCheck that you have permission to write to the destination folder.",
                detail=str(exc),
                exc=exc,
            )

    def _build_manual_duplicates_section(self) -> None:
        """Card showing images manually moved to trash, with per-item revert."""
        if not self._manual_trashed_items:
            return

        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE, highlightthickness=0)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=4, bg=_M_ERROR).pack(side=tk.LEFT, fill=tk.Y)
        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head = tk.Frame(card, bg=_M_ERROR_TINT, pady=0)
        head.pack(fill=tk.X)
        tk.Label(
            head,
            text=f"Duplicates  ({len(self._manual_trashed_items)} manually trashed)",
            font=("Segoe UI", 9, "bold"), bg=_M_ERROR_TINT, fg=_M_ERROR,
        ).pack(side=tk.LEFT, padx=12, pady=8)
        tk.Label(
            head, text="Images you manually moved to trash — click Revert to restore",
            font=("Segoe UI", 8), bg=_M_ERROR_TINT, fg=_M_TEXT3,
        ).pack(side=tk.LEFT, padx=4)

        grid_frame = tk.Frame(card, bg=_M_ERROR_TINT, padx=10, pady=8)
        grid_frame.pack(fill=tk.X)

        COLS = 5
        for i, item in enumerate(self._manual_trashed_items):
            path: Path = item["original"]
            trash_path: Path = item["trash"]
            rec: Optional[ImageRecord] = item["rec"]

            cell = tk.Frame(grid_frame, bg=_M_ERROR_TINT, padx=4, pady=4)
            cell.grid(row=i // COLS, column=i % COLS, padx=4, pady=4, sticky=tk.NW)

            thumb_lbl = tk.Label(cell, bg=_M_ERROR_TINT,
                                image=self._get_placeholder(_THUMB_SIZE, _M_ERROR_TINT))
            thumb_lbl.pack()
            self._load_thumbnail_async(trash_path if trash_path.exists() else path,
                                       thumb_lbl, _THUMB_SIZE, grayscale=True)

            tk.Label(cell, text="🗑  Trashed",
                     font=("Segoe UI", 8, "bold"), bg=_M_ERROR_TINT, fg="#757575",
                     ).pack(pady=(2, 0))

            fname = path.name
            tk.Label(
                cell,
                text=(fname[:15] + "…" if len(fname) > 16 else fname),
                font=("Segoe UI", 8), bg=_M_ERROR_TINT, fg=_M_TEXT2,
                wraplength=_THUMB_SIZE,
            ).pack()
            if rec is not None:
                tk.Label(cell, text=f"{rec.width}×{rec.height}  {rec.size_label()}",
                         font=("Segoe UI", 8), bg=_M_ERROR_TINT, fg=_M_TEXT3).pack()

            _mat_btn(cell, "↩ Revert",
                     lambda it=item: self._on_revert_manual_item(it),
                     bg=_M_WARNING_TINT, fg=_M_WARNING, font_size=7).pack(pady=(3, 0))

    # ── var initialisation (runs once, preserves state across page changes) ──────

    def _init_vars(self) -> None:
        """Pre-create BooleanVars for solo images only.
        Group/image vars are created lazily by _ensure_group_vars()."""
        for img_idx in range(len(self._solo_originals)):
            if img_idx not in self._solo_vars:
                self._solo_vars[img_idx] = tk.BooleanVar(value=False)

    def _ensure_group_vars(self, idx: int) -> None:
        """Lazily create BooleanVars for a single group (idempotent)."""
        if idx in self._group_vars:
            return  # already initialised
        grp = self._groups[idx]
        self._group_vars[idx] = tk.BooleanVar(value=True)
        self._group_status[idx] = ""
        for img_idx in range(len(grp.originals)):
            self._image_vars[(idx, "orig", img_idx)] = tk.BooleanVar(value=True)
        for img_idx in range(len(grp.previews)):
            self._image_vars[(idx, "prev", img_idx)] = tk.BooleanVar(value=True)

    def _reinit_manual_vars(self) -> None:
        """Rebuild per-manual-group BooleanVars after the group list changes."""
        new_vars: dict[int, tk.BooleanVar] = {}
        for i in range(len(self._manual_calib_groups)):
            new_vars[i] = self._manual_group_vars.get(i, tk.BooleanVar(value=True))
        self._manual_group_vars = new_vars

    def _path_to_record(self, path: Path) -> Optional[ImageRecord]:
        """Look up an ImageRecord by path from all groups + solo originals."""
        for g in self._groups:
            for r in g.originals + g.previews:
                if r.path == path:
                    return r
        for r in self._solo_originals:
            if r.path == path:
                return r
        return None

    # ── pagination ────────────────────────────────────────────────────────────

    def _total_pages(self) -> int:
        import math
        group_pages = max(1, math.ceil(len(self._groups) / self._page_size)) if self._groups else 1
        # Unique images get their own dedicated page when present
        extra = 1 if self._solo_originals else 0
        return group_pages + extra

    def _unique_page_index(self) -> int:
        """Return the page index of the dedicated Unique Images page, or -1."""
        if not self._solo_originals:
            return -1
        import math
        return max(1, math.ceil(len(self._groups) / self._page_size)) if self._groups else 1

    def _is_unique_page(self, page: int) -> bool:
        return self._solo_originals and page == self._unique_page_index()

    def _render_page(self, page: int) -> None:
        """Destroy current page widgets and build the requested page."""
        # Cancel pending thumbnails
        self._thumb_batch_id += 1
        self._pending_thumbs.clear()

        # Remove accumulated var traces from previous page (prevents leaks)
        for var, tid in self._active_traces:
            try:
                var.trace_remove("write", tid)
            except Exception:
                pass
        self._active_traces.clear()
        # Clear photo refs from previous page to free memory
        self._photo_refs.clear()
        # Clear old widgets and per-page widget refs
        for widget in self._inner_frame.winfo_children():
            widget.destroy()
        self._group_border_frames.clear()
        self._group_calib_containers.clear()
        self._group_calib_badges.clear()
        self._manual_trash_tile_frames.clear()

        self._current_page = max(0, min(page, self._total_pages() - 1))

        if self._is_unique_page(self._current_page):
            self._build_solo_section()
        else:
            if not self._groups:
                tk.Label(
                    self._inner_frame, text="No duplicate groups found.",
                    font=("Segoe UI", 13), bg=_M_BG, fg=_M_TEXT3,
                ).pack(pady=40)
            else:
                start = self._current_page * self._page_size
                end   = min(start + self._page_size, len(self._groups))
                for idx in range(start, end):
                    try:
                        self._build_group_card(idx, self._groups[idx])
                    except Exception:
                        import traceback
                        traceback.print_exc()

            # Manual groups / broken shown on last duplicate-groups page
            last_dup_page = (self._unique_page_index() - 1
                             if self._solo_originals
                             else self._total_pages() - 1)
            if self._current_page >= last_dup_page:
                self._build_manual_duplicates_section()
                for mg_idx in range(len(self._manual_calib_groups)):
                    self._build_manual_group_card(mg_idx)
                self._build_unsorted_section()
                if self._broken_files:
                    self._build_broken_section()

        # Finalise: force geometry calculation so bbox covers all packed widgets,
        # then set scroll region, scroll to top, bind mousewheel
        self._inner_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all") or (0, 0, 0, 0))
        self._canvas.yview_moveto(0)
        self._update_page_nav()
        self._bind_mousewheel_recursive(self._inner_frame)
        # Safety net: re-check scrollregion after a brief delay in case
        # late geometry events change the frame size (e.g. thumbnail loading)
        self._canvas.after(200, self._on_frame_configure)
        # Load thumbnails after the layout is stable
        self._flush_pending_thumbs()

    def _update_page_nav(self) -> None:
        total  = self._total_pages()
        page   = self._current_page
        n      = len(self._groups)
        start  = page * self._page_size + 1

        # Enable / disable Prev & Next buttons
        if hasattr(self, "_prev_btn") and self._prev_btn.winfo_exists():
            if page > 0:
                _mat_enable(self._prev_btn)
            else:
                _mat_disable(self._prev_btn)
        if hasattr(self, "_next_btn") and self._next_btn.winfo_exists():
            if page < total - 1:
                _mat_enable(self._next_btn)
            else:
                _mat_disable(self._next_btn)

        # Highlight the unique button when on the unique page
        if hasattr(self, "_unique_btn") and self._unique_btn.winfo_exists():
            on_unique = self._is_unique_page(page)
            self._unique_btn.configure(
                bg=_M_SOLO_BORDER if on_unique else _M_SURFACE,
                fg="#FFFFFF" if on_unique else _M_SOLO_BORDER,
            )

        if self._is_unique_page(page):
            self._page_info_var.set(
                f"Unique Images  ·  {len(self._solo_originals)} files  ·  page {page + 1} of {total}")
        elif n > 0:
            end = min((page + 1) * self._page_size, n)
            self._page_info_var.set(
                f"Page {page + 1} of {total}  ·  groups {start}–{end} of {n}")
        else:
            self._page_info_var.set("")

    def _prev_page(self) -> None:
        if self._current_page > 0:
            self._render_page(self._current_page - 1)

    def _next_page(self) -> None:
        if self._current_page < self._total_pages() - 1:
            self._render_page(self._current_page + 1)

    def _on_page_size_change(self, *_) -> None:
        try:
            new_size = max(1, int(self._page_size_var.get()))
        except ValueError:
            return
        # Keep the first visible group on screen after resize
        first_group = self._current_page * self._page_size
        self._page_size = new_size
        self._render_page(first_group // new_size)
        # Persist the choice for next launch
        if self._settings and hasattr(self._settings, "report_page_size"):
            self._settings.report_page_size = new_size
            try:
                from pathlib import Path as _P
                from config import save_settings
                _sp = _P(__file__).parent / "settings.json"
                save_settings(self._settings, _sp)
            except Exception:
                pass

    # ── scroll helpers ────────────────────────────────────────────────────────

    def _bind_mousewheel_recursive(self, widget: tk.Widget) -> None:
        """Bind mousewheel scroll to widget and all descendants (iterative, non-blocking)."""
        _skip = (ttk.Scrollbar, ttk.Scale, ttk.Combobox)
        stack = [widget]
        handler = self._on_mousewheel
        while stack:
            w = stack.pop()
            try:
                if not isinstance(w, _skip):
                    w.bind("<MouseWheel>", handler)
                stack.extend(w.winfo_children())
            except Exception:
                pass

    def _on_mousewheel(self, event: tk.Event) -> None:
        # yscrollincrement=1 → each unit = 1 pixel; scroll ~100 px per notch
        px = int(-1 * (event.delta / 120) * 100)
        self._canvas.yview_scroll(px, "units")

    def _on_frame_configure(self, _event=None) -> None:
        # Update scrollregion only when the frame actually changed size
        # (placeholder images keep layout stable so this rarely fires after initial render)
        bbox = self._canvas.bbox("all")
        if bbox:
            self._canvas.configure(scrollregion=bbox)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._canvas.itemconfig(self._canvas_window, width=event.width)
