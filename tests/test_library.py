"""
tests/test_library.py — Tests for the Library hash-cache system.

Run with:
    python -m pytest tests/test_library.py -v
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

# ── project root on path ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from library import (
    DriveInfo,
    DriveStatus,
    FileRecord,
    FolderEntry,
    Library,
    _CACHE_VERSION,
    _INDEX_VERSION,
    compute_folder_fingerprint,
    find_drive_by_serial,
    get_drive_info,
    get_library_dir,
    update_folder,
)
from config import Settings


# ── tiny image factory ────────────────────────────────────────────────────────

def _make_image(path: Path, color=(200, 100, 50), size=(16, 16)) -> Path:
    """Create a minimal valid JPEG at *path*. Returns *path*."""
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_folder_entry(path: str = "/tmp/test", file_count: int = 3) -> FolderEntry:
    return FolderEntry(
        path               = path,
        drive_type         = "fixed",
        volume_serial      = 12345,
        folder_fingerprint = "abcdef1234567890",
        last_updated       = "2026-01-01T00:00:00",
        file_count         = file_count,
    )


def _make_file_record(path: str = "/tmp/img.jpg") -> FileRecord:
    return FileRecord(
        path       = path,
        mtime      = 1_700_000_000.0,
        size       = 102_400,
        phash      = "f" * 16,
        dhash      = "a" * 16,
        histogram  = [0.1] * 96,
        brightness = 128.0,
        width      = 1920,
        height     = 1080,
        metadata_count = 5,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. get_library_dir
# ═════════════════════════════════════════════════════════════════════════════

class TestGetLibraryDir:
    def test_returns_path(self):
        result = get_library_dir()
        assert isinstance(result, Path)

    def test_ends_with_library(self):
        assert get_library_dir().name == "library"

    def test_is_absolute(self):
        assert get_library_dir().is_absolute()


# ═════════════════════════════════════════════════════════════════════════════
# 2. compute_folder_fingerprint
# ═════════════════════════════════════════════════════════════════════════════

class TestComputeFolderFingerprint:
    def test_empty_folder_returns_hex(self, tmp_path):
        fp = compute_folder_fingerprint(tmp_path)
        assert len(fp) == 64
        int(fp, 16)   # must be valid hex

    def test_deterministic(self, tmp_path):
        _make_image(tmp_path / "a.jpg")
        _make_image(tmp_path / "b.jpg")
        fp1 = compute_folder_fingerprint(tmp_path)
        fp2 = compute_folder_fingerprint(tmp_path)
        assert fp1 == fp2

    def test_changes_when_file_added(self, tmp_path):
        _make_image(tmp_path / "a.jpg")
        fp_before = compute_folder_fingerprint(tmp_path)
        _make_image(tmp_path / "b.jpg")
        fp_after = compute_folder_fingerprint(tmp_path)
        assert fp_before != fp_after

    def test_changes_when_file_renamed(self, tmp_path):
        src = _make_image(tmp_path / "old.jpg")
        fp_before = compute_folder_fingerprint(tmp_path)
        src.rename(tmp_path / "new.jpg")
        fp_after = compute_folder_fingerprint(tmp_path)
        assert fp_before != fp_after

    def test_ignores_subdirectories(self, tmp_path):
        _make_image(tmp_path / "a.jpg")
        fp_before = compute_folder_fingerprint(tmp_path)
        sub = tmp_path / "subdir"
        sub.mkdir()
        fp_after = compute_folder_fingerprint(tmp_path)
        assert fp_before == fp_after   # subdir added → no change

    def test_non_existent_folder_returns_hex(self, tmp_path):
        fp = compute_folder_fingerprint(tmp_path / "nonexistent")
        assert len(fp) == 64


# ═════════════════════════════════════════════════════════════════════════════
# 3. get_drive_info
# ═════════════════════════════════════════════════════════════════════════════

class TestGetDriveInfo:
    def test_returns_drive_info(self, tmp_path):
        info = get_drive_info(tmp_path)
        assert isinstance(info, DriveInfo)

    def test_drive_type_is_known_string(self, tmp_path):
        info = get_drive_info(tmp_path)
        assert info.drive_type in ("fixed", "removable", "network", "cdrom", "ramdisk", "unknown")

    def test_volume_serial_is_int_or_none(self, tmp_path):
        info = get_drive_info(tmp_path)
        assert info.volume_serial is None or isinstance(info.volume_serial, int)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_fixed_drive_has_serial(self, tmp_path):
        info = get_drive_info(tmp_path)
        assert info.volume_serial is not None


# ═════════════════════════════════════════════════════════════════════════════
# 4. find_drive_by_serial
# ═════════════════════════════════════════════════════════════════════════════

class TestFindDriveBySerial:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_finds_current_drive(self, tmp_path):
        info = get_drive_info(tmp_path)
        if info.volume_serial:
            root = find_drive_by_serial(info.volume_serial)
            assert root is not None
            assert root.endswith("\\")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_nonexistent_serial_returns_none(self):
        result = find_drive_by_serial(0xDEADBEEF)
        assert result is None

    def test_non_windows_returns_none(self):
        if sys.platform == "win32":
            pytest.skip("non-Windows test")
        assert find_drive_by_serial(12345) is None


# ═════════════════════════════════════════════════════════════════════════════
# 5. FolderEntry serialisation
# ═════════════════════════════════════════════════════════════════════════════

class TestFolderEntrySerialisation:
    def test_round_trip_via_dict(self):
        entry = _make_folder_entry()
        restored = FolderEntry.from_dict(asdict(entry))
        assert restored == entry

    def test_from_dict_tolerates_missing_keys(self):
        minimal = {"path": "/some/path"}
        entry = FolderEntry.from_dict(minimal)
        assert entry.path == "/some/path"
        assert entry.drive_type == "unknown"
        assert entry.file_count == 0

    def test_json_round_trip(self):
        entry = _make_folder_entry()
        raw   = json.dumps(asdict(entry))
        restored = FolderEntry.from_dict(json.loads(raw))
        assert restored == entry


# ═════════════════════════════════════════════════════════════════════════════
# 6. FileRecord serialisation
# ═════════════════════════════════════════════════════════════════════════════

class TestFileRecordSerialisation:
    def test_round_trip_via_dict(self):
        rec      = _make_file_record()
        restored = FileRecord.from_dict(asdict(rec))
        assert restored == rec

    def test_from_dict_tolerates_missing_keys(self):
        minimal = {
            "path": "/img.jpg", "mtime": 1.0, "size": 100,
            "brightness": 128.0, "width": 10, "height": 10,
        }
        rec = FileRecord.from_dict(minimal)
        assert rec.phash == ""
        assert rec.histogram == []
        assert rec.metadata_count == 0

    def test_json_round_trip(self):
        rec  = _make_file_record()
        raw  = json.dumps(asdict(rec))
        rest = FileRecord.from_dict(json.loads(raw))
        assert rest == rec


# ═════════════════════════════════════════════════════════════════════════════
# 7. FileRecord.from_image_record and to_image_record
# ═════════════════════════════════════════════════════════════════════════════

class TestFileRecordImageRecordConversion:
    def _real_image_record(self, tmp_path):
        from scanner import _hash_image
        img_path = _make_image(tmp_path / "test.jpg")
        return _hash_image(img_path, Settings())

    def test_from_image_record_fields(self, tmp_path):
        ir  = self._real_image_record(tmp_path)
        fr  = FileRecord.from_image_record(ir)
        assert fr.path   == str(ir.path)
        assert fr.width  == ir.width
        assert fr.height == ir.height
        assert fr.size   == ir.file_size
        assert fr.mtime  == ir.mtime
        assert abs(fr.brightness - ir.brightness) < 0.01
        assert len(fr.histogram) == len(ir.histogram)

    def test_phash_dhash_are_hex_strings(self, tmp_path):
        ir = self._real_image_record(tmp_path)
        fr = FileRecord.from_image_record(ir)
        int(fr.phash, 16)   # must be valid hex
        int(fr.dhash, 16)

    def test_to_image_record_round_trip(self, tmp_path):
        ir1 = self._real_image_record(tmp_path)
        fr  = FileRecord.from_image_record(ir1)
        ir2 = fr.to_image_record()
        assert ir2.path   == ir1.path
        assert ir2.width  == ir1.width
        assert ir2.height == ir1.height
        # pHash distance to itself must be 0
        assert (ir2.phash - ir1.phash) == 0


# ═════════════════════════════════════════════════════════════════════════════
# 8. FileRecord.is_stale
# ═════════════════════════════════════════════════════════════════════════════

class TestFileRecordIsStale:
    def test_not_stale_when_unchanged(self, tmp_path):
        img  = _make_image(tmp_path / "a.jpg")
        stat = img.stat()
        rec  = _make_file_record(str(img))
        rec.mtime = stat.st_mtime   # library uses st_mtime, not min formula
        rec.size  = stat.st_size
        assert not rec.is_stale(img)

    def test_stale_when_size_differs(self, tmp_path):
        img  = _make_image(tmp_path / "a.jpg")
        stat = img.stat()
        rec  = _make_file_record(str(img))
        rec.mtime = stat.st_mtime
        rec.size  = stat.st_size + 1   # wrong size
        assert rec.is_stale(img)

    def test_stale_when_mtime_differs(self, tmp_path):
        img  = _make_image(tmp_path / "a.jpg")
        stat = img.stat()
        rec  = _make_file_record(str(img))
        rec.mtime = stat.st_mtime - 1.0   # older timestamp
        rec.size  = stat.st_size
        assert rec.is_stale(img)

    def test_stale_when_file_deleted(self, tmp_path):
        ghost = tmp_path / "gone.jpg"
        rec   = _make_file_record(str(ghost))
        assert rec.is_stale(ghost)


# ═════════════════════════════════════════════════════════════════════════════
# 9. Library — basic CRUD
# ═════════════════════════════════════════════════════════════════════════════

class TestLibraryCrud:
    def test_load_from_empty_dir(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        assert lib.folders == []

    def test_add_and_get_folder(self, tmp_path):
        lib   = Library.load(tmp_path / "lib")
        entry = _make_folder_entry(str(tmp_path / "photos"))
        lib.set_folder(entry)
        got = lib.get_folder(str(tmp_path / "photos"))
        assert got is not None
        assert got.file_count == entry.file_count

    def test_get_nonexistent_returns_none(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        assert lib.get_folder("/totally/fake/path") is None

    def test_folders_property(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        lib.set_folder(_make_folder_entry(str(tmp_path / "a")))
        lib.set_folder(_make_folder_entry(str(tmp_path / "b")))
        assert len(lib.folders) == 2

    def test_remove_folder(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        p   = str(tmp_path / "photos")
        lib.set_folder(_make_folder_entry(p))
        lib.remove_folder(p)
        assert lib.get_folder(p) is None

    def test_remove_nonexistent_is_noop(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        lib.remove_folder("/nonexistent")   # must not raise


# ═════════════════════════════════════════════════════════════════════════════
# 10. Library — persistence (save / reload)
# ═════════════════════════════════════════════════════════════════════════════

class TestLibraryPersistence:
    def test_save_and_reload(self, tmp_path):
        lib_dir = tmp_path / "lib"
        lib1 = Library.load(lib_dir)
        entry = _make_folder_entry(str(tmp_path / "photos"), file_count=7)
        lib1.set_folder(entry)

        lib2 = Library.load(lib_dir)
        got  = lib2.get_folder(str(tmp_path / "photos"))
        assert got is not None
        assert got.file_count == 7

    def test_index_json_version(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        lib.set_folder(_make_folder_entry(str(tmp_path / "x")))
        data = json.loads((tmp_path / "lib" / "index.json").read_text())
        assert data["version"] == _INDEX_VERSION

    def test_remove_persists(self, tmp_path):
        lib_dir = tmp_path / "lib"
        lib1 = Library.load(lib_dir)
        p    = str(tmp_path / "photos")
        lib1.set_folder(_make_folder_entry(p))
        lib1.remove_folder(p)

        lib2 = Library.load(lib_dir)
        assert lib2.get_folder(p) is None


# ═════════════════════════════════════════════════════════════════════════════
# 11. Library — file-record cache
# ═════════════════════════════════════════════════════════════════════════════

class TestLibraryCache:
    def test_load_cache_empty(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        assert lib.load_cache(str(tmp_path / "x")) == {}

    def test_save_and_load_cache(self, tmp_path):
        lib  = Library.load(tmp_path / "lib")
        path = str(tmp_path / "photos")
        rec  = _make_file_record("/photos/img.jpg")
        lib.save_cache(path, {"/photos/img.jpg": rec})

        loaded = lib.load_cache(path)
        assert "/photos/img.jpg" in loaded
        assert loaded["/photos/img.jpg"].width == rec.width

    def test_cache_version_in_json(self, tmp_path):
        lib  = Library.load(tmp_path / "lib")
        path = str(tmp_path / "x")
        lib.save_cache(path, {})
        # Find the hashes.json
        cache_files = list((tmp_path / "lib").rglob("hashes.json"))
        assert len(cache_files) == 1
        data = json.loads(cache_files[0].read_text())
        assert data["version"] == _CACHE_VERSION

    def test_cache_survives_reload(self, tmp_path):
        lib_dir = tmp_path / "lib"
        lib1 = Library.load(lib_dir)
        path = str(tmp_path / "photos")
        lib1.save_cache(path, {"k": _make_file_record("k")})

        lib2   = Library.load(lib_dir)
        loaded = lib2.load_cache(path)
        assert "k" in loaded

    def test_remove_folder_deletes_cache(self, tmp_path):
        lib  = Library.load(tmp_path / "lib")
        path = str(tmp_path / "photos")
        lib.set_folder(_make_folder_entry(path))
        lib.save_cache(path, {"k": _make_file_record("k")})
        lib.remove_folder(path)
        # Cache dir should be gone
        assert lib.load_cache(path) == {}


# ═════════════════════════════════════════════════════════════════════════════
# 12. Library — drive status
# ═════════════════════════════════════════════════════════════════════════════

class TestLibraryDriveStatus:
    def test_ok_when_path_exists(self, tmp_path):
        lib   = Library.load(tmp_path / "lib")
        entry = _make_folder_entry(str(tmp_path))
        status = lib.check_drive_status(entry)
        assert status.state == "ok"

    def test_missing_when_fixed_drive_path_gone(self, tmp_path):
        lib   = Library.load(tmp_path / "lib")
        entry = _make_folder_entry(str(tmp_path / "nonexistent"))
        entry.drive_type = "fixed"
        status = lib.check_drive_status(entry)
        assert status.state == "missing"
        assert status.new_path is None

    def test_missing_when_removable_not_found(self, tmp_path):
        lib   = Library.load(tmp_path / "lib")
        entry = _make_folder_entry(str(tmp_path / "nonexistent"))
        entry.drive_type    = "removable"
        entry.volume_serial = 0xDEADBEEF   # won't exist
        status = lib.check_drive_status(entry)
        # Either "missing" (serial not found) or "moved" (unlikely collision)
        assert status.state in ("missing", "moved")


# ═════════════════════════════════════════════════════════════════════════════
# 13. Library — update_path (drive-letter remap)
# ═════════════════════════════════════════════════════════════════════════════

class TestLibraryUpdatePath:
    def test_entry_remapped(self, tmp_path):
        lib  = Library.load(tmp_path / "lib")
        old  = str(tmp_path / "old_drive" / "Photos")
        new  = str(tmp_path / "new_drive" / "Photos")
        lib.set_folder(_make_folder_entry(old))

        lib.update_path(old, new)
        assert lib.get_folder(old) is None
        assert lib.get_folder(new) is not None

    def test_cache_moved_with_entry(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        old = str(tmp_path / "old" / "Photos")
        new = str(tmp_path / "new" / "Photos")
        lib.set_folder(_make_folder_entry(old))
        lib.save_cache(old, {"k": _make_file_record("k")})

        lib.update_path(old, new)
        assert lib.load_cache(new) != {}

    def test_update_nonexistent_path_is_noop(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        lib.update_path("/nonexistent/old", "/nonexistent/new")   # must not raise

    def test_remapped_entry_persists(self, tmp_path):
        lib_dir = tmp_path / "lib"
        lib1    = Library.load(lib_dir)
        old     = str(tmp_path / "old" / "Photos")
        new     = str(tmp_path / "new" / "Photos")
        lib1.set_folder(_make_folder_entry(old))
        lib1.update_path(old, new)

        lib2 = Library.load(lib_dir)
        assert lib2.get_folder(new) is not None
        assert lib2.get_folder(old) is None


# ═════════════════════════════════════════════════════════════════════════════
# 14. Library — verify_fingerprint
# ═════════════════════════════════════════════════════════════════════════════

class TestVerifyFingerprint:
    def test_matches_after_set(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")

        lib   = Library.load(tmp_path / "lib")
        entry = FolderEntry(
            path               = str(folder.resolve()),
            drive_type         = "fixed",
            volume_serial      = None,
            folder_fingerprint = compute_folder_fingerprint(folder),
            last_updated       = "2026-01-01T00:00:00",
            file_count         = 1,
        )
        lib.set_folder(entry)
        assert lib.verify_fingerprint(str(folder)) is True

    def test_mismatch_after_folder_changes(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")

        lib   = Library.load(tmp_path / "lib")
        entry = FolderEntry(
            path               = str(folder.resolve()),
            drive_type         = "fixed",
            volume_serial      = None,
            folder_fingerprint = compute_folder_fingerprint(folder),
            last_updated       = "2026-01-01T00:00:00",
            file_count         = 1,
        )
        lib.set_folder(entry)
        _make_image(folder / "b.jpg")   # add file → fingerprint changes
        assert lib.verify_fingerprint(str(folder)) is False

    def test_returns_false_for_unknown_folder(self, tmp_path):
        lib = Library.load(tmp_path / "lib")
        assert lib.verify_fingerprint(str(tmp_path / "unknown")) is False


# ═════════════════════════════════════════════════════════════════════════════
# 15. update_folder — integration tests
# ═════════════════════════════════════════════════════════════════════════════

class TestUpdateFolder:
    """Integration tests that create real tiny images and run update_folder."""

    def _lib(self, tmp_path: Path) -> Library:
        return Library.load(tmp_path / "lib")

    def test_fresh_folder_hashes_all_files(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg", color=(255, 0, 0))
        _make_image(folder / "b.jpg", color=(0, 255, 0))

        lib   = self._lib(tmp_path)
        entry = update_folder(lib, folder, Settings())

        assert entry.file_count == 2
        assert lib.get_folder(str(folder.resolve())) is not None
        cache = lib.load_cache(str(folder.resolve()))
        assert len(cache) == 2

    def test_second_run_uses_cache(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")

        lib = self._lib(tmp_path)
        update_folder(lib, folder, Settings())

        # Track which files get hashed on the second run via progress_cb
        hashed_names: list[str] = []
        def _cb(name, i, total):
            hashed_names.append(name)

        cache_before = lib.load_cache(str(folder.resolve()))
        update_folder(lib, folder, Settings(), progress_cb=_cb)
        cache_after  = lib.load_cache(str(folder.resolve()))

        # File count unchanged; cache record is the same object (same mtime/size)
        assert len(cache_before) == len(cache_after)
        for key in cache_before:
            assert cache_before[key] == cache_after[key]

    def test_modified_file_is_rehashed(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg", color=(255, 0, 0))

        lib = self._lib(tmp_path)
        update_folder(lib, folder, Settings())
        cache_before = lib.load_cache(str(folder.resolve()))
        mtime_before = cache_before[str(img)].mtime

        # Wait a tick then overwrite with a different image
        time.sleep(0.05)
        _make_image(img, color=(0, 0, 255))
        # Touch mtime explicitly to ensure it changes on fast filesystems
        os.utime(img, None)

        update_folder(lib, folder, Settings())
        cache_after = lib.load_cache(str(folder.resolve()))
        assert cache_after[str(img)].mtime != mtime_before

    def test_deleted_file_removed_from_cache(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        a = _make_image(folder / "a.jpg")
        b = _make_image(folder / "b.jpg")

        lib = self._lib(tmp_path)
        update_folder(lib, folder, Settings())
        assert lib.load_cache(str(folder.resolve()))[str(b)]   # exists

        b.unlink()
        update_folder(lib, folder, Settings())
        cache = lib.load_cache(str(folder.resolve()))
        assert str(b) not in cache
        assert str(a) in cache

    def test_new_file_added_to_cache(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")

        lib = self._lib(tmp_path)
        update_folder(lib, folder, Settings())
        assert lib.load_cache(str(folder.resolve())).__len__() == 1

        _make_image(folder / "b.jpg")
        update_folder(lib, folder, Settings())
        assert lib.load_cache(str(folder.resolve())).__len__() == 2

    def test_stop_flag_aborts_early(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        for i in range(10):
            _make_image(folder / f"{i}.jpg", color=(i * 20, 0, 0))

        lib       = self._lib(tmp_path)
        stop_flag = [False]
        visited: list[str] = []

        def _cb(name, i, total):
            visited.append(name)
            if i >= 2:
                stop_flag[0] = True

        update_folder(lib, folder, Settings(), progress_cb=_cb, stop_flag=stop_flag)
        # Should have stopped well before all 10 files
        assert len(visited) < 10

    def test_entry_has_correct_file_count(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        for i in range(5):
            _make_image(folder / f"{i}.jpg")

        lib   = self._lib(tmp_path)
        entry = update_folder(lib, folder, Settings())
        assert entry.file_count == 5

    def test_entry_stored_in_library(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")

        lib   = self._lib(tmp_path)
        entry = update_folder(lib, folder, Settings())
        assert lib.get_folder(str(folder.resolve())) is not None

    def test_entry_has_fingerprint(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")

        lib   = self._lib(tmp_path)
        entry = update_folder(lib, folder, Settings())
        assert len(entry.folder_fingerprint) == 64

    def test_progress_cb_called_for_each_file(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        for i in range(3):
            _make_image(folder / f"{i}.jpg")

        lib   = self._lib(tmp_path)
        calls: list[tuple] = []
        update_folder(lib, folder, Settings(), progress_cb=lambda n, i, t: calls.append((n, i, t)))
        assert len(calls) == 3

    def test_non_recursive_skips_subfolder_images(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "top.jpg")
        sub = folder / "sub"
        sub.mkdir()
        _make_image(sub / "deep.jpg")

        s = Settings()
        s.recursive = False
        lib   = self._lib(tmp_path)
        entry = update_folder(lib, folder, s)
        assert entry.file_count == 1   # only top.jpg

    def test_recursive_includes_subfolder_images(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "top.jpg")
        sub = folder / "sub"
        sub.mkdir()
        _make_image(sub / "deep.jpg")

        s = Settings()
        s.recursive = True
        lib   = self._lib(tmp_path)
        entry = update_folder(lib, folder, s)
        assert entry.file_count == 2


# ═════════════════════════════════════════════════════════════════════════════
# Step 3 — Scanner cache integration tests
# ═════════════════════════════════════════════════════════════════════════════

class TestScannerCacheIntegration:
    """Tests for collect_images() library_cache / trust_library parameters."""

    def _settings(self, n_threads: int = 1) -> Settings:
        s = Settings()
        s.recursive        = False
        s.use_rawpy        = False
        s.use_dual_hash    = True
        s.use_histogram    = True
        s.dark_protection  = False
        s.collect_metadata = False
        s.scan_threads     = n_threads
        s.min_dimension    = 0
        return s

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _collect(folder: Path, cache: "dict | None" = None,
                 trust: bool = False, threads: int = 1) -> list:
        from scanner import collect_images
        s = Settings()
        s.recursive        = False
        s.use_rawpy        = False
        s.use_dual_hash    = True
        s.use_histogram    = True
        s.dark_protection  = False
        s.collect_metadata = False
        s.scan_threads     = threads
        s.min_dimension    = 0
        return collect_images(
            folder       = folder,
            skip_paths   = set(),
            settings     = s,
            library_cache = cache,
            trust_library = trust,
        )

    # ── basic backward-compat ─────────────────────────────────────────────

    def test_no_cache_returns_records(self, tmp_path):
        """collect_images works normally when library_cache is None."""
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg")
        _make_image(folder / "b.jpg")

        records = self._collect(folder)
        assert len(records) == 2

    # ── cache population ──────────────────────────────────────────────────

    def test_fresh_scan_populates_cache(self, tmp_path):
        """After a scan with an empty cache dict, cache contains new entries."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg")

        cache: dict = {}
        self._collect(folder, cache=cache)

        assert len(cache) == 1
        key = str(img.resolve())
        assert key in cache
        assert cache[key].phash != ""

    def test_cache_populated_for_all_images(self, tmp_path):
        """All hashed images end up in the cache dict."""
        folder = tmp_path / "photos"
        folder.mkdir()
        for i in range(4):
            _make_image(folder / f"{i}.jpg", color=(i * 50, 0, 0))

        cache: dict = {}
        self._collect(folder, cache=cache)
        assert len(cache) == 4

    # ── cache hit / miss ─────────────────────────────────────────────────

    def test_cache_hit_returns_same_phash(self, tmp_path):
        """Second scan with populated cache returns the same phash as the first."""
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg", color=(100, 150, 200))

        cache: dict = {}
        recs_first  = self._collect(folder, cache=cache)
        phash_first = str(recs_first[0].phash)

        recs_second  = self._collect(folder, cache=cache)
        phash_second = str(recs_second[0].phash)

        assert phash_first == phash_second

    def test_stale_file_is_rehashed(self, tmp_path):
        """A file overwritten with new content triggers a cache miss and re-hash."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg", color=(10, 10, 10))

        cache: dict = {}
        self._collect(folder, cache=cache)
        mtime_before = cache[str(img.resolve())].mtime

        # Overwrite with different content and bump mtime
        time.sleep(0.05)
        _make_image(img, color=(240, 240, 240))
        os.utime(img, None)

        self._collect(folder, cache=cache)
        mtime_after = cache[str(img.resolve())].mtime

        # Cache entry mtime must be refreshed to the new write time
        assert mtime_after != mtime_before

    def test_cache_updated_after_rehash(self, tmp_path):
        """After a stale file is re-hashed, the cache entry is refreshed."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg", color=(10, 10, 10))

        cache: dict = {}
        self._collect(folder, cache=cache)
        mtime_before = cache[str(img.resolve())].mtime

        time.sleep(0.05)
        _make_image(img, color=(240, 240, 240))
        os.utime(img, None)

        self._collect(folder, cache=cache)
        mtime_after = cache[str(img.resolve())].mtime

        assert mtime_after != mtime_before

    # ── trust_library ─────────────────────────────────────────────────────

    def test_trust_library_skips_staleness_check(self, tmp_path):
        """trust_library=True returns the cached phash even if the file changed."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg", color=(10, 10, 10))

        cache: dict = {}
        recs_first  = self._collect(folder, cache=cache)
        phash_first = str(recs_first[0].phash)

        # Overwrite the file
        time.sleep(0.05)
        _make_image(img, color=(240, 240, 240))
        os.utime(img, None)

        # trust_library=True → must return the OLD (cached) phash
        recs_trusted = self._collect(folder, cache=cache, trust=True)
        assert str(recs_trusted[0].phash) == phash_first

    # ── corrupted cache entry ─────────────────────────────────────────────

    def test_corrupted_cache_entry_falls_back_to_fresh_hash(self, tmp_path):
        """A broken FileRecord in the cache triggers a fresh hash without crashing."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg")

        class _Broken:
            def is_stale(self, path):
                return False  # pretend not stale so we enter the cache branch

            def to_image_record(self):
                raise RuntimeError("simulated corruption")

        cache: dict = {str(img.resolve()): _Broken()}
        # Should not raise; should fall through to fresh hash
        records = self._collect(folder, cache=cache)
        assert len(records) == 1
        # The broken entry should be replaced with a real FileRecord
        assert hasattr(cache[str(img.resolve())], "phash")

    # ── threaded path ─────────────────────────────────────────────────────

    def test_threaded_scan_populates_cache(self, tmp_path):
        """Threaded collect_images also populates library_cache."""
        folder = tmp_path / "photos"
        folder.mkdir()
        for i in range(4):
            _make_image(folder / f"{i}.jpg", color=(i * 50, 0, 0))

        cache: dict = {}
        self._collect(folder, cache=cache, threads=2)
        assert len(cache) == 4

    def test_threaded_cache_hit_skips_rehash(self, tmp_path):
        """Second threaded scan reuses cache and returns same phashes."""
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg", color=(80, 120, 160))

        cache: dict = {}
        recs_first  = self._collect(folder, cache=cache, threads=2)
        recs_second = self._collect(folder, cache=cache, threads=2)

        assert str(recs_first[0].phash) == str(recs_second[0].phash)

    def test_threaded_stale_file_rehashed(self, tmp_path):
        """Threaded path re-hashes stale files and refreshes the cache entry."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg", color=(10, 10, 10))

        cache: dict = {}
        self._collect(folder, cache=cache, threads=2)
        mtime_before = cache[str(img.resolve())].mtime

        time.sleep(0.05)
        _make_image(img, color=(240, 240, 240))
        os.utime(img, None)

        self._collect(folder, cache=cache, threads=2)
        mtime_after = cache[str(img.resolve())].mtime

        assert mtime_after != mtime_before

    # ── cache key format ─────────────────────────────────────────────────

    def test_cache_key_is_resolved_absolute_path(self, tmp_path):
        """Cache keys are str(path.resolve()), matching what is_stale expects."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg")

        cache: dict = {}
        self._collect(folder, cache=cache)

        expected_key = str(img.resolve())
        assert expected_key in cache

    # ── record fields populated from cache ───────────────────────────────

    def test_cached_record_has_correct_dimensions(self, tmp_path):
        """ImageRecord reconstructed from cache has correct width/height."""
        folder = tmp_path / "photos"
        folder.mkdir()
        _make_image(folder / "a.jpg", size=(32, 24))

        cache: dict = {}
        self._collect(folder, cache=cache)

        # Second scan: comes from cache
        records = self._collect(folder, cache=cache)
        assert records[0].width  == 32
        assert records[0].height == 24

    def test_cached_record_has_file_size(self, tmp_path):
        """ImageRecord from cache has the correct file_size."""
        folder = tmp_path / "photos"
        folder.mkdir()
        img = _make_image(folder / "a.jpg")

        cache: dict = {}
        recs_first  = self._collect(folder, cache=cache)
        recs_second = self._collect(folder, cache=cache)

        assert recs_second[0].file_size == recs_first[0].file_size
        assert recs_second[0].file_size == img.stat().st_size


# ═════════════════════════════════════════════════════════════════════════════
# Entry point for direct execution
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
