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
    organize_by_date: bool = False
    date_folder_format: str = "%Y-%m-%d"
    disable_series_detection: bool = False  # skip series promotion in _classify_group
    calib_folder: str = ""                  # last-used calibration folder
    calibrated_threshold: int = 0           # best threshold from last calibration (0 = none)
    calibrated_preview_ratio: float = 0.0  # best preview_ratio from last calibration
    custom_main_folder: str = ""            # Custom Scan: reference folder (never modified)
    custom_check_folder: str = ""           # Custom Scan: folder to search for duplicates
    custom_out_folder: str = ""             # Custom Scan: output/trash folder
    auto_update: bool = True                # Check for updates on startup
    developer_mode: bool = False            # Show full error details / tracebacks
    cross_format_threshold_factor: float = 5.0  # pHash threshold multiplier for RAW vs JPEG pairs
    rotation_threshold_factor: float = 3.0     # pHash threshold multiplier for rotation-matched pairs
    # JPEG DCT re-encoding at a different orientation introduces up to ~6 bits of pHash
    # drift on photo-like images (quality=85).  factor=3.0 → rotation_thr = 2×3 = 6 → 100% coverage.
    dark_mode: bool = False                      # Night theme (dark background)
    scan_speed: int = 5                          # 1=quality → 10=speed (quick-mode quality slider)
    scan_threads: int = 0                        # parallel hashing threads (0 = os.cpu_count())
    # Calibrated on Canon EOS M100 CR2 vs camera JPEG:
    # max intra-group cross-format pHash = 10  (e.g. 001842.cr2 vs 104914.jpg)
    # min inter-group cross-format pHash = 14  → 4-bit safety gap
    # effective cross-format threshold  = 2 × 5 = 10  (covers all true pairs)


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
