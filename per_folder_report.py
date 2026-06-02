"""
per_folder_report.py — Mode B (per-folder) page rendering for the in-app
merge report.

Extracted from report_viewer.py to keep that module focused.  The single
public entry point, :func:`build_per_folder_page`, renders one source-folder
page for a ReportViewer running in ``"per_folder"`` pagination mode.  It reads
the viewer's ``_folder_labels`` / ``_folder_group_map`` / ``_groups`` and
delegates per-group rendering back to ``viewer._build_group_card``.

Theme tokens and the Tk helper wrappers live in report_viewer; they are
imported lazily inside the function so importing this module never creates an
import cycle with report_viewer.
"""
from __future__ import annotations

import tkinter as tk
import traceback


def build_per_folder_page(viewer, page: int) -> None:
    """Render one source-folder page for Mode B per-folder pagination.

    Each page shows:
    - A folder-header card with the source path and a summary line
      (N files to copy to main, M internal duplicates to trash).
    - All DuplicateGroup cards that belong to that source folder.

    When a folder has no groups, shows a "no duplicate groups" message for
    that folder instead.

    *viewer* is the owning ReportViewer instance; this function reads its
    ``_folder_labels`` / ``_folder_group_map`` / ``_groups`` / ``_inner_frame``
    and calls back into ``viewer._build_group_card``.
    """
    from report_viewer import (
        _tkframe, _tklabel,
        _M_BG, _M_TEXT1, _M_TEXT2, _M_TEXT3,
        _M_SURFACE, _M_PRIMARY, _M_PRIMARY_TINT,
    )

    if not viewer._folder_labels:
        _tklabel(
            viewer._inner_frame, bg=_M_BG, fg=_M_TEXT3,
            text="No source folders in merge plan.",
            font=("Segoe UI", 13),
        ).pack(pady=40)
        return

    page = max(0, min(page, len(viewer._folder_labels) - 1))
    folder_label = viewer._folder_labels[page]
    folder_grps  = viewer._folder_group_map.get(folder_label, [])

    # ── Folder header card ────────────────────────────────────────────
    hdr_outer = _tkframe(viewer._inner_frame, bg=_M_BG, padx=12, pady=8)
    hdr_outer.pack(fill=tk.X)

    hdr_card_wrap = _tkframe(hdr_outer, bg=_M_SURFACE, highlightthickness=0)
    hdr_card_wrap.pack(fill=tk.X)

    # Accent left border
    _tkframe(hdr_card_wrap, bg=_M_PRIMARY, width=4).pack(side=tk.LEFT, fill=tk.Y)

    hdr_card = _tkframe(hdr_card_wrap, bg=_M_PRIMARY_TINT)
    hdr_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    _hdr_row = _tkframe(hdr_card, bg=_M_PRIMARY_TINT)
    _hdr_row.pack(fill=tk.X, padx=12, pady=(8, 2))

    _tklabel(
        _hdr_row, bg=_M_PRIMARY_TINT, fg=_M_PRIMARY,
        text=f"Source folder  ·  page {page + 1} of {len(viewer._folder_labels)}",
        font=("Segoe UI", 8, "bold"),
    ).pack(side=tk.LEFT)

    _tklabel(
        hdr_card, bg=_M_PRIMARY_TINT, fg=_M_TEXT1,
        text=folder_label,
        font=("Segoe UI", 9, "bold"),
        anchor=tk.W, wraplength=900,
    ).pack(fill=tk.X, padx=12, pady=(0, 4))

    # Summary: count originals-to-copy and duplicates-to-trash
    n_groups = len(folder_grps)
    # originals = groups[i].originals (files to copy to main)
    # previews  = groups[i].previews (internal duplicates to trash in Mode B)
    n_to_copy = sum(len(g.originals or []) for g in folder_grps)
    n_internal_dups = sum(len(g.previews or []) for g in folder_grps)

    parts = []
    if n_to_copy:
        parts.append(f"{n_to_copy} file(s) to copy to main")
    if n_internal_dups:
        parts.append(f"{n_internal_dups} internal duplicate(s) to trash")
    if n_groups == 0:
        parts.append("No duplicate groups")
    summary_text = "  ·  ".join(parts) if parts else "No files"

    _tklabel(
        hdr_card, bg=_M_PRIMARY_TINT, fg=_M_TEXT2,
        text=summary_text,
        font=("Segoe UI", 8),
        anchor=tk.W,
    ).pack(fill=tk.X, padx=12, pady=(0, 8))

    # ── Group cards for this folder ───────────────────────────────────
    # We need to map these folder-local groups to their global indices in
    # viewer._groups so that _ensure_group_vars / _build_group_card work
    # correctly.  Build a lookup: group object id → global index (once per render).
    grp_id_to_global: dict[int, int] = {
        id(g): i for i, g in enumerate(viewer._groups)
    }

    if not folder_grps:
        _tklabel(
            viewer._inner_frame, bg=_M_BG, fg=_M_TEXT3,
            text="No duplicate groups in this source folder.",
            font=("Segoe UI", 11),
        ).pack(pady=20)
    else:
        for grp in folder_grps:
            # grp_id_to_global is keyed by id(grp), which IS the `is`
            # comparison — if the lookup misses, no linear fallback would
            # succeed either.  Skip groups that aren't in the global list.
            global_idx = grp_id_to_global.get(id(grp))
            if global_idx is None:
                continue
            try:
                viewer._build_group_card(global_idx, grp)
            except Exception as exc:
                tb_str = traceback.format_exc()
                print(tb_str)
                try:
                    err_frame = _tkframe(viewer._inner_frame, bg="#FFEBEE",
                                         pady=8, padx=12)
                    err_frame.pack(fill=tk.X, padx=12, pady=4)
                    _tklabel(err_frame, bg="#FFEBEE", fg="#C62828",
                             text=f"Group failed to render: {exc}",
                             font=("Segoe UI", 9)).pack(anchor=tk.W)
                except Exception:
                    pass
