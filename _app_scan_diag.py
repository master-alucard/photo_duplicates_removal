"""
_app_scan_diag.py -- diagnose which expected groups the app misses and why.

Usage:
    python _app_scan_diag.py

Simulates an app scan with default Settings() + use_rawpy=True (no other overrides)
against E:/MEDIA/test/Calibrate raw/groups, then cross-references the ground truth.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import Settings
from scanner import (
    collect_images,
    find_groups,
    RAW_EXTENSIONS,
    _can_be_similar,
    _CF_BASE_THRESHOLD,
    _same_dimensions,
    _CROSS_FORMAT_HIST_FLOOR,
)
from calibrator import load_ground_truth

CALIB_ROOT = Path(r"E:\MEDIA\test\Calibrate raw")
GROUPS_DIR = CALIB_ROOT / "groups"


# ---------------------------------------------------------------------------
# Guard trace helper
# ---------------------------------------------------------------------------

def guard_trace(a, b, settings) -> list:
    """Return strings describing which guards pass/fail for an image pair."""
    notes = []

    # 1. Aspect-ratio guard
    ar_a = a.width / a.height if a.height else 1.0
    ar_b = b.width / b.height if b.height else 1.0
    ar_tol = settings.ar_tolerance_pct / 100
    ar_diff_normal  = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001)
    ar_diff_rotated = abs(ar_a - 1.0 / ar_b) / max(ar_a, 1.0 / ar_b, 0.001) if ar_b else 1.0
    ar_min = min(ar_diff_normal, ar_diff_rotated)
    tag = "FAIL" if ar_min > ar_tol else "pass"
    notes.append(f"{tag} AR: diff={ar_min:.3f}  tol={ar_tol:.3f}")

    # Cross-format detection
    a_is_raw = a.path.suffix.lower() in RAW_EXTENSIONS
    b_is_raw = b.path.suffix.lower() in RAW_EXTENSIONS
    cross_format = a_is_raw != b_is_raw
    cf_factor = getattr(settings, "cross_format_threshold_factor", 6.0) if cross_format else 1.0
    notes.append(f"cross_format={cross_format}  cf_factor={cf_factor}")

    # EXIF date guard (cross-format only)
    if cross_format:
        a_date = getattr(a, "exif_date", None)
        b_date = getattr(b, "exif_date", None)
        if a_date is not None and b_date is not None:
            delta = abs((a_date - b_date).total_seconds())
            tag = "FAIL" if delta > 300 else "pass"
            notes.append(f"{tag} EXIF date: delta={delta:.0f}s  a={a_date}  b={b_date}")
        else:
            notes.append(f"SKIP EXIF date guard (one/both None): a={a_date}  b={b_date}")

    # 2. Brightness guard
    bright_thr = settings.brightness_max_diff * cf_factor
    bright_diff = abs(a.brightness - b.brightness)
    tag = "FAIL" if bright_diff > bright_thr else "pass"
    notes.append(
        f"{tag} brightness: diff={bright_diff:.1f}  thr={bright_thr:.1f}"
        f"  a={a.brightness:.1f}  b={b.brightness:.1f}"
    )

    # 3. pHash (with all modifiers)
    dist_normal = a.phash - b.phash
    same_dims = _same_dimensions(a, b, settings.series_tolerance_pct)
    eff_thr = int(settings.threshold * settings.series_threshold_factor) if same_dims else settings.threshold
    if cross_format:
        cf_abs = int(_CF_BASE_THRESHOLD * cf_factor)
        eff_thr = max(eff_thr, cf_abs)

    dark_adj = ""
    if settings.dark_protection:
        if a.brightness < settings.dark_threshold or b.brightness < settings.dark_threshold:
            eff_thr = max(1, int(eff_thr * settings.dark_tighten_factor))
            dark_adj = " [dark-tightened]"

    # Rotation distances
    dist_r90  = (a.phash - b.phash_r90)  if b.phash_r90  is not None else dist_normal
    dist_r180 = (a.phash - b.phash_r180) if b.phash_r180 is not None else dist_normal
    dist_r270 = (a.phash - b.phash_r270) if b.phash_r270 is not None else dist_normal
    dist_ar90  = (a.phash_r90  - b.phash) if a.phash_r90  is not None else dist_normal
    dist_ar180 = (a.phash_r180 - b.phash) if a.phash_r180 is not None else dist_normal
    dist_ar270 = (a.phash_r270 - b.phash) if a.phash_r270 is not None else dist_normal
    if cross_format:
        phash_dist = dist_normal
        is_rotated = False
    else:
        phash_dist = min(dist_normal, dist_r90, dist_r180, dist_r270,
                         dist_ar90, dist_ar180, dist_ar270)
        is_rotated = phash_dist < dist_normal

    if is_rotated:
        rot_floor = int(2 * getattr(settings, "rotation_threshold_factor", 3.0))
        eff_thr = max(eff_thr, rot_floor)

    tag = "FAIL" if phash_dist > eff_thr else "pass"
    notes.append(
        f"{tag} pHash: dist={phash_dist}  eff_thr={eff_thr}{dark_adj}"
        f"  (norm={dist_normal}, same_dims={same_dims}, rotated={is_rotated})"
    )

    # 4. dHash
    if settings.use_dual_hash and not cross_format and not is_rotated and dist_normal > 0:
        dhash_thr = eff_thr * 1.5
        dh_dist = a.dhash - b.dhash
        tag = "FAIL" if dh_dist > dhash_thr else "pass"
        notes.append(f"{tag} dHash: dist={dh_dist:.1f}  thr={dhash_thr:.1f}")

    # 5. Histogram
    if settings.use_histogram and a.histogram and b.histogram:
        intersection = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3
        if cross_format:
            floor = _CROSS_FORMAT_HIST_FLOOR
            tag = "FAIL" if intersection < floor else "pass"
            notes.append(f"{tag} hist: sim={intersection:.3f}  floor={floor} (CF disabled=0.0)")
        else:
            tag = "FAIL" if intersection < settings.hist_min_similarity else "pass"
            notes.append(f"{tag} hist: sim={intersection:.3f}  min={settings.hist_min_similarity}")

    return notes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 72)
    print("APP SCAN DIAGNOSTIC")
    print("Settings: loaded from settings.json (same as the running app)")
    print(f"Scan folder: {GROUPS_DIR}")
    print("=" * 72)
    print()

    from pathlib import Path as _Path
    from config import load_settings as _load_settings
    _settings_path = _Path(__file__).parent / "settings.json"
    if _settings_path.exists():
        settings = _load_settings(_settings_path)
    else:
        settings = Settings()
        settings.use_rawpy = True
    # raw_use_embedded_thumb comes from settings.json (or True by new default)

    cf_abs = int(_CF_BASE_THRESHOLD * settings.cross_format_threshold_factor)
    print(f"  threshold                     = {settings.threshold}")
    print(f"  raw_use_embedded_thumb        = {settings.raw_use_embedded_thumb}")
    print(f"  cross_format_threshold_factor = {settings.cross_format_threshold_factor}")
    print(f"  cf_abs_threshold              = {_CF_BASE_THRESHOLD} * {settings.cross_format_threshold_factor} = {cf_abs}")
    print(f"  brightness_max_diff           = {settings.brightness_max_diff}")
    print(f"  hist_min_similarity           = {settings.hist_min_similarity}")
    print(f"  ar_tolerance_pct              = {settings.ar_tolerance_pct}")
    print()

    # Collect
    print("Collecting images ...")
    records = collect_images(GROUPS_DIR, set(), settings)
    path_to_record = {r.path.resolve(): r for r in records}
    print(f"  Hashed: {len(records)} files")
    print()

    # Group
    print("Running find_groups ...")
    groups, _ = find_groups(records, settings)
    print(f"  Detected groups: {len(groups)}")
    print()

    # Ground truth
    gt = load_ground_truth(CALIB_ROOT)
    print(f"Ground truth: {len(gt.groups)} expected groups")
    print()

    # Build file -> group-index lookup
    file_to_group: dict = {}
    for gidx, g in enumerate(groups):
        for r in g.originals + g.previews:
            file_to_group[r.path.resolve()] = gidx

    matched = 0
    missed_list = []

    for eg in gt.groups:
        resolved_files = [f.resolve() for f in eg.all_files]
        hashed_recs    = [path_to_record.get(f) for f in resolved_files]
        unhashed       = [eg.all_files[i] for i, r in enumerate(hashed_recs) if r is None]
        hashed_recs    = [r for r in hashed_recs if r is not None]

        landing = {file_to_group.get(r.path.resolve()) for r in hashed_recs}
        landing.discard(None)

        all_same = (len(landing) == 1 and len(hashed_recs) == len(resolved_files))
        if all_same:
            matched += 1
            continue

        # --- missed ---
        missed_list.append(eg.folder_name)
        print(f"MISSED  {eg.folder_name}")
        print(f"  expected files: {[f.name for f in eg.all_files]}")

        if unhashed:
            print(f"  HASH FAILED (absent from records): {[f.name for f in unhashed]}")

        if len(hashed_recs) < 2:
            print(f"  Only {len(hashed_recs)} hashed record(s) -- cannot form a pair")
            print()
            continue

        # Landing groups for each hashed file
        for r in hashed_recs:
            g_idx = file_to_group.get(r.path.resolve())
            if g_idx is None:
                print(f"  {r.path.name:42s} -> (not in any group)")
            else:
                peers = [m.path.name for m in (groups[g_idx].originals + groups[g_idx].previews)
                         if m.path.resolve() != r.path.resolve()]
                print(f"  {r.path.name:42s} -> group[{g_idx}] peers: {peers}")

        # Guard trace for every expected pair
        print("  --- guard trace ---")
        for i in range(len(hashed_recs)):
            for j in range(i + 1, len(hashed_recs)):
                a = hashed_recs[i]
                b = hashed_recs[j]
                result = _can_be_similar(a, b, settings)
                print(f"  [{a.path.name}] vs [{b.path.name}]")
                print(f"    dims      : {a.width}x{a.height} vs {b.width}x{b.height}")
                print(f"    brightness: {a.brightness:.1f} vs {b.brightness:.1f}")
                print(f"    can_be_similar -> {result}")
                for note in guard_trace(a, b, settings):
                    print(f"      {note}")
        print()

    print("=" * 72)
    print("SUMMARY")
    print(f"  Expected groups : {len(gt.groups)}")
    print(f"  App-detected    : {len(groups)}")
    print(f"  Matched         : {matched}")
    print(f"  Missed          : {len(missed_list)}")
    if missed_list:
        print(f"  Missed list     : {missed_list}")
    print("=" * 72)


if __name__ == "__main__":
    main()
