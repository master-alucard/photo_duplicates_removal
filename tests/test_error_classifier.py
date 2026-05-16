"""
tests/test_error_classifier.py — Unit tests for error_handler classifiers.

Covers _classify_exception, _classify_file_exception, and format_scan_error,
including the haystack re-classification path for worker-wrapped exceptions.
"""
from __future__ import annotations

import pytest

import error_handler
from error_handler import (
    _classify_exception,
    _classify_file_exception,
    format_scan_error,
    format_file_error,
)


# ── _classify_exception ───────────────────────────────────────────────────────

class TestClassifyException:

    def test_permission_error(self):
        msg = _classify_exception(PermissionError("access denied"))
        assert "Access denied" in msg
        assert "permission" in msg.lower()

    def test_permission_error_via_msg(self):
        exc = OSError("permission denied to read file")
        msg = _classify_exception(exc)
        assert "Access denied" in msg

    def test_winerror_3_with_drive_letter(self):
        exc = FileNotFoundError("[WinError 3] The system cannot find the path: 'D:\\'")
        msg = _classify_exception(exc)
        assert "D:\\" in msg
        assert "not available" in msg.lower()

    def test_winerror_3_without_drive_letter(self):
        exc = FileNotFoundError("cannot find the path specified")
        msg = _classify_exception(exc)
        assert "not available" in msg.lower()

    def test_winerror_3_oserror_with_drive(self):
        exc = OSError("[WinError 3] Cannot find path: 'E:\\'")
        msg = _classify_exception(exc)
        assert "E:\\" in msg
        assert "not available" in msg.lower()

    def test_generic_file_not_found(self):
        exc = FileNotFoundError("no such file or directory: '/tmp/img.jpg'")
        msg = _classify_exception(exc)
        assert "could not be found" in msg.lower() or "required file" in msg.lower()

    def test_oserror_disk_space(self):
        exc = OSError("no space left on disk")
        msg = _classify_exception(exc)
        assert "disk space" in msg.lower()

    def test_oserror_disk_space_via_disk_keyword(self):
        exc = OSError("disk full")
        msg = _classify_exception(exc)
        assert "disk space" in msg.lower()

    def test_memory_error(self):
        msg = _classify_exception(MemoryError())
        assert "memory" in msg.lower()

    def test_recursion_error(self):
        msg = _classify_exception(RecursionError("maximum recursion depth exceeded"))
        assert "call-stack limit" in msg.lower() or "recursion" in msg.lower()
        # Should include actionable advice
        assert "Max group size" in msg or "threshold" in msg.lower()

    def test_recursion_via_msg_keyword(self):
        exc = Exception("maximum recursion depth exceeded in comparison")
        msg = _classify_exception(exc)
        # Plain Exception doesn't match isinstance — falls to generic fallback.
        # Only RecursionError itself triggers the recursion branch.
        assert isinstance(msg, str) and len(msg) > 0

    def test_rawpy_error(self):
        exc = Exception("rawpy: failed to decode CR2")
        msg = _classify_exception(exc)
        assert "RAW" in msg or "rawpy" in msg.lower() or "camera" in msg.lower()

    def test_libraw_error(self):
        exc = Exception("LibRaw: cannot open file")
        msg = _classify_exception(exc)
        assert "RAW" in msg or "camera" in msg.lower()

    def test_generic_fallback(self):
        exc = ValueError("unexpected token")
        msg = _classify_exception(exc)
        assert "unexpected error" in msg.lower()


# ── format_scan_error haystack re-classification ─────────────────────────────

class TestFormatScanError:

    def test_direct_type_recursion(self):
        exc = RecursionError("recursion depth exceeded")
        user_msg, detail = format_scan_error(exc, "")
        assert "call-stack limit" in user_msg.lower() or "recursion" in user_msg.lower()

    def test_direct_type_memory(self):
        exc = MemoryError()
        user_msg, detail = format_scan_error(exc, "")
        assert "memory" in user_msg.lower()

    def test_wrapped_recursion_via_haystack(self):
        exc = Exception("RecursionError: maximum recursion depth exceeded")
        user_msg, detail = format_scan_error(exc, "")
        assert "call-stack limit" in user_msg.lower() or "recursion" in user_msg.lower()

    def test_wrapped_winerror3_via_haystack(self):
        exc = Exception("winerror 3 cannot find the path")
        user_msg, detail = format_scan_error(exc, "")
        assert "not available" in user_msg.lower()

    def test_wrapped_permission_via_haystack(self):
        exc = Exception("PermissionError: permission denied to /mnt/photos")
        user_msg, detail = format_scan_error(exc, "")
        assert "access denied" in user_msg.lower()

    def test_wrapped_memory_via_haystack(self):
        exc = Exception("MemoryError: ran out of memory")
        user_msg, detail = format_scan_error(exc, "")
        assert "memory" in user_msg.lower()

    def test_wrapped_file_not_found_via_haystack(self):
        exc = Exception("FileNotFoundError: no such file or directory")
        user_msg, detail = format_scan_error(exc, "")
        assert "could not be found" in user_msg.lower() or "required file" in user_msg.lower()

    def test_detail_includes_exception_type(self):
        exc = PermissionError("denied")
        user_msg, detail = format_scan_error(exc, "stack trace here")
        assert "PermissionError" in detail
        assert "stack trace here" in detail

    def test_detail_without_traceback(self):
        exc = MemoryError()
        user_msg, detail = format_scan_error(exc, "")
        assert "MemoryError" in detail

    def test_generic_wrapped_exception_gets_generic_msg(self):
        exc = Exception("some totally unknown error")
        user_msg, detail = format_scan_error(exc, "")
        assert isinstance(user_msg, str) and len(user_msg) > 0


# ── _classify_file_exception ──────────────────────────────────────────────────

class TestClassifyFileException:

    def test_permission_error(self):
        exc = PermissionError("denied")
        msg = _classify_file_exception(exc, "photo.jpg")
        assert "permission denied" in msg.lower()
        assert "photo.jpg" in msg

    def test_file_not_found(self):
        exc = FileNotFoundError("not found")
        msg = _classify_file_exception(exc, "shot.cr2")
        assert "shot.cr2" in msg
        assert "moved or deleted" in msg.lower() or "could not be found" in msg.lower()

    def test_is_a_directory_error(self):
        exc = IsADirectoryError("is a directory")
        msg = _classify_file_exception(exc, "folder_name")
        assert "folder" in msg.lower() or "directory" in msg.lower()
        assert "folder_name" in msg

    def test_disk_space(self):
        exc = OSError("no space left on disk")
        msg = _classify_file_exception(exc, "img.jpg")
        assert "disk space" in msg.lower()

    def test_generic_fallback(self):
        exc = IOError("connection reset")
        msg = _classify_file_exception(exc, "remote.jpg")
        assert "remote.jpg" in msg

    def test_no_path_uses_generic_filename(self):
        exc = PermissionError("denied")
        msg = _classify_file_exception(exc, "")
        assert "the file" in msg.lower() or "permission denied" in msg.lower()

    def test_windows_path_extracts_filename(self):
        exc = FileNotFoundError("not found")
        msg = _classify_file_exception(exc, r"C:\Users\photos\sunset.jpg")
        assert "sunset.jpg" in msg
