"""
config.py — Settings dataclass with JSON persistence.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Settings:
    mode: str = "quick"                    # "quick" or "advanced"
    src_folder: str = ""
    out_folder: str = ""
    threshold: int = 2                     # pHash similarity threshold
    preview_ratio: float = 0.90            # per-dimension ratio (both w and h must be smaller)
    series_tolerance_pct: float = 0.0      # % tolerance for "same dimensions" series detection
    series_threshold_factor: float = 1.0  # pHash threshold multiplier for same-size images
    ar_tolerance_pct: float = 5.0          # aspect ratio tolerance %
    dark_protection: bool = True
    dark_threshold: float = 40.0
    dark_tighten_factor: float = 0.5
    use_dual_hash: bool = True
    use_histogram: bool = True
    hist_min_similarity: float = 0.70
    brightness_max_diff: float = 60.0
    use_rawpy: bool = False
    keep_strategy: str = "pixels"          # "pixels" or "oldest"
    keep_all_formats: bool = True
    prefer_rich_metadata: bool = True
    collect_metadata: bool = True
    export_csv: bool = True
    extended_report: bool = False
    sort_by_filename_date: bool = False
    sort_by_exif_date: bool = False
    min_dimension: int = 0
    recursive: bool = True
    skip_names: str = ".thumbnails, thumbs, @eaDir, Thumbs"
    dry_run: bool = True
    details_visible: bool = False
    ambiguous_detection: bool = False
    ambiguous_threshold_factor: float = 1.5
    date_folder_format: str = "%Y-%m-%d"     # strftime format for date subfolders (used by Organize tab)
    # ── Organize by Date (standalone tab) ─────────────────────────────────────
    # All date_org_* fields are owned by the new "Organize by Date" tab and are
    # completely independent of the duplicate-removal pipeline.
    date_org_src: str = ""                          # source folder
    date_org_out: str = ""                          # destination root (used when not in-place)
    date_org_in_place: bool = False                 # True = organize within source; False = use date_org_out
    date_org_op: str = "move"                       # "move" or "copy"
    date_org_use_exif: bool = True                  # priority 1: EXIF DateTimeOriginal
    date_org_use_filename: bool = True              # priority 2: date embedded in filename
    date_org_use_mtime: bool = True                 # priority 3: file modification time (last fallback)
    date_org_unknown_folder: str = "unknown_date"   # subfolder name for files with no detectable date
    date_org_conflict: str = "rename"               # "rename" | "skip" | "overwrite"
    date_org_recursive: bool = True
    date_org_include_raw: bool = True               # include CR2/NEF/ARW/DNG/etc in addition to JPEG/PNG
    date_org_move_sidecars: bool = True             # also relocate .xmp/.aae companions next to their image
    date_org_dry_run: bool = True                   # default to safe preview on first launch
    disable_series_detection: bool = False  # skip series promotion in _classify_group
    calib_folder: str = ""                  # last-used calibration folder
    calibrated_threshold: int = 0           # best threshold from last calibration (0 = none)
    calibrated_preview_ratio: float = 0.0  # best preview_ratio from last calibration
    custom_main_folder: str = ""            # Custom Scan: reference folder (never modified)
    custom_check_folder: str = ""           # Custom Scan: folder to search for duplicates
    custom_out_folder: str = ""             # Custom Scan: output/trash folder
    auto_update: bool = True                # Check for updates on startup
    skipped_update_versions: list = field(default_factory=list)  # Versions the user clicked "Skip" on (no popup)
    developer_mode: bool = False            # Show full error details / tracebacks
    cross_format_threshold_factor: float = 6.0  # pHash threshold multiplier for RAW vs JPEG pairs
    rotation_threshold_factor: float = 3.0     # pHash threshold multiplier for rotation-matched pairs
    # JPEG DCT re-encoding at a different orientation introduces up to ~6 bits of pHash
    # drift on photo-like images (quality=85).  factor=3.0 → rotation_thr = 2×3 = 6 → 100% coverage.
    report_page_size: int = 20                     # Groups per page in report viewer
    dark_mode: bool = False                      # Night theme (dark background)
    scan_speed: int = 5                          # 1=quality → 10=speed (quick-mode quality slider)
    scan_threads: int = 0                        # parallel hashing threads (0 = auto, drive-aware)
    io_parallelism: str = "auto"                 # "auto" | "ssd" | "hdd" — controls per-drive read concurrency
    hdd_thread_cap: int = 2                      # max parallel readers when drive is HDD (prevents seek-thrash & overheating)
    raw_use_embedded_thumb: bool = False         # Opt-in: use rawpy.extract_thumb() (~6× faster, but invalidates v1.1.9 and earlier RAW cache — phash differs from postprocess by ~30 bits)
    # ── Runaway-group safety net ─────────────────────────────────────────────
    # Single-linkage union-find can chain unrelated images together via 1-bit
    # pHash intermediates, which is common when a collection contains many
    # near-uniform images (blank screenshots, dark photos, document scans).
    # If a raw union-find bucket exceeds ``max_group_size``, scanner splits it
    # by requiring every member to pass the full ``_can_be_similar`` guard
    # against the bucket's medoid (chain-breaker).  Set to 0 to disable.
    max_group_size: int = 50
    # Calibrated on Canon EOS M100 CR2 vs camera JPEG (35 matched pairs, keep_all_formats=False):
    # max intra-group cross-format pHash = 12  (tone-mapped pairs with distinct processing)
    # min inter-group cross-format pHash = 20  → 8-bit safety gap
    # effective cross-format threshold  = 2 × 6 = 12  (covers all true pairs)
    # Note: rawpy postprocess() produces ~2× brighter output than camera JPEG engine;
    # histogram intersection collapses to 0.000-0.243 for true pairs → histogram
    # guard disabled for cross-format (_CROSS_FORMAT_HIST_FLOOR = 0.0 in scanner.py)


DEFAULTS = Settings()


def load_settings(path: Path) -> Settings:
    """Load settings from a JSON file. Returns defaults if file does not exist or is invalid."""
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Only apply known fields; ignore unknowns for forward-compatibility
        known = {f for f in Settings.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return Settings(**filtered)
    except Exception:
        return Settings()


def save_settings(settings: Settings, path: Path) -> None:
    """Serialize settings to a JSON file next to main.py."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(settings), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
