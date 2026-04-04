"""
library_tab.py — Library tab for Image Deduper.

Displays every tracked folder with its drive status, cached file count
and last-updated time.  Provides Add Folder / Update / Delete /
Remap Drive operations with background progress.

Public API:
    build_library_tab(frame: ttk.Frame, app: "App") -> None
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from main import App   # type-check only — avoids circular import at runtime

from library import (
    Library,
    FolderEntry,
    DriveStatus,
    get_library_dir,
    update_folder,
)

# ── Colour palette (matches main.py / about_tab.py) ──────────────────────────

_BG          = "#F5F5F5"
_SURFACE     = "#FFFFFF"
_ACCENT      = "#1565C0"
_ACCENT_DARK = "#0D47A1"
_ACCENT_TINT = "#E3F2FD"
_SUCCESS     = "#2E7D32"
_SUCCESS_BG  = "#E8F5E9"
_ERROR       = "#C62828"
_ERROR_BG    = "#FFEBEE"
_WARNING     = "#E65100"
_WARNING_BG  = "#FFF3E0"
_DIVIDER     = "#E0E0E0"
_TEXT1       = "#212121"
_TEXT2       = "#616161"
_TEXT3       = "#9E9E9E"
_DISABLED    = "#BDBDBD"


# ── Status badge helpers ──────────────────────────────────────────────────────

_STATUS_LABELS = {
    "ok":      ("✓  OK",       _SUCCESS),
    "moved":   ("⚡  Moved",   _WARNING),
    "missing": ("✗  Missing",  _ERROR),
    "unknown": ("?  Unknown",  _TEXT3),
}

_DRIVE_TYPE_LABELS = {
    "fixed":     "Fixed",
    "removable": "Removable",
    "network":   "Network",
    "cdrom":     "CD/DVD",
    "ramdisk":   "RAM",
    "unknown":   "Unknown",
}


def _fmt_date(iso: str) -> str:
    """Format an ISO datetime string for display, e.g. '2026-04-03 14:22'."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d  %H:%M")
    except Exception:
        return iso or "—"


def _darken(hex_color: str) -> str:
    try:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
        f = 0.82
        return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"
    except Exception:
        return hex_color


def _mat_btn(parent, text, command, bg, fg="#FFFFFF", font_size=9, **kw) -> tk.Button:
    btn = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=_darken(bg), activeforeground=fg,
        relief=tk.FLAT, bd=0, padx=12, pady=5,
        font=("Segoe UI", font_size, "bold"), cursor="hand2", **kw,
    )
    btn._mat_bg = bg

    def _enter(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=_darken(btn._mat_bg))

    def _leave(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=btn._mat_bg)

    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)
    return btn


def _mat_enable(btn: tk.Button) -> None:
    btn.configure(state=tk.NORMAL, bg=btn._mat_bg, cursor="hand2")


def _mat_disable(btn: tk.Button) -> None:
    btn.configure(state=tk.DISABLED, bg=_DISABLED, cursor="")


# ── Main builder ──────────────────────────────────────────────────────────────

def build_library_tab(frame: ttk.Frame, app: "App") -> None:
    """
    Populate *frame* with the full Library tab UI.
    Called once from App._build_library_tab().
    Stores the controller as ``app._library_ctrl`` so other parts of the app
    (e.g. scan workers) can call ``app._library_ctrl.reload()`` to refresh.
    """
    ctrl = _LibraryTabController(frame, app)
    ctrl.build()
    app._library_ctrl = ctrl  # expose for cross-module refresh


# ── Controller ────────────────────────────────────────────────────────────────

class _LibraryTabController:
    """
    Manages all state and logic for the Library tab.

    Attributes
    ----------
    _lib        : Library loaded from user AppData; reloaded after mutations.
    _statuses   : dict[path_str, DriveStatus] — cached drive-status results.
    _busy       : bool — True while a background Add/Update is running.
    _selected   : str | None — path string of the selected treeview row.
    """

    def __init__(self, frame: ttk.Frame, app: "App") -> None:
        self._frame  = frame
        self._app    = app
        self._lib    = Library.load(get_library_dir())
        self._statuses: dict[str, DriveStatus] = {}
        self._busy   = False
        self._selected: Optional[str] = None

        # Widget refs populated in build()
        self._tree:        ttk.Treeview
        self._btn_add:     tk.Button
        self._btn_update:  tk.Button
        self._btn_delete:  tk.Button
        self._btn_remap:   tk.Button
        self._btn_refresh: tk.Button
        self._prog:        ttk.Progressbar
        self._status_lbl:  tk.Label
        self._summary_lbl: tk.Label

    # ── Build ─────────────────────────────────────────────────────────────

    def build(self) -> None:
        f = self._frame
        f.configure(style="TFrame")

        # ── Header card ───────────────────────────────────────────────────
        hdr_outer = tk.Frame(f, bg=_BG)
        hdr_outer.pack(fill=tk.X, padx=20, pady=(16, 0))

        hdr_card = tk.Frame(hdr_outer, bg=_SURFACE,
                            highlightbackground=_DIVIDER, highlightthickness=1)
        hdr_card.pack(fill=tk.X)

        tk.Frame(hdr_card, width=4, bg=_ACCENT).pack(side=tk.LEFT, fill=tk.Y)
        hdr_inner = tk.Frame(hdr_card, bg=_SURFACE, padx=16, pady=12)
        hdr_inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(hdr_inner, text="Hash Library",
                 font=("Segoe UI", 11, "bold"),
                 bg=_SURFACE, fg=_TEXT1).pack(anchor=tk.W)
        tk.Label(
            hdr_inner,
            text="Pre-computed image hashes are stored here so repeat scans skip re-hashing unchanged files.\n"
                 "Adding a folder hashes it once and caches the results automatically.",
            font=("Segoe UI", 9), bg=_SURFACE, fg=_TEXT2,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))

        # ── Toolbar ───────────────────────────────────────────────────────
        tb_outer = tk.Frame(f, bg=_BG)
        tb_outer.pack(fill=tk.X, padx=20, pady=(12, 0))

        tb = tk.Frame(tb_outer, bg=_SURFACE,
                      highlightbackground=_DIVIDER, highlightthickness=1)
        tb.pack(fill=tk.X)
        tb_inner = tk.Frame(tb, bg=_SURFACE, padx=10, pady=8)
        tb_inner.pack(fill=tk.X)

        self._btn_add = _mat_btn(
            tb_inner, "＋  Add Folder", self._on_add, _ACCENT)
        self._btn_add.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_update = _mat_btn(
            tb_inner, "↺  Update", self._on_update, "#37474F")
        self._btn_update.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_delete = _mat_btn(
            tb_inner, "🗑  Delete", self._on_delete, _ERROR)
        self._btn_delete.pack(side=tk.LEFT, padx=(0, 12))

        # Separator
        tk.Frame(tb_inner, width=1, bg=_DIVIDER).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self._btn_remap = _mat_btn(
            tb_inner, "⇄  Remap Drive", self._on_remap, _WARNING)
        self._btn_remap.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_refresh = _mat_btn(
            tb_inner, "⟳  Refresh Status", self._on_refresh_status, "#455A64")
        self._btn_refresh.pack(side=tk.LEFT)

        # Summary label (right-aligned)
        self._summary_lbl = tk.Label(tb_inner, text="",
                                     font=("Segoe UI", 9), bg=_SURFACE, fg=_TEXT2)
        self._summary_lbl.pack(side=tk.RIGHT, padx=8)

        # ── Treeview ──────────────────────────────────────────────────────
        tree_outer = tk.Frame(f, bg=_BG)
        tree_outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=(10, 0))

        tree_card = tk.Frame(tree_outer, bg=_SURFACE,
                             highlightbackground=_DIVIDER, highlightthickness=1)
        tree_card.pack(fill=tk.BOTH, expand=True)

        cols = ("path", "status", "type", "files", "updated")
        self._tree = ttk.Treeview(tree_card, columns=cols, show="headings",
                                  selectmode="browse")

        self._tree.heading("path",    text="Folder",        anchor=tk.W)
        self._tree.heading("status",  text="Drive Status",  anchor=tk.W)
        self._tree.heading("type",    text="Drive Type",    anchor=tk.W)
        self._tree.heading("files",   text="Cached Files",  anchor=tk.E)
        self._tree.heading("updated", text="Last Updated",  anchor=tk.W)

        self._tree.column("path",    width=380, minwidth=200, stretch=True)
        self._tree.column("status",  width=110, minwidth=90,  stretch=False)
        self._tree.column("type",    width=100, minwidth=80,  stretch=False)
        self._tree.column("files",   width=90,  minwidth=60,  stretch=False, anchor=tk.E)
        self._tree.column("updated", width=150, minwidth=120, stretch=False)

        # Row tags for status colours
        self._tree.tag_configure("ok",      foreground=_SUCCESS)
        self._tree.tag_configure("moved",   foreground=_WARNING)
        self._tree.tag_configure("missing", foreground=_ERROR)
        self._tree.tag_configure("unknown", foreground=_TEXT3)

        vsb = ttk.Scrollbar(tree_card, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # ── Progress / status bar ─────────────────────────────────────────
        pb_outer = tk.Frame(f, bg=_BG)
        pb_outer.pack(fill=tk.X, padx=20, pady=(6, 14))

        self._prog = ttk.Progressbar(pb_outer, mode="determinate", length=300)
        self._prog.pack(side=tk.LEFT)

        self._status_lbl = tk.Label(pb_outer, text="",
                                    font=("Segoe UI", 9), bg=_BG, fg=_TEXT2,
                                    anchor=tk.W)
        self._status_lbl.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        # ── Initial data load + auto status check ────────────────────────
        self._reload_table()
        self._update_btn_states()
        # Kick off drive-status check in the background so rows don't sit
        # on "Checking…" permanently.  Only fires if there is at least one
        # tracked folder (avoids a pointless thread on first launch).
        if self._lib.folders:
            self._on_refresh_status()

        # ── Auto-refresh when the Library tab becomes visible ─────────────
        # This guarantees fresh data even when the scan finished while the
        # user was on a different tab, or when reload() had a silent error.
        def _on_tab_change(event=None):
            try:
                nb = f.nametowidget(f.winfo_parent())
                if nb.select() == str(f):
                    self._reload_table()
                    if self._lib.folders and not self._busy:
                        self._on_refresh_status()
            except Exception:
                pass

        # Bind on the Notebook that owns this frame
        try:
            nb_widget = f.nametowidget(f.winfo_parent())
            nb_widget.bind("<<NotebookTabChanged>>", _on_tab_change, add=True)
        except Exception:
            pass

    # ── Public refresh ────────────────────────────────────────────────────

    def reload(self) -> None:
        """Reload library from disk and refresh the table.

        Safe to call from the main thread at any time (e.g. after a scan
        writes new hashes to disk).
        """
        self._reload_table()
        if self._lib.folders and not self._busy:
            self._on_refresh_status()

    # ── Table helpers ─────────────────────────────────────────────────────

    def _reload_table(self) -> None:
        """Re-read the library from disk and repopulate the treeview."""
        self._lib = Library.load(get_library_dir())
        children = self._tree.get_children()
        if children:
            self._tree.delete(*children)
        entries = self._lib.folders

        total_files = sum(e.file_count for e in entries)
        n = len(entries)
        summary = f"{n} folder{'s' if n != 1 else ''}  ·  {total_files:,} cached files"
        self._summary_lbl.configure(text=summary)

        for entry in entries:
            status = self._statuses.get(entry.path)
            self._insert_row(entry, status)

        self._selected = None
        self._update_btn_states()

    def _insert_row(self, entry: FolderEntry,
                    status: Optional[DriveStatus] = None) -> None:
        """Insert or update a single treeview row for *entry*."""
        if status is None:
            status_text = "Checking…"
            tag = "unknown"
        else:
            label, _ = _STATUS_LABELS.get(status.state, ("?  Unknown", _TEXT3))
            status_text = label
            tag = status.state

        drive_label = _DRIVE_TYPE_LABELS.get(entry.drive_type, entry.drive_type.title())
        updated     = _fmt_date(entry.last_updated)

        self._tree.insert(
            "", tk.END,
            iid   = entry.path,
            values = (entry.path, status_text, drive_label,
                      f"{entry.file_count:,}", updated),
            tags  = (tag,),
        )

    def _refresh_row(self, entry: FolderEntry) -> None:
        """Refresh just one row in-place (status may have changed)."""
        if not self._tree.exists(entry.path):
            return
        status = self._statuses.get(entry.path)
        if status is None:
            status_text, tag = "Checking…", "unknown"
        else:
            label, _ = _STATUS_LABELS.get(status.state, ("?  Unknown", _TEXT3))
            status_text, tag = label, status.state

        drive_label = _DRIVE_TYPE_LABELS.get(entry.drive_type, entry.drive_type.title())
        updated     = _fmt_date(entry.last_updated)

        self._tree.item(
            entry.path,
            values = (entry.path, status_text, drive_label,
                      f"{entry.file_count:,}", updated),
            tags   = (tag,),
        )

    # ── Button state management ───────────────────────────────────────────

    def _update_btn_states(self) -> None:
        """Enable/disable toolbar buttons based on selection and busy state."""
        has_sel  = self._selected is not None
        can_remap = False
        if has_sel:
            st = self._statuses.get(self._selected)
            can_remap = st is not None and st.state == "moved" and st.new_path is not None

        if self._busy:
            _mat_disable(self._btn_add)
            _mat_disable(self._btn_update)
            _mat_disable(self._btn_delete)
            _mat_disable(self._btn_remap)
            _mat_disable(self._btn_refresh)
        else:
            _mat_enable(self._btn_add)
            if has_sel:
                _mat_enable(self._btn_update)
                _mat_enable(self._btn_delete)
            else:
                _mat_disable(self._btn_update)
                _mat_disable(self._btn_delete)
            if can_remap:
                _mat_enable(self._btn_remap)
            else:
                _mat_disable(self._btn_remap)
            _mat_enable(self._btn_refresh)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_btn_states()
        if not busy:
            self._prog.configure(value=0)
            self._status_lbl.configure(text="")

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_select(self, _event=None) -> None:
        sel = self._tree.selection()
        self._selected = sel[0] if sel else None
        self._update_btn_states()

    def _on_add(self) -> None:
        folder_str = filedialog.askdirectory(
            title="Select folder to add to Library",
            parent=self._frame,
        )
        if not folder_str:
            return
        folder = Path(folder_str).resolve()
        self._run_update(folder)

    def _on_update(self) -> None:
        if not self._selected:
            return
        folder = Path(self._selected).resolve()
        self._run_update(folder)

    def _on_delete(self) -> None:
        if not self._selected:
            return
        path = self._selected
        short = Path(path).name or path
        if not messagebox.askyesno(
            "Remove from Library",
            f'Remove  "{short}"  from the Library?\n\n'
            "Cached hashes for this folder will be deleted. "
            "The folder itself and its images are not affected.",
            parent=self._frame,
        ):
            return
        self._lib.remove_folder(path)   # already calls save() internally
        self._statuses.pop(path, None)
        self._reload_table()

    def _on_remap(self) -> None:
        if not self._selected:
            return
        st = self._statuses.get(self._selected)
        if st is None or st.state != "moved" or not st.new_path:
            messagebox.showinfo(
                "Remap Drive",
                "No automatic remap is available for this folder.\n\n"
                "The drive may be missing or not connected.",
                parent=self._frame,
            )
            return
        old_path = self._selected
        new_path = st.new_path

        if not messagebox.askyesno(
            "Remap Drive",
            f"The drive for this folder has been detected at a new location.\n\n"
            f"Old path:  {old_path}\n"
            f"New path:  {new_path}\n\n"
            "Update the library to use the new path?",
            parent=self._frame,
        ):
            return

        self._lib.update_path(old_path, new_path)   # already calls save() internally
        # Move the status over to the new path key
        self._statuses.pop(old_path, None)
        self._statuses[new_path] = DriveStatus(state="ok")
        self._reload_table()
        self._status_lbl.configure(
            text=f"Drive remapped → {new_path}", fg=_SUCCESS)

    def _on_refresh_status(self) -> None:
        """Re-check drive status for all tracked folders in the background."""
        self._set_busy(True)
        self._status_lbl.configure(text="Checking drive status…", fg=_TEXT2)
        threading.Thread(target=self._bg_check_all_statuses,
                         daemon=True).start()

    # ── Background: drive status check ───────────────────────────────────

    def _bg_check_all_statuses(self) -> None:
        """Run in a background thread — check drive status for every folder."""
        for entry in self._lib.folders:
            try:
                st = self._lib.check_drive_status(entry)
                self._statuses[entry.path] = st
            except Exception:
                self._statuses[entry.path] = DriveStatus(state="unknown")
        self._frame.after(0, self._after_status_check)

    def _after_status_check(self) -> None:
        for entry in self._lib.folders:
            self._refresh_row(entry)
        self._set_busy(False)   # already calls _update_btn_states() internally
        self._status_lbl.configure(text="Status refreshed.", fg=_TEXT2)

    # ── Background: update / add folder ──────────────────────────────────

    def _run_update(self, folder: Path) -> None:
        """Start a background update_folder() for *folder*."""
        self._set_busy(True)
        self._prog.configure(mode="indeterminate", value=0)
        self._prog.start(12)
        self._status_lbl.configure(text=f"Hashing {folder.name}…", fg=_TEXT2)

        stop_flag: list[bool] = [False]

        def _progress(name: str, done: int, total: int) -> None:
            def _ui():
                if total > 0:
                    self._prog.stop()
                    self._prog.configure(mode="determinate",
                                         value=int(done / total * 100))
                label = f"{name}  ({done}/{total})"
                self._status_lbl.configure(text=label)
            self._frame.after(0, _ui)

        def _bg() -> None:
            entry = None
            error: Optional[Exception] = None
            try:
                entry = update_folder(
                    self._lib, folder,
                    settings    = self._app.settings,
                    progress_cb = _progress,
                    stop_flag   = stop_flag,
                )
                self._lib.save()
                # Check drive status while still on background thread
                try:
                    st = self._lib.check_drive_status(entry)
                    self._statuses[entry.path] = st
                except Exception:
                    self._statuses[str(folder.resolve())] = DriveStatus(state="ok")
            except Exception as exc:
                error = exc
            self._frame.after(0, lambda: self._after_update(entry, error))

        threading.Thread(target=_bg, daemon=True).start()

    def _after_update(self, entry: Optional[FolderEntry],
                       error: Optional[Exception]) -> None:
        self._prog.stop()
        self._set_busy(False)

        if error is not None:
            self._status_lbl.configure(
                text=f"Error: {error}", fg=_ERROR)
            messagebox.showerror(
                "Library Error",
                f"Failed to update folder:\n\n{error}",
                parent=self._frame,
            )
            return

        if entry is None:
            self._status_lbl.configure(text="Nothing to update.", fg=_TEXT2)
            return

        self._reload_table()
        self._status_lbl.configure(
            text=f"Done — {entry.file_count:,} files cached for {Path(entry.path).name}",
            fg=_SUCCESS,
        )
