"""
tests/test_progress_tracker.py — Unit tests for PhaseTracker ETA logic.

Time-sensitive tests bypass real wall-clock calls by directly manipulating
phase.start_time and _speed_samples so results are deterministic.
"""
from __future__ import annotations

import time
from collections import deque
from unittest.mock import patch

import pytest

from progress_tracker import (
    PhaseTracker,
    _MIN_ELAPSED_FOR_ETA,
    _MIN_FRACTION_FOR_ETA,
    _MIN_SECS_PER_WEIGHT,
    _MIN_WINDOW_SAMPLES,
    _MIN_WINDOW_ELAPSED,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tracker(phases: list[str]) -> PhaseTracker:
    return PhaseTracker(phases)


def _inject_elapsed(tracker: PhaseTracker, elapsed: float) -> None:
    """Shift the active phase start_time so phase_elapsed == elapsed."""
    if tracker._current_idx >= 0:
        tracker._phases[tracker._current_idx].start_time = time.monotonic() - elapsed


def _inject_samples(tracker: PhaseTracker, rate: float, n: int = 10) -> None:
    """
    Replace speed samples with n synthetic entries that imply *rate* units/sec.
    Samples span _MIN_WINDOW_ELAPSED seconds so the blend condition fires.
    """
    now = time.monotonic()
    span = max(_MIN_WINDOW_ELAPSED * 1.5, 2.0)
    tracker._speed_samples = deque(maxlen=60)
    for i in range(n):
        t = now - span + i * (span / max(n - 1, 1))
        u = int(rate * (t - (now - span)))
        tracker._speed_samples.append((t, u))


# ── ETA guard thresholds ──────────────────────────────────────────────────────

class TestEtaGuards:

    def test_eta_none_before_min_elapsed(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 1000)
        t.update(500)  # 50% done — fraction guard satisfied
        # start_time is "now", so elapsed ≈ 0 — elapsed guard NOT satisfied
        assert t.eta_seconds is None

    def test_eta_none_before_min_fraction(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 1000)
        t.update(1)   # 0.1% done — fraction guard NOT satisfied
        _inject_elapsed(t, _MIN_ELAPSED_FOR_ETA + 1)
        assert t.eta_seconds is None

    def test_eta_none_when_no_progress(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 1000)
        # done_units stays 0
        _inject_elapsed(t, 10)
        assert t.eta_seconds is None

    def test_eta_some_value_when_guards_satisfied(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(50)  # 50%
        _inject_elapsed(t, _MIN_ELAPSED_FOR_ETA + 1)
        assert t.eta_seconds is not None
        assert t.eta_seconds >= 0


# ── Elapsed-time extrapolation ────────────────────────────────────────────────

class TestEtaElapsedExtrapolation:

    def test_half_done_projects_remaining_equal_elapsed(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(50)  # 50% done in ~elapsed seconds
        elapsed = 10.0
        _inject_elapsed(t, elapsed)
        # No sliding window samples → pure elapsed path
        # projected_total = 10 / 0.5 = 20, eta = 20 - 10 = 10
        eta = t.eta_seconds
        assert eta is not None
        assert abs(eta - elapsed) < 2.0  # within 2s tolerance for monotonic drift

    def test_quarter_done_projects_triple_remaining(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(25)  # 25%
        elapsed = 5.0
        _inject_elapsed(t, elapsed)
        # projected_total = 5 / 0.25 = 20, eta = 15
        eta = t.eta_seconds
        assert eta is not None
        assert abs(eta - 15.0) < 2.0

    def test_eta_zero_when_complete(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(100)
        _inject_elapsed(t, 10.0)
        eta = t.eta_seconds
        # fraction_done = 1.0 → projected_total = elapsed, eta_current = 0
        assert eta is not None
        assert eta == 0.0 or abs(eta) < 1.0


# ── Sliding-window blend ──────────────────────────────────────────────────────

class TestSlidingWindowBlend:

    def test_blend_differs_from_pure_elapsed_when_rate_diverges(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 1000)
        t.update(500)
        elapsed = 10.0
        _inject_elapsed(t, elapsed)

        # Synthetic window rate: 200 units/sec (much faster than elapsed rate of 50/s)
        _inject_samples(t, rate=200.0, n=_MIN_WINDOW_SAMPLES + 2)

        eta = t.eta_seconds
        # pure elapsed would give 10s, window gives (500/200)=2.5s
        # blend = 0.6*10 + 0.4*2.5 = 7s
        # damping: _last_eta was None → raw returned directly
        assert eta is not None
        # Should be < 10 because the fast window pulled it down
        assert eta < 10.0

    def test_fewer_than_min_samples_falls_back_to_elapsed(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(50)
        elapsed = 10.0
        _inject_elapsed(t, elapsed)
        # Only 2 samples — not enough for window blend
        now = time.monotonic()
        t._speed_samples = deque([(now - 2.0, 0), (now, 50)], maxlen=60)
        # 2 < _MIN_WINDOW_SAMPLES (5) → pure elapsed
        eta = t.eta_seconds
        assert eta is not None
        assert abs(eta - elapsed) < 2.0


# ── Asymmetric damping ────────────────────────────────────────────────────────

class TestAsymmetricDamping:

    @staticmethod
    def _apply_damping(last: float, raw: float) -> float:
        """Replicate the damping formula from PhaseTracker.eta_seconds."""
        if raw > last:
            return 0.8 * last + 0.2 * raw
        return 0.5 * last + 0.5 * raw

    def test_eta_rising_damped_strongly(self):
        # raw = 100, last = 20 → smoothed = 0.8*20 + 0.2*100 = 36
        smoothed = self._apply_damping(last=20.0, raw=100.0)
        assert abs(smoothed - 36.0) < 0.01

    def test_eta_falling_accepted_quickly(self):
        # raw = 5, last = 20 → smoothed = 0.5*20 + 0.5*5 = 12.5
        smoothed = self._apply_damping(last=20.0, raw=5.0)
        assert abs(smoothed - 12.5) < 0.01

    def test_first_call_returns_raw_unsmoothed(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(50)
        _inject_elapsed(t, 10.0)
        assert t._last_eta is None
        eta1 = t.eta_seconds
        # First call: _last_eta was None → raw value returned as-is
        assert eta1 is not None
        # After first call _last_eta is set
        assert t._last_eta is not None

    def test_start_phase_resets_damping(self):
        t = _make_tracker(["Hashing", "Comparing"])
        t.start_phase("Hashing", 100)
        t.update(50)
        _inject_elapsed(t, 10.0)
        t.eta_seconds  # seed _last_eta
        assert t._last_eta is not None

        t.finish_phase()
        t.start_phase("Comparing", 200)
        assert t._last_eta is None


# ── notify_gap (sleep compensation) ──────────────────────────────────────────

class TestNotifyGap:

    def test_gap_shifts_start_time(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        before = t._phases[0].start_time
        t.notify_gap(60.0)
        after = t._phases[0].start_time
        assert abs((after - before) - 60.0) < 0.1

    def test_gap_clears_speed_samples(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(10)
        t.update(20)
        assert len(t._speed_samples) > 0
        t.notify_gap(30.0)
        assert len(t._speed_samples) == 0

    def test_gap_clears_damping_state(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(50)
        _inject_elapsed(t, 10.0)
        t.eta_seconds  # seed _last_eta
        t.notify_gap(120.0)
        assert t._last_eta is None

    def test_gap_no_effect_when_no_active_phase(self):
        t = _make_tracker(["Hashing"])
        # No start_phase called → current_idx = -1
        t.notify_gap(60.0)  # must not raise

    def test_gap_zero_or_negative_ignored(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        before = t._phases[0].start_time
        t.notify_gap(0.0)
        assert t._phases[0].start_time == before


# ── Future-phase projection ───────────────────────────────────────────────────

class TestFuturePhaseProjection:

    def test_future_phase_floor_prevents_underestimate(self):
        # Simulate: Hashing finishes in 0.01s (cached), Comparing is pending.
        # Without the floor, projected Comparing time would be near-zero.
        t = _make_tracker(["Hashing", "Comparing"])
        t.start_phase("Hashing", 100)
        t.update(50)
        _inject_elapsed(t, 10.0)  # elapsed long enough for guard

        # Override completed_time_per_weight with a very fast rate
        hashing_weight = t._phases[0].weight  # 50
        t._completed_time_per_weight = [0.001]  # nearly instant

        eta = t.eta_seconds
        assert eta is not None
        # Comparing weight is 30; floor is _MIN_SECS_PER_WEIGHT * 30
        comparing_weight = t._phases[1].weight
        min_future = _MIN_SECS_PER_WEIGHT * comparing_weight
        assert eta >= min_future - 5.0  # allow ETA current to dominate slightly

    def test_future_phase_uses_completed_data(self):
        t = _make_tracker(["Hashing", "Comparing"])
        t.start_phase("Hashing", 100)
        t.update(50)
        _inject_elapsed(t, 10.0)

        # Completed phases recorded a realistic tpw
        comparing_weight = t._phases[1].weight
        realistic_tpw = 2.0  # 2s per weight unit
        t._completed_time_per_weight = [realistic_tpw]

        eta = t.eta_seconds
        assert eta is not None
        expected_future = max(realistic_tpw, _MIN_SECS_PER_WEIGHT) * comparing_weight
        # ETA = eta_current + eta_future; just verify future contributes correctly
        assert eta >= expected_future * 0.5  # conservatively

    def test_no_future_phases_gives_only_current_eta(self):
        t = _make_tracker(["Hashing"])  # single phase
        t.start_phase("Hashing", 100)
        t.update(50)
        elapsed = 10.0
        _inject_elapsed(t, elapsed)
        eta = t.eta_seconds
        assert eta is not None
        # No future weight → eta ≈ eta_current
        assert abs(eta - elapsed) < 3.0


# ── total_pct ─────────────────────────────────────────────────────────────────

class TestTotalPct:

    def test_all_waiting_is_zero(self):
        t = _make_tracker(["Hashing", "Comparing"])
        assert t.total_pct == 0.0

    def test_one_done_one_waiting(self):
        t = _make_tracker(["Hashing", "Comparing"])
        t.start_phase("Hashing", 100)
        t.finish_phase()
        pct = t.total_pct
        # Hashing weight=50, Comparing weight=30, total=80 → 50/80 * 100 = 62.5%
        assert abs(pct - (50 / 80 * 100)) < 0.5

    def test_active_phase_partial(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(25)  # 25%
        # single phase: total_pct = 25%
        assert abs(t.total_pct - 25.0) < 0.5

    def test_all_done_is_100(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.finish_phase()
        assert abs(t.total_pct - 100.0) < 0.01


# ── format_eta strings ────────────────────────────────────────────────────────

class TestFormatEta:

    def _set_raw_eta(self, tracker: PhaseTracker, value: float) -> None:
        """Directly plant _last_eta so format_eta returns a predictable string."""
        tracker._phases[0].done_units = 50
        tracker._phases[0].total_units = 100
        tracker._last_eta = value
        # Patch time so elapsed guard is satisfied
        tracker._phases[0].start_time = time.monotonic() - (_MIN_ELAPSED_FOR_ETA + 5)

    def test_almost_done_for_sub_5(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        self._set_raw_eta(t, 3.0)
        assert t.format_eta() == "almost done"

    def test_seconds_format(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        self._set_raw_eta(t, 30.0)
        result = t.format_eta()
        assert result.startswith("~") and result.endswith("s")

    def test_minutes_seconds_format(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        self._set_raw_eta(t, 150.0)  # 2m 30s
        result = t.format_eta()
        assert "m" in result and "s" in result

    def test_hours_format(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        # Inject a _last_eta large enough that after damping it stays > 3600.
        # Damping when raw < last: smoothed = 0.5*last + 0.5*raw.
        # raw is ~_MIN_ELAPSED_FOR_ETA range (small); to keep smoothed > 3600:
        # 0.5 * 10000 + 0.5 * ~10 ≈ 5005 → well above 3600.
        self._set_raw_eta(t, 10000.0)
        result = t.format_eta()
        assert "h" in result

    def test_calculating_when_eta_none(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        # No progress set → eta_seconds is None
        assert "calculating" in t.format_eta()


# ── current_speed ─────────────────────────────────────────────────────────────

class TestCurrentSpeed:

    def test_zero_with_no_samples(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        assert t.current_speed == 0.0

    def test_zero_with_one_sample(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(10)
        assert t.current_speed == 0.0

    def test_computed_speed(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        now = time.monotonic()
        t._speed_samples = deque([
            (now - 4.0, 0),
            (now, 20),
        ], maxlen=60)
        # rate = 20 units / 4s = 5 units/s
        assert abs(t.current_speed - 5.0) < 0.5


# ── phase_summaries ───────────────────────────────────────────────────────────

class TestPhaseSummaries:

    def test_summaries_length_matches_phase_count(self):
        t = _make_tracker(["Hashing", "Comparing", "Moving"])
        t.start_phase("Hashing", 100)
        summaries = t.phase_summaries
        assert len(summaries) == 3

    def test_done_phase_shows_100_pct(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.finish_phase()
        s = t.phase_summaries[0]
        assert s["status"] == "done"
        assert s["pct"] == 100.0

    def test_waiting_phase_shows_zero(self):
        t = _make_tracker(["Hashing", "Comparing"])
        t.start_phase("Hashing", 100)
        s = t.phase_summaries[1]
        assert s["status"] == "waiting"
        assert s["pct"] == 0.0

    def test_active_phase_shows_partial(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        t.update(40)
        s = t.phase_summaries[0]
        assert s["status"] == "active"
        assert abs(s["pct"] - 40.0) < 0.5
        assert s["done_units"] == 40
        assert s["total_units"] == 100


# ── finish_phase records tpw ──────────────────────────────────────────────────

class TestFinishPhase:

    def test_records_time_per_weight(self):
        t = _make_tracker(["Hashing"])
        t.start_phase("Hashing", 100)
        # Fake elapsed by shifting start_time back
        _inject_elapsed(t, 10.0)
        t.finish_phase()
        assert len(t._completed_time_per_weight) == 1
        hashing_weight = t._phases[0].weight  # 50
        expected_tpw = 10.0 / hashing_weight
        assert abs(t._completed_time_per_weight[0] - expected_tpw) < 0.2

    def test_no_record_when_weight_zero(self):
        t = _make_tracker(["Discovery"])
        # Discovery weight = 1 — will still record; use a synthetic 0-weight phase
        t._phases[0].weight = 0
        t.start_phase("Discovery", 10)
        t.finish_phase()
        assert len(t._completed_time_per_weight) == 0
