"""
_calib_runner.py — Standalone calibration runner for the 3-iteration tuning workflow.
Run from the repo root:  python _calib_runner.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure the repo root is on the path when run from elsewhere
sys.path.insert(0, str(Path(__file__).parent))

from config import Settings
from calibrator import run_calibration, format_log, load_ground_truth

CALIB_FOLDERS = {
    "RAW":      Path(r"E:\MEDIA\test\Calibrate raw"),
    "JPEG":     Path(r"E:\MEDIA\test\Calibration"),
    "CrossFmt": Path(r"E:\MEDIA\test\calibration_cf"),
}

def f1_from_result(r) -> float:
    """Compute F1 from a CalibrationResult (precision/recall over group-level TP)."""
    # TP = groups_found (all expected group members in same detected group)
    # FN = groups missed
    # FP = false_positives (singles or negatives wrongly merged)
    tp = r.groups_found
    fn = r.groups_total - r.groups_found
    fp = r.false_positives + (r.negatives_total - r.negatives_correct)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _make_settings_for_folder(folder_name: str) -> Settings:
    """Return Settings tuned for the given calibration folder.

    RAW and CF folders:
    - use_rawpy=True: CR2 files must be hashed; without this only JPEG halves are
      seen and cross-format pairs can never form.
    - keep_all_formats=False: calibration data expects CR2 to be the authoritative
      master and the camera JPEG to be classified as a preview/duplicate.  With
      keep_all_formats=True both formats would be kept as originals and the group
      would not appear in the result at all, causing all expected groups to be
      scored as missed.
    JPEG folder: pure JPEG pairs — default settings are fine.
    """
    s = Settings()
    if folder_name in ("RAW", "CrossFmt"):
        s.use_rawpy = True
        s.keep_all_formats = False
    return s


def run_all(label: str, settings: Settings | None = None) -> dict:
    """Run calibration on all 3 folders and return per-folder F1 + best result."""

    results = {}
    for name, folder in CALIB_FOLDERS.items():
        if not folder.exists():
            print(f"  [{name}] SKIP — folder not found: {folder}")
            results[name] = None
            continue

        folder_settings = _make_settings_for_folder(name) if settings is None else settings
        gt = load_ground_truth(folder)
        print(f"  [{name}] groups={len(gt.groups)}  "
              f"negatives={len(gt.negatives)}  singles={len(gt.singles)}")

        t0 = time.time()
        all_res, log = run_calibration(
            folder, folder_settings,
            progress_cb=lambda msg, cur, tot: print(f"    {msg} ({cur}/{tot})", end="\r", flush=True),
        )
        elapsed = time.time() - t0
        print()  # newline after progress carriage-returns

        if not all_res:
            print(f"  [{name}] ERROR — no results returned")
            results[name] = None
            continue

        best = all_res[0]
        f1 = f1_from_result(best)

        print(f"  [{name}] done in {elapsed:.1f}s  "
              f"best: thr={best.threshold} ratio={best.preview_ratio:.3f} "
              f"score={best.score*100:.1f}%  F1={f1*100:.1f}%  "
              f"groups={best.groups_found}/{best.groups_total}  "
              f"neg={best.negatives_correct}/{best.negatives_total}  "
              f"fp={best.false_positives}")

        # Top-3 results
        print(f"  [{name}] Top 3 configs:")
        for r in all_res[:3]:
            f1r = f1_from_result(r)
            print(f"           thr={r.threshold} ratio={r.preview_ratio:.3f} "
                  f"score={r.score*100:.1f}% F1={f1r*100:.1f}% "
                  f"groups={r.groups_found}/{r.groups_total} "
                  f"fp={r.false_positives} neg={r.negatives_correct}/{r.negatives_total}")

        results[name] = {"best": best, "f1": f1, "log": log, "all_res": all_res}

    return results


def print_summary(iteration: int, results: dict) -> None:
    print(f"\n{'='*60}")
    print(f"ITERATION {iteration} SUMMARY")
    print(f"{'='*60}")
    for name, v in results.items():
        if v is None:
            print(f"  {name:<10}: N/A")
        else:
            r = v["best"]
            print(f"  {name:<10}: F1={v['f1']*100:.1f}%  score={r.score*100:.1f}%  "
                  f"thr={r.threshold}  ratio={r.preview_ratio:.3f}  "
                  f"groups={r.groups_found}/{r.groups_total}  "
                  f"neg={r.negatives_correct}/{r.negatives_total}  "
                  f"fp={r.false_positives}")


def verbose_failures(results: dict) -> None:
    """Print missed groups and false positives for diagnosis."""
    for name, v in results.items():
        if v is None or v["log"] is None:
            continue
        log = v["log"]
        missed = [gd for gd in log.group_diagnoses if not gd.detected_together]
        fp_neg = [nd for nd in log.negative_diagnoses if nd.wrongly_merged]
        fp_sing = [sd for sd in log.single_diagnoses if sd.wrongly_grouped_with]

        if missed or fp_neg or fp_sing:
            print(f"\n  [{name}] FAILURES:")
            for gd in missed:
                print(f"    MISSED GROUP [{gd.folder_name}]")
                for pd in gd.pair_diagnoses:
                    print(f"      {pd.name_a} <-> {pd.name_b}: blocked_by={pd.blocked_by}  "
                          f"pHash={pd.phash_dist}(lim={pd.effective_threshold})  "
                          f"hist={pd.hist_sim:.3f if pd.hist_sim is not None else 'N/A'}  "
                          f"bri={pd.brightness_diff:.1f}")
            for nd in fp_neg:
                print(f"    FALSE POS (negative) [{nd.folder_name}]")
                for a, b in nd.merged_pairs:
                    print(f"      merged: {a} <-> {b}")
                for pd in nd.pair_diagnoses:
                    print(f"      {pd.name_a} <-> {pd.name_b}: blocked_by={pd.blocked_by}  "
                          f"pHash={pd.phash_dist}  hist={pd.hist_sim:.3f if pd.hist_sim is not None else 'N/A'}")
            for sd in fp_sing:
                print(f"    FALSE POS (single) {sd.filename} -> merged with {sd.wrongly_grouped_with}")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("BASELINE CALIBRATION (current defaults)")
    print("="*60)
    baseline = run_all("baseline")
    print_summary(0, baseline)
    verbose_failures(baseline)
