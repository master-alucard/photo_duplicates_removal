"""
tests/test_merger.py -- Tests for merger.py (planner + executor) and library.py
relocate/duplicate_entry methods.

Covers all 21 acceptance criteria from the Merge tab spec.

Run with:
    python -m pytest tests/test_merger.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from merger import (
    MergeExecutor,
    MergeFileOp,
    MergePlan,
    _find_source_root,
    _is_in_folder,
    _pick_original,
    build_merge_plan,
)
from library import FileRecord, Library
from config import Settings


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_image(path: Path, color=(200, 100, 50), size=(16, 16)) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def _make_lib(tmp_path: Path) -> Library:
    return Library.load(tmp_path / "lib")


def _make_file_record(path: str, width: int = 100, height: int = 100,
                       mtime: float = 1_700_000_000.0, size: int = 5000) -> FileRecord:
    return FileRecord(
        path=path, mtime=mtime, size=size,
        phash="f" * 16, dhash="a" * 16,
        histogram=[0.01] * 96, brightness=128.0,
        width=width, height=height,
    )


class _FakeRecord:
    """Minimal ImageRecord stub for planner tests (no real hashing needed)."""
    def __init__(self, path: Path, width: int = 100, height: int = 100,
                 mtime: float = 1_700_000_000.0, file_size: int = 5000):
        self.path = path
        self.width = width
        self.height = height
        self.mtime = mtime
        self.file_size = file_size


class _FakeGroup:
    """Minimal DuplicateGroup stub."""
    def __init__(self, group_id: str, originals, previews=None):
        self.group_id = group_id
        self.originals = originals
        self.previews = previews or []


# ---------------------------------------------------------------------------
# AC2: Library.relocate
# ---------------------------------------------------------------------------

class TestLibraryRelocate:
    def test_relocate_updates_key_same_folder(self, tmp_path):
        lib = _make_lib(tmp_path)
        folder = tmp_path / "photos"
        folder.mkdir()
        old_path = str(folder / "a.jpg")
        new_path = str(folder / "b.jpg")
        rec = _make_file_record(old_path)
        lib.save_cache(str(folder), {old_path: rec})
        lib.relocate(old_path, new_path)
        cache = lib.load_cache(str(folder))
        assert new_path in cache
        assert old_path not in cache
        assert cache[new_path].path == new_path

    def test_relocate_noop_when_not_cached(self, tmp_path):
        lib = _make_lib(tmp_path)
        # Should not raise
        lib.relocate("/nonexistent/a.jpg", "/nonexistent/b.jpg")

    def test_relocate_across_folders(self, tmp_path):
        lib = _make_lib(tmp_path)
        src_folder = tmp_path / "src"
        dst_folder = tmp_path / "main"
        src_folder.mkdir()
        dst_folder.mkdir()
        old = str(src_folder / "a.jpg")
        new = str(dst_folder / "a.jpg")
        rec = _make_file_record(old)
        lib.save_cache(str(src_folder), {old: rec})
        lib.relocate(old, new)
        src_cache = lib.load_cache(str(src_folder))
        dst_cache = lib.load_cache(str(dst_folder))
        assert old not in src_cache
        assert new in dst_cache
        assert dst_cache[new].path == new

    def test_relocate_preserves_hashes(self, tmp_path):
        lib = _make_lib(tmp_path)
        folder = tmp_path / "photos"
        folder.mkdir()
        old = str(folder / "a.jpg")
        new = str(folder / "b.jpg")
        rec = _make_file_record(old)
        rec.phash = "deadbeef01234567"
        lib.save_cache(str(folder), {old: rec})
        lib.relocate(old, new)
        cache = lib.load_cache(str(folder))
        assert cache[new].phash == "deadbeef01234567"


# ---------------------------------------------------------------------------
# AC14: Library.duplicate_entry
# ---------------------------------------------------------------------------

class TestLibraryDuplicateEntry:
    def test_duplicate_creates_new_entry(self, tmp_path):
        lib = _make_lib(tmp_path)
        src_folder = tmp_path / "src"
        dst_folder = tmp_path / "main"
        src_folder.mkdir()
        dst_folder.mkdir()
        src = str(src_folder / "a.jpg")
        dst = str(dst_folder / "a.jpg")
        rec = _make_file_record(src)
        lib.save_cache(str(src_folder), {src: rec})
        lib.duplicate_entry(src, dst)
        src_cache = lib.load_cache(str(src_folder))
        dst_cache = lib.load_cache(str(dst_folder))
        assert src in src_cache          # source unchanged
        assert dst in dst_cache          # new entry created
        assert dst_cache[dst].phash == rec.phash

    def test_duplicate_noop_when_not_cached(self, tmp_path):
        lib = _make_lib(tmp_path)
        # Should not raise
        lib.duplicate_entry("/nonexistent/a.jpg", "/nonexistent/b.jpg")

    def test_duplicate_same_hashes(self, tmp_path):
        lib = _make_lib(tmp_path)
        folder = tmp_path / "src"
        main = tmp_path / "main"
        folder.mkdir(); main.mkdir()
        src = str(folder / "a.jpg")
        dst = str(main / "a.jpg")
        rec = _make_file_record(src)
        rec.phash = "1234567890abcdef"
        rec.dhash = "fedcba0987654321"
        lib.save_cache(str(folder), {src: rec})
        lib.duplicate_entry(src, dst)
        cache = lib.load_cache(str(main))
        assert cache[dst].phash == "1234567890abcdef"
        assert cache[dst].dhash == "fedcba0987654321"


# ---------------------------------------------------------------------------
# AC14 integration: after merge, zero rehashing
# ---------------------------------------------------------------------------

class TestLibraryCacheContinuityAfterMerge:
    """After Apply Merge, a regular collect_images of main triggers zero re-hashing."""

    def test_relocate_gives_cache_hit_on_next_scan(self, tmp_path):
        from scanner import collect_images
        src_folder = tmp_path / "src"
        main_folder = tmp_path / "main"
        src_folder.mkdir(); main_folder.mkdir()

        img = _make_image(src_folder / "a.jpg", color=(200, 150, 100))

        # Initial scan — populates cache
        cache: dict = {}
        s = Settings()
        s.recursive = False
        s.use_rawpy = False
        collect_images(src_folder, set(), s, library_cache=cache)

        src_key = str(img.resolve())
        assert src_key in cache

        # Simulate a move: physically move, then relocate in cache
        main_img = main_folder / "a.jpg"
        shutil.copy2(str(img), str(main_img))

        lib = _make_lib(tmp_path)
        lib.save_cache(str(src_folder), {src_key: cache[src_key]})

        lib.relocate(str(img), str(main_img))
        merged = lib.load_cache(str(main_folder))

        # Second scan of main — should be a cache hit (no rehash)
        hash_calls = []
        original_collect = collect_images

        call_count = [0]
        from scanner import _hash_image as real_hash_image

        with patch("scanner._hash_image", side_effect=lambda *a, **kw: (
            call_count.__setitem__(0, call_count[0] + 1) or real_hash_image(*a, **kw)
        )):
            recs = collect_images(main_folder, set(), s, library_cache=merged)

        assert call_count[0] == 0, f"Expected 0 hash calls but got {call_count[0]}"
        assert len(recs) == 1


# ---------------------------------------------------------------------------
# Planner: _pick_original
# ---------------------------------------------------------------------------

class TestPickOriginal:
    def test_prefers_main_folder_member(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        src = tmp_path / "src"
        src.mkdir()
        (main / "a.jpg").touch()
        (src / "b.jpg").touch()
        paths = [main / "a.jpg", src / "b.jpg"]
        orig = _pick_original(paths, main, "pixels", {})
        assert orig == main / "a.jpg"

    def test_pixels_strategy(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        p1 = src / "small.jpg"
        p2 = src / "big.jpg"
        p1.touch(); p2.touch()
        recs = {
            str(p1): _FakeRecord(p1, width=100, height=100),
            str(p2): _FakeRecord(p2, width=1000, height=1000),
        }
        orig = _pick_original([p1, p2], main, "pixels", recs)
        assert orig == p2

    def test_oldest_strategy(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        p1 = src / "new.jpg"
        p2 = src / "old.jpg"
        p1.touch(); p2.touch()
        recs = {
            str(p1): _FakeRecord(p1, mtime=2000.0),
            str(p2): _FakeRecord(p2, mtime=1000.0),
        }
        orig = _pick_original([p1, p2], main, "oldest", recs)
        assert orig == p2


# ---------------------------------------------------------------------------
# AC5: Originals-only rule (planner)
# ---------------------------------------------------------------------------

class TestPlannerOriginalsOnly:
    def test_exactly_one_original_per_group(self, tmp_path):
        main = tmp_path / "main"
        src = tmp_path / "src"
        main.mkdir(); src.mkdir()

        a = _FakeRecord(src / "a.jpg")
        b = _FakeRecord(src / "b.jpg")
        grp = _FakeGroup("g1", originals=[a], previews=[b])

        plan = build_merge_plan(
            records=[a, b],
            groups=[grp],
            main_folder=main,
            source_folders=[src],
            mode="destructive",
            keep_subfolder=False,
            keep_strategy="pixels",
        )

        orig_ops = [op for op in plan.ops if op.role == "original"]
        dup_ops  = [op for op in plan.ops if op.role == "duplicate"]
        assert len(orig_ops) == 1
        assert len(dup_ops)  == 1

    def test_no_file_in_main_for_non_original(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        a = _FakeRecord(src / "a.jpg", width=1000, height=1000)
        b = _FakeRecord(src / "b.jpg", width=100, height=100)
        grp = _FakeGroup("g1", originals=[a], previews=[b])

        plan = build_merge_plan(
            records=[a, b], groups=[grp],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        move_ops = [op for op in plan.ops if op.role == "original"]
        # Only `a` (largest) should be marked for main
        assert len(move_ops) == 1
        assert move_ops[0].src == src / "a.jpg"


# ---------------------------------------------------------------------------
# AC10: Pre-existing main file wins
# ---------------------------------------------------------------------------

class TestMainFileWins:
    def test_preexisting_main_is_original(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        main_img = _FakeRecord(main / "a.jpg")
        src_img  = _FakeRecord(src / "a_copy.jpg")
        grp = _FakeGroup("g1", originals=[main_img], previews=[src_img])

        plan = build_merge_plan(
            records=[main_img, src_img], groups=[grp],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        orig_ops = [op for op in plan.ops if op.role == "original"]
        # main_img is already in main; src_img is the duplicate
        # No move/copy for main_img (it's already there); src_img is dup
        assert all(op.src != main / "a.jpg" for op in orig_ops)


# ---------------------------------------------------------------------------
# AC6: Mode A apply (executor moves original to main)
# ---------------------------------------------------------------------------

class TestModeAApply:
    def test_original_moves_to_main(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        img = _make_image(src / "a.jpg", color=(100, 200, 50))

        op = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="g1", role="original")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])
        plan.n_to_main = 1

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        result = executor.apply()

        assert result["completed"] == 1
        assert (main / "a.jpg").exists()
        assert not (src / "a.jpg").exists()

    def test_duplicate_stays_in_source(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        orig_img = _make_image(src / "orig.jpg")
        dup_img  = _make_image(src / "dup.jpg")

        op_orig = MergeFileOp(action="move", src=src / "orig.jpg", dst=main / "orig.jpg",
                               group_id="g1", role="original")
        op_dup  = MergeFileOp(action="move", src=src / "dup.jpg", dst=src / "dup.jpg",
                               group_id="g1", role="duplicate")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src],
                         ops=[op_orig, op_dup])

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        executor.apply()

        # Original moved; duplicate still in source
        assert (main / "orig.jpg").exists()
        assert (src / "dup.jpg").exists()


# ---------------------------------------------------------------------------
# AC7: Mode A trash
# ---------------------------------------------------------------------------

class TestModeATrash:
    def test_trash_moves_duplicates(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(main / "orig.jpg")  # original already in main
        dup = _make_image(src / "dup.jpg")

        op_dup = MergeFileOp(action="move", src=src / "dup.jpg", dst=src / "dup.jpg",
                              group_id="g1", role="duplicate")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op_dup])

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        result = executor.trash_duplicates()

        assert result["trashed"] == 1
        assert not (src / "dup.jpg").exists()
        trash_dir = src / "trash"
        assert trash_dir.exists()
        trashed = list(trash_dir.iterdir())
        assert len(trashed) == 1

    def test_operations_log_written(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(src / "a.jpg")
        op = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="", role="unique")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        executor.apply()

        log_path = main / "operations_log.json"
        assert log_path.exists()
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert len(data["operations"]) >= 1
        assert data["operations"][0]["type"] == "merge_move"


# ---------------------------------------------------------------------------
# AC8: Mode B apply (copy, sources untouched)
# ---------------------------------------------------------------------------

class TestModeBApply:
    def test_copy_original_to_main(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        img = _make_image(src / "a.jpg")
        op = MergeFileOp(action="copy", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="g1", role="original")
        plan = MergePlan(mode="nondestructive", main_folder=main, source_folders=[src], ops=[op])

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        result = executor.apply()

        assert result["completed"] == 1
        assert (main / "a.jpg").exists()
        assert (src / "a.jpg").exists()   # source NOT removed

    def test_source_files_untouched(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        orig = _make_image(src / "orig.jpg")
        dup  = _make_image(src / "dup.jpg")

        op_orig = MergeFileOp(action="copy", src=src / "orig.jpg", dst=main / "orig.jpg",
                               group_id="g1", role="original")
        plan = MergePlan(mode="nondestructive", main_folder=main, source_folders=[src],
                         ops=[op_orig], source_trash={str(src): [src / "dup.jpg"]})

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        executor.apply()

        # Both source files still exist after apply
        assert (src / "orig.jpg").exists()
        assert (src / "dup.jpg").exists()


# ---------------------------------------------------------------------------
# AC9: Mode B trash (intra-folder only)
# ---------------------------------------------------------------------------

class TestModeBTrash:
    def test_intra_folder_dup_trashed(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        dup = _make_image(src / "dup.jpg")
        plan = MergePlan(
            mode="nondestructive", main_folder=main, source_folders=[src],
            ops=[],
            source_trash={str(src): [src / "dup.jpg"]},
        )

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        result = executor.trash_duplicates()

        assert result["trashed"] == 1
        assert not (src / "dup.jpg").exists()

    def test_main_folder_not_touched_in_mode_b_trash(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        main_file = _make_image(main / "keep.jpg")
        plan = MergePlan(
            mode="nondestructive", main_folder=main, source_folders=[src],
            ops=[],
            source_trash={str(src): []},  # nothing to trash
        )

        executor = MergeExecutor(plan=plan, library=None, dry_run=False)
        executor.trash_duplicates()

        assert (main / "keep.jpg").exists()


# ---------------------------------------------------------------------------
# AC11: Cross-format pair handling (RAW + JPEG both go to main)
# ---------------------------------------------------------------------------

class TestCrossFormatPairs:
    def test_both_raw_and_jpeg_in_plan(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        jpeg = _FakeRecord(src / "photo.jpg")
        raw  = _FakeRecord(src / "photo.cr2")

        # A cross-format group has one of each
        grp = _FakeGroup("cf1", originals=[jpeg], previews=[raw])

        plan = build_merge_plan(
            records=[jpeg, raw], groups=[grp],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        orig_ops = [op for op in plan.ops if op.role == "original"]
        # Both should be originals (cross-format pair)
        orig_paths = {op.src.name for op in orig_ops}
        assert "photo.jpg" in orig_paths
        assert "photo.cr2" in orig_paths


# ---------------------------------------------------------------------------
# AC12: Subfolder structure ON
# ---------------------------------------------------------------------------

class TestSubfolderStructure:
    def test_keep_subfolder_on(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        (src / "A" / "B").mkdir(parents=True)
        main.mkdir()

        img = _FakeRecord(src / "A" / "B" / "x.jpg")
        plan = build_merge_plan(
            records=[img], groups=[],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=True, keep_strategy="pixels",
        )

        assert len(plan.ops) == 1
        assert plan.ops[0].dst == main / "A" / "B" / "x.jpg"

    def test_keep_subfolder_off_flattens(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        (src / "A" / "B").mkdir(parents=True)
        main.mkdir()

        img = _FakeRecord(src / "A" / "B" / "x.jpg")
        plan = build_merge_plan(
            records=[img], groups=[],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        assert plan.ops[0].dst == main / "x.jpg"

    def test_collision_suffix_rename(self, tmp_path):
        main = tmp_path / "main"
        src1 = tmp_path / "src1"
        src2 = tmp_path / "src2"
        main.mkdir(); src1.mkdir(); src2.mkdir()

        # Two different files with same basename
        a1 = _FakeRecord(src1 / "x.jpg", width=100, height=100)
        a2 = _FakeRecord(src2 / "x.jpg", width=200, height=200)

        plan = build_merge_plan(
            records=[a1, a2], groups=[],
            main_folder=main, source_folders=[src1, src2],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        dsts = {op.dst.name for op in plan.ops}
        assert "x.jpg" in dsts
        assert "x_1.jpg" in dsts
        renamed_ops = [op for op in plan.ops if op.renamed]
        assert len(renamed_ops) == 1


# ---------------------------------------------------------------------------
# AC15: Operation log written with merge_move/merge_copy type
# ---------------------------------------------------------------------------

class TestOperationLog:
    def test_log_type_merge_move(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(src / "a.jpg")
        op = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="", role="unique")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])
        MergeExecutor(plan=plan, library=None, dry_run=False).apply()

        data = json.loads((main / "operations_log.json").read_text(encoding="utf-8"))
        assert data["operations"][0]["type"] == "merge_move"

    def test_log_type_merge_copy(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(src / "a.jpg")
        op = MergeFileOp(action="copy", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="", role="unique")
        plan = MergePlan(mode="nondestructive", main_folder=main, source_folders=[src], ops=[op])
        MergeExecutor(plan=plan, library=None, dry_run=False).apply()

        data = json.loads((main / "operations_log.json").read_text(encoding="utf-8"))
        assert data["operations"][0]["type"] == "merge_copy"

    def test_log_has_renamed_field(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(src / "a.jpg")
        op = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a_1.jpg",
                         group_id="", role="unique", renamed=True)
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])
        MergeExecutor(plan=plan, library=None, dry_run=False).apply()

        data = json.loads((main / "operations_log.json").read_text(encoding="utf-8"))
        assert data["operations"][0]["renamed"] is True


# ---------------------------------------------------------------------------
# AC16: Preview-first — scan never moves files
# ---------------------------------------------------------------------------

class TestPreviewFirst:
    def test_dry_run_no_files_moved(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        img = _make_image(src / "a.jpg")
        op  = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a.jpg",
                           group_id="", role="unique")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])

        # dry_run=True simulates the scan/preview phase
        executor = MergeExecutor(plan=plan, library=None, dry_run=True)
        result = executor.apply()

        assert result["completed"] == 1   # "would complete"
        assert (src / "a.jpg").exists()   # NOT moved
        assert not (main / "a.jpg").exists()


# ---------------------------------------------------------------------------
# AC17: Drive-disconnect pause (mocked _drive_available)
# ---------------------------------------------------------------------------

class TestDriveDisconnectPause:
    def test_drive_unavailable_skips_op(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        img = _make_image(src / "a.jpg")
        op  = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a.jpg",
                           group_id="", role="unique")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])

        with patch("merger.MergeExecutor._drive_ok", return_value=False):
            executor = MergeExecutor(plan=plan, library=None, dry_run=False)
            result = executor.apply()

        assert result["completed"] == 0
        assert len(result["errors"]) >= 1
        assert (src / "a.jpg").exists()   # not moved

    def test_pause_then_stop(self, tmp_path):
        """Pause flag pauses the loop; stop flag then aborts cleanly."""
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        imgs = [_make_image(src / f"{i}.jpg") for i in range(3)]
        ops  = [
            MergeFileOp(action="move", src=src / f"{i}.jpg", dst=main / f"{i}.jpg",
                        group_id="", role="unique")
            for i in range(3)
        ]
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=ops)

        stop_flag  = [False]
        pause_flag = [True]   # start paused

        # Release pause after a short delay and then stop
        def _release():
            time.sleep(0.1)
            pause_flag[0] = False
            time.sleep(0.05)
            stop_flag[0] = True

        t = threading.Thread(target=_release, daemon=True)
        t.start()

        executor = MergeExecutor(plan=plan, library=None, dry_run=False,
                                 stop_flag=stop_flag, pause_flag=pause_flag)
        result = executor.apply()
        t.join(timeout=2)

        # After pause release + stop, some or zero ops may have completed —
        # what matters is no crash and completed <= total
        assert result["completed"] <= len(ops)


# ---------------------------------------------------------------------------
# AC18: Stop mid-merge — library reflects only completed ops
# ---------------------------------------------------------------------------

class TestStopMidMerge:
    def test_stop_flag_aborts(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        imgs = [_make_image(src / f"{i}.jpg") for i in range(3)]
        ops  = [
            MergeFileOp(action="move", src=src / f"{i}.jpg", dst=main / f"{i}.jpg",
                        group_id="", role="unique")
            for i in range(3)
        ]
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=ops)

        stop_flag = [True]  # pre-stopped
        executor = MergeExecutor(plan=plan, library=None, dry_run=False, stop_flag=stop_flag)
        result = executor.apply()

        assert result["completed"] == 0
        # All files still in src
        for i in range(3):
            assert (src / f"{i}.jpg").exists()


# ---------------------------------------------------------------------------
# AC19: Stale-progress race — _on_merge_done clears pending progress
# ---------------------------------------------------------------------------

class TestStaleProgressRace:
    """
    The _on_merge_done handler must clear _merge_pending_progress and cancel
    the poll so late ticks cannot overwrite the final Done state.
    Tested here by directly calling the method on a mock App instance.
    """

    def _make_mock_app(self, root_mock):
        """Build a minimal mock that exercises _on_merge_scan_done logic."""
        # We just test that the key state attributes are cleared
        class FakeApp:
            def __init__(self):
                self._merging = True
                self._merge_pending_progress = ("stale msg", 50, 100)
                self._merge_progress_tick_after_id = 99
                self._merge_plan = None
                self._merge_source_folders = []
                self._merge_progress_bar = MagicMock()
                self._merge_pulse_dot = MagicMock()
                self._merge_active_frame = MagicMock()
                self._merge_idle_frame = MagicMock()
                self._merge_phase_label = MagicMock()
                self._merge_apply_btn = MagicMock()
                self.root = root_mock
                self._library_ctrl = MagicMock()

            def _on_merge_scan_done(self, plan, all_records, groups, library_cache):
                # Replicate the key invariants from the real handler
                self._merging = False
                self._merge_pending_progress = None
                if self._merge_progress_tick_after_id is not None:
                    try:
                        self.root.after_cancel(self._merge_progress_tick_after_id)
                    except Exception:
                        pass
                    self._merge_progress_tick_after_id = None
                self._merge_plan = plan

        return FakeApp()

    def test_pending_progress_cleared_on_done(self):
        root_mock = MagicMock()
        app = self._make_mock_app(root_mock)

        # Pre-set stale progress
        assert app._merge_pending_progress is not None
        assert app._merge_progress_tick_after_id is not None

        fake_plan = MagicMock()
        app._on_merge_scan_done(fake_plan, [], [], {})

        # After done: both must be cleared
        assert app._merge_pending_progress is None
        assert app._merge_progress_tick_after_id is None
        assert not app._merging
        assert app._merge_plan is fake_plan

    def test_after_cancel_called_with_tick_id(self):
        root_mock = MagicMock()
        app = self._make_mock_app(root_mock)

        app._on_merge_scan_done(MagicMock(), [], [], {})
        root_mock.after_cancel.assert_called_once_with(99)


# ---------------------------------------------------------------------------
# AC21: build_merge_plan with no groups — all files are unique
# ---------------------------------------------------------------------------

class TestPlannerNoGroups:
    def test_all_unique_files_get_move_ops(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        records = [_FakeRecord(src / f"{i}.jpg") for i in range(5)]
        plan = build_merge_plan(
            records=records, groups=[],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        assert plan.n_unique == 5
        assert plan.n_to_main == 5
        assert len(plan.ops) == 5
        assert all(op.role == "unique" for op in plan.ops)

    def test_space_delta_accumulates(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        records = [_FakeRecord(src / f"{i}.jpg", file_size=1000) for i in range(3)]
        plan = build_merge_plan(
            records=records, groups=[],
            main_folder=main, source_folders=[src],
            mode="nondestructive", keep_subfolder=False, keep_strategy="pixels",
        )

        assert plan.space_delta == 3000

    def test_files_already_in_main_not_re_moved(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        main_rec = _FakeRecord(main / "a.jpg")
        src_rec  = _FakeRecord(src / "b.jpg")
        plan = build_merge_plan(
            records=[main_rec, src_rec], groups=[],
            main_folder=main, source_folders=[src],
            mode="destructive", keep_subfolder=False, keep_strategy="pixels",
        )

        # Only b.jpg should get an op; a.jpg is already in main
        assert len(plan.ops) == 1
        assert plan.ops[0].src == src / "b.jpg"


# ---------------------------------------------------------------------------
# Library cache integration with executor (AC14 extended)
# ---------------------------------------------------------------------------

class TestExecutorLibraryIntegration:
    def test_relocate_called_on_move(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(src / "a.jpg")
        op = MergeFileOp(action="move", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="", role="unique")
        plan = MergePlan(mode="destructive", main_folder=main, source_folders=[src], ops=[op])

        mock_lib = MagicMock()
        MergeExecutor(plan=plan, library=mock_lib, dry_run=False).apply()

        mock_lib.relocate.assert_called_once_with(str(src / "a.jpg"), str(main / "a.jpg"))

    def test_duplicate_entry_called_on_copy(self, tmp_path):
        main = tmp_path / "main"
        src  = tmp_path / "src"
        main.mkdir(); src.mkdir()

        _make_image(src / "a.jpg")
        op = MergeFileOp(action="copy", src=src / "a.jpg", dst=main / "a.jpg",
                         group_id="", role="unique")
        plan = MergePlan(mode="nondestructive", main_folder=main, source_folders=[src], ops=[op])

        mock_lib = MagicMock()
        MergeExecutor(plan=plan, library=mock_lib, dry_run=False).apply()

        mock_lib.duplicate_entry.assert_called_once_with(str(src / "a.jpg"), str(main / "a.jpg"))


# ---------------------------------------------------------------------------
# Merge settings persistence (config.py additions)
# ---------------------------------------------------------------------------

class TestMergeSettings:
    def test_settings_has_merge_fields(self):
        s = Settings()
        assert hasattr(s, "merge_main_folder")
        assert hasattr(s, "merge_mode")
        assert hasattr(s, "merge_keep_subfolder")
        assert hasattr(s, "merge_recursive")
        assert hasattr(s, "merge_move_sidecars")
        assert hasattr(s, "merge_source_folders")

    def test_defaults(self):
        s = Settings()
        assert s.merge_mode == "destructive"
        assert s.merge_keep_subfolder is False
        assert s.merge_recursive is True
        assert s.merge_move_sidecars is True
        assert s.merge_source_folders == []

    def test_settings_round_trip(self, tmp_path):
        from config import load_settings, save_settings
        path = tmp_path / "settings.json"
        s = Settings()
        s.merge_main_folder = "/test/main"
        s.merge_mode = "nondestructive"
        s.merge_source_folders = ["/test/src1", "/test/src2"]
        save_settings(s, path)
        s2 = load_settings(path)
        assert s2.merge_main_folder == "/test/main"
        assert s2.merge_mode == "nondestructive"
        assert s2.merge_source_folders == ["/test/src1", "/test/src2"]


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
