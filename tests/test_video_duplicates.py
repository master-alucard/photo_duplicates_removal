"""Tests for video duplicate detection (issue #300).

Covers the Compare Conditions feature: format match + size match.  Uses synthetic
``ImageRecord`` instances so no real video files / ffmpeg are needed.
"""
from __future__ import annotations

from pathlib import Path

import imagehash
import numpy as np

from config import Settings, DEFAULTS
from scanner import ImageRecord, find_video_duplicates


def _zero_hash() -> imagehash.ImageHash:
    return imagehash.ImageHash(np.zeros((8, 8), dtype=bool))


def _video_rec(path: str, size: int, mtime: float = 0.0) -> ImageRecord:
    return ImageRecord(
        path=Path(path),
        width=0,
        height=0,
        file_size=size,
        phash=_zero_hash(),
        dhash=_zero_hash(),
        mtime=mtime,
        brightness=128.0,
        histogram=[],
        is_video=True,
    )


def test_default_settings_enable_video_detection():
    """Issue #300: out of the box, videos must NOT be silently skipped."""
    s = Settings()
    assert s.include_videos is True
    assert s.video_match_format is True
    assert s.video_match_size is True


def test_same_size_same_format_grouped():
    """Two videos with same size + same extension are flagged as duplicates."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mp4", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1
    assert len(groups[0].originals) + len(groups[0].previews) == 2


def test_same_size_different_format_not_grouped_when_format_required():
    """When format-match is ON, .mp4 and .mov of identical bytes do NOT group."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mov", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    assert groups == []


def test_same_size_different_format_grouped_when_format_off():
    """When format-match is OFF, identical-byte videos group across containers."""
    s = Settings(video_match_format=False, video_match_size=True,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mov", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1


def test_same_format_different_size_not_grouped():
    """Same extension but different bytes never group when size-match is required."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mp4", 2048),
    ]
    groups = find_video_duplicates(recs, s)
    assert groups == []


def test_format_only_matches_grouping():
    """When only format-match is enabled, all same-extension videos group together."""
    s = Settings(video_match_format=True, video_match_size=False,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mp4", 9999),
        _video_rec("c.mov", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    # Two .mp4 records bucket together; the lone .mov drops.
    assert len(groups) == 1
    members = groups[0].originals + groups[0].previews
    assert {m.path.suffix.lower() for m in members} == {".mp4"}
    assert len(members) == 2


def test_neither_condition_returns_empty():
    """Belt-and-braces: with both flags OFF the function refuses to group."""
    s = Settings(video_match_format=False, video_match_size=False,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mp4", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    assert groups == []


def test_extension_match_is_case_insensitive():
    """`.MP4` and `.mp4` must be treated as the same format."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.MP4", 4096),
        _video_rec("b.mp4", 4096),
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1


# ── ambiguous flag for size-only matches ─────────────────────────────────────

def test_size_only_group_marked_ambiguous_when_no_thumbs():
    """Groups formed on size alone (no thumbnails) must be is_ambiguous=True."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=True)
    # Both have zero hash (extraction failed / not attempted)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mp4", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1
    assert groups[0].is_ambiguous is True


def test_size_only_group_marked_ambiguous_when_thumb_disabled():
    """With video_use_thumb=False, every group must be is_ambiguous=True."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=False)
    recs = [
        _video_rec("a.mp4", 1024),
        _video_rec("b.mp4", 1024),
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1
    assert groups[0].is_ambiguous is True


def _video_rec_with_hash(path: str, size: int, hash_val: imagehash.ImageHash,
                         mtime: float = 0.0) -> ImageRecord:
    return ImageRecord(
        path=Path(path),
        width=0, height=0,
        file_size=size,
        phash=hash_val,
        dhash=_zero_hash(),
        mtime=mtime,
        brightness=128.0,
        histogram=[],
        is_video=True,
    )


def _nonzero_hash_video() -> imagehash.ImageHash:
    arr = np.zeros((8, 8), dtype=bool)
    arr[0, 0] = True
    return imagehash.ImageHash(arr)


def test_visually_confirmed_group_not_ambiguous():
    """A group where both thumbnails are non-zero and similar must NOT be ambiguous."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=True)
    h = _nonzero_hash_video()
    recs = [
        _video_rec_with_hash("a.mp4", 1024, h),
        _video_rec_with_hash("b.mp4", 1024, h),
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1
    assert groups[0].is_ambiguous is False


def test_mixed_thumb_group_marked_ambiguous():
    """If one video in a bucket has a zero hash, the group must be ambiguous."""
    s = Settings(video_match_format=True, video_match_size=True,
                 video_use_thumb=True)
    h = _nonzero_hash_video()
    recs = [
        _video_rec_with_hash("a.mp4", 1024, h),            # good thumb
        _video_rec("b.mp4", 1024),                          # zero hash (failed)
    ]
    groups = find_video_duplicates(recs, s)
    assert len(groups) == 1
    assert groups[0].is_ambiguous is True
