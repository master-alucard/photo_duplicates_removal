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


# ── 0-byte video guard ────────────────────────────────────────────────────────

def test_collect_videos_skips_zero_byte_files(tmp_path):
    """0-byte video files must be silently skipped, not included in results."""
    from config import Settings
    from scanner import collect_videos

    good = _make_fake_video(tmp_path, "good.mp4", size=100)
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    settings = Settings(include_videos=True, video_use_thumb=False, recursive=False)
    records = collect_videos(tmp_path, set(), settings)

    paths = {r.path for r in records}
    assert good in paths
    assert empty not in paths


def test_collect_videos_zero_byte_does_not_call_extractor(tmp_path):
    """_extract_video_thumb must never be called for a 0-byte file."""
    from config import Settings
    from scanner import collect_videos

    (tmp_path / "zero.mp4").write_bytes(b"")
    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)

    with patch("scanner._extract_video_thumb") as mock_ex:
        collect_videos(tmp_path, set(), settings)

    mock_ex.assert_not_called()


# ── short video / seek position ──────────────────────────────────────────────

def test_probe_video_duration_returns_none_when_ffprobe_missing():
    """_probe_video_duration must return None gracefully when ffprobe is absent."""
    from scanner import _probe_video_duration
    with patch("subprocess.run", side_effect=FileNotFoundError):
        dur = _probe_video_duration(Path("any.mp4"))
    assert dur is None


def test_probe_video_duration_returns_none_on_bad_output():
    """_probe_video_duration must return None when ffprobe output is not a float."""
    from scanner import _probe_video_duration
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = b"N/A\n"
    with patch("subprocess.run", return_value=mock_result):
        dur = _probe_video_duration(Path("any.mp4"))
    assert dur is None


def test_extract_video_thumb_uses_midpoint_for_short_video():
    """For a 0.5 s video, ffmpeg must be invoked with seek ~0.25 s."""
    from scanner import _extract_video_thumb
    import subprocess as _sp

    captured_args = []

    def _fake_run(args, **kwargs):
        captured_args.append(args)
        r = MagicMock()
        r.returncode = 1
        r.stdout = b""
        return r

    with patch("scanner._probe_video_duration", return_value=0.5):
        with patch("subprocess.run", side_effect=_fake_run):
            _extract_video_thumb(Path("short.mp4"))

    # Find the ffmpeg call (not ffprobe)
    ffmpeg_calls = [a for a in captured_args if a and "ffmpeg" in a[0]]
    assert ffmpeg_calls, "expected an ffmpeg subprocess call"
    seek_arg = ffmpeg_calls[0][ffmpeg_calls[0].index("-ss") + 1]
    seek_val = float(seek_arg)
    assert abs(seek_val - 0.25) < 0.01, f"expected seek ~0.25 s, got {seek_val}"


# ── graceful degradation (ffmpeg/OpenCV missing) ──────────────────────────────

def test_extract_video_thumb_returns_none_when_both_tools_missing():
    """If both ffmpeg and cv2 are unavailable, return None without crashing."""
    from scanner import _extract_video_thumb

    with patch("scanner._probe_video_duration", return_value=None):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with patch.dict("sys.modules", {"cv2": None}):
                result = _extract_video_thumb(Path("clip.mp4"))

    assert result is None


# ── ffmpeg timeout ─────────────────────────────────────────────────────────────

def test_extract_video_thumb_handles_timeout():
    """A subprocess.TimeoutExpired must not propagate — return None instead."""
    from scanner import _extract_video_thumb
    import subprocess as _sp

    with patch("scanner._probe_video_duration", return_value=None):
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="ffmpeg", timeout=10)):
            with patch.dict("sys.modules", {"cv2": None}):
                result = _extract_video_thumb(Path("hanging.mp4"))

    assert result is None


# ── corrupt video / failed_paths_out ─────────────────────────────────────────

def test_collect_videos_corrupt_file_logged_to_failed_paths_out(tmp_path):
    """A file that raises during stat must appear in failed_paths_out."""
    from config import Settings
    from scanner import collect_videos

    # Create a legitimate-looking .mp4 name but make stat() raise
    vid = tmp_path / "corrupt.mp4"
    vid.write_bytes(b"garbage" * 10)
    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)

    failed = []
    # Patch path.stat to raise for the specific file
    original_stat = Path.stat

    def _bad_stat(self, *args, **kwargs):
        if self.name == "corrupt.mp4":
            raise OSError("Simulated I/O error")
        return original_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", _bad_stat):
        with patch("scanner._extract_video_thumb", return_value=None):
            records = collect_videos(tmp_path, set(), settings, failed_paths_out=failed)

    assert any(p.name == "corrupt.mp4" for p in failed)
    # The corrupt file must not appear in results
    assert all(r.path.name != "corrupt.mp4" for r in records)


def test_collect_videos_corrupt_file_reported_via_progress_cb(tmp_path):
    """A failed file must trigger a progress_cb message containing 'Failed'."""
    from config import Settings
    from scanner import collect_videos

    vid = tmp_path / "bad.mp4"
    vid.write_bytes(b"x" * 50)
    settings = Settings(include_videos=True, video_use_thumb=True, recursive=False)
    messages = []

    original_stat = Path.stat

    def _bad_stat(self, *args, **kwargs):
        if self.name == "bad.mp4":
            raise OSError("disk error")
        return original_stat(self, *args, **kwargs)

    def _cb(msg, *args):
        messages.append(msg)

    with patch.object(Path, "stat", _bad_stat):
        collect_videos(tmp_path, set(), settings, progress_cb=_cb)

    assert any("Failed" in m for m in messages)


# ── progress reporting density ─────────────────────────────────────────────

def test_collect_videos_progress_every_file_for_small_collections(tmp_path):
    """With <= 20 videos, progress_cb must fire for every file."""
    from config import Settings
    from scanner import collect_videos

    n = 5
    for k in range(n):
        _make_fake_video(tmp_path, f"v{k}.mp4")

    settings = Settings(include_videos=True, video_use_thumb=False, recursive=False)
    calls = []

    def _cb(msg, current, total, stage):
        if "Indexing" in msg:
            calls.append(current)

    with patch("scanner._extract_video_thumb", return_value=None):
        collect_videos(tmp_path, set(), settings, progress_cb=_cb)

    # We expect one "Indexing" call per file
    assert len(calls) == n
