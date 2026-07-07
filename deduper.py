"""
deduper.py — command-line mode for Image Deduper (Mantis #130).

Runs the exact same scan pipeline as the GUI (scanner.collect_images ->
scanner.find_groups) and, optionally, the exact same trash move
(mover.move_groups) without opening a window.

Usage:
    python deduper.py --scan <folder> [--threshold 0-100] [--auto-move-trash]
                      [--out <folder>]

Behaviour:
    * Without --auto-move-trash the run is a DRY RUN: duplicate groups are
      reported on stdout and no file is touched.
    * With --auto-move-trash, duplicate files (the copies the GUI would
      pre-select for trashing) are moved to <out>/trash/ and the move is
      logged to <out>/operations_log.json, so the GUI's Revert keeps working.
    * Originals are never touched.  Ambiguous groups are never auto-moved
      (mover.move_groups skips them) — they are reported for manual review.
    * --threshold is a similarity percentage (0-100), per the ticket example
      "--threshold 90".  Internally the app uses a 64-bit pHash Hamming
      distance (settings.threshold, GUI default 2 ~= 97% similarity), so the
      CLI converts:  distance = round((100 - pct) * 64 / 100).
      Example: 90 -> 6 bits.  When --threshold is omitted, the value from
      settings.json is used unchanged.
    * All other tuning comes from settings.json, exactly like the GUI, and
      the persistent library hash cache is reused when available.
    * Image formats only — video duplicate detection stays GUI-only.

Exit codes:
    0 — scan completed (duplicates found or not)
    1 — fatal error, or --auto-move-trash finished with move errors
    2 — bad command-line usage (argparse)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from config import Settings, load_settings
from mover import move_groups, ops_log_path
from scanner import DuplicateGroup, collect_images, find_groups

SETTINGS_PATH = Path(__file__).parent / "settings.json"

_PHASH_BITS = 64


def similarity_to_hamming(pct: int) -> int:
    """Convert a 0-100 similarity percentage to a pHash Hamming distance.

    100% similarity -> 0 bits of allowed difference; 0% -> all 64 bits.
    The ticket example --threshold 90 maps to round(10% of 64) = 6 bits.
    """
    bits = round((100 - pct) * _PHASH_BITS / 100)
    return max(0, min(_PHASH_BITS, bits))


def _load_library_cache(folder: Path) -> dict:
    """Load the persistent hash cache for *folder*, mirroring the GUI scan
    worker.  Returns an empty dict when the library is unavailable."""
    try:
        from library import Library, get_library_dir
        lib = Library.load(get_library_dir())
        return lib.load_cache_merged(str(folder.resolve()))
    except Exception as exc:
        print(f"[warn] library cache unavailable: {exc}", file=sys.stderr)
        return {}


def _make_progress_cb():
    """Console progress callback (ProgressCb signature), throttled to at most
    one line per second per phase, written to stderr so stdout stays a clean
    machine-readable report."""
    state = {"phase": "", "last": 0.0}

    def cb(msg: str, done: int, total: int, phase: str) -> None:
        now = time.monotonic()
        phase_changed = phase != state["phase"]
        finished = total > 0 and done >= total
        if not (phase_changed or finished or now - state["last"] >= 1.0):
            return
        state["phase"] = phase
        state["last"] = now
        if total > 0:
            print(f"[{phase}] {done:,}/{total:,}", file=sys.stderr)
        else:
            print(f"[{phase}] {msg}", file=sys.stderr)

    return cb


def _fmt_size(nbytes: int) -> str:
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def _print_groups(groups: list[DuplicateGroup]) -> tuple[int, int, int]:
    """Print every duplicate group to stdout.

    Returns (movable_files, movable_bytes, ambiguous_groups) where "movable"
    counts the previews of non-ambiguous groups — exactly the set
    mover.move_groups would move.
    """
    movable_files = 0
    movable_bytes = 0
    ambiguous = 0

    for group in groups:
        tags = []
        if group.is_series:
            tags.append("series")
        if group.is_ambiguous:
            tags.append("ambiguous: manual review, not auto-moved")
            ambiguous += 1
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        n_files = len(group.originals) + len(group.previews)
        print(f"\nGroup {group.group_id or '?'} ({n_files} files){tag_str}")
        for rec in group.originals:
            print(f"  keep : {rec.path}  ({rec.width}x{rec.height}, "
                  f"{_fmt_size(rec.file_size)})")
        for rec in group.previews:
            print(f"  trash: {rec.path}  ({rec.width}x{rec.height}, "
                  f"{_fmt_size(rec.file_size)})")
            if not group.is_ambiguous:
                movable_files += 1
                movable_bytes += rec.file_size

    return movable_files, movable_bytes, ambiguous


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deduper.py",
        description="Image Deduper CLI: scan a folder for duplicate images "
                    "and optionally move the duplicates to a trash folder. "
                    "Without --auto-move-trash this is a dry run: duplicates "
                    "are reported and nothing is moved.",
    )
    parser.add_argument(
        "--scan", required=True, metavar="FOLDER",
        help="Directory to scan for duplicate images (recursive per "
             "settings.json, default recursive).",
    )
    parser.add_argument(
        "--threshold", type=int, default=None, metavar="PCT",
        help="Similarity threshold as a percentage, 0-100 (e.g. 90). "
             "Converted internally to a pHash Hamming distance: "
             "round((100 - PCT) * 64 / 100). Omitted = use settings.json.",
    )
    parser.add_argument(
        "--auto-move-trash", action="store_true",
        help="Move detected duplicate files to <out>/trash/ instead of only "
             "reporting them. Originals are never touched; ambiguous groups "
             "are skipped. The move is logged to operations_log.json and can "
             "be reverted from the GUI.",
    )
    parser.add_argument(
        "--out", default=None, metavar="FOLDER",
        help="Output folder for trash/ and operations_log.json "
             "(default: the --scan folder).",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.threshold is not None and not 0 <= args.threshold <= 100:
        parser.error("--threshold must be between 0 and 100")

    scan_folder = Path(args.scan)
    if not scan_folder.exists() or not scan_folder.is_dir():
        print(f"ERROR: scan folder not found: {scan_folder}", file=sys.stderr)
        return 1

    out_folder = Path(args.out) if args.out else scan_folder
    trash_dir = out_folder / "trash"

    settings: Settings = load_settings(SETTINGS_PATH)
    if args.threshold is not None:
        settings.threshold = similarity_to_hamming(args.threshold)

    print("Image Deduper - CLI mode")
    print(f"  scan folder : {scan_folder}")
    print(f"  threshold   : {settings.threshold} bits (pHash Hamming distance)"
          + (f" (from --threshold {args.threshold}%)"
             if args.threshold is not None else " (from settings.json)"))
    print(f"  mode        : {'AUTO-MOVE to ' + str(trash_dir) if args.auto_move_trash else 'dry run (report only)'}")

    # ── scan (same pipeline as the GUI scan worker) ──────────────────────
    progress_cb = _make_progress_cb()
    lib_cache = _load_library_cache(scan_folder)

    # Never rescan our own trash output.
    skip_paths = {trash_dir.resolve()}

    try:
        records = collect_images(
            scan_folder, skip_paths, settings,
            progress_cb=progress_cb,
            library_cache=lib_cache,
            trust_library=False,
        )
        groups, _ = find_groups(records, settings, progress_cb=progress_cb)
    except Exception as exc:
        print(f"ERROR: scan failed: {exc}", file=sys.stderr)
        return 1

    # ── report ───────────────────────────────────────────────────────────
    print(f"\nScanned images    : {len(records):,}")
    if not groups:
        print("Duplicate groups  : 0")
        print("\nNo duplicates found.")
        return 0

    movable_files, movable_bytes, ambiguous = _print_groups(groups)

    print(f"\nDuplicate groups  : {len(groups):,}"
          + (f" ({ambiguous:,} ambiguous, skipped from auto-move)"
             if ambiguous else ""))
    print(f"Duplicate files   : {movable_files:,}")
    print(f"Reclaimable space : {_fmt_size(movable_bytes)}")

    # ── move (optional) ──────────────────────────────────────────────────
    if not args.auto_move_trash:
        print("\nDry run: nothing was moved. "
              "Re-run with --auto-move-trash to move duplicates to trash/.")
        return 0

    try:
        moved, errors = move_groups(groups, out_folder, dry_run=False)
    except Exception as exc:
        print(f"ERROR: move failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nMoved {moved:,} duplicate file(s) to {trash_dir}"
          + (f" ({errors:,} errors)" if errors else ""))
    print(f"Operations log    : {ops_log_path(out_folder)} (revertable from the GUI)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
