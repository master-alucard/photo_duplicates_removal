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

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import imagehash

from config import Settings
from scanner import (
    ImageRecord,
    _split_oversized_bucket,
    find_groups,
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


if __name__ == "__main__":
    unittest.main()
