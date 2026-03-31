"""
report_viewer.py — In-app Tkinter report viewer with thumbnails, checkboxes,
                   apply and revert support.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

try:
    from PIL import Image as PILImage, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

from scanner import DuplicateGroup, ImageRecord


_THUMB_SIZE = 160
_CARD_BG = "#ffffff"
_SERIES_COLOR = "#7c3aed"
_ORIG_BG = "#f0fdf4"
_PREV_BG = "#fff7f7"
_HEADER_BG = "#1a73e8"


class ReportViewer(tk.Toplevel):
    """Full-screen in-app report viewer with checkboxes and revert support."""

    def __init__(
        self,
        parent: tk.Widget,
        groups: List[DuplicateGroup],
        ops_log_path: Optional[Path] = None,
        on_apply_cb: Optional[Callable] = None,
    ) -> None:
        super().__init__(parent)
        self.title("Image Deduper — Review Results")
        self.geometry("1100x750")
        self.minsize(800, 500)

        self._groups = groups
        self._ops_log_path = ops_log_path
        self._on_apply_cb = on_apply_cb

        # Photo reference storage (prevent GC)
        self._photo_refs: list = []

        # Per-group checkboxes (index -> BooleanVar)
        self._group_vars: dict[int, tk.BooleanVar] = {}
        # Per-image checkboxes (group_idx, role, img_idx) -> BooleanVar
        self._image_vars: dict[tuple, tk.BooleanVar] = {}
        # Group card frames
        self._group_frames: dict[int, tk.Frame] = {}

        self._build_ui()
        self.lift()
        self.focus_set()

        # Keyboard bindings
        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Up>", lambda _: self._scroll(-3))
        self.bind("<Down>", lambda _: self._scroll(3))
        self.bind("<space>", self._toggle_focused_group)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header bar
        hdr = tk.Frame(self, bg=_HEADER_BG)
        hdr.pack(fill=tk.X)

        tk.Label(
            hdr, text="Review Duplicate Groups",
            font=("Segoe UI", 13, "bold"),
            bg=_HEADER_BG, fg="white"
        ).pack(side=tk.LEFT, padx=16, pady=10)

        # Stats
        n_groups = len(self._groups)
        n_previews = sum(len(g.previews) for g in self._groups)
        n_series = sum(1 for g in self._groups if g.is_series)
        stats_text = (
            f"{n_groups} groups  \u00b7  {n_previews} previews  "
            f"\u00b7  {n_series} series groups"
        )
        tk.Label(
            hdr, text=stats_text,
            font=("Segoe UI", 9),
            bg=_HEADER_BG, fg="#b3cfff"
        ).pack(side=tk.LEFT, padx=8)

        # Select all / none buttons
        ttk.Button(hdr, text="Select All", command=self._select_all).pack(side=tk.RIGHT, padx=4, pady=8)
        ttk.Button(hdr, text="Select None", command=self._select_none).pack(side=tk.RIGHT, padx=4, pady=8)

        # Action bar
        action_bar = ttk.Frame(self)
        action_bar.pack(fill=tk.X, padx=8, pady=4)

        self._apply_btn = ttk.Button(
            action_bar, text="Apply Selected",
            command=self._on_apply
        )
        self._apply_btn.pack(side=tk.LEFT, padx=4)

        if self._ops_log_path and self._ops_log_path.exists():
            ttk.Button(
                action_bar, text="Revert Selected",
                command=self._on_revert_selected
            ).pack(side=tk.LEFT, padx=4)
            ttk.Button(
                action_bar, text="Revert All",
                command=self._on_revert_all
            ).pack(side=tk.LEFT, padx=4)

        self._status_lbl = ttk.Label(action_bar, text="", foreground="#444")
        self._status_lbl.pack(side=tk.RIGHT, padx=8)

        # Scrollable canvas
        container = tk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(container, bg="#f0f2f5", highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._inner_frame = tk.Frame(self._canvas, bg="#f0f2f5")
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._inner_frame, anchor=tk.NW
        )

        self._inner_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)

        # Build group cards
        if self._groups:
            for idx, group in enumerate(self._groups):
                self._build_group_card(idx, group)
        else:
            tk.Label(
                self._inner_frame,
                text="No duplicate groups found.",
                font=("Segoe UI", 12),
                bg="#f0f2f5", fg="#888"
            ).pack(pady=60)

        # Pre-select all groups
        self._select_all()

    def _build_group_card(self, idx: int, group: DuplicateGroup) -> None:
        """Build a card widget for one DuplicateGroup."""
        outer = tk.Frame(self._inner_frame, bg="#f0f2f5", pady=6, padx=10)
        outer.pack(fill=tk.X)
        self._group_frames[idx] = outer

        card = tk.Frame(outer, bg=_CARD_BG, relief=tk.FLAT,
                        highlightbackground="#d1d5db", highlightthickness=1)
        card.pack(fill=tk.X)

        # Card header
        head = tk.Frame(card, bg="#e8f0fe")
        head.pack(fill=tk.X)

        # Group checkbox
        g_var = tk.BooleanVar(value=True)
        self._group_vars[idx] = g_var
        cb = tk.Checkbutton(
            head, variable=g_var, bg="#e8f0fe",
            command=lambda i=idx: self._on_group_toggle(i)
        )
        cb.pack(side=tk.LEFT, padx=(8, 0), pady=6)

        # Group label
        n_orig = len(group.originals)
        n_prev = len(group.previews)
        lbl_text = (
            f"#{idx + 1}  ({group.group_id})  "
            f"{n_orig} original{'s' if n_orig != 1 else ''} "
            f"\u00b7 {n_prev} preview{'s' if n_prev != 1 else ''}"
        )
        tk.Label(
            head, text=lbl_text,
            font=("Segoe UI", 9, "bold"),
            bg="#e8f0fe", fg="#1a73e8"
        ).pack(side=tk.LEFT, padx=6)

        if group.is_series:
            tk.Label(
                head, text="SERIES \u2014 all images kept",
                font=("Segoe UI", 8, "bold"),
                bg=_SERIES_COLOR, fg="white",
                padx=8, pady=2
            ).pack(side=tk.LEFT, padx=6)

        # Card body: two columns
        body = tk.Frame(card, bg=_CARD_BG)
        body.pack(fill=tk.X)

        # Originals column
        orig_col = tk.Frame(body, bg=_ORIG_BG, padx=10, pady=8)
        orig_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            orig_col, text="Originals to keep \u2192 results/",
            font=("Segoe UI", 8, "bold"),
            bg=_ORIG_BG, fg="#16a34a"
        ).pack(anchor=tk.W, pady=(0, 6))

        orig_grid = tk.Frame(orig_col, bg=_ORIG_BG)
        orig_grid.pack(fill=tk.X)
        for img_idx, rec in enumerate(group.originals):
            key = (idx, "orig", img_idx)
            v = tk.BooleanVar(value=True)
            self._image_vars[key] = v
            self._build_image_tile(orig_grid, rec, v, img_idx % 3, img_idx // 3,
                                   bg=_ORIG_BG)

        # Separator
        sep = tk.Frame(body, bg="#e5e7eb", width=1)
        sep.pack(side=tk.LEFT, fill=tk.Y)

        # Previews column
        prev_col = tk.Frame(body, bg=_PREV_BG, padx=10, pady=8)
        prev_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(
            prev_col, text="Previews to trash \u2192 trash/",
            font=("Segoe UI", 8, "bold"),
            bg=_PREV_BG, fg="#dc2626"
        ).pack(anchor=tk.W, pady=(0, 6))

        prev_grid = tk.Frame(prev_col, bg=_PREV_BG)
        prev_grid.pack(fill=tk.X)
        for img_idx, rec in enumerate(group.previews):
            key = (idx, "prev", img_idx)
            v = tk.BooleanVar(value=True)
            self._image_vars[key] = v
            self._build_image_tile(prev_grid, rec, v, img_idx % 4, img_idx // 4,
                                   bg=_PREV_BG, max_thumb=120)

    def _build_image_tile(
        self,
        parent: tk.Frame,
        rec: ImageRecord,
        var: tk.BooleanVar,
        col: int, row: int,
        bg: str,
        max_thumb: int = _THUMB_SIZE,
    ) -> None:
        """Build a single image tile with thumbnail, checkbox, and metadata."""
        tile = tk.Frame(parent, bg=bg, padx=4, pady=4)
        tile.grid(row=row, column=col, padx=4, pady=4, sticky=tk.NW)

        # Thumbnail
        thumb_lbl = tk.Label(tile, bg=bg)
        thumb_lbl.pack()
        self._load_thumbnail_async(rec.path, thumb_lbl, max_thumb)

        # Checkbox + filename
        cb_frame = tk.Frame(tile, bg=bg)
        cb_frame.pack(fill=tk.X)
        tk.Checkbutton(cb_frame, variable=var, bg=bg).pack(side=tk.LEFT)
        fname = rec.path.name
        if len(fname) > 18:
            fname = fname[:15] + "..."
        tk.Label(
            cb_frame, text=fname,
            font=("Segoe UI", 7),
            bg=bg, wraplength=max_thumb
        ).pack(side=tk.LEFT)

        # Dimensions + size
        meta = f"{rec.width}x{rec.height}  {rec.size_label()}"
        tk.Label(tile, text=meta, font=("Segoe UI", 7), bg=bg, fg="#666").pack()

        # Date
        tk.Label(
            tile, text=rec.date_label(),
            font=("Segoe UI", 7), bg=bg, fg="#aaa"
        ).pack()

    def _load_thumbnail_async(self, path: Path, label: tk.Label, max_px: int) -> None:
        """Load thumbnail in a background thread and update the label."""
        def _load() -> None:
            if not _PIL_AVAILABLE:
                return
            try:
                with PILImage.open(path) as img:
                    img.thumbnail((max_px, max_px), PILImage.LANCZOS)
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")
                    photo = ImageTk.PhotoImage(img)
                    self._photo_refs.append(photo)
                    label.after(0, lambda p=photo: label.configure(image=p))
            except Exception:
                label.after(0, lambda: label.configure(text="[no preview]", fg="#aaa"))

        threading.Thread(target=_load, daemon=True).start()

    # ── selection helpers ─────────────────────────────────────────────────

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
        """Sync image checkboxes when a group checkbox is toggled."""
        checked = self._group_vars[group_idx].get()
        for key, var in self._image_vars.items():
            if key[0] == group_idx:
                var.set(checked)

    def _toggle_focused_group(self, _event=None) -> None:
        """Toggle the first group's checkbox (placeholder for focus tracking)."""
        if 0 in self._group_vars:
            current = self._group_vars[0].get()
            self._group_vars[0].set(not current)
            self._on_group_toggle(0)

    # ── actions ──────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        """Collect checked preview images and call on_apply_cb."""
        if self._on_apply_cb is None:
            messagebox.showinfo("Apply", "No apply callback configured.", parent=self)
            return

        selected_group_ids = [
            self._groups[idx].group_id
            for idx, var in self._group_vars.items()
            if var.get()
        ]

        if not selected_group_ids:
            messagebox.showwarning("Apply", "No groups selected.", parent=self)
            return

        selected_groups = [g for g in self._groups if g.group_id in selected_group_ids]
        self._apply_btn.config(state=tk.DISABLED)
        self._status_lbl.config(text="Applying...")
        self.update_idletasks()

        def _do_apply() -> None:
            try:
                self._on_apply_cb(selected_groups)
                self.after(0, lambda: self._status_lbl.config(
                    text=f"Applied {len(selected_groups)} groups."
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Error", str(exc), parent=self))
            finally:
                self.after(0, lambda: self._apply_btn.config(state=tk.NORMAL))

        threading.Thread(target=_do_apply, daemon=True).start()

    def _on_revert_selected(self) -> None:
        if not self._ops_log_path:
            return
        selected_group_ids = [
            self._groups[idx].group_id
            for idx, var in self._group_vars.items()
            if var.get()
        ]
        if not selected_group_ids:
            messagebox.showwarning("Revert", "No groups selected.", parent=self)
            return
        self._do_revert(selected_group_ids)

    def _on_revert_all(self) -> None:
        if not self._ops_log_path:
            return
        if not messagebox.askyesno(
            "Revert All",
            "Move all files back to their original locations?",
            parent=self
        ):
            return
        self._do_revert(None)

    def _do_revert(self, group_ids: Optional[list[str]]) -> None:
        self._status_lbl.config(text="Reverting...")
        self.update_idletasks()

        def _worker() -> None:
            from mover import revert_operations
            reverted, errors = revert_operations(self._ops_log_path, group_ids)
            msg = f"Reverted {reverted} files."
            if errors:
                msg += f" ({errors} errors)"
            self.after(0, lambda: self._status_lbl.config(text=msg))

        threading.Thread(target=_worker, daemon=True).start()

    # ── scroll helpers ────────────────────────────────────────────────────

    def _scroll(self, delta: int) -> None:
        self._canvas.yview_scroll(delta, "units")

    def _on_mousewheel(self, event: tk.Event) -> None:
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_frame_configure(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._canvas.itemconfig(self._canvas_window, width=event.width)
