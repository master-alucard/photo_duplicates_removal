"""
_diag_problem_pairs.py -- diagnostic for RAW set_032..set_035 problem pairs.
Run from repo root: python _diag_problem_pairs.py
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Settings
from scanner import (
    _hash_image, _hash_raw, _can_be_similar,
    RAW_EXTENSIONS, _CF_BASE_THRESHOLD,
)

RAW_GROUPS = Path(r"E:\MEDIA\test\Calibrate raw\groups")
CF_SINGLES = Path(r"E:\MEDIA\test\calibration_cf\singles")

PROBLEM_SETS = ["set_032", "set_033", "set_034", "set_035"]

def hash_file(p: Path, settings: Settings):
    ext = p.suffix.lower()
    if ext in RAW_EXTENSIONS:
        return _hash_raw(p, settings)
    else:
        return _hash_image(p, settings)

def describe(rec, label: str):
    print(f"  [{label}]")
    print(f"    path       : {rec.path.name}")
    print(f"    dims       : {rec.width}x{rec.height}")
    print(f"    file_size  : {rec.file_size:,} bytes ({rec.file_size/1024/1024:.2f} MB)")
    print(f"    brightness : {rec.brightness:.1f}")
    print(f"    exif_date  : {rec.exif_date}")
    ph_int = int(str(rec.phash), 16)
    print(f"    phash      : {rec.phash}  (int={ph_int})")

def pair_signals(a, b):
    phash_dist = a.phash - b.phash
    dhash_dist = a.dhash - b.dhash
    if a.histogram and b.histogram:
        hist_sim = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3
    else:
        hist_sim = None
    bri_delta = abs(a.brightness - b.brightness)
    exif_delta = None
    if a.exif_date and b.exif_date:
        exif_delta = abs((a.exif_date - b.exif_date).total_seconds())
    return phash_dist, dhash_dist, hist_sim, bri_delta, exif_delta

def main():
    s = Settings()
    s.use_rawpy = True
    s.keep_all_formats = False

    cf_factor = getattr(s, "cross_format_threshold_factor", 6.0)
    cf_abs_thr = int(_CF_BASE_THRESHOLD * cf_factor)

    print(f"\n=== DIAGNOSTIC: problem pairs set_032..set_035 ===")
    print(f"Settings: threshold={s.threshold}  cf_factor={cf_factor}  cf_abs_thr={cf_abs_thr}")
    print()

    for sname in PROBLEM_SETS:
        raw_dir = RAW_GROUPS / sname
        files = sorted(raw_dir.iterdir())
        raw_files = [f for f in files if f.suffix.lower() in RAW_EXTENSIONS]
        jpg_files = [f for f in files if f.suffix.lower() not in RAW_EXTENSIONS]

        print(f"\n{'='*60}")
        print(f"RAW GROUP: {sname}")
        print(f"{'='*60}")

        for rf in raw_files:
            for jf in jpg_files:
                print(f"\nPair: {rf.name}  <->  {jf.name}")

                # Hash from RAW groups context
                rec_raw = hash_file(rf, s)
                rec_jpg = hash_file(jf, s)

                if rec_raw is None or rec_jpg is None:
                    print("  ERROR: could not hash one or both files")
                    continue

                describe(rec_raw, "RAW")
                describe(rec_jpg, "JPEG")

                ph, dh, hist, bri, exif_delta = pair_signals(rec_raw, rec_jpg)
                print(f"\n  pHash dist   : {ph}  (cf_abs_thr={cf_abs_thr}  -> {'PASS' if ph <= cf_abs_thr else 'FAIL'})")
                print(f"  dHash dist   : {dh}")
                print(f"  hist_sim     : {hist:.4f}" if hist is not None else "  hist_sim     : N/A")
                print(f"  bri_delta    : {bri:.1f}")
                print(f"  exif_delta   : {exif_delta:.0f}s" if exif_delta is not None else "  exif_delta   : N/A (missing EXIF)")
                can_sim = _can_be_similar(rec_raw, rec_jpg, s)
                print(f"  _can_be_similar: {can_sim}")

        # Now check the same files in CF singles context
        print(f"\n--- CROSS-FORMAT SINGLES context for {sname} ---")
        for rf in raw_files:
            cf_rf = CF_SINGLES / rf.name
            if not cf_rf.exists():
                print(f"  {rf.name} NOT FOUND in CF/singles")
                continue
            print(f"\n  CF single: {cf_rf.name}")
            rec_cf_raw = hash_file(cf_rf, s)
            if rec_cf_raw:
                describe(rec_cf_raw, "CF-RAW")

        for jf in jpg_files:
            cf_jf = CF_SINGLES / jf.name
            if not cf_jf.exists():
                print(f"  {jf.name} NOT FOUND in CF/singles")
                continue
            print(f"\n  CF single: {cf_jf.name}")
            rec_cf_jpg = hash_file(cf_jf, s)
            if rec_cf_jpg:
                describe(rec_cf_jpg, "CF-JPEG")

        # Cross-compare CF RAW vs CF JPEG
        for rf in raw_files:
            for jf in jpg_files:
                cf_rf = CF_SINGLES / rf.name
                cf_jf = CF_SINGLES / jf.name
                if not cf_rf.exists() or not cf_jf.exists():
                    continue
                r1 = hash_file(cf_rf, s)
                r2 = hash_file(cf_jf, s)
                if r1 is None or r2 is None:
                    continue
                ph, dh, hist, bri, exif_delta = pair_signals(r1, r2)
                can_sim = _can_be_similar(r1, r2, s)
                print(f"\n  CF-pair signals ({rf.name} <-> {jf.name}):")
                print(f"    pHash dist   : {ph}  cf_abs_thr={cf_abs_thr} -> {'PASS' if ph <= cf_abs_thr else 'FAIL'}")
                print(f"    hist_sim     : {hist:.4f}" if hist is not None else "    hist_sim     : N/A")
                print(f"    bri_delta    : {bri:.1f}")
                print(f"    exif_delta   : {exif_delta:.0f}s" if exif_delta is not None else "    exif_delta   : N/A")
                print(f"    _can_be_similar: {can_sim}")

    print("\n=== DONE ===\n")

if __name__ == "__main__":
    main()
