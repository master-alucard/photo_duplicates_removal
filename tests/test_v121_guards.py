"""
tests/test_v121_guards.py — Coverage for the v1.2.1 GUI-side guards (#2153).

Covers:
  * ReportViewer out_folder ctor param: frozen at construction, immune to
    later settings-field edits, settings fallback when the param is omitted,
    in-viewer Change Folder still redirects (#2149 semantics)
  * ReportViewer._on_trash_selected missing-folder guard (no move attempted)
  * App._accept_and_move frozen scan-time folder + missing-folder guard
  * App._install_rawpy frozen-build guard (never relaunches the exe)
  * deduper._make_progress_cb throttle (phase change / finished / 1s gate)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tkinter as tk

from test_report_viewer import _get_root, _make_group


def _make_viewer(root, out_folder=None, settings_out=""):
    from config import Settings
    from report_viewer import ReportViewer
    settings = Settings()
    settings.out_folder = settings_out
    viewer = ReportViewer(
        root, [_make_group(idx=0)], settings=settings,
        out_folder=out_folder,
    )
    return viewer, settings


class TestViewerOutFolder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def test_ctor_param_wins_over_settings(self):
        viewer, _ = _make_viewer(self.root, out_folder=r"C:\scan_time_out",
                                 settings_out=r"C:\live_field")
        self.assertEqual(viewer._out_folder, r"C:\scan_time_out")
        viewer.destroy()

    def test_settings_edits_after_construction_do_not_redirect(self):
        viewer, settings = _make_viewer(self.root, out_folder=r"C:\scan_time_out",
                                        settings_out=r"C:\scan_time_out")
        settings.out_folder = r"C:\edited_later"
        self.assertEqual(viewer._out_folder, r"C:\scan_time_out")
        viewer.destroy()

    def test_settings_fallback_when_param_omitted(self):
        viewer, _ = _make_viewer(self.root, out_folder=None,
                                 settings_out=r"C:\from_settings")
        self.assertEqual(viewer._out_folder, r"C:\from_settings")
        viewer.destroy()

    def test_change_folder_updates_working_value(self):
        viewer, _ = _make_viewer(self.root, out_folder=r"C:\scan_time_out")
        with patch("tkinter.filedialog.askdirectory",
                   return_value=r"C:\user_choice"):
            viewer._on_change_folder()
        self.assertEqual(viewer._out_folder, r"C:\user_choice")
        viewer.destroy()

    def test_manual_trash_missing_folder_guard(self):
        viewer, _ = _make_viewer(self.root,
                                 out_folder=r"C:\definitely\missing\folder_xyz")
        viewer._manual_trash_selected = {Path("/fake/img.jpg")}
        with patch("report_viewer.error_handler.show_warning") as warn, \
             patch("mover.trash_files") as trash:
            viewer._on_trash_selected()
        warn.assert_called_once()
        self.assertIn("Output Folder Missing", warn.call_args.args)
        trash.assert_not_called()
        viewer.destroy()


class TestAcceptAndMoveGuards(unittest.TestCase):
    """Exercise App._accept_and_move guards on a stub object — the guards run
    before any widget access, so a full App is not needed."""

    def _stub(self, last_scan_out=None, settings_out=""):
        import main as main_mod
        stub = MagicMock()
        stub.root = None
        stub.scan_groups = [_make_group(idx=0)]
        stub.settings.out_folder = settings_out
        if last_scan_out is not None:
            stub._last_scan_out_folder = last_scan_out
        else:
            # getattr fallback path: ensure attribute is absent
            del stub._last_scan_out_folder
        return main_mod, stub

    def test_uses_frozen_scan_time_folder_not_live_field(self):
        main_mod, stub = self._stub(
            last_scan_out=r"C:\definitely\missing\frozen_xyz",
            settings_out=r"C:\also\missing\live_abc",
        )
        with patch.object(main_mod.messagebox, "askyesno", return_value=True), \
             patch.object(main_mod.error_handler, "show_warning") as warn:
            main_mod.App._accept_and_move(stub)
        # Guard fired on the FROZEN path, proving the frozen value was used.
        warn.assert_called_once()
        self.assertIn("frozen_xyz", str(warn.call_args))

    def test_missing_folder_blocks_before_move(self):
        main_mod, stub = self._stub(
            last_scan_out=r"C:\definitely\missing\folder_xyz")
        with patch.object(main_mod.messagebox, "askyesno", return_value=True), \
             patch.object(main_mod.error_handler, "show_warning") as warn, \
             patch.object(main_mod, "move_groups") as mover:
            main_mod.App._accept_and_move(stub)
        warn.assert_called_once()
        mover.assert_not_called()

    def test_empty_out_folder_blocks_with_clear_warning(self):
        main_mod, stub = self._stub(last_scan_out=None, settings_out="")
        with patch.object(main_mod.messagebox, "askyesno", return_value=True), \
             patch.object(main_mod.error_handler, "show_warning") as warn, \
             patch.object(main_mod, "move_groups") as mover:
            main_mod.App._accept_and_move(stub)
        warn.assert_called_once()
        self.assertIn("No Output Folder", warn.call_args.args)
        mover.assert_not_called()


class TestInstallRawpyFrozenGuard(unittest.TestCase):

    def test_frozen_build_never_spawns_subprocess(self):
        import main as main_mod
        stub = MagicMock()
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(main_mod.error_handler, "show_info") as info, \
             patch("subprocess.run") as run:
            main_mod.App._install_rawpy(stub)
        info.assert_called_once()
        run.assert_not_called()
        # No install window was created either.
        stub.root.assert_not_called()


class TestCliProgressThrottle(unittest.TestCase):

    def test_phase_change_finish_and_time_gate(self):
        import deduper
        lines = []
        cb = deduper._make_progress_cb()

        fake_now = [1000.0]
        with patch("deduper.time") as t, \
             patch("deduper.sys") as fake_sys:
            t.monotonic = lambda: fake_now[0]
            fake_sys.stderr = MagicMock()
            fake_sys.stderr.write = lambda s: lines.append(s)

            cb("start", 1, 10, "Hashing")        # phase change -> emit
            cb("mid", 2, 10, "Hashing")          # same phase, <1s -> suppressed
            fake_now[0] += 1.1
            cb("later", 3, 10, "Hashing")        # >1s elapsed -> emit
            cb("done", 10, 10, "Hashing")        # finished -> emit
            cb("new", 0, 0, "Grouping")          # phase change -> emit

        text = "".join(lines)
        self.assertEqual(text.count("[Hashing]"), 3)
        self.assertEqual(text.count("[Grouping]"), 1)
        self.assertNotIn("2/10", text.replace(",", ""))



class TestEllipsizeMiddle(unittest.TestCase):

    def test_short_text_unchanged(self):
        import main as main_mod
        self.assertEqual(main_mod._ellipsize_middle("abc", 60), "abc")

    def test_long_text_keeps_head_and_tail(self):
        import main as main_mod
        name = "Extracting frames from video 5/40: " + "x" * 80 + "_001993.mp4"
        out = main_mod._ellipsize_middle(name, 60)
        self.assertEqual(len(out), 60)
        self.assertIn("…", out)
        self.assertTrue(out.startswith("Extracting frames"))
        self.assertTrue(out.endswith("_001993.mp4"))

    def test_exact_limit_unchanged(self):
        import main as main_mod
        text = "a" * 60
        self.assertEqual(main_mod._ellipsize_middle(text, 60), text)


class TestThemedDialogs(unittest.TestCase):
    """Smoke tests: the themed note dialog renders with palette colors and
    show_warning/show_info route through it (no native messagebox)."""

    @classmethod
    def setUpClass(cls):
        cls.root = _get_root()

    def _open_and_close(self, fn, title):
        import error_handler
        # Auto-close: destroy the dialog as soon as it appears.
        def _close_soon():
            for w in self.root.winfo_children():
                if isinstance(w, tk.Toplevel) and w.title() == title:
                    w.destroy()
                    return
            self.root.after(20, _close_soon)
        self.root.after(20, _close_soon)
        with patch.object(error_handler.messagebox, "showwarning") as native_w, \
             patch.object(error_handler.messagebox, "showinfo") as native_i:
            fn()
        native_w.assert_not_called()
        native_i.assert_not_called()

    def test_show_warning_uses_themed_dialog(self):
        import error_handler
        self._open_and_close(
            lambda: error_handler.show_warning(self.root, "WarnTitle", "msg"),
            "WarnTitle")

    def test_show_info_uses_themed_dialog(self):
        import error_handler
        self._open_and_close(
            lambda: error_handler.show_info(self.root, "InfoTitle", "msg"),
            "InfoTitle")

    def test_dark_palette_applied(self):
        import error_handler
        import theme
        dark = theme.get_palette(True)

        class _S:
            dark_mode = True
        old = error_handler._settings
        error_handler.set_settings(_S())
        try:
            captured = {}
            def _close_soon():
                for w in self.root.winfo_children():
                    if isinstance(w, tk.Toplevel) and w.title() == "DarkT":
                        captured["bg"] = w.cget("bg")
                        w.destroy()
                        return
                self.root.after(20, _close_soon)
            self.root.after(20, _close_soon)
            error_handler.show_info(self.root, "DarkT", "msg")
            self.assertEqual(captured.get("bg"), dark["CARD_BG"])
        finally:
            error_handler.set_settings(old)


if __name__ == "__main__":
    unittest.main()
