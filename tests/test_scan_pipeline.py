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


# ── compare-scan reclassification (the data-safety boundary) ──────────────────

class _Rec:
    """Minimal stand-in for an ImageRecord: only .path is consulted."""

    def __init__(self, path):
        self.path = Path(path)

    def __repr__(self):
        return f"<{self.path.name}>"


class _Grp:
    def __init__(self, originals, previews):
        self.originals = list(originals)
        self.previews = list(previews)


def _folders(tmp_path):
    main = tmp_path / "main"
    check = tmp_path / "check"
    main.mkdir()
    check.mkdir()
    return main, check


class TestReclassifyCompareGroups:
    """Main-folder files must never end up as trash candidates."""

    def test_cross_folder_group_puts_main_in_originals(self, tmp_path):
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        g = _Grp([_Rec(main / "a.jpg")], [_Rec(check / "a_copy.jpg")])

        cross, within = reclassify_compare_groups([g], main, check)

        assert len(cross) == 1 and not within
        assert [r.path.name for r in cross[0].originals] == ["a.jpg"]
        assert [r.path.name for r in cross[0].previews] == ["a_copy.jpg"]

    def test_main_side_wins_regardless_of_input_labelling(self, tmp_path):
        """Even if the grouper labelled the Check copy as the original, the
        Main copy must be promoted -- otherwise the reference file is trashed."""
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        g = _Grp([_Rec(check / "a_copy.jpg")], [_Rec(main / "a.jpg")])

        cross, _ = reclassify_compare_groups([g], main, check)

        assert [r.path.name for r in cross[0].originals] == ["a.jpg"]
        assert [r.path.name for r in cross[0].previews] == ["a_copy.jpg"]

    def test_main_only_group_is_dropped(self, tmp_path):
        """Duplicates living only inside Main are not a Compare Scan result."""
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        g = _Grp([_Rec(main / "a.jpg")], [_Rec(main / "a_dupe.jpg")])

        cross, within = reclassify_compare_groups([g], main, check)

        assert not cross and not within

    def test_within_check_group_keeps_first_as_original(self, tmp_path):
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        g = _Grp([_Rec(check / "x.jpg")], [_Rec(check / "x_copy.jpg")])

        cross, within = reclassify_compare_groups([g], main, check)

        assert not cross and len(within) == 1
        assert len(within[0].originals) == 1
        assert len(within[0].previews) == 1

    def test_lone_check_file_is_dropped(self, tmp_path):
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        g = _Grp([_Rec(check / "solo.jpg")], [])

        cross, within = reclassify_compare_groups([g], main, check)

        assert not cross and not within

    def test_no_main_file_ever_becomes_a_preview(self, tmp_path):
        """The invariant, over a mixed batch: nothing under Main may be offered
        for trashing."""
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        groups = [
            _Grp([_Rec(main / "a.jpg")], [_Rec(check / "a2.jpg")]),
            _Grp([_Rec(check / "b.jpg")], [_Rec(check / "b2.jpg")]),
            _Grp([_Rec(main / "c.jpg")], [_Rec(main / "c2.jpg")]),
            _Grp([_Rec(check / "d.jpg")], [_Rec(main / "d2.jpg")]),
        ]

        cross, within = reclassify_compare_groups(groups, main, check)

        main_res = main.resolve()
        for g in cross + within:
            for rec in g.previews:
                assert not str(rec.path.resolve()).startswith(str(main_res)), (
                    f"Main-folder file offered for trashing: {rec.path}"
                )

    def test_empty_input(self, tmp_path):
        from scan_pipeline import reclassify_compare_groups
        main, check = _folders(tmp_path)
        assert reclassify_compare_groups([], main, check) == ([], [])

    def test_nested_check_folder_puts_file_in_both_lists(self, tmp_path):
        """DOCUMENTS PRE-EXISTING BEHAVIOR -- not an endorsement.

        When Check is nested inside Main, a Check file matches both folders, so
        it is placed in BOTH originals and previews: simultaneously a keeper and
        a trash candidate. _start_custom_scan rejects Main == Check but does not
        reject nesting, so this is reachable (Main=E:\Photos, Check=E:\Photos\2024).

        Preserved verbatim through the Stage 4b extraction. Filed separately
        rather than fixed inside a behavior-preserving refactor; if the fix lands,
        update this test to assert the corrected behavior.
        """
        from scan_pipeline import reclassify_compare_groups
        main = tmp_path / "main"
        check = main / "sub"
        check.mkdir(parents=True)
        nested = _Rec(check / "a_copy.jpg")
        g = _Grp([_Rec(main / "a.jpg")], [nested])

        cross, _ = reclassify_compare_groups([g], main, check)

        names_o = [r.path.name for r in cross[0].originals]
        names_p = [r.path.name for r in cross[0].previews]
        assert "a_copy.jpg" in names_o and "a_copy.jpg" in names_p, (
            "current behavior: the nested file appears on both sides"
        )
