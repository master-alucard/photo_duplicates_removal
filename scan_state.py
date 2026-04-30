"""
scan_state.py — Serialization of scan state for pause/resume support.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import imagehash


STATE_VERSION   = 2
RESULTS_VERSION = 1


@dataclass
class ScanState:
    version: int = STATE_VERSION
    source_folder: str = ""
    output_folder: str = ""
    settings_snapshot: dict = field(default_factory=dict)
    phase: str = "hashing"          # "hashing" or "comparing"
    records: list[dict] = field(default_factory=list)   # serialized ImageRecords
    compare_i: int = 0              # outer loop index when paused during comparing
    union_parent: list[int] = field(default_factory=list)


@dataclass
class CustomScanState:
    """Pause/resume state for Compare Scan (two-folder cross-comparison)."""
    version: int = STATE_VERSION
    main_folder: str = ""
    check_folder: str = ""
    output_folder: str = ""
    settings_snapshot: dict = field(default_factory=dict)
    # Phases: "main_hashing", "main_done", "check_hashing",
    #         "check_done", "comparing"
    phase: str = ""
    main_records: list[dict] = field(default_factory=list)
    check_records: list[dict] = field(default_factory=list)
    compare_i: int = 0
    union_parent: list[int] = field(default_factory=list)


def custom_state_path(output_folder: Path) -> Path:
    """Return canonical path for custom_scan_state.json inside output_folder."""
    return output_folder / "custom_scan_state.json"


def save_custom_state(state: CustomScanState, path: Path) -> None:
    """Serialize CustomScanState to JSON."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(state)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        import sys
        print(f"[scan_state] Warning: could not save custom state: {exc}",
              file=sys.stderr)


def load_custom_state(path: Path) -> "Optional[CustomScanState]":
    """Deserialize CustomScanState from JSON.  Returns None if missing/invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != STATE_VERSION:
            return None
        return CustomScanState(
            version=data.get("version", STATE_VERSION),
            main_folder=data.get("main_folder", ""),
            check_folder=data.get("check_folder", ""),
            output_folder=data.get("output_folder", ""),
            settings_snapshot=data.get("settings_snapshot", {}),
            phase=data.get("phase", ""),
            main_records=data.get("main_records", []),
            check_records=data.get("check_records", []),
            compare_i=data.get("compare_i", 0),
            union_parent=data.get("union_parent", []),
        )
    except Exception:
        return None


def delete_custom_state(output_folder: Path) -> None:
    """Delete the custom scan state file if it exists."""
    p = custom_state_path(output_folder)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


def save_state(state: ScanState, path: Path) -> None:
    """Serialize ScanState to JSON at the given path."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(state)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        # Non-fatal — log to stderr and continue
        import sys
        print(f"[scan_state] Warning: could not save state: {exc}", file=sys.stderr)


def load_state(path: Path) -> Optional[ScanState]:
    """Deserialize ScanState from JSON. Returns None if file missing or invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != STATE_VERSION:
            return None  # Incompatible version — discard
        return ScanState(
            version=data.get("version", STATE_VERSION),
            source_folder=data.get("source_folder", ""),
            output_folder=data.get("output_folder", ""),
            settings_snapshot=data.get("settings_snapshot", {}),
            phase=data.get("phase", "hashing"),
            records=data.get("records", []),
            compare_i=data.get("compare_i", 0),
            union_parent=data.get("union_parent", []),
        )
    except Exception:
        return None


def state_path(output_folder: Path) -> Path:
    """Return the canonical path for scan_state.json inside output_folder."""
    return output_folder / "scan_state.json"


def delete_state(output_folder: Path) -> None:
    """Delete the scan state file if it exists."""
    p = state_path(output_folder)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


# ── DuplicateGroup <-> dict serialization ────────────────────────────────────

def serialize_group(grp) -> dict:
    return {
        "originals":   [serialize_record(r) for r in grp.originals],
        "previews":    [serialize_record(r) for r in grp.previews],
        "is_series":   grp.is_series,
        "is_ambiguous": grp.is_ambiguous,
        "group_id":    grp.group_id,
    }


def deserialize_group(data: dict):
    from scanner import DuplicateGroup
    return DuplicateGroup(
        originals=[deserialize_record(r) for r in data.get("originals", [])],
        previews=[deserialize_record(r) for r in data.get("previews", [])],
        is_series=data.get("is_series", False),
        is_ambiguous=data.get("is_ambiguous", False),
        group_id=data.get("group_id", ""),
    )


# ── Completed-scan results persistence ───────────────────────────────────────

def results_path(output_folder: Path) -> Path:
    """Canonical path for the completed-scan results file."""
    return output_folder / "scan_results.json"


def save_results(
    groups: list,
    solo_originals: list,
    broken_files: list,
    total_scanned: int,
    output_folder: Path,
    src_folder: str = "",
    dry_run: bool = True,
    report_html: str = "",
) -> None:
    """Persist completed scan results so they can be restored after app restart."""
    path = results_path(output_folder)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version":       RESULTS_VERSION,
            "src_folder":    src_folder,
            "out_folder":    str(output_folder),
            "total_scanned": total_scanned,
            "dry_run":       dry_run,
            "report_html":   report_html,
            "groups":        [serialize_group(g) for g in groups],
            "solo_originals": [serialize_record(r) for r in solo_originals],
            "broken_files":  [str(p) for p in broken_files],
        }
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        import sys
        print(f"[scan_state] Warning: could not save results: {exc}", file=sys.stderr)


def load_results(output_folder: Path) -> "Optional[dict]":
    """Load persisted scan results. Returns None if missing, corrupt, or wrong version."""
    path = results_path(output_folder)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != RESULTS_VERSION:
            return None
        return {
            "src_folder":    data.get("src_folder", ""),
            "out_folder":    data.get("out_folder", str(output_folder)),
            "total_scanned": data.get("total_scanned", 0),
            "dry_run":       data.get("dry_run", True),
            "report_html":   data.get("report_html", ""),
            "groups":        [deserialize_group(g) for g in data.get("groups", [])],
            "solo_originals": [deserialize_record(r) for r in data.get("solo_originals", [])],
            "broken_files":  [Path(p) for p in data.get("broken_files", [])],
        }
    except Exception:
        return None


def delete_results(output_folder: Path) -> None:
    """Delete the results file after the user applies / moves files."""
    p = results_path(output_folder)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


# ── Custom (Compare Scan) results persistence ────────────────────────────────

def custom_results_path(output_folder: Path) -> Path:
    """Canonical path for the completed Compare Scan results file."""
    return output_folder / "custom_scan_results.json"


def save_custom_results(
    groups: list,
    broken_files: list,
    total_main: int,
    total_check: int,
    output_folder: Path,
    main_folder: str = "",
    check_folder: str = "",
    dry_run: bool = True,
    report_html: str = "",
) -> None:
    """Persist completed Compare Scan results so they can be restored after app restart."""
    path = custom_results_path(output_folder)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version":       RESULTS_VERSION,
            "main_folder":   main_folder,
            "check_folder":  check_folder,
            "out_folder":    str(output_folder),
            "total_main":    total_main,
            "total_check":   total_check,
            "dry_run":       dry_run,
            "report_html":   report_html,
            "groups":        [serialize_group(g) for g in groups],
            "broken_files":  [str(p) for p in broken_files],
        }
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        import sys
        print(f"[scan_state] Warning: could not save custom results: {exc}", file=sys.stderr)


def load_custom_results(output_folder: Path) -> "Optional[dict]":
    """Load persisted Compare Scan results. Returns None if missing or corrupt."""
    path = custom_results_path(output_folder)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != RESULTS_VERSION:
            return None
        return {
            "main_folder":  data.get("main_folder", ""),
            "check_folder": data.get("check_folder", ""),
            "out_folder":   data.get("out_folder", str(output_folder)),
            "total_main":   data.get("total_main", 0),
            "total_check":  data.get("total_check", 0),
            "dry_run":      data.get("dry_run", True),
            "report_html":  data.get("report_html", ""),
            "groups":       [deserialize_group(g) for g in data.get("groups", [])],
            "broken_files": [Path(p) for p in data.get("broken_files", [])],
        }
    except Exception:
        return None


def delete_custom_results(output_folder: Path) -> None:
    """Delete the custom results file."""
    p = custom_results_path(output_folder)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


# ── ImageRecord <-> dict serialization ──────────────────────────────────────

def serialize_record(rec) -> dict:
    """
    Convert an ImageRecord to a JSON-serializable dict.
    Stores phash/dhash as hex strings.
    """
    return {
        "path": str(rec.path),
        "width": rec.width,
        "height": rec.height,
        "file_size": rec.file_size,
        "phash": str(rec.phash),
        "dhash": str(rec.dhash),
        "mtime": rec.mtime,
        "brightness": rec.brightness,
        "histogram": rec.histogram,
        "companions": [str(c) for c in rec.companions],
        "metadata_count": rec.metadata_count,
        "phash_r90":  str(rec.phash_r90)  if rec.phash_r90  is not None else None,
        "phash_r180": str(rec.phash_r180) if rec.phash_r180 is not None else None,
        "phash_r270": str(rec.phash_r270) if rec.phash_r270 is not None else None,
        "is_video":   getattr(rec, "is_video", False),
    }


def deserialize_record(data: dict):
    """
    Reconstruct an ImageRecord from a serialized dict.
    Imports ImageRecord from scanner to avoid circular imports.
    """
    from scanner import ImageRecord  # local import to avoid circular
    _r90  = data.get("phash_r90")
    _r180 = data.get("phash_r180")
    _r270 = data.get("phash_r270")
    return ImageRecord(
        path=Path(data["path"]),
        width=data["width"],
        height=data["height"],
        file_size=data["file_size"],
        phash=imagehash.hex_to_hash(data["phash"]),
        dhash=imagehash.hex_to_hash(data["dhash"]),
        mtime=data["mtime"],
        brightness=data["brightness"],
        histogram=data["histogram"],
        companions=[Path(c) for c in data.get("companions", [])],
        metadata_count=data.get("metadata_count", 0),
        phash_r90  = imagehash.hex_to_hash(_r90)  if _r90  else None,
        phash_r180 = imagehash.hex_to_hash(_r180) if _r180 else None,
        phash_r270 = imagehash.hex_to_hash(_r270) if _r270 else None,
        is_video   = bool(data.get("is_video", False)),
    )
