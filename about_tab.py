"""
about_tab.py — About page for Image Deduper.

Provides build_about_tab(parent, app) which fills a ttk.Frame with:
  - App identity (name, version, copyright, links)
  - Auto-update toggle + Check Now button
  - Privacy policy
  - Open-source library attributions
  - System information
"""
from __future__ import annotations

import platform
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import App   # type-check only; no runtime circular import

# ── App metadata ─────────────────────────────────────────────────────────────

APP_NAME      = "Image Deduper"
APP_VERSION   = "1.0.5"
APP_COPYRIGHT = "© 2026 Katador.net  ·  All rights reserved."
APP_EMAIL     = "office@katador.net"
GITHUB_URL    = "https://github.com/master-alucard/photo_duplicates_removal"
WEBSITE_URL   = "https://katador.net"
RELEASES_API  = "https://api.github.com/repos/master-alucard/photo_duplicates_removal/releases/latest"

# ── Colour palette (matches main.py / report_viewer.py) ─────────────────────

_BG          = "#F5F5F5"
_SURFACE     = "#FFFFFF"
_PRIMARY     = "#1565C0"
_PRIMARY_TINT= "#E3F2FD"
_SUCCESS     = "#2E7D32"
_SUCCESS_TINT= "#E8F5E9"
_ERROR       = "#C62828"
_ERROR_TINT  = "#FFEBEE"
_WARNING     = "#E65100"
_DIVIDER     = "#E0E0E0"
_TEXT1       = "#212121"
_TEXT2       = "#616161"
_TEXT3       = "#9E9E9E"

# ── Privacy policy text ───────────────────────────────────────────────────────

_PRIVACY_POLICY = """\
Image Deduper processes all images entirely on your device. No image \
data, file paths, metadata, or personal information is ever transmitted \
to any server or third party.

What this app accesses
  • Image files in the folders you select — read locally to compute \
perceptual hashes and find duplicates. Files are never copied, \
uploaded, or shared with any external service.
  • Settings and scan history are saved to your local AppData folder \
and are never transmitted.

Auto-update check
  If "Check for updates automatically" is enabled, the app sends a \
single HTTPS request to the GitHub public API (api.github.com) on \
startup. This request contains only your current app version number \
in the User-Agent header. No image data, file paths, or personal \
information are included. GitHub's own Privacy Policy applies to this \
request: https://docs.github.com/en/site-policy/privacy-policies
  You can disable this check at any time using the checkbox on this page.

Data collection
  None. There is no analytics, telemetry, crash reporting, or any \
form of user tracking in this application.

Third-party network contact
  The only external network request this app ever makes is the \
optional GitHub update check described above. All other operations \
are fully local and offline.

Data retention
  Settings and scan history are stored locally in your AppData folder \
and are deleted when you uninstall the application. No data is \
retained by Katador.net.

Contact
  Questions about privacy: privacy@katador.net

Last updated: April 2026
"""

# ── Open-source attributions ─────────────────────────────────────────────────

_LIBRARIES = [
    ("Pillow",      "≥ 10.0",  "HPND License",    "https://python-pillow.org"),
    ("imagehash",   "≥ 4.3.1", "MIT License",      "https://github.com/JohannesBuchner/imagehash"),
    ("NumPy",       "",        "BSD-3-Clause",      "https://numpy.org"),
    ("SciPy",       "",        "BSD-3-Clause",      "https://scipy.org"),
    ("PyWavelets",  "",        "MIT License",       "https://pywavelets.readthedocs.io"),
    ("piexif",      "≥ 1.1.3", "MIT License",       "https://piexif.readthedocs.io"),
    ("rawpy",       "≥ 0.18",  "MIT License (opt)", "https://letmaik.github.io/rawpy"),
    ("Python",      sys.version.split()[0], "PSF License", "https://python.org"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mat_btn(parent, text, command, bg, fg="#FFFFFF", font_size=9, **kw):
    def _darken(c):
        try:
            r, g, b = int(c[1:3],16), int(c[3:5],16), int(c[5:7],16)
            f = 0.82
            return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"
        except Exception:
            return c
    btn = tk.Button(parent, text=text, command=command,
                    bg=bg, fg=fg, activebackground=_darken(bg), activeforeground=fg,
                    relief=tk.FLAT, bd=0, padx=12, pady=5,
                    font=("Segoe UI", font_size, "bold"), cursor="hand2", **kw)
    btn.bind("<Enter>", lambda _: btn.configure(bg=_darken(bg)))
    btn.bind("<Leave>", lambda _: btn.configure(bg=bg))
    return btn


def _section(parent, title: str) -> tk.Frame:
    """Render a titled card section. Returns the inner content frame."""
    outer = tk.Frame(parent, bg=_BG)
    outer.pack(fill=tk.X, padx=20, pady=(0, 12))

    card = tk.Frame(outer, bg=_SURFACE,
                    highlightbackground=_DIVIDER, highlightthickness=1)
    card.pack(fill=tk.X)

    tk.Frame(card, width=4, bg=_PRIMARY).pack(side=tk.LEFT, fill=tk.Y)
    inner = tk.Frame(card, bg=_SURFACE, padx=16, pady=12)
    inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    tk.Label(inner, text=title, font=("Segoe UI", 10, "bold"),
             bg=_SURFACE, fg=_TEXT1).pack(anchor=tk.W, pady=(0, 8))
    tk.Frame(inner, height=1, bg=_DIVIDER).pack(fill=tk.X, pady=(0, 10))
    return inner


def _open_url(url: str) -> None:
    import webbrowser
    webbrowser.open(url)


def _link_btn(parent, text: str, url: str, bg=_SURFACE) -> tk.Label:
    """Clickable hyperlink label."""
    lbl = tk.Label(parent, text=text, font=("Segoe UI", 9, "underline"),
                   bg=bg, fg=_PRIMARY, cursor="hand2")
    lbl.bind("<Button-1>", lambda _: _open_url(url))
    return lbl


# ── GitHub update check ───────────────────────────────────────────────────────

def _check_github(timeout: int = 8) -> dict | None:
    """
    Poll GitHub Releases API for a newer version.
    Returns {'version': str, 'url': str, 'notes': str} or None.
    Runs on a background thread — never call from the main thread.
    """
    import json, urllib.request
    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        tag = data["tag_name"].lstrip("v")
        current = tuple(int(x) for x in APP_VERSION.split("."))
        latest  = tuple(int(x) for x in tag.split("."))
        if latest > current:
            asset = next(
                (a for a in data.get("assets", []) if a["name"].endswith(".exe")),
                None,
            )
            return {
                "version": tag,
                "url": asset["browser_download_url"] if asset else None,
                "notes": data.get("body", ""),
            }
        return {"version": tag, "url": None, "notes": ""}  # up to date
    except Exception:
        return None   # network unavailable or parse error


# ── Main builder ─────────────────────────────────────────────────────────────

def build_about_tab(frame: ttk.Frame, app: "App") -> None:
    """
    Populate *frame* (a ttk.Frame already added to the notebook) with
    the full About page content.
    Called once from App._build_about_tab().
    """

    # ── Scrollable canvas (matches report_viewer pattern) ─────────────────
    container = tk.Frame(frame, bg=_BG)
    container.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(container, bg=_BG, highlightthickness=0)
    vsb = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    inner = tk.Frame(canvas, bg=_BG)
    win_id = canvas.create_window((0, 0), window=inner, anchor=tk.NW)

    inner.bind("<Configure>",
               lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfig(win_id, width=e.width))
    canvas.bind("<Enter>",
                lambda _: canvas.bind_all("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta/120), "units")))
    canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

    # top padding
    tk.Frame(inner, bg=_BG, height=16).pack()

    # ── 1. Identity card ──────────────────────────────────────────────────
    id_outer = tk.Frame(inner, bg=_BG)
    id_outer.pack(fill=tk.X, padx=20, pady=(0, 12))

    id_card = tk.Frame(id_outer, bg=_PRIMARY,
                       highlightbackground=_PRIMARY, highlightthickness=1)
    id_card.pack(fill=tk.X)

    id_body = tk.Frame(id_card, bg=_PRIMARY, padx=24, pady=18)
    id_body.pack(fill=tk.X)

    tk.Label(id_body, text=APP_NAME,
             font=("Segoe UI", 20, "bold"), bg=_PRIMARY, fg="#FFFFFF",
             ).pack(anchor=tk.W)
    tk.Label(id_body, text=f"Version {APP_VERSION}",
             font=("Segoe UI", 10), bg=_PRIMARY, fg="#BBDEFB",
             ).pack(anchor=tk.W, pady=(2, 0))
    tk.Label(id_body, text=APP_COPYRIGHT,
             font=("Segoe UI", 9), bg=_PRIMARY, fg="#90CAF9",
             ).pack(anchor=tk.W, pady=(4, 12))

    _email_lbl = tk.Label(id_body, text=APP_EMAIL,
                          font=("Segoe UI", 9), bg=_PRIMARY, fg="#90CAF9",
                          cursor="hand2")
    _email_lbl.pack(anchor=tk.W, pady=(0, 10))
    _email_lbl.bind("<Button-1>", lambda _: _open_url(f"mailto:{APP_EMAIL}"))

    btn_row = tk.Frame(id_body, bg=_PRIMARY)
    btn_row.pack(anchor=tk.W)
    _mat_btn(btn_row, "🐙  GitHub",
             lambda: _open_url(GITHUB_URL),
             "#0D47A1", font_size=9).pack(side=tk.LEFT, padx=(0, 8))
    _mat_btn(btn_row, "🌐  katador.net",
             lambda: _open_url(WEBSITE_URL),
             "#0D47A1", font_size=9).pack(side=tk.LEFT, padx=(0, 8))
    _mat_btn(btn_row, "✉  Email",
             lambda: _open_url(f"mailto:{APP_EMAIL}"),
             "#0D47A1", font_size=9).pack(side=tk.LEFT)

    # ── 2. Updates card ───────────────────────────────────────────────────
    upd_inner = _section(inner, "Updates")

    auto_row = tk.Frame(upd_inner, bg=_SURFACE)
    auto_row.pack(fill=tk.X, pady=(0, 10))

    ttk.Checkbutton(
        auto_row,
        text="Check for updates automatically on startup",
        variable=app.auto_update_var,
        command=app._on_setting_change,
    ).pack(side=tk.LEFT)

    status_var = tk.StringVar(value="Not checked yet.")
    status_lbl = tk.Label(upd_inner, textvariable=status_var,
                          font=("Segoe UI", 9), bg=_SURFACE, fg=_TEXT2)
    status_lbl.pack(anchor=tk.W, pady=(0, 8))

    _download_btn: list[tk.Widget] = []   # single-element list so inner funcs can rebind it

    def _do_check() -> None:
        status_var.set("Checking…")
        check_btn.configure(state=tk.DISABLED)

        def _run():
            result = _check_github()
            def _update():
                # Remove any previous download button before potentially creating a new one
                if _download_btn:
                    try:
                        _download_btn[0].destroy()
                    except Exception:
                        pass
                    _download_btn.clear()

                check_btn.configure(state=tk.NORMAL)
                if result is None:
                    status_var.set("Could not reach GitHub — check your internet connection.")
                    status_lbl.configure(fg=_WARNING)
                elif result["url"] is None and result["version"]:
                    status_var.set(
                        f"✓  You have the latest version ({APP_VERSION}).")
                    status_lbl.configure(fg=_SUCCESS)
                elif result.get("url"):
                    status_var.set(
                        f"⬆  Version {result['version']} is available!")
                    status_lbl.configure(fg=_PRIMARY)
                    btn = _mat_btn(upd_inner, f"⬇  Download {result['version']}",
                                   lambda: _open_url(result["url"]),
                                   _PRIMARY, font_size=9)
                    btn.pack(anchor=tk.W, pady=(4, 0))
                    _download_btn.append(btn)
                else:
                    status_var.set("No update found.")
                    status_lbl.configure(fg=_TEXT2)
            inner.after(0, _update)

        threading.Thread(target=_run, daemon=True).start()

    check_btn = _mat_btn(upd_inner, "Check Now", _do_check, _PRIMARY, font_size=9)
    check_btn.pack(anchor=tk.W)

    # Run auto-check in background if enabled
    if app.auto_update_var.get():
        threading.Thread(target=lambda: (
            __import__("time").sleep(2),   # small delay so UI is fully loaded first
            inner.after(0, _do_check),
        ), daemon=True).start()

    # ── 3. Privacy Policy card ────────────────────────────────────────────
    priv_inner = _section(inner, "Privacy Policy")

    priv_text = tk.Text(
        priv_inner, wrap=tk.WORD, relief=tk.FLAT, padx=4, pady=4,
        bg="#FAFAFA", fg=_TEXT2, font=("Segoe UI", 9),
        height=14, state=tk.DISABLED,
    )
    priv_sb = ttk.Scrollbar(priv_inner, orient=tk.VERTICAL,
                             command=priv_text.yview)
    priv_text.configure(yscrollcommand=priv_sb.set)
    priv_sb.pack(side=tk.RIGHT, fill=tk.Y)
    priv_text.pack(fill=tk.X)

    priv_text.configure(state=tk.NORMAL)
    priv_text.insert("1.0", _PRIVACY_POLICY)
    priv_text.configure(state=tk.DISABLED)

    # ── 4. Open-source libraries card ─────────────────────────────────────
    lib_inner = _section(inner, "Open-Source Libraries")

    for name, ver, lic, url in _LIBRARIES:
        row = tk.Frame(lib_inner, bg=_SURFACE)
        row.pack(fill=tk.X, pady=3)

        name_lbl = tk.Label(row, text=name + (" " + ver if ver else ""),
                            font=("Segoe UI", 9, "bold"),
                            bg=_SURFACE, fg=_TEXT1, width=22, anchor=tk.W)
        name_lbl.pack(side=tk.LEFT)

        tk.Label(row, text=lic, font=("Segoe UI", 9),
                 bg=_SURFACE, fg=_TEXT3, width=22, anchor=tk.W).pack(side=tk.LEFT)

        _link_btn(row, url, url, bg=_SURFACE).pack(side=tk.LEFT)

    # ── 5. System information card ────────────────────────────────────────
    sys_inner = _section(inner, "System Information")

    def _sys_row(label: str, value: str) -> None:
        row = tk.Frame(sys_inner, bg=_SURFACE)
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=label, font=("Segoe UI", 9),
                 bg=_SURFACE, fg=_TEXT2, width=20, anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(row, text=value, font=("Segui UI", 9, "bold"),
                 bg=_SURFACE, fg=_TEXT1).pack(side=tk.LEFT)

    _sys_row("App version",  APP_VERSION)
    _sys_row("Python",       sys.version.split()[0])
    _sys_row("Platform",     platform.system() + " " + platform.release())
    _sys_row("Architecture", platform.machine())
    _sys_row("Frozen (EXE)", "Yes" if getattr(sys, "frozen", False) else "No (source)")

    # bottom padding
    tk.Frame(inner, bg=_BG, height=20).pack()
