"""
tests/test_scan_characterization.py — Characterization tests for the scan
orchestration in main.py (Stage 0 of the 2026-07 refactor plan).

WHY THIS FILE EXISTS
--------------------
Everything *around* the scan orchestration is well covered (scanner, library,
mover, merger, report_viewer), but ``App._worker`` and ``App._custom_worker``
— the glue that wires them together — had no direct coverage at all.  That gap
is where the recent bug class lived: the trash-path defect (#159/#161) and the
compare-scan wrong-out-folder defect (#2149) were the *same* bug appearing
twice, because the two scan paths are separate code.

These tests pin the CURRENT behavior of both workers so the upcoming
unification refactor (Stages 2-4) breaks loudly instead of silently.  They are
characterization tests: they assert what the code does today, not what it
ideally should do.  If a later stage changes an assertion here, that is a
behavior change and must be a deliberate, separately-reviewed decision.

The workers are driven directly against a lightweight stub rather than a real
``App``.  Between them they touch only ~14 attributes on ``self`` (progress
callback, stop/pause flags, ``root.after``, and result fields), so a stub
exercises the REAL production code path with no Tk event loop.

THE INVARIANT THAT MATTERS MOST
-------------------------------
In Compare Scan, files in the Main (reference) folder must NEVER be offered for
trashing — only Check-folder files may be.  ``test_main_folder_files_never_land
_in_previews`` is the data-safety guard for that; treat a failure there as a
release blocker, not a test bug.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _make_photo(path: Path, seed: int) -> None:
    """Write a structured, non-uniform image so pHash is stable and distinct
    seeds land far apart in Hamming distance."""
    img = Image.new("RGB", (320, 240), (seed * 37 % 256, seed * 83 % 256, 200))
    draw = ImageDraw.Draw(img)
    for i in range(12):
        x = (seed * 13 + i * 29) % 300
        y = (seed * 7 + i * 17) % 220
        color = ((seed + i * 31) % 256, (seed * 5 + i * 47) % 256, (i * 21) % 256)
        draw.rectangle([x, y, x + 40 + (i % 3) * 15, y + 25 + (i % 4) * 10], fill=color)
        draw.ellipse([x // 2, y // 2, x // 2 + 30, y // 2 + 30], fill=color)
    img.save(path, "JPEG", quality=92)


class _WorkerStub:
    """Minimal stand-in for ``App`` covering exactly what the two scan workers
    touch.  ``root.after`` runs the callback synchronously so completion is
    observable without a Tk event loop."""

    def __init__(self) -> None:
        # shared
        self.progress: list[tuple] = []
        self.done_calls: list[tuple] = []
        self.error_calls: list[tuple] = []

        class _Root:
            @staticmethod
            def after(_delay, fn=None, *a):
                if callable(fn):
                    return fn()
                return None

        self.root = _Root()

        # regular-scan surface
        self._stop_flag = [False]
        self._pause_flag = [False]
        self.scan_records: list = []
        self.scan_groups: list = []
        self._solo_originals: list = []
        self._broken_files: list = []
        self.report_path = None
        self._paused_state = None
        self._last_scan_out_folder = None

        # compare-scan surface
        self._custom_stop_flag = [False]
        self._custom_pause_flag = [False]
        self._custom_groups: list = []
        self._custom_broken: list = []
        self._custom_report_path = None
        self._custom_paused_state = None

    # progress callbacks
    def _progress_cb(self, msg, done, total, phase):
        self.progress.append((msg, done, total, phase))

    def _custom_progress_cb(self, msg, done, total, phase):
        self.progress.append((msg, done, total, phase))

    # completion callbacks
    def _on_done(self, msg, success=True, paused=False, **kw):
        self.done_calls.append((msg, success, paused))

    def _on_custom_done(self, msg, success=True, paused=False, **kw):
        self.done_calls.append((msg, success, paused))

    def _on_error(self, msg, tb=""):
        self.error_calls.append((msg, tb))

    def _on_custom_error(self, msg, tb=""):
        self.error_calls.append((msg, tb))

    def _save_pause_state(self, *a, **kw):
        return None


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Point the hash library at a throwaway dir so tests never read or write
    the real user library (the workers load it unconditionally)."""
    import library
    lib_dir = tmp_path / "_library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(library, "get_library_dir", lambda: lib_dir)
    return lib_dir


def _settings(src: Path, out: Path, **over):
    from config import Settings
    s = Settings(
        src_folder=str(src),
        out_folder=str(out),
        recursive=False,
        scan_threads=2,
    )
    s.dry_run = True            # default; never move fixture files unless asked
    s.include_videos = False    # keep characterization on the image path
    s.collect_metadata = False
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _all_paths(groups) -> set[str]:
    out = set()
    for g in groups:
        for r in list(g.originals) + list(g.previews):
            out.add(str(Path(r.path).resolve()))
    return out


def _all_names(groups) -> set[str]:
    """File names only.  Membership checks must not run against full paths:
    pytest's tmp_path is derived from the test name, so a substring like
    "main_only" would match the containing directory and silently pass."""
    return {Path(p).name for p in _all_paths(groups)}


def _preview_paths(groups) -> set[str]:
    return {str(Path(r.path).resolve()) for g in groups for r in g.previews}


def _original_paths(groups) -> set[str]:
    return {str(Path(r.path).resolve()) for g in groups for r in g.originals}


# ── regular scan (App._worker) ────────────────────────────────────────────────

class TestRegularScanCharacterization:

    def _run(self, tmp_path, **over):
        import main
        src = tmp_path / "src"
        out = tmp_path / "out"
        src.mkdir()
        # one duplicate pair + one unique image
        original = src / "photo_a.jpg"
        _make_photo(original, seed=3)
        shutil.copy2(original, src / "photo_a_copy.jpg")
        _make_photo(src / "photo_b.jpg", seed=44)

        stub = _WorkerStub()
        settings = _settings(src, out, **over)
        main.App._worker(stub, src, out, settings)
        return stub, src, out

    def test_duplicate_pair_is_detected_and_results_published(self, tmp_path):
        stub, _src, _out = self._run(tmp_path)

        assert stub.error_calls == [], f"worker raised: {stub.error_calls}"
        assert len(stub.scan_records) == 3, "all three images should be hashed"
        assert len(stub.scan_groups) == 1, "the identical pair forms exactly one group"
        assert stub.done_calls, "worker must report completion"
        assert stub.done_calls[-1][1] is True, "completion should report success"

    def test_group_splits_into_one_original_and_one_preview(self, tmp_path):
        stub, _src, _out = self._run(tmp_path)
        g = stub.scan_groups[0]
        assert len(g.originals) == 1
        assert len(g.previews) == 1
        # The unique image is never part of a duplicate group.
        assert not any("photo_b" in n for n in _all_names(stub.scan_groups))

    def test_out_folder_is_frozen_at_scan_time(self, tmp_path):
        """Locks in the #159/#2149 fix: the viewer and accept-move must use the
        folder this scan ran with, not whatever the field says later."""
        stub, _src, out = self._run(tmp_path)
        assert stub._last_scan_out_folder == str(out)

    def test_report_is_generated(self, tmp_path):
        stub, _src, _out = self._run(tmp_path)
        assert stub.report_path is not None
        assert Path(stub.report_path).exists()

    def test_dry_run_moves_nothing(self, tmp_path):
        stub, src, out = self._run(tmp_path, dry_run=True)
        remaining = sorted(p.name for p in src.iterdir() if p.is_file())
        assert remaining == ["photo_a.jpg", "photo_a_copy.jpg", "photo_b.jpg"]
        assert not (out / "trash").exists()

    def test_real_run_moves_previews_only_and_keeps_originals(self, tmp_path):
        """The core data contract: exactly one of the identical pair is
        trashed, the other stays, and the unique file is untouched."""
        stub, src, out = self._run(tmp_path, dry_run=False)

        trash = out / "trash"
        assert trash.exists(), "non-dry-run should create the trash folder"
        trashed = [p for p in trash.rglob("*") if p.is_file()]
        assert len(trashed) == 1, f"expected exactly one file trashed, got {trashed}"

        survivors = sorted(p.name for p in src.iterdir() if p.is_file())
        assert "photo_b.jpg" in survivors, "unique image must never be moved"
        pair_left = [n for n in survivors if n.startswith("photo_a")]
        assert len(pair_left) == 1, "exactly one of the duplicate pair must remain"

    def test_corrupt_and_zero_byte_files_do_not_break_the_scan(self, tmp_path):
        import main
        src = tmp_path / "src"
        out = tmp_path / "out"
        src.mkdir()
        _make_photo(src / "good.jpg", seed=7)
        (src / "empty.jpg").write_bytes(b"")
        (src / "garbage.jpg").write_bytes(b"\xff\xd8not-a-real-jpeg")

        stub = _WorkerStub()
        main.App._worker(stub, src, out, _settings(src, out))

        assert stub.error_calls == [], "unreadable files must not abort the scan"
        assert stub.done_calls and stub.done_calls[-1][1] is True
        good = [r for r in stub.scan_records if "good" in str(r.path)]
        assert len(good) == 1, "the valid image must still be hashed"

    def test_stop_flag_aborts_without_success(self, tmp_path):
        import main
        src = tmp_path / "src"
        out = tmp_path / "out"
        src.mkdir()
        for i in range(6):
            _make_photo(src / f"s{i}.jpg", seed=i + 60)

        stub = _WorkerStub()
        stub._stop_flag[0] = True    # pre-set: abort at the first checkpoint
        main.App._worker(stub, src, out, _settings(src, out))

        assert stub.done_calls, "a stopped scan must still report completion"
        assert stub.done_calls[-1][1] is False, "stopped scan must not report success"


# ── compare scan (App._custom_worker) ─────────────────────────────────────────

class TestCompareScanCharacterization:
    """Compare Scan: Main is the reference (never trashed), Check holds the
    candidates."""

    def _build(self, tmp_path):
        main_dir = tmp_path / "main"
        check_dir = tmp_path / "check"
        out_dir = tmp_path / "out"
        main_dir.mkdir()
        check_dir.mkdir()

        # cross-folder duplicate: same image in both folders
        shared = main_dir / "shared.jpg"
        _make_photo(shared, seed=11)
        shutil.copy2(shared, check_dir / "shared_copy.jpg")

        # main-only image (must be dropped from results entirely)
        _make_photo(main_dir / "main_only.jpg", seed=22)

        # within-check duplicate pair
        dupe = check_dir / "inner.jpg"
        _make_photo(dupe, seed=33)
        shutil.copy2(dupe, check_dir / "inner_copy.jpg")

        # check-only unique image
        _make_photo(check_dir / "check_only.jpg", seed=55)

        return main_dir, check_dir, out_dir

    def _run(self, tmp_path, **over):
        import main
        main_dir, check_dir, out_dir = self._build(tmp_path)
        stub = _WorkerStub()
        settings = _settings(main_dir, out_dir, **over)
        main.App._custom_worker(stub, main_dir, check_dir, out_dir, settings)
        return stub, main_dir, check_dir, out_dir

    def test_completes_successfully(self, tmp_path):
        stub, *_ = self._run(tmp_path)
        assert stub.error_calls == [], f"worker raised: {stub.error_calls}"
        assert stub.done_calls and stub.done_calls[-1][1] is True

    def test_main_folder_files_never_land_in_previews(self, tmp_path):
        """DATA-SAFETY INVARIANT. Previews are what the UI offers for trashing;
        a Main-folder file appearing there means the reference library is at
        risk. A failure here is a release blocker."""
        stub, main_dir, _check, _out = self._run(tmp_path)

        main_res = str(main_dir.resolve())
        offending = [p for p in _preview_paths(stub._custom_groups)
                     if p.startswith(main_res)]
        assert offending == [], (
            f"Main-folder files offered for trashing: {offending}"
        )

    def test_cross_folder_match_puts_main_in_originals_and_check_in_previews(self, tmp_path):
        stub, main_dir, check_dir, _out = self._run(tmp_path)

        cross = [g for g in stub._custom_groups
                 if any("shared" in str(r.path) for r in g.originals + g.previews)]
        assert len(cross) == 1, "the cross-folder pair should form one group"
        g = cross[0]
        assert all(str(Path(r.path).resolve()).startswith(str(main_dir.resolve()))
                   for r in g.originals), "originals must come from Main"
        assert all(str(Path(r.path).resolve()).startswith(str(check_dir.resolve()))
                   for r in g.previews), "previews must come from Check"

    def test_main_only_duplicates_are_dropped_from_results(self, tmp_path):
        stub, _main, _check, _out = self._run(tmp_path)
        assert not any("main_only" in n for n in _all_names(stub._custom_groups)), (
            "a file present only in Main is not a Compare Scan result"
        )

    def test_within_check_duplicates_keep_one_original(self, tmp_path):
        stub, _main, check_dir, _out = self._run(tmp_path)

        inner = [g for g in stub._custom_groups
                 if any("inner" in str(r.path) for r in g.originals + g.previews)]
        assert len(inner) == 1, "the within-Check pair should form one group"
        g = inner[0]
        assert len(g.originals) == 1, "one copy is kept as the original"
        assert len(g.previews) == 1, "the other is a trash candidate"
        assert all(str(Path(r.path).resolve()).startswith(str(check_dir.resolve()))
                   for r in g.originals + g.previews)

    def test_unique_check_file_is_not_grouped(self, tmp_path):
        stub, *_ = self._run(tmp_path)
        assert not any("check_only" in n for n in _all_names(stub._custom_groups))

    def test_stop_flag_aborts_without_success(self, tmp_path):
        import main
        main_dir, check_dir, out_dir = self._build(tmp_path)
        stub = _WorkerStub()
        stub._custom_stop_flag[0] = True
        main.App._custom_worker(stub, main_dir, check_dir, out_dir,
                                _settings(main_dir, out_dir))
        assert stub.done_calls and stub.done_calls[-1][1] is False

    def test_broken_files_are_collected_not_fatal(self, tmp_path):
        import main
        main_dir = tmp_path / "main"
        check_dir = tmp_path / "check"
        out_dir = tmp_path / "out"
        main_dir.mkdir()
        check_dir.mkdir()
        shared = main_dir / "ok.jpg"
        _make_photo(shared, seed=99)
        shutil.copy2(shared, check_dir / "ok_copy.jpg")
        (check_dir / "broken.jpg").write_bytes(b"\xff\xd8nope")

        stub = _WorkerStub()
        main.App._custom_worker(stub, main_dir, check_dir, out_dir,
                                _settings(main_dir, out_dir))

        assert stub.error_calls == [], "an unreadable file must not abort the scan"
        assert stub.done_calls and stub.done_calls[-1][1] is True
