"""
Benchmark: accuracy vs speed for different settings combinations.
Hashes images ONCE, then benchmarks find_groups with varying settings.
Metadata collection cost measured separately.

Usage: python -X utf8 bench_quick.py [calibration_folder]
"""
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import Settings
from calibrator import load_ground_truth, _score
from scanner import collect_images, find_groups

FOLDER = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"E:\MEDIA\test\Calibration")
REPEATS = 5   # repeats of find_groups for stable timing

# ── Best known detection settings from calibration ────────────────────────────
BEST = Settings(
    threshold=2,
    preview_ratio=0.90,
    series_threshold_factor=0.5,
    use_histogram=True,
    use_dual_hash=True,
    collect_metadata=True,
    dark_protection=True,
    ambiguous_detection=False,
    recursive=True,
    min_dimension=0,
)


@dataclass
class BenchResult:
    label: str
    group_time: float   # avg find_groups time (seconds)
    accuracy: float
    n_groups: int


def run_combo(
    label: str,
    records: list,
    overrides: dict[str, Any],
    gt,
) -> BenchResult:
    cfg = deepcopy(BEST)
    for k, v in overrides.items():
        setattr(cfg, k, v)

    # Warm-up
    find_groups(records, cfg)

    # Timed runs
    t0 = time.perf_counter()
    for _ in range(REPEATS):
        groups, _ = find_groups(records, cfg)
    group_time = (time.perf_counter() - t0) / REPEATS

    result = _score(gt, groups, cfg.threshold, cfg.preview_ratio)
    acc = result.score

    print(f"  {label:<44}  group={group_time:.3f}s  acc={acc*100:.1f}%  groups={len(groups)}")
    return BenchResult(label, group_time, acc, len(groups))


def main():
    print(f"Calibration folder : {FOLDER}")
    print(f"Loading ground truth…")
    gt = load_ground_truth(FOLDER)
    print(f"  {len(gt.groups)} ground-truth groups\n")

    # ── Hash once with full metadata (measures collection cost separately) ──
    print("Hashing with metadata (full)…")
    t0 = time.perf_counter()
    records_full = collect_images(FOLDER, skip_paths=set(), settings=BEST)
    hash_full_time = time.perf_counter() - t0
    print(f"  {len(records_full)} records in {hash_full_time:.1f}s\n")

    cfg_no_meta = deepcopy(BEST)
    cfg_no_meta.collect_metadata = False
    print("Hashing without metadata (EXIF skip)…")
    t0 = time.perf_counter()
    records_no_meta = collect_images(FOLDER, skip_paths=set(), settings=cfg_no_meta)
    hash_no_meta_time = time.perf_counter() - t0
    print(f"  {len(records_no_meta)} records in {hash_no_meta_time:.1f}s")
    meta_saving = hash_full_time - hash_no_meta_time
    print(f"  Metadata collection overhead: {meta_saving:.1f}s "
          f"({meta_saving/hash_full_time*100:.0f}% of hash phase)\n")

    # ── Grouping benchmarks (find_groups only, records reused) ───────────────
    combos: list[tuple[str, dict, list]] = [
        # label, overrides, records_to_use
        ("Baseline (full accuracy)",            {},                            records_full),
        ("No dHash",                            {"use_dual_hash": False},      records_full),
        ("No histogram",                        {"use_histogram": False},      records_full),
        ("No dark protection",                  {"dark_protection": False},    records_full),
        ("threshold=5",                         {"threshold": 5},              records_full),
        ("threshold=10",                        {"threshold": 10},             records_full),
        ("No dHash + no histogram",             {"use_dual_hash": False,
                                                 "use_histogram": False},      records_full),
        ("No dHash + no histogram + no dark",   {"use_dual_hash": False,
                                                 "use_histogram": False,
                                                 "dark_protection": False},   records_full),
        ("Speed-max (all guards off)",          {"use_dual_hash": False,
                                                 "use_histogram": False,
                                                 "dark_protection": False},   records_no_meta),
        ("Speed-max + threshold=5",             {"use_dual_hash": False,
                                                 "use_histogram": False,
                                                 "dark_protection": False,
                                                 "threshold": 5},             records_no_meta),
        ("Speed-max + threshold=10",            {"use_dual_hash": False,
                                                 "use_histogram": False,
                                                 "dark_protection": False,
                                                 "threshold": 10},            records_no_meta),
    ]

    print(f"Benchmarking find_groups ({REPEATS} repeats each)…\n")
    results: list[tuple[BenchResult, list]] = []
    for label, overrides, recs in combos:
        r = run_combo(label, recs, overrides, gt)
        uses_meta = (recs is records_full)
        results.append((r, uses_meta))

    baseline = results[0][0]

    # ── Summary table ─────────────────────────────────────────────────────────
    print("\n" + "=" * 105)
    print(f"{'Setting combination':<44}  {'Group':>7}  {'+Meta':>6}  "
          f"{'Total':>7}  {'Acc%':>6}  {'Speedup':>8}  {'Acc Δ':>7}")
    print("=" * 105)

    baseline_total = baseline.group_time + hash_full_time
    for r, uses_meta in results:
        meta_time = hash_full_time if uses_meta else hash_no_meta_time
        total = r.group_time + meta_time
        speedup = baseline_total / total if total > 0 else 1.0
        acc_delta = (r.accuracy - baseline.accuracy) * 100
        meta_lbl = "yes" if uses_meta else "no "
        marker = " ◄" if r.label == "Baseline (full accuracy)" else ""
        print(
            f"{r.label:<44}  {r.group_time:>6.3f}s  {meta_lbl:>6}  "
            f"{total:>6.1f}s  {r.accuracy*100:>5.1f}%  "
            f"{speedup:>7.2f}×  {acc_delta:>+6.1f}%{marker}"
        )
    print("=" * 105)
    print(f"\nHash phase: with metadata={hash_full_time:.1f}s  without={hash_no_meta_time:.1f}s")
    print(f"Baseline total (hash+group): {baseline_total:.1f}s  accuracy: {baseline.accuracy*100:.1f}%")

    # ── Speed tier summary ────────────────────────────────────────────────────
    print("\n── Speed Tier Recommendations ──────────────────────────────────────────")
    tiers = [
        ("Accuracy", 1.0, 0.0),
        ("Balanced", 1.3, -2.0),
        ("Fast",     1.8, -5.0),
        ("Speed",    2.5, -10.0),
    ]
    print(f"  {'Tier':<12}  {'Min speedup':>11}  {'Max acc loss':>13}")
    print(f"  {'-'*12}  {'-'*11}  {'-'*13}")
    for tier, min_spd, max_loss in tiers:
        candidates = [
            (r, m) for r, m in results
            if (r.accuracy - baseline.accuracy) * 100 >= max_loss
        ]
        if candidates:
            best_r, best_m = max(candidates,
                                 key=lambda x: (baseline_total / (x[0].group_time + (hash_full_time if x[1] else hash_no_meta_time))))
            meta_t = hash_full_time if best_m else hash_no_meta_time
            total_t = best_r.group_time + meta_t
            spd = baseline_total / total_t
            print(f"  {tier:<12}  {spd:>10.2f}×  {(best_r.accuracy-baseline.accuracy)*100:>+12.1f}%"
                  f"  → {best_r.label}")
        else:
            print(f"  {tier:<12}  (no candidate meeting constraint)")


if __name__ == "__main__":
    main()
