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
