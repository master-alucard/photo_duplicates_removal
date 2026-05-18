"""
tests/test_video_cache.py — Tests for video pHash caching in library.py.

Verifies that collect_videos() reuses cached pHashes for unchanged files
and re-extracts when a file's mtime or size changes.
"""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import imagehash
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from library import Library, VideoRecord, _VIDEO_CACHE_VERSION


# ── helpers ───────────────────────────────────────────────────────────────────

def _zero_hash() -> imagehash.ImageHash:
    return imagehash.ImageHash(np.zeros((8, 8), dtype=bool))


def _nonzero_hash() -> imagehash.ImageHash:
    arr = np.zeros((8, 8), dtype=bool)
    arr[0, 0] = True
    return imagehash.ImageHash(arr)


# ── VideoRecord unit tests ────────────────────────────────────────────────────

def test_video_record_from_dict_roundtrip():
    vr = VideoRecord(path="/a/b.mp4", mtime=1234.5, size=999, phash="aabbccdd")
    d = asdict(vr)
    restored = VideoRecord.from_dict(d)
    assert restored.path == vr.path
    assert restored.mtime == vr.mtime
    assert restored.size == vr.size
    assert restored.phash == vr.phash


def test_video_record_is_stale_size_change(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 100)
    st = f.stat()
    vr = VideoRecord(path=str(f), mtime=st.st_mtime, size=999, phash="aa")
    # size mismatch → stale
    assert vr.is_stale(f) is True


def test_video_record_is_stale_unchanged(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 100)
    st = f.stat()
    vr = VideoRecord(path=str(f), mtime=st.st_mtime, size=st.st_size, phash="aa")
    assert vr.is_stale(f) is False


def test_video_record_is_stale_missing_file(tmp_path):
    vr = VideoRecord(path=str(tmp_path / "gone.mp4"), mtime=1.0, size=1, phash="aa")
    assert vr.is_stale(tmp_path / "gone.mp4") is True


# ── Library video cache persistence ──────────────────────────────────────────

def test_library_save_and_load_video_cache(tmp_path):
    lib = Library(tmp_path / "lib")
    folder = str(tmp_path / "videos")

    cache = {
        "/a/b.mp4": VideoRecord("/a/b.mp4", 100.0, 1024, "deadbeef"),
        "/a/c.mp4": VideoRecord("/a/c.mp4", 200.0, 2048, ""),
    }
    lib.save_video_cache(folder, cache)
    loaded = lib.load_video_cache(folder)

    assert set(loaded.keys()) == set(cache.keys())
    assert loaded["/a/b.mp4"].phash == "deadbeef"
    assert loaded["/a/c.mp4"].phash == ""


def test_library_load_video_cache_missing_returns_empty(tmp_path):
    lib = Library(tmp_path / "lib")
    result = lib.load_video_cache(str(tmp_path / "nonexistent"))
    assert result == {}


def test_library_video_cache_corrupt_json_returns_empty(tmp_path):
    lib = Library(tmp_path / "lib")
    folder_str = str(tmp_path / "videos")
    # Save something then corrupt it
    lib.save_video_cache(folder_str, {})
    cache_file = lib._video_cache_path(lib._norm(folder_str))
    cache_file.write_text("NOT JSON", encoding="utf-8")
    result = lib.load_video_cache(folder_str)
    assert result == {}


# ── collect_videos cache integration ─────────────────────────────────────────

def _make_fake_video(tmp_path: Path, name: str, size: int = 100) -> Path:
    """Create a dummy file (not a real video) to stand in for path.stat()."""
    p = tmp_path / name
    p.write_bytes(b"0" * size)
    return p


def test_collect_videos_uses_cache_hit(tmp_path):
    """Cache hit: _extract_video_thumb must NOT be called for an unchanged file."""
    from config import Settings
    from scanner import collect_videos

    vid = _make_fake_video(tmp_path, "test.mp4")
    st = vid.stat()
    stored_hash = str(_nonzero_hash())

    lib = Library(tmp_path / "lib")
    lib.save_video_cache(
        str(tmp_path),
        {str(vid): VideoRecord(str(vid), st.st_mtime, st.st_size, stored_hash)},
    )

    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)
    with patch("scanner._extract_video_thumb") as mock_extract:
        records = collect_videos(tmp_path, set(), settings, library=lib)

    mock_extract.assert_not_called()
    assert len(records) == 1
    # pHash must match the stored hash, not a zero hash
    assert str(records[0].phash) == stored_hash


def test_collect_videos_cache_miss_calls_extraction(tmp_path):
    """Cache miss (stale mtime): _extract_video_thumb must be called."""
    from config import Settings
    from scanner import collect_videos

    vid = _make_fake_video(tmp_path, "clip.mp4")
    st = vid.stat()

    lib = Library(tmp_path / "lib")
    # Store with wrong mtime to force a miss
    lib.save_video_cache(
        str(tmp_path),
        {str(vid): VideoRecord(str(vid), st.st_mtime - 999, st.st_size, "aabbccdd")},
    )

    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)
    with patch("scanner._extract_video_thumb", return_value=None) as mock_extract:
        records = collect_videos(tmp_path, set(), settings, library=lib)

    mock_extract.assert_called_once_with(vid)
    assert len(records) == 1


def test_collect_videos_no_library_still_works(tmp_path):
    """Without a library argument the function works as before (no crash)."""
    from config import Settings
    from scanner import collect_videos

    vid = _make_fake_video(tmp_path, "movie.mp4")
    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)

    with patch("scanner._extract_video_thumb", return_value=None):
        records = collect_videos(tmp_path, set(), settings)  # no library arg

    assert len(records) == 1


def test_collect_videos_cache_persisted_after_scan(tmp_path):
    """After a scan with a library, the video cache file must exist on disk."""
    from config import Settings
    from scanner import collect_videos

    _make_fake_video(tmp_path, "a.mp4")
    lib = Library(tmp_path / "lib")
    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)

    with patch("scanner._extract_video_thumb", return_value=None):
        collect_videos(tmp_path, set(), settings, library=lib)

    # Load back and verify entry exists
    loaded = lib.load_video_cache(str(tmp_path))
    assert len(loaded) == 1
