"""
tests/test_group_split.py — Verify the runaway-group safety net.

Context: union-find is *single-linkage*.  If many near-uniform images are
scanned (blank screenshots, near-black photos, document scans), pairs that
are only 1-bit apart chain unrelated images into one mega-group.  On large
collections this surfaces as a "big false-positive group" in the results.

The fix in ``scanner._split_oversized_bucket`` detects runaway buckets and
re-validates every member against the bucket's medoid.  Unrelated members
that slipped in via the chain are demoted out of the group.

Run with:
    python -m pytest tests/test_group_split.py -v
"""
from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import imagehash

from config import Settings
from scanner import (
    ImageRecord,
    _split_oversized_bucket,
    _can_be_similar,
    find_groups,
    find_video_duplicates,
)


def _make_record(idx: int, phash_hex: str, brightness: float = 128.0,
                 w: int = 1920, h: int = 1080) -> ImageRecord:
    """Build a minimal ImageRecord backed by a real imagehash.ImageHash so
    ``a.phash - b.phash`` returns the real Hamming distance."""
    return ImageRecord(
        path=Path(f"/synthetic/img_{idx:04d}.jpg"),
        width=w, height=h,
        file_size=500_000,
        phash=imagehash.hex_to_hash(phash_hex),
        dhash=imagehash.hex_to_hash(phash_hex),
        mtime=0.0,
        brightness=brightness,
        histogram=[0.1, 0.2, 0.3, 0.4] * 48,   # 192-length = 3*64-bin
        companions=None,
        metadata_count=0,
    )


def _bit_flip(h_hex: str, n_bits: int) -> str:
    """Flip the low-order ``n_bits`` of a 16-hex-char (64-bit) hash."""
    v = int(h_hex, 16)
    mask = (1 << n_bits) - 1
    return f"{v ^ mask:016x}"


class TestSplitOversizedBucket(unittest.TestCase):
    """Direct tests of the split helper — independent of find_groups."""

    def setUp(self):
        self.settings = Settings()
        self.settings.threshold = 2
        self.settings.max_group_size = 10
        self.settings.use_histogram = False  # focus on pHash behaviour
        self.settings.dark_protection = False
        self.settings.series_threshold_factor = 1.0

    def test_small_bucket_passes_through_unchanged(self):
        """Buckets under the cap are returned as a single sub-group."""
        records = [_make_record(i, "0" * 16) for i in range(5)]
        result = _split_oversized_bucket(
            list(range(5)), records, self.settings, cap=10,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(sorted(result[0]), list(range(5)))

    def test_chain_is_broken(self):
        """20 images chained 1-bit-apart end-to-end span 19 bits of
        Hamming distance from first to last.  After split, the medoid
        only pulls in neighbours within threshold of itself, so the
        mega-group collapses to a much smaller one."""
        # Build a chain: i-th hash differs from (i-1)-th by exactly 1 bit
        records = []
        base = 0
        for i in range(20):
            h = f"{base ^ ((1 << i) - 1):016x}"
            records.append(_make_record(i, h, brightness=128.0))

        result = _split_oversized_bucket(
            list(range(20)), records, self.settings, cap=10,
        )

        # The medoid is somewhere in the middle; its direct-match radius is
        # settings.threshold = 2 bits, so at most ~4 neighbours survive.
        # What matters: we no longer return a single 20-member mega-group.
        largest = max((len(g) for g in result), default=0)
        self.assertLess(largest, 20,
                        "Chain must be broken — mega-group should be split")

    def test_genuine_duplicates_survive_split(self):
        """When all members are pairwise-similar to the medoid (true dups),
        the bucket is NOT broken up."""
        # All hashes within 1 bit of base → pairwise within 2 bits
        records = [_make_record(0, "0" * 16)]
        for i in range(19):
            records.append(_make_record(i + 1, f"{1 << i:016x}"))  # single-bit variants

        result = _split_oversized_bucket(
            list(range(20)), records, self.settings, cap=10,
        )

        # All 20 should still be in the first sub-group (medoid is hash 0,
        # every other hash is exactly 1 bit from it).
        self.assertEqual(len(result[0]), 20)

    def test_cap_zero_disables_split(self):
        """``max_group_size=0`` means never split, even for huge buckets."""
        records = [_make_record(i, _bit_flip("0" * 16, i % 64)) for i in range(100)]
        result = _split_oversized_bucket(
            list(range(100)), records, self.settings, cap=0,
        )
        # cap=0 short-circuits: returns single sub-group
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]), 100)


class TestSplitRecursionLimit(unittest.TestCase):
    """
    Regression test for RecursionError in _split_oversized_bucket.

    When a large bucket contains images where every medoid matches nothing,
    the old recursive implementation removed exactly 1 item per call, hitting
    Python's ~1000-frame call-stack limit for buckets > ~1000 images.
    The iterative rewrite must handle this without raising RecursionError.
    """

    def test_large_unmatching_bucket_does_not_recurse(self):
        """1 200 records in a bucket where every medoid matches nothing.

        Old recursive code: 1 200 - cap ≈ 1 150 stack frames → RecursionError.
        Iterative rewrite: 1 150 loop iterations → completes normally.
        """
        from scanner import _split_oversized_bucket

        settings = Settings()
        # threshold=0 → only exact hash matches (distance=0) are accepted.
        # Every record has a unique hash (distance ≥ 1 from all others), so
        # _can_be_similar always returns False: every medoid matches nothing.
        settings.threshold = 0
        settings.use_histogram = False
        settings.dark_protection = False

        n = 1200
        records = []
        for i in range(n):
            # f"{i:016x}" gives 1 200 distinct 64-bit hex values; each pair has
            # Hamming distance ≥ 1 which exceeds threshold=0.
            h = f"{i:016x}"
            records.append(_make_record(i, h, brightness=128.0, w=100, h=100))

        indices = list(range(n))
        # cap=50 — bucket is 24× oversized; each iteration only removes the
        # lone medoid, so the old code needed ~1 150 recursive calls.
        try:
            result = _split_oversized_bucket(indices, records, settings, cap=50)
        except RecursionError:
            self.fail(
                "_split_oversized_bucket raised RecursionError on a "
                f"{n}-item all-unmatching bucket — must use iterative implementation"
            )
        # No matches → no pair survives → result has at most the final
        # capped-out remainder (≤ cap items).
        total_members = sum(len(g) for g in result)
        self.assertLessEqual(total_members, 50,
                             "Only the final capped remainder should survive")


class TestFindGroupsEndToEnd(unittest.TestCase):
    """End-to-end: find_groups with many near-identical images should not
    return one mega-group when ``max_group_size`` is set."""

    def test_find_groups_splits_oversized_bucket(self):
        settings = Settings()
        settings.threshold = 2
        settings.max_group_size = 10
        settings.use_histogram = False
        settings.dark_protection = False
        settings.series_threshold_factor = 1.0
        settings.disable_series_detection = True   # avoid series promotion noise

        # 30 images in a chain — union-find bundles all 30 into one bucket,
        # but medoid split should trim it back.
        records = []
        for i in range(30):
            h = f"{((1 << i) - 1) & ((1 << 64) - 1):016x}"
            records.append(_make_record(i, h, brightness=128.0,
                                         w=800 + i, h=600 + i))

        groups, _ = find_groups(records, settings)

        # Without the fix this returns one group of 30; with the fix the
        # chain is broken and no single group spans the full set.
        max_group = max((len(g.originals) + len(g.previews) for g in groups),
                        default=0)
        self.assertLess(max_group, 30,
                        "find_groups must split oversized union-find buckets")


class TestCrossFormatKeepAllFormats(unittest.TestCase):
    """
    Verify that keep_all_formats=True hides cross-format groups entirely.

    When the user enables "keep all formats", a JPEG+NEF pair should NOT
    appear in the review list at all — neither file should be trashable.
    With keep_all_formats=False, the RAW (NEF) stays as the original and
    the JPEG is flagged as a duplicate (preview).
    """

    BASE_HASH = "0" * 16   # pHash = 0 → distance 0 to itself → exact match

    def _nef_record(self, w: int = 6036, h: int = 4020) -> ImageRecord:
        return ImageRecord(
            path=Path("/photos/img_001.nef"),
            width=w, height=h,
            file_size=22_000_000,
            phash=imagehash.hex_to_hash(self.BASE_HASH),
            dhash=imagehash.hex_to_hash(self.BASE_HASH),
            mtime=1_555_000_000.0,
            brightness=128.0,
            histogram=[1 / 96] * 96,
            companions=[],
            metadata_count=3,
        )

    def _jpg_record(self, w: int = 6000, h: int = 4000) -> ImageRecord:
        return ImageRecord(
            path=Path("/photos/img_001.jpg"),
            width=w, height=h,
            file_size=12_000_000,
            phash=imagehash.hex_to_hash(self.BASE_HASH),
            dhash=imagehash.hex_to_hash(self.BASE_HASH),
            mtime=1_556_000_000.0,
            brightness=130.0,
            histogram=[1 / 96] * 96,
            companions=[],
            metadata_count=3,
        )

    def _settings(self, keep_all_formats: bool) -> Settings:
        s = Settings()
        s.threshold = 2
        s.keep_all_formats = keep_all_formats
        s.keep_strategy = "pixels"
        s.use_histogram = False
        s.dark_protection = False
        s.series_threshold_factor = 2.0
        return s

    def test_keep_all_formats_true_hides_cross_format_group(self):
        """
        With keep_all_formats=True a JPEG+NEF pair must NOT appear in results.
        Both formats are kept → previews list is empty → group returns None.
        """
        records = [self._nef_record(), self._jpg_record()]
        settings = self._settings(keep_all_formats=True)

        groups, _ = find_groups(records, settings)

        self.assertEqual(
            len(groups), 0,
            "keep_all_formats=True must hide JPEG+NEF cross-format pairs "
            f"(got {len(groups)} group(s))",
        )

    def test_keep_all_formats_false_puts_jpeg_in_previews(self):
        """
        With keep_all_formats=False the NEF is kept and the JPEG is a duplicate.
        """
        records = [self._nef_record(), self._jpg_record()]
        settings = self._settings(keep_all_formats=False)

        groups, _ = find_groups(records, settings)

        self.assertEqual(len(groups), 1, "Expected exactly 1 group")
        g = groups[0]
        orig_paths = {r.path.suffix.lower() for r in g.originals}
        prev_paths = {r.path.suffix.lower() for r in g.previews}
        self.assertIn(".nef", orig_paths, "NEF must be in originals")
        self.assertIn(".jpg", prev_paths, "JPEG must be in previews (duplicate)")
        self.assertNotIn(".nef", prev_paths, "NEF must NOT be in previews")

    def test_keep_all_formats_true_portrait_landscape_pair(self):
        """
        Portrait JPEG (4000×6000) matched with landscape NEF (6036×4020)
        via rotation-aware dimension check — must also be hidden when
        keep_all_formats=True.
        """
        nef = self._nef_record(w=6036, h=4020)   # landscape NEF
        jpg = self._jpg_record(w=4000, h=6000)   # portrait JPEG
        settings = self._settings(keep_all_formats=True)

        groups, _ = find_groups([nef, jpg], settings)

        self.assertEqual(
            len(groups), 0,
            "Portrait JPEG + landscape NEF with keep_all_formats=True must be hidden",
        )

    def test_keep_all_formats_near_full_res_jpeg_hidden(self):
        """
        Near-full-res JPEG (5705×3803, ~5.5% smaller than NEF 6036×4020) must
        NOT appear as a preview when keep_all_formats=True.

        Regression: _split_by_format used a strict 2% cross-format tolerance for
        _same_size_as_best, which incorrectly classified 5705×3803 as "too small"
        and put it in previews.  The fix adds a fallback: if _is_preview() returns
        False (the file is ≥ 90% of global_best in both dimensions), it is kept
        as an original regardless of the 2% cross-format dim tolerance.
        """
        nef = self._nef_record(w=6036, h=4020)    # full-res RAW
        jpg = self._jpg_record(w=5705, h=3803)    # ~94 % of NEF size — not a thumbnail
        settings = self._settings(keep_all_formats=True)

        groups, _ = find_groups([nef, jpg], settings)

        self.assertEqual(
            len(groups), 0,
            "Near-full-res JPEG (≥90 % of NEF) must be hidden with keep_all_formats=True "
            f"(got {len(groups)} group(s) — JPEG incorrectly in previews)",
        )

    def test_keep_all_formats_thumbnail_jpeg_still_trashed(self):
        """
        A genuine thumbnail JPEG (3000×2000, 50% of NEF 6036×4020) must still
        appear as a preview even when keep_all_formats=True.

        The fallback in _split_by_format must not absorb actual thumbnails.
        """
        nef = self._nef_record(w=6036, h=4020)    # full-res RAW
        jpg = self._jpg_record(w=3000, h=2000)    # 50 % size — clear thumbnail
        settings = self._settings(keep_all_formats=True)

        groups, _ = find_groups([nef, jpg], settings)

        self.assertEqual(len(groups), 1, "Thumbnail JPEG must produce a group")
        g = groups[0]
        prev_paths = {r.path.suffix.lower() for r in g.previews}
        self.assertIn(".jpg", prev_paths, "Thumbnail JPEG must be in previews")


class TestExifDateGuard(unittest.TestCase):
    """
    Cross-format pairs (RAW+JPEG) from different sessions must NOT be grouped
    together even when their pHash is identical.

    Root cause (v1.1.23): the lenient cross-format pHash threshold (12 bits)
    lets same-subject photos taken on different days pass _can_be_similar and
    get chained into one mega-group via union-find single-linkage.

    Fix: if both files have EXIF dates that differ by > 5 minutes the pair is
    rejected immediately in _can_be_similar before any pHash comparison.
    """

    _BASE = "0" * 16  # pHash distance 0 — would always be accepted without the guard

    def _settings(self) -> Settings:
        s = Settings()
        s.threshold = 2
        s.cross_format_threshold_factor = 6.0
        s.series_threshold_factor = 2.0
        s.keep_all_formats = True
        s.use_histogram = False
        s.dark_protection = False
        return s

    def _cr2(self, exif_date: "datetime.datetime", w: int = 6024, h: int = 4020) -> ImageRecord:
        return ImageRecord(
            path=Path("/photos/img.cr2"),
            width=w, height=h,
            file_size=30_000_000,
            phash=imagehash.hex_to_hash(self._BASE),
            dhash=imagehash.hex_to_hash(self._BASE),
            mtime=exif_date.timestamp(),
            brightness=128.0,
            histogram=[1 / 96] * 96,
            metadata_count=5,
            exif_date=exif_date,
        )

    def _jpg(self, exif_date: "datetime.datetime", w: int = 6000, h: int = 4000) -> ImageRecord:
        return ImageRecord(
            path=Path("/photos/img.jpg"),
            width=w, height=h,
            file_size=6_000_000,
            phash=imagehash.hex_to_hash(self._BASE),
            dhash=imagehash.hex_to_hash(self._BASE),
            mtime=exif_date.timestamp(),
            brightness=128.0,
            histogram=[1 / 96] * 96,
            metadata_count=5,
            exif_date=exif_date,
        )

    def test_same_session_still_linked(self):
        """RAW+JPEG from the same second must still be grouped (guard must not over-reject)."""
        t = datetime.datetime(2019, 9, 13, 15, 33, 0)
        cr2 = self._cr2(t)
        jpg = self._jpg(t)
        settings = self._settings()
        self.assertTrue(
            _can_be_similar(cr2, jpg, settings),
            "_can_be_similar must return True for same-timestamp RAW+JPEG pair",
        )

    def test_cross_day_cross_format_rejected_by_guard(self):
        """RAW and JPEG from 10 days apart must be rejected by the EXIF date guard."""
        cr2 = self._cr2(datetime.datetime(2019, 9, 13, 15, 33, 0))
        jpg = self._jpg(datetime.datetime(2019, 9, 3,  9, 35, 0))
        settings = self._settings()
        self.assertFalse(
            _can_be_similar(cr2, jpg, settings),
            "_can_be_similar must return False when EXIF dates differ by > 5 minutes",
        )

    def test_cross_day_not_grouped_end_to_end(self):
        """find_groups must produce 0 groups for a cross-day RAW+JPEG pair."""
        cr2 = self._cr2(datetime.datetime(2019, 9, 13, 15, 33, 0))
        jpg = self._jpg(datetime.datetime(2019, 9, 3,  9, 35, 0))
        groups, _ = find_groups([cr2, jpg], self._settings())
        self.assertEqual(
            len(groups), 0,
            "Cross-day RAW+JPEG with identical pHash must produce 0 groups "
            f"(got {len(groups)})",
        )

    def test_missing_exif_date_does_not_block(self):
        """When either file lacks an EXIF date the guard must not fire (None → skip)."""
        t = datetime.datetime(2019, 9, 13, 15, 33, 0)
        cr2 = self._cr2(t)
        # JPEG with no EXIF date (as if it came from an old cache entry)
        jpg_no_date = ImageRecord(
            path=Path("/photos/img.jpg"),
            width=6000, height=4000,
            file_size=6_000_000,
            phash=imagehash.hex_to_hash(self._BASE),
            dhash=imagehash.hex_to_hash(self._BASE),
            mtime=0.0,
            brightness=128.0,
            histogram=[1 / 96] * 96,
            metadata_count=0,
            exif_date=None,  # ← no date available
        )
        settings = self._settings()
        self.assertTrue(
            _can_be_similar(cr2, jpg_no_date, settings),
            "_can_be_similar must not reject pairs when EXIF date is missing",
        )


class TestSameFormatSameDimKeepBoth(unittest.TestCase):
    """
    Two RAW files from the same camera (identical dimensions, different content)
    must both be kept as originals when keep_all_formats=True, even when series
    detection is disabled.

    Root cause (v1.1.23): in _split_by_format the non_series_in_ext[1:] loop
    called _is_preview(m, global_best).  For same-dimension files _is_preview
    unconditionally returns True (case 1: "same-resolution duplicates").  The
    second CR2 was therefore always classified as a preview regardless of its
    pHash distance from the best — trashing a genuine original.

    Fix: for same-dimension files, only trash if pHash ≤ _EXACT_DUP_PHASH (2).
    """

    _HASH_A = "f0f0f0f0f0f0f0f0"   # 32 bits set
    # 8-bit flip → distance 8 (> _EXACT_DUP_PHASH=2, so genuinely different shots)
    _HASH_B = "f0f0f0f0f0f0f0ff"

    def _cr2(self, idx: int, phash_hex: str,
             exif_date: "datetime.datetime | None" = None) -> ImageRecord:
        return ImageRecord(
            path=Path(f"/photos/{idx:03d}.cr2"),
            width=6024, height=4020,
            file_size=35_000_000 - idx * 100,   # tiny size difference so sort is stable
            phash=imagehash.hex_to_hash(phash_hex),
            dhash=imagehash.hex_to_hash(phash_hex),
            mtime=1_000_000.0 + idx,
            brightness=128.0,
            histogram=[1 / 96] * 96,
            metadata_count=3,
            exif_date=exif_date,
        )

    def _jpg(self, phash_hex: str,
             exif_date: "datetime.datetime | None" = None) -> ImageRecord:
        return ImageRecord(
            path=Path("/photos/bridge.jpg"),
            width=6000, height=4000,
            file_size=6_000_000,
            phash=imagehash.hex_to_hash(phash_hex),
            dhash=imagehash.hex_to_hash(phash_hex),
            mtime=1_000_500.0,
            brightness=130.0,
            histogram=[1 / 96] * 96,
            metadata_count=3,
            exif_date=exif_date,
        )

    def _settings(self) -> Settings:
        s = Settings()
        s.threshold = 2
        s.cross_format_threshold_factor = 6.0
        s.series_threshold_factor = 2.0
        s.keep_all_formats = True
        s.use_histogram = False
        s.dark_protection = False
        # Disable series detection so the CR2 pair goes through non_series_in_ext
        s.disable_series_detection = True
        return s

    def test_two_cr2s_different_content_both_kept(self):
        """
        CR2_A and CR2_B have identical dimensions but pHash distance 8 (different
        shots).  They are bridged into one group by a JPEG (cross-format, same pHash
        as CR2_A).  With disable_series_detection=True they end up in
        non_series_in_ext.  Both must be kept as originals → group is hidden.
        """
        t = datetime.datetime(2019, 9, 3, 10, 0, 0)
        cr2_a = self._cr2(0, self._HASH_A, t)   # best for .cr2 extension
        cr2_b = self._cr2(1, self._HASH_B, t)   # distance 8 from cr2_a
        jpg   = self._jpg(self._HASH_A, t)       # bridges cr2_a↔cr2_b via cross-format
        settings = self._settings()

        groups, _ = find_groups([cr2_a, cr2_b, jpg], settings)

        self.assertEqual(
            len(groups), 0,
            "Two same-dim CR2s with different content and a JPEG bridge must be "
            f"hidden (all originals) with keep_all_formats=True (got {len(groups)} group(s))",
        )

    def test_exact_duplicate_cr2_is_still_trashed(self):
        """
        Two CR2s with pHash distance 0 (exact same image) must still produce
        a group — only the best copy is kept; the other is a duplicate.
        """
        t = datetime.datetime(2019, 9, 3, 10, 0, 0)
        cr2_a = self._cr2(0, self._HASH_A, t)
        cr2_b = self._cr2(1, self._HASH_A, t)   # identical pHash → distance 0
        jpg   = self._jpg(self._HASH_A, t)
        settings = self._settings()

        groups, _ = find_groups([cr2_a, cr2_b, jpg], settings)

        # Exact duplicate must be trashed, so at least one group exists
        total_previews = sum(len(g.previews) for g in groups)
        self.assertGreater(
            total_previews, 0,
            "Exact-duplicate CR2 (pHash distance 0) must appear in previews",
        )


class TestVideoZeroHashFix(unittest.TestCase):
    """
    Videos whose thumbnail extraction failed (zero pHash) must still be detected
    as duplicates of same-size videos that have valid thumbnails.

    Root cause (v1.1.23): find_video_duplicates set has_thumbs=True when ANY
    member had a non-zero thumbnail.  Zero-hash members were then compared
    against real hashes and failed (distance ~32 >> threshold 8), so they were
    silently excluded from the group.

    Fix: when comparing thumbnails, a zero hash on either side is treated as
    "thumbnail unknown" — the pair is grouped by size alone.
    """

    _ZERO = "0" * 16                  # hash for failed thumbnail extraction
    _REAL = "f0f0f0f0f0f0f0f0"       # a valid non-zero thumbnail hash
    _SIZE = 500_000_000               # 500 MB — any consistent value

    def _video(self, phash_hex: str, size: int = _SIZE, mtime: float = 0.0) -> ImageRecord:
        return ImageRecord(
            path=Path(f"/videos/{phash_hex[:8]}.mp4"),
            width=0, height=0,
            file_size=size,
            phash=imagehash.hex_to_hash(phash_hex),
            dhash=imagehash.hex_to_hash(self._ZERO),
            mtime=mtime,
            brightness=128.0,
            histogram=[],
            is_video=True,
        )

    def _settings(self) -> Settings:
        s = Settings()
        s.video_use_thumb = True
        return s

    def test_zero_hash_video_grouped_with_valid_thumbnail(self):
        """
        A same-size video with a zero hash (thumbnail failed) must be detected
        as a duplicate of a video that has a valid thumbnail.
        """
        video_ok   = self._video(self._REAL, mtime=1000.0)   # valid thumbnail
        video_fail = self._video(self._ZERO, mtime=2000.0)   # failed thumbnail

        groups = find_video_duplicates([video_ok, video_fail], self._settings())

        self.assertEqual(len(groups), 1, "Same-size videos must form one group")
        g = groups[0]
        self.assertEqual(len(g.originals), 1)
        self.assertEqual(len(g.previews), 1)
        # Oldest (smallest mtime) is kept as the original
        self.assertEqual(g.originals[0].mtime, 1000.0)

    def test_two_valid_thumbnails_still_compared_by_phash(self):
        """
        Two same-size videos with different valid thumbnails (distance > 8) must
        NOT be grouped — the fix must not disable thumbnail comparison entirely.
        """
        hash_a = "f0f0f0f0f0f0f0f0"
        hash_b = "0f0f0f0f0f0f0f0f"   # 32 bits differ from hash_a → distance 32 >> 8
        video_a = self._video(hash_a)
        video_b = self._video(hash_b)

        groups = find_video_duplicates([video_a, video_b], self._settings())

        self.assertEqual(
            len(groups), 0,
            "Same-size videos with very different thumbnails must not be grouped",
        )

    def test_two_zero_hash_videos_grouped_by_size(self):
        """
        Two same-size videos with zero hashes (both thumbnails failed) must be
        grouped — fallback to size-only comparison (existing behaviour, unchanged).
        """
        video_a = self._video(self._ZERO, mtime=1000.0)
        video_b = self._video(self._ZERO, mtime=2000.0)

        groups = find_video_duplicates([video_a, video_b], self._settings())

        self.assertEqual(len(groups), 1, "Same-size zero-hash videos must be grouped by size")


class TestGroup24BurstShotChainFix(unittest.TestCase):
    """
    Regression test for the Group-24 false-chain bug.

    Context: calibration_cf folder, set_024 and set_025.  Four files:
      A = CR2_011485 (pHash X)   C = JPG_202278 (pHash X)   -- true pair (same shot)
      B = CR2_011486 (pHash Y)   D = JPG_202287 (pHash Y)   -- true pair (same shot)

    X and Y differ by pHash=4, dHash=5 (consecutive burst shots of a textured
    surface).  At threshold=4 the same-format burst-shot edges A↔B and C↔D
    (both pHash=4) passed the old dHash check (dhash_thr = threshold * 1.5 = 6,
    dHash=5 ≤ 6).  Union-find then chained A, B, C, D into one group instead
    of two.

    Fix: reduce the dHash multiplier from 1.5 to 1.0 so
    dhash_thr = threshold = 4, blocking dHash=5 edges.
    True pairs (pHash=0, dHash=0) are unaffected (pHash=0 → dHash skipped).
    """

    # pHash values derived from real calibration files
    _HASH_A = "acf5f21884e3996c"   # A=CR2_011485, C=JPG_202278 (identical shot)
    _HASH_B = "ade5f2188ce3916c"   # B=CR2_011486, D=JPG_202287 (identical shot)

    def _cr2(self, idx: int, phash_hex: str, dhash_hex: str) -> ImageRecord:
        return ImageRecord(
            path=Path(f"/calib/{idx:03d}.cr2"),
            width=6024, height=4020,
            file_size=29_000_000,
            phash=imagehash.hex_to_hash(phash_hex),
            dhash=imagehash.hex_to_hash(dhash_hex),
            mtime=1_000_000.0 + idx,
            brightness=117.0,
            histogram=[1 / 96] * 96,
            metadata_count=3,
        )

    def _jpg(self, idx: int, phash_hex: str, dhash_hex: str) -> ImageRecord:
        return ImageRecord(
            path=Path(f"/calib/{idx:03d}.jpg"),
            width=6000, height=4000,
            file_size=3_200_000,
            phash=imagehash.hex_to_hash(phash_hex),
            dhash=imagehash.hex_to_hash(dhash_hex),
            mtime=1_000_000.0 + idx + 0.5,
            brightness=128.0,
            histogram=[1 / 96] * 96,
            metadata_count=0,
        )

    def _settings(self) -> Settings:
        s = Settings()
        s.threshold = 4                   # user's calibrated setting that triggered the bug
        s.use_dual_hash = True
        s.use_histogram = False           # isolate hash-based logic
        s.dark_protection = False
        s.series_threshold_factor = 1.0   # as in calibration_cf
        s.use_rawpy = True
        s.raw_use_embedded_thumb = True
        s.keep_all_formats = False
        return s

    def test_true_cf_pairs_pass(self):
        """Same-shot CR2+JPEG pairs (pHash=0, dHash=0) must still be similar."""
        settings = self._settings()
        # A and C are the same shot
        a = self._cr2(0, self._HASH_A, self._HASH_A)
        c = self._jpg(2, self._HASH_A, self._HASH_A)
        self.assertTrue(
            _can_be_similar(a, c, settings),
            "True CF pair (pHash=0, dHash=0) must pass _can_be_similar",
        )

    def test_burst_shot_same_format_blocked(self):
        """Consecutive burst-shot same-format pairs (pHash=4, dHash=5) must be blocked."""
        settings = self._settings()
        # A and B are consecutive burst shots — different images, pHash=4, dHash=5
        a = self._cr2(0, self._HASH_A, "ee2fc6e3f7fb9f93")
        b = self._cr2(1, self._HASH_B, "6e07c7e3f7fb8f93")
        ph = int(a.phash - b.phash)
        dh = int(a.dhash - b.dhash)
        self.assertEqual(ph, 4, f"Expected pHash=4, got {ph}")
        self.assertEqual(dh, 5, f"Expected dHash=5, got {dh}")
        self.assertFalse(
            _can_be_similar(a, b, settings),
            "Burst-shot same-format pair (pHash=4, dHash=5, threshold=4) must be blocked "
            "by dHash check (dhash_thr = threshold * 1.0 = 4 < dHash=5)",
        )

    def test_group_24_splits_into_two_groups(self):
        """
        All four files (A=CR2_485, B=CR2_486, C=JPG_278, D=JPG_287) must form
        exactly two groups: {A, C} and {B, D}.  Union-find must NOT chain them all
        into one group via the A↔B and C↔D burst-shot edges.
        """
        settings = self._settings()
        a = self._cr2(0, self._HASH_A, "ee2fc6e3f7fb9f93")
        b = self._cr2(1, self._HASH_B, "6e07c7e3f7fb8f93")
        c = self._jpg(2, self._HASH_A, "ee2fc6e3f7fb9f93")
        d = self._jpg(3, self._HASH_B, "6e07c7e3f7fb8f93")

        groups, _ = find_groups([a, b, c, d], settings)

        self.assertEqual(
            len(groups), 2,
            f"Expected 2 groups (one per burst-shot pair), got {len(groups)}. "
            "Group-24 false-chain bug: burst-shot edges A↔B and C↔D must be blocked "
            "by the dHash check (dhash_thr = threshold * 1.0).",
        )
        # Verify each group contains exactly one CR2 and one JPEG
        for g in groups:
            all_files = g.originals + g.previews
            exts = {r.path.suffix.lower() for r in all_files}
            self.assertIn(".cr2", exts, "Each group must contain a CR2")
            self.assertIn(".jpg", exts, "Each group must contain a JPEG")
            self.assertEqual(len(all_files), 2, "Each group must have exactly 2 files")


if __name__ == "__main__":
    unittest.main()
