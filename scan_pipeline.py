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


def _in_folder(path: Path, resolved_folder: Path) -> bool:
    """True when *path* lies inside *resolved_folder* (which must already be
    resolved)."""
    try:
        Path(path).resolve().relative_to(resolved_folder)
        return True
    except ValueError:
        return False


def folders_are_nested(a: Path, b: Path) -> bool:
    """True when one folder contains the other (either direction).

    Compare Scan classifies files by folder membership, so overlapping folders
    make a file belong to both roles. Callers warn the user before scanning;
    see reclassify_compare_groups for how the overlap is then resolved.
    Identical folders are not "nested" -- that case is rejected separately.
    """
    a_res, b_res = Path(a).resolve(), Path(b).resolve()
    if a_res == b_res:
        return False
    return _in_folder(a_res, b_res) or _in_folder(b_res, a_res)


def inner_folder_of(a: Path, b: Path) -> "Optional[Path]":
    """Return whichever of *a* / *b* is nested inside the other, else None.

    The inner folder is the more specific one, which is the side that wins when
    a file belongs to both (see reclassify_compare_groups).
    """
    a_res, b_res = Path(a).resolve(), Path(b).resolve()
    if a_res == b_res:
        return None
    if _in_folder(a_res, b_res):
        return a_res
    if _in_folder(b_res, a_res):
        return b_res
    return None


def reclassify_compare_groups(groups: list, main_folder: Path,
                              check_folder: Path) -> "tuple[list, list]":
    """Re-split Compare Scan groups into (cross_folder, within_check).

    THIS IS THE COMPARE SCAN DATA-SAFETY BOUNDARY. Group members are relabeled
    so that:

      * files from the Main (reference) folder become ``originals`` -- the UI
        never offers originals for trashing;
      * files from the Check folder become ``previews`` -- the trash candidates.

    Groups with no Check-folder member are dropped entirely: a duplicate pair
    living only inside Main is not something a Compare Scan may act on.

    Mutates the passed groups in place (as the original inline code did) and
    returns the two buckets so callers can count them separately.

    NESTED FOLDERS (#2298). When one folder contains the other, a file under
    the inner folder is inside both, and previously landed in BOTH lists --
    shown as the surviving original while also being offered for trashing.
    Classification is now exclusive: **the more deeply nested folder wins**,
    because it is the more specific statement of intent. That matches what the
    user means in both directions:

      * Check inside Main  (Main=Photos, Check=Photos/2024): files under 2024
        are the candidates to clean -> Check wins, they stay trashable.
      * Main inside Check  (Main=Photos/2024, Check=Photos): files under 2024
        are the reference -> Main wins, they stay protected.

    Either way no file can be an original and a trash candidate at once.
    """
    main_res = Path(main_folder).resolve()
    check_res = Path(check_folder).resolve()

    # Resolve the overlap once per call rather than per member.
    check_inside_main = _in_folder(check_res, main_res) and check_res != main_res
    main_inside_check = _in_folder(main_res, check_res) and check_res != main_res

    def _roles(record) -> "tuple[bool, bool]":
        in_main = _in_folder(record.path, main_res)
        in_check = _in_folder(record.path, check_res)
        if in_main and in_check:
            # Overlap: award the file to the deeper (more specific) folder.
            if check_inside_main:
                in_main = False
            elif main_inside_check:
                in_check = False
        return in_main, in_check

    cross_groups: list = []
    within_check_groups: list = []

    for g in groups:
        members = list(g.originals) + list(g.previews)
        roles = [(r, *_roles(r)) for r in members]
        from_main = [r for r, in_m, _ in roles if in_m]
        from_check = [r for r, _, in_c in roles if in_c]

        if from_main and from_check:
            # Cross-folder match: Main copies are the keepers.
            g.originals = from_main
            g.previews = from_check
            cross_groups.append(g)
        elif not from_main and len(from_check) > 1:
            # Duplicates that exist only inside Check: keep the first, offer
            # the rest for trashing.
            g.originals = from_check[:1]
            g.previews = from_check[1:]
            within_check_groups.append(g)
        # else: Main-only (or a lone Check file) -- not a Compare Scan result.

    return cross_groups, within_check_groups


def compute_solo_originals(records: list, groups: list) -> list:
    """Return the records that landed in no duplicate group.

    These are the "unique" files the review screen shows separately: scanned,
    hashed, and matched against everything else without finding a partner.

    Comparison is on resolved paths, so a record reached via a different but
    equivalent path (junction, trailing separator, case difference on Windows)
    is still recognised as grouped and is not reported as unique.
    """
    grouped = {
        Path(r.path).resolve()
        for g in groups
        for r in list(g.originals) + list(g.previews)
    }
    return [r for r in records if Path(r.path).resolve() not in grouped]


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
