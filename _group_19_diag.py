"""
_group_19_diag.py -- Diagnostic for Group #19 false-chain bug.

Loads the four files involved (A=CR2_011443, B=CR2_011444, C=JPG_201900,
D=JPG_201909), computes pHash / dHash / histogram / brightness / EXIF for
each, and prints all pairwise distances plus _can_be_similar() results so you
can see exactly which edges formed the false union-find chain.

Usage (from repo root):
    python _group_19_diag.py
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import load_settings
from scanner import (
    _hash_image, _hash_raw, _can_be_similar,
    RAW_EXTENSIONS, _CROSS_FORMAT_DIM_TOL, _EXACT_DUP_PHASH,
    _CF_EMBEDDED_THUMB_MAX_PHASH,
)

SETTINGS_PATH = Path(__file__).parent / "settings.json"
CF_FOLDER = Path("E:/MEDIA/test/calibration_cf")

FILES = {
    "A_cr2_011443": CF_FOLDER / "groups/set_019/Canon EOS M100 6000x4000_011443.cr2",
    "B_cr2_011444": CF_FOLDER / "groups/set_020/Canon EOS M100 6000x4000_011444.cr2",
    "C_jpg_201900":  CF_FOLDER / "groups/set_019/file 6000x4000_201900.jpg",
    "D_jpg_201909":  CF_FOLDER / "groups/set_020/file 6000x4000_201909.jpg",
}


def _hist_intersection(a, b) -> float:
    return sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3


def main() -> None:
    settings = load_settings(SETTINGS_PATH)

    print("=" * 70)
    print("GROUP #19 DIAGNOSTIC")
    print("=" * 70)
    print(f"Settings: threshold={settings.threshold}  "
          f"series_thr_factor={settings.series_threshold_factor}  "
          f"cf_factor={settings.cross_format_threshold_factor}  "
          f"raw_use_embedded_thumb={settings.raw_use_embedded_thumb}")
    print()
    print("Derived thresholds:")
    print(f"  series_thr              = {int(settings.threshold * settings.series_threshold_factor)}")
    print(f"  _EXACT_DUP_PHASH        = {_EXACT_DUP_PHASH}")
    print(f"  _CROSS_FORMAT_DIM_TOL   = {_CROSS_FORMAT_DIM_TOL}")
    print(f"  _CF_EMBEDDED_THUMB_MAX  = {_CF_EMBEDDED_THUMB_MAX_PHASH} (cap for embedded-thumb CF pairs)")
    print()

    print("Loading files...")
    records: dict = {}
    for name, path in FILES.items():
        if not path.exists():
            print(f"  ERROR: {path} not found")
            continue
        ext = path.suffix.lower()
        rec = _hash_raw(path, settings) if ext in RAW_EXTENSIONS else _hash_image(path, settings)
        if rec is None:
            print(f"  ERROR: hashing failed for {name}")
            continue
        records[name] = rec
        print(f"  {name}: {rec.width}x{rec.height}  brightness={rec.brightness:.1f}  "
              f"pHash={rec.phash}  exif={rec.exif_date}  {rec.file_size/1e6:.2f}MB")
    print()

    names = list(records.keys())
    recs  = list(records.values())

    print("PAIRWISE DISTANCES:")
    print(f"{'Pair':<42} {'pHash':>6} {'dHash':>6} {'hist':>6} {'brite':>6} {'same_d':>7} {'cross':>5}")
    print("-" * 80)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            na, nb = names[i], names[j]
            ra, rb = recs[i], recs[j]
            ph = int(ra.phash - rb.phash)
            dh = int(ra.dhash - rb.dhash)
            hi = _hist_intersection(ra, rb)
            bd = abs(ra.brightness - rb.brightness)
            is_raw_a = ra.path.suffix.lower() in RAW_EXTENSIONS
            is_raw_b = rb.path.suffix.lower() in RAW_EXTENSIONS
            cross = is_raw_a != is_raw_b
            tol = settings.series_tolerance_pct / 100.0
            eff_tol = max(tol, _CROSS_FORMAT_DIM_TOL) if cross else tol
            wr = abs(ra.width  - rb.width)  / max(ra.width,  rb.width)
            hr = abs(ra.height - rb.height) / max(ra.height, rb.height)
            sd = wr <= eff_tol and hr <= eff_tol
            lbl = f"{na} vs {nb}"
            print(f"{lbl:<42} {ph:>6} {dh:>6} {hi:>6.3f} {bd:>6.1f} {str(sd):>7} {str(cross):>5}")

    print()
    print("_can_be_similar() RESULTS:")
    edges = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            na, nb = names[i], names[j]
            r = _can_be_similar(recs[i], recs[j], settings)
            edges.append((na, nb, r))
            status = "PASS" if r else "FAIL"
            print(f"  {na} vs {nb}: {status}")

    print()
    passing = [(a, b) for a, b, r in edges if r]
    if passing:
        print("Union-find chains (PASS pairs will be merged):")
        for a, b in passing:
            print(f"  {a} <--> {b}")
    else:
        print("No edges pass -- 4 files will NOT be chained (fix is working).")


if __name__ == "__main__":
    main()
