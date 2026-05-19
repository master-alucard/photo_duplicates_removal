"""
_diag_false_chains.py -- Identify exactly which GT groups are being merged
(false-positive chains) and print the pairwise signals for every cross-GT edge
that caused the merge.

Usage:
    python _diag_false_chains.py [RAW|JPEG|CrossFmt]

If no argument is given, runs all three folders.
"""
from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from config import load_settings, Settings
from scanner import (
    collect_images,
    find_groups,
    _can_be_similar,
    RAW_EXTENSIONS,
    ImageRecord,
)
from calibrator import load_ground_truth
from library import Library, get_library_dir

SETTINGS_PATH = Path(__file__).parent / "settings.json"

CALIB_FOLDERS = {
    "RAW":      Path(r"E:\MEDIA\test\Calibrate raw"),
    "JPEG":     Path(r"E:\MEDIA\test\Calibration"),
    "CrossFmt": Path(r"E:\MEDIA\test\calibration_cf"),
}


def _get_records(folder: Path, settings: Settings) -> list:
    lib_cache: dict = {}
    try:
        lib = Library.load(get_library_dir())
        lib_cache = lib.load_cache_merged(str(folder.resolve()))
    except Exception as e:
        print(f"  [warn] Library cache unavailable: {e}")
    skip_paths: set = set()
    return collect_images(folder, skip_paths, settings, library_cache=lib_cache, trust_library=False)


def _pair_signals(a: ImageRecord, b: ImageRecord, settings: Settings) -> dict:
    """Return raw signal values between a pair, plus whether _can_be_similar passes."""
    a_is_raw = a.path.suffix.lower() in RAW_EXTENSIONS
    b_is_raw = b.path.suffix.lower() in RAW_EXTENSIONS
    cross_format = a_is_raw != b_is_raw

    phash_dist = int(a.phash - b.phash)
    dhash_dist = int(a.dhash - b.dhash)

    hist_sim = 0.0
    if a.histogram and b.histogram:
        hist_sim = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3

    brightness_diff = abs(a.brightness - b.brightness)
    tol = settings.series_tolerance_pct / 100
    same_dims = bool(
        a.width and a.height and b.width and b.height
        and abs(a.width - b.width) / max(a.width, b.width) <= tol
        and abs(a.height - b.height) / max(a.height, b.height) <= tol
    )
    ar_a = a.width / a.height if a.height else 1.0
    ar_b = b.width / b.height if b.height else 1.0
    ar_diff = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001)
    exif_delta = None
    if a.exif_date and b.exif_date:
        exif_delta = abs((a.exif_date - b.exif_date).total_seconds())
    return dict(
        phash=phash_dist, dhash=dhash_dist,
        hist=round(hist_sim, 4),
        brightness_diff=round(brightness_diff, 1),
        same_dims=same_dims, ar_diff=round(ar_diff, 4),
        cross_format=cross_format, exif_delta_s=exif_delta,
        passes=_can_be_similar(a, b, settings),
    )


def _fmt_sig(sig: dict) -> str:
    ph = sig["phash"]
    dh = sig["dhash"]
    hs = sig["hist"]
    bd = sig["brightness_diff"]
    ar = sig["ar_diff"]
    sd = sig["same_dims"]
    cf = sig["cross_format"]
    ex = sig["exif_delta_s"]
    return (f"phash={ph}  dhash={dh}  hist={hs}  br_diff={bd}"
            f"  ar_diff={ar}  same_dims={sd}  cross_fmt={cf}  exif_delta={ex}")


def diagnose_folder(name: str, folder: Path, settings: Settings) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"FOLDER: {name} -- {folder}")
    print(sep)
    if not folder.exists():
        print("  SKIP -- folder not found")
        return

    records = _get_records(folder, settings)
    groups, _ = find_groups(records, settings)
    app_groups = len(groups)
    gt = load_ground_truth(folder)
    gt_total = len(gt.groups)

    # Map resolved path -> GT group index
    path_to_gt: dict = {}
    for gidx, eg in enumerate(gt.groups):
        for f in eg.all_files:
            path_to_gt[f.resolve()] = gidx

    # Map resolved path -> app group index
    path_to_app: dict = {}
    for appidx, g in enumerate(groups):
        for r in g.originals + g.previews:
            path_to_app[r.path.resolve()] = appidx

    # Find app groups that contain files from >1 GT group
    app_to_gt_groups: dict = defaultdict(set)
    for rp, appidx in path_to_app.items():
        gtidx = path_to_gt.get(rp)
        if gtidx is not None:
            app_to_gt_groups[appidx].add(gtidx)

    merged = {aid: gts for aid, gts in app_to_gt_groups.items() if len(gts) > 1}

    print(f"  App groups:  {app_groups}  |  GT expected: {gt_total}  |  Delta: {app_groups - gt_total:+d}")
    print(f"  Merged app groups (contain >1 GT group): {len(merged)}")
    if not merged:
        print("  No false merges found.")
        return

    total_edges = 0

    for appidx, gt_idxs in sorted(merged.items()):
        gt_list = sorted(gt_idxs)
        gt_names = [gt.groups[gi].folder_name for gi in gt_list]
        print(f"\n  --- App group #{appidx}  merges GT groups: {gt_names}")

        gt_recs: dict = defaultdict(list)
        for r in groups[appidx].originals + groups[appidx].previews:
            rp = r.path.resolve()
            gtidx = path_to_gt.get(rp)
            if gtidx is not None:
                gt_recs[gtidx].append(r)

        printed_any = False
        for ii, gi in enumerate(gt_list):
            for gj in gt_list[ii + 1:]:
                for ra in gt_recs[gi]:
                    for rb in gt_recs[gj]:
                        sig = _pair_signals(ra, rb, settings)
                        if sig["passes"]:
                            gna = gt.groups[gi].folder_name
                            gnb = gt.groups[gj].folder_name
                            print(f"    EDGE: GT[{gna}] x GT[{gnb}]")
                            print(f"      A: {ra.path.name}  ({ra.width}x{ra.height}  br={ra.brightness:.0f})")
                            print(f"      B: {rb.path.name}  ({rb.width}x{rb.height}  br={rb.brightness:.0f})")
                            print(f"      {_fmt_sig(sig)}")
                            printed_any = True
                            total_edges += 1

        if not printed_any:
            # Transitive merge: no direct cross-GT edge passes, but an intermediate
            # record inside one GT group bridges to the other.  Print closest pairs.
            print("    (No direct cross-GT edge passes _can_be_similar -- merge is transitive)")
            all_pairs = []
            for ii, gi in enumerate(gt_list):
                for gj in gt_list[ii + 1:]:
                    for ra in gt_recs[gi]:
                        for rb in gt_recs[gj]:
                            sig = _pair_signals(ra, rb, settings)
                            gna = gt.groups[gi].folder_name
                            gnb = gt.groups[gj].folder_name
                            all_pairs.append((sig["phash"], ra, rb, gna, gnb, sig))
            all_pairs.sort(key=lambda x: x[0])
            for phd, ra, rb, gna, gnb, sig in all_pairs[:3]:
                print(f"    Closest cross-GT pair (phash={phd}): {ra.path.name} x {rb.path.name}")
                print(f"      GT[{gna}] x GT[{gnb}]  {_fmt_sig(sig)}")

    print(f"\n  Total confirmed false edges printed: {total_edges}")


def main() -> None:
    settings = load_settings(SETTINGS_PATH)
    print("DIAGNOSTIC: False-chain analysis")
    print(f"  threshold={settings.threshold}  series_factor={settings.series_threshold_factor}")
    print(f"  dark_protection={settings.dark_protection}  use_dual_hash={settings.use_dual_hash}")
    print(f"  hist_min_similarity={settings.hist_min_similarity}")
    print(f"  raw_use_embedded_thumb={settings.raw_use_embedded_thumb}")
    print(f"  cf_factor={settings.cross_format_threshold_factor}")

    if len(sys.argv) > 1:
        key = sys.argv[1]
        if key not in CALIB_FOLDERS:
            print(f"Unknown folder key: {key}. Use one of: {list(CALIB_FOLDERS)}")
            sys.exit(1)
        diagnose_folder(key, CALIB_FOLDERS[key], settings)
        return

    for name, folder in CALIB_FOLDERS.items():
        diagnose_folder(name, folder, settings)

    print("\nDone.")


if __name__ == "__main__":
    main()
