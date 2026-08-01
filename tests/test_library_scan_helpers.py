"""
tests/test_library_scan_helpers.py — Unit tests for the shared scan-worker cache
helpers in library.py (Stage 2 of the 2026-07 refactor plan).

These three functions were previously duplicated between the regular scan
(inline, twice: fresh path and resume path) and the Compare Scan (as nested
defs). Consolidating them is what stops a fix landing in one path and not the
other -- the failure mode behind #159/#161 and #2149.

All three are best-effort by contract: the library is a cache, so any failure
must degrade to "re-hash the files", never abort a scan. The tests below pin
that contract explicitly, because it is the property the callers rely on.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from library import (
    FileRecord,
    inject_records_into_cache,
    load_scan_cache,
    writeback_scan_results,
)


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Never touch the real user library."""
    import library
    lib_dir = tmp_path / "_library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(library, "get_library_dir", lambda: lib_dir)
    return lib_dir


def _make_record(folder: Path, name: str = "img.jpg"):
    """Hash a real image so the record round-trips through FileRecord."""
    from config import Settings
    from scanner import _hash_image
    p = folder / name
    Image.new("RGB", (64, 48), (120, 30, 200)).save(p, "JPEG", quality=90)
    return _hash_image(p, Settings())


# ── load_scan_cache ───────────────────────────────────────────────────────────

class TestLoadScanCache:

    def test_empty_library_returns_usable_result(self, tmp_path):
        cache, lib = load_scan_cache(tmp_path)
        assert lib is not None, "an empty library still loads"
        assert cache == {} or cache is None

    def test_accepts_str_and_path(self, tmp_path):
        by_path, _ = load_scan_cache(tmp_path)
        by_str, _ = load_scan_cache(str(tmp_path))
        assert type(by_path) is type(by_str)

    def test_library_failure_degrades_to_none_not_exception(self, tmp_path):
        """The caller treats (None, None) as 'no cache, hash everything'. A
        raise here would abort a scan over a cache problem."""
        with patch("library.Library.load", side_effect=OSError("disk gone")):
            cache, lib = load_scan_cache(tmp_path)
        assert cache is None and lib is None

    def test_roundtrip_after_writeback(self, tmp_path):
        rec = _make_record(tmp_path)
        writeback_scan_results(tmp_path, [rec])
        cache, _ = load_scan_cache(tmp_path)
        assert cache, "records written back must be readable again"
        assert str(rec.path) in cache or str(rec.path.resolve()) in cache


# ── inject_records_into_cache ─────────────────────────────────────────────────

class TestInjectRecords:

    def test_none_cache_becomes_dict(self, tmp_path):
        rec = _make_record(tmp_path)
        out = inject_records_into_cache(None, [rec])
        assert isinstance(out, dict)
        assert str(rec.path.resolve()) in out

    def test_existing_entries_are_preserved(self, tmp_path):
        rec = _make_record(tmp_path)
        pre = {"sentinel": "keep-me"}
        out = inject_records_into_cache(pre, [rec])
        assert out["sentinel"] == "keep-me"
        assert str(rec.path.resolve()) in out

    def test_empty_record_list_is_noop(self):
        out = inject_records_into_cache({"a": 1}, [])
        assert out == {"a": 1}

    def test_unconvertible_record_does_not_lose_the_cache(self, tmp_path):
        """One bad record must not discard the good ones -- a resume would
        then silently re-hash the entire folder."""
        good = _make_record(tmp_path, "good.jpg")

        class _Bad:
            path = "not-a-path-object"   # .resolve() will raise

        out = inject_records_into_cache({}, [_Bad(), good])
        assert str(good.path.resolve()) in out
        assert len(out) == 1

    def test_injected_entries_are_filerecords(self, tmp_path):
        rec = _make_record(tmp_path)
        out = inject_records_into_cache({}, [rec])
        assert isinstance(out[str(rec.path.resolve())], FileRecord)


# ── writeback_scan_results ────────────────────────────────────────────────────

class TestWritebackScanResults:

    def test_empty_records_writes_nothing(self, tmp_path, _isolated_library):
        writeback_scan_results(tmp_path, [])
        cache, _ = load_scan_cache(tmp_path)
        assert not cache

    def test_records_are_persisted_with_folder_entry(self, tmp_path):
        import library
        rec = _make_record(tmp_path)
        writeback_scan_results(tmp_path, [rec])

        lib = library.Library.load(library.get_library_dir())
        folders = lib.folders if isinstance(lib.folders, dict) else {
            f.path: f for f in lib.folders}
        assert str(tmp_path.resolve()) in folders, (
            "writeback must register the folder, not just the hashes"
        )

    def test_library_failure_is_silent(self, tmp_path):
        """Writeback runs after a successful scan; a cache-write failure must
        not surface to the user as a scan error."""
        rec = _make_record(tmp_path)
        with patch("library.Library.load", side_effect=OSError("read-only fs")):
            writeback_scan_results(tmp_path, [rec])   # must not raise

    def test_vanished_file_still_recorded(self, tmp_path):
        """A file deleted between hashing and writeback falls back to a record
        without mtime rather than being dropped."""
        rec = _make_record(tmp_path)
        rec.path.unlink()
        writeback_scan_results(tmp_path, [rec])
        cache, _ = load_scan_cache(tmp_path)
        assert cache, "record should still be written without stat info"
