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


# ── Material Design colour palette ────────────────────────────────────────────

_M_BG           = "#F5F5F5"   # Grey 100 – window background
_M_SURFACE      = "#FFFFFF"   # Card surface
_M_PRIMARY      = "#1565C0"   # Blue 800
_M_PRIMARY_DARK = "#0D47A1"   # Blue 900
_M_PRIMARY_TINT = "#E3F2FD"   # Blue 50
_M_SUCCESS      = "#2E7D32"   # Green 800
_M_SUCCESS_TINT = "#E8F5E9"   # Green 50
_M_ERROR        = "#C62828"   # Red 800
_M_ERROR_TINT   = "#FFEBEE"   # Red 50
_M_WARNING      = "#E65100"   # Deep Orange 900
_M_WARNING_TINT = "#FFF3E0"   # Orange 50
_M_PURPLE       = "#6A1B9A"   # Purple 800 (series)
_M_DIVIDER      = "#E0E0E0"   # Grey 300
_M_TEXT1        = "#212121"   # Grey 900
_M_TEXT2        = "#616161"   # Grey 700
_M_TEXT3        = "#9E9E9E"   # Grey 500
_M_SOLO_TINT    = "#E1F5FE"   # Light Blue 50
_M_SOLO_BORDER  = "#0288D1"   # Light Blue 700
_M_BROKEN_TINT  = "#FCE4EC"   # Pink 50
_M_BROKEN_BDR   = "#AD1457"   # Pink 800
_M_MANUAL      = "#5C6BC0"   # Indigo 400
_M_MANUAL_TINT = "#E8EAF6"   # Indigo 50
_M_MANUAL_HDR  = "#EDE7F6"   # Purple 50
_M_MANUAL_DARK = "#4527A0"   # Deep Purple 800

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
    _mat_btn(win, "Close", win.destroy, _M_PRIMARY).pack(pady=10)


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
    def _darken(event):
        btn.configure(bg=_darken_color(bg))
    def _restore(event):
        btn.configure(bg=bg)
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=_darken_color(bg), activeforeground=fg,
        relief=tk.FLAT, bd=0, padx=12, pady=5,
        font=("Segoe UI", font_size, "bold"),
        cursor="hand2", **kw,
    )
    btn.bind("<Enter>", _darken)
    btn.bind("<Leave>", _restore)
    return btn


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

class ReportViewer(tk.Toplevel):
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
    ) -> None:
        super().__init__(parent)
        self.title("Image Deduper — Review Results")
        self.geometry("1160x800")
        self.minsize(800, 520)
        self.configure(bg=_M_BG)

        self._groups          = groups
        self._ops_log_path    = ops_log_path
        self._on_apply_cb     = on_apply_cb
        self._solo_originals  = solo_originals or []
        self._broken_files    = broken_files or []
        self._settings        = settings

        # Photo reference storage (prevent GC)
        self._photo_refs: list = []

        # Per-group: include checkbox, status, border-frame ref
        # (pre-created for ALL groups so page changes don't lose state)
        self._group_vars: dict[int, tk.BooleanVar] = {}
        self._group_status: dict[int, str] = {}       # "confirmed" | "wrong" | ""
        self._group_border_frames: dict[int, tk.Frame] = {}  # only current page

        # Per-image: belongs-to-group checkbox  (group_idx, role, img_idx) → BoolVar
        self._image_vars: dict[tuple, tk.BooleanVar] = {}

        # Solo original checkboxes
        self._solo_vars: dict[int, tk.BooleanVar] = {}

        # Manual calibration groups (list of path lists, created by user in the review)
        self._manual_calib_groups: list[list[Path]] = []
        self._manual_group_vars: dict[int, tk.BooleanVar] = {}    # per-manual-group include checkbox
        self._manual_used_paths: set[Path] = set()                 # paths assigned to manual groups
        self._manual_selected_paths: set[Path] = set()             # currently selected tile paths
        self._manual_tile_frames: dict[Path, tk.Frame] = {}        # path → tile frame

        # Pagination
        self._current_page: int = 0
        self._page_size: int = 20

        self._build_ui()
        self.lift()
        self.focus_set()

        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Up>",    lambda _: self._scroll(-3))
        self.bind("<Down>",  lambda _: self._scroll(3))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._setup_style()

        # ── Header bar ────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=_M_PRIMARY)
        hdr.pack(fill=tk.X)

        tk.Label(
            hdr, text="Review Scan Results",
            font=("Segoe UI", 14, "bold"), bg=_M_PRIMARY, fg="#FFFFFF",
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
            font=("Segoe UI", 9), bg=_M_PRIMARY, fg="#BBDEFB",
        ).pack(side=tk.LEFT, padx=4)

        _mat_btn(hdr, "Select None", self._select_none, _M_PRIMARY_DARK,
                 ).pack(side=tk.RIGHT, padx=6, pady=8)
        _mat_btn(hdr, "Select All", self._select_all, _M_PRIMARY_DARK,
                 ).pack(side=tk.RIGHT, padx=2, pady=8)

        # ── Action bar ────────────────────────────────────────────────────
        act = tk.Frame(self, bg=_M_SURFACE, pady=6,
                       highlightbackground=_M_DIVIDER, highlightthickness=1)
        act.pack(fill=tk.X)

        self._apply_btn = _mat_btn(act, "▶  Apply Selected", self._on_apply, _M_PRIMARY)
        self._apply_btn.pack(side=tk.LEFT, padx=(12, 4))

        if self._ops_log_path and self._ops_log_path.exists():
            _mat_btn(act, "↩  Revert Selected", self._on_revert_selected,
                     "#455A64").pack(side=tk.LEFT, padx=4)
            _mat_btn(act, "↩  Revert All", self._on_revert_all,
                     "#455A64").pack(side=tk.LEFT, padx=4)

        # Calibrate from Review
        tk.Frame(act, width=1, bg=_M_DIVIDER).pack(side=tk.LEFT, fill=tk.Y,
                                                    padx=12, pady=4)
        _mat_btn(act, "⚙  Calibrate from Review",
                 self._show_calibration_info, "#5C6BC0").pack(side=tk.LEFT, padx=4)
        _info_btn(act, "calibrate_review", bg=_M_SURFACE).pack(side=tk.LEFT, padx=0)

        # Manual group creation (only shown when there are solo originals)
        if self._solo_originals:
            tk.Frame(act, width=1, bg=_M_DIVIDER).pack(side=tk.LEFT, fill=tk.Y,
                                                        padx=8, pady=4)
            self._manual_sel_lbl = tk.Label(
                act, text="0 selected",
                font=("Segoe UI", 8), bg=_M_SURFACE, fg=_M_TEXT3,
            )
            self._manual_sel_lbl.pack(side=tk.LEFT, padx=(4, 2))
            _mat_btn(act, "+  Create Manual Group",
                     self._manual_create_group, _M_MANUAL,
                     font_size=8).pack(side=tk.LEFT, padx=4)

        self._status_lbl = tk.Label(act, text="", bg=_M_SURFACE,
                                    fg=_M_TEXT2, font=("Segoe UI", 9))
        self._status_lbl.pack(side=tk.RIGHT, padx=12)

        # ── Pagination nav bar ────────────────────────────────────────────
        nav = tk.Frame(self, bg=_M_SURFACE,
                       highlightbackground=_M_DIVIDER, highlightthickness=1)
        nav.pack(fill=tk.X)

        self._page_info_var = tk.StringVar(value="")
        tk.Label(nav, textvariable=self._page_info_var,
                 bg=_M_SURFACE, fg=_M_TEXT2,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=14)

        # Per-page size selector (right side)
        tk.Label(nav, text="Groups per page:",
                 bg=_M_SURFACE, fg=_M_TEXT2,
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=(0, 4))
        self._page_size_var = tk.StringVar(value=str(self._page_size))
        ps_cb = ttk.Combobox(nav, textvariable=self._page_size_var,
                             values=["10", "20", "50", "100"],
                             width=5, state="readonly")
        ps_cb.pack(side=tk.RIGHT, padx=(0, 12))
        self._page_size_var.trace_add("write", self._on_page_size_change)

        # ── Scrollable canvas ─────────────────────────────────────────────
        container = tk.Frame(self, bg=_M_BG)
        container.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(container, bg=_M_BG, highlightthickness=0)
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

        # Scroll: use bind_all only while mouse is inside the canvas area so
        # that popups / calibration window don't also scroll this canvas.
        self._canvas.bind("<Enter>", self._on_canvas_enter)
        self._canvas.bind("<Leave>", self._on_canvas_leave)

        # Pre-create ALL checkbox vars so state persists across page changes
        self._init_vars()
        # Render the first page
        self._render_page(0)

    def _setup_style(self) -> None:
        style = ttk.Style()
        try:
            style.configure("TScrollbar", troughcolor=_M_BG, background=_M_PRIMARY)
        except Exception:
            pass

    # ── group card ────────────────────────────────────────────────────────────

    def _build_group_card(self, idx: int, group: DuplicateGroup) -> None:
        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)
        self._group_frames[idx] = outer

        # Card with left-colour border
        card_wrap = tk.Frame(outer, bg=_M_SURFACE,
                             highlightbackground=_M_DIVIDER, highlightthickness=1)
        card_wrap.pack(fill=tk.X)

        left_border = tk.Frame(card_wrap, width=5, bg=_M_PRIMARY)
        left_border.pack(side=tk.LEFT, fill=tk.Y)
        self._group_border_frames[idx] = left_border

        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Header ──────────────────────────────────────────────────────
        head = tk.Frame(card, bg=_M_PRIMARY_TINT, pady=0)
        head.pack(fill=tk.X)

        g_var = self._group_vars[idx]  # pre-created by _init_vars; preserves state across pages
        tk.Checkbutton(
            head, variable=g_var, bg=_M_PRIMARY_TINT,
            command=lambda i=idx: self._on_group_toggle(i),
            activebackground=_M_PRIMARY_TINT,
        ).pack(side=tk.LEFT, padx=(10, 0), pady=8)

        n_orig = len(group.originals)
        n_prev = len(group.previews)
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

        # Action buttons (right side of header)
        btn_frame = tk.Frame(head, bg=_M_PRIMARY_TINT)
        btn_frame.pack(side=tk.RIGHT, padx=8, pady=6)

        _info_btn(btn_frame, "wrong_group", bg=_M_PRIMARY_TINT).pack(
            side=tk.RIGHT, padx=0)
        _mat_btn(
            btn_frame, "✗  Wrong Group",
            lambda i=idx: self._on_wrong_group(i),
            bg="#FFEBEE", fg=_M_ERROR, font_size=8,
        ).pack(side=tk.RIGHT, padx=4)

        _info_btn(btn_frame, "same_image", bg=_M_PRIMARY_TINT).pack(
            side=tk.RIGHT, padx=0)
        _mat_btn(
            btn_frame, "✓  Same Image",
            lambda i=idx: self._on_confirm_group(i),
            bg=_M_SUCCESS_TINT, fg=_M_SUCCESS, font_size=8,
        ).pack(side=tk.RIGHT, padx=4)

        # ── Body: originals | separator | previews ───────────────────────
        body = tk.Frame(card, bg=_M_SURFACE)
        body.pack(fill=tk.X)

        orig_col = tk.Frame(body, bg=_M_SUCCESS_TINT, padx=10, pady=8)
        orig_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            orig_col, text="Originals  →  results/",
            font=("Segoe UI", 8, "bold"), bg=_M_SUCCESS_TINT, fg=_M_SUCCESS,
        ).pack(anchor=tk.W, pady=(0, 6))

        orig_grid = tk.Frame(orig_col, bg=_M_SUCCESS_TINT)
        orig_grid.pack(fill=tk.X)
        for img_idx, rec in enumerate(group.originals):
            key = (idx, "orig", img_idx)
            v = self._image_vars[key]  # pre-created by _init_vars
            self._build_image_tile(orig_grid, rec, v, img_idx % 3, img_idx // 3,
                                   bg=_M_SUCCESS_TINT)

        tk.Frame(body, width=1, bg=_M_DIVIDER).pack(side=tk.LEFT, fill=tk.Y)

        prev_col = tk.Frame(body, bg=_M_ERROR_TINT, padx=10, pady=8)
        prev_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            prev_col, text="Duplicates to trash  →  trash/",
            font=("Segoe UI", 8, "bold"), bg=_M_ERROR_TINT, fg=_M_ERROR,
        ).pack(anchor=tk.W, pady=(0, 6))

        prev_grid = tk.Frame(prev_col, bg=_M_ERROR_TINT)
        prev_grid.pack(fill=tk.X)
        for img_idx, rec in enumerate(group.previews):
            key = (idx, "prev", img_idx)
            v = self._image_vars[key]  # pre-created by _init_vars
            self._build_image_tile(prev_grid, rec, v, img_idx % 4, img_idx // 4,
                                   bg=_M_ERROR_TINT, max_thumb=120)

    # ── solo & broken sections ────────────────────────────────────────────────

    def _build_solo_section(self) -> None:
        visible = [r for r in self._solo_originals if r.path not in self._manual_used_paths]
        if not visible:
            return

        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE,
                             highlightbackground=_M_SOLO_BORDER, highlightthickness=1)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=5, bg=_M_SOLO_BORDER).pack(side=tk.LEFT, fill=tk.Y)
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
        # Rebuild tile frame dict so selection clicks work from this section
        self._manual_tile_frames = {}
        col = 0
        row = 0
        for img_idx, rec in enumerate(self._solo_originals):
            if rec.path in self._manual_used_paths:
                continue
            v = self._solo_vars[img_idx]
            tile = self._build_image_tile(grid_frame, rec, v, col, row, bg=_M_SOLO_TINT)
            self._manual_tile_frames[rec.path] = tile
            # Bind click on tile and non-interactive children for manual group selection
            self._bind_tile_select(tile, rec.path)
            col += 1
            if col >= 5:
                col = 0
                row += 1

    def _build_broken_section(self) -> None:
        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE,
                             highlightbackground=_M_BROKEN_BDR, highlightthickness=1)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=5, bg=_M_BROKEN_BDR).pack(side=tk.LEFT, fill=tk.Y)
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
    ) -> None:
        tile = tk.Frame(parent, bg=bg, padx=4, pady=4)
        tile.grid(row=row, column=col, padx=4, pady=4, sticky=tk.NW)

        # Thumbnail
        thumb_lbl = tk.Label(tile, bg=bg)
        thumb_lbl.pack()
        self._load_thumbnail_async(rec.path, thumb_lbl, max_thumb)

        # Checkbox: "part of this group" (unchecked = different image)
        cb_frame = tk.Frame(tile, bg=bg)
        cb_frame.pack(fill=tk.X)

        cb = tk.Checkbutton(
            cb_frame, variable=var,
            bg=bg, activebackground=bg,
            command=lambda lbl=None, v=var: self._update_tile_label(v, lbl),
        )
        cb.pack(side=tk.LEFT)

        fname = rec.path.name
        fname_short = fname[:15] + "…" if len(fname) > 16 else fname
        tk.Label(
            cb_frame, text=fname_short,
            font=("Segoe UI", 7), bg=bg, fg=_M_TEXT1,
            wraplength=max_thumb,
        ).pack(side=tk.LEFT)

        _info_btn(cb_frame, "different_image", bg=bg).pack(side=tk.LEFT, padx=0)

        tk.Label(
            tile, text=f"{rec.width}×{rec.height}  {rec.size_label()}",
            font=("Segoe UI", 7), bg=bg, fg=_M_TEXT2,
        ).pack()
        tk.Label(
            tile, text=rec.date_label(),
            font=("Segoe UI", 7), bg=bg, fg=_M_TEXT3,
        ).pack()

        # "Different image" badge (shown when unchecked)
        diff_badge = tk.Label(
            tile, text="≠ different image",
            font=("Segoe UI", 7, "italic"), bg=bg, fg=_M_WARNING,
        )
        # Patch checkbox to also toggle badge
        def _toggle_badge(*_):
            if var.get():
                diff_badge.pack_forget()
            else:
                diff_badge.pack(pady=(0, 2))
        var.trace_add("write", _toggle_badge)
        return tile

    def _update_tile_label(self, var: tk.BooleanVar, lbl) -> None:
        pass  # handled by trace

    def _bind_tile_select(self, widget: tk.Widget, path: Path) -> None:
        """Recursively bind <Button-1> for manual-group selection, skipping Checkbutton."""
        if not isinstance(widget, tk.Checkbutton):
            widget.bind("<Button-1>", lambda e, p=path: self._manual_toggle(p))
        for child in widget.winfo_children():
            self._bind_tile_select(child, path)

    def _load_thumbnail_async(self, path: Path, label: tk.Label, max_px: int) -> None:
        def _load() -> None:
            if not _PIL_AVAILABLE:
                return
            with _THUMB_SEMAPHORE:
                try:
                    with PILImage.open(path) as img:
                        img.thumbnail((max_px, max_px), PILImage.LANCZOS)
                        if img.mode not in ("RGB", "RGBA"):
                            img = img.convert("RGB")
                        photo = ImageTk.PhotoImage(img)
                        self._photo_refs.append(photo)
                        label.after(0, lambda p=photo: label.configure(image=p))
                except Exception:
                    label.after(0, lambda: label.configure(
                        text="[no preview]", fg=_M_TEXT3))

        threading.Thread(target=_load, daemon=True).start()

    # ── group confirmation ────────────────────────────────────────────────────

    def _on_confirm_group(self, idx: int) -> None:
        """Toggle 'Same Image' confirmation status for a group."""
        current = self._group_status.get(idx, "")
        new_status = "" if current == "confirmed" else "confirmed"
        self._group_status[idx] = new_status
        self._group_vars[idx].set(True)
        self._on_group_toggle(idx)
        color = _M_SUCCESS if new_status == "confirmed" else _M_PRIMARY
        if idx in self._group_border_frames:
            self._group_border_frames[idx].configure(bg=color)

    def _on_wrong_group(self, idx: int) -> None:
        """Toggle 'Wrong Group' status for a group."""
        current = self._group_status.get(idx, "")
        new_status = "" if current == "wrong" else "wrong"
        self._group_status[idx] = new_status
        if new_status == "wrong":
            self._group_vars[idx].set(False)
            self._on_group_toggle(idx)
        color = _M_ERROR if new_status == "wrong" else _M_PRIMARY
        if idx in self._group_border_frames:
            self._group_border_frames[idx].configure(bg=color)

    # ── selection helpers ─────────────────────────────────────────────────────

    def _select_all(self) -> None:
        for v in self._group_vars.values():
            v.set(True)
        for v in self._image_vars.values():
            v.set(True)

    def _select_none(self) -> None:
        for v in self._group_vars.values():
            v.set(False)
        for v in self._image_vars.values():
            v.set(False)

    def _on_group_toggle(self, group_idx: int) -> None:
        checked = self._group_vars[group_idx].get()
        for key, var in self._image_vars.items():
            if key[0] == group_idx:
                var.set(checked)

    # ── apply / revert ────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        if self._on_apply_cb is None:
            messagebox.showinfo("Apply", "No apply callback configured.", parent=self)
            return
        selected = [
            self._groups[idx].group_id
            for idx, var in self._group_vars.items() if var.get()
        ]
        if not selected:
            messagebox.showwarning("Apply", "No groups selected.", parent=self)
            return
        groups_to_apply = [g for g in self._groups if g.group_id in selected]
        self._apply_btn.config(state=tk.DISABLED)
        self._status_lbl.config(text="Applying…")
        self.update_idletasks()

        def _do() -> None:
            try:
                self._on_apply_cb(groups_to_apply)
                self.after(0, lambda: self._status_lbl.config(
                    text=f"Applied {len(groups_to_apply)} groups."))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Error", str(exc), parent=self))
            finally:
                self.after(0, lambda: self._apply_btn.config(state=tk.NORMAL))

        threading.Thread(target=_do, daemon=True).start()

    def _on_revert_selected(self) -> None:
        if not self._ops_log_path:
            return
        selected = [
            self._groups[idx].group_id
            for idx, var in self._group_vars.items() if var.get()
        ]
        if not selected:
            messagebox.showwarning("Revert", "No groups selected.", parent=self)
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
            msg = f"Reverted {reverted} files."
            if errors:
                msg += f" ({errors} errors)"
            self.after(0, lambda: self._status_lbl.config(text=msg))

        threading.Thread(target=_worker, daemon=True).start()

    # ── calibrate from review ─────────────────────────────────────────────────

    def _show_calibration_info(self) -> None:
        """Show instruction popup then open calibration with data from review."""
        win = tk.Toplevel(self)
        win.title("Calibrate from Review")
        win.geometry("540x380")
        win.grab_set()
        win.resizable(False, False)
        win.configure(bg=_M_SURFACE)

        tk.Label(
            win, text="Calibrate from your review",
            font=("Segoe UI", 13, "bold"), bg=_M_SURFACE, fg=_M_TEXT1,
        ).pack(anchor=tk.W, padx=20, pady=(18, 6))

        tk.Frame(win, height=1, bg=_M_DIVIDER).pack(fill=tk.X, padx=20, pady=(0, 10))

        instructions = (
            "Before clicking 'Start Calibration', make sure your review is correct:\n\n"
            "  ✓  Groups with checkbox CHECKED  →  treated as confirmed duplicates\n"
            "        (used as positive examples in calibration)\n\n"
            "  ✗  Groups with checkbox UNCHECKED  →  treated as wrong matches\n"
            "        (used as negative examples — photos that should NOT be grouped)\n\n"
            "  ✗  Images unchecked within a group  →  treated as unrelated singles\n"
            "        (will not be placed in any calibration group)\n\n"
            "You can also use the '✓ Same Image' and '✗ Wrong Group' buttons on each\n"
            "card to quickly set group status before running calibration.\n\n"
            "Calibration will run 3 rounds and find the best threshold + ratio for\n"
            "your photo library based on your manual corrections."
        )
        txt = tk.Text(win, wrap=tk.WORD, padx=14, pady=6, relief=tk.FLAT,
                      bg=_M_SURFACE, fg=_M_TEXT2, font=("Segoe UI", 9), height=14)
        txt.insert("1.0", instructions)
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True, padx=8)

        btn_row = tk.Frame(win, bg=_M_SURFACE)
        btn_row.pack(fill=tk.X, padx=16, pady=12)
        _mat_btn(btn_row, "Cancel", win.destroy, "#757575").pack(side=tk.RIGHT, padx=4)
        _mat_btn(
            btn_row, "Start Calibration ▶",
            lambda: (win.destroy(), self._run_review_calibration()),
            _M_PRIMARY,
        ).pack(side=tk.RIGHT, padx=4)

    def _run_review_calibration(self) -> None:
        """Build calibration data from review choices and open calibration window."""
        has_checked   = any(v.get() for v in self._group_vars.values())
        has_unchecked = any(not v.get() for v in self._group_vars.values())
        has_manual    = any(v.get() for v in self._manual_group_vars.values())
        has_solo      = any(v.get() for v in self._solo_vars.values())
        if not has_checked and not has_unchecked and not has_manual and not has_solo:
            messagebox.showinfo(
                "Nothing to calibrate",
                "No groups or images are selected for calibration.\n\n"
                "Check some scan groups, manual groups, or unique images first.",
                parent=self,
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
                # Confirmed group: images with checkbox True → calibration group
                confirmed_paths: list[Path] = []
                skipped_paths:   list[Path] = []
                for img_idx, rec in enumerate(grp.originals):
                    key = (idx, "orig", img_idx)
                    if self._image_vars.get(key, tk.BooleanVar(value=True)).get():
                        confirmed_paths.append(rec.path)
                    else:
                        skipped_paths.append(rec.path)
                for img_idx, rec in enumerate(grp.previews):
                    key = (idx, "prev", img_idx)
                    if self._image_vars.get(key, tk.BooleanVar(value=True)).get():
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

                # Unchecked images within the group → singles
                for p in skipped_paths:
                    _link(p, singles_dir / p.name)
            else:
                # Wrong-match group: all images → negatives
                ndir = neg_dir / f"n{idx:04d}"
                ndir.mkdir(exist_ok=True)
                for rec in all_recs:
                    _link(rec.path, ndir / rec.path.name)

        if errors:
            messagebox.showwarning(
                "Some files skipped",
                f"{len(errors)} file(s) could not be linked/copied:\n\n"
                + "\n".join(errors[:10]),
                parent=self,
            )

        # Add manually created calibration groups (only if their checkbox is checked)
        for gi, paths in enumerate(self._manual_calib_groups):
            if not self._manual_group_vars.get(gi, tk.BooleanVar(value=True)).get():
                continue
            if len(paths) < 2:
                continue
            gdir = groups_dir / f"manual_{gi:04d}"
            gdir.mkdir(exist_ok=True)
            for p in paths:
                _link(p, gdir / p.name)

        # Solo originals checked in the solo section → include as calibration singles
        for img_idx, rec in enumerate(self._solo_originals):
            if self._solo_vars.get(img_idx, tk.BooleanVar(value=False)).get():
                _link(rec.path, singles_dir / rec.path.name)

        # Check if we have anything useful
        n_groups = sum(1 for d in groups_dir.iterdir() if d.is_dir())
        n_negs   = sum(1 for d in neg_dir.iterdir() if d.is_dir())
        if n_groups == 0 and n_negs == 0:
            messagebox.showinfo(
                "Not enough data",
                "Need at least one confirmed group or one wrong-match group to calibrate.\n\n"
                "Check some groups as correct or uncheck wrongly matched groups first.",
                parent=self,
            )
            shutil.rmtree(base, ignore_errors=True)
            return

        # Open calibration window with the temp folder
        from calibration_window import CalibrationWindow
        from config import Settings

        calib_settings = copy.deepcopy(self._settings) if self._settings else Settings()
        calib_settings.calib_folder = str(base)

        def _apply_cb(threshold: int, preview_ratio: float) -> None:
            pass  # caller (main.py) handles applying via folder_cb/calibration_applied_cb

        CalibrationWindow(
            self,
            calib_settings,
            apply_cb=_apply_cb,
        )

    # ── manual calibration groups section ────────────────────────────────────

    def _build_manual_group_card(self, mg_idx: int) -> None:
        """Render a proper group card for a manually-created calibration group."""
        paths = self._manual_calib_groups[mg_idx]
        outer = tk.Frame(self._inner_frame, bg=_M_BG, pady=5, padx=12)
        outer.pack(fill=tk.X)

        card_wrap = tk.Frame(outer, bg=_M_SURFACE,
                             highlightbackground=_M_MANUAL, highlightthickness=1)
        card_wrap.pack(fill=tk.X)

        tk.Frame(card_wrap, width=5, bg=_M_MANUAL).pack(side=tk.LEFT, fill=tk.Y)
        card = tk.Frame(card_wrap, bg=_M_SURFACE)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Header
        head = tk.Frame(card, bg=_M_MANUAL_TINT, pady=0)
        head.pack(fill=tk.X)

        g_var = self._manual_group_vars.get(mg_idx)
        if g_var is None:
            g_var = tk.BooleanVar(value=True)
            self._manual_group_vars[mg_idx] = g_var

        tk.Checkbutton(
            head, variable=g_var, bg=_M_MANUAL_TINT,
            activebackground=_M_MANUAL_TINT,
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
                self._build_image_tile(img_frame, rec, dummy_var, i % COLS, i // COLS,
                                       bg=_M_MANUAL_TINT)
            else:
                cell = tk.Frame(img_frame, bg=_M_MANUAL_TINT, padx=4, pady=4)
                cell.grid(row=i // COLS, column=i % COLS, padx=4, pady=4, sticky=tk.NW)
                lbl = tk.Label(cell, bg=_M_MANUAL_TINT)
                lbl.pack()
                self._load_thumbnail_async(path, lbl, _THUMB_SIZE)
                tk.Label(
                    cell,
                    text=(path.name[:15] + "…" if len(path.name) > 16 else path.name),
                    font=("Segoe UI", 7), bg=_M_MANUAL_TINT, fg=_M_TEXT2,
                ).pack()

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
        n = len(self._manual_selected_paths)
        if hasattr(self, "_manual_sel_lbl") and self._manual_sel_lbl.winfo_exists():
            self._manual_sel_lbl.configure(
                text=f"{n} selected",
                fg=_M_MANUAL if n > 0 else _M_TEXT3,
            )

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
            # Restore paths to solo section if not used in any remaining manual group
            still_used = {p for g in self._manual_calib_groups for p in g}
            for p in paths:
                if p not in still_used:
                    self._manual_used_paths.discard(p)
            self._reinit_manual_vars()
            self._render_page(self._current_page)

    # ── var initialisation (runs once, preserves state across page changes) ──────

    def _init_vars(self) -> None:
        """Pre-create every BooleanVar for all groups and solo images."""
        for idx, grp in enumerate(self._groups):
            if idx not in self._group_vars:
                self._group_vars[idx] = tk.BooleanVar(value=True)
            if idx not in self._group_status:
                self._group_status[idx] = ""
            for img_idx in range(len(grp.originals)):
                key = (idx, "orig", img_idx)
                if key not in self._image_vars:
                    self._image_vars[key] = tk.BooleanVar(value=True)
            for img_idx in range(len(grp.previews)):
                key = (idx, "prev", img_idx)
                if key not in self._image_vars:
                    self._image_vars[key] = tk.BooleanVar(value=True)
        for img_idx in range(len(self._solo_originals)):
            if img_idx not in self._solo_vars:
                self._solo_vars[img_idx] = tk.BooleanVar(value=False)

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
        if not self._groups:
            return 1
        import math
        return math.ceil(len(self._groups) / self._page_size)

    def _render_page(self, page: int) -> None:
        """Destroy current page widgets and build the requested page."""
        # Clear old widgets and per-page widget refs
        for widget in self._inner_frame.winfo_children():
            widget.destroy()
        self._group_border_frames.clear()

        self._current_page = max(0, min(page, self._total_pages() - 1))

        if not self._groups:
            tk.Label(
                self._inner_frame, text="No duplicate groups found.",
                font=("Segoe UI", 13), bg=_M_BG, fg=_M_TEXT3,
            ).pack(pady=40)
        else:
            start = self._current_page * self._page_size
            end   = min(start + self._page_size, len(self._groups))
            for idx in range(start, end):
                self._build_group_card(idx, self._groups[idx])

        # Solo / broken / manual-groups sections shown on last page
        if self._current_page >= self._total_pages() - 1:
            for mg_idx in range(len(self._manual_calib_groups)):
                self._build_manual_group_card(mg_idx)
            if self._solo_originals:
                self._build_solo_section()
            if self._broken_files:
                self._build_broken_section()

        self._canvas.yview_moveto(0)
        self._update_page_nav()

    def _update_page_nav(self) -> None:
        total  = self._total_pages()
        page   = self._current_page
        n      = len(self._groups)
        start  = page * self._page_size + 1
        end    = min((page + 1) * self._page_size, n)

        if n > 0:
            self._page_info_var.set(
                f"Page {page + 1} of {total}  ·  groups {start}–{end} of {n}"
                f"  ·  scroll past edge to flip page")
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

    # ── scroll helpers ────────────────────────────────────────────────────────

    def _on_canvas_enter(self, _=None) -> None:
        """Capture all mousewheel events while the pointer is inside the canvas."""
        self.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_canvas_leave(self, _=None) -> None:
        """Release the global mousewheel capture when the pointer leaves."""
        self.unbind_all("<MouseWheel>")

    def _scroll(self, delta: int) -> None:
        self._canvas.yview_scroll(delta, "units")

    def _on_mousewheel(self, event: tk.Event) -> None:
        delta = int(-1 * (event.delta / 120))
        top, bottom = self._canvas.yview()
        if delta > 0 and bottom >= 1.0:
            # Scrolled past the bottom — go to next page
            if self._current_page < self._total_pages() - 1:
                self._render_page(self._current_page + 1)
                # _render_page already calls yview_moveto(0)
        elif delta < 0 and top <= 0.0:
            # Scrolled past the top — go to previous page
            if self._current_page > 0:
                self._render_page(self._current_page - 1)
                self._canvas.yview_moveto(1.0)
        else:
            self._canvas.yview_scroll(delta, "units")

    def _on_frame_configure(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._canvas.itemconfig(self._canvas_window, width=event.width)
