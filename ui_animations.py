"""
ui_animations.py -- Reusable Tkinter animation helpers for Image Deduper.

All animations use root.after() callbacks on the main thread -- no threads,
no external libraries. Each helper is a self-contained class that can be
started, stopped, and reset independently.

Windows 10 graceful-degradation: wm_attributes -alpha is not used;
canvas-based colour animations work on all Windows 10 configurations.
"""
from __future__ import annotations

import math
import tkinter as tk


# -- Colour mixing helpers ----------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_color(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return _rgb_to_hex(r, g, b)


def _mix_alpha(color: str, bg: str, alpha: float) -> str:
    return _lerp_color(bg, color, alpha)


# -- PulsingDot --------------------------------------------------------------------------

class PulsingDot:
    PERIOD_MS = 1200
    TICK_MS   = 40
    DOT_SIZE  = 8

    def __init__(self, parent, fg_color="#2E7D32", bg_color="#FFFFFF", min_alpha=0.35):
        self._fg      = fg_color
        self._bg      = bg_color
        self._min_a   = min_alpha
        self._running = False
        self._after_id = None
        self._phase   = 0.0
        self._step    = self.TICK_MS / self.PERIOD_MS

        pad  = 2
        size = self.DOT_SIZE + pad * 2
        self.canvas = tk.Canvas(parent, width=size, height=size,
                                bg=bg_color, highlightthickness=0, bd=0)
        self._oval = self.canvas.create_oval(
            pad, pad, pad + self.DOT_SIZE, pad + self.DOT_SIZE,
            fill=fg_color, outline="")

    def start(self):
        if self._running:
            return
        self._running = True
        self._phase = 0.0
        self._tick()

    def stop(self):
        self._running = False
        if self._after_id is not None:
            try:
                self.canvas.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.canvas.itemconfig(self._oval, fill=self._fg)
        except Exception:
            pass

    def hide(self):
        self.stop()
        try:
            self.canvas.pack_forget()
        except Exception:
            pass

    def show(self, side=tk.LEFT, padx=(0, 4)):
        try:
            self.canvas.pack(side=side, padx=padx)
        except Exception:
            pass
        self.start()

    def update_colors(self, fg_color, bg_color):
        self._fg = fg_color
        self._bg = bg_color
        try:
            self.canvas.configure(bg=bg_color)
            self.canvas.itemconfig(self._oval, fill=fg_color)
        except Exception:
            pass

    def _tick(self):
        if not self._running:
            return
        raw_alpha = 0.5 + 0.5 * math.sin(self._phase * 2 * math.pi)
        alpha = self._min_a + (1.0 - self._min_a) * raw_alpha
        color = _mix_alpha(self._fg, self._bg, alpha)
        try:
            self.canvas.itemconfig(self._oval, fill=color)
        except Exception:
            pass
        self._phase = (self._phase + self._step) % 1.0
        try:
            self._after_id = self.canvas.after(self.TICK_MS, self._tick)
        except Exception:
            self._after_id = None


# -- PhaseFlash --------------------------------------------------------------------------

class PhaseFlash:
    STEPS   = 6
    TICK_MS = 50

    def __init__(self, label, accent_color, normal_color):
        self._label    = label
        self._accent   = accent_color
        self._normal   = normal_color
        self._step     = 0
        self._after_id = None

    def trigger(self):
        if self._after_id is not None:
            try:
                self._label.after_cancel(self._after_id)
            except Exception:
                pass
        self._step = 0
        self._tick()

    def update_colors(self, accent_color, normal_color):
        self._accent = accent_color
        self._normal = normal_color

    def _tick(self):
        if self._step >= self.STEPS:
            try:
                self._label.configure(foreground=self._normal)
            except Exception:
                pass
            self._after_id = None
            return
        half = self.STEPS / 2
        t = self._step / half if self._step < half else 2.0 - self._step / half
        t = max(0.0, min(1.0, t))
        color = _lerp_color(self._normal, self._accent, t)
        try:
            self._label.configure(foreground=color)
        except Exception:
            pass
        self._step += 1
        try:
            self._after_id = self._label.after(self.TICK_MS, self._tick)
        except Exception:
            self._after_id = None


# -- bind_press_feedback -----------------------------------------------------------------

def bind_press_feedback(btn, darken_factor=0.82):
    def _hex_darken(color, f):
        try:
            r, g, b = _hex_to_rgb(color)
            return _rgb_to_hex(int(r * f), int(g * f), int(b * f))
        except Exception:
            return color

    def _on_press(_event):
        if str(btn["state"]) == "disabled":
            return
        orig = getattr(btn, "_mat_bg", None)
        if orig is None:
            return
        pressed = _hex_darken(orig, darken_factor)
        try:
            btn.configure(bg=pressed)
        except Exception:
            return
        btn.after(100, lambda: _restore(orig))

    def _restore(orig):
        try:
            if str(btn["state"]) != "disabled":
                btn.configure(bg=orig)
        except Exception:
            pass

    btn.bind("<ButtonPress-1>", _on_press)


# -- TabIndicator -----------------------------------------------------------------------

class TabIndicator:
    """
    An animated sliding underline indicator for a ttk.Notebook.

    Places a thin coloured bar (height=3px) at the bottom of the notebook's
    tab strip using place() and animates it sliding to the newly selected tab
    when <<NotebookTabChanged>> fires.

    Usage:
        indicator = TabIndicator(notebook, accent_color="#1565C0")
        indicator.bind()   # attaches the event listener and draws initial state
    """

    TICK_MS      = 12    # ~83 fps -- fast enough to look smooth
    DURATION_MS  = 180   # total slide duration
    BAR_HEIGHT   = 3     # pixels

    def __init__(self, notebook, accent_color: str = "#1565C0") -> None:
        self._nb       = notebook
        self._accent   = accent_color
        self._after_id = None

        # The indicator bar -- created on first bind so the notebook exists
        self._bar = tk.Frame(notebook, bg=accent_color, height=self.BAR_HEIGHT,
                             bd=0, highlightthickness=0)

        self._current_x  = 0
        self._current_w  = 0
        self._target_x   = 0
        self._target_w   = 0
        self._steps      = max(1, self.DURATION_MS // self.TICK_MS)
        self._step       = 0

    def bind(self) -> None:
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # Draw at startup after geometry is available
        self._nb.after(100, self._jump_to_current)

    def update_color(self, accent_color: str) -> None:
        self._accent = accent_color
        try:
            self._bar.configure(bg=accent_color)
        except Exception:
            pass

    def _on_tab_changed(self, _event=None) -> None:
        target = self._get_tab_rect()
        if target is None:
            return
        tx, ty, tw, th = target
        # Bar sits at the bottom of the tab header area
        bar_y = ty + th - self.BAR_HEIGHT
        self._target_x = tx
        self._target_w = tw
        # Start animation from current position
        if self._after_id is None:
            self._step = 0
            self._tick(bar_y)

    def _jump_to_current(self) -> None:
        """Instantly position the bar on the current tab (no animation)."""
        target = self._get_tab_rect()
        if target is None:
            try:
                self._nb.after(200, self._jump_to_current)
            except Exception:
                pass
            return
        tx, ty, tw, th = target
        bar_y = ty + th - self.BAR_HEIGHT
        self._current_x = tx
        self._current_w = tw
        self._target_x  = tx
        self._target_w  = tw
        try:
            self._bar.place(x=tx, y=bar_y, width=tw, height=self.BAR_HEIGHT)
            self._bar.lift()
        except Exception:
            pass

    def _get_tab_rect(self):
        """Return (x, y, width, height) of the currently selected tab, or None."""
        try:
            idx = self._nb.index(self._nb.select())
            bbox = self._nb.bbox(idx)
            if bbox and len(bbox) == 4:
                return bbox
        except Exception:
            pass
        return None

    def _tick(self, bar_y: int) -> None:
        self._step += 1
        t = self._step / self._steps
        # ease-out
        t_e = 1.0 - (1.0 - t) ** 2
        self._current_x = int(self._current_x + (self._target_x - self._current_x) * t_e)
        self._current_w = int(self._current_w + (self._target_w - self._current_w) * t_e)
        try:
            self._bar.place(x=self._current_x, y=bar_y,
                            width=self._current_w, height=self.BAR_HEIGHT)
            self._bar.lift()
        except Exception:
            pass

        if self._step >= self._steps:
            self._after_id = None
            # Snap to exact target
            try:
                self._bar.place(x=self._target_x, y=bar_y,
                                width=self._target_w, height=self.BAR_HEIGHT)
            except Exception:
                pass
            return

        try:
            self._after_id = self._nb.after(self.TICK_MS, lambda: self._tick(bar_y))
        except Exception:
            self._after_id = None


# -- SlideDownReveal -------------------------------------------------------------------

class SlideDownReveal:
    """
    Animate a tk.Frame sliding down into view from zero height to full height
    over *duration_ms* milliseconds.  Uses place() for the measurement pass and
    pack() for the final state, so it works inside normal pack-managed layouts.

    Because Tk widgets report winfo_reqheight() only after they have been placed
    once, this class uses a two-phase approach:
      1. Briefly place the frame with relwidth=1 but height=1 to trigger geometry.
      2. Animate height from 1 to the measured height.
      3. Restore pack() layout at full height.

    Usage:
        reveal = SlideDownReveal(card_frame, duration_ms=350)
        reveal.play()   # call after the frame has been packed (content already built)
    """

    TICK_MS = 16   # ~60 fps

    def __init__(self, frame: tk.Frame, duration_ms: int = 350) -> None:
        self._frame       = frame
        self._duration_ms = duration_ms
        self._after_id    = None
        self._steps       = max(1, duration_ms // self.TICK_MS)
        self._current     = 0
        self._target_h    = 0

    def play(self) -> None:
        """Start the reveal animation.  The frame must already be packed."""
        if self._after_id is not None:
            try:
                self._frame.after_cancel(self._after_id)
            except Exception:
                pass
        # Force geometry calculation
        try:
            self._frame.update_idletasks()
            self._target_h = self._frame.winfo_reqheight()
        except Exception:
            return  # no geometry yet; skip animation gracefully
        if self._target_h <= 1:
            return  # nothing to animate

        self._current = 0
        # Start from zero height using place() overlay; pack stays in place
        # for layout but we temporarily suppress visibility by setting height
        # via the internal geometry (not easily done with pack alone).
        # Instead: we use an overlay frame that collapses from full to zero,
        # giving the appearance of the card sliding into view.
        # We create a fresh overlay each play() call to avoid stale refs.
        try:
            self._overlay = tk.Frame(
                self._frame.master,
                bg=self._frame.master.cget("bg"),
                height=self._target_h,
            )
            self._overlay.place_configure(
                in_=self._frame, relx=0, y=0, relwidth=1, height=self._target_h
            )
            self._overlay.lift()
        except Exception:
            return

        self._tick()

    def _tick(self) -> None:
        self._current += 1
        t = self._current / self._steps
        # ease-out: t' = 1 - (1-t)^2
        t_eased = 1.0 - (1.0 - t) ** 2
        # Overlay shrinks from full height to 0 as t_eased goes 0 -> 1
        remaining_h = int(self._target_h * (1.0 - t_eased))
        try:
            self._overlay.place_configure(height=remaining_h)
        except Exception:
            pass

        if self._current >= self._steps:
            try:
                self._overlay.place_forget()
                self._overlay.destroy()
            except Exception:
                pass
            self._after_id = None
            return

        try:
            self._after_id = self._frame.after(self.TICK_MS, self._tick)
        except Exception:
            self._after_id = None
