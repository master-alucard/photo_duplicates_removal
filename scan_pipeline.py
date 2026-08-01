"""
scan_pipeline.py — Phase implementations shared by the regular scan and the
Compare Scan.

Stage 4 of projects/ImageDeduper/docs/refactor-plan-2026-07.md.

The two scan workers historically carried their own copy of every phase, which
is why the same defect had to be fixed twice (#159/#161, then #2149). Each phase
lands here as one implementation both paths call, so a fix can only be made in
one place.

Phases are plain functions, not App methods: they take their flags and callbacks
as arguments and touch no Tk, so they are unit-testable without a GUI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from library import (
    inject_records_into_cache,
    load_scan_cache,
    writeback_scan_results,
)
from scanner import collect_images


def collect_folder_records(
    folder: Path,
    skip_paths: "set[Path]",
    settings: Any,
    *,
    progress_cb: Optional[Callable] = None,
    stop_flag: Optional[list] = None,
    pause_flag: Optional[list] = None,
    failed_paths: Optional[list] = None,
    trust: bool = False,
    resume_records: Optional[list] = None,
    writeback: bool = True,
) -> list:
    """Hash every image in *folder* and return the records.

    Wraps the three steps both scan paths perform per folder: load the library
    cache, hash, and write results back.

    Args:
        trust: skip per-file staleness checks. Pass
            ``LibraryOptions.effective_trust`` -- never a raw user flag, or
            browse mode could serve stale hashes.
        resume_records: records already hashed by an interrupted run. When
            given they are injected into the cache and trusted unconditionally
            (they were just computed), overriding *trust*.
        writeback: persist results for future scans. The regular scan's resume
            path passes False -- see the note in the caller; kept as a parameter
            rather than normalized so this stays a behavior-preserving change.
    """
    cache, _ = load_scan_cache(folder)

    if resume_records is not None:
        cache = inject_records_into_cache(cache, resume_records)
        trust_library = True
    else:
        trust_library = trust

    records = collect_images(
        folder, skip_paths, settings,
        progress_cb=progress_cb,
        stop_flag=stop_flag,
        pause_flag=pause_flag,
        failed_paths=failed_paths,
        library_cache=cache,
        trust_library=trust_library,
    )

    if writeback:
        writeback_scan_results(folder, records)

    return records
