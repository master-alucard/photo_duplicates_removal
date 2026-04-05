"""
progress_tracker.py — Multi-phase progress tracking with ETA calculation.
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
        # Keep only the last 20 samples for speed estimation
        if len(self._speed_samples) > 20:
            self._speed_samples.pop(0)

    def finish_phase(self) -> None:
        """Mark the current phase as finished."""
        if self._current_idx < 0:
            return
        phase = self._phases[self._current_idx]
        phase.done_units = phase.total_units
        phase.end_time = time.monotonic()
        phase.status = "done"

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
        """Estimated seconds remaining. None if not enough data."""
        if self._current_idx < 0:
            return None
        phase = self._phases[self._current_idx]
        if phase.done_units == 0 or phase.total_units == 0:
            return None

        # Use speed from recent samples
        if len(self._speed_samples) >= 2:
            t0, u0 = self._speed_samples[0]
            t1, u1 = self._speed_samples[-1]
            elapsed = t1 - t0
            units_done = u1 - u0
            if elapsed > 0 and units_done > 0:
                rate = units_done / elapsed  # units per second
                remaining_in_phase = phase.total_units - phase.done_units
                # Also add estimates for subsequent phases
                eta = remaining_in_phase / rate
                # Estimate remaining phases based on current phase speed
                current_phase_weight = phase.weight
                remaining_weight = sum(
                    p.weight for p in self._phases[self._current_idx + 1:]
                    if p.status == "waiting"
                )
                if current_phase_weight > 0 and phase.done_units > 0:
                    current_phase_elapsed = time.monotonic() - phase.start_time
                    phase_fraction_done = phase.done_units / phase.total_units
                    if phase_fraction_done > 0:
                        projected_phase_total_time = current_phase_elapsed / phase_fraction_done
                        time_per_weight_unit = projected_phase_total_time / current_phase_weight
                        eta += remaining_weight * time_per_weight_unit
                return max(0.0, eta)

        # Fall back to elapsed-time extrapolation
        if phase.start_time > 0 and phase.done_units > 0:
            elapsed = time.monotonic() - phase.start_time
            fraction_done = phase.done_units / phase.total_units
            if fraction_done > 0:
                total_estimated = elapsed / fraction_done
                return max(0.0, total_estimated - elapsed)

        return None

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
            return "calculating..."
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
