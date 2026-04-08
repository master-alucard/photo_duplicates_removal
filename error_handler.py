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
    Show an error dialog with a Copy to Clipboard button.

    In normal mode  → shows only user_msg (clear, non-technical).
    In developer mode → appends full detail / exception traceback.
    """
    msg = _build_msg(user_msg, detail, exc)
    _show_custom_error(parent, title, msg)


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


def _show_custom_error(
    parent: Optional[tk.Widget],
    title: str,
    msg: str,
) -> None:
    """Custom error dialog with OK and Copy to Clipboard buttons."""
    root = parent.winfo_toplevel() if parent else None

    win = tk.Toplevel(root)
    win.title(title)
    win.resizable(False, False)
    win.grab_set()

    # ── Icon + message ────────────────────────────────────────────────────
    body = tk.Frame(win, padx=20, pady=16)
    body.pack(fill=tk.BOTH, expand=True)

    icon_lbl = tk.Label(body, text="✕", font=("Segoe UI", 18, "bold"),
                        fg="#FFFFFF", bg="#C62828", width=2, height=1,
                        relief=tk.FLAT)
    icon_lbl.pack(side=tk.LEFT, anchor=tk.N, padx=(0, 14))

    msg_lbl = tk.Label(body, text=msg, justify=tk.LEFT,
                       wraplength=420, font=("Segoe UI", 9),
                       anchor=tk.W)
    msg_lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # ── Button bar ────────────────────────────────────────────────────────
    btn_bar = tk.Frame(win, pady=10, padx=20)
    btn_bar.pack(fill=tk.X)

    copied_var = tk.StringVar(value="📋  Copy to Clipboard")

    def _copy():
        try:
            win.clipboard_clear()
            win.clipboard_append(msg)
            win.update()
            copied_var.set("✓  Copied!")
            win.after(2000, lambda: copied_var.set("📋  Copy to Clipboard"))
        except Exception:
            pass

    copy_btn = tk.Button(btn_bar, textvariable=copied_var, command=_copy,
                         font=("Segoe UI", 9), relief=tk.FLAT, bd=0,
                         padx=10, pady=4, cursor="hand2")
    copy_btn.configure(bg="#E8EFF9", fg="#1565C0",
                       activebackground="#D0DCF0", activeforeground="#1565C0")
    copy_btn.pack(side=tk.LEFT)

    ok_btn = tk.Button(btn_bar, text="OK", command=win.destroy,
                       font=("Segoe UI", 9, "bold"), relief=tk.FLAT, bd=0,
                       padx=20, pady=4, cursor="hand2")
    ok_btn.configure(bg="#C62828", fg="#FFFFFF",
                     activebackground="#B71C1C", activeforeground="#FFFFFF")
    ok_btn.pack(side=tk.RIGHT)

    win.bind("<Return>", lambda _: win.destroy())
    win.bind("<Escape>", lambda _: win.destroy())

    # Centre over parent
    win.update_idletasks()
    if root:
        px = root.winfo_x() + root.winfo_width() // 2 - win.winfo_width() // 2
        py = root.winfo_y() + root.winfo_height() // 2 - win.winfo_height() // 2
        win.geometry(f"+{px}+{py}")

    win.wait_window()


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
