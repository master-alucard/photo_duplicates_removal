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

    def test_nested_check_folder_classifies_exclusively(self, tmp_path):
        """#2298 FIXED. Was: a Check folder nested inside Main put the same file
        in BOTH originals and previews -- shown as the keeper while also being
        offered for trashing.

        Now classification is exclusive and the deeper folder wins: files under
        Check are Check files, so they stay trashable and never appear as
        originals.
        """
        from scan_pipeline import reclassify_compare_groups
        main = tmp_path / "main"
        check = main / "sub"
        check.mkdir(parents=True)
        g = _Grp([_Rec(main / "a.jpg")], [_Rec(check / "a_copy.jpg")])

        cross, _ = reclassify_compare_groups([g], main, check)

        names_o = [r.path.name for r in cross[0].originals]
        names_p = [r.path.name for r in cross[0].previews]
        assert names_o == ["a.jpg"], "only the outer-folder file is the keeper"
        assert names_p == ["a_copy.jpg"], "the nested file is the candidate"
        assert not set(names_o) & set(names_p), (
            "no file may be an original and a trash candidate at once"
        )

    def test_nested_main_folder_protects_the_inner_reference(self, tmp_path):
        """The other nesting direction (#2298). Main inside Check means the
        inner folder is the REFERENCE, so the deeper-folder-wins rule must
        protect it rather than offer it for trashing."""
        from scan_pipeline import reclassify_compare_groups
        check = tmp_path / "library"
        main = check / "reference"
        main.mkdir(parents=True)
        g = _Grp([_Rec(main / "keep.jpg")], [_Rec(check / "dupe.jpg")])

        cross, _ = reclassify_compare_groups([g], main, check)

        names_o = [r.path.name for r in cross[0].originals]
        names_p = [r.path.name for r in cross[0].previews]
        assert names_o == ["keep.jpg"], "the inner Main file must stay protected"
        assert names_p == ["dupe.jpg"]
        assert not set(names_o) & set(names_p)

    def test_nested_never_yields_a_file_on_both_sides(self, tmp_path):
        """The #2298 invariant over a batch of groups."""
        from scan_pipeline import reclassify_compare_groups
        main = tmp_path / "photos"
        check = main / "2024"
        check.mkdir(parents=True)
        groups = [
            _Grp([_Rec(main / "a.jpg")], [_Rec(check / "a2.jpg")]),
            _Grp([_Rec(check / "b.jpg")], [_Rec(check / "b2.jpg")]),
            _Grp([_Rec(main / "c.jpg")], [_Rec(main / "c2.jpg")]),
        ]

        cross, within = reclassify_compare_groups(groups, main, check)

        for g in cross + within:
            o = {id(r) for r in g.originals}
            p = {id(r) for r in g.previews}
            assert not (o & p), "a record appeared as both keeper and candidate"


class TestFolderNesting:

    def test_detects_child_inside_parent(self, tmp_path):
        from scan_pipeline import folders_are_nested
        parent = tmp_path / "p"
        child = parent / "c"
        child.mkdir(parents=True)
        assert folders_are_nested(parent, child) is True
        assert folders_are_nested(child, parent) is True, "direction-agnostic"

    def test_siblings_are_not_nested(self, tmp_path):
        from scan_pipeline import folders_are_nested
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert folders_are_nested(a, b) is False

    def test_identical_folders_are_not_nested(self, tmp_path):
        """Main == Check is rejected by its own check with a clearer message;
        reporting it as "nested" would show the wrong warning."""
        from scan_pipeline import folders_are_nested
        assert folders_are_nested(tmp_path, tmp_path) is False

    def test_inner_folder_of_returns_the_deeper_one(self, tmp_path):
        from scan_pipeline import inner_folder_of
        parent = tmp_path / "p"
        child = parent / "c"
        child.mkdir(parents=True)
        assert inner_folder_of(parent, child) == child.resolve()
        assert inner_folder_of(child, parent) == child.resolve()

    def test_inner_folder_of_none_for_unrelated(self, tmp_path):
        from scan_pipeline import inner_folder_of
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert inner_folder_of(a, b) is None
        assert inner_folder_of(tmp_path, tmp_path) is None


# ── solo originals (files that matched nothing) ───────────────────────────────

class TestComputeSoloOriginals:

    def test_records_in_no_group_are_returned(self, tmp_path):
        from scan_pipeline import compute_solo_originals
        a, b, lone = _Rec(tmp_path / "a.jpg"), _Rec(tmp_path / "b.jpg"), _Rec(tmp_path / "lone.jpg")
        groups = [_Grp([a], [b])]

        solo = compute_solo_originals([a, b, lone], groups)

        assert [r.path.name for r in solo] == ["lone.jpg"]

    def test_no_groups_means_everything_is_solo(self, tmp_path):
        from scan_pipeline import compute_solo_originals
        recs = [_Rec(tmp_path / f"{n}.jpg") for n in ("a", "b")]
        assert compute_solo_originals(recs, []) == recs

    def test_all_grouped_means_nothing_is_solo(self, tmp_path):
        from scan_pipeline import compute_solo_originals
        a, b = _Rec(tmp_path / "a.jpg"), _Rec(tmp_path / "b.jpg")
        assert compute_solo_originals([a, b], [_Grp([a], [b])]) == []

    def test_previews_count_as_grouped(self, tmp_path):
        """A record only ever listed as a preview is still grouped -- reporting
        it as unique would show the same file in two places in the UI."""
        from scan_pipeline import compute_solo_originals
        a, b = _Rec(tmp_path / "a.jpg"), _Rec(tmp_path / "b.jpg")
        solo = compute_solo_originals([a, b], [_Grp([a], [b])])
        assert b not in solo

    def test_equivalent_paths_are_recognised_as_grouped(self, tmp_path):
        """Grouping is compared on resolved paths, so a record reached by a
        different-but-equivalent path is not double-reported as unique."""
        from scan_pipeline import compute_solo_originals
        real = tmp_path / "a.jpg"
        real.write_bytes(b"x")
        grouped = _Rec(real)
        same_via_dotdot = _Rec(tmp_path / "sub" / ".." / "a.jpg")
        (tmp_path / "sub").mkdir()

        solo = compute_solo_originals([same_via_dotdot], [_Grp([grouped], [])])

        assert solo == [], "the same file by another path must count as grouped"

    def test_empty_records(self):
        from scan_pipeline import compute_solo_originals
        assert compute_solo_originals([], []) == []
