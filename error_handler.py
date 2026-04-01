"""
error_handler.py — Centralised error display for Image Deduper.

Usage:
    from error_handler import show_error, show_warning, show_info, set_settings

    # Call once at startup:
    set_settings(app.settings)

    # Then use anywhere:
    show_error(parent, "Scan failed",
               user_msg="The scan failed. Check folder permissions.",
               detail=f"{exception_message}\n\n{traceback_text}")
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Optional

# ── module-level settings reference ──────────────────────────────────────────
_settings = None


def set_settings(s) -> None:
    """Register the app Settings object so dev-mode preference is read."""
    global _settings
    _settings = s


def is_developer_mode() -> bool:
    """Return True when developer mode is enabled in current settings."""
    return bool(_settings and getattr(_settings, "developer_mode", False))


# ── public API ────────────────────────────────────────────────────────────────

def show_error(
    parent: Optional[tk.Widget],
    title: str,
    user_msg: str,
    detail: str = "",
    exc: Optional[BaseException] = None,
) -> None:
    """
    Show an error dialog.

    In normal mode  → shows only user_msg (clear, non-technical).
    In developer mode → appends full detail / exception traceback.
    """
    msg = _build_msg(user_msg, detail, exc)
    messagebox.showerror(title, msg, parent=parent)


def show_warning(
    parent: Optional[tk.Widget],
    title: str,
    user_msg: str,
    detail: str = "",
) -> None:
    """Show a warning dialog, optionally with developer detail."""
    msg = _build_msg(user_msg, detail)
    messagebox.showwarning(title, msg, parent=parent)


def show_info(
    parent: Optional[tk.Widget],
    title: str,
    user_msg: str,
    detail: str = "",
) -> None:
    """Show an informational dialog, optionally with developer detail."""
    msg = _build_msg(user_msg, detail)
    messagebox.showinfo(title, msg, parent=parent)


def format_scan_error(exc: BaseException, tb: str) -> tuple[str, str]:
    """
    Return (user_msg, detail) for a scan/processing exception.
    Analyses the exception type to give the most helpful user message.
    """
    user_msg = _classify_exception(exc)
    detail = f"{type(exc).__name__}: {exc}"
    if tb:
        detail += f"\n\n{tb}"
    return user_msg, detail


def format_file_error(exc: BaseException, path: str = "") -> tuple[str, str]:
    """Return (user_msg, detail) for a file I/O exception."""
    user_msg = _classify_file_exception(exc, path)
    detail = f"{type(exc).__name__}: {exc}"
    return user_msg, detail


# ── internal helpers ──────────────────────────────────────────────────────────

def _build_msg(user_msg: str, detail: str = "", exc: Optional[BaseException] = None) -> str:
    if not is_developer_mode():
        return user_msg
    parts = [user_msg]
    if detail:
        parts.append(f"\n── Developer detail ──────────────────\n{detail}")
    if exc is not None:
        import traceback as _tb
        tb_str = _tb.format_exc()
        if tb_str and tb_str.strip() != "NoneType: None":
            parts.append(f"\n── Traceback ─────────────────────────\n{tb_str}")
    return "\n".join(parts)


def _classify_exception(exc: BaseException) -> str:
    """Map a general exception to a plain-English user message."""
    name = type(exc).__name__
    msg  = str(exc).lower()

    if isinstance(exc, PermissionError) or "permission denied" in msg:
        return (
            "Access denied — the app couldn't read or write a file.\n"
            "Check that you have permission to access the source and output folders."
        )
    if isinstance(exc, FileNotFoundError) or "no such file" in msg:
        return (
            "A required file or folder could not be found.\n"
            "It may have been moved, renamed or deleted while the scan was running."
        )
    if isinstance(exc, OSError) and ("no space" in msg or "disk" in msg):
        return (
            "Not enough disk space to complete the operation.\n"
            "Free up some space on the output drive and try again."
        )
    if isinstance(exc, MemoryError):
        return (
            "The app ran out of memory.\n"
            "Try scanning a smaller folder or closing other applications."
        )
    if "rawpy" in msg or "libraw" in msg.lower():
        return (
            "Could not process a RAW image file.\n"
            "The file may be corrupt or use an unsupported camera format."
        )
    # Generic fallback
    return (
        "An unexpected error stopped the operation.\n"
        "Please check your folder permissions and available disk space,\n"
        "then try again. Enable Developer Mode in Settings for more details."
    )


def _classify_file_exception(exc: BaseException, path: str = "") -> str:
    """Map a file I/O exception to a plain-English user message."""
    msg = str(exc).lower()
    fname = path.split("\\")[-1].split("/")[-1] if path else "the file"

    if isinstance(exc, PermissionError) or "permission denied" in msg:
        return f"Could not access '{fname}' — permission denied."
    if isinstance(exc, FileNotFoundError) or "no such file" in msg:
        return (
            f"'{fname}' could not be found.\n"
            "It may have been moved or deleted."
        )
    if isinstance(exc, IsADirectoryError):
        return f"Expected a file but found a folder: '{fname}'."
    if isinstance(exc, OSError) and ("no space" in msg or "disk" in msg):
        return "Not enough disk space to move the file."
    return (
        f"Could not process '{fname}'.\n"
        "Check folder permissions and available disk space."
    )
