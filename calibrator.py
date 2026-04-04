"""
calibrator.py — Iterative auto-configure: 4-round coarse-to-fine grid search
with detailed per-algorithm-step diagnostic log generation.

Round 1 (Coarse Grid)    threshold × preview_ratio       — 45 combos
Round 2 (Fine Ratio)     top-5 thresholds + denser ratio — ~30 new combos
Round 3 (Feature Flags)  best settings ± one toggle each — ~6 variants
Round 4 (Param Sweeps)   optimise each impactful param   — conditional

After all rounds the best result is re-scored in verbose mode, producing a
line-by-line log of every guard check for every pair in every expected group
AND every negative pair.  Send the log text directly to the developer for
settings / algorithm fixes.

── Calibration folder layout ────────────────────────────────────────────────

  calibration_data/
  │
  ├── groups/          ← pairs/groups the algorithm MUST detect as duplicates
  │   ├── <any_name>/      ← one sub-folder per known duplicate set
  │   │   ├── photo.jpg    ← largest by pixels = expected original (kept)
  │   │   └── thumb.jpg    ← smaller copy      = expected preview  (trashed)
  │   └── ...
  │
  ├── negatives/       ← pairs that LOOK similar but must NOT be grouped
  │   ├── <any_name>/      ← one sub-folder per "hard negative" pair/set
  │   │   ├── shot_a.jpg   ← different photos — must stay in separate groups
  │   │   └── shot_b.jpg
  │   └── ...
  │
  └── singles/         ← unique photos; must not be grouped with anything
      ├── unique_001.jpg
      └── ...

False positives (negatives wrongly grouped) are penalised 3× more than
missed duplicates, because trashing a genuine original is worse than
keeping an extra copy.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from config import Settings
from scanner import (
    IMAGE_EXTENSIONS,
    RAW_EXTENSIONS,
    DuplicateGroup,
    ImageRecord,
    collect_images,
    _classify_group,   # private but accessible; used by the fast calibration path
)


# ── round grid definitions ────────────────────────────────────────────────────

ROUND1_THRESHOLDS = [4, 6, 8, 10, 12, 14, 16, 18, 20]
ROUND1_RATIOS     = [0.75, 0.80, 0.85, 0.90, 0.95]

# Round 4 — per-parameter sweep grids.
# Each grid is used only when the corresponding feature showed measurable impact
# in Round 3 (score change ≥ _ROUND4_IMPACT_THR).
ROUND4_SERIES_THRESHOLD_FACTORS = [0.50, 0.75, 1.00, 1.25, 1.50, 2.00]
ROUND4_BRIGHTNESS_MAX_DIFFS     = [20.0, 40.0, 60.0, 80.0, 100.0, 150.0, 200.0]
ROUND4_HIST_MIN_SIMILARITIES    = [0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90]
ROUND4_AR_TOLERANCE_PCTS        = [2.0, 3.0, 5.0, 8.0, 10.0, 15.0]
ROUND4_CF_THRESHOLD_FACTORS     = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
ROUND4_DARK_THRESHOLDS          = [20.0, 30.0, 40.0, 50.0, 60.0, 80.0, 100.0]
ROUND4_DARK_TIGHTEN_FACTORS     = [0.25, 0.33, 0.50, 0.67, 0.75, 1.00]
ROUND4_SERIES_TOLERANCE_PCTS    = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0]

# Minimum absolute score change (0-1) in Round 3 that triggers a Round 4 sweep
# for that parameter.  0.005 = 0.5 percentage-point impact.
_ROUND4_IMPACT_THR = 0.005

# Weight applied to negatives in the composite score.
# 3 means "avoiding a false positive is 3× more important than finding one more dup."
_NEG_WEIGHT = 3

# Relaxed histogram floor for cross-format pairs — must match scanner constant.
_CF_HIST_FLOOR = 0.25


# ── ground truth ──────────────────────────────────────────────────────────────

@dataclass
class ExpectedGroup:
    folder_name: str
    all_files: list[Path]
    expected_original: Path        # largest by file size at load time;
    expected_previews: list[Path]  # re-determined by pixel count post-scan


@dataclass
class NegativePair:
    """A set of photos that look similar but must NOT end up in the same group."""
    folder_name: str
    all_files: list[Path]


@dataclass
class GroundTruth:
    groups:    list[ExpectedGroup]  # must group together
    negatives: list[NegativePair]   # must NOT group together
    singles:   list[Path]           # must not be grouped with anything


def load_ground_truth(calibration_root: Path) -> GroundTruth:
    """Parse calibration folder structure into GroundTruth."""
    groups_dir    = calibration_root / "groups"
    negatives_dir = calibration_root / "negatives"
    singles_dir   = calibration_root / "singles"

    groups:    list[ExpectedGroup] = []
    negatives: list[NegativePair]  = []
    singles:   list[Path]          = []

    if groups_dir.exists():
        for gf in sorted(groups_dir.iterdir()):
            if not gf.is_dir():
                continue
            files = [
                f for f in sorted(gf.iterdir())
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
            ]
            if len(files) < 2:
                continue
            by_size = sorted(files, key=lambda f: f.stat().st_size, reverse=True)
            groups.append(ExpectedGroup(
                folder_name=gf.name,
                all_files=files,
                expected_original=by_size[0],
                expected_previews=by_size[1:],
            ))

    if negatives_dir.exists():
        for nf in sorted(negatives_dir.iterdir()):
            if not nf.is_dir():
                continue
            files = [
                f for f in sorted(nf.iterdir())
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
            ]
            if len(files) < 2:
                continue
            negatives.append(NegativePair(folder_name=nf.name, all_files=files))

    if singles_dir.exists():
        for f in sorted(singles_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                singles.append(f)

    return GroundTruth(groups=groups, negatives=negatives, singles=singles)


def validate_calibration_folder(calibration_root: Path) -> tuple[bool, str]:
    """Return (is_valid, human-readable summary)."""
    if not calibration_root.exists():
        return False, "Folder does not exist."
    groups_dir = calibration_root / "groups"
    if not groups_dir.exists():
        return False, "Missing 'groups/' sub-folder."

    valid_groups = sum(
        1 for gf in groups_dir.iterdir()
        if gf.is_dir()
        and sum(1 for f in gf.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS) >= 2
    )
    if valid_groups == 0:
        return False, "No valid groups found (each sub-folder needs ≥ 2 images)."

    neg_dir = calibration_root / "negatives"
    neg_count = sum(
        1 for nf in neg_dir.iterdir()
        if nf.is_dir()
        and sum(1 for f in nf.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS) >= 2
    ) if neg_dir.exists() else 0

    singles_dir = calibration_root / "singles"
    singles_count = sum(
        1 for f in singles_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    ) if singles_dir.exists() else 0

    parts = [f"{valid_groups} group(s)"]
    if neg_count:
        parts.append(f"{neg_count} negative pair(s)")
    if singles_count:
        parts.append(f"{singles_count} single(s)")
    return True, ",  ".join(parts) + " ready."


# ── pixel-count refinement ────────────────────────────────────────────────────

def _refine_originals(
    ground_truth: GroundTruth,
    path_to_record: dict[Path, ImageRecord],
) -> None:
    """
    In-place: update each ExpectedGroup's expected_original and expected_previews
    using actual pixel count (width×height) from scanned ImageRecords.
    File size was the load-time proxy; pixel count is more accurate.

    Tiebreaker: when multiple files share the same pixel count (e.g. all 6000×4000),
    the larger file (more bytes = less compressed = higher quality) is considered the
    expected original.  This matches scanner._sort_key which also uses -file_size as
    the secondary sort key.
    """
    for eg in ground_truth.groups:
        resolved_all = [f.resolve() for f in eg.all_files]
        recs = [path_to_record.get(p) for p in resolved_all]
        recs = [r for r in recs if r is not None]
        if not recs:
            continue
        # Primary key: pixels (more = better original); secondary: file size (larger
        # file = less compressed = preferred when resolutions are equal, e.g. burst
        # shots or same-quality duplicates at the same resolution).
        best = max(recs, key=lambda r: (r.width * r.height, r.file_size))
        eg.expected_original  = best.path
        eg.expected_previews  = [
            r.path for r in recs if r.path.resolve() != best.path.resolve()
        ]


# ── pair-level diagnosis ──────────────────────────────────────────────────────

@dataclass
class PairDiagnosis:
    name_a: str
    name_b: str
    w_a: int;  h_a: int;  size_kb_a: float
    w_b: int;  h_b: int;  size_kb_b: float
    ar_diff_pct:    float
    brightness_diff: float
    phash_dist:     int
    dhash_dist:     Optional[int]  = None
    hist_sim:       Optional[float] = None
    same_dims:      bool           = False
    effective_threshold: int       = 0
    blocked_by:     Optional[str]  = None   # None = all guards passed → grouped
    a_role:         Optional[str]  = None   # "original" / "preview" / "ungrouped"
    b_role:         Optional[str]  = None


def _diagnose_pair(a: ImageRecord, b: ImageRecord, settings: Settings) -> PairDiagnosis:
    """
    Replicate every guard in _can_be_similar and capture each value so the
    log can show exactly which guard passed or blocked a pair.
    """
    ar_a = a.width / a.height if a.height else 1.0
    ar_b = b.width / b.height if b.height else 1.0
    ar_diff = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001) * 100.0

    brightness_diff = abs(a.brightness - b.brightness)

    tol = settings.series_tolerance_pct / 100.0
    same_dims = (
        a.width > 0 and b.width > 0
        and abs(a.width  - b.width)  / max(a.width,  b.width)  <= tol
        and abs(a.height - b.height) / max(a.height, b.height) <= tol
    )
    eff_thr = (int(settings.threshold * settings.series_threshold_factor)
               if same_dims else settings.threshold)
    if settings.dark_protection:
        if a.brightness < settings.dark_threshold or b.brightness < settings.dark_threshold:
            eff_thr = max(1, int(eff_thr * settings.dark_tighten_factor))

    phash_dist = a.phash - b.phash

    dhash_dist: Optional[int] = None
    if settings.use_dual_hash:
        dhash_dist = a.dhash - b.dhash

    hist_sim: Optional[float] = None
    if settings.use_histogram and a.histogram and b.histogram:
        hist_sim = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3.0

    blocked_by: Optional[str] = None
    if ar_diff > settings.ar_tolerance_pct:
        blocked_by = "AR"
    elif brightness_diff > settings.brightness_max_diff:
        blocked_by = "brightness"
    elif phash_dist > eff_thr:
        blocked_by = "pHash"
    elif dhash_dist is not None and phash_dist > 0 and dhash_dist > eff_thr * 1.5:
        # dHash is skipped when pHash==0 (perceptually identical); see scanner._can_be_similar
        blocked_by = "dHash"
    elif hist_sim is not None and hist_sim < settings.hist_min_similarity:
        blocked_by = "histogram"

    return PairDiagnosis(
        name_a=a.path.name, name_b=b.path.name,
        w_a=a.width, h_a=a.height, size_kb_a=a.file_size / 1024,
        w_b=b.width, h_b=b.height, size_kb_b=b.file_size / 1024,
        ar_diff_pct=ar_diff, brightness_diff=brightness_diff,
        phash_dist=phash_dist, dhash_dist=dhash_dist, hist_sim=hist_sim,
        same_dims=same_dims, effective_threshold=eff_thr, blocked_by=blocked_by,
    )


# ── diagnostic dataclasses ────────────────────────────────────────────────────

@dataclass
class GroupDiagnosis:
    folder_name:       str
    file_infos:        list[str]                     # "name  W×H  N KB"
    expected_original: str
    expected_previews: list[str]
    detected_together: bool
    split_into:        int                           # detected group count
    original_correct:  bool
    previews_correct:  list[tuple[str, bool]]        # (name, was_preview)
    pair_diagnoses:    list[PairDiagnosis]


@dataclass
class NegativeDiagnosis:
    """Diagnosis for a negative pair — files that must NOT end up in the same group."""
    folder_name:    str
    file_infos:     list[str]
    wrongly_merged: bool                              # True = false positive (bad)
    merged_pairs:   list[tuple[str, str]]             # which files were wrongly merged
    pair_diagnoses: list[PairDiagnosis]               # all guard values for every pair


@dataclass
class SingleDiagnosis:
    filename:            str
    dims:                str
    wrongly_grouped_with: Optional[str]
    pair:                Optional[PairDiagnosis]


# ── calibration result ────────────────────────────────────────────────────────

@dataclass
class CalibrationResult:
    threshold:     int
    preview_ratio: float
    score:         float

    groups_found:      int
    groups_total:      int
    originals_correct: int
    originals_total:   int
    previews_correct:  int
    previews_total:    int
    false_positives:   int
    negatives_correct: int = 0
    negatives_total:   int = 0

    round_number:  int = 0
    variant_label: str = ""


@dataclass
class CalibrationLog:
    rounds_run:          int
    total_combos:        int
    best_result:         CalibrationResult
    settings_used:       dict
    group_diagnoses:     list[GroupDiagnosis]
    negative_diagnoses:  list[NegativeDiagnosis]
    single_diagnoses:    list[SingleDiagnosis]
    feature_comparison:  list[CalibrationResult]
    # Round 4 results: list of (param_name, [CalibrationResult per value tested])
    parameter_sweeps:    list[tuple[str, list[CalibrationResult]]] = field(default_factory=list)
    # Best value found per parameter in Round 4 (param_name -> value)
    optimized_params:    dict = field(default_factory=dict)


# ── fast scoring (no diagnostics) ────────────────────────────────────────────

def _score(
    ground_truth: GroundTruth,
    detected_groups: list[DuplicateGroup],
    threshold: int,
    preview_ratio: float,
    round_number: int = 0,
    variant_label: str = "",
) -> CalibrationResult:
    path_to_dgroup: dict[Path, DuplicateGroup] = {}
    # Also build a path→record map from detected groups (ImageRecord carries file_size & dims).
    path_to_record: dict[Path, "ImageRecord"] = {}
    for dg in detected_groups:
        for r in dg.originals + dg.previews:
            path_to_dgroup[r.path.resolve()] = dg
            path_to_record[r.path.resolve()] = r

    groups_found = originals_correct = originals_total = 0
    previews_correct = previews_total = false_positives = 0

    for eg in ground_truth.groups:
        resolved_all  = [f.resolve() for f in eg.all_files]
        resolved_orig = eg.expected_original.resolve()
        resolved_prev = [f.resolve() for f in eg.expected_previews]

        ids = {id(path_to_dgroup[p]) for p in resolved_all if p in path_to_dgroup}
        originals_total += 1
        previews_total  += len(resolved_prev)

        if len(ids) == 1 and ids:
            groups_found += 1
            dg = path_to_dgroup.get(resolved_all[0])
            if dg:
                orig_ok = any(r.path.resolve() == resolved_orig for r in dg.originals)
                if not orig_ok:
                    # Accept a "better or equal" original: same or more pixels,
                    # same or more bytes.  This covers the case where adjacent
                    # expected groups merge because they are perceptually identical
                    # (pHash=0-2) and the algorithm correctly keeps the globally
                    # best copy rather than the per-pair expected original.
                    exp_rec = path_to_record.get(resolved_orig)
                    if exp_rec is not None:
                        exp_px = exp_rec.width * exp_rec.height
                        orig_ok = any(
                            (r.width * r.height > exp_px)
                            or (r.width * r.height == exp_px
                                and r.file_size >= exp_rec.file_size)
                            for r in dg.originals
                        )
                if orig_ok:
                    originals_correct += 1
                for p in resolved_prev:
                    if any(r.path.resolve() == p for r in dg.previews):
                        previews_correct += 1

    # Singles
    for sp in ground_truth.singles:
        if sp.resolve() in path_to_dgroup:
            false_positives += 1

    total_singles   = len(ground_truth.singles)
    singles_correct = total_singles - false_positives

    # Negatives — penalised at _NEG_WEIGHT
    neg_correct = 0
    for np in ground_truth.negatives:
        resolved = [f.resolve() for f in np.all_files]
        merged = any(
            path_to_dgroup.get(resolved[i]) is not None
            and path_to_dgroup.get(resolved[i]) is path_to_dgroup.get(resolved[j])
            for i in range(len(resolved))
            for j in range(i + 1, len(resolved))
        )
        if not merged:
            neg_correct += 1

    neg_total = len(ground_truth.negatives)

    max_score = (len(ground_truth.groups) * 2 + originals_total + previews_total
                 + total_singles + neg_total * _NEG_WEIGHT)
    actual = (groups_found * 2 + originals_correct + previews_correct
              + singles_correct + neg_correct * _NEG_WEIGHT)
    score = actual / max_score if max_score > 0 else 0.0

    return CalibrationResult(
        threshold=threshold, preview_ratio=preview_ratio, score=score,
        groups_found=groups_found, groups_total=len(ground_truth.groups),
        originals_correct=originals_correct, originals_total=originals_total,
        previews_correct=previews_correct, previews_total=previews_total,
        false_positives=false_positives,
        negatives_correct=neg_correct, negatives_total=neg_total,
        round_number=round_number, variant_label=variant_label,
    )


# ── verbose scoring (with full diagnostics) ───────────────────────────────────

def _score_verbose(
    ground_truth: GroundTruth,
    detected_groups: list[DuplicateGroup],
    all_records: list[ImageRecord],
    settings: Settings,
    round_number: int = 0,
    variant_label: str = "",
) -> tuple[CalibrationResult, list[GroupDiagnosis], list[NegativeDiagnosis], list[SingleDiagnosis]]:
    path_to_dgroup: dict[Path, DuplicateGroup] = {}
    for dg in detected_groups:
        for r in dg.originals + dg.previews:
            path_to_dgroup[r.path.resolve()] = dg

    path_to_record: dict[Path, ImageRecord] = {
        r.path.resolve(): r for r in all_records
    }

    # Refine expected originals using pixel count from actual scanned records
    _refine_originals(ground_truth, path_to_record)

    group_diags: list[GroupDiagnosis]    = []
    neg_diags:   list[NegativeDiagnosis] = []
    single_diags: list[SingleDiagnosis]  = []

    groups_found = originals_correct = originals_total = 0
    previews_correct_count = previews_total = false_positives = 0

    # ── positive groups ───────────────────────────────────────────────────────
    for eg in ground_truth.groups:
        resolved_all  = [f.resolve() for f in eg.all_files]
        resolved_orig = eg.expected_original.resolve()
        resolved_prev = [f.resolve() for f in eg.expected_previews]

        ids = {id(path_to_dgroup[p]) for p in resolved_all if p in path_to_dgroup}
        detected_together = len(ids) == 1 and bool(ids)

        originals_total += 1
        previews_total  += len(resolved_prev)

        orig_correct = False
        prev_correct_list: list[tuple[str, bool]] = []

        dg = path_to_dgroup.get(resolved_all[0]) if detected_together else None
        if detected_together and dg:
            groups_found += 1
            orig_correct = any(r.path.resolve() == resolved_orig for r in dg.originals)
            if not orig_correct:
                # Accept a "better or equal" original: same or more pixels + bytes.
                # Handles the case where adjacent expected groups merged because they
                # are perceptually identical (pHash=0-2); the algorithm correctly
                # keeps the globally best copy rather than the per-pair expected one.
                exp_rec = path_to_record.get(resolved_orig)
                if exp_rec is not None:
                    exp_px = exp_rec.width * exp_rec.height
                    orig_correct = any(
                        (r.width * r.height > exp_px)
                        or (r.width * r.height == exp_px
                            and r.file_size >= exp_rec.file_size)
                        for r in dg.originals
                    )
            if orig_correct:
                originals_correct += 1
            for p in resolved_prev:
                ok = any(r.path.resolve() == p for r in dg.previews)
                prev_correct_list.append((Path(p).name, ok))
                if ok:
                    previews_correct_count += 1

        recs = [path_to_record.get(p) for p in resolved_all]
        recs = [r for r in recs if r is not None]

        pair_diags: list[PairDiagnosis] = []
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                pd = _diagnose_pair(recs[i], recs[j], settings)
                if dg:
                    orig_paths = {r.path.resolve() for r in dg.originals}
                    prev_paths = {r.path.resolve() for r in dg.previews}
                    pd.a_role = ("original" if recs[i].path.resolve() in orig_paths
                                 else "preview" if recs[i].path.resolve() in prev_paths
                                 else "ungrouped")
                    pd.b_role = ("original" if recs[j].path.resolve() in orig_paths
                                 else "preview" if recs[j].path.resolve() in prev_paths
                                 else "ungrouped")
                pair_diags.append(pd)

        file_infos = [
            f"{r.path.name}  {r.width}×{r.height}  {r.file_size/1024:.0f} KB"
            for r in recs
        ]
        group_diags.append(GroupDiagnosis(
            folder_name=eg.folder_name,
            file_infos=file_infos,
            expected_original=eg.expected_original.name,
            expected_previews=[Path(p).name for p in resolved_prev],
            detected_together=detected_together,
            split_into=len(ids),
            original_correct=orig_correct,
            previews_correct=prev_correct_list,
            pair_diagnoses=pair_diags,
        ))

    # ── negative pairs ────────────────────────────────────────────────────────
    neg_correct = 0
    for np in ground_truth.negatives:
        resolved = [f.resolve() for f in np.all_files]
        recs = [path_to_record.get(p) for p in resolved]
        recs = [r for r in recs if r is not None]

        merged_pairs: list[tuple[str, str]] = []
        for i in range(len(resolved)):
            for j in range(i + 1, len(resolved)):
                dg_i = path_to_dgroup.get(resolved[i])
                dg_j = path_to_dgroup.get(resolved[j])
                if dg_i is not None and dg_i is dg_j:
                    merged_pairs.append((resolved[i].name, resolved[j].name))

        wrongly_merged = bool(merged_pairs)
        if not wrongly_merged:
            neg_correct += 1

        pair_diags = []
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                pair_diags.append(_diagnose_pair(recs[i], recs[j], settings))

        file_infos = [
            f"{r.path.name}  {r.width}×{r.height}  {r.file_size/1024:.0f} KB"
            for r in recs
        ]
        neg_diags.append(NegativeDiagnosis(
            folder_name=np.folder_name,
            file_infos=file_infos,
            wrongly_merged=wrongly_merged,
            merged_pairs=merged_pairs,
            pair_diagnoses=pair_diags,
        ))

    neg_total = len(ground_truth.negatives)

    # ── singles ───────────────────────────────────────────────────────────────
    for sp in ground_truth.singles:
        resolved = sp.resolve()
        dg = path_to_dgroup.get(resolved)
        wrec = path_to_record.get(resolved)
        if dg:
            false_positives += 1
            others = [r for r in (dg.originals + dg.previews)
                      if r.path.resolve() != resolved]
            pair: Optional[PairDiagnosis] = None
            grouped_with = None
            if others and wrec:
                other = min(others, key=lambda r: abs(wrec.phash - r.phash))
                pair = _diagnose_pair(wrec, other, settings)
                grouped_with = other.path.name
            single_diags.append(SingleDiagnosis(
                filename=sp.name,
                dims=f"{wrec.width}×{wrec.height}" if wrec else "?",
                wrongly_grouped_with=grouped_with,
                pair=pair,
            ))
        else:
            rec = path_to_record.get(resolved)
            single_diags.append(SingleDiagnosis(
                filename=sp.name,
                dims=f"{rec.width}×{rec.height}" if rec else "?",
                wrongly_grouped_with=None,
                pair=None,
            ))

    total_singles   = len(ground_truth.singles)
    singles_correct = total_singles - false_positives
    max_score = (len(ground_truth.groups) * 2 + originals_total + previews_total
                 + total_singles + neg_total * _NEG_WEIGHT)
    actual = (groups_found * 2 + originals_correct + previews_correct_count
              + singles_correct + neg_correct * _NEG_WEIGHT)
    score = actual / max_score if max_score > 0 else 0.0

    result = CalibrationResult(
        threshold=settings.threshold, preview_ratio=settings.preview_ratio,
        score=score,
        groups_found=groups_found, groups_total=len(ground_truth.groups),
        originals_correct=originals_correct, originals_total=originals_total,
        previews_correct=previews_correct_count, previews_total=previews_total,
        false_positives=false_positives,
        negatives_correct=neg_correct, negatives_total=neg_total,
        round_number=round_number, variant_label=variant_label,
    )
    return result, group_diags, neg_diags, single_diags


# ── fast calibration path: pre-computed pair distances ───────────────────────
#
# find_groups(records, cfg) is called ~100-150 times per calibration run.
# Each call recomputes identical pHash (7 rotation combos), dHash, and
# histogram distances for every pair.  Those numpy operations dominate.
#
# Strategy: compute all pair distances ONCE after hashing, store them as plain
# Python ints/floats in _PairData, then evaluate threshold decisions cheaply.
# Expected speedup: 20-50× (depends on calibration dataset size).


@dataclass(frozen=True, slots=True)
class _PairData:
    """All distance data for a pair that is invariant to calibration settings."""
    i: int
    j: int
    # pHash — minimum rotation-aware distance (all 7 combos pre-reduced)
    phash_rot_dist: int
    # pHash — direct comparison (no rotation), needed for is_rotated flag
    phash_norm_dist: int
    # dHash: -1 if unavailable (None dhash on either record)
    dhash_dist: int
    # Histogram intersection: -1.0 if unavailable
    hist_sim: float
    # Per-image brightness (needed for dark-protection check per combo)
    brightness_a: float
    brightness_b: float
    # Pre-computed brightness diff
    brightness_diff: float
    # AR: minimum of normal and rotated AR diff, in percent
    ar_min_diff_pct: float
    # Dimension ratios for same_dims re-evaluation (series_tolerance_pct varies)
    w_ratio: float
    h_ratio: float
    # Fixed flags
    cross_format: bool


def _build_pair_cache(records: list[ImageRecord]) -> list[_PairData]:
    """
    Pre-compute all pair-level data that is invariant to calibration settings.
    Called once after hashing; every _test() combo reuses these values.
    """
    pairs: list[_PairData] = []
    n = len(records)

    for i in range(n):
        a = records[i]
        ar_a = a.width / a.height if a.height else 1.0
        a_raw = a.path.suffix.lower() in RAW_EXTENSIONS

        for j in range(i + 1, n):
            b = records[j]
            ar_b = b.width / b.height if b.height else 1.0
            b_raw = b.path.suffix.lower() in RAW_EXTENSIONS

            # ── AR diffs ─────────────────────────────────────────────────────
            ar_diff_normal  = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001) * 100
            ar_diff_rotated = (
                abs(ar_a - 1.0 / ar_b) / max(ar_a, 1.0 / ar_b, 0.001) * 100
                if ar_b else 100.0
            )

            # ── pHash (7 rotation combos) ─────────────────────────────────────
            d_norm = int(a.phash - b.phash)
            best   = d_norm
            for bh in (b.phash_r90, b.phash_r180, b.phash_r270):
                if bh is not None:
                    best = min(best, int(a.phash - bh))
            for ah in (a.phash_r90, a.phash_r180, a.phash_r270):
                if ah is not None:
                    best = min(best, int(ah - b.phash))

            # ── dHash ─────────────────────────────────────────────────────────
            dhash_d = (
                int(a.dhash - b.dhash)
                if a.dhash is not None and b.dhash is not None
                else -1
            )

            # ── Histogram intersection ────────────────────────────────────────
            hist = -1.0
            if a.histogram and b.histogram:
                hist = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3.0

            # ── Dimension ratios ──────────────────────────────────────────────
            w_r = (abs(a.width  - b.width)  / max(a.width,  b.width)
                   if a.width  and b.width  else 1.0)
            h_r = (abs(a.height - b.height) / max(a.height, b.height)
                   if a.height and b.height else 1.0)

            pairs.append(_PairData(
                i=i, j=j,
                phash_rot_dist=best,
                phash_norm_dist=d_norm,
                dhash_dist=dhash_d,
                hist_sim=hist,
                brightness_a=a.brightness,
                brightness_b=b.brightness,
                brightness_diff=abs(a.brightness - b.brightness),
                ar_min_diff_pct=min(ar_diff_normal, ar_diff_rotated),
                w_ratio=w_r,
                h_ratio=h_r,
                cross_format=(a_raw != b_raw),
            ))

    # Sort ascending by rotation-aware pHash distance.
    # Allows early exit when phash_rot_dist exceeds the maximum possible
    # effective threshold — all remaining pairs cannot pass the pHash gate.
    pairs.sort(key=lambda p: p.phash_rot_dist)
    return pairs


def _find_groups_fast(
    pair_cache: list[_PairData],
    records:    list[ImageRecord],
    settings:   Settings,
) -> list[DuplicateGroup]:
    """
    Equivalent to find_groups(records, settings) but uses pre-computed pair
    distances from _build_pair_cache().  All threshold decisions are pure
    Python int/float comparisons — no numpy calls.

    Results are identical to find_groups() for the brute-force path (n < 200).
    For calibration datasets (typically 50-300 images) the two are equivalent.
    """
    n = len(records)

    # ── union-find ────────────────────────────────────────────────────────────
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(x: int, y: int) -> None:
        px, py = _find(x), _find(y)
        if px != py:
            parent[px] = py

    # ── read settings into locals (cheaper attribute access in tight loop) ────
    tol        = settings.series_tolerance_pct / 100.0
    threshold  = settings.threshold
    s_factor   = settings.series_threshold_factor
    cf_factor  = getattr(settings, "cross_format_threshold_factor", 5.0)
    dark_on    = settings.dark_protection
    dark_thr   = settings.dark_threshold
    dark_tight = settings.dark_tighten_factor
    ar_tol     = settings.ar_tolerance_pct
    bri_max    = settings.brightness_max_diff
    use_dhash  = settings.use_dual_hash
    use_hist   = settings.use_histogram
    hist_min   = settings.hist_min_similarity
    # Fixed rotation floor (not scaled by calibration threshold — see v1.0.5 fix)
    rot_floor  = int(2 * getattr(settings, "rotation_threshold_factor", 3.0))

    # Upper bound on any effective threshold for this settings combo.
    # Since pairs are sorted ascending by phash_rot_dist, we can break early.
    max_eff_thr = max(
        threshold,
        int(threshold * s_factor),
        int(threshold * cf_factor),
        rot_floor,
    )

    # ── pair evaluation ───────────────────────────────────────────────────────
    for pd in pair_cache:
        # Early exit: all remaining pairs have phash_rot_dist > max possible threshold
        if pd.phash_rot_dist > max_eff_thr:
            break

        # 1. AR guard
        if pd.ar_min_diff_pct > ar_tol:
            continue

        # 2. Cross-format relaxation factor
        eff_cf = cf_factor if pd.cross_format else 1.0

        # 3. Brightness guard
        if pd.brightness_diff > bri_max * eff_cf:
            continue

        # 4. Same-dimension check (re-evaluated per combo: series_tolerance_pct varies)
        same_dims = pd.w_ratio <= tol and pd.h_ratio <= tol

        # 5. Effective pHash threshold
        eff_thr = int(threshold * s_factor) if same_dims else threshold
        if pd.cross_format:
            eff_thr = max(eff_thr, int(threshold * eff_cf))
        if dark_on and (pd.brightness_a < dark_thr or pd.brightness_b < dark_thr):
            eff_thr = max(1, int(eff_thr * dark_tight))

        # 6. Rotation-lenient floor
        is_rotated = pd.phash_rot_dist < pd.phash_norm_dist
        if is_rotated:
            eff_thr = max(eff_thr, rot_floor)

        # 7. pHash gate
        if pd.phash_rot_dist > eff_thr:
            continue

        # 8. dHash gate (skipped for rotation matches and cross-format)
        if use_dhash and not pd.cross_format and not is_rotated and pd.phash_norm_dist > 0:
            if pd.dhash_dist >= 0 and pd.dhash_dist > eff_thr * 1.5:
                continue

        # 9. Histogram gate (relaxed floor for cross-format pairs)
        if use_hist and pd.hist_sim >= 0.0:
            if pd.cross_format:
                if pd.hist_sim < _CF_HIST_FLOOR:
                    continue
            elif pd.hist_sim < hist_min:
                continue

        _union(pd.i, pd.j)

    # ── group by union-find root ──────────────────────────────────────────────
    buckets: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        buckets[_find(i)].append(i)

    groups: list[DuplicateGroup] = []
    group_counter = 0
    for indices in buckets.values():
        if len(indices) < 2:
            continue
        group_counter += 1
        members = [records[i] for i in indices]
        result = _classify_group(members, settings, f"g{group_counter:04d}")
        if result is not None:
            groups.append(result)

    return groups


# ── public entry point ────────────────────────────────────────────────────────

def run_calibration(
    calibration_root: Path,
    base_settings: Settings,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
    stop_flag: Optional[list[bool]] = None,
) -> tuple[list[CalibrationResult], Optional[CalibrationLog]]:
    """
    3-round iterative calibration.

    Round 1 — Full threshold × preview_ratio grid  (45 combos).
    Round 2 — Dense preview_ratio around top-5 thresholds  (~30 new combos).
    Round 3 — Feature flag variants with the best settings  (~6 variants).

    Returns (all_results_sorted, detailed_log).
    """
    ground_truth = load_ground_truth(calibration_root)
    if not ground_truth.groups:
        return [], None

    if progress_cb:
        progress_cb("Hashing calibration images…", 0, 1)

    scan_cfg = copy.deepcopy(base_settings)
    scan_cfg.recursive           = True
    scan_cfg.ambiguous_detection = False
    scan_cfg.min_dimension       = 0
    scan_cfg.threshold           = max(ROUND1_THRESHOLDS)

    records = collect_images(
        calibration_root, skip_paths=set(),
        settings=scan_cfg,
        progress_cb=lambda msg, cur, tot, phase: (progress_cb(msg, 0, 1) if progress_cb else None),
        stop_flag=stop_flag,
    )
    if not records:
        return [], None

    # Refine expected originals once using actual pixel counts before any scoring
    path_to_record_early = {r.path.resolve(): r for r in records}
    _refine_originals(ground_truth, path_to_record_early)

    # Pre-compute all pair distances once — reused by every _test() call (20-50× speedup)
    pair_cache = _build_pair_cache(records)

    all_results: list[CalibrationResult] = []
    tested: set[tuple[int, float, str]] = set()

    def _test(threshold, ratio, rnd, label="", extra_cfg=None):
        key = (threshold, round(ratio, 4), label)
        if key in tested:
            return
        tested.add(key)

        cfg = copy.deepcopy(base_settings)
        cfg.threshold               = threshold
        cfg.preview_ratio           = ratio
        cfg.ambiguous_detection     = False
        cfg.series_threshold_factor = 1.0
        if extra_cfg:
            for k, v in extra_cfg.items():
                setattr(cfg, k, v)

        groups = _find_groups_fast(pair_cache, records, cfg)
        result = _score(ground_truth, groups, threshold, ratio, rnd, label)
        all_results.append(result)

    # ── Round 1: coarse grid ──────────────────────────────────────────────────
    r1_combos = len(ROUND1_THRESHOLDS) * len(ROUND1_RATIOS)
    done = 0
    for threshold in ROUND1_THRESHOLDS:
        for ratio in ROUND1_RATIOS:
            if stop_flag and stop_flag[0]:
                break
            if progress_cb:
                progress_cb(f"[Round 1] threshold={threshold}  ratio={ratio:.2f}",
                            done, r1_combos)
            _test(threshold, ratio, rnd=1)
            done += 1

    if stop_flag and stop_flag[0]:
        all_results.sort(key=lambda r: r.score, reverse=True)
        return all_results, None

    # ── Round 2: fine ratio around top-5 thresholds ───────────────────────────
    top5 = sorted(all_results, key=lambda r: r.score, reverse=True)[:5]
    top_thresholds = list(dict.fromkeys(r.threshold for r in top5))
    best_ratio_r1  = top5[0].preview_ratio
    r2_ratios = sorted({
        round(best_ratio_r1 + d, 3)
        for d in [-0.04, -0.03, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04]
        if 0.60 <= best_ratio_r1 + d <= 0.99
    })
    r2_combos = len(top_thresholds) * len(r2_ratios)
    done = 0
    for threshold in top_thresholds:
        for ratio in r2_ratios:
            if stop_flag and stop_flag[0]:
                break
            if progress_cb:
                progress_cb(f"[Round 2] threshold={threshold}  ratio={ratio:.3f}",
                            done, r2_combos)
            _test(threshold, ratio, rnd=2)
            done += 1

    if stop_flag and stop_flag[0]:
        all_results.sort(key=lambda r: r.score, reverse=True)
        return all_results, None

    # ── Round 3: feature flag variants ───────────────────────────────────────
    best = max(all_results, key=lambda r: r.score)
    feature_variants = [
        ("no_series_detect", {"disable_series_detection": True}),
        ("no_dual_hash",     {"use_dual_hash": False}),
        ("no_histogram",     {"use_histogram": False}),
        ("no_dark_protect",  {"dark_protection": False}),
        ("loose_AR",         {"ar_tolerance_pct": base_settings.ar_tolerance_pct * 2}),
        ("tight_AR",         {"ar_tolerance_pct": max(1.0, base_settings.ar_tolerance_pct / 2)}),
        ("loose_brightness", {"brightness_max_diff": base_settings.brightness_max_diff * 2}),
    ]
    r3_combos = len(feature_variants)
    done = 0
    for label, extra in feature_variants:
        if stop_flag and stop_flag[0]:
            break
        if progress_cb:
            progress_cb(f"[Round 3] {label}", done, r3_combos)
        _test(best.threshold, best.preview_ratio, rnd=3, label=label, extra_cfg=extra)
        done += 1

    # ── Round 4: conditional per-parameter sweeps ────────────────────────────
    # For each guard that showed measurable impact in Round 3, sweep its
    # numeric parameter to find the optimum value.  series_threshold_factor
    # is always swept since series detection is critical on most datasets.
    feature_results_r4 = [r for r in all_results if r.round_number == 3]
    feature_map = {r.variant_label: r for r in feature_results_r4}

    def _r3_delta(label: str) -> float:
        """Absolute score gap between baseline and feature variant (positive = variant worse)."""
        r = feature_map.get(label)
        return abs(best.score - r.score) if r else 0.0

    # Build ordered list of (param_name, values) to sweep
    r4_sweeps: list[tuple[str, list]] = []
    # series_threshold_factor — always, because series detection dominates most datasets
    r4_sweeps.append(("series_threshold_factor", ROUND4_SERIES_THRESHOLD_FACTORS))
    # histogram similarity — sweep if disabling histogram had measurable impact
    if _r3_delta("no_histogram") >= _ROUND4_IMPACT_THR:
        r4_sweeps.append(("hist_min_similarity", ROUND4_HIST_MIN_SIMILARITIES))
    # brightness limit — sweep if loosening brightness changed the score
    if _r3_delta("loose_brightness") >= _ROUND4_IMPACT_THR:
        r4_sweeps.append(("brightness_max_diff", ROUND4_BRIGHTNESS_MAX_DIFFS))
    # AR tolerance — sweep if either loose or tight variant showed impact
    ar_impact = max(_r3_delta("loose_AR"), _r3_delta("tight_AR"))
    if ar_impact >= _ROUND4_IMPACT_THR:
        r4_sweeps.append(("ar_tolerance_pct", ROUND4_AR_TOLERANCE_PCTS))
    # cross_format_threshold_factor — sweep only when RAW files are present
    if any(r.path.suffix.lower() in RAW_EXTENSIONS for r in records):
        r4_sweeps.append(("cross_format_threshold_factor", ROUND4_CF_THRESHOLD_FACTORS))
    # dark_threshold + dark_tighten_factor — sweep if disabling dark protection had impact
    if _r3_delta("no_dark_protect") >= _ROUND4_IMPACT_THR:
        r4_sweeps.append(("dark_threshold",     ROUND4_DARK_THRESHOLDS))
        r4_sweeps.append(("dark_tighten_factor", ROUND4_DARK_TIGHTEN_FACTORS))
    # series_tolerance_pct — sweep if series detection had impact (i.e. series exist in data)
    if _r3_delta("no_series_detect") >= _ROUND4_IMPACT_THR:
        r4_sweeps.append(("series_tolerance_pct", ROUND4_SERIES_TOLERANCE_PCTS))

    parameter_sweep_results: list[tuple[str, list[CalibrationResult]]] = []
    optimized_params: dict = {}

    r4_total = sum(len(vals) for _, vals in r4_sweeps)
    r4_done  = 0
    for param_name, values in r4_sweeps:
        if stop_flag and stop_flag[0]:
            break
        sweep: list[CalibrationResult] = []
        for val in values:
            if stop_flag and stop_flag[0]:
                break
            label = f"{param_name}={val}"
            if progress_cb:
                progress_cb(f"[Round 4] {param_name}={val}", r4_done, r4_total)
            _test(best.threshold, best.preview_ratio, rnd=4, label=label,
                  extra_cfg={param_name: val})
            r = next((x for x in all_results if x.variant_label == label), None)
            if r:
                sweep.append(r)
            r4_done += 1
        if sweep:
            parameter_sweep_results.append((param_name, sweep))
            best_in_sweep = max(sweep, key=lambda x: x.score)
            val_str = best_in_sweep.variant_label.split("=", 1)[1]
            try:
                optimized_params[param_name] = float(val_str)
            except ValueError:
                optimized_params[param_name] = val_str

    # ── Verbose log for the best result ───────────────────────────────────────
    all_results.sort(key=lambda r: r.score, reverse=True)
    best_final = all_results[0]

    log_cfg = copy.deepcopy(base_settings)
    log_cfg.threshold               = best_final.threshold
    log_cfg.preview_ratio           = best_final.preview_ratio
    log_cfg.ambiguous_detection     = False
    log_cfg.series_threshold_factor = 1.0

    # Apply the variant's extra flags so the verbose log matches the actual score
    if best_final.variant_label:
        variant_extra = [e for lbl, e in feature_variants if lbl == best_final.variant_label]
        for extra in variant_extra:
            for k, v in extra.items():
                setattr(log_cfg, k, v)

    # Apply Round 4 optimized param so the verbose log uses the actual best setting
    # (e.g. series_threshold_factor=0.5) rather than the 1.0 baseline written above.
    if best_final.round_number == 4 and best_final.variant_label and "=" in best_final.variant_label:
        param_name, val_str = best_final.variant_label.split("=", 1)
        try:
            setattr(log_cfg, param_name, float(val_str))
        except (ValueError, AttributeError):
            pass

    log_groups = _find_groups_fast(pair_cache, records, log_cfg)

    path_to_record = {r.path.resolve(): r for r in records}
    _refine_originals(ground_truth, path_to_record)   # refine before verbose score

    _, group_diags, neg_diags, single_diags = _score_verbose(
        ground_truth, log_groups, records, log_cfg,
        round_number=best_final.round_number,
    )

    feature_results = [r for r in all_results if r.round_number == 3]

    settings_snapshot = {
        "ar_tolerance_pct":                  base_settings.ar_tolerance_pct,
        "brightness_max_diff":               base_settings.brightness_max_diff,
        "use_dual_hash":                     base_settings.use_dual_hash,
        "use_histogram":                     base_settings.use_histogram,
        "hist_min_similarity":               base_settings.hist_min_similarity,
        "dark_protection":                   base_settings.dark_protection,
        "dark_threshold":                    base_settings.dark_threshold,
        "dark_tighten_factor":               base_settings.dark_tighten_factor,
        "series_tolerance_pct":              base_settings.series_tolerance_pct,
        "series_threshold_factor":           base_settings.series_threshold_factor,
        "disable_series_detection":          base_settings.disable_series_detection,
        "cross_format_threshold_factor":     getattr(base_settings, "cross_format_threshold_factor", 5.0),
    }

    rounds_run = 4 if parameter_sweep_results else 3

    log = CalibrationLog(
        rounds_run=rounds_run,
        total_combos=len(tested),
        best_result=best_final,
        settings_used=settings_snapshot,
        group_diagnoses=group_diags,
        negative_diagnoses=neg_diags,
        single_diagnoses=single_diags,
        feature_comparison=feature_results,
        parameter_sweeps=parameter_sweep_results,
        optimized_params=optimized_params,
    )
    return all_results, log


# ── log formatter ─────────────────────────────────────────────────────────────

def format_log(log: CalibrationLog) -> str:
    """Human-readable, copy-paste-friendly diagnostic report."""
    lines: list[str] = []

    def h(text=""):
        lines.append(text)

    def sep(char="─", width=72):
        lines.append(char * width)

    sep("═")
    h("CALIBRATION DIAGNOSTIC LOG")
    sep("═")
    h(f"Rounds run         : {log.rounds_run}")
    h(f"Total combos tested: {log.total_combos}")
    br = log.best_result
    h(f"Best result        : threshold={br.threshold}  "
      f"preview_ratio={br.preview_ratio:.3f}  "
      f"score={br.score * 100:.1f}%  (round {br.round_number})")
    h(f"                   : groups {br.groups_found}/{br.groups_total}  "
      f"originals {br.originals_correct}/{br.originals_total}  "
      f"previews {br.previews_correct}/{br.previews_total}  "
      f"negatives {br.negatives_correct}/{br.negatives_total}  "
      f"fp={br.false_positives}")
    h()
    h("Constant settings during calibration:")
    for k, v in log.settings_used.items():
        h(f"  {k:<30} = {v}")

    # ── feature comparison ────────────────────────────────────────────────────
    if log.feature_comparison:
        h()
        sep()
        h("ROUND 3 — FEATURE FLAG COMPARISON  (baseline = best settings above)")
        sep()
        base_score = log.best_result.score
        for r in sorted(log.feature_comparison, key=lambda x: x.score, reverse=True):
            delta = (r.score - base_score) * 100
            sign  = "+" if delta >= 0 else ""
            h(f"  {r.variant_label:<22}  score={r.score*100:.1f}%  "
              f"({sign}{delta:.1f}pp)  "
              f"groups={r.groups_found}/{r.groups_total}  "
              f"neg={r.negatives_correct}/{r.negatives_total}  "
              f"fp={r.false_positives}")

    # ── Round 4: parameter sweeps ─────────────────────────────────────────────
    if log.parameter_sweeps:
        h()
        sep()
        h("ROUND 4 — PARAMETER SWEEPS  (each param swept independently at best threshold/ratio)")
        sep()
        base_score = log.best_result.score
        for param_name, sweep_results in log.parameter_sweeps:
            if not sweep_results:
                continue
            best_val = log.optimized_params.get(param_name)
            h(f"  {param_name}:")
            for r in sorted(sweep_results,
                            key=lambda x: float(x.variant_label.split("=", 1)[1])):
                val_str = r.variant_label.split("=", 1)[1]
                delta   = (r.score - base_score) * 100
                sign    = "+" if delta >= 0 else ""
                try:
                    is_best = best_val is not None and abs(float(val_str) - best_val) < 1e-9
                except (ValueError, TypeError):
                    is_best = (val_str == str(best_val))
                mark = "  ← best" if is_best else ""
                h(f"    {val_str:<8}  score={r.score*100:.1f}%  ({sign}{delta:.1f}pp){mark}")
        if log.optimized_params:
            h()
            h("  Recommended settings from Round 4:")
            for param, val in log.optimized_params.items():
                base_val = log.settings_used.get(param, "?")
                changed  = not (isinstance(val, float) and isinstance(base_val, float)
                                and abs(val - base_val) < 1e-9)
                arrow = "  ← CHANGED" if changed else "  (unchanged)"
                h(f"    {param:<38} = {val}{arrow}")

    # ── group diagnostics ─────────────────────────────────────────────────────
    h()
    sep()
    h(f"POSITIVE GROUPS  ({len(log.group_diagnoses)} expected — algorithm must detect these)")
    sep()
    for gd in log.group_diagnoses:
        status = "✓ DETECTED" if gd.detected_together else f"✗ MISSED (split into {gd.split_into} group(s))"
        h(f"\n  [{gd.folder_name}]  {status}")
        for info in gd.file_infos:
            tag = "  ← expected ORIGINAL" if gd.expected_original in info else ""
            h(f"    {info}{tag}")
        if gd.detected_together:
            h(f"    Classification — original: {'✓' if gd.original_correct else '✗'}")
            for name, ok in gd.previews_correct:
                h(f"                     preview '{name}': {'✓' if ok else '✗'}")
        _format_pairs(gd.pair_diagnoses, log.settings_used, lines, indent="    ")

    # ── negative diagnostics ──────────────────────────────────────────────────
    h()
    sep()
    h(f"NEGATIVE PAIRS  ({len(log.negative_diagnoses)} expected — algorithm must NOT group these)")
    sep()
    h(f"  (each false merge is penalised {_NEG_WEIGHT}× in the score)")
    for nd in log.negative_diagnoses:
        status = "✗ FALSE POSITIVE — wrongly merged!" if nd.wrongly_merged else "✓ Correctly kept separate"
        h(f"\n  [{nd.folder_name}]  {status}")
        for info in nd.file_infos:
            h(f"    {info}")
        if nd.wrongly_merged:
            for a, b in nd.merged_pairs:
                h(f"    !! Merged pair: '{a}'  ↔  '{b}'")
        _format_pairs(nd.pair_diagnoses, log.settings_used, lines, indent="    ")

    # ── singles ───────────────────────────────────────────────────────────────
    h()
    sep()
    h(f"SINGLES  ({len(log.single_diagnoses)} expected — must stay ungrouped)")
    sep()
    for sd in log.single_diagnoses:
        if sd.wrongly_grouped_with is None:
            h(f"\n  '{sd.filename}'  {sd.dims}  → ✓ ungrouped")
        else:
            h(f"\n  '{sd.filename}'  {sd.dims}  → ✗ FALSE POSITIVE — merged with '{sd.wrongly_grouped_with}'")
            if sd.pair:
                _format_pair_block(sd.pair, log.settings_used, lines, indent="    ")

    # ── recommendations ───────────────────────────────────────────────────────
    h()
    sep()
    h("RECOMMENDATIONS")
    sep()

    missed     = [gd for gd in log.group_diagnoses    if not gd.detected_together]
    fp_neg     = [nd for nd in log.negative_diagnoses if nd.wrongly_merged]
    fp_singles = [sd for sd in log.single_diagnoses   if sd.wrongly_grouped_with]

    if not missed and not fp_neg and not fp_singles:
        h("  ✓ Perfect score on this dataset with the selected settings.")
    else:
        if missed:
            blocked: dict[str, int] = {}
            for gd in missed:
                for pd in gd.pair_diagnoses:
                    if pd.blocked_by:
                        blocked[pd.blocked_by] = blocked.get(pd.blocked_by, 0) + 1
            h(f"  {len(missed)} missed group(s). Most common block:")
            tips = {
                "pHash":      f"→ raise threshold (current {br.threshold})",
                "AR":         "→ raise ar_tolerance_pct",
                "brightness": "→ raise brightness_max_diff",
                "dHash":      "→ disable dual hash (use_dual_hash=False)",
                "histogram":  "→ lower hist_min_similarity",
            }
            for guard, count in sorted(blocked.items(), key=lambda x: -x[1]):
                h(f"    {guard}: {count} pair(s)  {tips.get(guard, '')}")

        if fp_neg or fp_singles:
            total_fp = len(fp_neg) + len(fp_singles)
            h(f"  {total_fp} false positive(s). To fix:")
            h(f"    → lower threshold (current {br.threshold})")
            h(f"    → enable histogram or dual hash to add extra separation guard")

    if log.feature_comparison:
        best_feat = max(log.feature_comparison, key=lambda r: r.score)
        if best_feat.score > br.score + 0.005:
            h()
            h(f"  Feature '{best_feat.variant_label}' scores higher "
              f"({best_feat.score*100:.1f}% vs {br.score*100:.1f}%)")
            h(f"  → Consider enabling that flag in default settings.")

    sep("═")
    return "\n".join(lines)


# ── log formatting helpers ─────────────────────────────────────────────────────

def _format_pairs(
    pair_diags: list[PairDiagnosis],
    settings_used: dict,
    lines: list[str],
    indent: str,
) -> None:
    if not pair_diags:
        return
    lines.append(f"{indent}Pair analysis:")
    for pd in pair_diags:
        _format_pair_block(pd, settings_used, lines, indent + "  ")


def _format_pair_block(
    pd: PairDiagnosis,
    settings_used: dict,
    lines: list[str],
    indent: str,
) -> None:
    pass_fail = ("PASS → grouped" if pd.blocked_by is None
                 else f"FAIL — blocked by {pd.blocked_by}")
    lines.append(f"{indent}{pd.name_a}  ↔  {pd.name_b}")
    lines.append(
        f"{indent}  Dims       : {pd.w_a}×{pd.h_a} ({pd.size_kb_a:.0f} KB)"
        f"  vs  {pd.w_b}×{pd.h_b} ({pd.size_kb_b:.0f} KB)"
        + ("  [same dims]" if pd.same_dims else "")
    )
    ar_lim  = settings_used.get("ar_tolerance_pct", "?")
    br_lim  = settings_used.get("brightness_max_diff", "?")
    hs_min  = settings_used.get("hist_min_similarity", "?")
    ar_ok   = "OK" if isinstance(ar_lim, (int, float)) and pd.ar_diff_pct <= ar_lim else "FAIL"
    br_ok   = "OK" if isinstance(br_lim, (int, float)) and pd.brightness_diff <= br_lim else "FAIL"
    ph_ok   = "OK" if pd.phash_dist <= pd.effective_threshold else "FAIL"
    lines.append(f"{indent}  AR diff    : {pd.ar_diff_pct:.2f}%  [limit {ar_lim}%]  {ar_ok}")
    lines.append(f"{indent}  Brightness : {pd.brightness_diff:.1f}  [limit {br_lim}]  {br_ok}")
    lines.append(f"{indent}  pHash      : {pd.phash_dist}  [limit {pd.effective_threshold}]  {ph_ok}")
    if pd.dhash_dist is not None:
        dh_lim = pd.effective_threshold * 1.5
        dh_ok  = "OK" if pd.dhash_dist <= dh_lim else "FAIL"
        lines.append(f"{indent}  dHash      : {pd.dhash_dist}  [limit {dh_lim:.0f}]  {dh_ok}")
    if pd.hist_sim is not None:
        hs_ok = "OK" if isinstance(hs_min, float) and pd.hist_sim >= hs_min else "FAIL"
        lines.append(f"{indent}  Histogram  : {pd.hist_sim:.3f}  [min {hs_min}]  {hs_ok}")
    lines.append(f"{indent}  → {pass_fail}")
    if pd.a_role and pd.b_role and pd.blocked_by is None:
        lines.append(f"{indent}  Roles: {pd.name_a}={pd.a_role}  {pd.name_b}={pd.b_role}")
