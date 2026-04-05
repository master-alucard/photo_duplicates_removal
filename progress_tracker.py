"""
progress_tracker.py — Multi-phase progress tracking with ETA calculation.

ETA strategy:
  - Primary estimate: elapsed-time extrapolation (stable across the whole phase)
  - Secondary: sliding-window rate (reacts faster to speed changes mid-phase)
  - Blend: 60% elapsed-based + 40% window-based once enough data exists
  - Future phases: projected from current phase speed, with a floor to prevent
    underestimates when cache hits make the current phase artificially fast.
  - Guard: no ETA shown until ≥ 2 s elapsed AND ≥ 3% of phase completed,
    preventing misleading flashes of "~3 s" at start of cached scans.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# Phase weights (must sum to a meaningful total; proportions matter)
PHASE_WEIGHTS: dict[str, int] = {
    "Discovery": 1,
    "Hashing": 50,
    "Comparing": 30,
    "Metadata": 10,
    "Moving": 5,
    "Report": 4,
    # Compare Scan (custom) phases — weights reflect typical wall-clock ratio
    "Main folder": 40,
    "Check folder": 40,
}
_DEFAULT_WEIGHT = 10  # fallback for unknown phase names

# Minimum elapsed seconds and fraction-done before trusting the rate enough
# to display a numeric ETA.  Prevents wildly optimistic estimates when the
# first N files are library-cache hits (instant hash lookups).
_MIN_ELAPSED_FOR_ETA = 2.0   # seconds
_MIN_FRACTION_FOR_ETA = 0.03  # 3%

# When projecting time for future phases, enforce at least this many seconds
# per weight unit.  Prevents an instant-cache Hashing phase (weight 50, 2 s)
# from estimating the O(n²) Comparing phase (weight 30) at 1.2 s.
_MIN_SECS_PER_WEIGHT = 0.5

# Minimum sliding-window samples required for the blended estimate.
# Fewer samples fall back to pure elapsed-time extrapolation.
_MIN_WINDOW_SAMPLES = 5
_MIN_WINDOW_ELAPSED = 1.0  # seconds — window span must be ≥ this


@dataclass
class _PhaseInfo:
    name: str
    weight: int
    total_units: int = 0
    done_units: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "waiting"   # "waiting", "active", "done"


class PhaseTracker:
    """Tracks progress across multiple named phases with ETA calculation."""

    def __init__(self, phases: list[str]) -> None:
        self._phases: list[_PhaseInfo] = []
        self._current_idx: int = -1
        self._total_weight: int = 0
        self._speed_samples: list[tuple[float, int]] = []  # (timestamp, cumulative_done)
        # Track completed phase durations for better cross-phase projection
        self._completed_time_per_weight: list[float] = []

        for name in phases:
            w = PHASE_WEIGHTS.get(name, _DEFAULT_WEIGHT)
            self._phases.append(_PhaseInfo(name=name, weight=w))
            self._total_weight += w

    # ── control ──────────────────────────────────────────────────────────────

    def start_phase(self, name: str, total_units: int) -> None:
        """Mark a named phase as started with the given total work units."""
        idx = self._find_phase(name)
        if idx == -1:
            # Auto-add unknown phases
            w = PHASE_WEIGHTS.get(name, _DEFAULT_WEIGHT)
            self._phases.append(_PhaseInfo(name=name, weight=w))
            self._total_weight += w
            idx = len(self._phases) - 1

        phase = self._phases[idx]
        phase.total_units = max(total_units, 1)
        phase.done_units = 0
        phase.start_time = time.monotonic()
        phase.status = "active"
        self._current_idx = idx
        self._speed_samples.clear()

    def update(self, done_units: int) -> None:
        """Update progress in the current phase."""
        if self._current_idx < 0:
            return
        phase = self._phases[self._current_idx]
        phase.done_units = min(done_units, phase.total_units)
        now = time.monotonic()
        self._speed_samples.append((now, done_units))
        # Keep only the last 30 samples for speed estimation
        if len(self._speed_samples) > 30:
            self._speed_samples.pop(0)

    def notify_gap(self, gap_seconds: float) -> None:
        """Compensate for a detected time gap (e.g., system sleep/hibernate).

        Shifts the active phase's start_time forward by *gap_seconds*,
        so elapsed-time calculations exclude the gap.  Speed samples are
        cleared because they span the gap and would produce incorrect rates.
        """
        if self._current_idx < 0 or gap_seconds <= 0:
            return
        phase = self._phases[self._current_idx]
        if phase.start_time > 0:
            phase.start_time += gap_seconds
        self._speed_samples.clear()

    def finish_phase(self) -> None:
        """Mark the current phase as finished."""
        if self._current_idx < 0:
            return
        phase = self._phases[self._current_idx]
        phase.done_units = phase.total_units
        phase.end_time = time.monotonic()
        phase.status = "done"
        # Record actual time-per-weight for this completed phase
        duration = phase.end_time - phase.start_time
        if phase.weight > 0 and duration > 0:
            self._completed_time_per_weight.append(duration / phase.weight)

    # ── properties ───────────────────────────────────────────────────────────

    @property
    def total_pct(self) -> float:
        """Overall progress percentage 0.0-100.0 across all phases."""
        if self._total_weight == 0:
            return 0.0
        done_weight = 0.0
        for phase in self._phases:
            if phase.status == "done":
                done_weight += phase.weight
            elif phase.status == "active" and phase.total_units > 0:
                fraction = phase.done_units / phase.total_units
                done_weight += phase.weight * fraction
        return min(100.0, done_weight / self._total_weight * 100.0)

    @property
    def current_phase_pct(self) -> float:
        """Progress percentage 0.0-100.0 within the current phase."""
        if self._current_idx < 0:
            return 0.0
        phase = self._phases[self._current_idx]
        if phase.total_units == 0:
            return 0.0
        return min(100.0, phase.done_units / phase.total_units * 100.0)

    @property
    def eta_seconds(self) -> Optional[float]:
        """Estimated seconds remaining. None if not enough data.

        Two guards prevent misleading early estimates:
          1. Must have ≥ _MIN_ELAPSED_FOR_ETA seconds of data.
          2. Must have completed ≥ _MIN_FRACTION_FOR_ETA of the phase.
        This prevents showing "~3 s" when 860 cached files flash through
        the Hashing phase in 2 seconds but Comparing is yet to start.
        """
        if self._current_idx < 0:
            return None
        phase = self._phases[self._current_idx]
        if phase.done_units == 0 or phase.total_units == 0:
            return None

        now = time.monotonic()
        phase_elapsed = now - phase.start_time if phase.start_time > 0 else 0.0
        fraction_done = phase.done_units / phase.total_units

        # Guard: don't show a numeric ETA until we have meaningful data
        if phase_elapsed < _MIN_ELAPSED_FOR_ETA or fraction_done < _MIN_FRACTION_FOR_ETA:
            return None

        # ── Current phase ETA ────────────────────────────────────────────
        # Primary: elapsed-time extrapolation (most stable over the whole phase)
        projected_total = phase_elapsed / fraction_done
        eta_current = max(0.0, projected_total - phase_elapsed)

        # Secondary: sliding-window rate (reacts faster to mid-phase speed changes)
        if len(self._speed_samples) >= _MIN_WINDOW_SAMPLES:
            t0, u0 = self._speed_samples[0]
            t1, u1 = self._speed_samples[-1]
            window_elapsed = t1 - t0
            window_units = u1 - u0
            if window_elapsed >= _MIN_WINDOW_ELAPSED and window_units > 0:
                rate = window_units / window_elapsed
                eta_window = (phase.total_units - phase.done_units) / rate
                # Blend: 60% elapsed-based (stable) + 40% window (responsive)
                eta_current = 0.6 * eta_current + 0.4 * eta_window

        # ── Future phases ETA ────────────────────────────────────────────
        eta_future = 0.0
        remaining_weight = sum(
            p.weight for p in self._phases[self._current_idx + 1:]
            if p.status == "waiting"
        )
        if remaining_weight > 0:
            # Best estimate: use actual completed-phase data if available
            if self._completed_time_per_weight:
                # Average across all completed phases for a realistic baseline
                avg_tpw = sum(self._completed_time_per_weight) / len(self._completed_time_per_weight)
            else:
                # Only the current phase is available — project from it
                current_weight = phase.weight
                avg_tpw = projected_total / current_weight if current_weight > 0 else 1.0

            # Floor: prevent instant-cache phases from producing tiny projections.
            # O(n²) Comparing after cached Hashing would otherwise estimate ~1 s.
            avg_tpw = max(avg_tpw, _MIN_SECS_PER_WEIGHT)
            eta_future = remaining_weight * avg_tpw

        return max(0.0, eta_current + eta_future)

    @property
    def phase_summaries(self) -> list[dict]:
        """List of phase summary dicts: {name, status, duration_s, pct}."""
        result = []
        now = time.monotonic()
        for phase in self._phases:
            if phase.status == "done":
                duration = phase.end_time - phase.start_time
                pct = 100.0
            elif phase.status == "active":
                duration = now - phase.start_time if phase.start_time > 0 else 0.0
                pct = self._phase_pct(phase)
            else:
                duration = 0.0
                pct = 0.0
            result.append({
                "name": phase.name,
                "status": phase.status,
                "duration_s": duration,
                "pct": pct,
                "done_units": phase.done_units,
                "total_units": phase.total_units,
            })
        return result

    # ── formatting ───────────────────────────────────────────────────────────

    def format_eta(self) -> str:
        """Return human-readable ETA string like '~2m 30s' or 'calculating...'"""
        eta = self.eta_seconds
        if eta is None:
            return "calculating\u2026"
        total_s = int(eta)
        if total_s < 5:
            return "almost done"
        if total_s < 60:
            return f"~{total_s}s"
        minutes = total_s // 60
        seconds = total_s % 60
        if minutes >= 60:
            hours = minutes // 60
            mins = minutes % 60
            return f"~{hours}h {mins}m"
        return f"~{minutes}m {seconds}s"

    @property
    def current_phase_name(self) -> str:
        """Name of the currently active phase."""
        if self._current_idx < 0:
            return ""
        return self._phases[self._current_idx].name

    @property
    def current_phase_number(self) -> int:
        """1-based index of the current phase."""
        return self._current_idx + 1

    @property
    def total_phases(self) -> int:
        return len(self._phases)

    # ── internal helpers ─────────────────────────────────────────────────────

    def _find_phase(self, name: str) -> int:
        for i, p in enumerate(self._phases):
            if p.name == name:
                return i
        return -1

    @staticmethod
    def _phase_pct(phase: _PhaseInfo) -> float:
        if phase.total_units == 0:
            return 0.0
        return min(100.0, phase.done_units / phase.total_units * 100.0)
