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
    Returns (moved_count, error_count).
    Writes operations_log.json in output_folder.

    Date-based organizing lives in its own tab — see
    ``organize_by_date_standalone`` below.  The ``settings`` parameter is
    accepted but unused; kept for call-site compatibility.
    """
    trash_dir = output_folder / "trash"

    if not dry_run:
        trash_dir.mkdir(parents=True, exist_ok=True)

    moved_previews = 0
    error_count = 0
    operations: list[dict] = []

    for group in groups:
        # Ambiguous groups are flagged for manual review — never move their files
        if getattr(group, "is_ambiguous", False):
            continue

        group_id = getattr(group, "group_id", "unknown")

        for preview in group.previews:
            if not preview.path.exists():
                op = {
                    "group_id": group_id,
                    "type": "preview",
                    "from": str(preview.path),
                    "to": "",
                    "status": "skipped: file not found",
                }
                operations.append(op)
                error_count += 1
                continue

            dest = _unique_path(trash_dir / preview.path.name)
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
                    error_count += 1
            else:
                op["status"] = "dry_run"
                moved_previews += 1
            operations.append(op)

    if not dry_run:
        _write_ops_log(operations, output_folder)

    return moved_previews, error_count


# ── Standalone "Organize by Date" engine ─────────────────────────────────────
# Used by the dedicated Organize-by-Date tab.  No coupling to the duplicate
# pipeline — operates directly on a source folder, optional destination
# folder, and a small set of explicit knobs.

# Sidecar extensions that travel with their image (RAW edits, iPhone .aae, etc.)
SIDECAR_EXTENSIONS = {".xmp", ".aae"}

# RAW formats the organize tab can include alongside images.  Mirrors the set
# in scanner.RAW_EXTENSIONS so the two stay in sync without a hard import.
ORGANIZE_RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf",
    ".rw2", ".pef", ".srw", ".x3f", ".3fr",
}

# Image extensions copied from scanner.IMAGE_EXTENSIONS to avoid a circular
# import — these are the formats Pillow can open natively.
ORGANIZE_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif",
    ".gif", ".heic", ".heif",
}


def _resolve_date(
    path: Path,
    use_exif: bool,
    use_filename: bool,
    use_mtime: bool,
):
    """Return the first detectable date for *path* per configured priority,
    or None if every enabled source returns nothing."""
    import datetime as _dt
    from metadata import extract_date_from_exif, extract_date_from_filename

    if use_exif and path.exists():
        try:
            dt = extract_date_from_exif(path)
            if dt:
                return dt
        except Exception:
            pass
    if use_filename:
        try:
            dt = extract_date_from_filename(path.name)
            if dt:
                return dt
        except Exception:
            pass
    if use_mtime:
        try:
            return _dt.datetime.fromtimestamp(path.stat().st_mtime)
        except Exception:
            pass
    return None


def _resolve_conflict(dest: Path, policy: str) -> "Path | None":
    """
    Apply the conflict policy when *dest* already exists.

    Returns:
      * a Path to write to ("rename" appends _1, _2, …; "overwrite" returns
        ``dest`` unchanged after deleting the existing file)
      * None when policy is "skip" and the destination already exists
        (caller should record as skipped and move on).
    """
    if not dest.exists():
        return dest
    if policy == "skip":
        return None
    if policy == "overwrite":
        try:
            dest.unlink()
        except Exception:
            pass
        return dest
    # default → rename
    return _unique_path(dest)


def organize_by_date_standalone(
    src_folder: Path,
    out_folder: "Path | None",
    *,
    in_place: bool,
    operation: str,                  # "move" | "copy"
    date_format: str,
    use_exif: bool,
    use_filename: bool,
    use_mtime: bool,
    unknown_folder: str,
    conflict_policy: str,            # "rename" | "skip" | "overwrite"
    recursive: bool,
    include_raw: bool,
    move_sidecars: bool,
    dry_run: bool,
    progress_cb=None,                # progress(msg, done, total, phase_name)
    stop_flag: "list[bool] | None" = None,
) -> dict:
    """
    Walk *src_folder*, classify each image by date, and move or copy it into
    a date-named subfolder.

    Returns a result dict with counters and an operations list.  Writes
    ``operations_log.json`` next to the chosen destination root so the
    standard :func:`revert_operations` continues to work.
    """
    src_folder = Path(src_folder)
    if not src_folder.exists() or not src_folder.is_dir():
        raise FileNotFoundError(f"Source folder not found: {src_folder}")

    # Resolve destination root
    if in_place:
        dest_root = src_folder
    else:
        if out_folder is None:
            raise ValueError("out_folder is required when in_place=False")
        dest_root = Path(out_folder)
        if not dry_run:
            dest_root.mkdir(parents=True, exist_ok=True)

    # Build the file extension whitelist
    exts = set(ORGANIZE_IMAGE_EXTENSIONS)
    if include_raw:
        exts |= ORGANIZE_RAW_EXTENSIONS

    # ── Phase 1: discover candidate files ────────────────────────────────
    if progress_cb:
        progress_cb("Scanning source folder…", 0, 0, "Scanning")

    files: list[Path] = []
    if recursive:
        iterator = src_folder.rglob("*")
    else:
        iterator = src_folder.iterdir()

    # Skip date-target subfolders that we'd create ourselves so re-running
    # the operation in-place doesn't churn already-organized files.
    for p in iterator:
        if stop_flag and stop_flag[0]:
            break
        try:
            if not p.is_file():
                continue
            if p.suffix.lower() not in exts:
                continue
            files.append(p)
        except Exception:
            continue

    total = len(files)
    if total == 0:
        if progress_cb:
            progress_cb("No images found.", 0, 0, "Done")
        return {
            "scanned": 0, "moved": 0, "copied": 0, "skipped": 0,
            "errors": 0, "no_date": 0, "operations": [], "dry_run": dry_run,
            "dest_root": str(dest_root),
        }

    # ── Phase 2: organize ────────────────────────────────────────────────
    operations: list[dict] = []
    moved = copied = skipped = errors = no_date = 0
    sidecar_count = 0

    for i, src_path in enumerate(files):
        if stop_flag and stop_flag[0]:
            break
        if progress_cb and (i % 25 == 0 or i == total - 1):
            progress_cb(f"Organizing {i + 1:,} / {total:,}…", i, total, "Organizing")

        dt = _resolve_date(src_path, use_exif, use_filename, use_mtime)
        if dt is None:
            sub = unknown_folder or "unknown_date"
            no_date += 1
        else:
            try:
                sub = dt.strftime(date_format)
            except Exception:
                sub = unknown_folder or "unknown_date"
                no_date += 1

        dest_dir = dest_root / sub
        dest = dest_dir / src_path.name

        # Skip when source is already in the correct folder
        if dest_dir.resolve() == src_path.parent.resolve():
            skipped += 1
            operations.append({
                "type": "organize", "op": operation,
                "from": str(src_path), "to": str(dest),
                "status": "skipped: already in target folder",
            })
            continue

        resolved = _resolve_conflict(dest, conflict_policy)
        if resolved is None:
            skipped += 1
            operations.append({
                "type": "organize", "op": operation,
                "from": str(src_path), "to": str(dest),
                "status": "skipped: target exists (conflict=skip)",
            })
            continue
        dest = resolved

        op = {
            "type": "organize", "op": operation,
            "from": str(src_path), "to": str(dest),
        }
        if dry_run:
            op["status"] = "dry_run"
            if operation == "move":
                moved += 1
            else:
                copied += 1
            operations.append(op)
        else:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if operation == "copy":
                    shutil.copy2(str(src_path), str(dest))
                    copied += 1
                else:
                    shutil.move(str(src_path), str(dest))
                    moved += 1
                op["status"] = operation + "d"
                operations.append(op)

                # Co-locate sidecars (xmp / aae)
                if move_sidecars:
                    for ext in SIDECAR_EXTENSIONS:
                        sib = src_path.with_suffix(ext)
                        if not sib.exists():
                            # Try uppercase too (camera default on some bodies)
                            sib_u = src_path.with_suffix(ext.upper())
                            sib = sib_u if sib_u.exists() else None
                        if sib is None or not sib.exists():
                            continue
                        sib_dest = _unique_path(dest_dir / sib.name)
                        try:
                            if operation == "copy":
                                shutil.copy2(str(sib), str(sib_dest))
                            else:
                                shutil.move(str(sib), str(sib_dest))
                            sidecar_count += 1
                            operations.append({
                                "type": "organize_sidecar", "op": operation,
                                "from": str(sib), "to": str(sib_dest),
                                "status": operation + "d",
                            })
                        except Exception as exc:
                            operations.append({
                                "type": "organize_sidecar", "op": operation,
                                "from": str(sib), "to": str(sib_dest),
                                "status": f"error: {exc}",
                            })
            except Exception as exc:
                op["status"] = f"error: {exc}"
                errors += 1
                operations.append(op)

    if not dry_run and operations:
        _write_ops_log(operations, dest_root)

    if progress_cb:
        progress_cb("Done.", total, total, "Done")

    return {
        "scanned": total,
        "moved": moved,
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
        "no_date": no_date,
        "sidecars": sidecar_count,
        "operations": operations,
        "dry_run": dry_run,
        "dest_root": str(dest_root),
    }


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
