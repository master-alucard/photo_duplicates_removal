"""
scanner.py — Image collection and duplicate-group detection via perceptual hashing.
v2 rewrite with aspect-ratio guard, brightness guard, dual-hash, histogram intersection,
series detection, RAW companion matching, and pause/resume support.
"""
from __future__ import annotations

import datetime
import os
import sys as _sys
import time as _time
from collections import defaultdict
import numpy as _np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Literal, Optional

# ── Recursion-depth safety net ───────────────────────────────────────────────
# Raise Python's default 1 000-frame recursion limit when scanning very large
# image collections.  Most code paths in this module are iterative (BK-tree
# query/insert, _split_oversized_bucket work-queue, union-find with path
# compression), but defensive numerics inside dependencies (numpy / Pillow
# format plugins) and any future contributor mistakes still benefit from a
# bigger headroom on a 64-bit Python.  The chosen value is well below the
# native C-stack limit on Windows / Linux / macOS so we won't blow the stack.
_MIN_RECURSION_LIMIT = 5000
try:
    if _sys.getrecursionlimit() < _MIN_RECURSION_LIMIT:
        _sys.setrecursionlimit(_MIN_RECURSION_LIMIT)
except Exception:
    # setrecursionlimit can fail on exotic interpreters — keep the default.
    pass

from PIL import Image, ImageOps
import imagehash

from config import Settings
from metadata import (
    extract_date_from_filename,
    extract_date_from_exif,
    extract_date_from_exif_from_img,
)

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".gif", ".heic", ".heif",
}

RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf",
    ".rw2", ".pef", ".srw", ".x3f", ".3fr",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm",
    ".flv", ".3gp", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
    ".rm", ".rmvb", ".vob", ".divx", ".asf", ".f4v",
}

KeepStrategy = Literal["pixels", "oldest"]
ProgressCb = Callable[[str, int, int, str], None]

# Files in the same dimension bucket with pHash distance ≤ this value are
# treated as exact duplicates (same image saved twice) — only the best copy
# is kept.  Files with pHash > this value are genuine burst/series shots and
# all are kept as originals.  Calibration data shows 0 for exact dupes and
# ≥ 3 for genuinely different shots, so 2 is a clean separator.
_EXACT_DUP_PHASH = 2

# Use BK-tree candidate filtering when the collection exceeds this size.
# Lowered from 200: BK-tree is O(n log n) vs brute-force O(n²), and 50 images
# is already enough to see a speedup. 200 was leaving medium-sized scans on the
# slow path unnecessarily.
_BKTREE_THRESHOLD = 50

# When comparing RAW vs JPEG file dimensions, allow up to this relative
# difference before declaring them "different resolutions".  rawpy decodes
# the full sensor area (including a few border pixels) so a Canon M100 CR2
# decodes to 6024×4020 while the camera JPEG is 6000×4000 — 0.4% difference.
# 2 % is a safe upper bound that still distinguishes full-res from downscaled.
_CROSS_FORMAT_DIM_TOL = 0.02

# Cross-format histogram floor — DISABLED (set to 0.0).
#
# Rationale: rawpy's default postprocess(use_camera_wb=True, output_bps=8)
# maps sensor data linearly to 8-bit without the camera's tone curve / S-curve,
# producing images ~2× brighter (brightness ≈ 196/255) compared to the
# camera-generated JPEG (brightness ≈ 92/255).  This brightness difference
# collapses histogram intersection to 0.000–0.243 for true same-shot RAW+JPEG
# pairs — all below the former floor of 0.25 — causing every cross-format
# duplicate to be missed.
#
# The pHash + cross_format_threshold_factor guards already handle false-positive
# discrimination (different scenes always have pHash distance > 10 bits).
_CROSS_FORMAT_HIST_FLOOR = 0.0

# Base threshold used when computing the absolute cross-format pHash threshold.
#
# ``cross_format_threshold_factor`` (default 6.0) was calibrated at threshold=2:
#   effective CF threshold = 2 * 6.0 = 12 bits
# This covers all true RAW+JPEG pairs (max intra-group pHash=12) while staying
# below the inter-group safety gap (min inter-group pHash=20).
#
# Using the user's current ``settings.threshold`` as the base would cause the CF
# threshold to scale linearly with the sweep value during calibration (e.g.
# threshold=4 → cf_thr=24, which overlaps the inter-group gap and chains unrelated
# landscape shots together via single-linkage union-find).
_CF_BASE_THRESHOLD: int = 2

# ── Hashing performance constants ────────────────────────────────────────────
#
# Pre-downscale every image to at most _HASH_WORKING_SIZE pixels on its longest
# side before hashing.  imagehash.phash internally resizes to 32×32 (LANCZOS)
# anyway; starting from 1024-max is ~40× faster than starting from 24 MP and
# produces numerically-equivalent hashes (≤ 1-bit drift, well below detection
# thresholds).  Memory per thread drops from ~300 MB to ~6 MB for RAW/24MP JPEG.
_HASH_WORKING_SIZE = 1024

# When the scan folder lives on an HDD (seek-sensitive), cap parallel readers to
# this value to prevent seek-thrash and reduce disk overheating.  Purely CPU-bound
# work (pHash on a pre-downscaled image) is light enough that 4 threads saturate
# a modern SSD / NVMe without thrashing an HDD (2 sequential decoders + 2 CPU-only
# workers is a good balance for mixed drives).  Users with slow spinning disks can
# override via Settings → scan_threads = 1.
_HDD_THREAD_CAP = 4


# ── BK-tree for fast nearest-neighbour hash lookup ────────────────────────────

def _hamming(a: int, b: int) -> int:
    """Popcount of XOR — counts differing bits between two integers.

    Uses int.bit_count() (Python 3.11+) which is implemented in C and
    ~3× faster than the equivalent ``bin(a ^ b).count('1')`` string path.
    Falls back to the string path on older interpreters so tests pass on 3.10.
    """
    try:
        return (a ^ b).bit_count()
    except AttributeError:  # Python < 3.11
        return bin(a ^ b).count('1')


class _BKTree:
    """Burkhard-Keller tree keyed by Hamming distance.

    Reduces candidate lookup from O(n) to O(log n) average for small thresholds
    in a 64-bit hash space.  For threshold=2 this eliminates >99% of unnecessary
    _can_be_similar calls on large collections.
    """
    __slots__ = ("_root",)

    def __init__(self) -> None:
        # Each node is [hash_int, record_idx, {distance: child_node}]
        self._root: "list | None" = None

    def insert(self, hash_int: int, idx: int) -> None:
        node: list = [hash_int, idx, {}]
        if self._root is None:
            self._root = node
            return
        cur = self._root
        while True:
            d = _hamming(hash_int, cur[0])
            if d not in cur[2]:
                cur[2][d] = node
                return
            cur = cur[2][d]

    def query(self, hash_int: int, threshold: int) -> list:
        """Return indices of all entries within Hamming distance ≤ threshold."""
        if self._root is None:
            return []
        results: list = []
        stack = [self._root]
        while stack:
            cur = stack.pop()
            d = _hamming(hash_int, cur[0])
            if d <= threshold:
                results.append(cur[1])
            lo = d - threshold
            hi = d + threshold
            for dist, child in cur[2].items():
                if lo <= dist <= hi:
                    stack.append(child)
        return results


@dataclass
class ImageRecord:
    path: Path
    width: int
    height: int
    file_size: int
    phash: imagehash.ImageHash
    dhash: imagehash.ImageHash
    mtime: float
    brightness: float               # mean pixel brightness 0.0-255.0
    histogram: list[float]          # 96-value normalized (32 bins x 3 RGB channels)
    companions: list[Path] = field(default_factory=list)  # RAW companion paths
    metadata_count: int = 0         # number of EXIF fields
    # Rotation hashes (None when record came from an old cache / serialised state)
    phash_r90:  "Optional[imagehash.ImageHash]" = None
    phash_r180: "Optional[imagehash.ImageHash]" = None
    phash_r270: "Optional[imagehash.ImageHash]" = None
    # Video flag — True for records created by collect_videos().  width/height=0,
    # phash holds the thumbnail frame hash (or zero-hash if extraction failed).
    is_video: bool = False
    # EXIF capture date/time (DateTimeOriginal).  None when unavailable (RAW postprocess
    # path, PNG, non-EXIF formats, old library cache entries).  Used by the cross-format
    # date guard in _can_be_similar to prevent same-subject photos from different days
    # from being chained into one group via the lenient cross-format pHash threshold.
    exif_date: "Optional[datetime.datetime]" = None

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def size_label(self) -> str:
        mb = self.file_size / (1024 * 1024)
        return f"{mb:.2f} MB" if mb >= 1 else f"{self.file_size // 1024} KB"

    def dim_label(self) -> str:
        if self.is_video:
            return "Video"
        mp = self.pixels / 1_000_000
        return f"{self.width}x{self.height}  ({mp:.1f} MP)"

    def date_label(self) -> str:
        import datetime
        try:
            return datetime.datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return "Unknown date"


@dataclass
class DuplicateGroup:
    originals: List[ImageRecord]
    previews: List[ImageRecord] = field(default_factory=list)
    is_series: bool = False
    is_ambiguous: bool = False   # uncertain matches — not moved, flagged for review
    group_id: str = ""           # e.g., "g0001"


# ── histogram computation ─────────────────────────────────────────────────────

def _compute_histogram(img: Image.Image) -> list[float]:
    """
    Compute a 96-value normalized histogram (32 bins x 3 RGB channels).
    Returns values in [0.0, 1.0] range (per-channel sum = 1.0).

    Vectorized via numpy: ~6× faster than the equivalent pure-Python loop for
    the 768-element input that PIL.Image.histogram() produces.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")

    raw = img.histogram()   # 768 values: 256 per channel
    # Reshape to (3, 256), normalize each channel to sum=1, then bin 256→32
    arr = _np.array(raw, dtype=_np.float32).reshape(3, 256)
    totals = arr.sum(axis=1, keepdims=True).clip(min=1)
    arr /= totals
    # Downsample 256 bins → 32 bins (sum 8 adjacent bins per bucket)
    binned = arr.reshape(3, 32, 8).sum(axis=2)
    return binned.ravel().tolist()


def _compute_brightness(img: Image.Image) -> float:
    """Return mean pixel brightness 0.0-255.0."""
    gray = img if img.mode == "L" else img.convert("L")
    return float(_np.asarray(gray, dtype=_np.float32).mean())


# ── thread-count resolution ──────────────────────────────────────────────────

def _resolve_thread_count(settings: Settings, folder: Path) -> int:
    """Determine hashing thread count from settings + drive type of *folder*.

    Priority:
      1. ``settings.scan_threads > 0`` → user's explicit override (respected as-is).
      2. ``settings.io_parallelism == "ssd"`` → full CPU parallelism (for NVMe / SATA SSD).
      3. ``settings.io_parallelism == "hdd"`` → single reader (for HDD / network drives).
      4. ``settings.io_parallelism == "auto"`` (default) → probe the drive type
         and cap at ``settings.hdd_thread_cap`` (default 2) when the drive is
         ``fixed`` / ``removable`` / ``network`` / ``unknown``.

    Why ``fixed`` is treated as possibly-HDD: on Windows, both HDDs and SSDs
    report as ``fixed`` via ``GetDriveTypeW`` — there is no reliable cheap
    detection.  The cap of 2 threads still saturates an SSD for pHash work
    (which is CPU-bound after pre-downscaling) while completely eliminating
    HDD seek-thrash and thermal load.  Users with SSD can override by setting
    ``io_parallelism="ssd"`` in Settings or by setting ``scan_threads`` to
    their preferred value.
    """
    import os as _os
    cpu_n = max(1, (_os.cpu_count() or 1))

    explicit = getattr(settings, "scan_threads", 0) or 0
    if explicit > 0:
        return min(explicit, cpu_n * 2)  # cap against insane values

    mode = getattr(settings, "io_parallelism", "auto")
    hdd_cap = max(1, int(getattr(settings, "hdd_thread_cap", _HDD_THREAD_CAP)))

    if mode == "ssd":
        return cpu_n
    if mode == "hdd":
        return 1

    # "auto" — probe drive type
    try:
        from library import get_drive_info
        dt = get_drive_info(folder).drive_type
    except Exception:
        dt = "unknown"

    # Spinning / networked / unknown → conservative cap.
    # NVMe/SATA SSDs still return "fixed" on Windows; we prefer the safe default.
    if dt in ("fixed", "removable", "network", "unknown", "cdrom"):
        return min(cpu_n, hdd_cap)
    return cpu_n


# ── collection ───────────────────────────────────────────────────────────────

def collect_images(
    folder: Path,
    skip_paths: set[Path],
    settings: Settings,
    progress_cb: Optional[ProgressCb] = None,
    stop_flag: Optional[list[bool]] = None,
    pause_flag: Optional[list[bool]] = None,
    failed_paths: Optional[list] = None,
    library_cache: "Optional[dict]" = None,
    trust_library: bool = False,
) -> List[ImageRecord]:
    """
    Walk folder, compute phash/dhash/histogram/brightness for every qualifying image.
    RAW files are either hashed (if use_rawpy) or stem-matched to their JPEG siblings.

    Args:
        library_cache:  Optional dict[str, FileRecord] keyed by resolved absolute path
                        string.  When supplied, cache hits avoid PIL/imagehash entirely.
                        After the scan the dict is updated in-place with every freshly
                        hashed record so subsequent scans benefit from the new entries.
        trust_library:  When True, cached entries are used without checking file
                        modification time / size (useful for "skip change check" mode).
                        When False (default) each cached entry is validated via
                        FileRecord.is_stale() before being trusted.
    """
    skip_names_set = {s.strip() for s in settings.skip_names.split(",") if s.strip()}

    # ── discovery ──────────────────────────────────────────────────────────

    all_image_paths: list[Path] = []
    raw_by_stem: dict[tuple[Path, str], Path] = {}  # (folder, stem) -> raw path

    if settings.recursive:
        for root, dirs, files in os.walk(folder):
            root_path = Path(root).resolve()
            dirs[:] = [
                d for d in dirs
                if (root_path / d).resolve() not in skip_paths
                and d not in skip_names_set
            ]
            for fname in files:
                fpath = Path(root) / fname
                ext = fpath.suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    all_image_paths.append(fpath)
                elif ext in RAW_EXTENSIONS:
                    raw_by_stem[(Path(root).resolve(), fpath.stem.lower())] = fpath
    else:
        for fname in os.listdir(folder):
            fpath = folder / fname
            if not fpath.is_file():
                continue
            ext = fpath.suffix.lower()
            if ext in IMAGE_EXTENSIONS:
                all_image_paths.append(fpath)
            elif ext in RAW_EXTENSIONS:
                raw_by_stem[(folder.resolve(), fpath.stem.lower())] = fpath

    # If use_rawpy, also add RAW paths to process queue
    rawpy_available = False
    if settings.use_rawpy:
        try:
            import rawpy  # type: ignore  # noqa: F401
            rawpy_available = True
        except ImportError:
            rawpy_available = False

    if settings.use_rawpy and rawpy_available:
        for (_, _), raw_path in raw_by_stem.items():
            all_image_paths.append(raw_path)

    total = len(all_image_paths)
    records: list[ImageRecord] = []
    # Build stem -> record map for RAW companion matching
    stem_to_record: dict[tuple[Path, str], ImageRecord] = {}

    n_threads = _resolve_thread_count(settings, folder)

    # Pre-resolve every path once in the main thread so worker threads never call
    # path.resolve() (which on Windows invokes GetFinalPathNameByHandleW — a
    # syscall per file) under thread-pool contention.  For N=10,000 files this
    # saves several seconds of cumulative syscall overhead; for N=100k it can
    # save 30–60 s.
    resolved_strs: "list[str]" = [str(p.resolve()) for p in all_image_paths]
    # Derive parent Paths from resolved strings — string slicing, no syscall.
    parent_resolved: "list[Path]" = [Path(s).parent for s in resolved_strs]

    # Tracks (resolved_str, ImageRecord) pairs that were freshly hashed (not from
    # cache) so we can write them back into library_cache after collection.
    _freshly_hashed: "list[tuple[str, ImageRecord]]" = []

    def _hash_one(args: tuple) -> "tuple[int, Path, Optional[ImageRecord], Optional[Exception], bool]":
        """Hash worker.

        Checks stop_flag at entry so cancelled-but-queued futures exit almost
        immediately without touching the disk.  In-flight tasks already past
        the check finish their current file (at most ``n_threads`` files).
        """
        i, path = args

        # ── early-exit on stop/pause (fast path — don't even stat the file) ──
        if (stop_flag and stop_flag[0]) or (pause_flag and pause_flag[0]):
            return i, path, None, None, False

        # ── cache lookup ──────────────────────────────────────────────────
        # Use the pre-resolved string (computed once in the main thread) instead
        # of calling path.resolve() here — eliminates a syscall per worker call.
        if library_cache is not None:
            cached = library_cache.get(resolved_strs[i])
            if cached is not None:
                # For RAW files, invalidate cache entries that were hashed with
                # a different raw_use_embedded_thumb setting to prevent stale
                # hashes after the user toggles the fast-path option.
                _ext = path.suffix.lower()
                if _ext in RAW_EXTENSIONS:
                    _expected_mode = "embedded" if (settings.use_rawpy and rawpy_available and getattr(settings, "raw_use_embedded_thumb", False)) else ""
                    if cached.hash_mode != _expected_mode:
                        cached = None
                # Rotation hashes are unconditional in the current hasher; an
                # empty phash_r90 means the entry was written by a pre-rotation
                # code version.  Treat as stale so rotated duplicates aren't
                # silently missed after the user upgrades.  Respects
                # trust_library: when the user has explicitly said "trust this
                # cache, skip freshness checks", we honor it.  ``getattr`` keeps
                # us safe against fake cache stubs used in tests.
                if (
                    cached is not None
                    and not trust_library
                    and not getattr(cached, "phash_r90", None)
                ):
                    cached = None
            if cached is not None and (trust_library or not cached.is_stale(path)):
                try:
                    return i, path, cached.to_image_record(), None, True
                except Exception:
                    pass  # corrupted cache entry → fall through to fresh hash

        # Check again after cache lookup — the stat() in is_stale() can be slow
        if (stop_flag and stop_flag[0]) or (pause_flag and pause_flag[0]):
            return i, path, None, None, False

        # ── fresh hash (with single retry for stale handles after sleep) ──
        try:
            ext = path.suffix.lower()
            if ext in RAW_EXTENSIONS and settings.use_rawpy and rawpy_available:
                rec = _hash_raw(path, settings)
            else:
                rec = _hash_image(path, settings)
            return i, path, rec, None, False
        except (OSError, IOError):
            # File handle may be stale after system sleep — retry once
            try:
                ext = path.suffix.lower()
                if ext in RAW_EXTENSIONS and settings.use_rawpy and rawpy_available:
                    rec = _hash_raw(path, settings)
                else:
                    rec = _hash_image(path, settings)
                return i, path, rec, None, False
            except Exception as exc2:
                return i, path, None, exc2, False
        except Exception as exc:
            return i, path, None, exc, False

    if n_threads > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        futures = {}
        completed = 0
        pool = ThreadPoolExecutor(max_workers=n_threads)
        try:
            for i, path in enumerate(all_image_paths):
                if stop_flag and stop_flag[0]:
                    break
                if pause_flag and pause_flag[0]:
                    break
                futures[pool.submit(_hash_one, (i, path))] = i

            ordered: dict[int, Optional[ImageRecord]] = {}
            ordered_cache_hit: dict[int, bool] = {}
            _interrupted = False
            # Rate-limit progress callbacks: emit at most 20 updates/sec.
            # Without this, a warm library cache can produce 10,000+ callbacks/sec
            # which — in the old root.after(0, ...) pattern — hammered the Tcl
            # lock.  Even with the new poll-ticker, limiting to 20/sec keeps the
            # main thread free for UI events.
            _last_cb: float = 0.0
            _CB_INTERVAL: float = 0.05  # seconds between progress updates
            for fut in as_completed(futures):
                if stop_flag and stop_flag[0]:
                    _interrupted = True
                    break
                if pause_flag and pause_flag[0]:
                    _interrupted = True
                    break
                try:
                    idx, path, rec, exc, was_cache_hit = fut.result()
                except Exception:
                    completed += 1
                    continue
                completed += 1
                if progress_cb:
                    _now = _time.monotonic()
                    if _now - _last_cb >= _CB_INTERVAL or completed == total:
                        progress_cb(f"Hashing {path.name}", completed, total, "Hashing")
                        _last_cb = _now
                if exc is not None:
                    if failed_paths is not None:
                        failed_paths.append(path)
                elif rec is not None:
                    ordered[idx] = rec
                    ordered_cache_hit[idx] = was_cache_hit

            if _interrupted:
                # Cancel queued futures immediately — do NOT wait for in-flight
                # to drain (in-flight tasks check the flag at entry and return
                # quickly anyway, so wait=True below is near-instant).
                pool.shutdown(wait=False, cancel_futures=True)
        finally:
            # Idempotent — drains the (now empty or fast-exiting) queue.
            pool.shutdown(wait=True)

        for idx in sorted(ordered):
            rec = ordered[idx]
            if rec is None:
                continue
            records.append(rec)
            stem_key = (parent_resolved[idx], all_image_paths[idx].stem.lower())
            stem_to_record[stem_key] = rec
            if not ordered_cache_hit.get(idx, True):
                _freshly_hashed.append((resolved_strs[idx], rec))
    else:
        _last_cb_seq: float = 0.0
        _CB_INTERVAL_SEQ: float = 0.05
        for i, path in enumerate(all_image_paths):
            if stop_flag and stop_flag[0]:
                break
            if pause_flag and pause_flag[0]:
                break

            if progress_cb:
                _now_seq = _time.monotonic()
                if _now_seq - _last_cb_seq >= _CB_INTERVAL_SEQ or i + 1 == total:
                    progress_cb(f"Hashing {path.name}", i + 1, total, "Hashing")
                    _last_cb_seq = _now_seq

            try:
                rec = None
                was_cache_hit = False

                # ── cache lookup (sequential) ─────────────────────────────
                if library_cache is not None:
                    cached = library_cache.get(resolved_strs[i])
                    if cached is not None:
                        _ext = path.suffix.lower()
                        if _ext in RAW_EXTENSIONS:
                            _expected_mode = "embedded" if (settings.use_rawpy and rawpy_available and getattr(settings, "raw_use_embedded_thumb", False)) else ""
                            if cached.hash_mode != _expected_mode:
                                cached = None
                        if (
                            cached is not None
                            and not trust_library
                            and not getattr(cached, "phash_r90", None)
                        ):
                            cached = None
                    if cached is not None and (trust_library or not cached.is_stale(path)):
                        try:
                            rec = cached.to_image_record()
                            was_cache_hit = True
                        except Exception:
                            rec = None  # fall through to fresh hash

                # ── fresh hash ────────────────────────────────────────────
                if rec is None:
                    ext = path.suffix.lower()
                    if ext in RAW_EXTENSIONS and settings.use_rawpy and rawpy_available:
                        rec = _hash_raw(path, settings)
                    else:
                        rec = _hash_image(path, settings)

                if rec is None:
                    continue

                records.append(rec)
                stem_key = (parent_resolved[i], path.stem.lower())
                stem_to_record[stem_key] = rec
                if not was_cache_hit:
                    _freshly_hashed.append((resolved_strs[i], rec))

            except Exception as exc:
                if progress_cb:
                    progress_cb(f"  Skip {path.name}: {exc}", i + 1, total, "Hashing")
                if failed_paths is not None:
                    failed_paths.append(path)

    # ── attach RAW companions (stem matching) ────────────────────────────

    if not (settings.use_rawpy and rawpy_available):
        for (folder_path, stem), raw_path in raw_by_stem.items():
            key = (folder_path, stem)
            if key in stem_to_record:
                stem_to_record[key].companions.append(raw_path)

    # ── write freshly-hashed records back into library_cache ─────────────
    # This keeps the caller's cache dict up-to-date so the next scan can
    # benefit from the new entries without needing a separate save step.

    if library_cache is not None and _freshly_hashed:
        try:
            from library import FileRecord as _FileRecord
            for fh_resolved, fh_rec in _freshly_hashed:
                try:
                    st_mtime = Path(fh_resolved).stat().st_mtime
                    _fh_ext = Path(fh_resolved).suffix.lower()
                    _fh_mode = ""
                    if _fh_ext in RAW_EXTENSIONS and settings.use_rawpy and rawpy_available:
                        _fh_mode = "embedded" if getattr(settings, "raw_use_embedded_thumb", False) else ""
                    library_cache[fh_resolved] = _FileRecord.from_image_record(
                        fh_rec, st_mtime=st_mtime, hash_mode=_fh_mode
                    )
                except Exception:
                    pass  # best-effort; don't let a stat failure abort the scan
        except ImportError:
            pass  # library module not available — cache write-back skipped

    return records


def _downscaled_for_hashing(img: "Image.Image") -> "Image.Image":
    """Return *img* downscaled in-place to at most ``_HASH_WORKING_SIZE`` pixels
    on its longest side (preserving aspect ratio), or the original unchanged if
    it already fits within that size.

    imagehash.phash will further downscale this to 32×32 via LANCZOS.  Starting
    from 1024-max instead of the full-res image saves ~40× on rotation hashes
    with ≤ 1-bit drift on the final 64-bit pHash (well below any threshold).

    IMPORTANT: this mutates *img* when a resize is needed.  Callers must capture
    width/height and any other data from *img* BEFORE calling this function and
    must not use the original image object for anything else afterwards.  Both
    ``_hash_image`` and ``_hash_raw`` already follow this contract.
    """
    w, h = img.size
    if max(w, h) <= _HASH_WORKING_SIZE:
        return img
    # thumbnail() operates in-place — no copy needed; saves ~6 MB per thread
    # on 24 MP images and eliminates one full decompressed-buffer allocation.
    img.thumbnail((_HASH_WORKING_SIZE, _HASH_WORKING_SIZE), Image.LANCZOS)
    return img


def _hash_image(path: Path, settings: Settings) -> Optional[ImageRecord]:
    """Open a regular image and compute all hash/feature data.

    Performance notes:
      • JPEG draft mode: ``img.draft("RGB", (W, H))`` is called immediately
        after ``Image.open()`` (before any pixel access).  For a 24 MP JPEG
        libjpeg will decode at 1/8 scale, reducing decode time by ~4-8× and
        memory by ~64×.  The downscaled image is visually identical to the
        full-res source for pHash purposes.
      • The image is pre-downscaled to ``_HASH_WORKING_SIZE`` before any hash
        computation — full-res 24 MP buffers are never held in RAM, and the
        three rotation hashes cost ~40× less CPU than before.
      • EXIF metadata count is read from the already-open PIL image (no second
        file open via count_metadata_fields(path) as in earlier versions).
    """
    with Image.open(path) as img:
        # Capture the declared file dimensions BEFORE draft mode is applied.
        # img.size after Image.open() but before img.load() reads from the
        # file header and gives the *true* source dimensions.  After draft()
        # and load() img.size returns the smaller decoded dimensions (e.g.
        # 3000×2000 for a 6000×4000 JPEG decoded at 1/2 scale), which would
        # make the JPEG appear as a preview-sized copy of its NEF companion.
        file_w, file_h = img.size

        # JPEG draft mode — must be set BEFORE the first pixel access.
        # exif_transpose() triggers img.load() internally, so draft must
        # come first.  Only effective for JPEG; silently ignored by other formats.
        if getattr(img, "format", None) == "JPEG":
            img.draft("RGB", (_HASH_WORKING_SIZE, _HASH_WORKING_SIZE))

        # EXIF metadata count and capture date — read BEFORE exif_transpose, which
        # strips the orientation tag.  EXIF is parsed from the file header during
        # open() without triggering pixel load, so this is safe after draft().
        metadata_count = 0
        if settings.collect_metadata:
            from metadata import count_metadata_fields_from_img
            metadata_count = count_metadata_fields_from_img(img)

        # Always extract EXIF date (needed for cross-format date guard regardless
        # of collect_metadata setting).  Falls back to None for non-EXIF formats.
        exif_date = extract_date_from_exif_from_img(img)

        img = ImageOps.exif_transpose(img)
        img.load()

        if img.mode not in ("RGB", "RGBA", "L", "P"):
            img = img.convert("RGB")
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        draft_w, draft_h = img.size

        # Recover original dimensions from the pre-draft file size, correcting
        # for any EXIF rotation that exif_transpose() may have applied.
        # Strategy: compare aspect ratios to detect whether w and h were swapped.
        #   • If scale_x ≈ scale_y  → no rotation (or 180°) — use file_w, file_h.
        #   • If scale_x ≈ draft_w/file_h → 90°/270° rotation — swap file dims.
        if file_w > 0 and file_h > 0 and draft_w > 0 and draft_h > 0:
            err_normal  = abs(draft_h / file_h  - draft_w / file_w)
            err_rotated = abs(draft_h / file_w  - draft_w / file_h)
            if err_rotated < err_normal:
                w, h = file_h, file_w   # exif_transpose swapped width↔height
            else:
                w, h = file_w, file_h   # no swap (0° or 180°)
        else:
            w, h = draft_w, draft_h     # fallback for zero-dimension edge cases

        if settings.min_dimension > 0 and max(w, h) < settings.min_dimension:
            return None

        rgb = img if img.mode == "RGB" else img.convert("RGB")
        # Pre-downscale once; reuse for all pHash/dHash/histogram/brightness/rotation work.
        work = _downscaled_for_hashing(rgb)

        ph = imagehash.phash(work)
        dh = imagehash.dhash(work) if settings.use_dual_hash else imagehash.ImageHash(_np.zeros((8, 8), dtype=bool))
        hist = _compute_histogram(work) if settings.use_histogram else []
        brightness = _compute_brightness(work) if settings.dark_protection else 128.0
        # Rotation hashes — rotate the downscaled working image (90°/180°/270°
        # are exact axis flips, no interpolation needed).
        ph_r90  = imagehash.phash(work.rotate(90,  expand=True))
        ph_r180 = imagehash.phash(work.rotate(180, expand=True))
        ph_r270 = imagehash.phash(work.rotate(270, expand=True))

    stat = path.stat()
    mtime = min(stat.st_mtime, stat.st_ctime)

    return ImageRecord(
        path=path, width=w, height=h,
        file_size=stat.st_size,
        phash=ph, dhash=dh,
        mtime=mtime,
        brightness=brightness,
        histogram=hist,
        metadata_count=metadata_count,
        phash_r90=ph_r90, phash_r180=ph_r180, phash_r270=ph_r270,
        exif_date=exif_date,
    )


def _hash_raw(path: Path, settings: Settings) -> Optional[ImageRecord]:
    """Decode a RAW file with rawpy and compute hashes.

    Fast path: try ``raw.extract_thumb()`` to get the camera's embedded JPEG
    preview — typically ~1620×1080 and decodes in a few ms.  This is ~30-80×
    faster than ``raw.postprocess()`` (which demosaics the full sensor).

    The reported width/height always come from ``raw.sizes`` (full sensor
    dimensions) so cross-format RAW+JPEG dimension bucketing continues to
    work regardless of which decode path was used.

    Fallback: if the RAW has no embedded thumb (rare for Canon/Nikon/Sony,
    more common for older cameras) or extract_thumb() errors, we fall back
    to ``raw.postprocess()`` as before.
    """
    import rawpy  # type: ignore
    import io
    from PIL import Image as PILImage

    img: "Optional[Image.Image]" = None
    full_w = 0
    full_h = 0

    with rawpy.imread(str(path)) as raw:
        # Full-sensor dimensions — reported regardless of thumb vs postprocess path
        try:
            sizes = raw.sizes
            full_w, full_h = int(sizes.width), int(sizes.height)
        except Exception:
            full_w, full_h = 0, 0

        if getattr(settings, "raw_use_embedded_thumb", True):
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = PILImage.open(io.BytesIO(thumb.data))
                    img.load()
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img = PILImage.fromarray(thumb.data)
            except Exception:
                img = None  # fall through to postprocess

        if img is None:
            rgb_array = raw.postprocess(use_camera_wb=True, output_bps=8)
            img = PILImage.fromarray(rgb_array)
            if full_w == 0 or full_h == 0:
                full_w, full_h = img.size

    # If sizes were unavailable, fall back to the decoded image's own dims
    if full_w == 0 or full_h == 0:
        full_w, full_h = img.size

    # Extract EXIF date BEFORE mode conversion: .convert("RGB") strips metadata.
    # For the embedded-JPEG-thumb path, img is a PIL JPEG with a full EXIF block
    # including DateTimeOriginal.  For the postprocess path, img comes from a
    # numpy array (no EXIF) so attempt 1 returns None and we fall back to a
    # direct PIL open of the RAW file which may expose EXIF via a TIFF reader.
    raw_exif_date = extract_date_from_exif_from_img(img)
    if raw_exif_date is None:
        raw_exif_date = extract_date_from_exif(path)

    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    if img.mode == "RGBA":
        img = img.convert("RGB")

    if settings.min_dimension > 0 and max(full_w, full_h) < settings.min_dimension:
        return None

    # Pre-downscale once for all hash/feature work.
    work = _downscaled_for_hashing(img)

    ph = imagehash.phash(work)
    dh = imagehash.dhash(work)
    hist = _compute_histogram(work)
    brightness = _compute_brightness(work)
    ph_r90  = imagehash.phash(work.rotate(90,  expand=True))
    ph_r180 = imagehash.phash(work.rotate(180, expand=True))
    ph_r270 = imagehash.phash(work.rotate(270, expand=True))

    stat = path.stat()
    mtime = min(stat.st_mtime, stat.st_ctime)

    return ImageRecord(
        path=path, width=full_w, height=full_h,
        file_size=stat.st_size,
        phash=ph, dhash=dh,
        mtime=mtime,
        brightness=brightness,
        histogram=hist,
        metadata_count=0,
        phash_r90=ph_r90, phash_r180=ph_r180, phash_r270=ph_r270,
        exif_date=raw_exif_date,
    )


# ── similarity guards ─────────────────────────────────────────────────────────

def _same_dimensions(a: ImageRecord, b: ImageRecord, tol_pct: float) -> bool:
    """True if width and height of a and b are within tol_pct of each other."""
    if not (a.width and a.height and b.width and b.height):
        return False
    tol = tol_pct / 100.0
    return (abs(a.width  - b.width)  / max(a.width,  b.width)  <= tol and
            abs(a.height - b.height) / max(a.height, b.height) <= tol)


def _can_be_similar(a: ImageRecord, b: ImageRecord, settings: Settings) -> bool:
    """
    Apply all layered similarity guards. ALL must pass for the images to be considered
    visually similar. Returns True only if every guard passes.

    Same-dimension pairs (potential series/edited shots) use a higher pHash threshold
    so JPEG quality variation doesn't prevent them from being grouped — _classify_group
    will then promote them all to originals via series detection.

    Cross-format pairs (RAW vs JPEG) use a more lenient threshold because rawpy
    demosaicing produces a slightly different rendering than the camera's own JPEG
    processor — typical pHash distance is 3-8 bits even for the same shot.
    """
    # 1. Aspect ratio guard (valid for all pairs including cross-format).
    #    Also accept the reciprocal AR so that 90°/270° rotated copies pass
    #    (portrait vs landscape versions of the same image).
    ar_a = a.width / a.height if a.height else 1.0
    ar_b = b.width / b.height if b.height else 1.0
    ar_tol = settings.ar_tolerance_pct / 100
    ar_diff_normal  = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001)
    ar_diff_rotated = abs(ar_a - 1.0 / ar_b) / max(ar_a, 1.0 / ar_b, 0.001) if ar_b else 1.0
    if min(ar_diff_normal, ar_diff_rotated) > ar_tol:
        return False

    # Detect cross-format pairs (RAW vs non-RAW or vice versa).
    # RAW demosaicing differs from camera JPEG rendering, so we relax several
    # guards by the cross_format_threshold_factor (default 2.0).
    a_is_raw = a.path.suffix.lower() in RAW_EXTENSIONS
    b_is_raw = b.path.suffix.lower() in RAW_EXTENSIONS
    cross_format = a_is_raw != b_is_raw
    cf_factor = (
        getattr(settings, "cross_format_threshold_factor", 2.0)
        if cross_format else 1.0
    )

    # Cross-format EXIF date guard.
    # Same-shot RAW+JPEG pairs share identical DateTimeOriginal timestamps.
    # Photos of the same scene taken on different days (or different sessions)
    # can still pass the lenient cross-format pHash threshold (12 bits) even
    # though they are genuinely different images — e.g., a recurring landscape
    # shot at the same location.  Reject any cross-format pair whose EXIF dates
    # differ by more than 5 minutes: a genuine RAW+JPEG pair shot by the same
    # camera will always have timestamps within a few seconds of each other.
    if cross_format:
        a_date = getattr(a, "exif_date", None)
        b_date = getattr(b, "exif_date", None)
        if a_date is not None and b_date is not None:
            if abs((a_date - b_date).total_seconds()) > 300:
                return False

    # 2. Brightness guard — relax for cross-format: RAW white balance differs from JPEG
    if abs(a.brightness - b.brightness) > settings.brightness_max_diff * cf_factor:
        return False

    # 3. pHash — use a more lenient threshold for same-dimension pairs so that
    #    burst/series shots (which may differ by quality) still get grouped.
    #    _classify_group will keep all same-size members as originals.
    same_dims = _same_dimensions(a, b, settings.series_tolerance_pct)
    if same_dims:
        eff_threshold = int(settings.threshold * settings.series_threshold_factor)
    else:
        eff_threshold = settings.threshold

    # Cross-format pairs get an additional threshold relaxation on top of series scaling.
    #
    # IMPORTANT: the effective cross-format threshold is a FIXED physical constant
    # calibrated on Canon EOS M100 CR2 vs camera JPEG pairs (max intra-group pHash=12,
    # min inter-group pHash=20 → 8-bit safety gap).  It must NOT scale with the user's
    # current ``threshold`` setting, because at threshold=4 the naive ``threshold *
    # cf_factor = 4 * 6 = 24`` would exceed the inter-group gap (20 bits) and cause
    # unrelated landscape shots to chain together via single-linkage union-find.
    #
    # Formula: cf_abs_threshold = _CF_BASE_THRESHOLD * cf_factor  (always uses base=2)
    # Default: 2 * 6.0 = 12 bits — covers all true RAW+JPEG pairs, stays below gap.
    if cross_format:
        cf_abs_threshold = int(_CF_BASE_THRESHOLD * cf_factor)
        eff_threshold = max(eff_threshold, cf_abs_threshold)

    if settings.dark_protection:
        if a.brightness < settings.dark_threshold or b.brightness < settings.dark_threshold:
            eff_threshold = max(1, int(eff_threshold * settings.dark_tighten_factor))

    # Rotation-aware pHash: check all orientations of both images.
    # We check b's rotations vs a's upright hash AND a's rotations vs b's upright
    # hash, so it doesn't matter which image is the "rotated" one.
    dist_normal = a.phash - b.phash
    # b's rotations vs a's upright hash
    dist_r90    = (a.phash - b.phash_r90)  if b.phash_r90  is not None else dist_normal
    dist_r180   = (a.phash - b.phash_r180) if b.phash_r180 is not None else dist_normal
    dist_r270   = (a.phash - b.phash_r270) if b.phash_r270 is not None else dist_normal
    # a's rotations vs b's upright hash (symmetric — needed when a is the rotated copy)
    dist_ar90   = (a.phash_r90  - b.phash) if a.phash_r90  is not None else dist_normal
    dist_ar180  = (a.phash_r180 - b.phash) if a.phash_r180 is not None else dist_normal
    dist_ar270  = (a.phash_r270 - b.phash) if a.phash_r270 is not None else dist_normal
    phash_dist  = min(dist_normal, dist_r90, dist_r180, dist_r270,
                      dist_ar90, dist_ar180, dist_ar270)
    is_rotated  = phash_dist < dist_normal   # True when a rotation gives a closer match

    # JPEG DCT re-encoding at a different orientation introduces up to ~6 bits of
    # pHash drift even for pixel-identical content.  Apply a rotation-lenient
    # threshold floor so that 90°/180°/270°-rotated JPEG duplicates are not
    # filtered out.  rotation_threshold_factor=3.0 (default) → floor = 2×3 = 6 bits.
    #
    # IMPORTANT: the floor is an absolute bit count, not a multiple of the current
    # threshold.  Using settings.threshold × factor would explode during calibration
    # (which sweeps threshold up to 20), making the floor 60 bits and causing all
    # same-dimension pairs to look like rotation duplicates (O(n²) performance hit).
    # The JPEG re-encoding drift is a fixed physical property (~6 bits), independent
    # of whatever threshold the user or calibrator has chosen.
    if is_rotated:
        _base = 2  # default threshold — rotation drift is relative to this, not the sweep value
        rotation_floor = int(_base * getattr(settings, "rotation_threshold_factor", 3.0))
        eff_threshold = max(eff_threshold, rotation_floor)

    if phash_dist > eff_threshold:
        return False

    # 4. dHash — also scale for same-dimension pairs.
    # Skipped for cross-format pairs (rawpy demosaicing produces very different
    # directional gradients vs the camera JPEG engine).
    # Skipped for rotation matches: dHash is directional and not rotation-invariant —
    # a 90°-rotated copy will always fail a naive dHash check.
    # Also skipped when pHash == 0: a definitive identity signal; dHash would only add
    # false negatives due to JPEG quality / denoising differences.
    if settings.use_dual_hash and not cross_format and not is_rotated and dist_normal > 0:
        dhash_thr = eff_threshold * 1.5
        if a.dhash - b.dhash > dhash_thr:
            return False

    # 5. Histogram intersection.
    #    Cross-format pairs (RAW vs JPEG) use a relaxed floor instead of the full
    #    hist_min_similarity check.  Same-shot RAW vs camera JPEG typically achieves
    #    histogram similarity ≥ 0.40 despite different colour rendering / tone curves;
    #    genuinely different images fall below 0.25.  Without this floor, the lenient
    #    cross-format pHash threshold (10 bits) can false-positive unrelated images.
    if settings.use_histogram and a.histogram and b.histogram:
        intersection = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3
        if cross_format:
            if intersection < _CROSS_FORMAT_HIST_FLOOR:
                return False
        elif intersection < settings.hist_min_similarity:
            return False

    return True


# ── grouping ──────────────────────────────────────────────────────────────────

def _split_oversized_bucket(
    indices: list[int],
    records: list[ImageRecord],
    settings: Settings,
    cap: int,
    stop_flag: Optional[list[bool]] = None,
) -> list[list[int]]:
    """Break a runaway union-find bucket into tight sub-groups by requiring
    every member to pair-match the bucket's medoid.

    Union-find is *single-linkage* — A joins B's component if *any* existing
    member is pHash-similar enough.  With many near-uniform images (blank
    screenshots, near-black photos, document scans) this chains unrelated
    photos into one mega-group via 1-bit pHash intermediates.  The chain
    breaker: pick the medoid (index with minimum total pHash distance to
    sampled peers) and demand every member pass the full ``_can_be_similar``
    guard against that medoid directly.  Rejected members are processed
    iteratively so a bucket that contains several distinct duplicate clusters
    still surfaces all of them.

    Called only for buckets whose size exceeds ``settings.max_group_size``.
    Small buckets keep their original transitive closure, which is the
    correct semantics for genuine dup groups.

    ``cap <= 0`` is treated as "feature disabled" — the bucket is returned
    unchanged regardless of its size.

    Implemented iteratively (work-queue) rather than recursively to avoid
    Python's call-stack limit.  In the worst case (every medoid matches only
    itself) the old recursive version needed O(n) stack frames — fatal for
    buckets > ~1 000 images.  The iterative version has O(1) stack depth.
    """
    if cap <= 0 or len(indices) <= cap:
        return [list(indices)]

    result:  list[list[int]] = []
    pending: list[list[int]] = [list(indices)]   # work queue

    while pending:
        # Honor Stop between rounds — a giant bucket can need many splits
        # and would otherwise ignore the Stop button for tens of seconds.
        if stop_flag and stop_flag[0]:
            # Return what we have so far so the caller can still surface
            # the partial result if it wants to.  find_groups discards it.
            return result + pending
        bucket = pending.pop()

        # Small enough — keep transitive closure as-is (genuine dup group).
        if len(bucket) <= cap:
            result.append(bucket)
            continue

        # Sample if huge — O(k) medoid computation for k=200 is plenty.
        if len(bucket) <= 200:
            sample: list[int] = bucket
        else:
            # Deterministic stride sample so the split is reproducible
            # across re-runs (pytest, cache replays, etc.).
            step = max(1, len(bucket) // 200)
            sample = [bucket[i] for i in range(0, len(bucket), step)][:200]

        def _total_dist(idx: int, _sample: list[int] = sample) -> int:
            h_i = records[idx].phash
            total = 0
            for j in _sample:
                if j != idx:
                    total += h_i - records[j].phash   # Hamming via ImageHash.__sub__
            return total

        medoid = min(sample, key=_total_dist)

        # Members that pass the FULL similarity guard against the medoid are
        # kept together; others are queued for another split round.
        kept:     list[int] = [medoid]
        rejected: list[int] = []
        med_rec = records[medoid]
        for idx in bucket:
            if idx == medoid:
                continue
            if _can_be_similar(med_rec, records[idx], settings):
                kept.append(idx)
            else:
                rejected.append(idx)

        # Publish the kept cluster if meaningful.
        if len(kept) >= 2:
            result.append(kept)

        # Queue the rejected remainder for another split round.
        # Termination guarantee: len(rejected) < len(bucket) always because
        # the medoid is always consumed into `kept`.  The work queue strictly
        # shrinks every iteration, so the loop terminates in ≤ n rounds.
        if len(rejected) >= 2:
            pending.append(rejected)
        # Single rejected items have no pair — silently drop (no group).

    return result


def _sort_key(r: ImageRecord, settings: Settings):
    """Return sort key so the *best* image sorts first (ascending).

    When keep_all_formats is disabled, RAW files are preferred as originals over
    their JPEG equivalents — they contain more data and are the true camera output.
    raw_priority=0 sorts RAW before JPEG; raw_priority=1 sorts JPEG last.
    """
    strategy = settings.keep_strategy

    # RAW preference: 0 = RAW (keep), 1 = non-RAW (may be trashed as duplicate)
    # Only applied when not keeping all formats — in keep_all_formats mode both
    # RAW and JPEG are already kept as separate originals.
    if not getattr(settings, "keep_all_formats", True):
        raw_priority = 0 if r.path.suffix.lower() in RAW_EXTENSIONS else 1
    else:
        raw_priority = 0  # no preference — format split is handled by _split_by_format

    if strategy == "oldest":
        # Try EXIF date first, then filename date, then mtime.
        # Use -file_size as tiebreaker so the larger (higher-quality) file wins
        # when two images share the same timestamp (e.g. burst or exact duplicates).
        if settings.sort_by_exif_date:
            dt = extract_date_from_exif(r.path)
            if dt is not None:
                return (raw_priority, dt.timestamp(), -r.file_size)
        if settings.sort_by_filename_date:
            dt = extract_date_from_filename(r.path.name)
            if dt is not None:
                return (raw_priority, dt.timestamp(), -r.file_size)
        return (raw_priority, r.mtime, -r.file_size)
    # pixels strategy: prefer more pixels, then larger file size as tiebreaker.
    # This ensures that between two same-resolution files (pHash=0 exact duplicates)
    # the less-compressed (larger file) copy is consistently chosen as the original.
    return (raw_priority, -r.pixels, -r.file_size)


def find_groups(
    records: list[ImageRecord],
    settings: Settings,
    progress_cb: Optional[ProgressCb] = None,
    stop_flag: Optional[list[bool]] = None,
    pause_flag: Optional[list[bool]] = None,
    resume_state=None,   # ScanState instance if resuming
) -> tuple[list[DuplicateGroup], Optional[object]]:
    """
    Group visually similar images, apply series detection, and classify originals vs previews.

    Returns (groups, partial_state_or_none).
    partial_state_or_none is a ScanState if paused mid-comparison, else None.
    """
    n = len(records)

    # Initialize union-find, possibly from resumed state
    if resume_state and resume_state.union_parent and len(resume_state.union_parent) == n:
        parent = list(resume_state.union_parent)
        start_i = resume_state.compare_i
    else:
        parent = list(range(n))
        start_i = 0

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Maximum effective threshold used across all pair types.
    # Series pairs use a multiplied threshold; cross-format RAW/JPEG pairs use
    # cross_format_threshold_factor; rotation pairs use rotation_threshold_factor.
    # We query the BK-tree with the widest radius and let _can_be_similar apply
    # the exact per-pair logic.
    _max_query_thr = max(
        settings.threshold,
        int(settings.threshold * getattr(settings, "series_threshold_factor",    1.0)),
        int(settings.threshold * getattr(settings, "cross_format_threshold_factor", 2.0)),
        int(2 * getattr(settings, "rotation_threshold_factor",  3.0)),  # fixed floor, not scaled
    )

    if n > _BKTREE_THRESHOLD:
        # ── BK-tree fast path ─────────────────────────────────────────────
        # Pre-compute integer hash values for all 4 orientations once.
        # Querying with all rotation hashes ensures rotated copies are found
        # even though the BK-tree is indexed only on the upright phash.
        hash_ints      = [int(str(r.phash), 16) for r in records]
        hash_ints_r90  = [
            int(str(r.phash_r90),  16) if r.phash_r90  is not None else hash_ints[i]
            for i, r in enumerate(records)
        ]
        hash_ints_r180 = [
            int(str(r.phash_r180), 16) if r.phash_r180 is not None else hash_ints[i]
            for i, r in enumerate(records)
        ]
        hash_ints_r270 = [
            int(str(r.phash_r270), 16) if r.phash_r270 is not None else hash_ints[i]
            for i, r in enumerate(records)
        ]

        bk = _BKTree()
        for i, h in enumerate(hash_ints):
            bk.insert(h, i)

        for i in range(start_i, n):
            if stop_flag and stop_flag[0]:
                return [], None
            if pause_flag and pause_flag[0]:
                from scan_state import ScanState, serialize_record
                partial = ScanState(
                    phase="comparing",
                    records=[serialize_record(r) for r in records],
                    compare_i=i,
                    union_parent=list(parent),
                )
                return [], partial

            if progress_cb and i % 200 == 0:
                progress_cb(
                    f"Comparing {i + 1:,} / {n:,}…",
                    i, n, "Comparing",
                )

            # Query with all 4 rotations to catch rotation-equivalent duplicates
            candidates: set[int] = set(bk.query(hash_ints[i],      _max_query_thr))
            candidates.update(    bk.query(hash_ints_r90[i],  _max_query_thr))
            candidates.update(    bk.query(hash_ints_r180[i], _max_query_thr))
            candidates.update(    bk.query(hash_ints_r270[i], _max_query_thr))
            for j in candidates:
                if j <= i:
                    continue
                if _can_be_similar(records[i], records[j], settings):
                    union(i, j)

        if progress_cb:
            progress_cb("Comparing complete.", n, n, "Comparing")

    else:
        # ── O(n²) brute-force for small collections / resumed scans ───────
        total_comparisons = n * (n - 1) // 2
        done_comparisons  = (
            start_i * (n - start_i) + start_i * (start_i - 1) // 2
            if start_i else 0
        )

        for i in range(start_i, n):
            if stop_flag and stop_flag[0]:
                return [], None
            if pause_flag and pause_flag[0]:
                from scan_state import ScanState, serialize_record
                partial = ScanState(
                    phase="comparing",
                    records=[serialize_record(r) for r in records],
                    compare_i=i,
                    union_parent=list(parent),
                )
                return [], partial

            if progress_cb and i % 10 == 0:
                progress_cb(
                    f"Comparing image {i + 1}/{n}…",
                    done_comparisons, total_comparisons, "Comparing",
                )

            for j in range(i + 1, n):
                done_comparisons += 1
                if _can_be_similar(records[i], records[j], settings):
                    union(i, j)

        if progress_cb:
            progress_cb("Comparing complete.", total_comparisons, total_comparisons, "Comparing")

    # ── bucket by union-find root ────────────────────────────────────────
    buckets: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        buckets[find(i)].append(i)

    groups: list[DuplicateGroup] = []
    group_counter = 0
    grouped_indices: set[int] = set()

    # Runaway-group safety net: when ``max_group_size`` is set, any bucket
    # that grew beyond that cap via union-find chaining is split by the
    # medoid rule.  Prevents a single mega-group of hundreds of unrelated
    # near-uniform images (blank docs, dark photos) from appearing in the
    # results.  Disabled when ``max_group_size`` <= 0.
    _cap = max(0, int(getattr(settings, "max_group_size", 0)))

    for indices in buckets.values():
        # Stop is honored between buckets so giant clusters don't tie up the
        # Stop button while their split + classify is in flight.
        if stop_flag and stop_flag[0]:
            return [], None
        if len(indices) < 2:
            continue

        # Split oversized buckets; otherwise pass the bucket through as-is.
        if _cap > 0 and len(indices) > _cap:
            sub_buckets = _split_oversized_bucket(
                indices, records, settings, _cap, stop_flag=stop_flag,
            )
        else:
            sub_buckets = [indices]

        for sub in sub_buckets:
            if len(sub) < 2:
                continue
            group_counter += 1
            members = [records[i] for i in sub]
            result_group = _classify_group(
                members, settings, f"g{group_counter:04d}", stop_flag=stop_flag,
            )
            if result_group is not None:
                groups.append(result_group)
                grouped_indices.update(sub)

    # ── Ambiguous detection ───────────────────────────────────────────────
    # Find image pairs whose pHash distance is within (threshold, threshold*factor].
    # These are "borderline" matches — not confidently duplicates, but not clearly
    # different either. We group them and flag as ambiguous for manual review.
    if settings.ambiguous_detection and settings.ambiguous_threshold_factor > 1.0:
        amb_threshold = settings.threshold * settings.ambiguous_threshold_factor
        # Only consider singletons not already in a regular group
        singletons = [i for i in range(n) if i not in grouped_indices]
        m = len(singletons)

        if m >= 2:
            amb_parent = list(range(m))

            def amb_find(x: int) -> int:
                while amb_parent[x] != x:
                    amb_parent[x] = amb_parent[amb_parent[x]]
                    x = amb_parent[x]
                return x

            def amb_union(x: int, y: int) -> None:
                px, py = amb_find(x), amb_find(y)
                if px != py:
                    amb_parent[px] = py

            def _amb_pair_ok(a: ImageRecord, b: ImageRecord) -> bool:
                ar_a = a.width / a.height if a.height else 1.0
                ar_b = b.width / b.height if b.height else 1.0
                if abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001) > settings.ar_tolerance_pct / 100:
                    return False
                if abs(a.brightness - b.brightness) > settings.brightness_max_diff:
                    return False
                pdist = a.phash - b.phash
                return settings.threshold < pdist <= amb_threshold

            if m > _BKTREE_THRESHOLD:
                # BK-tree fast path for ambiguous detection
                sing_hashes = [int(str(records[singletons[si]].phash), 16) for si in range(m)]
                amb_bk = _BKTree()
                for si, h in enumerate(sing_hashes):
                    amb_bk.insert(h, si)
                for si in range(m):
                    for sj in amb_bk.query(sing_hashes[si], int(amb_threshold)):
                        if sj <= si:
                            continue
                        if _amb_pair_ok(records[singletons[si]], records[singletons[sj]]):
                            amb_union(si, sj)
            else:
                for si in range(m):
                    for sj in range(si + 1, m):
                        if _amb_pair_ok(records[singletons[si]], records[singletons[sj]]):
                            amb_union(si, sj)

            amb_buckets: dict[int, list[int]] = defaultdict(list)
            for si in range(m):
                amb_buckets[amb_find(si)].append(si)

            for bucket_si in amb_buckets.values():
                if len(bucket_si) < 2:
                    continue
                group_counter += 1
                members = [records[singletons[si]] for si in bucket_si]
                gid = f"amb{group_counter:04d}"
                groups.append(DuplicateGroup(
                    originals=members,
                    previews=[],
                    is_series=False,
                    is_ambiguous=True,
                    group_id=gid,
                ))

    return groups, None


def _classify_group(
    members: list[ImageRecord],
    settings: Settings,
    group_id: str,
    stop_flag: Optional[list[bool]] = None,
) -> Optional[DuplicateGroup]:
    """
    Given a set of similar images, classify each as original or preview.

    Same-dimension handling distinguishes three cases:
      - Exact duplicates (pHash ≤ _EXACT_DUP_PHASH within the bucket): same image
        saved/exported twice.  Keep the best copy; trash the rest as duplicates.
      - Cross-format pairs (RAW vs JPEG of the same shot): behaviour depends on
        keep_all_formats.  When True (default), ALL members are kept as originals
        so the group disappears from the review list — nothing is trashable.
        When False, the RAW is the authoritative master (kept) and every non-RAW
        (JPEG/PNG) is marked as a duplicate (preview/trash).
      - Genuine series / burst (pHash > _EXACT_DUP_PHASH, same format): different
        shots at the same resolution.  Keep all; none are previews.

    A member is only a preview if BOTH dimensions are strictly smaller than the best.

    Returns None if no previews were found (nothing to trash).
    """
    members_sorted = sorted(members, key=lambda r: _sort_key(r, settings))
    global_best = members_sorted[0]

    # Precompute once per record so the O(n²) same-dimension loop below
    # doesn't repeat ``path.suffix.lower()`` ~n² times for large series
    # buckets (500-member burst → ~250k lower() calls saved).
    is_raw_flags = [r.path.suffix.lower() in RAW_EXTENSIONS for r in members_sorted]

    # ── series / exact-dup detection ─────────────────────────────────────
    series_indices:   set[int] = set()   # genuine burst shots — keep all
    exact_dup_indices: set[int] = set()  # exact same-image copies — trash extras
    is_series = False

    if not getattr(settings, "disable_series_detection", False):
        tol = settings.series_tolerance_pct / 100.0

        def _same_dim(i: int, j: int) -> bool:
            a = members_sorted[i]
            b = members_sorted[j]
            if a.width == 0 or a.height == 0 or b.width == 0 or b.height == 0:
                return False
            w_ratio = abs(a.width - b.width) / max(a.width, b.width)
            h_ratio = abs(a.height - b.height) / max(a.height, b.height)
            # For cross-format pairs (RAW vs JPEG), use a wider tolerance:
            # rawpy decodes the full sensor area, adding a few border pixels
            # vs the camera JPEG crop (e.g. 6024×4020 vs 6000×4000 = 0.4%).
            effective_tol = (
                max(tol, _CROSS_FORMAT_DIM_TOL)
                if is_raw_flags[i] != is_raw_flags[j] else tol
            )
            if w_ratio <= effective_tol and h_ratio <= effective_tol:
                return True
            # Also check swapped (portrait RAW vs landscape JPEG or vice versa):
            # rawpy may decode a CR2 in portrait orientation (height > width) while
            # the matching camera JPEG is stored as landscape (width > height).
            # e.g. CR2 4020×6024 vs JPEG 6000×4000 — same sensor, axes transposed.
            w_ratio_rot = abs(a.width - b.height) / max(a.width, b.height)
            h_ratio_rot = abs(a.height - b.width) / max(a.height, b.width)
            return w_ratio_rot <= effective_tol and h_ratio_rot <= effective_tol

        # Build dimension buckets (union-find for transitivity)
        dim_parent = list(range(len(members_sorted)))

        def dim_find(x: int) -> int:
            while dim_parent[x] != x:
                dim_parent[x] = dim_parent[dim_parent[x]]
                x = dim_parent[x]
            return x

        def dim_union(x: int, y: int) -> None:
            px, py = dim_find(x), dim_find(y)
            if px != py:
                dim_parent[px] = py

        for i in range(len(members_sorted)):
            # Cheap periodic stop check for very large groups (5000+ members
            # would otherwise tie up the Stop button for several seconds).
            if stop_flag and stop_flag[0] and (i & 0x3F) == 0:
                return None
            for j in range(i + 1, len(members_sorted)):
                if _same_dim(i, j):
                    dim_union(i, j)

        dim_buckets: dict[int, list[int]] = defaultdict(list)
        for i in range(len(members_sorted)):
            dim_buckets[dim_find(i)].append(i)

        for bucket_indices in dim_buckets.values():
            if len(bucket_indices) < 2:
                continue

            recs = [members_sorted[i] for i in bucket_indices]

            # Check whether all pairs in this bucket are visually identical.
            # pHash ≤ _EXACT_DUP_PHASH means "same image, different file" —
            # keep only the best copy and trash the rest.
            # pHash > _EXACT_DUP_PHASH means genuine burst/series shots.
            #
            # Special case: cross-format pairs (RAW vs JPEG of the same shot)
            # have pHash distance 3-8 bits due to different colour rendering, so
            # they would incorrectly be classified as series shots.  Instead we
            # treat all-cross-format buckets as exact duplicates so the best copy
            # (RAW when keep_all_formats=False) is kept and the rest are trashed.
            #
            # We only need the boolean "any pair exceeds _EXACT_DUP_PHASH",
            # not the exact maximum — short-circuit on the first such pair so
            # large genuine-series buckets (e.g. 500-shot bursts) don't pay the
            # full O(n²) cost just to learn they are not exact duplicates.
            exceeds_exact = False
            for a in range(len(recs)):
                if exceeds_exact:
                    break
                ph_a = recs[a].phash
                for b in range(a + 1, len(recs)):
                    if int(ph_a - recs[b].phash) > _EXACT_DUP_PHASH:
                        exceeds_exact = True
                        break

            # A bucket is "all cross-format" when it contains both RAW and
            # non-RAW files — i.e. the same shot in two different file formats.
            raw_flags = {is_raw_flags[bi] for bi in bucket_indices}
            all_cross_format = (True in raw_flags and False in raw_flags)

            if all_cross_format:
                # Cross-format bucket: same shot captured as both RAW and JPEG.
                #
                # keep_all_formats=True  → user explicitly wants every format
                #   preserved.  Mark ALL members as series originals; this leaves
                #   previews empty so _classify_group returns None and the group
                #   never appears in the review list — nothing can be trashed.
                #
                # keep_all_formats=False → RAW is the authoritative master; the
                #   camera-generated JPEG is a derivative that can be trashed.
                #   • Every RAW file in the bucket → series_indices (always kept).
                #   • Every non-RAW (JPEG/PNG) file → exact_dup_indices (preview).
                if getattr(settings, "keep_all_formats", True):
                    # All formats preserved — hide the group entirely.
                    series_indices.update(bucket_indices)
                else:
                    for bi in bucket_indices:
                        if members_sorted[bi].path.suffix.lower() in RAW_EXTENSIONS:
                            series_indices.add(bi)      # RAW: always keep as original
                        else:
                            exact_dup_indices.add(bi)   # JPEG/PNG: always a duplicate
                is_series = True
            elif not exceeds_exact:
                # True exact duplicates: same format, same content.
                # Keep the best (bucket_indices[0]); trash the rest.
                for dup_idx in bucket_indices[1:]:
                    exact_dup_indices.add(dup_idx)
            else:
                # Possible series OR rotation-equivalent pair with elevated direct pHash.
                # 180°-rotated JPEG copies have the same dimensions but large direct
                # pHash distance; their rotation-aware minimum distance is small (≤ 6).
                # Detect rotation duplicates by checking if ALL intra-bucket pairs have
                # a rotation-aware minimum distance within the rotation threshold.
                # Fixed physical floor — same reasoning as in _can_be_similar:
                # rotation drift ≤ 6 bits regardless of the calibration threshold.
                _base = 2
                rot_thr = int(_base * getattr(settings, "rotation_threshold_factor", 3.0))

                def _rot_aware_dist(ra: "ImageRecord", rb: "ImageRecord") -> int:
                    d = int(ra.phash - rb.phash)
                    for rh in (rb.phash_r90, rb.phash_r180, rb.phash_r270):
                        if rh is not None:
                            d = min(d, int(ra.phash - rh))
                    for rh in (ra.phash_r90, ra.phash_r180, ra.phash_r270):
                        if rh is not None:
                            d = min(d, int(rh - rb.phash))
                    return d

                all_rotation_pairs = all(
                    _rot_aware_dist(recs[a], recs[b]) <= rot_thr
                    for a in range(len(recs))
                    for b in range(a + 1, len(recs))
                )

                if all_rotation_pairs:
                    # Same image saved at different orientations.  Keep best; trash rest.
                    for dup_idx in bucket_indices[1:]:
                        exact_dup_indices.add(dup_idx)
                else:
                    # Possible series / burst.  Union-find is single-linkage, so a
                    # chain A→B→C (each pair ≤ threshold) can end up in the same
                    # bucket even when A and C are visually unrelated.  Guard against
                    # this by requiring every confirmed series member to be within
                    # series_threshold of the bucket medoid — not just of its nearest
                    # neighbour.  Outliers are excluded from series_indices and fall
                    # through to the normal _is_preview check below.
                    series_thr = int(settings.threshold * settings.series_threshold_factor)

                    # Medoid: the member with the smallest total pHash distance to
                    # all others in the bucket (most "central" image).
                    medoid_local = min(
                        range(len(recs)),
                        key=lambda li: sum(
                            int(recs[li].phash - recs[lj].phash)
                            for lj in range(len(recs)) if lj != li
                        ),
                    )
                    med_rec = recs[medoid_local]

                    confirmed_series: list[int] = []
                    for k_local, (bi, rec_bi) in enumerate(zip(bucket_indices, recs)):
                        # Use rotation-aware distance so rotated copies aren't evicted.
                        d = int(med_rec.phash - rec_bi.phash)
                        for rh in (rec_bi.phash_r90, rec_bi.phash_r180, rec_bi.phash_r270):
                            if rh is not None:
                                d = min(d, int(med_rec.phash - rh))
                        if d <= series_thr:
                            confirmed_series.append(bi)

                    if len(confirmed_series) >= 2:
                        is_series = True
                        series_indices.update(confirmed_series)
                    # Members not in confirmed_series are not added to series_indices;
                    # they fall through to _is_preview at the classify stage below.

    # ── classify originals vs previews ───────────────────────────────────
    preview_ratio_gap = 1.0 - settings.preview_ratio

    originals: list[ImageRecord] = []
    previews: list[ImageRecord] = []

    if settings.keep_all_formats:
        originals, previews = _split_by_format(
            members_sorted, global_best, settings, series_indices, exact_dup_indices
        )
    else:
        for idx, member in enumerate(members_sorted):
            if idx in exact_dup_indices:
                previews.append(member)
            elif idx in series_indices:
                originals.append(member)
            elif _is_preview(member, global_best, preview_ratio_gap):
                previews.append(member)
            else:
                originals.append(member)

    # If no previews, this group is all originals (nothing to trash)
    if not previews:
        return None

    return DuplicateGroup(
        originals=originals,
        previews=previews,
        is_series=is_series,
        group_id=group_id,
    )


def _is_preview(member: ImageRecord, best: ImageRecord, ratio_gap: float) -> bool:
    """
    Return True if member should be treated as a duplicate/preview of best.

    Three cases:
    1. Same resolution: all members other than best are previews (burst shots /
       same-quality duplicates). The one with the most pixels (best) is kept;
       the rest are sent to trash.  ratio_gap is ignored because there is no
       meaningful size difference to measure.
    2. Rotation-equivalent: member is the same image at a 90°/270° orientation
       (width and height exactly swapped vs best).  The non-best member is the
       copy to trash; the best (higher quality / larger file) is kept.
    3. Different resolution: member must be strictly smaller in BOTH dimensions
       by at least ratio_gap fraction (original resize/compress workflow).
    """
    if best.width == 0 or best.height == 0:
        return False
    if member is best:
        return False
    # Same-resolution duplicates (burst shots, re-saves, same-quality copies)
    if member.width == best.width and member.height == best.height:
        return True
    # Rotation-equivalent duplicates: 90° or 270°-rotated copies swap w↔h exactly.
    # pHash comparison has already confirmed visual similarity; here we just need
    # to recognise the transposed-dimension signature so the non-best copy is trashed.
    if member.width == best.height and member.height == best.width:
        return True
    # Downscaled / compressed copies
    w_threshold = best.width * (1.0 - ratio_gap)
    h_threshold = best.height * (1.0 - ratio_gap)
    return member.width < w_threshold and member.height < h_threshold


def _split_by_format(
    members: list[ImageRecord],
    global_best: ImageRecord,
    settings: Settings,
    series_indices: set[int],
    exact_dup_indices: set[int],
) -> tuple[list[ImageRecord], list[ImageRecord]]:
    """
    For keep_all_formats mode:
    - Keep the best representative of EACH file extension, but ONLY if it has
      the same dimensions as the global best (i.e., the full-resolution copy).
    - Smaller versions in any format are still treated as previews.
    - Genuine series members (different content, same dims) are always kept.
    - Exact duplicate members (same content, same dims) are trashed.
    """
    by_ext: dict[str, list[tuple[int, ImageRecord]]] = defaultdict(list)
    for idx, m in enumerate(members):
        by_ext[m.path.suffix.lower()].append((idx, m))

    tol = settings.series_tolerance_pct / 100.0
    preview_ratio_gap = 1.0 - settings.preview_ratio

    def _same_size_as_best(r: ImageRecord) -> bool:
        if global_best.width == 0 or global_best.height == 0:
            return False
        w_ratio = abs(r.width - global_best.width) / max(r.width, global_best.width)
        h_ratio = abs(r.height - global_best.height) / max(r.height, global_best.height)
        # Cross-format pairs get a wider tolerance: rawpy decodes a few extra
        # sensor-border pixels so a CR2 decodes larger than the camera JPEG.
        r_is_raw = r.path.suffix.lower() in RAW_EXTENSIONS
        best_is_raw = global_best.path.suffix.lower() in RAW_EXTENSIONS
        effective_tol = (
            max(tol, _CROSS_FORMAT_DIM_TOL)
            if r_is_raw != best_is_raw else tol
        )
        return w_ratio <= effective_tol and h_ratio <= effective_tol

    originals: list[ImageRecord] = []
    previews: list[ImageRecord] = []

    for ext_items in by_ext.values():
        ext_sorted = sorted(ext_items, key=lambda t: _sort_key(t[1], settings))

        # Exact duplicates are always trashed regardless of size
        exact_in_ext    = [(idx, m) for idx, m in ext_sorted if idx in exact_dup_indices]
        series_in_ext   = [(idx, m) for idx, m in ext_sorted if idx in series_indices]
        non_series_in_ext = [
            (idx, m) for idx, m in ext_sorted
            if idx not in series_indices and idx not in exact_dup_indices
        ]

        for _, m in exact_in_ext:
            previews.append(m)

        # Genuine series members of this format are always kept as originals
        for _, m in series_in_ext:
            originals.append(m)

        # For non-series, non-exact-dup members: keep the best if full-resolution
        if non_series_in_ext:
            ns_best_idx, ns_best = non_series_in_ext[0]
            # A format's best copy is kept as an original when it is either:
            #   (a) same-size as the global best (within cross-format tolerance), OR
            #   (b) not small enough to count as a preview under the preview_ratio rule.
            # Case (b) handles cross-format pairs like a 5 700×3 800 camera JPEG matched
            # with a 6 036×4 020 RAW — they differ by ~5 % which exceeds the strict
            # 2 % cross-format dim tolerance but are clearly not preview-sized thumbnails
            # (94 % of full size).  Without this fallback those near-full-res JPEGs were
            # incorrectly trashed even when keep_all_formats=True.
            if _same_size_as_best(ns_best) or not _is_preview(ns_best, global_best, preview_ratio_gap):
                originals.append(ns_best)
            else:
                previews.append(ns_best)
            # Remaining: preview-size check — near-same-size kept as originals.
            # Extra guard for same-dimension files: only trash if pHash is close
            # enough to confirm an exact duplicate.  Different photos taken with
            # the same camera model share identical width×height but have distinct
            # content and must not be trashed as previews.
            for _, m in non_series_in_ext[1:]:
                if _is_preview(m, global_best, preview_ratio_gap):
                    if m.width == global_best.width and m.height == global_best.height:
                        # Same dimensions as best: only trash genuine exact duplicates.
                        if (m.phash - ns_best.phash) <= _EXACT_DUP_PHASH:
                            previews.append(m)   # exact duplicate → trash
                        else:
                            originals.append(m)  # different shot, same camera → keep
                    else:
                        previews.append(m)   # rotation-equivalent or smaller → trash
                else:
                    originals.append(m)

    return originals, previews


# ── Video duplicate detection ─────────────────────────────────────────────────

_FFMPEG_TIMEOUT = 10            # seconds — prevents hangs on malformed/truncated videos
_VIDEO_EXTRACT_WORKERS = 2      # concurrent ffmpeg/OpenCV calls during cache-miss extraction


def _probe_video_duration(path: Path) -> "Optional[float]":
    """Return video duration in seconds via ffprobe, or None if unavailable.

    Used by _extract_video_thumb to choose a safe seek position for very
    short clips (duration < 1 s).
    """
    try:
        import subprocess
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _extract_video_thumb(path: Path) -> "Optional[Image.Image]":
    """Try to extract a representative frame from a video file.

    Tries ffmpeg subprocess first (widely available), then OpenCV as fallback.
    Returns a PIL Image (RGB), or None if neither tool is available or both fail.

    Seek position:
    - Query duration via ffprobe and seek to ``min(duration / 2, 1.0)`` so that
      very short clips (< 1 s) get a frame at their midpoint rather than past EOF.
    - Falls back to seeking at 1 s without a prior duration probe when ffprobe is
      unavailable (behaviour is unchanged for normal-length videos).
    - If the timed-out ffmpeg call fails (malformed file), returns None.

    Both ffmpeg and ffprobe calls are bounded by timeouts to prevent hangs on
    corrupt or truncated video files.
    """
    import subprocess
    import io as _io

    # ── determine a safe seek offset ─────────────────────────────────────────
    seek_s = 1.0
    dur = _probe_video_duration(path)
    if dur is not None and dur < 2.0:
        # For clips shorter than 2 s, seek to the midpoint so we always land
        # on a valid frame.  For normal videos this evaluates to >= 1.0.
        seek_s = max(dur / 2.0, 0.0)

    seek_str = f"{seek_s:.3f}"

    # ── 1. ffmpeg path ─────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", seek_str, "-i", str(path),
                "-frames:v", "1", "-f", "image2pipe",
                "-vcodec", "png", "-",
            ],
            capture_output=True, timeout=_FFMPEG_TIMEOUT,
        )
        if result.returncode == 0 and result.stdout:
            img = Image.open(_io.BytesIO(result.stdout))
            img.load()
            return img.convert("RGB") if img.mode != "RGB" else img
    except FileNotFoundError:
        # ffmpeg not installed — fall through to OpenCV silently.
        pass
    except Exception:
        pass

    # ── 2. OpenCV fallback ────────────────────────────────────────────────
    try:
        import cv2  # type: ignore
        cap = cv2.VideoCapture(str(path))
        seek_ms = seek_s * 1000.0
        cap.set(cv2.CAP_PROP_POS_MSEC, seek_ms)
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    except ImportError:
        # cv2 not installed — that's fine, it's optional.
        pass
    except Exception:
        pass

    return None


def collect_videos(
    folder: Path,
    skip_paths: set[Path],
    settings: Settings,
    progress_cb: Optional[ProgressCb] = None,
    stop_flag: Optional[list[bool]] = None,
    library=None,
    failed_paths_out: "Optional[list[Path]]" = None,
) -> List[ImageRecord]:
    """Walk *folder* and return one :class:`ImageRecord` per video file found.

    Records have ``is_video=True``, ``width=0``, ``height=0``.
    ``phash`` holds a thumbnail pHash when ``settings.video_use_thumb`` is True
    and a frame could be extracted via ffmpeg or OpenCV; otherwise a zero hash.

    Args:
        library: Optional :class:`~library.Library` instance.  When supplied,
                 previously extracted thumbnail pHashes are reused for files
                 whose ``mtime`` and ``size`` are unchanged (cache hit), so
                 repeated scans of a large video collection skip the costly
                 ffmpeg subprocess for every file.
        failed_paths_out: Optional list; any video file that caused an
                 unrecoverable exception during stat/hashing is appended here
                 so callers can report failures without crashing the scan.

    Parallelism: up to ``_VIDEO_EXTRACT_WORKERS`` ffmpeg/OpenCV subprocesses run
    concurrently for cache-miss files.  Files that hit the cache are never sent
    to the pool.  The cap is intentionally low (2) to avoid overwhelming a HDD
    or spinning fans on a large video collection.
    """
    skip_names_set = {s.strip() for s in settings.skip_names.split(",") if s.strip()}
    video_paths: list[Path] = []

    if settings.recursive:
        for root, dirs, files in os.walk(folder):
            root_path = Path(root).resolve()
            dirs[:] = [
                d for d in dirs
                if (root_path / d).resolve() not in skip_paths
                and d not in skip_names_set
            ]
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix.lower() in VIDEO_EXTENSIONS:
                    video_paths.append(fpath)
    else:
        for fname in os.listdir(folder):
            fpath = folder / fname
            if fpath.is_file() and fpath.suffix.lower() in VIDEO_EXTENSIONS:
                video_paths.append(fpath)

    total = len(video_paths)
    _zero = imagehash.ImageHash(_np.zeros((8, 8), dtype=bool))
    use_thumb = bool(getattr(settings, "video_use_thumb", True))

    # Load per-folder video pHash cache when a library is available.
    from library import VideoRecord as _VideoRecord
    _vcache_old: dict[str, _VideoRecord] = (
        library.load_video_cache(str(folder)) if library is not None else {}
    )
    _vcache_new: dict[str, _VideoRecord] = {}

    # Progress throttle: report every file when the collection is small;
    # otherwise at most once every 5 files to avoid flooding the UI.
    _progress_step = 1 if total <= 20 else 5
    failed_paths: list[Path] = []

    # ── Phase 1: stat + cache check (fast, sequential) ─────────────────────
    # Produces two buckets: `ready` (cache hits, no ffmpeg needed) and
    # `to_extract` (cache misses that need frame extraction).
    # Both share the same slot in the final `ordered_phashes` list, keyed by
    # path string so Phase 3 can assemble records in original walk order.

    @dataclass
    class _VideoSlot:
        path: Path
        stat_result: "os.stat_result"
        ph: "imagehash.ImageHash"           # filled by cache hit or extraction
        done: bool = False                  # True once ph is final

    slots: list[_VideoSlot] = []
    extract_indices: list[int] = []         # indices into slots[] needing extraction

    for i, path in enumerate(video_paths):
        if stop_flag and stop_flag[0]:
            break
        if progress_cb and (i == 0 or i % _progress_step == 0 or i == total - 1):
            progress_cb(
                f"Indexing video {i + 1}/{total}: {path.name}",
                i + 1, total, "Videos",
            )
        try:
            stat = path.stat()
            # Skip zero-byte files — they cannot be valid videos.
            if stat.st_size == 0:
                if progress_cb:
                    progress_cb(
                        f"Skipped (0-byte): {path.name}",
                        i + 1, total, "Videos",
                    )
                continue
            file_key = str(path)

            if use_thumb:
                cached = _vcache_old.get(file_key)
                if (
                    cached is not None
                    and cached.mtime == stat.st_mtime
                    and cached.size == stat.st_size
                ):
                    # Cache hit — resolve pHash immediately, no extraction needed.
                    ph = imagehash.hex_to_hash(cached.phash) if cached.phash else _zero
                    slots.append(_VideoSlot(path=path, stat_result=stat, ph=ph, done=True))
                else:
                    # Cache miss — mark slot for parallel extraction.
                    slot_idx = len(slots)
                    slots.append(_VideoSlot(path=path, stat_result=stat, ph=_zero, done=False))
                    extract_indices.append(slot_idx)
            else:
                # No thumb requested — carry forward existing cache entry.
                cached = _vcache_old.get(file_key)
                if (
                    cached is not None
                    and cached.mtime == stat.st_mtime
                    and cached.size == stat.st_size
                ):
                    _vcache_new[file_key] = cached
                slots.append(_VideoSlot(path=path, stat_result=stat, ph=_zero, done=True))

        except Exception as _exc:
            failed_paths.append(path)
            if failed_paths_out is not None:
                failed_paths_out.append(path)
            if progress_cb:
                progress_cb(
                    f"Failed (skipped): {path.name} — {_exc}",
                    i + 1, total, "Videos",
                )

    # ── Phase 2: bounded parallel extraction for cache-miss files ──────────
    if extract_indices and not (stop_flag and stop_flag[0]):
        from concurrent.futures import ThreadPoolExecutor as _TPE

        def _extract_one(slot_idx: int):
            """Extract thumbnail for one slot; safe to run in a thread."""
            slot = slots[slot_idx]
            ph = _zero
            try:
                thumb = _extract_video_thumb(slot.path)
                if thumb is not None:
                    work = _downscaled_for_hashing(
                        thumb if thumb.mode == "RGB" else thumb.convert("RGB")
                    )
                    ph = imagehash.phash(work)
            except Exception:
                ph = _zero
            slot.ph = ph
            slot.done = True
            file_key = str(slot.path)
            _vcache_new[file_key] = _VideoRecord(
                path=file_key,
                mtime=slot.stat_result.st_mtime,
                size=slot.stat_result.st_size,
                phash=str(ph) if ph is not _zero else "",
            )

        with _TPE(max_workers=_VIDEO_EXTRACT_WORKERS) as _pool:
            list(_pool.map(_extract_one, extract_indices))

    # ── Phase 3: assemble ImageRecord list in original walk order ──────────
    records: list[ImageRecord] = []
    for slot in slots:
        records.append(ImageRecord(
            path=slot.path,
            width=0, height=0,
            file_size=slot.stat_result.st_size,
            phash=slot.ph,
            dhash=_zero,
            mtime=min(slot.stat_result.st_mtime, slot.stat_result.st_ctime),
            brightness=128.0,
            histogram=[],
            is_video=True,
        ))

    # Persist the updated video cache (new + untouched carry-forwards from old).
    if library is not None and _vcache_new:
        # Merge: start from old cache, update with fresh extractions.
        merged_vcache = dict(_vcache_old)
        merged_vcache.update(_vcache_new)
        # Drop entries for files that no longer exist in this scan.
        seen = {str(p) for p in video_paths}
        merged_vcache = {k: v for k, v in merged_vcache.items() if k in seen}
        library.save_video_cache(str(folder), merged_vcache)

    return records


def find_video_duplicates(
    video_records: List[ImageRecord],
    settings: Settings,
) -> List[DuplicateGroup]:
    """Group video files that are likely duplicates.

    Primary criterion: **exact file size**.  Two videos with an identical byte
    count are almost certainly the same file in a personal photo library.

    Secondary criterion (optional): **thumbnail pHash similarity** within each
    same-size bucket.  When ``settings.video_use_thumb`` is True and at least one
    record in a bucket has a non-zero thumbnail hash, the bucket is sub-divided by
    pHash distance (threshold = 8 bits).  Pairs whose thumbnails differ by more
    than 8 bits are treated as coincidentally same-size but visually different
    videos and are *not* grouped together.

    Strategy: keep the file with the earliest mtime (oldest copy) as the original;
    mark the rest as previews (candidates for trash).

    Returns a list of :class:`DuplicateGroup` objects (group_id prefix ``"v"``).
    """
    if not video_records:
        return []

    _zero_hash_int = 0
    use_thumb = bool(getattr(settings, "video_use_thumb", True))
    match_format = bool(getattr(settings, "video_match_format", True))
    match_size = bool(getattr(settings, "video_match_size", True))
    _THUMB_THR = 8  # Hamming bits — allows minor encode differences in thumbnails

    # If neither format nor size matching is enabled, do not group anything.
    # (Without at least one criterion every pair would be a "duplicate" — useless
    # and dangerous.  Issue #300: the UI guarantees at least one is ON, but this
    # belt-and-braces check protects programmatic callers.)
    if not match_format and not match_size:
        return []

    # Phase 1: bucket by (ext if format-match enabled, size if size-match enabled).
    # An empty string / zero acts as a wildcard for the disabled dimension.
    by_key: dict[tuple[str, int], list[ImageRecord]] = defaultdict(list)
    for rec in video_records:
        ext_key = rec.path.suffix.lower() if match_format else ""
        size_key = rec.file_size if match_size else 0
        by_key[(ext_key, size_key)].append(rec)
    # Local alias so the rest of the function reads naturally.
    by_size = by_key

    groups: list[DuplicateGroup] = []
    group_counter = 0

    for _key, members in by_size.items():
        if len(members) < 2:
            continue

        # Check whether any thumbnails were successfully extracted
        has_thumbs = use_thumb and any(
            int(str(r.phash), 16) != _zero_hash_int for r in members
        )

        if has_thumbs:
            # Phase 2: sub-group within same-size bucket by thumbnail pHash.
            # Videos whose thumbnail extraction failed (zero hash) are treated
            # as size-only matches and grouped with every other member in the
            # bucket — their content is unknown but exact-size match is already
            # strong evidence of duplication in a personal library.
            n = len(members)
            parent = list(range(n))

            def _find(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def _union(x: int, y: int) -> None:
                px, py = _find(x), _find(y)
                if px != py:
                    parent[px] = py

            # Track which pairs joined due to a missing thumbnail (zero-hash).
            # If any pair in a final bucket is size-only (one or both thumbs
            # missing), we mark that group ambiguous so the UI flags it.
            ambiguous_pairs: set[tuple[int, int]] = set()

            for i in range(n):
                for j in range(i + 1, n):
                    pi_zero = int(str(members[i].phash), 16) == _zero_hash_int
                    pj_zero = int(str(members[j].phash), 16) == _zero_hash_int
                    # If either thumbnail is missing, group by size alone.
                    if pi_zero or pj_zero:
                        _union(i, j)
                        ambiguous_pairs.add((i, j))
                    elif members[i].phash - members[j].phash <= _THUMB_THR:
                        _union(i, j)

            buckets: dict[int, list[int]] = defaultdict(list)
            for i in range(n):
                buckets[_find(i)].append(i)

            for bucket_indices in buckets.values():
                if len(bucket_indices) < 2:
                    continue
                bucket_members = [members[i] for i in bucket_indices]
                # Keep oldest (smallest mtime); use file_size desc as tiebreaker
                bucket_members.sort(key=lambda r: (r.mtime, -r.file_size))
                group_counter += 1
                # Mark ambiguous if any member pair in this bucket lacked a thumbnail.
                idx_set = set(bucket_indices)
                is_amb = any(
                    (a in idx_set and b in idx_set) for a, b in ambiguous_pairs
                )
                groups.append(DuplicateGroup(
                    originals=[bucket_members[0]],
                    previews=bucket_members[1:],
                    is_series=False,
                    is_ambiguous=is_amb,
                    group_id=f"v{group_counter:04d}",
                ))
        else:
            # Size-only: no thumbnails available at all.  Mark ambiguous so the
            # report flags these groups — same size alone is strong evidence in
            # personal libraries but can produce false positives (e.g. two
            # different videos encoded to the same target bitrate).
            members_sorted = sorted(members, key=lambda r: (r.mtime, -r.file_size))
            group_counter += 1
            groups.append(DuplicateGroup(
                originals=[members_sorted[0]],
                previews=members_sorted[1:],
                is_series=False,
                is_ambiguous=True,   # no visual confirmation available
                group_id=f"v{group_counter:04d}",
            ))

    return groups
