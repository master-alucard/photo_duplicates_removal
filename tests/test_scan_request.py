"""
tests/test_scan_request.py — Unit tests for the shared scan input object
(Stage 3 of the 2026-07 refactor plan).

ScanRequest gives both scan paths one input shape, and owns two rules that were
previously duplicated in each worker and could therefore drift apart:

  * LibraryOptions.effective_trust — staleness checks may only be skipped when
    Library mode is on AND trust is enabled.
  * frozen_out_folder — the output folder as of scan start. Re-reading the UI
    field at apply-time is the defect behind #159/#161 and #2149.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scan_request import MODE_COMPARE, MODE_SINGLE, LibraryOptions, ScanRequest


class _FakeSettings:
    pass


# ── LibraryOptions ────────────────────────────────────────────────────────────

class TestLibraryOptions:

    @pytest.mark.parametrize("use,trust,expected", [
        (True,  True,  True),
        (True,  False, False),
        (False, True,  False),   # the one that matters: browse mode
        (False, False, False),
    ])
    def test_effective_trust_truth_table(self, use, trust, expected):
        assert LibraryOptions(use=use, trust=trust).effective_trust is expected

    def test_defaults_are_safe(self):
        opts = LibraryOptions()
        assert opts.use is False and opts.trust is False
        assert opts.effective_trust is False

    def test_is_immutable(self):
        """Options are captured at scan start; mutating them mid-scan would
        reintroduce the live-read class of bug."""
        opts = LibraryOptions(use=True, trust=True)
        with pytest.raises(Exception):
            opts.use = False        # type: ignore[misc]


# ── construction ──────────────────────────────────────────────────────────────

class TestSingleMode:

    def test_minimal_construction(self, tmp_path):
        req = ScanRequest.single(tmp_path / "src", tmp_path / "out", _FakeSettings())
        assert req.mode == MODE_SINGLE
        assert req.is_compare is False
        assert req.src == tmp_path / "src"
        assert req.primary_library.effective_trust is False

    def test_library_flags_are_carried(self, tmp_path):
        req = ScanRequest.single(tmp_path / "s", tmp_path / "o", _FakeSettings(),
                                 use_library=True, trust_library=True)
        assert req.primary_library.effective_trust is True

    def test_primary_folder_is_src(self, tmp_path):
        req = ScanRequest.single(tmp_path / "s", tmp_path / "o", _FakeSettings())
        assert req.primary_folder == tmp_path / "s"

    def test_folders_to_hash_is_single_entry(self, tmp_path):
        req = ScanRequest.single(tmp_path / "s", tmp_path / "o", _FakeSettings())
        folders = req.folders_to_hash
        assert len(folders) == 1
        assert folders[0][0] == tmp_path / "s"

    def test_missing_src_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="requires src"):
            ScanRequest(mode=MODE_SINGLE, out_folder=tmp_path, settings=_FakeSettings())

    def test_string_paths_are_coerced(self, tmp_path):
        req = ScanRequest.single(str(tmp_path / "s"), str(tmp_path / "o"),
                                 _FakeSettings())
        assert isinstance(req.src, Path)
        assert isinstance(req.out_folder, Path)


class TestCompareMode:

    def _req(self, tmp_path, **kw):
        return ScanRequest.compare(
            tmp_path / "main", tmp_path / "check", tmp_path / "out",
            _FakeSettings(), **kw)

    def test_minimal_construction(self, tmp_path):
        req = self._req(tmp_path)
        assert req.mode == MODE_COMPARE
        assert req.is_compare is True
        assert req.main_folder == tmp_path / "main"
        assert req.check_folder == tmp_path / "check"

    def test_per_folder_library_flags_are_independent(self, tmp_path):
        req = self._req(tmp_path, use_lib_main=True, trust_main=True,
                        use_lib_check=False, trust_check=True)
        assert req.primary_library.effective_trust is True
        assert req.check_library.effective_trust is False, (
            "check folder in browse mode must not inherit main's trust"
        )

    def test_primary_folder_is_main(self, tmp_path):
        assert self._req(tmp_path).primary_folder == tmp_path / "main"

    def test_folders_to_hash_is_main_then_check(self, tmp_path):
        req = self._req(tmp_path, use_lib_main=True, use_lib_check=False)
        folders = req.folders_to_hash
        assert [f for f, _ in folders] == [tmp_path / "main", tmp_path / "check"]
        assert folders[0][1].use is True and folders[1][1].use is False, (
            "each folder must carry its own library options, in order"
        )

    def test_missing_folders_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="main_folder and check_folder"):
            ScanRequest(mode=MODE_COMPARE, out_folder=tmp_path,
                        settings=_FakeSettings(), main_folder=tmp_path / "m")


# ── shared behavior ───────────────────────────────────────────────────────────

class TestFrozenOutFolder:

    def test_matches_out_folder_at_construction(self, tmp_path):
        req = ScanRequest.single(tmp_path / "s", tmp_path / "out", _FakeSettings())
        assert req.frozen_out_folder == str(tmp_path / "out")

    def test_available_for_compare_mode_too(self, tmp_path):
        req = ScanRequest.compare(tmp_path / "m", tmp_path / "c", tmp_path / "out",
                                  _FakeSettings())
        assert req.frozen_out_folder == str(tmp_path / "out")


def test_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown scan mode"):
        ScanRequest(mode="sideways", out_folder=tmp_path, settings=_FakeSettings())
