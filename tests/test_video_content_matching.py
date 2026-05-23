"""
tests/test_video_content_matching.py -- Tests for content-based video duplicate
detection (multi-frame pHash + duration proximity).

All tests use synthetic data; no real video files or ffmpeg calls needed.
Covers:
  - _video_content_match() predicate
  - find_video_duplicates() Pass B (content mode)
  - False-positive guards (duration gate, frame mismatch)
  - Settings interactions (format, size, content flags)
  - Pass A + Pass B no double-grouping
"""
from __future__ import annotations

from pathlib import Path

import imagehash
import numpy as np

from config import Settings
from scanner import (
    ImageRecord,
    find_video_duplicates,
    _video_content_match,
    _CONTENT_FRAME_HAMMING_THR,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _zero_hash() -> imagehash.ImageHash:
    return imagehash.ImageHash(np.zeros((8, 8), dtype=bool))


def _nonzero_hash(seed: int = 1) -> imagehash.ImageHash:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 2, (8, 8), dtype=bool)
    return imagehash.ImageHash(arr)


def _phash_str(seed: int) -> str:
    return str(_nonzero_hash(seed))


def _video_rec(
    path: str,
    size: int,
    mtime: float = 0.0,
    duration: float | None = None,
    frame_hashes: list[str] | None = None,
) -> ImageRecord:
    rec = ImageRecord(
        path=Path(path), width=0, height=0, file_size=size,
        phash=_zero_hash(), dhash=_zero_hash(), mtime=mtime,
        brightness=128.0, histogram=[], is_video=True,
    )
    rec._video_duration = duration           # type: ignore[attr-defined]
    rec._video_frame_hashes = frame_hashes or []  # type: ignore[attr-defined]
    return rec


def _matching_frames(seed: int = 42) -> list[str]:
    """5 identical hashes -- simulates same-content pair."""
    h = _phash_str(seed)
    return [h] * 5


def _different_frames(base_seed: int = 42) -> list[str]:
    """5 hashes with high pairwise distances -- different content."""
    return [_phash_str(base_seed + i * 100) for i in range(5)]


def _s(**kw) -> Settings:
    """Default settings for content-only matching."""
    d = dict(video_match_format=False, video_match_size=False,
             video_match_content=True, video_use_thumb=False)
    d.update(kw)
    return Settings(**d)


# ── _video_content_match unit tests ──────────────────────────────────────────

def test_vcm_identical_same_duration():
    h = _matching_frames()
    assert _video_content_match(h, h, 10.0, 10.0) is True


def test_vcm_duration_too_large():
    """13 s vs 12 s = 8.3% > 5% limit -- must not match."""
    h = _matching_frames()
    assert _video_content_match(h, h, 13.0, 12.0) is False


def test_vcm_duration_within_threshold():
    """10.0 s vs 10.4 s = 4% < 5% -- should match."""
    h = _matching_frames()
    assert _video_content_match(h, h, 10.0, 10.4) is True


def test_vcm_none_duration():
    h = _matching_frames()
    assert _video_content_match(h, h, None, 10.0) is False
    assert _video_content_match(h, h, 10.0, None) is False


def test_vcm_empty_hashes():
    assert _video_content_match([], [], 10.0, 10.0) is False
    assert _video_content_match(_matching_frames(), [], 10.0, 10.0) is False


def test_vcm_different_content_no_match():
    """Two different-content videos with matching duration must not group."""
    ha = _different_frames(1)
    hb = _different_frames(200)
    assert _video_content_match(ha, hb, 12.0, 12.0) is False


def test_vcm_mostly_matching_frames():
    """4/5 matching frames (80%) satisfies the 60% threshold."""
    base = _matching_frames(seed=7)
    modified = list(base)
    modified[-1] = _phash_str(999)
    dist = imagehash.hex_to_hash(base[-1]) - imagehash.hex_to_hash(modified[-1])
    if dist > _CONTENT_FRAME_HAMMING_THR:
        # Only assert if the replacement hash is genuinely far enough
        assert _video_content_match(base, modified, 10.0, 10.0) is True


def test_vcm_single_shared_intro_frame_no_match():
    """Videos sharing only position-0 frame (1/5 = 20%) must not match."""
    shared = _phash_str(42)
    ha = [shared] + [_phash_str(i) for i in range(1, 5)]
    hb = [shared] + [_phash_str(i + 100) for i in range(1, 5)]
    all_diff = all(
        imagehash.hex_to_hash(ha[i]) - imagehash.hex_to_hash(hb[i]) > _CONTENT_FRAME_HAMMING_THR
        for i in range(1, 5)
    )
    if all_diff:
        assert _video_content_match(ha, hb, 10.0, 10.0) is False


# ── find_video_duplicates content mode tests ─────────────────────────────────

def test_content_dup_different_size_groups():
    """Two same-content videos with different byte sizes MUST group (the core bug fix)."""
    h = _matching_frames(seed=3)
    recs = [
        _video_rec("a.mp4", 1024, duration=10.0, frame_hashes=h),
        _video_rec("b.mp4", 2048, duration=10.0, frame_hashes=h),  # different size
    ]
    groups = find_video_duplicates(recs, _s())
    assert len(groups) == 1
    assert len(groups[0].originals + groups[0].previews) == 2


def test_content_dup_not_ambiguous():
    """Content-matched groups (multi-frame + duration) must NOT be is_ambiguous."""
    h = _matching_frames(seed=5)
    recs = [
        _video_rec("a.mp4", 1000, duration=12.0, frame_hashes=h),
        _video_rec("b.mp4", 3000, duration=12.0, frame_hashes=h),
    ]
    groups = find_video_duplicates(recs, _s())
    assert len(groups) == 1
    assert groups[0].is_ambiguous is False


def test_unrelated_videos_no_group():
    """Three unrelated videos (different frames, different durations) must not group."""
    recs = [
        _video_rec("a.mp4", 1000, duration=12.0, frame_hashes=_different_frames(1)),
        _video_rec("b.mp4", 2000, duration=12.0, frame_hashes=_different_frames(200)),
        _video_rec("c.mp4", 3000, duration=90.0, frame_hashes=_different_frames(400)),
    ]
    assert find_video_duplicates(recs, _s()) == []


def test_duration_gate_prevents_false_positive():
    """13 s vs 12 s = 8.3% diff -- must not group even if all frame hashes match."""
    h = _matching_frames(seed=9)
    recs = [
        _video_rec("a.mp4", 1000, duration=13.0, frame_hashes=h),
        _video_rec("b.mp4", 2000, duration=12.0, frame_hashes=h),
    ]
    assert find_video_duplicates(recs, _s()) == []


def test_content_disabled_no_group():
    """With video_match_content=False, different-size videos must not group."""
    h = _matching_frames(seed=11)
    recs = [
        _video_rec("a.mp4", 1024, duration=10.0, frame_hashes=h),
        _video_rec("b.mp4", 2048, duration=10.0, frame_hashes=h),
    ]
    s = _s(video_match_content=False, video_match_size=False, video_match_format=False)
    assert find_video_duplicates(recs, s) == []


def test_format_restriction_in_content_pass():
    """When video_match_format=True, .mp4 vs .mov must NOT group in content pass."""
    h = _matching_frames(seed=13)
    recs = [
        _video_rec("a.mp4", 1000, duration=10.0, frame_hashes=h),
        _video_rec("b.mov", 2000, duration=10.0, frame_hashes=h),
    ]
    assert find_video_duplicates(recs, _s(video_match_format=True)) == []


def test_format_off_cross_container_match():
    """When video_match_format=False, .mp4 and .mov with same content group."""
    h = _matching_frames(seed=15)
    recs = [
        _video_rec("a.mp4", 1000, duration=10.0, frame_hashes=h),
        _video_rec("b.mov", 2000, duration=10.0, frame_hashes=h),
    ]
    groups = find_video_duplicates(recs, _s(video_match_format=False))
    assert len(groups) == 1


def test_no_double_grouping_with_pass_a():
    """Records caught by Pass A (exact size) must not also appear in Pass B."""
    h = _matching_frames(seed=17)
    recs = [
        _video_rec("a.mp4", 1024, duration=10.0, frame_hashes=h),
        _video_rec("b.mp4", 1024, duration=10.0, frame_hashes=h),
    ]
    s = Settings(video_match_format=True, video_match_size=True,
                 video_match_content=True, video_use_thumb=False)
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1


def test_three_videos_two_dups_one_unrelated():
    """Only the duplicate pair groups; the unrelated third video is not included."""
    dup = _matching_frames(seed=19)
    other = _different_frames(500)
    recs = [
        _video_rec("a.mp4", 1000, duration=10.0, frame_hashes=dup),
        _video_rec("b.mp4", 2000, duration=10.0, frame_hashes=dup),
        _video_rec("c.mp4", 3000, duration=10.0, frame_hashes=other),
    ]
    groups = find_video_duplicates(recs, _s())
    assert len(groups) == 1
    names = {m.path.name for m in groups[0].originals + groups[0].previews}
    assert "a.mp4" in names
    assert "b.mp4" in names
    assert "c.mp4" not in names


def test_oldest_mtime_is_original():
    """The record with the smallest mtime is the original in a content group."""
    h = _matching_frames(seed=21)
    recs = [
        _video_rec("newer.mp4", 1000, mtime=100.0, duration=10.0, frame_hashes=h),
        _video_rec("older.mp4", 2000, mtime=50.0,  duration=10.0, frame_hashes=h),
    ]
    groups = find_video_duplicates(recs, _s())
    assert len(groups) == 1
    assert groups[0].originals[0].path.name == "older.mp4"


def test_missing_content_fields_no_crash():
    """Records without _video_duration / _video_frame_hashes attrs must not crash."""
    recs = [
        ImageRecord(path=Path("a.mp4"), width=0, height=0, file_size=1000,
                    phash=_zero_hash(), dhash=_zero_hash(), mtime=0.0,
                    brightness=128.0, histogram=[], is_video=True),
        ImageRecord(path=Path("b.mp4"), width=0, height=0, file_size=2000,
                    phash=_zero_hash(), dhash=_zero_hash(), mtime=0.0,
                    brightness=128.0, histogram=[], is_video=True),
    ]
    s = Settings(video_match_format=False, video_match_size=False,
                 video_match_content=True, video_use_thumb=False)
    assert isinstance(find_video_duplicates(recs, s), list)


def test_very_short_video_empty_hashes_no_group():
    """Videos with duration < 0.5 s have empty frame_hashes; must not crash or group."""
    recs = [
        _video_rec("short1.mp4", 500, duration=0.3, frame_hashes=[]),
        _video_rec("short2.mp4", 600, duration=0.3, frame_hashes=[]),
    ]
    assert find_video_duplicates(recs, _s()) == []
