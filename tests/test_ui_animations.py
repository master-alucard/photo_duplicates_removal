"""
tests/test_ui_animations.py -- Tests for ui_animations module.

Tests run headless: a Tk root is created withdrawn so no window appears.
All animation callbacks are tested for correct state transitions without
actually waiting for after() timers (the tests call internal _tick methods
directly or verify state after start/stop).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tkinter as tk

_ROOT = None

def _get_root():
    global _ROOT
    if _ROOT is None or not _ROOT.winfo_exists():
        _ROOT = tk.Tk()
        _ROOT.withdraw()
    return _ROOT


class TestColourHelpers(unittest.TestCase):

    def test_hex_to_rgb_black(self):
        from ui_animations import _hex_to_rgb
        self.assertEqual(_hex_to_rgb("#000000"), (0, 0, 0))

    def test_hex_to_rgb_white(self):
        from ui_animations import _hex_to_rgb
        self.assertEqual(_hex_to_rgb("#ffffff"), (255, 255, 255))

    def test_hex_to_rgb_without_hash(self):
        from ui_animations import _hex_to_rgb
        self.assertEqual(_hex_to_rgb("ff0000"), (255, 0, 0))

    def test_rgb_to_hex_round_trip(self):
        from ui_animations import _hex_to_rgb, _rgb_to_hex
        original = "#1a2b3c"
        r, g, b = _hex_to_rgb(original)
        self.assertEqual(_rgb_to_hex(r, g, b), original)

    def test_lerp_color_at_zero(self):
        from ui_animations import _lerp_color
        self.assertEqual(_lerp_color("#ff0000", "#0000ff", 0.0), "#ff0000")

    def test_lerp_color_at_one(self):
        from ui_animations import _lerp_color
        self.assertEqual(_lerp_color("#ff0000", "#0000ff", 1.0), "#0000ff")

    def test_lerp_color_midpoint(self):
        from ui_animations import _lerp_color
        mid = _lerp_color("#000000", "#ffffff", 0.5)
        # midpoint should give ~127 each channel
        self.assertTrue(mid.startswith("#"))
        r = int(mid[1:3], 16)
        self.assertAlmostEqual(r, 127, delta=2)

    def test_mix_alpha_at_zero(self):
        from ui_animations import _mix_alpha
        # alpha=0 should give the bg colour
        result = _mix_alpha("#ff0000", "#ffffff", 0.0)
        self.assertEqual(result, "#ffffff")

    def test_mix_alpha_at_one(self):
        from ui_animations import _mix_alpha
        result = _mix_alpha("#ff0000", "#ffffff", 1.0)
        self.assertEqual(result, "#ff0000")


class TestPulsingDot(unittest.TestCase):

    def setUp(self):
        self.root = _get_root()

    def test_starts_not_running(self):
        from ui_animations import PulsingDot
        dot = PulsingDot(self.root)
        self.assertFalse(dot._running)
        self.assertIsNone(dot._after_id)

    def test_start_sets_running(self):
        from ui_animations import PulsingDot
        dot = PulsingDot(self.root)
        dot.start()
        self.assertTrue(dot._running)

    def test_stop_clears_running(self):
        from ui_animations import PulsingDot
        dot = PulsingDot(self.root)
        dot.start()
        dot.stop()
        self.assertFalse(dot._running)
        self.assertIsNone(dot._after_id)

    def test_double_start_idempotent(self):
        """start() when already running must not reset phase or restart tick."""
        from ui_animations import PulsingDot
        dot = PulsingDot(self.root)
        # Simulate "already running" without actually scheduling after() timers.
        dot._running = True
        dot._phase   = 0.75
        # Call start() — should return early because _running is True
        # and should NOT touch _phase (which would be reset to 0.0).
        dot.start()
        self.assertTrue(dot._running, "dot should still be running")
        self.assertAlmostEqual(dot._phase, 0.75, places=5,
                               msg="start() on running dot must not reset phase")

    def test_update_colors(self):
        from ui_animations import PulsingDot
        dot = PulsingDot(self.root, fg_color="#ff0000", bg_color="#000000")
        dot.update_colors("#00ff00", "#ffffff")
        self.assertEqual(dot._fg, "#00ff00")
        self.assertEqual(dot._bg, "#ffffff")

    def test_phase_wraps(self):
        from ui_animations import PulsingDot
        dot = PulsingDot(self.root)
        dot._phase = 0.99
        dot._running = True
        dot._tick()
        # After one tick with step ~0.033, phase should wrap and stay < 1
        self.assertGreaterEqual(dot._phase, 0.0)
        self.assertLess(dot._phase, 1.0)


class TestPhaseFlash(unittest.TestCase):

    def setUp(self):
        self.root = _get_root()

    def test_initial_state(self):
        from ui_animations import PhaseFlash
        lbl = tk.Label(self.root, text="test")
        flash = PhaseFlash(lbl, "#1565C0", "#000000")
        self.assertEqual(flash._step, 0)
        self.assertIsNone(flash._after_id)

    def test_trigger_advances_step(self):
        from ui_animations import PhaseFlash
        lbl = tk.Label(self.root, text="test")
        flash = PhaseFlash(lbl, "#1565C0", "#000000")
        flash.trigger()
        # After trigger -> _tick() called once, step should be 1
        self.assertEqual(flash._step, 1)

    def test_trigger_completes_on_step_6(self):
        from ui_animations import PhaseFlash
        lbl = tk.Label(self.root, text="test")
        flash = PhaseFlash(lbl, "#1565C0", "#1B1B1F")
        # Drive _step to the completion threshold; _tick() returns early and
        # does not schedule another after(), so after_id stays None.
        flash._step = PhaseFlash.STEPS
        flash._tick()
        # At or beyond STEPS, _tick resets foreground and clears after_id
        self.assertIsNone(flash._after_id)

    def test_update_colors(self):
        from ui_animations import PhaseFlash
        lbl = tk.Label(self.root, text="test")
        flash = PhaseFlash(lbl, "#000000", "#ffffff")
        flash.update_colors("#aabbcc", "#112233")
        self.assertEqual(flash._accent, "#aabbcc")
        self.assertEqual(flash._normal, "#112233")


class TestBindPressFeedback(unittest.TestCase):

    def setUp(self):
        self.root = _get_root()

    def test_binds_to_button(self):
        from ui_animations import bind_press_feedback
        btn = tk.Button(self.root, text="test", bg="#ff0000")
        btn._mat_bg = "#ff0000"
        bind_press_feedback(btn)
        # Tk normalises <ButtonPress-1> to <Button-1> in the binding table
        bindings = btn.bind()
        self.assertTrue(
            "<ButtonPress-1>" in bindings or "<Button-1>" in bindings,
            f"Expected button press binding, got: {bindings}"
        )

    def test_no_error_without_mat_bg(self):
        from ui_animations import bind_press_feedback
        btn = tk.Button(self.root, text="test", bg="#ff0000")
        # Should not raise even without _mat_bg
        try:
            bind_press_feedback(btn)
        except Exception as e:
            self.fail(f"bind_press_feedback raised: {e}")


if __name__ == "__main__":
    unittest.main()
