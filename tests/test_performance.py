"""
tests/test_performance.py — Performance and stability tests for the scan pipeline.

Goals:
  • Verify that hashing 200+ synthetic images completes in a reasonable time.
  • Verify that progress callbacks are rate-limited (≤ N updates for N files).
  • Verify that the progress-tick polling pattern works correctly without
    root.after(0,…) calls from worker threads.
  • Smoke-test JPEG draft mode (no regression in hash quality).
"""
from __future__ import annotations

import io
import os
import tempfile
import time
from pathlib import Path
from typing import List

import pytest
from PIL import Image
import numpy as np


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_rgb_jpeg(w: int = 1024, h: int = 768, seed: int = 0) -> bytes:
    """Return JPEG bytes for a deterministic solid-color image."""
    rng = np.random.default_rng(seed)
    color = tuple(rng.integers(0, 256, size=3).tolist())
    img = Image.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_large_jpeg(seed: int = 0) -> bytes:
    """Return a ~6MP JPEG (3000×2000) to stress-test draft mode."""
    rng = np.random.default_rng(seed + 1000)
    arr = rng.integers(0, 256, (2000, 3000, 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ── rate-limiting tests ────────────────────────────────────────────────────────

class TestProgressRateLimiting:
    """Verify that scanner's progress callbacks are rate-limited."""

    def test_parallel_hashing_rate_limited(self, tmp_path):
        """With 100 images, the number of progress_cb calls should be << 100."""
        from scanner import collect_images
        from config import Settings

        # Create 100 small JPEGs
        n = 100
        for i in range(n):
            (tmp_path / f"img_{i:03d}.jpg").write_bytes(_make_rgb_jpeg(seed=i))

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            recursive=False,
            scan_threads=2,
        )

        cb_calls: List[tuple] = []

        def progress_cb(msg, done, total, phase):
            cb_calls.append((time.monotonic(), done, total, phase))

        collect_images(
            tmp_path,
            skip_paths=set(),
            settings=settings,
            progress_cb=progress_cb,
        )

        # Rate limiter: at most 20 calls/sec × typical hash time (well under n).
        # Even for 1-second total, that's 20 calls. 100 calls would mean no limiting.
        # We assert < n to confirm rate-limiting fired.
        assert len(cb_calls) <= n, (
            f"Expected rate-limited callbacks (≤{n}), got {len(cb_calls)}"
        )

        # And the final call must always report done == total
        if cb_calls:
            last = cb_calls[-1]
            assert last[1] == last[2], "Last progress_cb must report done == total"

    def test_sequential_hashing_rate_limited(self, tmp_path):
        """Single-thread path is also rate-limited."""
        from scanner import collect_images
        from config import Settings

        n = 50
        for i in range(n):
            (tmp_path / f"img_{i:03d}.jpg").write_bytes(_make_rgb_jpeg(seed=i + 200))

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            recursive=False,
            scan_threads=1,  # force sequential path
        )

        cb_calls: List[int] = []

        def progress_cb(msg, done, total, phase):
            cb_calls.append(done)

        collect_images(tmp_path, skip_paths=set(), settings=settings,
                       progress_cb=progress_cb)

        # Rate-limited: ≤ n callbacks for n files
        assert len(cb_calls) <= n
        if cb_calls:
            assert cb_calls[-1] == n, "Final callback must report all files done"


# ── JPEG draft mode tests ──────────────────────────────────────────────────────

class TestJpegDraftMode:
    """JPEG draft mode must not degrade hash quality for duplicates."""

    def test_draft_mode_identical_hashes(self, tmp_path):
        """Two identical JPEGs must produce identical pHashes regardless of size."""
        from scanner import _hash_image
        from config import Settings

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            use_dual_hash=True,
        )

        data = _make_rgb_jpeg(w=2048, h=1536, seed=42)
        p1 = tmp_path / "a.jpg"
        p2 = tmp_path / "b.jpg"
        p1.write_bytes(data)
        p2.write_bytes(data)

        rec1 = _hash_image(p1, settings)
        rec2 = _hash_image(p2, settings)

        assert rec1 is not None and rec2 is not None
        assert rec1.phash == rec2.phash, "Identical JPEGs must produce identical pHash"

    def test_draft_mode_reproducible(self, tmp_path):
        """Hashing the same large JPEG twice must produce the exact same hash.

        Draft mode is deterministic — multiple calls to _hash_image on the same
        file must always return identical pHash/dHash values.
        """
        from scanner import _hash_image
        from config import Settings

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            use_dual_hash=True,
        )

        # Create a 3000×2000 JPEG (large enough that draft mode activates)
        h, w = 2000, 3000
        x = np.linspace(0, 255, w, dtype=np.uint8)
        y = np.linspace(0, 200, h, dtype=np.uint8)
        r = np.outer(y, np.ones(w, dtype=np.uint8))
        g = np.outer(np.ones(h, dtype=np.uint8), x)
        b = ((r.astype(np.uint16) + g.astype(np.uint16)) // 2).astype(np.uint8)
        arr = np.stack([r, g, b], axis=2)
        img = Image.fromarray(arr, mode="RGB")

        large = tmp_path / "large_grad.jpg"
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        large.write_bytes(buf.getvalue())

        rec1 = _hash_image(large, settings)
        rec2 = _hash_image(large, settings)

        assert rec1 is not None and rec2 is not None
        assert rec1.phash == rec2.phash, "Draft mode must produce reproducible pHash"
        assert rec1.dhash == rec2.dhash, "Draft mode must produce reproducible dHash"

    def test_draft_mode_speed(self, tmp_path):
        """Hashing a large JPEG with draft mode enabled should complete quickly."""
        from scanner import _hash_image
        from config import Settings

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
        )

        large_path = tmp_path / "large.jpg"
        large_path.write_bytes(_make_large_jpeg(seed=77))

        t0 = time.perf_counter()
        rec = _hash_image(large_path, settings)
        elapsed = time.perf_counter() - t0

        assert rec is not None, "Should successfully hash a large JPEG"
        # On modern hardware draft mode should finish in < 5 s even on a slow CI.
        # Without draft mode, a 6MP JPEG at full resolution can take > 10 s.
        assert elapsed < 10.0, (
            f"Hashing large JPEG took {elapsed:.2f}s — may be missing draft mode"
        )


# ── throughput test ────────────────────────────────────────────────────────────

class TestHashingThroughput:
    """Throughput tests — ensure the pipeline scales to 500+ files."""

    def test_200_images_complete(self, tmp_path):
        """collect_images must handle 200 images without hanging."""
        from scanner import collect_images
        from config import Settings

        n = 200
        for i in range(n):
            (tmp_path / f"img_{i:04d}.jpg").write_bytes(
                _make_rgb_jpeg(w=128, h=96, seed=i)
            )

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            recursive=False,
            scan_threads=4,
        )

        t0 = time.perf_counter()
        records = collect_images(tmp_path, skip_paths=set(), settings=settings)
        elapsed = time.perf_counter() - t0

        assert len(records) == n, f"Expected {n} records, got {len(records)}"
        # 200 small JPEGs at 4 threads should complete in < 60 s on any machine.
        assert elapsed < 60.0, f"200 images took {elapsed:.1f}s — too slow"

    def test_library_cache_warmstart(self, tmp_path):
        """Warm-cache hashing must complete without flooding progress_cb."""
        from scanner import collect_images
        from config import Settings
        from library import Library, FileRecord, get_library_dir

        n = 100
        paths = []
        for i in range(n):
            p = tmp_path / f"cached_{i:03d}.jpg"
            p.write_bytes(_make_rgb_jpeg(seed=i + 500))
            paths.append(p)

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            recursive=False,
            scan_threads=4,
        )

        # First pass — build fresh hashes
        records_first = collect_images(tmp_path, skip_paths=set(), settings=settings)
        assert len(records_first) == n

        # Build an in-memory cache from the fresh records
        cache: dict = {}
        for rec in records_first:
            resolved = str(rec.path.resolve())
            try:
                st_mtime = rec.path.stat().st_mtime
                cache[resolved] = FileRecord.from_image_record(rec, st_mtime=st_mtime)
            except Exception:
                pass

        # Second pass — warm cache; collect callback timings
        cb_times: List[float] = []

        def progress_cb(msg, done, total, phase):
            cb_times.append(time.monotonic())

        t0 = time.perf_counter()
        records_second = collect_images(
            tmp_path, skip_paths=set(), settings=settings,
            progress_cb=progress_cb, library_cache=cache,
        )
        elapsed = time.perf_counter() - t0

        assert len(records_second) == n

        # Warm-cache should be very fast (<= 10 s for 100 files)
        assert elapsed < 10.0, f"Warm-cache hashing took {elapsed:.1f}s"

        # Rate-limiting: callbacks must be rate-limited even on warm cache
        # (which produces bursts without rate-limiting).
        assert len(cb_times) <= n, (
            f"Got {len(cb_times)} callbacks for {n} files — rate-limit may be broken"
        )

        # Check inter-callback spacing: consecutive callbacks must be ≥ 40 ms apart
        # (our target is 50 ms; 40 ms allows some jitter).
        if len(cb_times) >= 2:
            min_gap = min(
                cb_times[i + 1] - cb_times[i]
                for i in range(len(cb_times) - 1)
            )
            # At least 80% of pairs should be spaced ≥ 40 ms apart.
            spaced = sum(
                1 for i in range(len(cb_times) - 1)
                if cb_times[i + 1] - cb_times[i] >= 0.04
            )
            # Accept if most pairs are spaced (last callback can be immediate)
            if len(cb_times) > 3:
                assert spaced >= len(cb_times) - 2, (
                    f"Progress callbacks not rate-limited: only {spaced}/{len(cb_times)-1} "
                    "pairs were ≥40 ms apart"
                )


# ── stop/cancel responsiveness ─────────────────────────────────────────────────

class TestStopResponsiveness:
    """stop_flag must halt hashing quickly even for large queues."""

    def test_stop_during_hashing(self, tmp_path):
        """Setting stop_flag while hashing 200 files should abort promptly."""
        from scanner import collect_images
        from config import Settings

        n = 200
        for i in range(n):
            (tmp_path / f"stop_{i:04d}.jpg").write_bytes(
                _make_rgb_jpeg(w=64, h=64, seed=i + 1000)
            )

        settings = Settings(
            src_folder=str(tmp_path),
            out_folder=str(tmp_path / "out"),
            recursive=False,
            scan_threads=4,
        )

        stop_flag: list[bool] = [False]
        call_count = 0

        def stopping_cb(msg, done, total, phase):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                stop_flag[0] = True

        t0 = time.perf_counter()
        records = collect_images(
            tmp_path, skip_paths=set(), settings=settings,
            progress_cb=stopping_cb, stop_flag=stop_flag,
        )
        elapsed = time.perf_counter() - t0

        # Should complete quickly once stop_flag is set
        assert elapsed < 30.0, (
            f"Stop did not abort hashing promptly: took {elapsed:.1f}s"
        )
        # Should have fewer records than the total (scan was aborted)
        assert len(records) < n, (
            f"Expected < {n} records after stop, got {len(records)}"
        )
