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

# ── Material Design colour palette (light defaults, overwritten by _apply_theme) ──
_BG             = "#F5F5F5"
_SURFACE        = "#FFFFFF"
_M_PRIMARY      = "#1565C0"
_M_SUCCESS      = "#2E7D32"
_M_ERROR        = "#C62828"
_M_DIVIDER      = "#E0E0E0"
_M_TEXT1        = "#212121"
_M_TEXT2        = "#616161"
_MAT_DISABLED   = "#BDBDBD"
_M_DISABLED_FG  = "#838387"


def _apply_theme(dark: bool = False) -> None:
    """Overwrite module colours from the theme palette."""
    global _BG, _SURFACE, _M_PRIMARY, _M_SUCCESS, _M_ERROR
    global _M_DIVIDER, _M_TEXT1, _M_TEXT2, _MAT_DISABLED, _M_DISABLED_FG
    import theme as _t
    p = _t.get_palette(dark)
    _BG           = p["BG"]
    _SURFACE      = p["CARD_BG"]
    _M_PRIMARY    = p["ACCENT"]
    _M_SUCCESS    = p["SUCCESS"]
    _M_ERROR      = p["ERROR"]
    _M_DIVIDER    = p["DIVIDER"]
    _M_TEXT1      = p["TEXT1"]
    _M_TEXT2      = p["TEXT2"]
    _MAT_DISABLED = p["DISABLED"]
    _M_DISABLED_FG = p["DISABLED_FG"]

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
        relief=tk.FLAT, bd=0,
        font=("Segoe UI", font_size, "bold"), cursor="hand2", **kw,
    )
    # Apply colors after creation (ttkbootstrap patches tk.Button constructor)
    btn.configure(bg=bg, fg=fg, activebackground=_darken_color(bg),
                  activeforeground=fg, padx=12, pady=5)
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
    return btn


def _mat_enable(btn: "tk.Button") -> None:
    btn.configure(state=tk.NORMAL, bg=btn._mat_bg, fg=btn._mat_fg,
                  activebackground=_darken_color(btn._mat_bg),
                  activeforeground=btn._mat_fg, cursor="hand2")


def _mat_disable(btn: "tk.Button") -> None:
    btn.configure(state=tk.DISABLED, bg=_MAT_DISABLED, fg=_M_DISABLED_FG, cursor="")

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
        self._last_log              = None

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

        # ── parameters overview (scrollable) ──────────────────────────────
        ttk.Label(f, text="Calibration rounds and parameters:", font=_BOLD).pack(
            anchor="w", padx=16)

        params_outer = tk.Frame(f, bg=_BG)
        params_outer.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        self._params_text = tk.Text(
            params_outer, wrap="none", relief="flat",
            bg="#fafafa", padx=10, pady=6,
            font=_MONO, state="disabled", height=12,
        )
        params_vsb = ttk.Scrollbar(params_outer, orient="vertical",
                                   command=self._params_text.yview)
        params_hsb = ttk.Scrollbar(params_outer, orient="horizontal",
                                   command=self._params_text.xview)
        self._params_text.configure(yscrollcommand=params_vsb.set,
                                    xscrollcommand=params_hsb.set)
        params_vsb.pack(side="right", fill="y")
        params_hsb.pack(side="bottom", fill="x")
        self._params_text.pack(fill="both", expand=True)

        self._refresh_params_text()

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

    def _refresh_params_text(self, log=None) -> None:
        """Build and display the full calibration parameters overview in the Run tab."""
        try:
            from calibrator import (
                ROUND1_THRESHOLDS, ROUND1_RATIOS,
                ROUND4_SERIES_THRESHOLD_FACTORS, ROUND4_BRIGHTNESS_MAX_DIFFS,
                ROUND4_HIST_MIN_SIMILARITIES, ROUND4_AR_TOLERANCE_PCTS,
                ROUND4_CF_THRESHOLD_FACTORS, ROUND4_DARK_THRESHOLDS,
                ROUND4_DARK_TIGHTEN_FACTORS, ROUND4_SERIES_TOLERANCE_PCTS,
                _ROUND4_IMPACT_THR,
            )
        except Exception:
            return

        s = self.settings
        lines: list[str] = []

        # ── constant settings ──────────────────────────────────────────
        lines.append("── Constant settings (held fixed during calibration) ──────────────────")
        constants = [
            ("ar_tolerance_pct",              s.ar_tolerance_pct),
            ("brightness_max_diff",           s.brightness_max_diff),
            ("use_dual_hash",                 s.use_dual_hash),
            ("use_histogram",                 s.use_histogram),
            ("hist_min_similarity",           s.hist_min_similarity),
            ("dark_protection",               s.dark_protection),
            ("dark_threshold",                s.dark_threshold),
            ("dark_tighten_factor",           s.dark_tighten_factor),
            ("series_tolerance_pct",          s.series_tolerance_pct),
            ("series_threshold_factor",       s.series_threshold_factor),
            ("disable_series_detection",      s.disable_series_detection),
            ("cross_format_threshold_factor", getattr(s, "cross_format_threshold_factor", 5.0)),
        ]
        for k, v in constants:
            lines.append(f"  {k:<36} = {v}")

        # ── round 1 ───────────────────────────────────────────────────
        lines.append("")
        lines.append("── Round 1: Coarse threshold × preview_ratio grid ─────────────────────")
        thr_str = "  ".join(str(t) for t in ROUND1_THRESHOLDS)
        rat_str = "  ".join(f"{r:.2f}" for r in ROUND1_RATIOS)
        n_r1 = len(ROUND1_THRESHOLDS) * len(ROUND1_RATIOS)
        lines.append(f"  threshold     ({len(ROUND1_THRESHOLDS)} values): {thr_str}")
        lines.append(f"  preview_ratio ({len(ROUND1_RATIOS)} values): {rat_str}")
        lines.append(f"  Total: {n_r1} combinations")

        # ── round 2 ───────────────────────────────────────────────────
        lines.append("")
        lines.append("── Round 2: Fine preview_ratio around best Round 1 result ─────────────")
        lines.append("  Sweeps ±0.01 … ±0.04 around best R1 ratio for top-5 thresholds")
        lines.append("  (~8 new ratios × top-5 thresholds  ≈ up to 40 additional combos)")

        # ── round 3 ───────────────────────────────────────────────────
        lines.append("")
        lines.append("── Round 3: Feature flag variants (using best threshold / ratio) ───────")
        feature_variants = [
            ("no_series_detect",  "disable_series_detection = True"),
            ("no_dual_hash",      "use_dual_hash = False"),
            ("no_histogram",      "use_histogram = False"),
            ("no_dark_protect",   "dark_protection = False"),
            ("loose_AR",          f"ar_tolerance_pct = {s.ar_tolerance_pct * 2:.1f}  (doubled)"),
            ("tight_AR",          f"ar_tolerance_pct = {max(1.0, s.ar_tolerance_pct / 2):.1f}  (halved)"),
            ("loose_brightness",  f"brightness_max_diff = {s.brightness_max_diff * 2:.1f}  (doubled)"),
        ]
        for label, desc in feature_variants:
            lines.append(f"  {label:<22} → {desc}")

        # ── round 4 ───────────────────────────────────────────────────
        lines.append("")
        lines.append(
            f"── Round 4: Conditional sweeps  "
            f"(triggered when Round 3 impact ≥ {_ROUND4_IMPACT_THR * 100:.1f}%) ──────"
        )
        r4_params = [
            ("series_threshold_factor",       "always",
             ROUND4_SERIES_THRESHOLD_FACTORS),
            ("hist_min_similarity",           "if no_histogram impactful",
             ROUND4_HIST_MIN_SIMILARITIES),
            ("brightness_max_diff",           "if loose_brightness impactful",
             ROUND4_BRIGHTNESS_MAX_DIFFS),
            ("ar_tolerance_pct",              "if loose_AR or tight_AR impactful",
             ROUND4_AR_TOLERANCE_PCTS),
            ("cross_format_threshold_factor", "if RAW files present in data",
             ROUND4_CF_THRESHOLD_FACTORS),
            ("dark_threshold",                "if no_dark_protect impactful",
             ROUND4_DARK_THRESHOLDS),
            ("dark_tighten_factor",           "if no_dark_protect impactful",
             ROUND4_DARK_TIGHTEN_FACTORS),
            ("series_tolerance_pct",          "if no_series_detect impactful",
             ROUND4_SERIES_TOLERANCE_PCTS),
        ]
        swept_params: set[str] = set()
        if log:
            swept_params = {name for name, _ in log.parameter_sweeps}
        for param, condition, vals in r4_params:
            val_str = "  ".join(str(v) for v in vals)
            if log:
                status = "  ✓ swept" if param in swept_params else "  — skipped"
            else:
                status = ""
            lines.append(f"  {param:<36}  ({condition}){status}")
            lines.append(f"    values: {val_str}")

        # ── round 4 optimized results (shown after calibration) ───────
        if log and log.optimized_params:
            lines.append("")
            lines.append("── Round 4 Results: Recommended parameter values ──────────────────────")
            for param, val in log.optimized_params.items():
                lines.append(f"  {param:<36} → {val}")

        text = "\n".join(lines)
        self._params_text.configure(state="normal")
        self._params_text.delete("1.0", "end")
        self._params_text.insert("1.0", text)
        self._params_text.configure(state="disabled")

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
        rounds_run = log.rounds_run if log is not None else "?"
        self._status_var.set(
            f"Done. {n} combinations tested across {rounds_run} rounds. "
            f"Best: threshold={best.threshold}  "
            f"preview_ratio={best.preview_ratio:.3f}  "
            f"→ {best.score * 100:.1f}%"
        )
        self._progress["value"] = 100
        self._populate_results(results)

        if log is not None:
            from calibrator import format_log
            self._last_log = log
            self._log_text = format_log(log)
            self._set_log_text(self._log_text)
            self._refresh_params_text(log)

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
        self._do_apply(r.threshold, r.preview_ratio,
                       optimized_params=(self._last_log.optimized_params
                                         if self._last_log else None))

    def _apply_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            self._apply_var.set("Select a row first.")
            return
        idx = self._tree.index(sel[0])
        if idx < len(self.results):
            r = self.results[idx]
            self._do_apply(r.threshold, r.preview_ratio)

    def _do_apply(self, threshold: int, preview_ratio: float,
                  optimized_params: "dict | None" = None) -> None:
        self.apply_cb(threshold, preview_ratio)

        # Apply Round 4 optimized params (e.g. series_threshold_factor=0.5) directly
        # to the live settings object so they take effect immediately.
        extra_applied: list[str] = []
        if optimized_params:
            for param, val in optimized_params.items():
                try:
                    setattr(self.settings, param, val)
                    extra_applied.append(f"{param}={val}")
                except Exception:
                    pass

        if self.calibration_applied_cb:
            try:
                self.calibration_applied_cb(threshold, preview_ratio)
            except Exception:
                pass

        msg = f"Applied: threshold={threshold},  preview_ratio={preview_ratio:.2f}"
        if extra_applied:
            msg += "  |  " + ",  ".join(extra_applied)
        self._apply_var.set(msg)

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
