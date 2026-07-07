"""
tests/test_no_console_window.py — Regression guard for the ffmpeg console-window bug.

The app ships as a windowed (console=False) PyInstaller build. Any ffmpeg /
ffprobe child spawned WITHOUT subprocess.CREATE_NO_WINDOW gets its own visible
console window on Windows — during a video scan that cascades hundreds of
black windows across the screen (v1.2.0 field bug).

Every subprocess.run call in scanner.py must therefore pass
creationflags=scanner._SUBPROC_NO_WINDOW.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import scanner


def setup_module(_module=None):
    # Resolve the ffmpeg exe once, outside the subprocess.run patches below:
    # imageio_ffmpeg.get_ffmpeg_exe() makes its own internal subprocess call
    # on first resolve, which is not a scanner.py call site. A setup hook
    # (not module-level code) keeps pytest collection side-effect free.
    scanner._ffmpeg_exe()


def _capture_run(captured):
    """Return a fake subprocess.run that records its kwargs."""
    def _fake_run(args, **kwargs):
        captured.append((args, kwargs))
        r = MagicMock()
        r.returncode = 1
        r.stdout = b""
        r.stderr = b""
        return r
    return _fake_run


def _assert_all_no_window(captured):
    assert captured, "expected at least one subprocess.run call"
    for args, kwargs in captured:
        assert kwargs.get("creationflags") == scanner._SUBPROC_NO_WINDOW, (
            f"subprocess.run({args[0]}...) missing creationflags="
            f"_SUBPROC_NO_WINDOW — would open a console window in the "
            f"frozen build"
        )


def test_subproc_no_window_constant_matches_win32_flag():
    if sys.platform == "win32":
        import subprocess
        assert scanner._SUBPROC_NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        assert scanner._SUBPROC_NO_WINDOW == 0


def test_probe_video_duration_uses_no_window_flag():
    captured = []
    with patch("subprocess.run", side_effect=_capture_run(captured)):
        scanner._probe_video_duration(Path("clip.mp4"))
    _assert_all_no_window(captured)


def test_probe_video_duration_ffmpeg_uses_no_window_flag():
    captured = []
    with patch("subprocess.run", side_effect=_capture_run(captured)):
        scanner._probe_video_duration_ffmpeg(Path("clip.mp4"))
    _assert_all_no_window(captured)


def test_extract_video_thumb_uses_no_window_flag():
    captured = []
    with patch("scanner._probe_video_duration", return_value=None):
        with patch("subprocess.run", side_effect=_capture_run(captured)):
            with patch.dict("sys.modules", {"cv2": None}):
                scanner._extract_video_thumb(Path("clip.mp4"))
    _assert_all_no_window(captured)


def test_extract_multi_frame_hashes_uses_no_window_flag():
    captured = []
    with patch("subprocess.run", side_effect=_capture_run(captured)):
        scanner._extract_video_multi_frame_hashes(Path("clip.mp4"), duration=60.0)
    # Covers both the single-pass select-filter call and the per-frame
    # fallback loop (returncode=1 forces the fallback path to run too).
    _assert_all_no_window(captured)
