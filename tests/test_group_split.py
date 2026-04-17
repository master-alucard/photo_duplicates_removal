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


if __name__ == "__main__":
    unittest.main()
