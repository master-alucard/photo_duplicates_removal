"""
metadata.py — EXIF metadata reading, date extraction, and export utilities.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image
from PIL.ExifTags import TAGS


def read_exif(path: Path) -> dict:
    """Read EXIF/metadata from image using Pillow. Returns dict of tag_name->value."""
    result: dict = {}
    try:
        with Image.open(path) as img:
            exif_data = img._getexif()  # type: ignore[attr-defined]
            if exif_data:
                for tag_id, value in exif_data.items():
                    tag_name = TAGS.get(tag_id, str(tag_id))
                    # Convert bytes to string for JSON safety
                    if isinstance(value, bytes):
                        try:
                            value = value.decode("utf-8", errors="replace")
                        except Exception:
                            value = repr(value)
                    # Convert IFDRational to float
                    try:
                        from PIL.TiffImagePlugin import IFDRational
                        if isinstance(value, IFDRational):
                            value = float(value)
                    except ImportError:
                        pass
                    # Convert tuples of rationals
                    if isinstance(value, tuple):
                        converted = []
                        for v in value:
                            try:
                                from PIL.TiffImagePlugin import IFDRational
                                if isinstance(v, IFDRational):
                                    converted.append(float(v))
                                else:
                                    converted.append(v)
                            except ImportError:
                                converted.append(v)
                        value = converted
                    result[tag_name] = value
    except Exception:
        pass
    return result


def count_metadata_fields(path: Path) -> int:
    """Return number of non-empty EXIF fields. Used for tie-breaking originals."""
    try:
        exif = read_exif(path)
        return sum(1 for v in exif.values() if v is not None and v != "" and v != b"")
    except Exception:
        return 0


def extract_date_from_exif(path: Path) -> Optional[datetime]:
    """Return DateTimeOriginal or DateTime from EXIF, or None if not found."""
    try:
        exif = read_exif(path)
        for field_name in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
            value = exif.get(field_name)
            if value and isinstance(value, str):
                # EXIF date format: "2024:03:15 12:00:00"
                try:
                    return datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
                # Try alternative formats
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                    try:
                        return datetime.strptime(value.strip(), fmt)
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


def extract_date_from_filename(filename: str) -> Optional[datetime]:
    """
    Regex-detect dates in filename.
    Handles: 2024-03-15, 20240315, IMG_20240315_120000, DSC_20240315,
             Screenshot_2024-03-15, VID_20240315_120000, etc.
    """
    patterns = [
        # ISO format with time: 2024-03-15_12-00-00 or 2024-03-15 12:00:00
        (r"(\d{4})[_\-](\d{2})[_\-](\d{2})[_\- T](\d{2})[_:\-](\d{2})[_:\-](\d{2})",
         lambda m: datetime(int(m[0]), int(m[1]), int(m[2]),
                            int(m[3]), int(m[4]), int(m[5]))),
        # Compact with time: 20240315_120000 or 20240315120000
        (r"(\d{4})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})",
         lambda m: datetime(int(m[0]), int(m[1]), int(m[2]),
                            int(m[3]), int(m[4]), int(m[5]))),
        # ISO date only: 2024-03-15
        (r"(\d{4})[_\-](\d{2})[_\-](\d{2})",
         lambda m: datetime(int(m[0]), int(m[1]), int(m[2]))),
        # Compact date only: 20240315
        (r"(\d{4})(\d{2})(\d{2})",
         lambda m: datetime(int(m[0]), int(m[1]), int(m[2]))),
    ]

    for pattern, constructor in patterns:
        match = re.search(pattern, filename)
        if match:
            try:
                groups = match.groups()
                dt = constructor(groups)
                # Sanity check: year 1990-2040, month 1-12, day 1-31
                if 1990 <= dt.year <= 2040 and 1 <= dt.month <= 12 and 1 <= dt.day <= 31:
                    return dt
            except (ValueError, TypeError):
                continue
    return None


def save_metadata_json(groups: list, output_folder: Path) -> None:
    """Write metadata/<group_id>.json for each group."""
    metadata_dir = output_folder / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    for group in groups:
        group_id = getattr(group, "group_id", "unknown")
        group_data = {
            "group_id": group_id,
            "is_series": getattr(group, "is_series", False),
            "originals": [],
            "previews": [],
        }

        for rec in group.originals:
            group_data["originals"].append(_record_to_dict(rec))
        for rec in group.previews:
            group_data["previews"].append(_record_to_dict(rec))

        out_path = metadata_dir / f"{group_id}.json"
        try:
            out_path.write_text(
                json.dumps(group_data, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass


def export_metadata_csv(groups: list, output_folder: Path) -> None:
    """Write metadata_export.csv with all group metadata."""
    csv_path = output_folder / "metadata_export.csv"

    fieldnames = [
        "group_id", "is_series", "role", "filename", "path",
        "width", "height", "pixels", "file_size_kb", "mtime",
        "brightness", "metadata_count", "companions",
        "camera_make", "camera_model", "lens_model", "iso",
        "aperture", "shutter_speed", "focal_length",
        "date_taken", "gps_lat", "gps_lon",
    ]

    rows = []
    for group in groups:
        group_id = getattr(group, "group_id", "unknown")
        is_series = getattr(group, "is_series", False)

        for role, records in (("original", group.originals), ("preview", group.previews)):
            for rec in records:
                row = _record_to_csv_row(rec, group_id, is_series, role)
                rows.append(row)

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except Exception:
        pass


# ── internal helpers ─────────────────────────────────────────────────────────

def _record_to_dict(rec) -> dict:
    """Convert an ImageRecord to a JSON-serializable dict."""
    path = getattr(rec, "path", None)
    companions = getattr(rec, "companions", [])
    return {
        "path": str(path) if path else "",
        "filename": path.name if path else "",
        "width": getattr(rec, "width", 0),
        "height": getattr(rec, "height", 0),
        "file_size": getattr(rec, "file_size", 0),
        "mtime": getattr(rec, "mtime", 0.0),
        "brightness": round(getattr(rec, "brightness", 0.0), 2),
        "metadata_count": getattr(rec, "metadata_count", 0),
        "companions": [str(c) for c in companions],
    }


def _record_to_csv_row(rec, group_id: str, is_series: bool, role: str) -> dict:
    """Convert an ImageRecord to a flat dict for CSV export."""
    path = getattr(rec, "path", None)
    companions = getattr(rec, "companions", [])
    width = getattr(rec, "width", 0)
    height = getattr(rec, "height", 0)
    file_size = getattr(rec, "file_size", 0)

    row: dict = {
        "group_id": group_id,
        "is_series": is_series,
        "role": role,
        "filename": path.name if path else "",
        "path": str(path) if path else "",
        "width": width,
        "height": height,
        "pixels": width * height,
        "file_size_kb": round(file_size / 1024, 1) if file_size else 0,
        "mtime": getattr(rec, "mtime", 0.0),
        "brightness": round(getattr(rec, "brightness", 0.0), 2),
        "metadata_count": getattr(rec, "metadata_count", 0),
        "companions": "; ".join(str(c) for c in companions),
        "camera_make": "",
        "camera_model": "",
        "lens_model": "",
        "iso": "",
        "aperture": "",
        "shutter_speed": "",
        "focal_length": "",
        "date_taken": "",
        "gps_lat": "",
        "gps_lon": "",
    }

    # Try to pull EXIF fields if path exists
    if path and path.exists():
        try:
            exif = read_exif(path)
            row["camera_make"] = exif.get("Make", "")
            row["camera_model"] = exif.get("Model", "")
            row["lens_model"] = exif.get("LensModel", exif.get("Lens", ""))
            row["iso"] = exif.get("ISOSpeedRatings", "")
            _ap = exif.get("FNumber")
            if _ap:
                try:
                    row["aperture"] = f"f/{float(_ap):.1f}"
                except Exception:
                    row["aperture"] = str(_ap)
            _ss = exif.get("ExposureTime")
            if _ss:
                try:
                    val = float(_ss)
                    if val < 1:
                        row["shutter_speed"] = f"1/{int(round(1/val))}s"
                    else:
                        row["shutter_speed"] = f"{val:.1f}s"
                except Exception:
                    row["shutter_speed"] = str(_ss)
            _fl = exif.get("FocalLength")
            if _fl:
                try:
                    row["focal_length"] = f"{float(_fl):.0f}mm"
                except Exception:
                    row["focal_length"] = str(_fl)
            for dt_field in ("DateTimeOriginal", "DateTime"):
                val = exif.get(dt_field)
                if val:
                    row["date_taken"] = str(val)
                    break
        except Exception:
            pass

    return row
