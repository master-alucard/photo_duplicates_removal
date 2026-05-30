"""
merger.py -- Merge-tab planner and executor.

The PLANNER (build_merge_plan) is pure logic: given image records, options, and
mode it returns a MergePlan.  No I/O, fully unit-testable.

The EXECUTOR (MergeExecutor) applies a plan: moves/copies files, updates the
library cache, writes operations_log.json, and honours drive-disconnect pause.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MergeFileOp:
    """One planned file operation."""
    action: str          # "move" | "copy"
    src: Path
    dst: Path
    group_id: str = ""   # duplicate group id, or "" for unique files
    role: str = ""       # "original" | "duplicate" | "unique"
    renamed: bool = False  # True when dst stem was suffixed to avoid collision


@dataclass
class MergePlan:
    """Complete plan produced by the planner; consumed by the executor."""
    mode: str                          # "destructive" | "nondestructive"
    main_folder: Path
    source_folders: List[Path]
    ops: List[MergeFileOp] = field(default_factory=list)

    # Mode B: per-source trash lists  str(src_folder) -> [paths to trash]
    source_trash: dict = field(default_factory=dict)

    # Summary counters (filled by build_merge_plan)
    n_unique: int = 0
    n_groups: int = 0
    n_to_main: int = 0
    n_suffix_renames: int = 0
    # Cumulative file size that the merge will TOUCH on the main drive:
    # - Non-destructive (copy) mode: this is the new disk usage in main.
    # - Destructive (move) mode same drive: bytes are effectively
    #   re-parented, not duplicated, so "added to main" overstates by the
    #   in-source size; the UI shows it as "Total size" honestly.
    # - Destructive cross-drive: copy+delete, so the bytes ARE written to
    #   main before being unlinked from the source.
    space_delta: int = 0


# ---------------------------------------------------------------------------
# Planner helpers
# ---------------------------------------------------------------------------

def _is_in_folder(path: Path, folder: Path) -> bool:
    """Return True when path is inside folder.

    Uses pure-string normalisation rather than `Path.resolve()` — on Windows
    `resolve()` does parent-traversal I/O even for non-existent paths, and
    this helper is called per-record in the planner's hot loops.  The
    semantic trade-off: symlinks pointing OUT of a folder will not be
    detected as outside (since we don't follow symlinks here) — acceptable
    for photo libraries where symlink traversal is rare.
    """
    try:
        norm_path = os.path.normcase(os.path.normpath(str(path)))
        norm_folder = os.path.normcase(os.path.normpath(str(folder)))
        sep = os.sep
        # Treat folder as a prefix; add trailing sep so /foo doesn't match /foobar
        if not norm_folder.endswith(sep):
            norm_folder_pref = norm_folder + sep
        else:
            norm_folder_pref = norm_folder
        return norm_path == norm_folder.rstrip(sep) or norm_path.startswith(norm_folder_pref)
    except Exception:
        return False


def _pick_original(
    group_paths: List[Path],
    main_folder: Path,
    keep_strategy: str,
    records_by_path: dict,
) -> Path:
    """Pick the canonical original from a duplicate group.

    Priority:
      1. Any member already inside main_folder (pre-existing main file wins).
      2. keep_strategy: 'pixels' -> largest resolution; 'oldest' -> oldest mtime.
    """
    main_members = [p for p in group_paths if _is_in_folder(p, main_folder)]
    if main_members:
        return main_members[0]

    def _pixels(p: Path) -> int:
        rec = records_by_path.get(str(p))
        if rec is None:
            return 0
        return getattr(rec, "width", 0) * getattr(rec, "height", 0)

    def _mtime(p: Path) -> float:
        rec = records_by_path.get(str(p))
        if rec is None:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        return getattr(rec, "mtime", 0.0)

    if keep_strategy == "oldest":
        return min(group_paths, key=_mtime)
    else:
        return max(group_paths, key=_pixels)


def _find_source_root(path: Path, source_folders: List[Path]) -> Path:
    """Return the source folder that contains path p."""
    for sf in source_folders:
        if _is_in_folder(path, sf):
            return sf
    return source_folders[0] if source_folders else path.parent


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def build_merge_plan(
    records: list,
    groups: list,
    main_folder: Path,
    source_folders: List[Path],
    mode: str,
    keep_subfolder: bool,
    keep_strategy: str,
) -> MergePlan:
    """Pure-logic planner: produces a MergePlan from records and options.

    No filesystem I/O is performed.  Collision avoidance uses an in-memory
    set of claimed destination paths.
    """
    try:
        from scanner import RAW_EXTENSIONS
    except ImportError:
        RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf",
                          ".rw2", ".pef", ".srw", ".x3f", ".3fr"}

    plan = MergePlan(mode=mode, main_folder=main_folder, source_folders=list(source_folders))

    records_by_path: dict[str, object] = {str(r.path): r for r in records}

    # In-memory claimed-destination set to simulate suffix-rename without filesystem I/O
    claimed: set[str] = set()

    # Pre-populate with files already in main (they stay in place)
    for rec in records:
        if _is_in_folder(rec.path, main_folder):
            # Same string-normalisation as _compute_target for the claimed-
            # paths set — avoids a per-record resolve() I/O on Windows.
            claimed.add(os.path.normcase(os.path.normpath(str(rec.path))))

    def _is_cross_format_group(grp) -> bool:
        all_paths = [r.path for r in (grp.originals or [])] + [r.path for r in (grp.previews or [])]
        exts = {p.suffix.lower() for p in all_paths}
        has_raw = bool(exts & RAW_EXTENSIONS)
        has_img = bool(exts - RAW_EXTENSIONS)
        return has_raw and has_img

    def _pick_cross_format_originals(group_paths: List[Path]) -> set:
        """Pick one RAW + one non-RAW original from a cross-format group.

        The previous slice `group_paths[:2]` assumed the first two members
        were the RAW+JPEG pair, but member ordering is just enumeration
        order over (originals, previews) — a group with two JPEGs and one
        RAW could pick both JPEGs and drop the RAW entirely.  Partition
        explicitly by extension so the cross-format invariant ("both
        formats go to main") holds regardless of group size or ordering.
        """
        raw_members = [p for p in group_paths if p.suffix.lower() in RAW_EXTENSIONS]
        img_members = [p for p in group_paths if p.suffix.lower() not in RAW_EXTENSIONS]
        picks = set()
        if raw_members:
            picks.add(str(_pick_original(raw_members, main_folder, keep_strategy, records_by_path)))
        if img_members:
            picks.add(str(_pick_original(img_members, main_folder, keep_strategy, records_by_path)))
        return picks

    def _compute_target(src_path: Path, src_root: Path) -> tuple:
        if keep_subfolder:
            try:
                rel = src_path.relative_to(src_root)
            except ValueError:
                rel = Path(src_path.name)
            candidate = main_folder / rel
        else:
            candidate = main_folder / src_path.name

        # Use pure-string normalisation rather than Path.resolve() — the only
        # consumer is the in-memory `claimed` set, and resolve() on a not-yet-
        # existing path still does parent traversal I/O on Windows.
        norm = os.path.normcase(os.path.normpath(str(candidate)))
        if norm not in claimed:
            claimed.add(norm)
            return candidate, False

        stem, suffix, parent = candidate.stem, candidate.suffix, candidate.parent
        counter = 1
        while True:
            new_cand = parent / f"{stem}_{counter}{suffix}"
            norm2 = os.path.normcase(os.path.normpath(str(new_cand)))
            if norm2 not in claimed:
                claimed.add(norm2)
                return new_cand, True
            counter += 1

    # Paths that belong to at least one duplicate group
    grouped_paths: set[str] = set()
    for grp in groups:
        for r in (grp.originals or []):
            grouped_paths.add(str(r.path))
        for r in (grp.previews or []):
            grouped_paths.add(str(r.path))

    # Mode B per-source trash accumulator
    source_trash: dict[str, list] = {str(sf): [] for sf in source_folders}

    n_to_main = 0
    n_suffix_renames = 0
    n_unique = 0
    n_groups_with_source_members = 0
    space_delta = 0

    # --- Process duplicate groups ---
    for grp in groups:
        all_member_paths = [r.path for r in (grp.originals or [])] + [r.path for r in (grp.previews or [])]
        source_members = [p for p in all_member_paths if not _is_in_folder(p, main_folder)]
        if not source_members:
            continue

        n_groups_with_source_members += 1

        if _is_cross_format_group(grp):
            # Both formats go to main — pick one RAW and one non-RAW
            # explicitly, not whichever two happen to be enumerated first.
            originals_set = _pick_cross_format_originals(all_member_paths)
        else:
            orig = _pick_original(all_member_paths, main_folder, keep_strategy, records_by_path)
            originals_set = {str(orig)}

        for member in source_members:
            src_root = _find_source_root(member, source_folders)
            is_orig = str(member) in originals_set

            if is_orig:
                dst, renamed = _compute_target(member, src_root)
                if renamed:
                    n_suffix_renames += 1
                action = "move" if mode == "destructive" else "copy"
                rec = records_by_path.get(str(member))
                size = getattr(rec, "file_size", 0) if rec else 0
                if mode == "nondestructive":
                    space_delta += size
                plan.ops.append(MergeFileOp(
                    action=action, src=member, dst=dst,
                    group_id=grp.group_id, role="original", renamed=renamed,
                ))
                n_to_main += 1
            else:
                # duplicate: stays in source (Mode A) or flagged for intra-source trash (Mode B)
                if mode == "nondestructive":
                    sf_str = str(_find_source_root(member, source_folders))
                    if sf_str in source_trash:
                        source_trash[sf_str].append(member)
                else:
                    # Mode A: record as a "duplicate" op so trash_duplicates can find them
                    plan.ops.append(MergeFileOp(
                        action="move", src=member, dst=member,  # dst unused for trash
                        group_id=grp.group_id, role="duplicate", renamed=False,
                    ))

    plan.source_trash = source_trash

    # --- Process unique (non-grouped) source files ---
    all_src_paths = [r.path for r in records if not _is_in_folder(r.path, main_folder)]
    for src_path in all_src_paths:
        if str(src_path) in grouped_paths:
            continue
        src_root = _find_source_root(src_path, source_folders)
        dst, renamed = _compute_target(src_path, src_root)
        if renamed:
            n_suffix_renames += 1
        action = "move" if mode == "destructive" else "copy"
        rec = records_by_path.get(str(src_path))
        size = getattr(rec, "file_size", 0) if rec else 0
        space_delta += size
        plan.ops.append(MergeFileOp(
            action=action, src=src_path, dst=dst,
            group_id="", role="unique", renamed=renamed,
        ))
        n_unique += 1
        n_to_main += 1

    plan.n_unique = n_unique
    plan.n_groups = n_groups_with_source_members
    plan.n_to_main = n_to_main
    plan.n_suffix_renames = n_suffix_renames
    plan.space_delta = space_delta

    return plan


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

SIDECAR_EXTENSIONS = {".xmp", ".aae"}


class MergeExecutor:
    """Applies a MergePlan: moves/copies files, updates library cache, writes log."""

    def __init__(
        self,
        plan: MergePlan,
        library,
        *,
        dry_run: bool = False,
        stop_flag: "Optional[list]" = None,
        pause_flag: "Optional[list]" = None,
        # When a drive becomes unreachable mid-merge the executor appends the
        # offending path(s) here and sets pause_flag[0] = True.  The UI poll
        # consumes the list to show a "Reconnect drive X and click Resume"
        # modal.  Cleared by the executor when the drive returns OR by the UI
        # when Cancel is clicked.
        drive_disconnect_paths: "Optional[list]" = None,
        progress_cb: "Optional[Callable]" = None,
        move_sidecars: bool = True,
    ) -> None:
        self._plan          = plan
        self._library       = library
        self._dry_run       = dry_run
        self._stop_flag     = stop_flag if stop_flag is not None else [False]
        self._pause_flag    = pause_flag if pause_flag is not None else [False]
        self._drive_disconnect_paths = (
            drive_disconnect_paths if drive_disconnect_paths is not None else []
        )
        self._progress_cb   = progress_cb
        self._move_sidecars = move_sidecars
        self._operations: list[dict] = []
        self._completed_ops: int = 0

    # ---- Public API --------------------------------------------------------

    def apply(self) -> dict:
        """Execute the move/copy phase.  Returns a result dict."""
        plan  = self._plan
        # Only move/copy ops (not duplicate placeholders)
        move_ops = [op for op in plan.ops if op.role in ("original", "unique")]
        total = len(move_ops)
        errors: list[str] = []

        for i, op in enumerate(move_ops):
            if self._stop_flag[0]:
                break

            # Drive-disconnect pause
            while self._pause_flag[0]:
                time.sleep(0.2)
                if self._stop_flag[0]:
                    break
            if self._stop_flag[0]:
                break

            if self._progress_cb:
                self._progress_cb(f"Moving {i + 1}/{total}…", i, total)

            if not self._drive_ok(op.src) or not self._drive_ok(op.dst):
                # Auto-pause: wait for the drive(s) to come back instead of
                # silently skipping the op.  Resumes when both drives return
                # OR breaks out if stop_flag is set from the UI.
                if not self._wait_for_drives(op.src, op.dst):
                    # Stopped before drives returned.
                    break
                # Drives back; re-check before executing (file may have been
                # removed while the drive was offline; the op will then fail
                # below in the move/copy try-block and surface as an error).

            if not self._dry_run:
                try:
                    op.dst.parent.mkdir(parents=True, exist_ok=True)
                    if op.action == "move":
                        shutil.move(str(op.src), str(op.dst))
                        if self._library:
                            self._library.relocate(str(op.src), str(op.dst))
                    else:
                        shutil.copy2(str(op.src), str(op.dst))
                        if self._library:
                            self._library.duplicate_entry(str(op.src), str(op.dst))

                    if self._move_sidecars:
                        self._handle_sidecars(op)

                    self._log_op(op, status=op.action + "d")
                    self._completed_ops += 1
                    self._flush_log(plan.main_folder)
                except Exception as exc:
                    errors.append(f"{op.src.name}: {exc}")
                    self._log_op(op, status=f"error: {exc}")
            else:
                self._log_op(op, status="dry_run")
                self._completed_ops += 1

        return {
            "completed": self._completed_ops,
            "total":     total,
            "errors":    errors,
            "dry_run":   self._dry_run,
        }

    def trash_duplicates(self) -> dict:
        """Execute the trash phase (step 2 after Apply Merge).

        Mode A: trash source files that are duplicates (role='duplicate').
        Mode B: trash intra-folder duplicates from each source folder.
        """
        plan    = self._plan
        errors: list[str] = []
        trashed = 0

        if plan.mode == "destructive":
            dup_ops = [op for op in plan.ops if op.role == "duplicate"]
            for op in dup_ops:
                if self._stop_flag[0]:
                    break
                while self._pause_flag[0]:
                    time.sleep(0.2)
                    if self._stop_flag[0]:
                        break
                if self._stop_flag[0]:
                    break

                src = op.src
                # Drive-disconnect check matches apply() semantics — pause and
                # wait rather than silently dropping the trash op on the floor.
                if not self._drive_ok(src):
                    if not self._wait_for_drives(src):
                        break
                if not src.exists():
                    continue
                trash_dir = src.parent / "trash"
                if not self._dry_run:
                    try:
                        trash_dir.mkdir(parents=True, exist_ok=True)
                        from mover import _unique_path
                        dest = _unique_path(trash_dir / src.name)
                        shutil.move(str(src), str(dest))
                        trashed += 1
                        self._operations.append({
                            "type": "merge_trash",
                            "from": str(src),
                            "to":   str(dest),
                            "status": "moved",
                        })
                        self._flush_log(plan.main_folder)
                    except Exception as exc:
                        errors.append(f"{src.name}: {exc}")
                else:
                    trashed += 1

        else:  # nondestructive
            for sf_str, trash_paths in plan.source_trash.items():
                for src in trash_paths:
                    if self._stop_flag[0]:
                        break
                    while self._pause_flag[0]:
                        time.sleep(0.2)
                        if self._stop_flag[0]:
                            break
                    if self._stop_flag[0]:
                        break

                    if not self._drive_ok(src):
                        if not self._wait_for_drives(src):
                            break
                    if not src.exists():
                        continue
                    trash_dir = Path(sf_str) / "trash"
                    if not self._dry_run:
                        try:
                            trash_dir.mkdir(parents=True, exist_ok=True)
                            from mover import _unique_path
                            dest = _unique_path(trash_dir / src.name)
                            shutil.move(str(src), str(dest))
                            trashed += 1
                            self._operations.append({
                                "type": "merge_trash",
                                "from": str(src),
                                "to":   str(dest),
                                "status": "moved",
                            })
                            self._flush_log(plan.main_folder)
                        except Exception as exc:
                            errors.append(f"{src.name}: {exc}")
                    else:
                        trashed += 1

        return {"trashed": trashed, "errors": errors, "dry_run": self._dry_run}

    # ---- Helpers -----------------------------------------------------------

    @staticmethod
    def _drive_ok(path: Path) -> bool:
        from mover import _drive_available
        return _drive_available(path)

    def _wait_for_drives(self, *paths) -> bool:
        """Block until every path's drive is available again, or stop is set.

        Used when a drive vanishes mid-merge.  Sets pause_flag + records the
        unreachable paths so the UI can show the reconnect modal; auto-resumes
        when the drive returns.

        Returns True when drives are back, False when stop_flag was set.
        """
        # Tell the UI which drives are missing (first one is enough; modal
        # will show the drive letter from the path).
        for p in paths:
            try:
                self._drive_disconnect_paths.append(p)
            except Exception:
                pass
        self._pause_flag[0] = True
        while self._pause_flag[0]:
            if self._stop_flag[0]:
                return False
            if all(self._drive_ok(p) for p in paths):
                # Drives are back — auto-resume (don't wait for UI).
                self._pause_flag[0] = False
                # Drain any echoes the UI hasn't consumed yet.
                try:
                    self._drive_disconnect_paths.clear()
                except Exception:
                    pass
                return True
            time.sleep(0.5)
        return not self._stop_flag[0]

    def _handle_sidecars(self, op: MergeFileOp) -> None:
        for ext in SIDECAR_EXTENSIONS:
            for try_ext in (ext, ext.upper()):
                sib = op.src.with_suffix(try_ext)
                if sib.exists():
                    sib_dst = _unique_sidecar_path(op.dst.parent / sib.name)
                    try:
                        if op.action == "move":
                            shutil.move(str(sib), str(sib_dst))
                        else:
                            shutil.copy2(str(sib), str(sib_dst))
                    except Exception as _exc:
                        # Sidecar failure is non-fatal but should be visible —
                        # the primary file already moved/copied successfully.
                        print(
                            f"[merger] sidecar {op.action} failed for {sib.name}: {_exc}",
                            file=sys.stderr,
                        )
                    break

    def _log_op(self, op: MergeFileOp, status: str) -> None:
        self._operations.append({
            "type":     f"merge_{op.action}",
            "from":     str(op.src),
            "to":       str(op.dst),
            "group_id": op.group_id,
            "role":     op.role,
            "renamed":  op.renamed,
            "status":   status,
        })

    def _flush_log(self, dest_root: Path) -> None:
        """Write operations_log.json; called after every successful file op."""
        log_path = dest_root / "operations_log.json"
        log = {
            "version":    1,
            "timestamp":  datetime.now().isoformat(),
            "operations": self._operations,
        }
        try:
            log_path.write_text(
                json.dumps(log, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as _exc:
            # The operations log is the spec's resume-from-last-action source
            # of truth; a silent failure here would defeat drive-disconnect
            # safety on resume.  Make it visible.
            print(
                f"[merger] failed to write operations log at {log_path}: {_exc}",
                file=sys.stderr,
            )


def _unique_sidecar_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
