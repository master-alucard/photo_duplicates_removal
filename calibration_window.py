"""
calibration_window.py — Four-tab calibration wizard.

CalibrationPanel  – embeddable tk.Frame (all logic lives here).
CalibrationWindow – standalone tk.Toplevel that wraps the panel.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Callable

from config import Settings

# ── Material Design colour palette ───────────────────────────────────────────
_BG             = "#F5F5F5"   # Grey 100
_SURFACE        = "#FFFFFF"   # Card surface
_M_PRIMARY      = "#1565C0"   # Blue 800
_M_SUCCESS      = "#2E7D32"   # Green 800
_M_ERROR        = "#C62828"   # Red 800
_M_DIVIDER      = "#E0E0E0"   # Grey 300
_M_TEXT1        = "#212121"   # Grey 900
_M_TEXT2        = "#616161"   # Grey 700
_MAT_DISABLED   = "#BDBDBD"   # Grey 400

_MONO   = ("Consolas", 9)
_SMALL  = ("Segoe UI", 8)
_NORMAL = ("Segoe UI", 9)
_BOLD   = ("Segoe UI", 9, "bold")


def _darken_color(hex_color: str) -> str:
    try:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
        f = 0.85
        return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"
    except Exception:
        return hex_color


def _mat_btn(parent, text: str, command, bg: str, fg: str = "#FFFFFF",
             font_size: int = 9, **kw) -> "tk.Button":
    """Flat Material-style button."""
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


def _mat_enable(btn: "tk.Button") -> None:
    btn.configure(state=tk.NORMAL, bg=btn._mat_bg, cursor="hand2")


def _mat_disable(btn: "tk.Button") -> None:
    btn.configure(state=tk.DISABLED, bg=_MAT_DISABLED, cursor="")

_INSTRUCTIONS = """\
Calibration lets the app find the best detection settings for YOUR photo
library by testing many parameter combinations against a small set of photos
you have already sorted manually.

─── How to prepare your calibration data ──────────────────────────────────────

Create a folder with this structure:

  calibration_data/
  ├── groups/
  │   ├── set_001/           ← one sub-folder per known duplicate set
  │   │   ├── best.jpg       ←   put the ORIGINAL (best quality) photo here
  │   │   └── thumb.jpg      ←   put its DUPLICATE / smaller version here
  │   ├── set_002/
  │   │   ├── portrait.jpg
  │   │   └── portrait_small.jpg
  │   └── ...                ← add as many groups as you like (10–30 is ideal)
  └── singles/               ← photos that have NO duplicates  (optional)
      ├── unique_001.jpg
      └── ...

Rules
  • Every group sub-folder must contain at least 2 images.
  • The LARGEST file (by file size) in each group is automatically treated
    as the expected original; all smaller files are expected previews.
  • The singles/ folder is optional but improves calibration accuracy by
    helping the engine tune the false-positive rate.
  • Use real photos from your actual library for the most meaningful results.
  • 10–30 groups covering different scene types gives a good calibration.

─── What gets calibrated ───────────────────────────────────────────────────────

  threshold      How strictly two images must match to be considered duplicates.
  preview_ratio  How much smaller (in each dimension) a "preview" copy must be.

All other settings are kept at their current values during calibration.
"""


# ── Embeddable panel (all UI + logic) ────────────────────────────────────────

class CalibrationPanel(tk.Frame):
    """
    Four-tab calibration wizard as an embeddable tk.Frame.
    Can be placed inside any parent widget (Toplevel, Frame, etc.).
    """

    def __init__(
        self,
        parent: tk.Widget,
        settings: Settings,
        apply_cb: Callable[[int, float], None],
        folder_cb: "Callable[[str], None] | None" = None,
        calibration_applied_cb: "Callable[[int, float], None] | None" = None,
        on_close_cb: "Callable | None" = None,
        start_on_run_tab: bool = False,
    ) -> None:
        super().__init__(parent, bg=_BG)
        self.settings               = settings
        self.apply_cb               = apply_cb
        self.folder_cb              = folder_cb
        self.calibration_applied_cb = calibration_applied_cb
        self._on_close_cb           = on_close_cb
        self._start_on_run_tab      = start_on_run_tab
        self.results                = []
        self._stop_flag             = [False]

        self._calib_folder = tk.StringVar(value=settings.calib_folder or "")
        self._status_var   = tk.StringVar(value="Ready.")
        self._apply_var    = tk.StringVar()
        self._log_text: str = ""

        # Style — use winfo_toplevel() so it works from any parent depth
        try:
            style = ttk.Style(self.winfo_toplevel())
            style.configure("Calib.TNotebook", background=_BG)
            style.configure("Calib.TNotebook.Tab",
                            font=("Segoe UI", 9, "bold"),
                            padding=[10, 4])
            style.map("Calib.TNotebook.Tab",
                      background=[("selected", "#FFFFFF"), ("!selected", "#E3F2FD")],
                      foreground=[("selected", _M_PRIMARY), ("!selected", _M_TEXT2)])
        except Exception:
            pass

        self._build_ui()

        if self._start_on_run_tab:
            # Start on tab 2 (Run) when calibration data is pre-loaded
            self.after(50, lambda: self._nb.select(1))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._nb = ttk.Notebook(self, style="Calib.TNotebook")
        self._nb.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        self._tab_instr   = ttk.Frame(self._nb)
        self._tab_run     = ttk.Frame(self._nb)
        self._tab_results = ttk.Frame(self._nb)
        self._tab_log     = ttk.Frame(self._nb)

        self._nb.add(self._tab_instr,   text="  1. Instructions  ")
        self._nb.add(self._tab_run,     text="  2. Run  ")
        self._nb.add(self._tab_results, text="  3. Results  ")
        self._nb.add(self._tab_log,     text="  4. Log  ")

        self._build_instructions_tab()
        self._build_run_tab()
        self._build_results_tab()
        self._build_log_tab()

    # ── tab 1: instructions ───────────────────────────────────────────────────

    def _build_instructions_tab(self) -> None:
        f = self._tab_instr

        text = tk.Text(
            f, wrap="word", relief="flat",
            bg="#fafafa", padx=14, pady=12,
            font=_MONO, spacing1=1, spacing3=2,
        )
        text.insert("1.0", _INSTRUCTIONS)
        text.configure(state="disabled")

        sb = ttk.Scrollbar(f, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)

        bar = tk.Frame(f, bg=_BG)
        bar.pack(fill="x", padx=12, pady=6)
        _mat_btn(bar, "Next: Select folder and run →",
                 lambda: self._nb.select(1), _M_PRIMARY).pack(side="right")

    # ── tab 2: run ────────────────────────────────────────────────────────────

    def _build_run_tab(self) -> None:
        f = self._tab_run
        pad = dict(padx=16, pady=5)

        # ── folder picker ─────────────────────────────────────────────────
        ttk.Label(f, text="Calibration folder", font=_BOLD).pack(anchor="w", **pad)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=16, pady=2)
        ttk.Entry(row, textvariable=self._calib_folder, width=52).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._browse).pack(
            side="left", padx=(6, 0))

        self._dataset_lbl = ttk.Label(f, text="", foreground="#555", font=_SMALL)
        self._dataset_lbl.pack(anchor="w", padx=20, pady=(0, 4))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=16, pady=8)

        # ── what will be tested ───────────────────────────────────────────
        ttk.Label(f, text="Parameters being calibrated:", font=_BOLD).pack(
            anchor="w", padx=16)
        for line in (
            "• threshold  (similarity sensitivity, tested at: 4 6 8 10 12 14 16 18 20)",
            "• preview_ratio  (min size difference to classify as preview, tested: 0.75–0.95)",
        ):
            ttk.Label(f, text=line, font=_NORMAL).pack(anchor="w", padx=28, pady=1)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=16, pady=8)

        # ── progress ──────────────────────────────────────────────────────
        ttk.Label(f, textvariable=self._status_var, font=_SMALL).pack(
            anchor="w", padx=16, pady=2)
        self._progress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self._progress.pack(fill="x", padx=16, pady=4)

        # ── buttons ───────────────────────────────────────────────────────
        btn_row = tk.Frame(f, bg=_BG)
        btn_row.pack(fill="x", padx=16, pady=6)
        self._start_btn = _mat_btn(btn_row, "▶  Start Calibration", self._start, _M_SUCCESS)
        self._start_btn.pack(side="left")
        self._stop_btn = _mat_btn(btn_row, "■  Stop", self._stop, _M_ERROR)
        self._stop_btn.pack(side="left", padx=8)
        _mat_disable(self._stop_btn)

        self._calib_folder.trace_add("write", self._on_folder_change)

    # ── tab 3: results ────────────────────────────────────────────────────────

    def _build_results_tab(self) -> None:
        f = self._tab_results

        ttk.Label(f, text="Results — best combinations listed first", font=_BOLD).pack(
            anchor="w", padx=16, pady=(10, 4))

        # treeview
        cols = ("rnd", "threshold", "preview_ratio", "score",
                "groups", "originals", "previews", "fp", "variant")
        self._tree = ttk.Treeview(f, columns=cols, show="headings", height=14)

        col_cfg = [
            ("rnd",          "Rnd",           38),
            ("threshold",    "Threshold",     80),
            ("preview_ratio","Ratio",         68),
            ("score",        "Score %",       72),
            ("groups",       "Groups",        72),
            ("originals",    "Orig OK",       68),
            ("previews",     "Prev OK",       68),
            ("fp",           "False+",        55),
            ("variant",      "Variant",      115),
        ]
        for cid, head, width in col_cfg:
            self._tree.heading(cid, text=head,
                               command=lambda c=cid: self._sort_by(c))
            self._tree.column(cid, width=width, anchor="center")

        sb = ttk.Scrollbar(f, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=4)
        self._tree.pack(fill="both", expand=True, padx=16, pady=4)

        # tag for top row highlight (Material Green 50)
        self._tree.tag_configure("best", background="#E8F5E9")

        # action bar
        bar = tk.Frame(f, bg=_BG)
        bar.pack(fill="x", padx=16, pady=6)
        _mat_btn(bar, "✓  Apply Selected", self._apply_selected, _M_PRIMARY).pack(side="left")
        _mat_btn(bar, "★  Apply Best", self._apply_best, _M_SUCCESS).pack(side="left", padx=8)
        tk.Label(
            bar, textvariable=self._apply_var,
            fg=_M_SUCCESS, bg=_BG, font=_NORMAL,
        ).pack(side="left", padx=8)

    # ── event handlers ────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.askdirectory(
            title="Select calibration data folder",
            parent=self.winfo_toplevel(),
        )
        if path:
            self._calib_folder.set(path)

    def _on_folder_change(self, *_) -> None:
        folder = self._calib_folder.get().strip()
        if not folder:
            self._dataset_lbl.configure(text="")
            return
        from calibrator import validate_calibration_folder
        valid, msg = validate_calibration_folder(Path(folder))
        colour = "#555" if valid else "#cc0000"
        self._dataset_lbl.configure(text=msg, foreground=colour)
        if self.folder_cb:
            try:
                self.folder_cb(folder)
            except Exception:
                pass

    def _start(self) -> None:
        folder = self._calib_folder.get().strip()
        if not folder:
            self._status_var.set("Please select a calibration folder first.")
            return

        from calibrator import validate_calibration_folder
        valid, msg = validate_calibration_folder(Path(folder))
        if not valid:
            self._status_var.set(f"Cannot start: {msg}")
            return

        self._stop_flag[0] = False
        _mat_disable(self._start_btn)
        _mat_enable(self._stop_btn)
        self._status_var.set("Starting…")
        self._progress["value"] = 0
        self._apply_var.set("")

        def _thread() -> None:
            from calibrator import run_calibration

            def _progress(msg: str, cur: int, tot: int) -> None:
                pct = int(cur / tot * 100) if tot > 0 else 0
                self.after(0, lambda m=msg, p=pct: (
                    self._status_var.set(m),
                    self._progress.configure(value=p),
                ))

            results, log = run_calibration(
                Path(folder),
                self.settings,
                progress_cb=_progress,
                stop_flag=self._stop_flag,
            )
            self.after(0, lambda: self._on_done(results, log))

        threading.Thread(target=_thread, daemon=True).start()

    def _stop(self) -> None:
        self._stop_flag[0] = True
        _mat_disable(self._stop_btn)
        self._status_var.set("Stopping…")

    def _on_done(self, results: list, log=None) -> None:
        self.results = results
        _mat_enable(self._start_btn)
        _mat_disable(self._stop_btn)

        if not results:
            self._status_var.set(
                "No results — verify the folder contains valid groups."
            )
            return

        n = len(results)
        best = results[0]
        self._status_var.set(
            f"Done. {n} combinations tested across 3 rounds. "
            f"Best: threshold={best.threshold}  "
            f"preview_ratio={best.preview_ratio:.3f}  "
            f"→ {best.score * 100:.1f}%"
        )
        self._progress["value"] = 100
        self._populate_results(results)

        if log is not None:
            from calibrator import format_log
            self._log_text = format_log(log)
            self._set_log_text(self._log_text)

        self._nb.select(2)

    # ── results table ─────────────────────────────────────────────────────────

    def _populate_results(self, results: list) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

        for i, r in enumerate(results):
            tag = ("best",) if i == 0 else ()
            self._tree.insert(
                "", "end",
                values=(
                    r.round_number,
                    r.threshold,
                    f"{r.preview_ratio:.3f}",
                    f"{r.score * 100:.1f}%",
                    f"{r.groups_found}/{r.groups_total}",
                    f"{r.originals_correct}/{r.originals_total}",
                    f"{r.previews_correct}/{r.previews_total}",
                    str(r.false_positives),
                    r.variant_label or "",
                ),
                tags=tag,
            )

    _sort_asc: dict[str, bool] = {}

    def _sort_by(self, col: str) -> None:
        """Toggle-sort the results table by a column."""
        asc = not self._sort_asc.get(col, False)
        self._sort_asc[col] = asc

        col_map = {
            "rnd":          lambda r: r.round_number,
            "threshold":    lambda r: r.threshold,
            "preview_ratio":lambda r: r.preview_ratio,
            "score":        lambda r: r.score,
            "groups":       lambda r: r.groups_found,
            "originals":    lambda r: r.originals_correct,
            "previews":     lambda r: r.previews_correct,
            "fp":           lambda r: r.false_positives,
            "variant":      lambda r: r.variant_label,
        }
        key = col_map.get(col)
        if key and self.results:
            self.results.sort(key=key, reverse=not asc)
            self._populate_results(self.results)

    # ── log tab ───────────────────────────────────────────────────────────────

    def _build_log_tab(self) -> None:
        f = self._tab_log

        header = ttk.Frame(f)
        header.pack(fill="x", padx=16, pady=(10, 4))
        ttk.Label(header, text="Diagnostic log — per-pair algorithm analysis for the best result",
                  font=_BOLD).pack(side="left")

        btn_bar = tk.Frame(f, bg=_BG)
        btn_bar.pack(fill="x", padx=16, pady=(0, 4))
        _mat_btn(btn_bar, "Copy Log to Clipboard", self._copy_log, "#757575").pack(side="left")
        tk.Label(btn_bar,
                 text="  Paste this log when reporting algorithm issues",
                 fg=_M_TEXT2, bg=_BG, font=_SMALL).pack(side="left", padx=8)

        self._log_widget = tk.Text(
            f, wrap="none", relief="flat",
            bg="#fafafa", padx=10, pady=8,
            font=_MONO, state="disabled",
        )
        vsb = ttk.Scrollbar(f, orient="vertical",   command=self._log_widget.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self._log_widget.xview)
        self._log_widget.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._log_widget.pack(fill="both", expand=True, padx=(16, 0), pady=(0, 4))

        self._set_log_text("Run calibration to generate the diagnostic log.")

    def _set_log_text(self, text: str) -> None:
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.insert("1.0", text)
        self._log_widget.configure(state="disabled")

    def _copy_log(self) -> None:
        text = self._log_text or self._log_widget.get("1.0", "end")
        self.clipboard_clear()
        self.clipboard_append(text)
        self._apply_var.set("Log copied to clipboard.")

    # ── apply ─────────────────────────────────────────────────────────────────

    def _apply_best(self) -> None:
        if not self.results:
            messagebox.showinfo("No results", "Run calibration first.",
                                parent=self.winfo_toplevel())
            return
        r = self.results[0]
        self._do_apply(r.threshold, r.preview_ratio)

    def _apply_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            self._apply_var.set("Select a row first.")
            return
        idx = self._tree.index(sel[0])
        if idx < len(self.results):
            r = self.results[idx]
            self._do_apply(r.threshold, r.preview_ratio)

    def _do_apply(self, threshold: int, preview_ratio: float) -> None:
        self.apply_cb(threshold, preview_ratio)
        if self.calibration_applied_cb:
            try:
                self.calibration_applied_cb(threshold, preview_ratio)
            except Exception:
                pass
        self._apply_var.set(
            f"Applied: threshold={threshold},  preview_ratio={preview_ratio:.2f}"
        )

    # ── close / stop ──────────────────────────────────────────────────────────

    def stop_calibration(self) -> None:
        """Signal the running calibration thread to stop."""
        self._stop_flag[0] = True


# ── Standalone Toplevel window wrapping the panel ────────────────────────────

class CalibrationWindow(tk.Toplevel):
    """Standalone calibration window (wraps CalibrationPanel)."""

    def __init__(
        self,
        parent: tk.Widget,
        settings: Settings,
        apply_cb: Callable[[int, float], None],
        folder_cb: "Callable[[str], None] | None" = None,
        calibration_applied_cb: "Callable[[int, float], None] | None" = None,
    ) -> None:
        super().__init__(parent)
        self.title("Calibrate Detection Settings")
        self.geometry("760x600")
        self.minsize(640, 480)
        self.resizable(True, True)
        self.grab_set()
        self.configure(bg=_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._panel = CalibrationPanel(
            self,
            settings,
            apply_cb=apply_cb,
            folder_cb=folder_cb,
            calibration_applied_cb=calibration_applied_cb,
        )
        self._panel.pack(fill="both", expand=True)

    def _on_close(self) -> None:
        self._panel.stop_calibration()
        self.destroy()
