"""
scanner.py — Image collection and duplicate-group detection via perceptual hashing.
v2 rewrite with aspect-ratio guard, brightness guard, dual-hash, histogram intersection,
series detection, RAW companion matching, and pause/resume support.
"""
from __future__ import annotations

import os
from collections import defaultdict
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
    gray = img.convert("L")
    pixels = list(gray.getdata())
    return sum(pixels) / len(pixels) if pixels else 0.0


# ── collection ───────────────────────────────────────────────────────────────

def collect_images(
    folder: Path,
    skip_paths: set[Path],
    settings: Settings,
    progress_cb: Optional[ProgressCb] = None,
    stop_flag: Optional[list[bool]] = None,
    pause_flag: Optional[list[bool]] = None,
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
        dh = imagehash.dhash(rgb)
        hist = _compute_histogram(rgb)
        brightness = _compute_brightness(rgb)

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
    """
    # 1. Aspect ratio guard
    ar_a = a.width / a.height if a.height else 1.0
    ar_b = b.width / b.height if b.height else 1.0
    ar_diff = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001)
    if ar_diff > settings.ar_tolerance_pct / 100:
        return False

    # 2. Brightness guard
    if abs(a.brightness - b.brightness) > settings.brightness_max_diff:
        return False

    # 3. pHash — use a more lenient threshold for same-dimension pairs so that
    #    burst/series shots (which may differ by quality) still get grouped.
    #    _classify_group will keep all same-size members as originals.
    same_dims = _same_dimensions(a, b, settings.series_tolerance_pct)
    if same_dims:
        eff_threshold = int(settings.threshold * settings.series_threshold_factor)
    else:
        eff_threshold = settings.threshold

    if settings.dark_protection:
        if a.brightness < settings.dark_threshold or b.brightness < settings.dark_threshold:
            eff_threshold = max(1, int(eff_threshold * settings.dark_tighten_factor))

    if a.phash - b.phash > eff_threshold:
        return False

    # 4. dHash — also scale for same-dimension pairs
    if settings.use_dual_hash:
        dhash_thr = eff_threshold * 1.5
        if a.dhash - b.dhash > dhash_thr:
            return False

    # 5. Histogram intersection
    if settings.use_histogram and a.histogram and b.histogram:
        intersection = sum(min(x, y) for x, y in zip(a.histogram, b.histogram)) / 3
        if intersection < settings.hist_min_similarity:
            return False

    return True


# ── grouping ──────────────────────────────────────────────────────────────────

def _sort_key(r: ImageRecord, settings: Settings):
    """Return sort key so the *best* image sorts first (ascending)."""
    strategy = settings.keep_strategy
    if strategy == "oldest":
        # Try EXIF date first, then filename date, then mtime
        if settings.sort_by_exif_date:
            dt = extract_date_from_exif(r.path)
            if dt is not None:
                return dt.timestamp()
        if settings.sort_by_filename_date:
            dt = extract_date_from_filename(r.path.name)
            if dt is not None:
                return dt.timestamp()
        return r.mtime
    return -r.pixels


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

    total_comparisons = n * (n - 1) // 2
    done_comparisons = start_i * (n - start_i) + start_i * (start_i - 1) // 2 if start_i else 0

    for i in range(start_i, n):
        if stop_flag and stop_flag[0]:
            return [], None
        if pause_flag and pause_flag[0]:
            # Build partial state for resume
            from scan_state import ScanState, serialize_record
            from dataclasses import asdict
            partial = ScanState(
                phase="comparing",
                records=[serialize_record(r) for r in records],
                compare_i=i,
                union_parent=list(parent),
            )
            return [], partial

        if progress_cb and i % 10 == 0:
            pct_done = done_comparisons
            progress_cb(f"Comparing image {i + 1}/{n}...", pct_done, total_comparisons, "Comparing")

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

            for si in range(m):
                for sj in range(si + 1, m):
                    a, b = records[singletons[si]], records[singletons[sj]]
                    # Apply AR and brightness guards, but skip histogram/dHash
                    ar_a = a.width / a.height if a.height else 1.0
                    ar_b = b.width / b.height if b.height else 1.0
                    ar_diff = abs(ar_a - ar_b) / max(ar_a, ar_b, 0.001)
                    if ar_diff > settings.ar_tolerance_pct / 100:
                        continue
                    if abs(a.brightness - b.brightness) > settings.brightness_max_diff:
                        continue
                    pdist = a.phash - b.phash
                    if settings.threshold < pdist <= amb_threshold:
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

    Series detection:
    - Group members by (width, height) within series_tolerance_pct
    - Any bucket with 2+ members -> all are originals (is_series = True)
    - A member is only a preview if BOTH dimensions are strictly smaller than the best

    Returns None if no previews were found (all images are equal/series).
    """
    members_sorted = sorted(members, key=lambda r: _sort_key(r, settings))
    global_best = members_sorted[0]

    # ── series detection ────────────────────────────────────────────────
    tol = settings.series_tolerance_pct / 100.0

    def _same_dim(a: ImageRecord, b: ImageRecord) -> bool:
        if a.width == 0 or a.height == 0 or b.width == 0 or b.height == 0:
            return False
        w_ratio = abs(a.width - b.width) / max(a.width, b.width)
        h_ratio = abs(a.height - b.height) / max(a.height, b.height)
        return w_ratio <= tol and h_ratio <= tol

    # Build dimension buckets (union-find approach for transitivity)
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

    # Mark series members
    series_indices: set[int] = set()
    is_series = False
    for bucket_indices in dim_buckets.values():
        if len(bucket_indices) >= 2:
            is_series = True
            series_indices.update(bucket_indices)

    # ── classify originals vs previews ───────────────────────────────────
    preview_ratio_gap = 1.0 - settings.preview_ratio

    originals: list[ImageRecord] = []
    previews: list[ImageRecord] = []

    if settings.keep_all_formats:
        originals, previews = _split_by_format(
            members_sorted, global_best, settings, series_indices
        )
    else:
        for idx, member in enumerate(members_sorted):
            if idx in series_indices:
                # Series member: always keep
                originals.append(member)
            elif _is_preview(member, global_best, preview_ratio_gap):
                previews.append(member)
            else:
                # Not a series member but also not a clear preview -> keep as original
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
    Return True only if BOTH dimensions of member are strictly smaller than
    best's dimensions by at least ratio_gap fraction.
    """
    if best.width == 0 or best.height == 0:
        return False
    w_threshold = best.width * (1.0 - ratio_gap)
    h_threshold = best.height * (1.0 - ratio_gap)
    return member.width < w_threshold and member.height < h_threshold


def _split_by_format(
    members: list[ImageRecord],
    global_best: ImageRecord,
    settings: Settings,
    series_indices: set[int],
) -> tuple[list[ImageRecord], list[ImageRecord]]:
    """
    For keep_all_formats mode:
    - Keep the best representative of EACH file extension, but ONLY if it has
      the same dimensions as the global best (i.e., the full-resolution copy).
    - Smaller versions in any format are still treated as previews.
    - Series members (same-dim bucket) are always kept.
    """
    by_ext: dict[str, list[tuple[int, ImageRecord]]] = defaultdict(list)
    for idx, m in enumerate(members):
        by_ext[m.path.suffix.lower()].append((idx, m))

    tol = settings.series_tolerance_pct / 100.0

    def _same_size_as_best(r: ImageRecord) -> bool:
        """True if r has the same dimensions as global_best within tolerance."""
        if global_best.width == 0 or global_best.height == 0:
            return False
        w_ratio = abs(r.width - global_best.width) / max(r.width, global_best.width)
        h_ratio = abs(r.height - global_best.height) / max(r.height, global_best.height)
        return w_ratio <= tol and h_ratio <= tol

    originals: list[ImageRecord] = []
    previews: list[ImageRecord] = []

    for ext_items in by_ext.values():
        ext_sorted = sorted(ext_items, key=lambda t: _sort_key(t[1], settings))

        # Split ext members into series members and non-series members
        series_in_ext = [(idx, m) for idx, m in ext_sorted if idx in series_indices]
        non_series_in_ext = [(idx, m) for idx, m in ext_sorted if idx not in series_indices]

        # Series members of this format are always kept as originals
        for _, m in series_in_ext:
            originals.append(m)

        # For non-series members, keep only the best if it's the same size as global_best
        if non_series_in_ext:
            ns_best_idx, ns_best = non_series_in_ext[0]
            if _same_size_as_best(ns_best):
                originals.append(ns_best)
            else:
                previews.append(ns_best)
            # For remaining non-series members: only trash if they're genuinely small previews.
            # Without this check, near-same-size images in the same format get wrongly trashed.
            preview_ratio_gap = 1.0 - settings.preview_ratio
            for _, m in non_series_in_ext[1:]:
                if _is_preview(m, global_best, preview_ratio_gap):
                    previews.append(m)
                else:
                    originals.append(m)

    return originals, previews
