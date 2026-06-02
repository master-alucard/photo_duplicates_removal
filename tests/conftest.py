"""
tests/conftest.py — pytest configuration for the Image Deduper test suite.

Silences all Tk windows during test runs so the user doesn't see
roots/Toplevels flashing on screen while pytest churns through tests, and
shares a single Tk root across every test module.

Why a single root?  ttkbootstrap caches widget images on the Tk
interpreter it first sees.  When a second test module creates its own
``tk.Tk()`` root, widgets built with the ttkbootstrap style look up image
IDs in the *new* interpreter's image pool — and find nothing, which
raises ``TclError: image "pyimageNN" doesn't exist`` mid-test.

We prevent that by:

1. Creating one master Tk root at session start (withdrawn).
2. Publishing it into every known test module's ``_ROOT`` global so each
   module's ``_get_root()`` helper returns the shared root.
3. Monkey-patching ``tk.Tk.__init__`` so any *other* Tk() call simply
   re-uses the master root's Tcl interpreter.
4. Monkey-patching ``Toplevel`` + ``deiconify`` + ``grab_set`` to stay
   invisible / silent throughout the suite.
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk

import pytest


_MASTER_ROOT: tk.Tk | None = None


def _publish_master_root(root: tk.Tk) -> None:
    """Inject the shared root into every test module's ``_ROOT`` global."""
    for modname in list(sys.modules):
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        if not (modname.endswith("test_app")
                or modname.endswith("test_report_viewer")
                or modname.endswith("test_library")
                or modname.endswith("test_rotation_detection")
                or modname.endswith("test_cache_merge")
                or modname.endswith("test_ui_animations")):
            continue
        if hasattr(mod, "_ROOT"):
            try:
                setattr(mod, "_ROOT", root)
            except Exception:
                pass


@pytest.fixture(scope="session", autouse=True)
def _silent_tk_windows():
    """Ensure no Tk window is ever visible while tests run."""
    global _MASTER_ROOT

    # ── 1. Build the master root up front so all widgets share it ─────────
    # Use Tcl to withdraw the window *before* Tk maps it to the screen,
    # then set alpha=0 + overrideredirect so it stays invisible even if
    # Windows sends WM_SHOWWINDOW during later event processing.
    _MASTER_ROOT = tk.Tk()
    try:
        # Withdraw via raw Tcl before any Python-level event processing fires.
        _MASTER_ROOT.tk.call("wm", "withdraw", ".")
        _MASTER_ROOT.tk.call("wm", "attributes", ".", "-alpha", "0.0")
        _MASTER_ROOT.tk.call("wm", "overrideredirect", ".", "1")
    except Exception:
        try:
            _MASTER_ROOT.withdraw()
        except Exception:
            pass

    # Publish into any already-imported test modules (pytest may have
    # collected them before this fixture ran).
    _publish_master_root(_MASTER_ROOT)

    # ── 2. tk.Tk() — redirect to the master root ──────────────────────────
    _orig_tk_init = tk.Tk.__init__

    def _redirect_tk_init(self, *args, **kwargs):
        # Don't build a new Tcl interpreter; make this instance look like
        # the master so ttkbootstrap's image cache stays valid.
        try:
            self.tk        = _MASTER_ROOT.tk
            self.master    = None
            self._tkloaded = getattr(_MASTER_ROOT, "_tkloaded", True)
            self.children  = {}
            self._w        = _MASTER_ROOT._w
            self._name     = _MASTER_ROOT._name
            self._tclCommands = None
        except Exception:
            # Fall back to a real Tk if the master somehow went away.
            _orig_tk_init(self, *args, **kwargs)
            try:
                self.withdraw()
            except Exception:
                pass

    tk.Tk.__init__ = _redirect_tk_init   # type: ignore[method-assign]

    # ── 3. Toplevel — auto-withdraw ───────────────────────────────────────
    _orig_top_init = tk.Toplevel.__init__

    def _silent_top_init(self, *args, **kwargs):
        _orig_top_init(self, *args, **kwargs)
        try:
            self.withdraw()
        except Exception:
            pass

    tk.Toplevel.__init__ = _silent_top_init   # type: ignore[method-assign]

    # ── 4. deiconify → no-op for Wm roots/toplevels ───────────────────────
    _orig_deiconify = tk.Wm.deiconify
    tk.Wm.deiconify = lambda self: None   # type: ignore[method-assign]

    # ── 4b. wm_state → block any transition to 'normal' ──────────────────
    # deiconify patches the Python method, but Tk can also change state via
    # wm_state('normal') directly.  Block that path too.
    _orig_wm_state = tk.Wm.wm_state

    def _locked_wm_state(self, newstate=None):
        if newstate is not None and newstate not in ("withdrawn", "iconic"):
            return None   # silently ignore attempts to show the window
        return _orig_wm_state(self, newstate)

    tk.Wm.wm_state = _locked_wm_state   # type: ignore[method-assign]

    # ── 5. grab_set → don't fail on unviewable windows ────────────────────
    _orig_grab_set = tk.Misc.grab_set

    def _silent_grab_set(self, *a, **kw):
        try:
            return _orig_grab_set(self, *a, **kw)
        except Exception:
            return None

    tk.Misc.grab_set = _silent_grab_set   # type: ignore[method-assign]

    # ── 6. update_idletasks → re-withdraw master root afterwards ─────────
    # On Windows, processing pending events can cause the OS to send
    # WM_SHOWWINDOW, making the withdrawn root briefly visible on screen.
    # Re-withdrawing after each flush keeps it hidden.
    _orig_update_idletasks = tk.Misc.update_idletasks

    def _safe_update_idletasks(self):
        result = _orig_update_idletasks(self)
        if _MASTER_ROOT is not None:
            try:
                if _MASTER_ROOT.wm_state() != "withdrawn":
                    _MASTER_ROOT.withdraw()
            except Exception:
                pass
        return result

    tk.Misc.update_idletasks = _safe_update_idletasks   # type: ignore[method-assign]

    yield _MASTER_ROOT

    # ── restore ───────────────────────────────────────────────────────────
    tk.Tk.__init__            = _orig_tk_init
    tk.Toplevel.__init__      = _orig_top_init
    tk.Wm.deiconify           = _orig_deiconify
    tk.Wm.wm_state            = _orig_wm_state
    tk.Misc.grab_set          = _orig_grab_set
    tk.Misc.update_idletasks  = _orig_update_idletasks


@pytest.fixture(autouse=True)
def _reset_master_root_between_tests():
    """Destroy leaked widgets and cancel pending Tk timers between tests.

    The viewer tests build ReportViewers with ``_make_viewer`` and never destroy
    them, so each leaks onto the shared session root (see ``_silent_tk_windows``)
    along with 50-250 packed cards and pending timers: ``after(300, ...)`` +
    ``after(1000, ...)`` safety-net re-layouts and an ``after_idle`` finalizer
    that itself calls ``update_idletasks()``.

    Two failure modes accumulate as the suite runs:

    1. **Widget pileup.**  Every later ``update_idletasks()`` reprocesses
       geometry for *all* accumulated widgets across *all* leaked viewers, so
       the cost grows quadratically and the suite slows to an apparent hang.
    2. **Timer cascade.**  A later ``update_idletasks()`` re-enters idle
       processing through ``_finalize_scroll`` and fans out across every leaked
       viewer's queued callbacks.

    Both vanish if each test starts from a clean root.  Destroying the widgets
    a test created and draining the ``after`` backlog after it finishes does
    that.

    Only widgets created *during* the test are destroyed: children present
    before the test runs are preserved, so class-scoped viewers built once in
    ``setUpClass`` (e.g. ``TestReportViewerActionBar.cls.viewer``) survive across
    that class's methods.  This runs in teardown — after the test's assertions —
    so nothing it clears can affect what the test observed.

    It also drains lingering worker threads.  Building a viewer with video
    records spawns real thumbnail-loader daemon threads that hold the decode
    semaphore (``_THUMB_SEMAPHORE``) while they shell out to ffmpeg.  Left
    running, they starve the next test's loader of a slot, which made the
    ``TestVideoThumbnailLoader`` cache tests flaky.  Joining them (briefly,
    bounded) hands every test a free semaphore.
    """
    if _MASTER_ROOT is None:
        yield
        return
    try:
        preexisting = {id(c) for c in _MASTER_ROOT.winfo_children()}
    except Exception:
        preexisting = set()

    yield

    # Drain lingering worker threads (thumbnail loaders et al.) so they release
    # the decode semaphore before the next test runs.  Bounded so a genuinely
    # stuck thread can never hang the suite.
    main = threading.main_thread()
    drain_deadline = time.monotonic() + 3.0
    for t in threading.enumerate():
        if t is main or not t.is_alive():
            continue
        remaining = drain_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            t.join(timeout=remaining)
        except Exception:
            pass

    # Cancel pending timers first so none fire against a half-destroyed tree.
    try:
        for aid in _MASTER_ROOT.tk.splitlist(_MASTER_ROOT.tk.call("after", "info")):
            try:
                _MASTER_ROOT.after_cancel(aid)
            except Exception:
                pass
    except Exception:
        pass
    # Destroy only the widgets this test added, leaving class-scoped widgets and
    # the master root (with its ttkbootstrap image cache) intact.
    try:
        for child in list(_MASTER_ROOT.winfo_children()):
            if id(child) not in preexisting:
                try:
                    child.destroy()
                except Exception:
                    pass
    except Exception:
        pass


def pytest_collection_modifyitems(config, items):
    """Publish the shared master root into every test module after
    collection — by this point every test module has been imported and
    its ``_ROOT`` module-global exists."""
    if _MASTER_ROOT is not None:
        _publish_master_root(_MASTER_ROOT)
