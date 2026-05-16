"""
tests/test_reporter.py — Tests for reporter.generate_report.

Uses synthetic DuplicateGroup / ImageRecord objects so no real scan is needed.
Checks HTML output structure, statistics correctness, thumbnail threshold
switching, ambiguous-group markup, series badge, and the no-thumbnails notice.
"""
from __future__ import annotations

import re
from pathlib import Path

import imagehash
import pytest
from PIL import Image

from config import Settings
from reporter import generate_report, _THUMB_MAX_IMAGES, _thumb_b64
from scanner import DuplicateGroup, ImageRecord


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_jpeg(path: Path, color=(200, 100, 50)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path, format="JPEG")
    return path


def _make_record(path: Path) -> ImageRecord:
    img = Image.new("RGB", (16, 16), (128, 128, 128))
    ph = imagehash.average_hash(img)
    dh = imagehash.dhash(img)
    return ImageRecord(
        path=path,
        width=16, height=16,
        file_size=path.stat().st_size,
        phash=ph,
        dhash=dh,
        mtime=path.stat().st_mtime,
        brightness=128.0,
        histogram=[0.1] * 96,
    )


def _simple_group(
    tmp_path: Path,
    n_originals: int = 1,
    n_previews: int = 1,
    is_series: bool = False,
    is_ambiguous: bool = False,
    group_id: str = "g0001",
) -> DuplicateGroup:
    originals = []
    for i in range(n_originals):
        p = _make_jpeg(tmp_path / f"orig_{group_id}_{i}.jpg", color=(200 - i * 30, 100, 50))
        originals.append(_make_record(p))
    previews = []
    for i in range(n_previews):
        p = _make_jpeg(tmp_path / f"prev_{group_id}_{i}.jpg", color=(50, 100 + i * 30, 200))
        previews.append(_make_record(p))
    g = DuplicateGroup(originals=originals, previews=previews)
    g.is_series = is_series
    g.is_ambiguous = is_ambiguous
    g.group_id = group_id
    return g


def _run(
    groups,
    tmp_path: Path,
    total_scanned: int = 10,
    settings: Settings | None = None,
) -> tuple[Path, str]:
    if settings is None:
        settings = Settings()
    src = tmp_path / "source"
    out = tmp_path / "output"
    out.mkdir(parents=True, exist_ok=True)
    report_path = generate_report(groups, out, src, total_scanned, settings)
    html = report_path.read_text(encoding="utf-8")
    return report_path, html


# ═════════════════════════════════════════════════════════════════════════════
# Basic structure
# ═════════════════════════════════════════════════════════════════════════════

class TestReportStructure:

    def test_creates_report_html(self, tmp_path):
        g = _simple_group(tmp_path)
        path, _ = _run([g], tmp_path)
        assert path.name == "report.html"
        assert path.exists()

    def test_html_has_doctype(self, tmp_path):
        _, html = _run([_simple_group(tmp_path)], tmp_path)
        assert html.startswith("<!DOCTYPE html>")

    def test_empty_groups_shows_no_duplicates_message(self, tmp_path):
        _, html = _run([], tmp_path)
        assert "No duplicate groups found" in html

    def test_source_folder_in_header(self, tmp_path):
        g = _simple_group(tmp_path)
        src = tmp_path / "source"
        out = tmp_path / "output"
        out.mkdir(parents=True, exist_ok=True)
        report_path = generate_report([g], out, src, 5, Settings())
        html = report_path.read_text()
        assert str(src) in html

    def test_dry_run_tag_present_when_dry(self, tmp_path):
        s = Settings()
        s.dry_run = True
        _, html = _run([_simple_group(tmp_path)], tmp_path, settings=s)
        assert "DRY RUN" in html

    def test_live_tag_when_not_dry(self, tmp_path):
        s = Settings()
        s.dry_run = False
        _, html = _run([_simple_group(tmp_path)], tmp_path, settings=s)
        assert "FILES MOVED" in html


# ═════════════════════════════════════════════════════════════════════════════
# Statistics
# ═════════════════════════════════════════════════════════════════════════════

class TestReportStatistics:

    def test_total_scanned_in_stats(self, tmp_path):
        _, html = _run([_simple_group(tmp_path)], tmp_path, total_scanned=42)
        assert ">42<" in html

    def test_group_count_in_stats(self, tmp_path):
        groups = [_simple_group(tmp_path, group_id=f"g{i:04d}") for i in range(3)]
        _, html = _run(groups, tmp_path)
        assert ">3<" in html

    def test_previews_count_in_stats(self, tmp_path):
        groups = [
            _simple_group(tmp_path, n_previews=2, group_id="g0001"),
            _simple_group(tmp_path, n_previews=3, group_id="g0002"),
        ]
        _, html = _run(groups, tmp_path)
        assert ">5<" in html  # total_previews = 5

    def test_series_count_in_stats(self, tmp_path):
        groups = [
            _simple_group(tmp_path, is_series=True, group_id="g0001"),
            _simple_group(tmp_path, is_series=False, group_id="g0002"),
        ]
        _, html = _run(groups, tmp_path)
        assert ">1<" in html  # series_count = 1

    def test_space_saved_appears_in_stats(self, tmp_path):
        g = _simple_group(tmp_path, n_previews=1)
        _, html = _run([g], tmp_path)
        # Space saved is in "X.Y MB" format
        assert re.search(r'\d+\.\d+ MB', html)


# ═════════════════════════════════════════════════════════════════════════════
# Group cards
# ═════════════════════════════════════════════════════════════════════════════

class TestGroupCards:

    def test_group_id_in_card(self, tmp_path):
        g = _simple_group(tmp_path, group_id="g0042")
        _, html = _run([g], tmp_path)
        assert "g0042" in html

    def test_series_badge_when_is_series(self, tmp_path):
        g = _simple_group(tmp_path, is_series=True, group_id="g0001")
        _, html = _run([g], tmp_path)
        assert "SERIES" in html

    def test_no_series_badge_when_not_series(self, tmp_path):
        g = _simple_group(tmp_path, is_series=False, group_id="g0001")
        _, html = _run([g], tmp_path)
        # CSS class is always in <style>; check the actual badge element is absent
        assert 'class="series-badge"' not in html

    def test_ambiguous_badge_when_ambiguous(self, tmp_path):
        g = _simple_group(tmp_path, is_ambiguous=True, group_id="g0001")
        _, html = _run([g], tmp_path)
        assert "UNCERTAIN MATCH" in html
        assert "card-ambiguous" in html

    def test_no_ambiguous_badge_for_normal_group(self, tmp_path):
        g = _simple_group(tmp_path, is_ambiguous=False, group_id="g0001")
        _, html = _run([g], tmp_path)
        assert "UNCERTAIN MATCH" not in html

    def test_multiple_groups_all_appear(self, tmp_path):
        groups = [_simple_group(tmp_path, group_id=f"g{i:04d}") for i in range(5)]
        _, html = _run(groups, tmp_path)
        for i in range(5):
            assert f"g{i:04d}" in html

    def test_originals_label_kept(self, tmp_path):
        g = _simple_group(tmp_path, n_originals=2, n_previews=1, group_id="g0001")
        _, html = _run([g], tmp_path)
        assert "2 originals kept" in html

    def test_previews_label_removed(self, tmp_path):
        g = _simple_group(tmp_path, n_originals=1, n_previews=3, group_id="g0001")
        _, html = _run([g], tmp_path)
        assert "3 previews removed" in html


# ═════════════════════════════════════════════════════════════════════════════
# Thumbnail embedding threshold
# ═════════════════════════════════════════════════════════════════════════════

class TestThumbnailThreshold:

    def test_no_thumbs_notice_above_threshold(self, tmp_path):
        # Create enough groups to exceed _THUMB_MAX_IMAGES
        # Each group has 1 original + 1 preview = 2 images
        # We need > _THUMB_MAX_IMAGES total images
        n_groups = (_THUMB_MAX_IMAGES // 2) + 1
        groups = []
        for i in range(n_groups):
            # Use same files to avoid creating too many real images
            g = _simple_group(tmp_path, n_originals=1, n_previews=1, group_id=f"g{i:04d}")
            groups.append(g)

        _, html = _run(groups, tmp_path)
        assert "Thumbnails are not embedded" in html

    def test_no_thumbs_notice_absent_below_threshold(self, tmp_path):
        g = _simple_group(tmp_path, n_originals=1, n_previews=1, group_id="g0001")
        _, html = _run([g], tmp_path)
        assert "Thumbnails are not embedded" not in html


# ═════════════════════════════════════════════════════════════════════════════
# _thumb_b64 helper
# ═════════════════════════════════════════════════════════════════════════════

class TestThumbB64:

    def test_returns_data_uri_for_valid_jpeg(self, tmp_path):
        img_path = _make_jpeg(tmp_path / "test.jpg")
        result = _thumb_b64(img_path)
        assert result.startswith("data:image/jpeg;base64,")

    def test_returns_empty_for_nonexistent_file(self, tmp_path):
        result = _thumb_b64(tmp_path / "ghost.jpg")
        assert result == ""

    def test_returns_data_uri_for_png(self, tmp_path):
        p = tmp_path / "test.png"
        Image.new("RGB", (10, 10), (100, 150, 200)).save(p, format="PNG")
        result = _thumb_b64(p)
        assert result.startswith("data:image/png;base64,")

    def test_respects_max_px(self, tmp_path):
        import base64, io
        p = _make_jpeg(tmp_path / "big.jpg")
        result = _thumb_b64(p, max_px=8)
        assert result.startswith("data:image/")
        # Decode to verify dimensions <= 8
        raw = base64.b64decode(result.split(",", 1)[1])
        img = Image.open(io.BytesIO(raw))
        assert max(img.width, img.height) <= 8


# ═════════════════════════════════════════════════════════════════════════════
# Settings reflected in HTML
# ═════════════════════════════════════════════════════════════════════════════

class TestReportSettings:

    def test_keep_strategy_oldest_in_report(self, tmp_path):
        s = Settings()
        s.keep_strategy = "oldest"
        _, html = _run([_simple_group(tmp_path)], tmp_path, settings=s)
        assert "Oldest file date" in html

    def test_keep_strategy_largest_in_report(self, tmp_path):
        s = Settings()
        s.keep_strategy = "largest"
        _, html = _run([_simple_group(tmp_path)], tmp_path, settings=s)
        assert "Largest pixels" in html

    def test_keep_all_formats_yes_in_report(self, tmp_path):
        s = Settings()
        s.keep_all_formats = True
        _, html = _run([_simple_group(tmp_path)], tmp_path, settings=s)
        assert "Keep all formats" in html or "Yes" in html

    def test_html_report_is_valid_utf8(self, tmp_path):
        path, _ = _run([_simple_group(tmp_path)], tmp_path)
        # Already read as UTF-8 in _run; just verify the file round-trips
        content = path.read_bytes()
        content.decode("utf-8")  # must not raise
