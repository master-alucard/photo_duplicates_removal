"""
tests/test_low_entropy_rotation_guard.py
Regression tests for the low-entropy rotation false-merge bug.

Background
----------
Near-uniform images (night-sky photos, solid-colour screenshots) have pHash
patterns that are nearly symmetric under rotation.  A tiny bright moon on a
black field produces a pHash that, at 180-degree rotation, accidentally has a
Hamming distance of 4-6 bits against a different night-sky shot, triggering
the rotation floor (6 bits) even though the direct pHash distance is 28-36.

This was the root cause of the wrong app-group that merged:
  file 1620x1080_183007.jpg  (GT: negatives/neg_04)
  file 1620x1080_183906.jpg  (GT: groups/pair_114)
  file 1620x1080_183905.jpg  (GT: groups/pair_114)
  file 1620x1080_183309.jpg  (GT: negatives/neg_01)

Fix: _can_be_similar and _find_groups_fast both skip rotation-aware pHash
search when BOTH images have histogram entropy < _LOW_ENTROPY_THR.  The direct
pHash distance is used instead, so coincidental rotation symmetry can no longer
bridge unrelated shots.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

import imagehash

from config import Settings
from scanner import (
    ImageRecord,
    _can_be_similar,
    find_groups,
    _LOW_ENTROPY_THR,
)


def _low_entropy_histogram(bias: float = 0.99) -> list:
    """
    96-bin histogram (32 bins x 3 RGB channels) with low Shannon entropy.

    Each channel independently concentrates ``bias`` fraction in its darkest bin
    (bin 0 per channel) and spreads the remainder uniformly.  Each channel sums
    to 1.0 so that histogram intersection of two identical histograms equals 1.0.
    """
    h = [0.0] * 96
    remainder = (1.0 - bias) / 31  # spread across remaining 31 bins per channel
    for ch in range(3):
        base = ch * 32
        h[base] = bias
        for i in range(1, 32):
            h[base + i] = remainder
    return h


def _normal_histogram(seed: int = 0) -> list:
    """
    96-bin histogram with high Shannon entropy (representative of a normal photo).
    Each channel sums to 1.0; entropy well above _LOW_ENTROPY_THR (3.0 nats).
    """
    # Flat distribution across 32 bins per channel = max entropy per channel
    per_bin = 1.0 / 32
    h = [per_bin] * 96
    # Tiny perturbation so two different seeds produce slightly different histograms
    ch_offset = (seed % 3) * 32
    bin_offset = seed % 32
    h[ch_offset + bin_offset] += 0.05
    # Re-normalise the perturbed channel
    ch_sum = sum(h[ch_offset:ch_offset + 32])
    for i in range(ch_offset, ch_offset + 32):
        h[i] /= ch_sum
    return h


def _make_record(idx, phash_hex, dhash_hex=None, brightness=5.0,
                 histogram=None, phash_r90_hex=None,
                 phash_r180_hex=None, phash_r270_hex=None,
                 w=1620, h=1080):
    dh = imagehash.hex_to_hash(dhash_hex if dhash_hex else phash_hex)
    r90  = imagehash.hex_to_hash(phash_r90_hex)  if phash_r90_hex  else None
    r180 = imagehash.hex_to_hash(phash_r180_hex) if phash_r180_hex else None
    r270 = imagehash.hex_to_hash(phash_r270_hex) if phash_r270_hex else None
    return ImageRecord(
        path=Path(f"/synthetic/low_ent_{idx:04d}.jpg"),
        width=w, height=h,
        file_size=80_000,
        phash=imagehash.hex_to_hash(phash_hex),
        dhash=dh,
        mtime=float(idx),
        brightness=brightness,
        histogram=histogram if histogram is not None else _low_entropy_histogram(),
        companions=None,
        metadata_count=0,
        phash_r90=r90,
        phash_r180=r180,
        phash_r270=r270,
    )


def _make_settings(threshold=2):
    s = Settings()
    s.threshold = threshold
    s.use_dual_hash = True
    s.use_histogram = True
    s.hist_min_similarity = 0.70
    s.dark_protection = True
    s.dark_threshold = 40.0
    s.dark_tighten_factor = 0.5
    s.series_threshold_factor = 1.0
    s.series_tolerance_pct = 0.0
    s.rotation_threshold_factor = 3.0
    return s


# Real hashes measured from the actual files by _moon_diag.py
_PHASH_A  = "99266699666699dc"   # 183007
_PHASH_B  = "cccc333333cccccc"   # 183309
# Create A-r180 so that a.phash_r180 XOR b.phash has exactly 6 bits set
_B_INT   = int(_PHASH_B, 16)
_A_R180  = f"{(_B_INT ^ 0x3f):016x}"
# Genuine duplicate pair
_PHASH_SAME_A = "aaaa555555aaaa55"
_PHASH_SAME_B = "aaaa555555aaaa55"


class TestLowEntropyRotationGuard(unittest.TestCase):

    def setUp(self):
        self.s = _make_settings(threshold=2)

    def test_accidental_rotation_match_is_rejected(self):
        a = _make_record(0, _PHASH_A, phash_r180_hex=_A_R180)
        b = _make_record(1, _PHASH_B)

        direct_dist = a.phash - b.phash
        rot_dist    = a.phash_r180 - b.phash

        self.assertGreater(direct_dist, 10)
        self.assertLessEqual(rot_dist, 6)

        result = _can_be_similar(a, b, self.s)
        self.assertFalse(result,
            f"Low-entropy accidental rotation match (direct={direct_dist}, rot={rot_dist}) "
            "must be rejected")

    def test_genuine_duplicate_low_entropy_passes(self):
        a = _make_record(0, _PHASH_SAME_A, dhash_hex=_PHASH_SAME_A)
        b = _make_record(1, _PHASH_SAME_B, dhash_hex=_PHASH_SAME_B)
        self.assertEqual(a.phash - b.phash, 0)
        self.assertTrue(_can_be_similar(a, b, self.s),
            "Identical low-entropy images (pHash=0, dHash=0) must still be grouped")

    def test_normal_entropy_rotation_match_still_passes(self):
        rot_floor = int(2 * self.s.rotation_threshold_factor)
        a = _make_record(
            0, _PHASH_A, phash_r180_hex=_A_R180,
            brightness=128.0,
            histogram=_normal_histogram(0),
        )
        b = _make_record(
            1, _PHASH_B,
            brightness=128.0,
            histogram=_normal_histogram(1),
        )
        rot_dist = a.phash_r180 - b.phash
        self.assertLessEqual(rot_dist, rot_floor)
        result = _can_be_similar(a, b, self.s)
        self.assertTrue(result,
            f"Normal-entropy rotation match (rot_dist={rot_dist}) must still pass")

    def test_transitive_chain_breaks_for_low_entropy(self):
        """A-B genuine duplicate, B-C accidental rotation match: C must not join A-B."""
        s = _make_settings(threshold=2)
        s.use_histogram = False

        a = _make_record(
            0, _PHASH_SAME_A, dhash_hex=_PHASH_SAME_A,
            histogram=_low_entropy_histogram(),
        )
        b = _make_record(
            1, _PHASH_SAME_A, dhash_hex=_PHASH_SAME_A,
            phash_r180_hex=_A_R180,
            histogram=_low_entropy_histogram(),
        )
        c = _make_record(
            2, _PHASH_B, dhash_hex=_PHASH_B,
            histogram=_low_entropy_histogram(),
        )

        self.assertEqual(a.phash - b.phash, 0)
        direct_bc  = b.phash - c.phash
        rotated_bc = b.phash_r180 - c.phash
        self.assertGreater(direct_bc, 10, f"B-C direct pHash should be large: {direct_bc}")
        self.assertLessEqual(rotated_bc, 6, f"B-C rotated pHash should be <= rot floor: {rotated_bc}")

        records = [a, b, c]
        groups, _ = find_groups(records, s)

        group_map = {}
        for g_idx, g in enumerate(groups):
            for rec in g.originals + g.previews:
                if "0000" in rec.path.name:
                    group_map[0] = g_idx
                elif "0001" in rec.path.name:
                    group_map[1] = g_idx
                elif "0002" in rec.path.name:
                    group_map[2] = g_idx

        self.assertEqual(group_map.get(0), group_map.get(1),
            "A and B (pHash=0 duplicate) must be in the same group")

        if 2 in group_map:
            self.assertNotEqual(group_map[2], group_map.get(1),
                "C (accidental rotation match) must NOT chain into A-B group")


if __name__ == "__main__":
    unittest.main()
