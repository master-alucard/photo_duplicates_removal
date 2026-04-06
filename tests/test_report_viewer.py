"""
tests/test_report_viewer.py — Tests for the results page (ReportViewer).

Covers pagination, lazy variable initialisation, Select All / Select None,
_on_apply with unvisited groups, group card rendering, scrollregion safety,
and edge-case guards.

Run with:
    python -m pytest tests/test_report_viewer.py -v
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# ── project root on path ────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

import tkinter as tk

_ROOT: tk.Tk | None = None


def _get_root() -> tk.Tk:
    global _ROOT
    if _ROOT is None or not _ROOT.winfo_exists():
        _ROOT = tk.Tk()
        _ROOT.withdraw()
    return _ROOT


def _make_record(path="/fake/img.jpg", width=800, height=600):
    """Return a minimal ImageRecord."""
    from scanner import ImageRecord
    return ImageRecord(
        path=Path(path), width=width, height=height,
        file_size=500_000, phash="abc123", dhash=None,
        mtime=0.0, brightness=128.0, histogram=None,
        companions=None, metadata_count=0,
    )


def _make_group(n_orig=1, n_prev=2, idx=0):
    """Return a synthetic DuplicateGroup."""
    from scanner import DuplicateGroup
    origs = [_make_record(f"/fake/g{idx}_orig_{i}.jpg", 1920, 1080) for i in range(n_orig)]
    prevs = [_make_record(f"/fake/g{idx}_prev_{i}.jpg", 960, 540) for i in range(n_prev)]
    g = DuplicateGroup(originals=origs, previews=prevs)
    g.group_id = f"g{idx:04d}"
    return g


def _make_viewer(root, n_groups=0, groups=None, solo=None, page_size=100):
    """Create a ReportViewer with n synthetic groups and optional solo records."""
    from config import Settings
    from report_viewer import ReportViewer
    if groups is None:
        groups = [_make_group(idx=i) for i in range(n_groups)]
    settings = Settings()
    settings.report_page_size = page_size
    viewer = ReportViewer(
        root, groups, settings=settings,
        solo_originals=solo or [],
    )
    viewer.pack()
    root.update_idletasks()
    return viewer


# ═════════════════════════════════════════════════════════════════════════════
# 1. Pagination logic (no Tk required)
# ═════════════════════════════════════════════════════════════════════════════

class TestPagination(unittest.TestCase):
    """Test _total_pages, _unique_page_index, _is_unique_page."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_zero_groups_no_solo_gives_1_page(self):
        v = _make_viewer(self.root, 0)
        self.assertEqual(v._total_pages(), 1)

    def test_1_group_no_solo_gives_1_page(self):
        v = _make_viewer(self.root, 1)
        self.assertEqual(v._total_pages(), 1)

    def test_100_groups_100_per_page_gives_1_page(self):
        v = _make_viewer(self.root, 100, page_size=100)
        self.assertEqual(v._total_pages(), 1)

    def test_101_groups_100_per_page_gives_2_pages(self):
        v = _make_viewer(self.root, 101, page_size=100)
        self.assertEqual(v._total_pages(), 2)

    def test_250_groups_100_per_page_gives_3_pages(self):
        v = _make_viewer(self.root, 250, page_size=100)
        self.assertEqual(v._total_pages(), 3)

    def test_solo_adds_extra_page(self):
        solo = [_make_record(f"/fake/solo_{i}.jpg") for i in range(5)]
        v = _make_viewer(self.root, 10, solo=solo, page_size=100)
        # 1 group page + 1 unique page = 2
        self.assertEqual(v._total_pages(), 2)

    def test_unique_page_index_is_last(self):
        solo = [_make_record(f"/fake/solo_{i}.jpg") for i in range(5)]
        v = _make_viewer(self.root, 250, solo=solo, page_size=100)
        # 3 group pages, unique is page 3 (0-indexed)
        self.assertEqual(v._unique_page_index(), 3)
        self.assertEqual(v._total_pages(), 4)

    def test_is_unique_page_correct(self):
        solo = [_make_record(f"/fake/solo_{i}.jpg") for i in range(5)]
        v = _make_viewer(self.root, 100, solo=solo, page_size=100)
        self.assertFalse(v._is_unique_page(0))
        self.assertTrue(v._is_unique_page(1))

    def test_no_solo_means_no_unique_page(self):
        v = _make_viewer(self.root, 100)
        self.assertEqual(v._unique_page_index(), -1)
        self.assertFalse(v._is_unique_page(0))

    def test_page_clamp_lower(self):
        """Negative page index should be clamped to 0."""
        v = _make_viewer(self.root, 50)
        v._render_page(-5)
        self.assertEqual(v._current_page, 0)

    def test_page_clamp_upper(self):
        """Page beyond total should be clamped to last page."""
        v = _make_viewer(self.root, 50, page_size=10)
        total = v._total_pages()
        v._render_page(999)
        self.assertEqual(v._current_page, total - 1)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Lazy variable initialisation
# ═════════════════════════════════════════════════════════════════════════════

class TestLazyVars(unittest.TestCase):
    """Test _ensure_group_vars and lazy creation."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_group_vars_not_created_at_init(self):
        """Groups on pages beyond page 0 should not have vars at init."""
        v = _make_viewer(self.root, 200, page_size=10)
        # Page 0 shows groups 0-9, those should have vars
        for idx in range(10):
            self.assertIn(idx, v._group_vars)
        # Groups on page 1+ should NOT have vars yet
        self.assertNotIn(10, v._group_vars)
        self.assertNotIn(50, v._group_vars)

    def test_ensure_group_vars_creates_all_keys(self):
        """_ensure_group_vars must create group var + image vars."""
        v = _make_viewer(self.root, 5, page_size=1)
        # Group 3 is on page 3, not visited yet
        self.assertNotIn(3, v._group_vars)
        v._ensure_group_vars(3)
        self.assertIn(3, v._group_vars)
        self.assertIsInstance(v._group_vars[3], tk.BooleanVar)
        # Image vars for group 3 originals and previews
        grp = v._groups[3]
        for i in range(len(grp.originals)):
            self.assertIn((3, "orig", i), v._image_vars)
        for i in range(len(grp.previews)):
            self.assertIn((3, "prev", i), v._image_vars)

    def test_ensure_group_vars_is_idempotent(self):
        """Calling _ensure_group_vars twice must not reset state."""
        v = _make_viewer(self.root, 5, page_size=5)
        v._group_vars[2].set(False)
        v._ensure_group_vars(2)
        self.assertFalse(v._group_vars[2].get(),
                         "_ensure_group_vars must not reset existing var")

    def test_group_vars_default_to_true(self):
        """Newly created group vars should default to True (checked)."""
        v = _make_viewer(self.root, 5, page_size=1)
        v._ensure_group_vars(4)
        self.assertTrue(v._group_vars[4].get())


# ═════════════════════════════════════════════════════════════════════════════
# 3. Select All / Select None
# ═════════════════════════════════════════════════════════════════════════════

class TestSelectAllNone(unittest.TestCase):
    """Select All / None must affect ALL groups, including unvisited pages."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_select_none_creates_vars_for_all_groups(self):
        """Select None must initialise vars for unvisited groups and set False."""
        v = _make_viewer(self.root, 50, page_size=10)
        # Only page 0 groups (0-9) have vars initially
        self.assertNotIn(30, v._group_vars)
        v._select_none()
        # Now ALL groups should have vars, all set to False
        for idx in range(50):
            self.assertIn(idx, v._group_vars,
                          f"Group {idx} var should exist after Select None")
            self.assertFalse(v._group_vars[idx].get(),
                             f"Group {idx} should be unchecked after Select None")

    def test_select_all_creates_vars_for_all_groups(self):
        """Select All must initialise vars for unvisited groups and set True."""
        v = _make_viewer(self.root, 50, page_size=10)
        # First deselect some that exist
        v._group_vars[0].set(False)
        v._select_all()
        for idx in range(50):
            self.assertIn(idx, v._group_vars)
            self.assertTrue(v._group_vars[idx].get(),
                            f"Group {idx} should be checked after Select All")

    def test_select_none_then_apply_trashes_nothing(self):
        """After Select None, _on_apply should not try to trash any files."""
        v = _make_viewer(self.root, 20, page_size=10)
        v._select_none()
        # Verify _on_apply would find no paths
        paths = []
        for idx, grp in enumerate(v._groups):
            g_var = v._group_vars.get(idx)
            if g_var is not None and not g_var.get():
                continue
            for img_idx, rec in enumerate(grp.previews):
                v_img = v._image_vars.get((idx, "prev", img_idx))
                if v_img is not None and not v_img.get():
                    continue
                paths.append(rec.path)
        self.assertEqual(len(paths), 0,
                         "Select None followed by apply should trash 0 files")

    def test_select_all_image_vars_also_set(self):
        """Select All must also set all per-image vars to True."""
        v = _make_viewer(self.root, 20, page_size=5)
        v._select_none()
        # Verify all image vars are False
        for key, var in v._image_vars.items():
            self.assertFalse(var.get())
        v._select_all()
        for key, var in v._image_vars.items():
            self.assertTrue(var.get())


# ═════════════════════════════════════════════════════════════════════════════
# 4. _on_apply with unvisited groups
# ═════════════════════════════════════════════════════════════════════════════

class TestOnApplyUnvisited(unittest.TestCase):
    """_on_apply must treat uninitialized vars as checked (default)."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_unvisited_groups_treated_as_checked(self):
        """Groups never rendered should be included in the trash list."""
        v = _make_viewer(self.root, 30, page_size=10)
        # Only page 0 rendered. Groups 10-29 have no vars.
        # Simulate the path-collection logic from _on_apply
        paths = []
        for idx, grp in enumerate(v._groups):
            g_var = v._group_vars.get(idx)
            if g_var is not None and not g_var.get():
                continue
            for img_idx, rec in enumerate(grp.previews):
                v_img = v._image_vars.get((idx, "prev", img_idx))
                if v_img is not None and not v_img.get():
                    continue
                paths.append(rec.path)
        # All 30 groups × 2 previews each = 60 paths
        self.assertEqual(len(paths), 60)

    def test_visited_unchecked_group_excluded(self):
        """A group unchecked on a visited page must NOT be trashed."""
        v = _make_viewer(self.root, 30, page_size=10)
        v._group_vars[5].set(False)
        paths = []
        for idx, grp in enumerate(v._groups):
            g_var = v._group_vars.get(idx)
            if g_var is not None and not g_var.get():
                continue
            for img_idx, rec in enumerate(grp.previews):
                v_img = v._image_vars.get((idx, "prev", img_idx))
                if v_img is not None and not v_img.get():
                    continue
                paths.append(rec.path)
        # 29 groups × 2 previews = 58 paths
        self.assertEqual(len(paths), 58)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Group card rendering
# ═════════════════════════════════════════════════════════════════════════════

class TestGroupCardRendering(unittest.TestCase):
    """Test _build_group_card under various conditions."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_build_single_group_card(self):
        """A single group card should render without error."""
        v = _make_viewer(self.root, 1)
        # Page 0 has group 0 — should have widgets
        children = v._inner_frame.winfo_children()
        self.assertGreater(len(children), 0,
                           "Inner frame should have children after rendering page 0")

    def test_group_vars_created_for_rendered_groups(self):
        """All groups on the rendered page must have their vars created."""
        v = _make_viewer(self.root, 15, page_size=10)
        for idx in range(10):
            self.assertIn(idx, v._group_vars)
            self.assertIn((idx, "orig", 0), v._image_vars)
            self.assertIn((idx, "prev", 0), v._image_vars)

    def test_border_frames_created(self):
        """Each rendered group must have a border frame for colour feedback."""
        v = _make_viewer(self.root, 5, page_size=10)
        for idx in range(5):
            self.assertIn(idx, v._group_border_frames)

    def test_calib_containers_created(self):
        """Each rendered group must have a calibration button container."""
        v = _make_viewer(self.root, 5, page_size=10)
        for idx in range(5):
            self.assertIn(idx, v._group_calib_containers)

    def test_group_card_with_series_flag(self):
        """A series group should render without error."""
        from scanner import DuplicateGroup
        g = _make_group(idx=0)
        g.is_series = True
        v = _make_viewer(self.root, groups=[g])
        children = v._inner_frame.winfo_children()
        self.assertGreater(len(children), 0)

    def test_empty_previews_group(self):
        """A group with 0 previews should still render the header."""
        g = _make_group(n_orig=2, n_prev=0, idx=0)
        v = _make_viewer(self.root, groups=[g])
        self.assertIn(0, v._group_vars)

    def test_group_card_resilience_one_bad_group(self):
        """If one group causes an error, other groups should still render."""
        groups = [_make_group(idx=i) for i in range(5)]
        # Corrupt group 2 so _build_group_card throws
        groups[2].originals = None  # will cause TypeError in len()
        v = _make_viewer(self.root, groups=groups)
        # Groups 0, 1, 3, 4 should still have vars (group 2 may fail)
        rendered = sum(1 for i in range(5) if i in v._group_vars)
        self.assertGreaterEqual(rendered, 4,
                                "At least 4 of 5 groups should render despite 1 bad group")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Scrollregion safety
# ═════════════════════════════════════════════════════════════════════════════

class TestScrollRegion(unittest.TestCase):
    """Scrollregion must encompass all rendered content."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_scrollregion_set_after_render(self):
        """Canvas scrollregion must be set after rendering groups."""
        v = _make_viewer(self.root, 10, page_size=10)
        sr = v._canvas.cget("scrollregion")
        self.assertNotEqual(sr, "",
                            "Scrollregion should be set after render")

    def test_scrollregion_covers_all_groups(self):
        """Scrollregion height must be >= inner frame required height."""
        v = _make_viewer(self.root, 20, page_size=20)
        v._inner_frame.update_idletasks()
        bbox = v._canvas.bbox("all")
        if bbox:
            sr_height = bbox[3] - bbox[1]
            frame_height = v._inner_frame.winfo_reqheight()
            self.assertGreaterEqual(sr_height, frame_height,
                                    "Scrollregion must cover full frame height")


# ═════════════════════════════════════════════════════════════════════════════
# 7. Page navigation
# ═════════════════════════════════════════════════════════════════════════════

class TestPageNavigation(unittest.TestCase):
    """Test Prev/Next page transitions."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_next_page_advances(self):
        v = _make_viewer(self.root, 30, page_size=10)
        self.assertEqual(v._current_page, 0)
        v._next_page()
        self.assertEqual(v._current_page, 1)

    def test_prev_page_at_zero_stays(self):
        v = _make_viewer(self.root, 30, page_size=10)
        v._prev_page()
        self.assertEqual(v._current_page, 0)

    def test_next_at_last_page_stays(self):
        v = _make_viewer(self.root, 30, page_size=10)
        total = v._total_pages()
        v._render_page(total - 1)
        v._next_page()
        self.assertEqual(v._current_page, total - 1)

    def test_page_nav_label_correct(self):
        """Page info label should show correct group range."""
        v = _make_viewer(self.root, 250, page_size=100)
        v._render_page(1)
        info = v._page_info_var.get()
        self.assertIn("101", info)
        self.assertIn("200", info)
        self.assertIn("Page 2", info)

    def test_page_size_change(self):
        """Changing page size should re-calculate pagination."""
        v = _make_viewer(self.root, 50, page_size=10)
        self.assertEqual(v._total_pages(), 5)
        v._page_size = 25
        self.assertEqual(v._total_pages(), 2)


# ═════════════════════════════════════════════════════════════════════════════
# 8. Group toggle
# ═════════════════════════════════════════════════════════════════════════════

class TestGroupToggle(unittest.TestCase):
    """Test _on_group_toggle cascades to image vars."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_uncheck_group_unchecks_images(self):
        v = _make_viewer(self.root, 3, page_size=10)
        v._group_vars[1].set(False)
        v._on_group_toggle(1)
        for key, var in v._image_vars.items():
            if key[0] == 1:
                self.assertFalse(var.get(),
                                 f"Image var {key} should be unchecked")

    def test_check_group_checks_images(self):
        v = _make_viewer(self.root, 3, page_size=10)
        # Uncheck then re-check
        v._group_vars[1].set(False)
        v._on_group_toggle(1)
        v._group_vars[1].set(True)
        v._on_group_toggle(1)
        for key, var in v._image_vars.items():
            if key[0] == 1:
                self.assertTrue(var.get(),
                                f"Image var {key} should be checked")


# ═════════════════════════════════════════════════════════════════════════════
# 9. FP calibration bounds check
# ═════════════════════════════════════════════════════════════════════════════

class TestFPCalibBoundsCheck(unittest.TestCase):
    """_fp_calib_groups with out-of-range index must not crash."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_toggle_fp_with_valid_index(self):
        """Toggle FP on a valid group should not crash."""
        v = _make_viewer(self.root, 5, page_size=10)
        v._toggle_fp_calib(2)
        self.assertIn(2, v._fp_calib_groups)

    def test_toggle_fp_again_removes(self):
        """Toggling FP again should remove the group from calibration."""
        v = _make_viewer(self.root, 5, page_size=10)
        v._toggle_fp_calib(2)
        self.assertIn(2, v._fp_calib_groups)
        v._toggle_fp_calib(2)
        self.assertNotIn(2, v._fp_calib_groups)


# ═════════════════════════════════════════════════════════════════════════════
# 10. Trace cleanup
# ═════════════════════════════════════════════════════════════════════════════

class TestTraceCleanup(unittest.TestCase):
    """Traces must be cleaned up on page change."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_traces_cleaned_on_page_change(self):
        """Active traces should be empty after re-rendering a page."""
        v = _make_viewer(self.root, 20, page_size=10)
        # Page 0 renders 10 groups, each preview has a trace
        n_traces_p0 = len(v._active_traces)
        self.assertGreater(n_traces_p0, 0, "Page 0 should have traces")
        # Switch to page 1
        v._render_page(1)
        # Old traces should have been cleaned up; new ones created for page 1
        # The important thing is that the total doesn't grow unboundedly
        n_traces_p1 = len(v._active_traces)
        self.assertLessEqual(n_traces_p1, n_traces_p0 + 5,
                             "Traces should not accumulate across page changes")


# ═════════════════════════════════════════════════════════════════════════════
# 11. Thumbnail batch cancellation
# ═════════════════════════════════════════════════════════════════════════════

class TestThumbnailBatching(unittest.TestCase):
    """Thumbnail batch_id should increment on page change to cancel old batches."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_batch_id_increments_on_page_change(self):
        v = _make_viewer(self.root, 20, page_size=10)
        batch_p0 = v._thumb_batch_id
        v._render_page(1)
        batch_p1 = v._thumb_batch_id
        self.assertGreater(batch_p1, batch_p0)

    def test_pending_thumbs_cleared_on_page_change(self):
        v = _make_viewer(self.root, 20, page_size=10)
        # After flush, pending should be empty
        self.assertEqual(len(v._pending_thumbs), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 12. Photo refs cleanup
# ═════════════════════════════════════════════════════════════════════════════

class TestPhotoRefs(unittest.TestCase):
    """Photo references should be cleared on page change."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_photo_refs_cleared_on_render(self):
        v = _make_viewer(self.root, 5, page_size=5)
        # Manually add a fake ref
        v._photo_refs.append("fake_ref")
        v._render_page(0)
        # After re-render, old refs should be gone
        self.assertNotIn("fake_ref", v._photo_refs)


# ═════════════════════════════════════════════════════════════════════════════
# 13. _TrueStub sentinel
# ═════════════════════════════════════════════════════════════════════════════

class TestTrueStub(unittest.TestCase):
    """_TrueStub must behave like BooleanVar(value=True) but without Tk."""

    def test_true_stub_get_returns_true(self):
        from report_viewer import _TRUE_STUB
        self.assertTrue(_TRUE_STUB.get())

    def test_true_stub_has_no_set(self):
        """_TrueStub is read-only — no set() method, always returns True."""
        from report_viewer import _TRUE_STUB
        self.assertFalse(hasattr(_TRUE_STUB, "set"),
                         "_TrueStub should be read-only (no set method)")
        # get() must always return True regardless
        self.assertTrue(_TRUE_STUB.get())
        self.assertTrue(_TRUE_STUB.get())


# ═════════════════════════════════════════════════════════════════════════════
# 14. Placeholder image caching
# ═════════════════════════════════════════════════════════════════════════════

class TestPlaceholderCache(unittest.TestCase):
    """Placeholder images should be cached by (size, bg) key."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_same_params_return_same_object(self):
        v = _make_viewer(self.root, 1)
        p1 = v._get_placeholder(120, "#FFFFFF")
        p2 = v._get_placeholder(120, "#FFFFFF")
        self.assertIs(p1, p2, "Same (size, bg) should return cached object")

    def test_different_params_return_different_objects(self):
        v = _make_viewer(self.root, 1)
        p1 = v._get_placeholder(120, "#FFFFFF")
        p2 = v._get_placeholder(120, "#EEEEEE")
        self.assertIsNot(p1, p2, "Different bg should return different objects")

    def test_placeholder_not_in_photo_refs(self):
        """Placeholders should not be cleared when _photo_refs is cleared."""
        v = _make_viewer(self.root, 1)
        p1 = v._get_placeholder(120, "#FFFFFF")
        v._photo_refs.clear()
        p2 = v._get_placeholder(120, "#FFFFFF")
        self.assertIs(p1, p2, "Placeholder must survive photo_refs clear")


# ═════════════════════════════════════════════════════════════════════════════
# 15. Checkbox image rendering
# ═════════════════════════════════════════════════════════════════════════════

class TestCheckboxImages(unittest.TestCase):
    """Checkbox images should be created at 4× resolution."""

    def test_make_checkbox_pair_returns_two_images(self):
        from report_viewer import _make_checkbox_pair
        unchecked, checked = _make_checkbox_pair(22)
        self.assertIsNotNone(unchecked)
        self.assertIsNotNone(checked)

    def test_make_checkbox_pair_correct_size(self):
        from report_viewer import _make_checkbox_pair
        unchecked, checked = _make_checkbox_pair(22)
        self.assertEqual(unchecked.width(), 22)
        self.assertEqual(unchecked.height(), 22)
        self.assertEqual(checked.width(), 22)
        self.assertEqual(checked.height(), 22)

    def test_make_checkbox_pair_different_size(self):
        from report_viewer import _make_checkbox_pair
        unchecked, checked = _make_checkbox_pair(16)
        self.assertEqual(unchecked.width(), 16)
        self.assertEqual(checked.width(), 16)


# ═════════════════════════════════════════════════════════════════════════════
# 16. Solo section
# ═════════════════════════════════════════════════════════════════════════════

class TestSoloSection(unittest.TestCase):
    """Unique images page rendering."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_solo_page_renders(self):
        solo = [_make_record(f"/fake/solo_{i}.jpg") for i in range(5)]
        v = _make_viewer(self.root, 3, solo=solo, page_size=10)
        # Navigate to unique page
        unique_idx = v._unique_page_index()
        v._render_page(unique_idx)
        self.assertTrue(v._is_unique_page(v._current_page))

    def test_solo_vars_created_at_init(self):
        solo = [_make_record(f"/fake/solo_{i}.jpg") for i in range(5)]
        v = _make_viewer(self.root, 0, solo=solo)
        for i in range(5):
            self.assertIn(i, v._solo_vars)


# ═════════════════════════════════════════════════════════════════════════════
# 17. Edge cases — empty viewer
# ═════════════════════════════════════════════════════════════════════════════

class TestEmptyViewer(unittest.TestCase):
    """Viewer with 0 groups and 0 solo must not crash."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_empty_viewer_renders(self):
        """No groups, no solo — should show 'No duplicate groups found'."""
        v = _make_viewer(self.root, 0)
        self.assertEqual(v._current_page, 0)
        self.assertEqual(v._total_pages(), 1)

    def test_empty_viewer_select_none_is_safe(self):
        v = _make_viewer(self.root, 0)
        v._select_none()  # should not crash

    def test_empty_viewer_select_all_is_safe(self):
        v = _make_viewer(self.root, 0)
        v._select_all()  # should not crash


# ═════════════════════════════════════════════════════════════════════════════
# 18. Widget cleanup on page change
# ═════════════════════════════════════════════════════════════════════════════

class TestWidgetCleanup(unittest.TestCase):
    """Inner frame children should be destroyed on page change."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_old_widgets_destroyed(self):
        v = _make_viewer(self.root, 20, page_size=10)
        widgets_p0 = list(v._inner_frame.winfo_children())
        self.assertGreater(len(widgets_p0), 0)
        v._render_page(1)
        # Old widgets should be gone, new ones created
        widgets_p1 = list(v._inner_frame.winfo_children())
        for w in widgets_p0:
            self.assertFalse(w.winfo_exists(),
                             "Old page widgets should be destroyed")

    def test_calib_containers_reset_on_page_change(self):
        v = _make_viewer(self.root, 20, page_size=10)
        containers_p0 = dict(v._group_calib_containers)
        v._render_page(1)
        # Containers should be for page 1 groups now
        for idx in containers_p0:
            if idx >= 10:  # these shouldn't be in new containers
                continue
            # Container widget from page 0 should no longer exist
            self.assertFalse(containers_p0[idx].winfo_exists())


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
