"""
tests/test_app.py — Automated tests for Image Deduper.

Run with:
    python -m pytest tests/ -v
or:
    python tests/test_app.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# ── project root on path ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── single shared Tk root (one per process) ───────────────────────────────────
import tkinter as tk
_ROOT: tk.Tk | None = None


def _get_root() -> tk.Tk:
    global _ROOT
    if _ROOT is None or not _ROOT.winfo_exists():
        _ROOT = tk.Tk()
        _ROOT.withdraw()
    return _ROOT


def _make_record(path="/fake/img.jpg", width=800, height=600):
    """Return a minimal ImageRecord with correct field names."""
    from scanner import ImageRecord
    return ImageRecord(
        path=Path(path), width=width, height=height,
        file_size=500_000, phash="abc123", dhash=None,
        mtime=0.0, brightness=128.0, histogram=None,
        companions=None, metadata_count=0,
    )


def _make_group(n_orig=1, n_prev=2):
    """Return a synthetic DuplicateGroup."""
    from scanner import DuplicateGroup
    origs = [_make_record(f"/fake/orig_{i}.jpg", 1920, 1080) for i in range(n_orig)]
    prevs = [_make_record(f"/fake/prev_{i}.jpg", 960, 540) for i in range(n_prev)]
    g = DuplicateGroup(originals=origs, previews=prevs)
    g.group_id = 1
    return g


# ═════════════════════════════════════════════════════════════════════════════
# 1. Config / defaults
# ═════════════════════════════════════════════════════════════════════════════

class TestConfig(unittest.TestCase):

    def test_threshold_default_is_2(self):
        """Similarity threshold default must be 2."""
        from config import Settings
        self.assertEqual(Settings().threshold, 2)

    def test_defaults_object_matches_class(self):
        """DEFAULTS sentinel equals a fresh Settings()."""
        from config import DEFAULTS, Settings
        self.assertEqual(DEFAULTS.threshold, Settings().threshold)

    def test_load_settings_returns_default_on_missing_file(self):
        """load_settings falls back to defaults when the file doesn't exist."""
        from config import load_settings, Settings
        s = load_settings(Path("/nonexistent/path/settings.json"))
        self.assertIsInstance(s, Settings)
        self.assertEqual(s.threshold, 2)

    def test_dry_run_default_is_true(self):
        """dry_run defaults to True in Settings (scan-safe default)."""
        from config import Settings
        self.assertTrue(Settings().dry_run)

    def test_developer_mode_default_is_false(self):
        """developer_mode defaults to False (users see friendly messages)."""
        from config import Settings
        self.assertFalse(Settings().developer_mode)

    # ── Settings migration (v0 → v1) ─────────────────────────────────────────

    def test_migration_v0_raw_use_embedded_thumb_false_is_reset(self):
        """
        v0 file with raw_use_embedded_thumb=False must be upgraded to True.

        This was the primary root cause of under-detection on RAW and CF
        calibration folders: the old default (False) was written to settings.json
        before the default was changed to True in commit 925431d.  Any v0 file
        that has False stored must have it reset so the app uses the correct
        hashing mode without requiring the user to manually toggle the hidden
        (no-UI-control) setting.
        """
        import json
        import tempfile
        from config import load_settings, Settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "threshold": 4,
                "raw_use_embedded_thumb": False,
                # no settings_version → treated as v0
            }, f)
            tmp = Path(f.name)

        try:
            s = load_settings(tmp)
            self.assertTrue(
                s.raw_use_embedded_thumb,
                "load_settings must migrate v0 raw_use_embedded_thumb=False to True"
            )
            self.assertEqual(s.settings_version, 1, "migrated file must be stamped v1")
        finally:
            tmp.unlink(missing_ok=True)

    def test_migration_v0_cross_format_threshold_factor_too_low_is_reset(self):
        """
        v0 file with cross_format_threshold_factor < 4.0 must be reset to 6.0.

        The value 2.0 was left in settings.json by a calibration iteration run
        (ROUND4_CF_THRESHOLD_FACTORS sweep).  With factor=2.0 the effective CF
        pHash threshold is only 4 bits — far too tight to cover the 0–12 bit
        range of true RAW+JPEG pairs.  The correct production default is 6.0
        (effective threshold 12 bits, 8-bit safety gap to inter-group minimum).
        """
        import json
        import tempfile
        from config import load_settings, Settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "cross_format_threshold_factor": 2.0,
                # no settings_version → v0
            }, f)
            tmp = Path(f.name)

        try:
            s = load_settings(tmp)
            self.assertAlmostEqual(
                s.cross_format_threshold_factor, 6.0, places=3,
                msg="load_settings must migrate CF factor < 4.0 to default 6.0"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_migration_v0_raw_embedded_true_is_preserved(self):
        """
        v0 file with raw_use_embedded_thumb=True must be left unchanged.
        (Only False → True migration is needed; True was already correct.)
        """
        import json
        import tempfile
        from config import load_settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"raw_use_embedded_thumb": True}, f)
            tmp = Path(f.name)

        try:
            s = load_settings(tmp)
            self.assertTrue(s.raw_use_embedded_thumb)
        finally:
            tmp.unlink(missing_ok=True)

    def test_migration_v0_cf_factor_at_or_above_4_is_preserved(self):
        """
        v0 file with cross_format_threshold_factor >= 4.0 must be preserved.
        Only values below 4.0 (too tight) are reset.
        """
        import json
        import tempfile
        from config import load_settings

        for val in (4.0, 5.0, 6.0, 8.0):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump({"cross_format_threshold_factor": val}, f)
                tmp = Path(f.name)

            try:
                s = load_settings(tmp)
                self.assertAlmostEqual(
                    s.cross_format_threshold_factor, val, places=3,
                    msg=f"CF factor {val} >= 4.0 must not be changed by migration"
                )
            finally:
                tmp.unlink(missing_ok=True)

    def test_migration_v1_file_no_changes(self):
        """
        A v1 file is not modified by migration even if it has 'bad' values.
        Version 1 means the user explicitly set these fields on a v1 build,
        so we must respect whatever values are stored.
        """
        import json
        import tempfile
        from config import load_settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "settings_version": 1,
                "raw_use_embedded_thumb": False,   # user explicitly set False on v1
                "cross_format_threshold_factor": 2.0,  # user explicitly set
            }, f)
            tmp = Path(f.name)

        try:
            s = load_settings(tmp)
            # v1 values must be preserved as-is; migration only applies to v0
            self.assertFalse(
                s.raw_use_embedded_thumb,
                "v1 raw_use_embedded_thumb=False must not be migrated"
            )
            self.assertAlmostEqual(
                s.cross_format_threshold_factor, 2.0, places=3,
                msg="v1 CF factor=2.0 must not be migrated"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_raw_use_embedded_thumb_new_default_is_true(self):
        """
        Settings() default for raw_use_embedded_thumb must be True.
        Regression guard: this was changed in commit 925431d from False to True.
        """
        from config import Settings
        self.assertTrue(
            Settings().raw_use_embedded_thumb,
            "raw_use_embedded_thumb default must be True (changed in commit 925431d)"
        )

    def test_cross_format_threshold_factor_default_is_6(self):
        """
        Settings() default for cross_format_threshold_factor must be 6.0.
        This is calibrated to cover max intra-group pHash=12 with an 8-bit
        safety gap (inter-group minimum pHash=20).
        """
        from config import Settings
        self.assertAlmostEqual(
            Settings().cross_format_threshold_factor, 6.0, places=3,
            msg="cross_format_threshold_factor default must be 6.0"
        )

    def test_error_handler_normal_mode_hides_detail(self):
        """error_handler returns only user_msg when developer mode is OFF."""
        import error_handler
        error_handler.set_settings(None)          # no settings → dev mode OFF
        msg = error_handler._build_msg("Something went wrong.", detail="secret trace")
        self.assertEqual(msg, "Something went wrong.")
        self.assertNotIn("secret trace", msg)

    def test_error_handler_dev_mode_shows_detail(self):
        """error_handler appends detail when developer mode is ON."""
        import error_handler
        from config import Settings
        s = Settings()
        s.developer_mode = True
        error_handler.set_settings(s)
        msg = error_handler._build_msg("Something went wrong.", detail="secret trace")
        self.assertIn("secret trace", msg)
        error_handler.set_settings(None)          # reset


# ═════════════════════════════════════════════════════════════════════════════
# 2. ReportViewer — class structure (no Tk required)
# ═════════════════════════════════════════════════════════════════════════════

class TestReportViewerStructure(unittest.TestCase):

    def test_report_viewer_is_frame_not_toplevel(self):
        """ReportViewer must inherit from tk.Frame (embedded, not a popup)."""
        from report_viewer import ReportViewer
        self.assertTrue(issubclass(ReportViewer, tk.Frame))
        self.assertFalse(issubclass(ReportViewer, tk.Toplevel))

    def test_report_viewer_accepts_on_close_cb(self):
        """ReportViewer.__init__ must accept on_close_cb parameter."""
        import inspect
        from report_viewer import ReportViewer
        sig = inspect.signature(ReportViewer.__init__)
        self.assertIn("on_close_cb", sig.parameters)

    def test_report_viewer_apply_always_real_move(self):
        """_on_apply must hardcode dry=False regardless of settings.dry_run."""
        import inspect
        from report_viewer import ReportViewer
        src = inspect.getsource(ReportViewer._on_apply)
        self.assertIn("dry = False", src,
                      "_on_apply must set dry=False unconditionally")
        self.assertNotIn("settings.dry_run", src,
                         "_on_apply must not read settings.dry_run")

    def test_build_image_tile_has_show_checkbox_param(self):
        """_build_image_tile must accept show_checkbox parameter."""
        import inspect
        from report_viewer import ReportViewer
        sig = inspect.signature(ReportViewer._build_image_tile)
        self.assertIn("show_checkbox", sig.parameters)
        # default must be True so duplicates still get checkboxes
        self.assertTrue(sig.parameters["show_checkbox"].default)


# ═════════════════════════════════════════════════════════════════════════════
# 3. ReportViewer — checkbox visibility (Tk required)
# ═════════════════════════════════════════════════════════════════════════════

class TestReportViewerCheckboxes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()
        from config import Settings
        from report_viewer import ReportViewer
        cls.viewer = ReportViewer(cls.root, [], settings=Settings())
        cls.viewer.pack()
        cls.root.update_idletasks()

    def _collect_checkbuttons(self, widget):
        result = []
        if isinstance(widget, tk.Checkbutton):
            result.append(widget)
        for child in widget.winfo_children():
            result.extend(self._collect_checkbuttons(child))
        return result

    def _make_tile(self, show_checkbox=True):
        host = tk.Frame(self.root)
        rec = _make_record()
        var = tk.BooleanVar(value=True)
        tile = self.viewer._build_image_tile(
            host, rec, var, 0, 0, bg="#ffffff", show_checkbox=show_checkbox)
        self.root.update_idletasks()
        return tile

    def test_show_checkbox_false_produces_no_checkbutton(self):
        """Originals tile (show_checkbox=False) must contain no Checkbutton."""
        tile = self._make_tile(show_checkbox=False)
        cbs = self._collect_checkbuttons(tile)
        self.assertEqual(len(cbs), 0,
                         "Originals tile must contain zero Checkbuttons")

    def test_show_checkbox_true_produces_checkbutton(self):
        """Duplicate tile (show_checkbox=True) must contain a Checkbutton."""
        tile = self._make_tile(show_checkbox=True)
        cbs = self._collect_checkbuttons(tile)
        self.assertGreater(len(cbs), 0,
                           "Duplicate tile must contain at least one Checkbutton")


# ═════════════════════════════════════════════════════════════════════════════
# 4. ReportViewer — action bar buttons (Tk required)
# ═════════════════════════════════════════════════════════════════════════════

class TestReportViewerActionBar(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()
        from config import Settings
        from report_viewer import ReportViewer
        cls.viewer = ReportViewer(cls.root, [], settings=Settings())
        cls.viewer.pack()
        cls.root.update_idletasks()

    def _all_button_texts(self, widget=None):
        widget = widget or self.viewer
        result = []
        if isinstance(widget, tk.Button):
            try:
                result.append(widget.cget("text"))
            except Exception:
                pass
        for child in widget.winfo_children():
            result.extend(self._all_button_texts(child))
        return result

    def test_move_duplicates_button_exists(self):
        """Action bar must have a 'Move Duplicates' button."""
        texts = self._all_button_texts()
        self.assertTrue(
            any("Move Duplicates" in t for t in texts),
            f"'Move Duplicates' button not found. Buttons: {texts}"
        )

    def test_no_red_move_to_trash_button(self):
        """The standalone red 'Move to Trash' button must be gone."""
        def _red_trash(w):
            found = []
            if isinstance(w, tk.Button):
                try:
                    if "Move to Trash" in w.cget("text") and \
                       w.cget("bg").lower() in ("#c62828",):
                        found.append(w)
                except Exception:
                    pass
            for child in w.winfo_children():
                found.extend(_red_trash(child))
            return found
        self.assertEqual(len(_red_trash(self.viewer)), 0,
                         "Red 'Move to Trash' button should be removed")

    def test_apply_btn_is_green(self):
        """'Move Duplicates' button must be green (#2e7d32)."""
        bg = self.viewer._apply_btn.cget("bg").lower()
        self.assertEqual(bg, "#2e7d32",
                         f"Apply button should be green (#2e7d32), got {bg}")

    def test_select_buttons_have_white_bg(self):
        """Select All / Select None must be white for contrast on blue header."""
        white_select = []
        def _find(w):
            if isinstance(w, tk.Button):
                try:
                    if "Select" in w.cget("text") and w.cget("bg").lower() == "#ffffff":
                        white_select.append(w)
                except Exception:
                    pass
            for child in w.winfo_children():
                _find(child)
        _find(self.viewer)
        self.assertEqual(len(white_select), 2,
                         f"Expected 2 white Select buttons, found {len(white_select)}")


# ═════════════════════════════════════════════════════════════════════════════
# 5. Main App — embedding (Tk required)
# ═════════════════════════════════════════════════════════════════════════════

class TestMainEmbedding(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()
        orig = tk.Tk.mainloop
        tk.Tk.mainloop = lambda self: None
        try:
            import main
            cls.app = main.App(cls.root)
            cls.root.update_idletasks()
        finally:
            tk.Tk.mainloop = orig

    def test_has_embed_report_viewer(self):
        """App must have _embed_report_viewer method."""
        import main
        self.assertTrue(hasattr(main.App, "_embed_report_viewer"))

    def test_has_results_viewer_host(self):
        """_build_results_tab_content must create _results_viewer_host."""
        self.assertTrue(hasattr(self.app, "_results_viewer_host"))

    def test_has_results_summary_frame(self):
        """_build_results_tab_content must create _results_summary_frame."""
        self.assertTrue(hasattr(self.app, "_results_summary_frame"))

    def test_window_geometry_set_to_1160x800(self):
        """App.__init__ must call root.geometry('1160x800')."""
        import inspect, main
        src = inspect.getsource(main.App.__init__)
        self.assertIn("1160x800", src,
                      "App.__init__ must set geometry to 1160x800")

    def test_no_viewer_grab_set_in_open_inapp_report(self):
        """_open_inapp_report must not call grab_set() on a Toplevel viewer."""
        import inspect, main
        src = inspect.getsource(main.App._open_inapp_report)
        self.assertNotIn("grab_set", src,
                         "_open_inapp_report must not call grab_set()")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Slider — threshold recommended range
# ═════════════════════════════════════════════════════════════════════════════

class TestSliderRecommended(unittest.TestCase):

    def test_threshold_default_is_2(self):
        """Config default for threshold must be 2."""
        from config import Settings
        self.assertEqual(Settings().threshold, 2)

    def test_threshold_slider_rec_lo_is_2_in_settings_tab(self):
        """Settings tab threshold slider must have rec_lo=2."""
        import inspect, main
        src = inspect.getsource(main.App._build_settings_tab)
        self.assertIn('2, 12, 2, "threshold"', src,
                      "Settings tab threshold: rec_lo=2, rec_hi=12, default=2")

    def test_compare_tab_uses_calibration_threshold(self):
        """Compare Scan tab does not own a threshold slider — it reads the
        calibrated threshold from settings, so changes in the Settings tab
        propagate automatically.  Verify the tab references the calibrated
        threshold, not a duplicated slider."""
        import inspect, main
        src = inspect.getsource(main.App._build_custom_scan_tab)
        self.assertIn("calibrated_threshold", src,
                      "Compare tab should use the shared calibrated_threshold")


# ═════════════════════════════════════════════════════════════════════════════
# 7. Mover — trash_files (unit, no Tk)
# ═════════════════════════════════════════════════════════════════════════════

class TestMoverTrashFiles(unittest.TestCase):

    def test_trash_files_dry_run_moves_nothing(self):
        """trash_files with dry_run=True must not move any files."""
        import tempfile
        from mover import trash_files

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "img.jpg"
            src.write_bytes(b"FAKE")
            trash_dir = Path(tmp) / "trash"

            moved, errors = trash_files([src], trash_dir, dry_run=True)

            self.assertEqual(moved, 1, "dry_run should report 1 'moved'")
            self.assertTrue(src.exists(),
                            "Source file must NOT be removed in dry_run")
            self.assertFalse(trash_dir.exists(),
                             "trash/ folder must NOT be created in dry_run")

    def test_trash_files_real_move(self):
        """trash_files with dry_run=False must actually move the file."""
        import tempfile
        from mover import trash_files

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "img.jpg"
            src.write_bytes(b"FAKE")
            trash_dir = Path(tmp) / "trash"

            moved, errors = trash_files([src], trash_dir, dry_run=False)

            self.assertEqual(moved, 1)
            self.assertEqual(errors, [])
            self.assertFalse(src.exists(),
                             "Source must be gone after real move")
            self.assertTrue((trash_dir / "img.jpg").exists(),
                            "File must appear in trash/")

    def test_trash_files_missing_source(self):
        """trash_files must report an error for a non-existent file."""
        import tempfile
        from mover import trash_files

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "ghost.jpg"
            trash_dir = Path(tmp) / "trash"

            moved, errors = trash_files([missing], trash_dir, dry_run=False)

            self.assertEqual(moved, 0)
            self.assertEqual(len(errors), 1)


# ═════════════════════════════════════════════════════════════════════════════
# 8. Scanner — smoke tests (no Tk)
# ═════════════════════════════════════════════════════════════════════════════

class TestScannerSmoke(unittest.TestCase):

    def _collect(self, folder, settings=None):
        """Returns (records, broken_paths) using the failed_paths out-param."""
        from config import Settings
        from scanner import collect_images
        s = settings or Settings()
        broken = []
        records = collect_images(
            Path(folder), skip_paths=set(), settings=s,
            progress_cb=None, stop_flag=[False], failed_paths=broken,
        )
        return records, broken

    def test_collect_images_empty_folder(self):
        """collect_images on an empty folder returns empty list."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            records, broken = self._collect(tmp)
        self.assertEqual(records, [])
        self.assertEqual(broken, [])

    def test_collect_images_finds_jpg(self):
        """collect_images finds a .jpg file in the folder."""
        import tempfile
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "test.jpg"
            Image.new("RGB", (100, 100), color=(128, 128, 128)).save(str(img_path))
            records, _ = self._collect(tmp)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].path, img_path)

    def test_collect_images_skips_non_image(self):
        """collect_images must ignore non-image files."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readme.txt").write_text("hello")
            records, _ = self._collect(tmp)
        self.assertEqual(records, [])

    def test_find_groups_identical_images_grouped(self):
        """Two identical images must form at least one duplicate group."""
        import tempfile
        from PIL import Image
        from config import Settings
        from scanner import find_groups

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.jpg", "b.jpg"):
                Image.new("RGB", (200, 200), color=(64, 64, 64)).save(
                    str(Path(tmp) / name))

            settings = Settings()
            settings.threshold = 10
            records, _ = self._collect(tmp, settings)
            groups, _ = find_groups(records, settings=settings,
                                    progress_cb=None, stop_flag=[False])

        self.assertGreater(len(groups), 0,
                           "Identical images should form at least one group")


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
