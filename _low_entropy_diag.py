"""
_low_entropy_diag.py -- Diagnostic for wrong-merge cases in the Calibration folder.

For each case where files from DIFFERENT GT groups land in the SAME app group,
prints a full comparison table of all signals, then classifies as:
  Class A (algorithm bug): low-entropy images chained because pHash collapsed
                           but file_size / dHash / mtime clearly differ
  Class B (GT data bug):   high-entropy images that may be legitimately identical
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import load_settings
from scanner import collect_images, find_groups
from calibrator import load_ground_truth
import numpy as _np

SETTINGS_PATH = Path(__file__).parent / "settings.json"
CALIB_FOLDER  = Path(r"E:\MEDIA\test\Calibration")

# nats: below this = "low entropy" (dominated by 1-2 histogram bins)
ENTROPY_THR = 3.0


def histogram_entropy(histogram: list) -> float:
    """Shannon entropy (nats) of the 96-bin RGB histogram."""
    if not histogram:
        return 0.0
    arr = _np.array(histogram, dtype=_np.float64)
    s = arr.sum()
    if s <= 0:
        return 0.0
    arr = arr / s
    nz = arr[arr > 0]
    return float(-_np.sum(nz * _np.log(nz)))


def histogram_intersection(h1: list, h2: list) -> float:
    if not h1 or not h2:
        return -1.0
    return sum(min(x, y) for x, y in zip(h1, h2)) / 3.0


def main() -> None:
    settings = load_settings(SETTINGS_PATH)

    print("=" * 80)
    print("LOW-ENTROPY DIAGNOSTIC  --  Calibration folder")
    print("=" * 80)
    print(f"threshold={settings.threshold}  dark_protection={settings.dark_protection}")
    print(f"dark_threshold={settings.dark_threshold}  dark_tighten_factor={settings.dark_tighten_factor}")
    print(f"use_dual_hash={settings.use_dual_hash}  use_histogram={settings.use_histogram}")
    print(f"hist_min_similarity={settings.hist_min_similarity}  brightness_max_diff={settings.brightness_max_diff}")
    print()

    print("Scanning images...")
    lib_cache: dict = {}
    try:
        from library import Library, get_library_dir
        lib = Library.load(get_library_dir())
        lib_cache = lib.load_cache_merged(str(CALIB_FOLDER.resolve()))
    except Exception as e:
        print(f"  [warn] Library cache: {e}")

    records = collect_images(
        CALIB_FOLDER, skip_paths=set(), settings=settings,
        library_cache=lib_cache, trust_library=False,
    )
    groups, _ = find_groups(records, settings)
    print(f"  {len(records)} images -> {len(groups)} app groups")

    path_to_ag: dict = {}
    for gidx, g in enumerate(groups):
        for r in g.originals + g.previews:
            path_to_ag[r.path.resolve()] = gidx

    ptr = {r.path.resolve(): r for r in records}
    gt = load_ground_truth(CALIB_FOLDER)
    print(f"  {len(gt.groups)} GT groups")
    print()

    ag_to_gt: dict = {}
    for eg in gt.groups:
        for f in eg.all_files:
            ag = path_to_ag.get(f.resolve())
            if ag is not None:
                ag_to_gt.setdefault(ag, [])
                if eg.folder_name not in ag_to_gt[ag]:
                    ag_to_gt[ag].append(eg.folder_name)

    wrong = {ag: names for ag, names in ag_to_gt.items() if len(names) > 1}
    print(f"WRONG MERGES: {len(wrong)} app groups contain files from 2+ GT groups")
    print()

    class_a = []
    class_b = []

    for ag_idx, gt_names in sorted(wrong.items(), key=lambda x: x[1][0]):
        grp = groups[ag_idx]
        all_in_ag = grp.originals + grp.previews
        conflicting = []
        for name in gt_names:
            eg = next((x for x in gt.groups if x.folder_name == name), None)
            if eg:
                for f in eg.all_files:
                    r = ptr.get(f.resolve())
                    if r:
                        conflicting.append((name, r))

        is_focus = any(n in {"134", "380"} for n in gt_names)
        sep = "=" * 80 if is_focus else "-" * 60
        print(sep)
        print(f"APP_GRP {ag_idx}  GT groups merged: {', '.join(gt_names)}")
        print(f"  total files in app group: {len(all_in_ag)}")
        print()

        hdr = (f"  {'File':<45} {'KB':>7} {'phash':>16} {'dhash':>16}"
               f" {'Bright':>7} {'Entropy':>8} {'mtime':>12}  GT")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))

        recs_gt = []
        for gt_name, r in conflicting:
            ent = histogram_entropy(r.histogram)
            kb = r.file_size / 1024
            print(f"  {r.path.name:<45} {kb:>7.1f} {str(r.phash):>16} {str(r.dhash):>16}"
                  f" {r.brightness:>7.1f} {ent:>8.3f} {r.mtime:>12.0f}  [{gt_name}]")
            recs_gt.append((gt_name, r, ent))

        print()
        print("  Cross-GT pairwise comparison:")

        for i, (gta, ra, enta) in enumerate(recs_gt):
            for j, (gtb, rb, entb) in enumerate(recs_gt):
                if j <= i or gta == gtb:
                    continue
                pd = ra.phash - rb.phash
                dd = (ra.dhash - rb.dhash) if ra.dhash and rb.dhash else -1
                hi = histogram_intersection(ra.histogram, rb.histogram)
                sd = (abs(ra.file_size - rb.file_size)
                      / max(ra.file_size, rb.file_size, 1) * 100)
                mt = abs(ra.mtime - rb.mtime)
                both_low = enta < ENTROPY_THR and entb < ENTROPY_THR
                cls = ("A" if both_low and (sd > 5 or (dd > 2 and dd >= 0) or mt > 30)
                       else "B")
                print(f"    {ra.path.name}  <->  {rb.path.name}")
                print(f"      pHash={pd}  dHash={dd}  hist={hi:.3f}"
                      f"  size_delta={sd:.1f}%  mtime_delta={mt:.0f}s")
                print(f"      entropy=({enta:.3f}, {entb:.3f})"
                      f"  both_low={both_low}  -> CLASS {cls}")
                entry = (ag_idx, gt_names, pd, dd, sd, enta, entb)
                if cls == "A":
                    class_a.append(entry)
                else:
                    class_b.append(entry)
        print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Wrong-merge app groups : {len(wrong)}")
    print(f"Class A (algo bug)     : {len(class_a)}")
    print(f"Class B (GT/ambiguous) : {len(class_b)}")
    if class_a:
        phashs = [x[2] for x in class_a]
        dhashs = [x[3] for x in class_a if x[3] >= 0]
        sizes  = [x[4] for x in class_a]
        ents   = [x[5] for x in class_a] + [x[6] for x in class_a]
        print(f"  pHash dist: min={min(phashs)}  max={max(phashs)}"
              f"  mean={sum(phashs)/len(phashs):.1f}")
        if dhashs:
            print(f"  dHash dist: min={min(dhashs)}  max={max(dhashs)}"
                  f"  mean={sum(dhashs)/len(dhashs):.1f}")
        print(f"  size_delta: min={min(sizes):.1f}%  max={max(sizes):.1f}%"
              f"  mean={sum(sizes)/len(sizes):.1f}%")
        print(f"  entropy   : min={min(ents):.3f}  max={max(ents):.3f}"
              f"  mean={sum(ents)/len(ents):.3f}")
    print()

    print("=" * 80)
    print("FOCUSED DUMP -- GT groups 134 and 380")
    print("=" * 80)
    for name in ["pair_134", "pair_380"]:
        eg = next((x for x in gt.groups if x.folder_name == name), None)
        if eg is None:
            print(f"  GT group {name}: NOT FOUND")
            continue
        recs = [ptr.get(f.resolve()) for f in eg.all_files]
        recs = [r for r in recs if r is not None]
        print(f"\nGT group {name}:")
        for r in recs:
            ag = path_to_ag.get(r.path.resolve(), "UNGROUPED")
            ent = histogram_entropy(r.histogram)
            print(f"  {r.path.name:<45} {r.file_size/1024:>7.1f} KB"
                  f"  bright={r.brightness:.1f}  entropy={ent:.3f}"
                  f"  phash={r.phash}  mtime={r.mtime:.0f}  appgrp={ag}")
        print("  Pairwise:")
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                ra, rb = recs[i], recs[j]
                pd = ra.phash - rb.phash
                dd = (ra.dhash - rb.dhash) if ra.dhash and rb.dhash else -1
                hi = histogram_intersection(ra.histogram, rb.histogram)
                sd = (abs(ra.file_size - rb.file_size)
                      / max(ra.file_size, rb.file_size, 1) * 100)
                mt = abs(ra.mtime - rb.mtime)
                print(f"    {ra.path.name}  <->  {rb.path.name}")
                print(f"      pHash={pd}  dHash={dd}  hist={hi:.3f}"
                      f"  size_delta={sd:.1f}%  mtime_delta={mt:.0f}s")
        ags = {path_to_ag.get(r.path.resolve()) for r in recs}
        ags.discard(None)
        status = "WRONG MERGE" if len(ags) == 1 else "SPLIT or partial"
        print(f"  App groups hit: {ags} ({status})")


if __name__ == "__main__":
    main()
