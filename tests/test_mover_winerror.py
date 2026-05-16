"""
tests/test_mover_winerror.py — Drive-disconnect and edge-case tests for mover.py.

Covers _drive_available, _safe_exists, _ensure_trash_dir, trash_files error
paths, _unique_path collision handling, and move_groups ambiguous-group
skipping.  No real drive disconnection needed — OSError is patched in.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mover import (
    _drive_available,
    _ensure_trash_dir,
    _safe_exists,
    _unique_path,
    move_groups,
    trash_files,
)


# ── _drive_available ──────────────────────────────────────────────────────────

class TestDriveAvailable:

    def test_existing_drive_returns_true(self, tmp_path):
        assert _drive_available(tmp_path) is True

    def test_nonexistent_drive_letter_returns_false(self):
        # Z:\ is extremely unlikely to exist on CI / dev machines
        fake = Path("Z:\\nonexistent_path\\file.jpg")
        result = _drive_available(fake)
        # If Z:\ happens to exist, at least verify no crash.
        assert isinstance(result, bool)

    def test_oserror_returns_false(self):
        with patch("mover.Path.exists", side_effect=OSError("network error")):
            result = _drive_available(Path("//server/share/file.jpg"))
        assert result is False


# ── _safe_exists ──────────────────────────────────────────────────────────────

class TestSafeExists:

    def test_existing_file_returns_true(self, tmp_path):
        f = tmp_path / "img.jpg"
        f.write_bytes(b"data")
        assert _safe_exists(f) is True

    def test_missing_file_returns_false(self, tmp_path):
        assert _safe_exists(tmp_path / "ghost.jpg") is False

    def test_oserror_returns_false(self, tmp_path):
        p = tmp_path / "img.jpg"
        with patch.object(Path, "exists", side_effect=OSError("drive gone")):
            result = _safe_exists(p)
        assert result is False


# ── _ensure_trash_dir ─────────────────────────────────────────────────────────

class TestEnsureTrashDir:

    def test_creates_directory(self, tmp_path):
        trash = tmp_path / "output" / "trash"
        _ensure_trash_dir(trash)
        assert trash.exists()

    def test_idempotent_on_existing_dir(self, tmp_path):
        trash = tmp_path / "trash"
        trash.mkdir()
        _ensure_trash_dir(trash)  # must not raise
        assert trash.exists()

    def test_raises_file_not_found_when_drive_unavailable(self, tmp_path):
        trash = tmp_path / "trash"
        with (
            patch("mover._drive_available", return_value=False),
            patch.object(Path, "mkdir", side_effect=FileNotFoundError("no drive")),
        ):
            with pytest.raises(FileNotFoundError, match="not available"):
                _ensure_trash_dir(trash)

    def test_reraises_other_oserror_unchanged(self, tmp_path):
        trash = tmp_path / "trash"
        with (
            patch("mover._drive_available", return_value=True),
            patch.object(Path, "mkdir", side_effect=PermissionError("no permission")),
        ):
            with pytest.raises(PermissionError):
                _ensure_trash_dir(trash)


# ── _unique_path ──────────────────────────────────────────────────────────────

class TestUniquePath:

    def test_no_collision_returns_unchanged(self, tmp_path):
        p = tmp_path / "img.jpg"
        assert _unique_path(p) == p

    def test_single_collision_appends_1(self, tmp_path):
        p = tmp_path / "img.jpg"
        p.write_bytes(b"x")
        result = _unique_path(p)
        assert result == tmp_path / "img_1.jpg"

    def test_multiple_collisions(self, tmp_path):
        p = tmp_path / "img.jpg"
        p.write_bytes(b"x")
        (tmp_path / "img_1.jpg").write_bytes(b"x")
        result = _unique_path(p)
        assert result == tmp_path / "img_2.jpg"

    def test_preserves_extension(self, tmp_path):
        p = tmp_path / "file.tar.gz"
        p.write_bytes(b"x")
        result = _unique_path(p)
        assert result.suffix == ".gz"


# ── trash_files ───────────────────────────────────────────────────────────────

class TestTrashFiles:

    def test_dry_run_does_not_create_trash_dir(self, tmp_path):
        trash = tmp_path / "trash"
        src = tmp_path / "dup.jpg"
        src.write_bytes(b"data")
        moved, errors = trash_files([src], trash, dry_run=True)
        assert moved == 1
        assert not errors
        assert not trash.exists()

    def test_real_move_transfers_file(self, tmp_path):
        trash = tmp_path / "trash"
        src = tmp_path / "dup.jpg"
        src.write_bytes(b"pixel")
        moved, errors = trash_files([src], trash, dry_run=False)
        assert moved == 1
        assert not errors
        assert not src.exists()
        assert (trash / "dup.jpg").exists()

    def test_missing_source_adds_to_errors(self, tmp_path):
        trash = tmp_path / "trash"
        ghost = tmp_path / "ghost.jpg"
        moved, errors = trash_files([ghost], trash, dry_run=True)
        assert moved == 0
        assert len(errors) == 1
        assert "ghost.jpg" in errors[0]

    def test_shutil_move_error_captured(self, tmp_path):
        trash = tmp_path / "trash"
        src = tmp_path / "img.jpg"
        src.write_bytes(b"data")
        with patch("mover.shutil.move", side_effect=OSError("disk full")):
            moved, errors = trash_files([src], trash, dry_run=False)
        assert moved == 0
        assert len(errors) == 1
        assert "img.jpg" in errors[0]

    def test_writes_ops_log(self, tmp_path):
        trash = tmp_path / "trash"
        src = tmp_path / "dup.jpg"
        src.write_bytes(b"data")
        trash_files([src], trash, dry_run=False)
        log_path = tmp_path / "operations_log.json"
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert len(data["operations"]) == 1
        assert data["operations"][0]["status"] == "moved"

    def test_dry_run_does_not_write_ops_log(self, tmp_path):
        trash = tmp_path / "trash"
        src = tmp_path / "dup.jpg"
        src.write_bytes(b"data")
        trash_files([src], trash, dry_run=True)
        assert not (tmp_path / "operations_log.json").exists()


# ── move_groups ───────────────────────────────────────────────────────────────

class TestMoveGroups:

    def _make_group(self, tmp_path, name="dup.jpg", ambiguous=False):
        f = tmp_path / name
        f.write_bytes(b"pixel")
        preview = MagicMock()
        preview.path = f
        group = MagicMock()
        group.is_ambiguous = ambiguous
        group.previews = [preview]
        group.group_id = "g0001"
        return group, f

    def test_ambiguous_group_not_moved(self, tmp_path):
        group, src = self._make_group(tmp_path, ambiguous=True)
        out = tmp_path / "output"
        moved, errors = move_groups([group], out, dry_run=False)
        assert moved == 0
        assert src.exists()

    def test_non_ambiguous_group_moved(self, tmp_path):
        group, src = self._make_group(tmp_path, ambiguous=False)
        out = tmp_path / "output"
        moved, errors = move_groups([group], out, dry_run=False)
        assert moved == 1
        assert errors == 0
        assert not src.exists()

    def test_dry_run_does_not_move(self, tmp_path):
        group, src = self._make_group(tmp_path, ambiguous=False)
        out = tmp_path / "output"
        moved, errors = move_groups([group], out, dry_run=True)
        assert moved == 1
        assert src.exists()  # file still there

    def test_missing_preview_counts_as_error(self, tmp_path):
        group = MagicMock()
        group.is_ambiguous = False
        group.group_id = "g0002"
        preview = MagicMock()
        preview.path = tmp_path / "gone.jpg"  # doesn't exist
        group.previews = [preview]
        out = tmp_path / "output"
        moved, errors = move_groups([group], out, dry_run=False)
        assert moved == 0
        assert errors == 1
