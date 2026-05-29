"""
config.py — Settings dataclass with JSON persistence.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Increment this when a default value changes for a field that has no UI control
# (i.e. the user cannot change it via the Settings panel).  load_settings uses
# this to detect stale persisted values and overwrite them with the new defaults
# rather than silently running the app with the old wrong value.
#
# Version history:
#   0  — initial (no version field in file); raw_use_embedded_thumb defaulted False,
#         cross_format_threshold_factor had no stable default (was tuned by calib runs)
#   1  — raw_use_embedded_thumb default changed to True (commit 925431d);
#         cross_format_threshold_factor locked to 6.0 (commit 3233345/5f63627)
_SETTINGS_VERSION = 1


@dataclass
class Settings:
    # Internal schema version.  Written to every saved file so load_settings can
    # migrate stale on-disk values when defaults change.  Not shown in the UI.
    settings_version: int = _SETTINGS_VERSION
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
    # ── Video duplicate detection ──────────────────────────────────────────────
    # Default ON so videos are no longer silently skipped (issue #300).  Out-of-the-box
    # behaviour: bucket video files by (extension, file size) — sufficient to detect
    # the most common case of the same clip copied twice in a personal library.
    include_videos: bool = True             # Also scan for video duplicates
    video_use_thumb: bool = True            # Compare thumbnail frames when sizes match
    # Compare Conditions for videos (issue #300).  At least one of these must be ON
    # for video grouping to produce any results.  Both default ON.
    video_match_format: bool = True         # Two videos must share the same file extension
    video_match_size: bool = True           # Two videos must have an identical byte count
    # Content-based matching: find duplicates even when byte sizes differ (re-encoded,
    # different container, trimmed metadata).  Uses multi-frame pHash + duration proximity.
    # Interaction with other flags:
    #   video_match_content=True  — runs a content pass across ALL videos regardless of size.
    #                                The size-first pass still runs as a fast exact-match check.
    #   video_match_format=True   — when content matching is also ON, cross-format duplicates
    #                                (e.g. .mp4 vs .mov with same content) are still found.
    #                                Set video_match_format=False to detect those too.
    #   video_match_size=False    — disables the fast exact-size pre-pass; content matching
    #                                then handles everything (slower but more thorough).
    video_match_content: bool = True        # Find content-duplicate videos even if sizes differ
    # ── Merge tab ─────────────────────────────────────────────────────────────
    # All merge_* fields are owned by the Merge tab and are completely independent
    # of the duplicate-removal scan pipeline.
    merge_main_folder: str = ""               # consolidation target folder
    merge_source_folders: list = field(default_factory=list)  # list of source folder strings
    merge_mode: str = "destructive"           # "destructive" | "nondestructive"
    merge_keep_subfolder: bool = False        # True = preserve relative path structure
    merge_recursive: bool = True              # scan source subfolders recursively
    merge_include_videos: bool = False        # include video files in merge scan
    merge_move_sidecars: bool = True          # co-locate .xmp/.aae companions
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
    raw_use_embedded_thumb: bool = True          # Use rawpy.extract_thumb() for RAW hashing: 30-80x faster, and produces pHash that matches the camera-generated JPEG (same tone curve).
                                                 # Note: changes pHash values vs the old postprocess default — existing RAW cache entries hashed before this change will be treated as stale
                                                 # (mtime/size check in library.py detects the mismatch).  False = use rawpy.postprocess() demosaic (legacy behaviour, ~30 bit pHash offset).
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
    """Load settings from a JSON file, applying migrations for default changes.

    Returns fresh defaults if the file does not exist or cannot be parsed.

    Migration policy
    ----------------
    When a field's default value changes in code but the field has no UI control
    (the user cannot change it through the Settings panel), stale on-disk values
    would silently override the new default and break detection.  We detect this
    by comparing the file's ``settings_version`` to ``_SETTINGS_VERSION``:

    - ``settings_version`` absent or 0 → v0 file (written before versioning).
      Reset all v0→v1 migrated fields to their current code defaults.
    - ``settings_version`` == current → nothing to do; apply as-is.
    - ``settings_version`` > current → written by a newer build; unknown fields
      are already filtered out by the known-field guard below.
    """
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Only apply known fields; ignore unknowns for forward-compatibility
        known = {f for f in Settings.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        s = Settings(**filtered)
        file_version = data.get("settings_version", 0)
        _migrate(s, data)
        if file_version < _SETTINGS_VERSION:
            save_settings(s, path)
        return s
    except Exception:
        return Settings()


def _migrate(s: Settings, raw_data: dict) -> None:
    """Apply in-place migrations based on the on-disk settings_version.

    ``raw_data`` is the raw dict read from JSON (used to detect absent keys).
    """
    file_version = raw_data.get("settings_version", 0)
    defaults = Settings.__new__(Settings)
    # Initialise defaults without calling __init__ so we can read field defaults
    # directly from the class without side effects.
    defaults = Settings()

    if file_version < 1:
        # v0 → v1 migrations
        # ── raw_use_embedded_thumb ──────────────────────────────────────────
        # Default changed from False to True in commit 925431d.  The field has
        # no UI control; any False in a v0 file is the old default, not an
        # intentional user choice.  Reset to the new default (True).
        if not s.raw_use_embedded_thumb:
            s.raw_use_embedded_thumb = defaults.raw_use_embedded_thumb  # True
        # ── cross_format_threshold_factor ───────────────────────────────────
        # Was never settable via the UI.  Calibration runs or manual JSON edits
        # may have left 2.0 in the file (the value used during iteration 3-4
        # calibration).  The correct production default is 6.0 (covers all true
        # RAW+JPEG pairs with an 8-bit safety gap to the inter-group minimum).
        # Any value below 4.0 produces a CF threshold of < 8 bits, which misses
        # pairs where rawpy postprocess gives up to 6-bit pHash drift.
        if s.cross_format_threshold_factor < 4.0:
            s.cross_format_threshold_factor = defaults.cross_format_threshold_factor  # 6.0
        # Stamp the migrated version so the next save writes v1
        s.settings_version = _SETTINGS_VERSION


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
