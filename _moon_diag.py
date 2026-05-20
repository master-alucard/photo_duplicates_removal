"""
_moon_diag.py — Diagnostic for the 4 moon-photo false-merge.

Reports pairwise distances and GT group membership for:
  183007, 183906, 183905, 183309  (the 4 files in one wrong app group)
  183963, 183457, 183421          (secondary set from screenshot)

GT ground truth (from find command):
  groups/pair_114/file 1620x1080_183905.jpg
  groups/pair_114/file 1620x1080_183906.jpg
  negatives/neg_01/file 1620x1080_183309.jpg  ← should NOT be grouped with others
  negatives/neg_04/file 1620x1080_183007.jpg  ← should NOT be grouped with others
  negatives/neg_05/file 1620x1080_183963.jpg  ← negative
  negatives/neg_06/file 1620x1080_183421.jpg  ← should NOT be grouped with others
  negatives/neg_06/file 1620x1080_183457.jpg  ← should NOT be grouped with 183421?
"""
import sys
import os
import math
from pathlib import Path
from itertools import combinations

# Add repo to path so we can import scanner internals
REPO = Path(r"E:\Repositories2026\katador\image_duplicates_removal_v2")
sys.path.insert(0, str(REPO))

from PIL import Image
import imagehash
import numpy as np

from scanner import _compute_histogram, _compute_brightness, _histogram_entropy, _hamming

CALIB = Path(r"E:\MEDIA\test\Calibration")

# The 4 primary problem files
PRIMARY = [
    "file 1620x1080_183007.jpg",
    "file 1620x1080_183906.jpg",
    "file 1620x1080_183905.jpg",
    "file 1620x1080_183309.jpg",
]

# Secondary set
SECONDARY = [
    "file 1620x1080_183963.jpg",
    "file 1620x1080_183457.jpg",
    "file 1620x1080_183421.jpg",
]

GT_MAP = {
    "file 1620x1080_183905.jpg": "groups/pair_114",
    "file 1620x1080_183906.jpg": "groups/pair_114",
    "file 1620x1080_183309.jpg": "negatives/neg_01",
    "file 1620x1080_183007.jpg": "negatives/neg_04",
    "file 1620x1080_183963.jpg": "negatives/neg_05",
    "file 1620x1080_183421.jpg": "negatives/neg_06",
    "file 1620x1080_183457.jpg": "negatives/neg_06",
}


def locate_file(name):
    for root, dirs, files in os.walk(CALIB):
        if name in files:
            return Path(root) / name
    return None


def hash_image(path):
    with Image.open(path) as img:
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        # Downscale for speed (match scanner)
        w, h = img.size
        if max(w, h) > 1024:
            scale = 1024 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        ph = imagehash.phash(img, hash_size=8)
        dh = imagehash.dhash(img, hash_size=8)
        brightness = _compute_brightness(img)
        hist = _compute_histogram(img)
        entropy = _histogram_entropy(hist)
    return ph, dh, brightness, hist, entropy


def hist_intersection(ha, hb):
    return sum(min(x, y) for x, y in zip(ha, hb)) / 3.0


def analyze_set(names, label):
    print(f"\n{'='*70}")
    print(f"SET: {label}")
    print(f"{'='*70}")

    records = {}
    for name in names:
        path = locate_file(name)
        if path is None:
            print(f"  NOT FOUND: {name}")
            continue
        size = path.stat().st_size
        mtime = path.stat().st_mtime
        ph, dh, brightness, hist, entropy = hash_image(path)
        gt = GT_MAP.get(name, "UNKNOWN")
        records[name] = {
            "path": path,
            "size": size,
            "mtime": mtime,
            "phash": ph,
            "dhash": dh,
            "brightness": brightness,
            "entropy": entropy,
            "hist": hist,
            "gt": gt,
        }
        print(f"\n  FILE: {name}")
        print(f"    GT folder  : {gt}")
        print(f"    Size       : {size:,} bytes ({size//1024} KB)")
        print(f"    Brightness : {brightness:.2f}")
        print(f"    Entropy    : {entropy:.4f} nats  ({'LOW' if entropy < 3.0 else 'NORMAL'})")
        print(f"    pHash      : {ph}")
        print(f"    dHash      : {dh}")

    print(f"\n  PAIRWISE DISTANCES:")
    print(f"  {'Pair':<50} {'pHash':>6} {'dHash':>6} {'HistInt':>8} {'SameGT?':>8}")
    print(f"  {'-'*50} {'-'*6} {'-'*6} {'-'*8} {'-'*8}")

    for a_name, b_name in combinations(records.keys(), 2):
        a = records[a_name]
        b = records[b_name]
        pd = a["phash"] - b["phash"]
        dd = a["dhash"] - b["dhash"]
        hi = hist_intersection(a["hist"], b["hist"])
        same_gt = a["gt"] == b["gt"]
        label_pair = f"{a_name[-10:-4]} vs {b_name[-10:-4]}"
        flag = "CORRECT" if same_gt else "FALSE-MERGE" if pd <= 2 else ""
        print(f"  {label_pair:<50} {pd:>6} {dd:>6} {hi:>8.4f}  {str(same_gt):>7}  {flag}")

    return records


def main():
    print("Moon photo diagnostic — pairwise analysis")
    print(f"Calibration root: {CALIB}")

    primary_records = analyze_set(PRIMARY, "Primary 4 files (the wrong merge)")
    analyze_set(SECONDARY, "Secondary 3 files (from screenshot)")

    # Find the bridge edge that pulls 183309 into the cluster
    print(f"\n{'='*70}")
    print("BRIDGE ANALYSIS: Why does 183309 chain into the group?")
    print(f"{'='*70}")
    print()
    files_of_interest = {
        "183309": locate_file("file 1620x1080_183309.jpg"),
        "183007": locate_file("file 1620x1080_183007.jpg"),
        "183906": locate_file("file 1620x1080_183906.jpg"),
        "183905": locate_file("file 1620x1080_183905.jpg"),
    }
    hashes = {}
    for k, p in files_of_interest.items():
        if p:
            ph, dh, br, hist, ent = hash_image(p)
            hashes[k] = {"phash": ph, "dhash": dh, "entropy": ent, "hist": hist}

    # Default settings threshold = 2, series_threshold_factor = 1.0
    # For same-dims: eff_threshold = threshold * series_threshold_factor = 2
    # pHash guard: dist <= eff_threshold
    threshold = 2

    print(f"Default pHash threshold: {threshold}")
    print(f"dark_tighten_factor would tighten for dark images, but let's check dark_protection:")
    print(f"  dark_threshold default = 40.0, dark_tighten_factor = 0.5 -> eff_threshold would be max(1, int(2*0.5)) = 1 if brightness < 40")
    print()

    for a_key, b_key in combinations(hashes.keys(), 2):
        a = hashes[a_key]
        b = hashes[b_key]
        pd = a["phash"] - b["phash"]
        dd = a["dhash"] - b["dhash"]

        # Simulate dark_protection: brightness of moon photos is very low
        # Let's check if dark protection fires
        a_path = files_of_interest[a_key]
        b_path = files_of_interest[b_key]
        with Image.open(a_path) as ia:
            ia_br = _compute_brightness(ia)
        with Image.open(b_path) as ib:
            ib_br = _compute_brightness(ib)

        eff_thr = threshold
        dark_fired = False
        if ia_br < 40.0 or ib_br < 40.0:
            eff_thr = max(1, int(eff_thr * 0.5))
            dark_fired = True

        passes_phash = pd <= eff_thr
        ent_a = a["entropy"]
        ent_b = b["entropy"]
        low_ent = ent_a < 3.0 and ent_b < 3.0

        # Low entropy guard fires only when dist_normal == 0
        if pd == 0 and low_ent and dd >= 3:
            guard_result = "BLOCKED by low-entropy guard"
        elif passes_phash:
            guard_result = "PASSES (merged)"
        else:
            guard_result = "BLOCKED by pHash threshold"

        print(f"  {a_key} vs {b_key}:")
        print(f"    pHash={pd}, dHash={dd}, dark_fired={dark_fired}(br_a={ia_br:.1f}, br_b={ib_br:.1f})")
        print(f"    eff_threshold={eff_thr}, entropy_a={ent_a:.3f}, entropy_b={ent_b:.3f}, low_ent={low_ent}")
        print(f"    RESULT: {guard_result}")
        print()


if __name__ == "__main__":
    main()
