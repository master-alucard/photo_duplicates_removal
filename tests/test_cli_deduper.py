"""
tests/test_cli_deduper.py — CLI mode (deduper.py, Mantis #130).

Covers:
  * similarity percentage -> pHash Hamming distance conversion
  * argument validation (missing --scan, out-of-range --threshold)
  * dry run: duplicates reported on stdout, no files touched
  * --auto-move-trash: duplicate copy moved to <scan>/trash/, original kept,
    operations_log.json written
  * clean-folder scan reports no duplicates

Run with:
    python -m pytest tests/test_cli_deduper.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image, ImageDraw

import deduper


def _make_photo(path: Path, seed: int) -> None:
    """Write a structured, non-uniform test image so pHash is stable and
    distinct images are far apart in Hamming distance."""
    img = Image.new("RGB", (320, 240), (seed * 37 % 256, seed * 83 % 256, 200))
    draw = ImageDraw.Draw(img)
    for i in range(12):
        x = (seed * 13 + i * 29) % 300
        y = (seed * 7 + i * 17) % 220
        color = ((seed + i * 31) % 256, (seed * 5 + i * 47) % 256, (i * 21) % 256)
        draw.rectangle([x, y, x + 40 + (i % 3) * 15, y + 25 + (i % 4) * 10],
                       fill=color)
        draw.ellipse([x // 2, y // 2, x // 2 + 30, y // 2 + 30], fill=color)
    img.save(path, "JPEG", quality=92)


class _CliTestBase(unittest.TestCase):
    """Shared temp-folder setup + isolated settings/library."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deduper_cli_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Isolate from the developer's settings.json (use pure defaults) and
        # from the persistent library cache (empty).
        self._orig_settings_path = deduper.SETTINGS_PATH
        deduper.SETTINGS_PATH = self.tmp / "no_such_settings.json"
        self.addCleanup(setattr, deduper, "SETTINGS_PATH",
                        self._orig_settings_path)

        self._orig_cache_loader = deduper._load_library_cache
        deduper._load_library_cache = lambda folder: {}
        self.addCleanup(setattr, deduper, "_load_library_cache",
                        self._orig_cache_loader)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        """Run deduper.main() in-process, capturing stdout."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = deduper.main(list(argv))
        return code, buf.getvalue()


class TestThresholdConversion(unittest.TestCase):

    def test_percent_maps_to_hamming_bits(self):
        self.assertEqual(deduper.similarity_to_hamming(100), 0)
        self.assertEqual(deduper.similarity_to_hamming(90), 6)   # ticket example
        self.assertEqual(deduper.similarity_to_hamming(50), 32)
        self.assertEqual(deduper.similarity_to_hamming(0), 64)

    def test_result_is_clamped_to_hash_width(self):
        self.assertEqual(deduper.similarity_to_hamming(-10), 64)
        self.assertEqual(deduper.similarity_to_hamming(150), 0)


class TestArgumentValidation(_CliTestBase):

    def test_missing_scan_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            deduper.build_parser().parse_args([])
        self.assertEqual(ctx.exception.code, 2)

    def test_threshold_out_of_range_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                deduper.main(["--scan", str(self.tmp), "--threshold", "101"])
        self.assertEqual(ctx.exception.code, 2)

    def test_nonexistent_scan_folder_fails_cleanly(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = self.run_cli("--scan", str(self.tmp / "missing"))
        self.assertEqual(code, 1)


class TestDryRunAndAutoMove(_CliTestBase):

    def setUp(self):
        super().setUp()
        # One duplicate pair (byte-identical copies) + one distinct image.
        self.original = self.tmp / "photo_a.jpg"
        self.duplicate = self.tmp / "photo_a_copy.jpg"
        self.distinct = self.tmp / "photo_b.jpg"
        _make_photo(self.original, seed=3)
        shutil.copy2(self.original, self.duplicate)
        _make_photo(self.distinct, seed=44)

    def _files_in(self, folder: Path) -> set[str]:
        return {p.name for p in folder.rglob("*") if p.is_file()}

    def test_dry_run_reports_but_moves_nothing(self):
        before = self._files_in(self.tmp)
        code, out = self.run_cli("--scan", str(self.tmp), "--threshold", "90")

        self.assertEqual(code, 0)
        self.assertIn("dry run", out.lower())
        self.assertIn("Duplicate groups  : 1", out)
        self.assertIn("Duplicate files   : 1", out)
        # Both pair members appear in the report, distinct image in no group.
        self.assertIn("photo_a", out)
        self.assertNotIn("photo_b.jpg  (", out)
        # Nothing on disk changed; no trash folder, no ops log.
        self.assertEqual(self._files_in(self.tmp), before)
        self.assertFalse((self.tmp / "trash").exists())
        self.assertFalse((self.tmp / "operations_log.json").exists())

    def test_auto_move_trash_moves_duplicate_and_keeps_original(self):
        code, out = self.run_cli(
            "--scan", str(self.tmp), "--threshold", "90", "--auto-move-trash",
        )

        self.assertEqual(code, 0)
        self.assertIn("Moved 1", out)

        trash = self.tmp / "trash"
        self.assertTrue(trash.exists())
        trashed = self._files_in(trash)
        self.assertEqual(len(trashed), 1)
        # Exactly one copy of the pair moved; distinct image untouched;
        # exactly one pair member remains outside trash.
        self.assertTrue(self.distinct.exists())
        remaining_pair = [p for p in (self.original, self.duplicate)
                          if p.exists()]
        self.assertEqual(len(remaining_pair), 1)

        # Move logged in the standard revertable operations log.
        ops_file = self.tmp / "operations_log.json"
        self.assertTrue(ops_file.exists())
        ops = json.loads(ops_file.read_text(encoding="utf-8"))["operations"]
        moved_ops = [o for o in ops if o.get("status") == "moved"]
        self.assertEqual(len(moved_ops), 1)

    def test_out_flag_redirects_trash_and_ops_log(self):
        out_dir = Path(tempfile.mkdtemp(prefix="deduper_cli_out_"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)

        code, out = self.run_cli(
            "--scan", str(self.tmp), "--threshold", "90",
            "--auto-move-trash", "--out", str(out_dir),
        )

        self.assertEqual(code, 0)
        self.assertIn("Moved 1", out)

        # Trash and ops log land under --out, not under the scan folder.
        trash = out_dir / "trash"
        self.assertTrue(trash.exists())
        self.assertEqual(len(self._files_in(trash)), 1)
        self.assertTrue((out_dir / "operations_log.json").exists())
        self.assertFalse((self.tmp / "trash").exists())
        self.assertFalse((self.tmp / "operations_log.json").exists())

        # Original kept, distinct image untouched.
        self.assertTrue(self.distinct.exists())
        remaining_pair = [p for p in (self.original, self.duplicate)
                          if p.exists()]
        self.assertEqual(len(remaining_pair), 1)

    def test_clean_folder_reports_no_duplicates(self):
        clean = self.tmp / "clean"
        clean.mkdir()
        _make_photo(clean / "one.jpg", seed=5)
        _make_photo(clean / "two.jpg", seed=77)

        code, out = self.run_cli("--scan", str(clean))
        self.assertEqual(code, 0)
        self.assertIn("No duplicates found", out)
        self.assertFalse((clean / "trash").exists())


if __name__ == "__main__":
    unittest.main()
