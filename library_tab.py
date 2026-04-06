"""
library_tab.py — Library tab for Image Deduper.

Displays every tracked folder with its drive status, cached file count
and last-updated time.  Provides Add Folder / Update / Delete /
Remap Drive operations with background progress.

Public API:
    build_library_tab(frame: ttk.Frame, app: "App") -> None
"""
from __future__ import annotations

import os
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

# ── Material Design 3 colour palette (light defaults, overwritten by _apply_theme) ──

_BG          = "#F2F4F7"
_SURFACE     = "#FFFFFF"
_ACCENT      = "#1565C0"
_ACCENT_DARK = "#0D47A1"
_ACCENT_TINT = "#E8EFF9"
_SUCCESS     = "#2E7D32"
_SUCCESS_BG  = "#E8F5E9"
_ERROR       = "#C62828"
_ERROR_BG    = "#FFEBEE"
_WARNING     = "#E65100"
_WARNING_BG  = "#FFF3E0"
_DIVIDER     = "#DDE1E6"
_TEXT1       = "#1B1B1F"
_TEXT2       = "#49454F"
_TEXT3       = "#79747E"
_DISABLED    = "#C4C7C5"


def _apply_theme(dark: bool = False) -> None:
    """Overwrite module colours from the theme palette."""
    global _BG, _SURFACE, _ACCENT, _ACCENT_DARK, _ACCENT_TINT
    global _SUCCESS, _SUCCESS_BG, _ERROR, _ERROR_BG, _WARNING, _WARNING_BG
    global _DIVIDER, _TEXT1, _TEXT2, _TEXT3, _DISABLED
    import theme as _t
    p = _t.get_palette(dark)
    _BG          = p["BG"]
    _SURFACE     = p["CARD_BG"]
    _ACCENT      = p["ACCENT"]
    _ACCENT_DARK = p["ACCENT_DARK"]
    _ACCENT_TINT = p["ACCENT_TINT"]
    _SUCCESS     = p["SUCCESS"]
    _SUCCESS_BG  = p["SUCCESS_TINT"]
    _ERROR       = p["ERROR"]
    _ERROR_BG    = p["ERROR_TINT"]
    _WARNING     = p["WARNING"]
    _WARNING_BG  = p["WARNING_TINT"]
    _DIVIDER     = p["DIVIDER"]
    _TEXT1       = p["TEXT1"]
    _TEXT2       = p["TEXT2"]
    _TEXT3       = p["TEXT3"]
    _DISABLED    = p["DISABLED"]

_DUMMY_PREFIX = "DUMMY:"   # prefix for placeholder treeview items used in LibFolderPickerDialog

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
    btn._mat_fg = fg

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
    btn.configure(state=tk.NORMAL, bg=btn._mat_bg, fg=btn._mat_fg,
                  activebackground=_darken(btn._mat_bg),
                  activeforeground=btn._mat_fg, cursor="hand2")


def _mat_disable(btn: tk.Button) -> None:
    _dfg = "#605C66" if _BG.startswith("#1") else "#838387"
    btn.configure(state=tk.DISABLED, bg=_DISABLED, fg=_dfg, cursor="")


class LibFolderPickerDialog:
    """Modal dialog for picking any folder from the library hierarchy.

    Shows tracked library folders (bold) in a hierarchical tree.  Each node
    can be expanded to browse its filesystem sub-directories so the user can
    pick a sub-folder that is covered by a tracked ancestor's cache.

    Usage::

        dlg = LibFolderPickerDialog(parent_widget)
        if dlg.result:
            do_something(dlg.result)  # absolute path string
    """

    def __init__(self, parent: tk.Widget,
                 title: str = "Select Folder from Library") -> None:
        from library import Library, get_library_dir
        self._lib    = Library.load(get_library_dir())
        self._result: Optional[str] = None

        top = tk.Toplevel(parent)
        top.title(title)
        top.geometry("660x500")
        top.resizable(True, True)
        top.transient(parent)
        top.grab_set()

        # ── Instruction label ─────────────────────────────────────────────
        tk.Label(
            top,
            text="Tracked library folders are shown in bold.\n"
                 "Expand any folder to browse its sub-folders — all benefit from the parent's cached hashes.",
            justify=tk.LEFT,
            font=("Segoe UI", 9),
            bg=top.cget("bg"),
        ).pack(anchor=tk.W, padx=12, pady=(10, 4))

        # ── Tree ──────────────────────────────────────────────────────────
        tree_frame = tk.Frame(top)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        tree = ttk.Treeview(tree_frame, selectmode="browse")
        tree.heading("#0", text="Folder", anchor=tk.W)
        tree.column("#0", stretch=True)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        tree.tag_configure("tracked_ok",      font=("Segoe UI", 9, "bold"), foreground=_SUCCESS)
        tree.tag_configure("tracked_missing", font=("Segoe UI", 9, "bold"), foreground=_ERROR)

        self._tree = tree
        self._top  = top

        self._populate_tracked()
        tree.bind("<<TreeviewOpen>>", self._on_expand)
        tree.bind("<Double-1>",       self._on_double_click)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_frame = tk.Frame(top, bg=top.cget("bg"))
        btn_frame.pack(fill=tk.X, padx=10, pady=(6, 10))

        _mat_btn(btn_frame, "Select", self._confirm, _ACCENT).pack(side=tk.RIGHT, padx=(4, 0))
        tk.Button(btn_frame, text="Cancel", command=top.destroy,
                  relief=tk.FLAT, padx=10, pady=4).pack(side=tk.RIGHT)

        top.wait_window()

    # ── Population ────────────────────────────────────────────────────────

    def _populate_tracked(self) -> None:
        """Insert tracked folders in a parent→child hierarchy."""
        entries  = sorted(self._lib.folders, key=lambda e: e.path)
        inserted: list[str] = []

        for entry in entries:
            path       = entry.path
            parent_iid = self._best_parent(path, inserted)

            if parent_iid:
                try:
                    display = str(Path(path).relative_to(Path(parent_iid)))
                except ValueError:
                    display = Path(path).name
            else:
                display = path

            st  = self._lib.check_drive_status(entry)
            tag = "tracked_ok" if st.state == "ok" else "tracked_missing"

            if not self._tree.exists(path):
                self._tree.insert(parent_iid, tk.END,
                                  iid=path, text=display, tags=(tag,))

            inserted.append(path)
            self._maybe_add_dummy(path)

    @staticmethod
    def _best_parent(path: str, candidates: list) -> str:
        """Return the longest candidate that is a proper ancestor of *path*."""
        best, best_len = "", 0
        for c in candidates:
            if path.startswith(c + os.sep) and len(c) > best_len:
                best, best_len = c, len(c)
        return best

    def _maybe_add_dummy(self, folder_iid: str) -> None:
        """Add a placeholder child if the folder contains sub-directories."""
        try:
            has_sub = any(p.is_dir() for p in Path(folder_iid).iterdir())
            if has_sub:
                dummy = _DUMMY_PREFIX + folder_iid
                if not self._tree.exists(dummy):
                    self._tree.insert(folder_iid, tk.END, iid=dummy, text="")
        except (PermissionError, OSError):
            pass

    # ── Lazy expansion ────────────────────────────────────────────────────

    def _on_expand(self, _event=None) -> None:
        iid   = self._tree.focus()
        dummy = _DUMMY_PREFIX + iid
        if dummy not in self._tree.get_children(iid):
            return  # already fully loaded
        self._tree.delete(dummy)

        try:
            subdirs = sorted(
                p for p in Path(iid).iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except (PermissionError, OSError):
            return

        tracked = {e.path for e in self._lib.folders}
        existing = set(self._tree.get_children(iid))

        for subdir in subdirs:
            sub_str = str(subdir)
            if sub_str in existing:
                continue  # already inserted as a tracked child
            tag = ("tracked_ok",) if sub_str in tracked else ()
            self._tree.insert(iid, tk.END, iid=sub_str, text=subdir.name, tags=tag)
            self._maybe_add_dummy(sub_str)

    # ── Confirmation ──────────────────────────────────────────────────────

    def _on_double_click(self, event=None) -> None:
        item = self._tree.identify_row(event.y) if event else ""
        if item and not item.startswith(_DUMMY_PREFIX):
            self._result = item
            self._top.destroy()

    def _confirm(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith(_DUMMY_PREFIX):
            return
        self._result = iid
        self._top.destroy()

    @property
    def result(self) -> Optional[str]:
        return self._result


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

        hdr_card = tk.Frame(hdr_outer, bg=_SURFACE, highlightthickness=0)
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

        tb = tk.Frame(tb_outer, bg=_SURFACE, highlightthickness=0)
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

        tree_card = tk.Frame(tree_outer, bg=_SURFACE, highlightthickness=0)
        tree_card.pack(fill=tk.BOTH, expand=True)

        cols = ("status", "type", "files", "updated")
        self._tree = ttk.Treeview(tree_card, columns=cols, show="tree headings",
                                  selectmode="browse")

        self._tree.heading("#0",      text="Folder",        anchor=tk.W)
        self._tree.heading("status",  text="Drive Status",  anchor=tk.W)
        self._tree.heading("type",    text="Drive Type",    anchor=tk.W)
        self._tree.heading("files",   text="Cached Files",  anchor=tk.E)
        self._tree.heading("updated", text="Last Updated",  anchor=tk.W)

        self._tree.column("#0",      width=340, minwidth=180, stretch=True)
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
        """Re-read the library from disk and repopulate the treeview in folder hierarchy order."""
        self._lib = Library.load(get_library_dir())
        children = self._tree.get_children()
        if children:
            self._tree.delete(*children)
        entries = self._lib.folders

        total_files = sum(e.file_count for e in entries)
        n = len(entries)
        summary = f"{n} folder{'s' if n != 1 else ''}  ·  {total_files:,} cached files"
        self._summary_lbl.configure(text=summary)

        # Build parent→child hierarchy (sort by path so parents come first)
        sorted_entries = sorted(entries, key=lambda e: e.path)
        inserted: list[str] = []

        for entry in sorted_entries:
            path       = entry.path
            parent_iid = ""
            best_len   = 0
            for ins in inserted:
                if path.startswith(ins + os.sep) and len(ins) > best_len:
                    parent_iid = ins
                    best_len   = len(ins)

            status = self._statuses.get(path)
            self._insert_row(entry, status, parent_iid=parent_iid)
            inserted.append(path)

        # Auto-expand top-level nodes
        for iid in self._tree.get_children(""):
            self._tree.item(iid, open=True)

        self._selected = None
        self._update_btn_states()

    def _insert_row(self, entry: FolderEntry,
                    status: Optional[DriveStatus] = None,
                    parent_iid: str = "") -> None:
        """Insert a single treeview row for *entry* under *parent_iid*."""
        if status is None:
            status_text = "Checking…"
            tag = "unknown"
        else:
            label, _ = _STATUS_LABELS.get(status.state, ("?  Unknown", _TEXT3))
            status_text = label
            tag = status.state

        drive_label = _DRIVE_TYPE_LABELS.get(entry.drive_type, entry.drive_type.title())
        updated     = _fmt_date(entry.last_updated)

        # Display name: relative sub-path when nested, full path at root level
        path = entry.path
        if parent_iid:
            try:
                display = str(Path(path).relative_to(Path(parent_iid)))
            except ValueError:
                display = Path(path).name
        else:
            display = path

        self._tree.insert(
            parent_iid, tk.END,
            iid    = path,
            text   = display,
            values = (status_text, drive_label, f"{entry.file_count:,}", updated),
            tags   = (tag,),
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
            values = (status_text, drive_label, f"{entry.file_count:,}", updated),
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
