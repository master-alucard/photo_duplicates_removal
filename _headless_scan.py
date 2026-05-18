"""
_headless_scan.py — Headless scan that mirrors exactly what the app does.

Loads settings.json the same way App.__init__ does, builds a library cache the
same way the scan worker does, calls collect_images + find_groups with those
exact inputs, and scores the results against calibration ground truth.

Scoring follows the calibration runner's definition: an expected group is
"detected" when all its member files land in the same detected group.  This
matches how _calib_runner.py reports 100% — the raw "app groups" count is
lower than the GT group count because one app group can contain multiple
expected pairs (especially with keep_all_formats=False and use_rawpy=True
where CR2+JPEG triplets merge 2 expected pairs into 1 detected group).

Usage:
    python _headless_scan.py [folder]

If [folder] is omitted, runs against all three calibration folders.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import load_settings, Settings
from scanner import collect_images, find_groups
from calibrator import load_ground_truth

SETTINGS_PATH = Path(__file__).parent / "settings.json"

CALIB_FOLDERS = {
    "RAW":      Path(r"E:\MEDIA\test\Calibrate raw"),
    "JPEG":     Path(r"E:\MEDIA\test\Calibration"),
    "CrossFmt": Path(r"E:\MEDIA\test\calibration_cf"),
}

# Ground-truth expected group counts (total pairs/groups in the GT folders)
GT_EXPECTED = {
    "RAW":      35,
    "JPEG":     419,
    "CrossFmt": 31,
}


def scan_and_score(folder: Path, settings: Settings) -> tuple[int, int, int]:
    """Run the app scan pipeline and score against ground truth.

    Returns (app_groups, gt_matched, gt_total).

    app_groups  — raw count of DuplicateGroup objects (what the UI shows).
    gt_matched  — expected GT groups where all members land in one detected group.
    gt_total    — total expected GT groups.
    """
    from library import Library, get_library_dir

    lib_cache: dict = {}
    try:
        lib = Library.load(get_library_dir())
        lib_cache = lib.load_cache_merged(str(folder.resolve()))
    except Exception as e:
        print(f"  [warn] Library cache unavailable: {e}")

    skip_paths: set[Path] = set()
    records = collect_images(
        folder, skip_paths, settings,
        library_cache=lib_cache,
        trust_library=False,
    )
    groups, _ = find_groups(records, settings)
    app_groups = len(groups)

    # Score against calibration ground truth (same metric as _calib_runner.py)
    gt = load_ground_truth(folder)
    path_to_group: dict[Path, int] = {}
    for gidx, g in enumerate(groups):
        for r in g.originals + g.previews:
            path_to_group[r.path.resolve()] = gidx

    matched = 0
    for eg in gt.groups:
        resolved = [f.resolve() for f in eg.all_files]
        landing: set[int] = set()
        for f in resolved:
            gidx = path_to_group.get(f)
            if gidx is not None:
                landing.add(gidx)
        all_in_groups = all(path_to_group.get(f) is not None for f in resolved)
        if len(landing) == 1 and all_in_groups:
            matched += 1

    return app_groups, matched, len(gt.groups)


def main() -> None:
    settings = load_settings(SETTINGS_PATH)

    print("=" * 64)
    print("HEADLESS SCAN — using app settings (settings.json)")
    print("=" * 64)
    print(f"  settings_version              = {settings.settings_version}")
    print(f"  raw_use_embedded_thumb        = {settings.raw_use_embedded_thumb}")
    print(f"  cross_format_threshold_factor = {settings.cross_format_threshold_factor}")
    print(f"  keep_all_formats              = {settings.keep_all_formats}")
    print(f"  threshold                     = {settings.threshold}")
    print(f"  dark_protection               = {settings.dark_protection}")
    print(f"  use_rawpy                     = {settings.use_rawpy}")
    print()

    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
        if not folder.exists():
            print(f"ERROR: folder not found: {folder}")
            sys.exit(1)
        print(f"Scanning: {folder} ...")
        app_g, matched, total = scan_and_score(folder, settings)
        pct = matched / total * 100 if total else 0.0
        print(f"  App groups:  {app_g}")
        print(f"  GT detected: {matched}/{total} ({pct:.0f}%)")
        return

    # Run all three calibration folders
    all_ok = True
    for name, folder in CALIB_FOLDERS.items():
        if not folder.exists():
            print(f"[{name}] SKIP — folder not found: {folder}")
            continue
        print(f"[{name}] Scanning {folder} ...")
        app_g, matched, total = scan_and_score(folder, settings)
        pct = matched / total * 100 if total else 0.0
        status = "OK" if matched == total else "FAIL"
        if matched < total:
            all_ok = False
        print(f"[{name}] App groups: {app_g}  "
              f"GT detected: {matched}/{total} ({pct:.0f}%)  [{status}]")
        print()

    print("=" * 64)
    print("RESULT:", "ALL GT GROUPS DETECTED (100%)" if all_ok else "SOME GT GROUPS MISSED")
    print("=" * 64)


if __name__ == "__main__":
    main()
