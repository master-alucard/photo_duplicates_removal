"""
scan_state.py — Serialization of scan state for pause/resume support.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import imagehash


STATE_VERSION = 2


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
    }


def deserialize_record(data: dict):
    """
    Reconstruct an ImageRecord from a serialized dict.
    Imports ImageRecord from scanner to avoid circular imports.
    """
    from scanner import ImageRecord  # local import to avoid circular
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
    )
