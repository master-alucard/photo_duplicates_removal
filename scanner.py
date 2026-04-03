"""
scanner.py — Image collection and duplicate-group detection via perceptual hashing.
v2 rewrite with aspect-ratio guard, brightness guard, dual-hash, histogram intersection,
series detection, RAW companion matching, and pause/resume support.
"""
from __future__ import annotations

import os
from collections import defaultdict
import numpy as _np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Literal, Optional

from PIL import Image, ImageOps
import imagehash

from config import Settings
from metadata import extract_date_from_filename, extract_date_from_exif

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".gif", ".heic", ".heif",
}

RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf",
    ".rw2", ".pef", ".srw", ".x3f", ".3fr",
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
# Below this threshold the O(n²) brute-force is fast enough.
_BKTREE_THRESHOLD = 200

# When comparing RAW vs JPEG file dimensions, allow up to this relative
# difference before declaring them "different resolutions".  rawpy decodes
# the full sensor area (including a few border pixels) so a Canon M100 CR2
# decodes to 6024×4020 while the camera JPEG is 6000×4000 — 0.4% difference.
# 2 % is a safe upper bound that still distinguishes full-res from downscaled.
_CROSS_FORMAT_DIM_TOL = 0.02


# ── BK-tree for fast nearest-neighbour hash lookup ────────────────────────────

def _hamming(a: int, b: int) -> int:
    """Popcount of XOR — counts differing bits between two integers."""
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

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def size_label(self) -> str:
        mb = self.file_size / (1024 * 1024)
        return f"{mb:.2f} MB" if mb >= 1 else f"{self.file_size // 1024} KB"

    def dim_label(self) -> str:
        mp = self.pixels / 1_000_000
        return f"{self.width}x{self.height}  ({mp:.1f} MP)"

    def date_label(self) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")


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
    """
    if img.mode != "RGB":
        img = img.convert("RGB")

    raw = img.histogram()   # 256 values per channel = 768 total
    result: list[float] = []

    for channel in range(3):
        channel_data = raw[channel * 256: (channel + 1) * 256]
        total = sum(channel_data) or 1
        # Downsample 256 bins -> 32 bins (merge 8 adjacent bins)
        for bin_start in range(0, 256, 8):
            bin_val = sum(channel_data[bin_start: bin_start + 8]) / total
            result.append(bin_val)

    return result


def _compute_brightness(img: Image.Image) -> float:
    """Return mean pixel brightness 0.0-255.0."""
    import numpy as np
    gray = img.convert("L")
    return float(np.asarray(gray, dtype=np.float32).mean())


# ── collection ───────────────────────────────────────────────────────────────

def collect_images(
    folder: Path,
    skip_paths: set[Path],
    settings: Settings,
    progress_cb: Optional[ProgressCb] = None,
    stop_flag: Optional[list[bool]] = None,
    pause_flag: Optional[list[bool]] = None,
    failed_paths: Optional[list] = None,
) -> List[ImageRecord]:
    """
    Walk folder, compute phash/dhash/histogram/brightness for every qualifying image.
    RAW files are either hashed (if use_rawpy) or stem-matched to their JPEG siblings.
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

    n_threads = getattr(settings, "scan_threads", 1)
    if n_threads == 0:
        import os as _os
        n_threads = max(1, (_os.cpu_count() or 1))

    def _hash_one(args: tuple) -> "tuple[int, Path, Optional[ImageRecord], Optional[Exception]]":
        i, path = args
        try:
            ext = path.suffix.lower()
            if ext in RAW_EXTENSIONS and settings.use_rawpy and rawpy_available:
                rec = _hash_raw(path, settings)
            else:
                rec = _hash_image(path, settings)
            return i, path, rec, None
        except Exception as exc:
            return i, path, None, exc

    if n_threads > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        futures = {}
        completed = 0
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            for i, path in enumerate(all_image_paths):
                if stop_flag and stop_flag[0]:
                    break
                futures[pool.submit(_hash_one, (i, path))] = i

            ordered: dict[int, Optional[ImageRecord]] = {}
            for fut in as_completed(futures):
                if stop_flag and stop_flag[0]:
                    break
                idx, path, rec, exc = fut.result()
                completed += 1
                if progress_cb:
                    progress_cb(f"Hashing {path.name}", completed, total, "Hashing")
                if exc is not None:
                    if failed_paths is not None:
                        failed_paths.append(path)
                else:
                    ordered[idx] = rec

        for idx in sorted(ordered):
            rec = ordered[idx]
            if rec is None:
                continue
            records.append(rec)
            stem_key = (all_image_paths[idx].parent.resolve(), all_image_paths[idx].stem.lower())
            stem_to_record[stem_key] = rec
    else:
        for i, path in enumerate(all_image_paths):
            if stop_flag and stop_flag[0]:
                break
            if pause_flag and pause_flag[0]:
                break

            if progress_cb:
                progress_cb(f"Hashing {path.name}", i + 1, total, "Hashing")

            try:
                ext = path.suffix.lower()

                if ext in RAW_EXTENSIONS and settings.use_rawpy and rawpy_available:
                    rec = _hash_raw(path, settings)
                else:
                    rec = _hash_image(path, settings)

                if rec is None:
                    continue

                records.append(rec)
                stem_key = (path.parent.resolve(), path.stem.lower())
                stem_to_record[stem_key] = rec

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

    return records


def _hash_image(path: Path, settings: Settings) -> Optional[ImageRecord]:
    """Open a regular image and compute all hash/feature data."""
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img.load()

        if img.mode not in ("RGB", "RGBA", "L", "P"):
            img = img.convert("RGB")
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        w, h = img.size

        if settings.min_dimension > 0 and max(w, h) < settings.min_dimension:
            return None

        rgb = img if img.mode == "RGB" else img.convert("RGB")
        ph = imagehash.phash(rgb)
        dh = imagehash.dhash(rgb) if settings.use_dual_hash else imagehash.ImageHash(_np.zeros((8, 8), dtype=bool))
        hist = _compute_histogram(rgb) if settings.use_histogram else []
        brightness = _compute_brightness(rgb) if settings.dark_protection else 128.0

    stat = path.stat()
    mtime = min(stat.st_mtime, stat.st_ctime)

    metadata_count = 0
    if settings.collect_metadata:
        from metadata import count_metadata_fields
        metadata_count = count_metadata_fields(path)

    return ImageRecord(
        path=path, width=w, height=h,
        file_size=stat.st_size,
        phash=ph, dhash=dh,
        mtime=mtime,
        brightness=brightness,
        histogram=hist,
        metadata_count=metadata_count,
    )


def _hash_raw(path: Path, settings: Settings) -> Optional[ImageRecord]:
    """Decode a RAW file with rawpy and compute hashes."""
    import rawpy  # type: ignore
    with rawpy.imread(str(path)) as raw:
        rgb_array = raw.postprocess(use_camera_wb=True, output_bps=8)

    from PIL import Image as PILImage
    import numpy as np

    img = PILImage.fromarray(rgb_array)
    w, h = img.size

    if settings.min_dimension > 0 and max(w, h) < settings.min_dimension:
        return None

    ph = imagehash.phash(img)
    dh = imagehash.dhash(img)
    hist = _compute_histogram(img)
    brightness = _compute_brightness(img)

    stat = path.stat()
    mtime = min(stat.st_mtime, stat.st_ctime)

    return ImageRecord(
        path=path, width=w, height=h,
        file_size=stat.st_size,
        phash=ph, dhash=dh,
        mtime=mtime,
        brightness=brightness,
        histogram=hist,
        metadata_count=0,
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
    # 1. Aspect ratio guard (valid for all pairs including cross-format)
    ar_a = a.width / a.height if a.height else 1.0
    ar_b = b.width / b.height if b.height else 1.0
    ar_diff = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001)
    if ar_diff > settings.ar_tolerance_pct / 100:
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

    # Cross-format pairs get an additional threshold relaxation on top of series scaling
    if cross_format:
        eff_threshold = max(eff_threshold, int(settings.threshold * cf_factor))

    if settings.dark_protection:
        if a.brightness < settings.dark_threshold or b.brightness < settings.dark_threshold:
            eff_threshold = max(1, int(eff_threshold * settings.dark_tighten_factor))

    if a.phash - b.phash > eff_threshold:
        return False

    # 4. dHash — also scale for same-dimension pairs.
    # Skipped for cross-format pairs: rawpy demosaicing produces very different
    # directional gradients compared to the camera JPEG engine.  Calibration on
    # Canon EOS M100 shows intra-group dHash up to 18 bits — well above the 15-bit
    # limit that would be inferred from the pHash threshold — causing false negatives.
    # Also skipped when pHash == 0: perceptually identical images (same DCT spectrum)
    # may still diverge on local gradient direction (dHash) due to JPEG quality or
    # denoising differences.  pHash=0 is already a definitive identity signal, so an
    # additional dHash gate would only produce false negatives here.
    if settings.use_dual_hash and not cross_format and a.phash - b.phash > 0:
        dhash_thr = eff_threshold * 1.5
        if a.dhash - b.dhash > dhash_thr:
            return False

    # 5. Histogram intersection — skip for cross-format pairs: RAW color rendering
    #    (with camera profile) differs visibly from camera JPEG engine output, so
    #    histogram distance would be misleading.
    if not cross_format and settings.use_histogram and a.histogram and b.histogram:
        intersection = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3
        if intersection < settings.hist_min_similarity:
            return False

    return True


# ── grouping ──────────────────────────────────────────────────────────────────

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
    # cross_format_threshold_factor.  We query the BK-tree with the widest radius
    # and let _can_be_similar apply the exact per-pair logic.
    _max_query_thr = max(
        settings.threshold,
        int(settings.threshold * getattr(settings, "series_threshold_factor", 2.0)),
        int(settings.threshold * getattr(settings, "cross_format_threshold_factor", 2.0)),
    )

    if n > _BKTREE_THRESHOLD:
        # ── BK-tree fast path ─────────────────────────────────────────────
        # Pre-compute integer hash values once; str(phash) yields hex.
        hash_ints = [int(str(r.phash), 16) for r in records]

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

            for j in bk.query(hash_ints[i], _max_query_thr):
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

    for indices in buckets.values():
        if len(indices) < 2:
            continue

        group_counter += 1
        members = [records[i] for i in indices]
        result_group = _classify_group(members, settings, f"g{group_counter:04d}")
        if result_group is not None:
            groups.append(result_group)
            grouped_indices.update(indices)

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
) -> Optional[DuplicateGroup]:
    """
    Given a set of similar images, classify each as original or preview.

    Same-dimension handling distinguishes three cases:
      - Exact duplicates (pHash ≤ _EXACT_DUP_PHASH within the bucket): same image
        saved/exported twice.  Keep the best copy; trash the rest as duplicates.
      - Cross-format pairs (RAW vs JPEG of the same shot): pHash 3-8 bits apart
        due to different colour rendering.  keep_all_formats=True → keep both as
        originals; keep_all_formats=False → keep RAW, trash JPEG.
      - Genuine series / burst (pHash > _EXACT_DUP_PHASH, same format): different
        shots at the same resolution.  Keep all; none are previews.

    A member is only a preview if BOTH dimensions are strictly smaller than the best.

    Returns None if no previews were found (nothing to trash).
    """
    members_sorted = sorted(members, key=lambda r: _sort_key(r, settings))
    global_best = members_sorted[0]

    # ── series / exact-dup detection ─────────────────────────────────────
    series_indices:   set[int] = set()   # genuine burst shots — keep all
    exact_dup_indices: set[int] = set()  # exact same-image copies — trash extras
    is_series = False

    if not getattr(settings, "disable_series_detection", False):
        tol = settings.series_tolerance_pct / 100.0

        def _same_dim(a: ImageRecord, b: ImageRecord) -> bool:
            if a.width == 0 or a.height == 0 or b.width == 0 or b.height == 0:
                return False
            w_ratio = abs(a.width - b.width) / max(a.width, b.width)
            h_ratio = abs(a.height - b.height) / max(a.height, b.height)
            # For cross-format pairs (RAW vs JPEG), use a wider tolerance:
            # rawpy decodes the full sensor area, adding a few border pixels
            # vs the camera JPEG crop (e.g. 6024×4020 vs 6000×4000 = 0.4%).
            a_is_raw = a.path.suffix.lower() in RAW_EXTENSIONS
            b_is_raw = b.path.suffix.lower() in RAW_EXTENSIONS
            effective_tol = (
                max(tol, _CROSS_FORMAT_DIM_TOL)
                if a_is_raw != b_is_raw else tol
            )
            return w_ratio <= effective_tol and h_ratio <= effective_tol

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
            for j in range(i + 1, len(members_sorted)):
                if _same_dim(members_sorted[i], members_sorted[j]):
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
            max_pdist = max(
                recs[a].phash - recs[b].phash
                for a in range(len(recs))
                for b in range(a + 1, len(recs))
            )

            # A bucket is "all cross-format" when it contains both RAW and
            # non-RAW files — i.e. the same shot in two different file formats.
            raw_flags = {r.path.suffix.lower() in RAW_EXTENSIONS for r in recs}
            all_cross_format = (True in raw_flags and False in raw_flags)

            if all_cross_format:
                # Cross-format pair: same shot captured as both RAW and JPEG.
                # The elevated pHash distance (3-8 bits) is due to different
                # colour rendering, NOT different image content.
                if settings.keep_all_formats:
                    # Keep the best representative of each format; trash same-
                    # format extras.  members_sorted is already sorted best-first
                    # so the first RAW and first non-RAW in the bucket are keepers.
                    # We do NOT add these to series_indices — _split_by_format()
                    # will keep the best-of-format using _same_size_as_best().
                    raw_in_bucket = [
                        bi for bi in bucket_indices
                        if members_sorted[bi].path.suffix.lower() in RAW_EXTENSIONS
                    ]
                    nonraw_in_bucket = [
                        bi for bi in bucket_indices
                        if members_sorted[bi].path.suffix.lower() not in RAW_EXTENSIONS
                    ]
                    # Keep only the first (best-sorted) of each format; trash extras.
                    for dup_idx in raw_in_bucket[1:]:
                        exact_dup_indices.add(dup_idx)
                    for dup_idx in nonraw_in_bucket[1:]:
                        exact_dup_indices.add(dup_idx)
                else:
                    # Keep only the single best copy (RAW preferred via _sort_key);
                    # treat all other-format copies as duplicates to trash.
                    for dup_idx in bucket_indices[1:]:
                        exact_dup_indices.add(dup_idx)
            elif max_pdist <= _EXACT_DUP_PHASH:
                # True exact duplicates: same format, same content.
                # Keep the best (bucket_indices[0]); trash the rest.
                for dup_idx in bucket_indices[1:]:
                    exact_dup_indices.add(dup_idx)
            else:
                # Genuine series / burst: keep every member.
                is_series = True
                series_indices.update(bucket_indices)

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

    Two cases:
    1. Same resolution: all members other than best are previews (burst shots /
       same-quality duplicates). The one with the most pixels (best) is kept;
       the rest are sent to trash.  ratio_gap is ignored because there is no
       meaningful size difference to measure.
    2. Different resolution: member must be strictly smaller in BOTH dimensions
       by at least ratio_gap fraction (original resize/compress workflow).
    """
    if best.width == 0 or best.height == 0:
        return False
    # Same-resolution duplicates (burst shots, re-saves, same-quality copies)
    if member is not best and member.width == best.width and member.height == best.height:
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
            if _same_size_as_best(ns_best):
                originals.append(ns_best)
            else:
                previews.append(ns_best)
            # Remaining: preview-size check only — near-same-size kept as originals
            for _, m in non_series_in_ext[1:]:
                if _is_preview(m, global_best, preview_ratio_gap):
                    previews.append(m)
                else:
                    originals.append(m)

    return originals, previews
