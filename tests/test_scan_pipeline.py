"""
tests/test_scan_pipeline.py — Unit tests for the shared scan phases
(Stage 4 of the 2026-07 refactor plan).

collect_folder_records replaced SIX near-identical call sites (regular scan
fresh + resume, Compare Scan fresh main/check + resume main/check). These tests
pin the behaviors those sites differed on, so the parameters cannot silently
drift back apart.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from scan_pipeline import collect_folder_records


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    import library
    lib_dir = tmp_path / "_library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(library, "get_library_dir", lambda: lib_dir)
    return lib_dir


def _settings():
    from config import Settings
    s = Settings(recursive=False, scan_threads=2)
    return s


def _make_images(folder: Path, n: int = 3) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        Image.new("RGB", (64, 48), (i * 40 % 256, 90, 200)).save(
            folder / f"img_{i}.jpg", "JPEG", quality=90)
    return folder


# ── basic collection ──────────────────────────────────────────────────────────

class TestCollectFolderRecords:

    def test_returns_one_record_per_image(self, tmp_path):
        src = _make_images(tmp_path / "src", 3)
        records = collect_folder_records(src, set(), _settings())
        assert len(records) == 3

    def test_empty_folder_returns_empty_list(self, tmp_path):
        src = tmp_path / "empty"
        src.mkdir()
        assert collect_folder_records(src, set(), _settings()) == []

    def test_progress_callback_is_forwarded(self, tmp_path):
        src = _make_images(tmp_path / "src", 3)
        seen = []
        collect_folder_records(src, set(), _settings(),
                               progress_cb=lambda *a: seen.append(a))
        assert seen, "progress callback must reach the scanner"

    def test_failed_paths_collects_unreadable_files(self, tmp_path):
        src = _make_images(tmp_path / "src", 1)
        (src / "broken.jpg").write_bytes(b"\xff\xd8nope")
        failed: list = []
        records = collect_folder_records(src, set(), _settings(),
                                         failed_paths=failed)
        assert len(records) == 1
        assert len(failed) == 1

    def test_stop_flag_is_honored(self, tmp_path):
        src = _make_images(tmp_path / "src", 6)
        records = collect_folder_records(src, set(), _settings(),
                                         stop_flag=[True])
        assert len(records) < 6


# ── writeback (the parameter the six call sites disagreed on) ─────────────────

class TestWriteback:

    def test_writeback_on_by_default(self, tmp_path):
        from library import load_scan_cache
        src = _make_images(tmp_path / "src", 2)
        collect_folder_records(src, set(), _settings())
        cache, _ = load_scan_cache(src)
        assert cache, "results should be persisted for the next scan"

    def test_writeback_can_be_disabled(self, tmp_path):
        """The regular scan's resume path passes writeback=False; that
        asymmetry with the compare paths is preserved deliberately."""
        from library import load_scan_cache
        src = _make_images(tmp_path / "src", 2)
        collect_folder_records(src, set(), _settings(), writeback=False)
        cache, _ = load_scan_cache(src)
        assert not cache, "writeback=False must not persist anything"


# ── trust / resume semantics ──────────────────────────────────────────────────

class TestTrustAndResume:

    def test_trust_flag_is_passed_through(self, tmp_path):
        src = _make_images(tmp_path / "src", 1)
        with patch("scan_pipeline.collect_images", return_value=[]) as m:
            collect_folder_records(src, set(), _settings(), trust=True)
        assert m.call_args.kwargs["trust_library"] is True

    def test_default_is_untrusted(self, tmp_path):
        src = _make_images(tmp_path / "src", 1)
        with patch("scan_pipeline.collect_images", return_value=[]) as m:
            collect_folder_records(src, set(), _settings())
        assert m.call_args.kwargs["trust_library"] is False, (
            "staleness checks must run unless trust is explicitly requested"
        )

    def test_resume_records_force_trust_regardless_of_trust_arg(self, tmp_path):
        """Resumed records were just computed by the interrupted run, so they
        are trusted even when the folder itself is in browse mode."""
        src = _make_images(tmp_path / "src", 1)
        with patch("scan_pipeline.collect_images", return_value=[]) as m:
            collect_folder_records(src, set(), _settings(),
                                   trust=False, resume_records=[])
        assert m.call_args.kwargs["trust_library"] is True

    def test_resume_records_are_injected_into_the_cache(self, tmp_path):
        src = _make_images(tmp_path / "src", 2)
        # First pass produces real records to resume from.
        records = collect_folder_records(src, set(), _settings())
        with patch("scan_pipeline.collect_images", return_value=[]) as m:
            collect_folder_records(src, set(), _settings(),
                                   resume_records=records)
        cache = m.call_args.kwargs["library_cache"]
        assert cache, "resumed records must reach the scanner as cache entries"
        for rec in records:
            assert str(rec.path.resolve()) in cache

    def test_unavailable_library_degrades_to_full_rehash(self, tmp_path):
        """A broken/missing library must degrade to re-hashing, never abort.

        (None, None) is exactly what library.load_scan_cache returns when the
        library cannot be read -- it swallows the error itself, so that tuple
        IS the failure mode this phase has to survive.)
        """
        src = _make_images(tmp_path / "src", 2)
        with patch("scan_pipeline.load_scan_cache", return_value=(None, None)):
            records = collect_folder_records(src, set(), _settings())
        assert len(records) == 2, "all files should be hashed from scratch"

    def test_resume_with_unavailable_library_still_injects(self, tmp_path):
        """Resume must work even with no library: the injected records are the
        whole point, and inject_records_into_cache accepts a None cache."""
        src = _make_images(tmp_path / "src", 2)
        records = collect_folder_records(src, set(), _settings())
        with patch("scan_pipeline.load_scan_cache", return_value=(None, None)):
            with patch("scan_pipeline.collect_images", return_value=[]) as m:
                collect_folder_records(src, set(), _settings(),
                                       resume_records=records)
        cache = m.call_args.kwargs["library_cache"]
        assert cache and len(cache) == len(records)
