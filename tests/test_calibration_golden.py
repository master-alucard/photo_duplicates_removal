"""
tests/test_calibration_golden.py — Detection-accuracy golden master
(Stage 0 of the 2026-07 refactor plan).

Runs the real scan pipeline (``collect_images`` -> ``find_groups``) against the
three calibration corpora and asserts that every ground-truth duplicate group is
still detected, and that the raw group count has not drifted.

This is the safety net for the scan-unification refactor (Stages 2-4): a change
that silently alters grouping behavior will fail here even if every unit test
still passes.

DATA DEPENDENCY
---------------
Requires the calibration corpora on E:\\MEDIA\\test\\. Those are large photo
folders that are not in the repo, so these tests SKIP automatically when the
folders are absent — CI stays green, local runs enforce the gate.

REFRESHING THE BASELINE
-----------------------
The expected numbers below are a deliberate contract, not incidental values.
Only change them when a detection change is intended and reviewed; run
``/calibrate-deduper --baseline`` (or ``python _headless_scan.py``) and update
both the constants and the reason in the changelog.

SETTINGS ARE DEFINED HERE, DELIBERATELY
---------------------------------------
The test builds its own per-folder settings rather than reading the user's
``settings.json`` (machine-dependent) or reusing
``_calib_runner._make_settings_for_folder``. That helper documents the JPEG
corpus as "pure JPEG pairs — default settings are fine", which measurement
contradicts: the corpus contains RAW files, and 12 of its 418 ground-truth
groups are only detected with ``use_rawpy=True`` (406/418 without it). Owning
the settings here keeps the gate honest and self-describing.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from _calib_runner import CALIB_FOLDERS
from _headless_scan import scan_and_score
from config import Settings


# Golden master recorded 2026-07-15 on develop @ v1.2.3 (commit f17fc9d),
# immediately before the scan-unification refactor. All three corpora detect
# 100% of their ground-truth groups under the settings built by _settings_for().
#   gt_total   — every expected group must still be detected (asserted == 100%).
#   app_groups — raw DuplicateGroup count; guards against over-merging or
#                splitting that would leave GT detection at 100% while
#                degrading the user-visible result.
GOLDEN = {
    "RAW":      {"app_groups": 35,  "gt_total": 35},
    "JPEG":     {"app_groups": 393, "gt_total": 418},
    "CrossFmt": {"app_groups": 35,  "gt_total": 35},
}


def _settings_for(name: str) -> Settings:
    """Deterministic per-folder settings. Defaults except where a corpus needs
    a specific RAW-handling mode."""
    s = Settings()
    # RAW support is bundled in the shipped app and enabled in practice; all
    # three corpora contain RAW files (including the "JPEG" one — see above).
    s.use_rawpy = True
    if name in ("RAW", "CrossFmt"):
        # These corpora expect the RAW as original and its companion JPEG as
        # the duplicate, rather than keeping one of each format.
        s.keep_all_formats = False
    if name == "RAW":
        # Ground truth was built from the camera-embedded JPEG preview, which
        # is pixel-identical to the companion JPEG (pHash distance 0). With
        # rawpy postprocess the same pairs land 22-36 bits apart and are missed.
        s.raw_use_embedded_thumb = True
    return s


def _folder_for(name: str) -> Path:
    return CALIB_FOLDERS[name]


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_calibration_detection_is_unchanged(name):
    folder = _folder_for(name)
    if not folder.exists():
        pytest.skip(f"calibration corpus not present: {folder}")

    expected = GOLDEN[name]
    settings = _settings_for(name)

    t0 = time.perf_counter()
    app_groups, gt_matched, gt_total = scan_and_score(folder, settings)
    elapsed = time.perf_counter() - t0

    # 1. The ground-truth corpus itself must not have changed underneath us.
    assert gt_total == expected["gt_total"], (
        f"[{name}] ground-truth group count changed "
        f"({gt_total} vs recorded {expected['gt_total']}). The corpus was "
        f"edited — re-record the baseline deliberately rather than relaxing "
        f"this test."
    )

    # 2. Every expected duplicate group is still detected. This is the metric
    #    that matters to users: a miss here means real duplicates go unfound.
    assert gt_matched == gt_total, (
        f"[{name}] DETECTION REGRESSION: {gt_matched}/{gt_total} ground-truth "
        f"groups detected (was 100%). Scan completed in {elapsed:.1f}s."
    )

    # 3. Raw group count is stable. Catches over-merging / splitting that can
    #    keep GT detection at 100% while degrading the user-visible result.
    assert app_groups == expected["app_groups"], (
        f"[{name}] group count drifted: {app_groups} vs recorded "
        f"{expected['app_groups']}. GT detection is still "
        f"{gt_matched}/{gt_total}, so this is a grouping-shape change, not a "
        f"miss — confirm it is intended before updating the baseline."
    )


@pytest.mark.slow
def test_all_corpora_present_or_all_skipped():
    """Guard against a half-configured machine silently testing only part of
    the corpus set (e.g. one drive mounted, another not)."""
    present = [n for n in GOLDEN if _folder_for(n).exists()]
    if not present:
        pytest.skip("no calibration corpora present on this machine")
    missing = [n for n in GOLDEN if not _folder_for(n).exists()]
    assert not missing, (
        f"Partial calibration data: {present} present but {missing} missing. "
        f"Detection coverage would be incomplete and the gate misleading."
    )
