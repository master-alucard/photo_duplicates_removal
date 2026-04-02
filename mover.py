"""
mover.py — Move duplicate previews -> trash/ only. Originals are never touched.
Updates ImageRecord.path in-place so the reporter sees new locations.
Writes operations_log.json and supports revert.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional


OPS_LOG_VERSION = 1


def _date_subfolder(path: Path, fmt: str) -> str:
    """Return a date-based subfolder name for a file (e.g. '2024-03')."""
    import datetime
    from metadata import extract_date_from_exif, extract_date_from_filename
    dt = None
    if path.exists():
        dt = extract_date_from_exif(path)
    if dt is None:
        dt = extract_date_from_filename(path.name)
    if dt is None:
        try:
            dt = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        except Exception:
            return "unknown_date"
    try:
        return dt.strftime(fmt)
    except Exception:
        return "unknown_date"


def trash_files(
    paths: List[Path],
    trash_dir: Path,
    dry_run: bool = False,
) -> tuple[int, list]:
    """
    Move explicit file paths to trash_dir.
    Originals are never touched — only the caller-selected paths are moved.
    Returns (moved_count, error_list).
    Appends operations to operations_log.json in trash_dir.parent.
    """
    if not dry_run:
        trash_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    errors: list[str] = []
    operations: list[dict] = []

    for p in paths:
        if not p.exists():
            errors.append(f"{p.name}: file not found")
            continue
        dest = _unique_path(trash_dir / p.name)
        op = {"type": "trash", "from": str(p), "to": str(dest)}
        if not dry_run:
            try:
                shutil.move(str(p), str(dest))
                moved += 1
                op["status"] = "moved"
            except Exception as exc:
                errors.append(f"{p.name}: {exc}")
                op["status"] = f"error: {exc}"
        else:
            moved += 1
            op["status"] = "dry_run"
        operations.append(op)

    if not dry_run and operations:
        _write_ops_log(operations, trash_dir.parent)

    return moved, errors


def move_groups(
    groups: List,
    output_folder: Path,
    dry_run: bool = False,
    settings=None,
) -> tuple[int, int]:
    """
    Move duplicate previews to trash/ only. Originals are never touched.
    Returns (0, moved_previews).
    Writes operations_log.json in output_folder.
    """
    trash_dir = output_folder / "trash"

    if not dry_run:
        trash_dir.mkdir(parents=True, exist_ok=True)

    moved_previews = 0
    operations: list[dict] = []

    _by_date = settings and getattr(settings, "organize_by_date", False)
    _date_fmt = getattr(settings, "date_folder_format", "%Y-%m") if settings else "%Y-%m"

    def _dest_dir(base: Path, file_path: Path) -> Path:
        if _by_date:
            sub = _date_subfolder(file_path, _date_fmt)
            d = base / sub
            if not dry_run:
                d.mkdir(parents=True, exist_ok=True)
            return d
        return base

    for group in groups:
        # Ambiguous groups are flagged for manual review — never move their files
        if getattr(group, "is_ambiguous", False):
            continue

        group_id = getattr(group, "group_id", "unknown")

        for preview in group.previews:
            dest = _unique_path(_dest_dir(trash_dir, preview.path) / preview.path.name)
            op = {
                "group_id": group_id,
                "type": "preview",
                "from": str(preview.path),
                "to": str(dest),
            }
            if not dry_run:
                try:
                    shutil.move(str(preview.path), str(dest))
                    preview.path = dest
                    moved_previews += 1
                    op["status"] = "moved"
                except Exception as exc:
                    op["status"] = f"error: {exc}"
            else:
                op["status"] = "dry_run"
                moved_previews += 1
            operations.append(op)

    if not dry_run:
        _write_ops_log(operations, output_folder)

    return 0, moved_previews


def revert_operations(
    ops_log_path: Path,
    group_ids: Optional[list[str]] = None,
) -> tuple[int, int]:
    """
    Revert file moves recorded in operations_log.json.

    group_ids=None reverts all groups.
    Returns (reverted_count, error_count).
    """
    if not ops_log_path.exists():
        return 0, 0

    try:
        data = json.loads(ops_log_path.read_text(encoding="utf-8"))
        operations = data.get("operations", [])
    except Exception:
        return 0, 0

    reverted = 0
    errors = 0

    for op in operations:
        if op.get("status") != "moved":
            continue
        if group_ids is not None and op.get("group_id") not in group_ids:
            continue

        src = Path(op["to"])    # where it currently is
        dst = Path(op["from"])  # where it should go back

        if not src.exists():
            errors += 1
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dest = _unique_path(dst)
            shutil.move(str(src), str(dest))
            reverted += 1
        except Exception:
            errors += 1

    return reverted, errors


def ops_log_path(output_folder: Path) -> Path:
    """Return the canonical path for operations_log.json."""
    return output_folder / "operations_log.json"


def _write_ops_log(operations: list[dict], output_folder: Path) -> None:
    """Write operations_log.json to output_folder."""
    log = {
        "version": OPS_LOG_VERSION,
        "timestamp": datetime.now().isoformat(),
        "operations": operations,
    }
    log_path = ops_log_path(output_folder)
    try:
        log_path.write_text(
            json.dumps(log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _unique_path(path: Path) -> Path:
    """Return path if it doesn't exist, otherwise add _1, _2, ... suffix."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
