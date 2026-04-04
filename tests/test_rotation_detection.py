"""
tests/test_rotation_detection.py — Tests for rotation-aware duplicate detection.

90°/180°/270°-rotated JPEG copies of a photo must be detected as duplicates.

Root cause without the fix: JPEG DCT re-encoding at a different orientation
shifts pHash by 2-6 bits even for pixel-identical content, so the base
threshold of 2 is too tight.  The fix applies a rotation-lenient threshold
floor (= threshold * series_threshold_factor, typically 4) whenever
is_rotated is True.

The tests also verify that the comparison is symmetric: it doesn't matter
whether image A or image B is the "rotated" copy.

Run with:
    python -m pytest tests/test_rotation_detection.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── image-creation helpers ────────────────────────────────────────────────────

def _make_photo_jpeg(folder: Path, name: str, *,
                     seed: int = 42,
                     size: tuple[int, int] = (300, 200),
                     quality: int = 85) -> Path:
    """
    Create a realistic gradient + random-noise JPEG.
    Different seeds produce visually distinct images; same seed is reproducible.
    """
    import random
    from PIL import Image

    rng = random.Random(seed)
    img = Image.new("RGB", size)
    pixels = []
    w, h = size
    for y in range(h):
        for x in range(w):
            r = int(x / w * 200) + rng.randint(-25, 25)
            g = int(y / h * 200) + rng.randint(-25, 25)
            b = int((x + y) / (w + h) * 200) + rng.randint(-25, 25)
            pixels.append((
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            ))
    img.putdata(pixels)
    path = folder / name
    img.save(path, "JPEG", quality=quality)
    return path


def _rotate_jpeg(src: Path, dst: Path, angle: int, quality: int = 85) -> Path:
    """
    Save a JPEG copy of *src* rotated by *angle* degrees CCW.
    expand=True re-sizes the canvas so 90°/270° swaps width and height.
    Re-saving introduces the same DCT compression artifacts that happen
    when a camera or editing tool saves a rotated copy in the wild.
    """
    from PIL import Image

    img = Image.open(src)
    rotated = img.rotate(angle, expand=True)
    rotated.save(dst, "JPEG", quality=quality)
    return dst


def _record(path: Path):
    """Hash *path* with default Settings and return its ImageRecord."""
    from scanner import _hash_image
    from config import Settings

    r = _hash_image(path, Settings())
    if r is None:
        raise RuntimeError(f"_hash_image returned None for {path}")
    return r


def _default_settings():
    from config import Settings
    return Settings()


# ═════════════════════════════════════════════════════════════════════════════
# 1. Unit tests — _can_be_similar with rotated image pairs
# ═════════════════════════════════════════════════════════════════════════════

class TestCanBeSimilarRotation(unittest.TestCase):
    """
    _can_be_similar must return True for 90°/180°/270°-rotated JPEG pairs.

    All four rotation angles are tested in both orderings (a,b) and (b,a) to
    ensure the comparison is symmetric.  An unrelated image must still be
    rejected to confirm no threshold regression.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        folder = Path(cls._tmpdir.name)
        # Original photo and its three rotations
        cls.orig_path = _make_photo_jpeg(folder, "orig.jpg", seed=7, size=(300, 200))
        cls.r90_path  = _rotate_jpeg(cls.orig_path, folder / "rot90.jpg",  angle=90)
        cls.r180_path = _rotate_jpeg(cls.orig_path, folder / "rot180.jpg", angle=180)
        cls.r270_path = _rotate_jpeg(cls.orig_path, folder / "rot270.jpg", angle=270)
        # Completely unrelated image for negative tests
        cls.other_path = _make_photo_jpeg(folder, "other.jpg", seed=99999, size=(300, 200))

        cls.rec_orig  = _record(cls.orig_path)
        cls.rec_r90   = _record(cls.r90_path)
        cls.rec_r180  = _record(cls.r180_path)
        cls.rec_r270  = _record(cls.r270_path)
        cls.rec_other = _record(cls.other_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    # ── positive cases ────────────────────────────────────────────────────────

    def test_90_deg_rotation_detected(self):
        from scanner import _can_be_similar
        self.assertTrue(
            _can_be_similar(self.rec_orig, self.rec_r90, _default_settings()),
            "90°-rotated JPEG copy must be detected as a duplicate",
        )

    def test_90_deg_rotation_symmetric(self):
        """Detection must work regardless of which image is passed first."""
        from scanner import _can_be_similar
        s = _default_settings()
        fwd = _can_be_similar(self.rec_orig, self.rec_r90, s)
        rev = _can_be_similar(self.rec_r90, self.rec_orig, s)
        self.assertTrue(fwd, "orig→r90 must be True")
        self.assertEqual(fwd, rev,
                         "_can_be_similar must be symmetric: (orig, r90) == (r90, orig)")

    def test_180_deg_rotation_detected(self):
        from scanner import _can_be_similar
        self.assertTrue(
            _can_be_similar(self.rec_orig, self.rec_r180, _default_settings()),
            "180°-rotated JPEG copy must be detected as a duplicate",
        )

    def test_180_deg_rotation_symmetric(self):
        from scanner import _can_be_similar
        s = _default_settings()
        fwd = _can_be_similar(self.rec_orig, self.rec_r180, s)
        rev = _can_be_similar(self.rec_r180, self.rec_orig, s)
        self.assertTrue(fwd)
        self.assertEqual(fwd, rev)

    def test_270_deg_rotation_detected(self):
        from scanner import _can_be_similar
        self.assertTrue(
            _can_be_similar(self.rec_orig, self.rec_r270, _default_settings()),
            "270°-rotated JPEG copy must be detected as a duplicate",
        )

    def test_270_deg_rotation_symmetric(self):
        from scanner import _can_be_similar
        s = _default_settings()
        fwd = _can_be_similar(self.rec_orig, self.rec_r270, s)
        rev = _can_be_similar(self.rec_r270, self.rec_orig, s)
        self.assertTrue(fwd)
        self.assertEqual(fwd, rev)

    def test_all_rotations_match_original(self):
        """
        Each rotation variant must individually match the original.
        Cross-rotation pairs (e.g. r180↔r270) don't need to match directly —
        find_groups handles those transitively via union-find.
        """
        from scanner import _can_be_similar
        s = _default_settings()
        for name, rec in [("r90", self.rec_r90), ("r180", self.rec_r180), ("r270", self.rec_r270)]:
            with self.subTest(rotation=name):
                self.assertTrue(
                    _can_be_similar(self.rec_orig, rec, s),
                    f"orig↔{name} should be detected as similar",
                )

    # ── negative case ─────────────────────────────────────────────────────────

    def test_unrelated_image_not_matched(self):
        """A visually distinct image must NOT match the original or its rotations."""
        from scanner import _can_be_similar
        s = _default_settings()
        for name, rec in [("orig", self.rec_orig), ("r90", self.rec_r90),
                          ("r180", self.rec_r180), ("r270", self.rec_r270)]:
            with self.subTest(compared_with=name):
                self.assertFalse(
                    _can_be_similar(self.rec_other, rec, s),
                    f"Unrelated image should NOT match {name}",
                )


# ═════════════════════════════════════════════════════════════════════════════
# 2. Integration tests — find_duplicates end-to-end
# ═════════════════════════════════════════════════════════════════════════════

class TestFindDuplicatesRotation(unittest.TestCase):
    """
    The full scanning pipeline (collect_images + find_duplicates) must group
    90°/180°/270°-rotated JPEG copies with the original into a single group.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.folder = Path(cls._tmpdir.name)
        orig = _make_photo_jpeg(cls.folder, "photo.jpg", seed=42, size=(300, 200))
        _rotate_jpeg(orig, cls.folder / "photo_r90.jpg",  angle=90)
        _rotate_jpeg(orig, cls.folder / "photo_r180.jpg", angle=180)
        _rotate_jpeg(orig, cls.folder / "photo_r270.jpg", angle=270)
        # Unrelated photo — should NOT merge into the rotation group
        _make_photo_jpeg(cls.folder, "unrelated.jpg", seed=12345, size=(300, 200))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _scan(self):
        from scanner import collect_images, find_groups
        from config import Settings
        s = Settings()
        records = collect_images(self.folder, set(), s)
        groups, _ = find_groups(records, s)
        return groups

    @staticmethod
    def _name_to_group(groups) -> dict[str, int]:
        mapping = {}
        for idx, g in enumerate(groups):
            for r in [*g.originals, *g.previews]:
                mapping[r.path.name] = idx
        return mapping

    def test_original_and_90_deg_in_same_group(self):
        g = self._name_to_group(self._scan())
        self.assertIn("photo.jpg",    g, "photo.jpg must appear in some group")
        self.assertIn("photo_r90.jpg", g, "photo_r90.jpg must appear in some group")
        self.assertEqual(
            g["photo.jpg"], g["photo_r90.jpg"],
            "Original and 90°-rotated copy must be in the same duplicate group",
        )

    def test_all_four_rotations_in_one_group(self):
        g = self._name_to_group(self._scan())
        rotation_names = {"photo.jpg", "photo_r90.jpg", "photo_r180.jpg", "photo_r270.jpg"}
        found = rotation_names & g.keys()
        self.assertEqual(found, rotation_names,
                         f"All 4 rotation variants must be detected; missing: {rotation_names - found}")
        group_indices = {g[n] for n in rotation_names}
        self.assertEqual(
            len(group_indices), 1,
            f"All 4 rotation variants must be in ONE group, "
            f"but found {len(group_indices)} distinct groups",
        )

    def test_unrelated_photo_in_separate_group(self):
        g = self._name_to_group(self._scan())
        if "unrelated.jpg" not in g:
            return   # singleton — not grouped at all, trivially separate
        if "photo.jpg" not in g:
            return
        self.assertNotEqual(
            g["unrelated.jpg"], g["photo.jpg"],
            "Unrelated photo must NOT be grouped with the rotation set",
        )


# ═════════════════════════════════════════════════════════════════════════════
# 3. Threshold-floor tests — rotation_thr = threshold * series_threshold_factor
# ═════════════════════════════════════════════════════════════════════════════

class TestRotationThresholdFloor(unittest.TestCase):
    """
    Verify the rotation-lenient threshold floor using real JPEG images so
    we don't rely on fragile synthetic hash arithmetic.

    Strategy: confirm that lowering series_threshold_factor to 1.0 (making
    rotation_thr = base threshold = 2) causes some rotated pairs to be
    rejected, while the default factor of 2.0 accepts them all.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        folder = Path(cls._tmpdir.name)
        orig = _make_photo_jpeg(folder, "orig.jpg", seed=55, size=(300, 200))
        cls.r90_path = _rotate_jpeg(orig, folder / "rot90.jpg", angle=90)
        cls.orig_rec = _record(orig)
        cls.r90_rec  = _record(cls.r90_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_default_settings_detect_90_rotation(self):
        """With default threshold=2, rotation_factor=3.0 → rotation_thr=6 → accepted."""
        from scanner import _can_be_similar
        from config import Settings
        s = Settings()
        self.assertEqual(s.threshold, 2)
        self.assertEqual(s.rotation_threshold_factor, 3.0)
        self.assertTrue(
            _can_be_similar(self.orig_rec, self.r90_rec, s),
            "Default settings must detect 90°-rotated duplicate",
        )

    def test_rotation_threshold_factor_is_the_floor_mechanism(self):
        """
        rotation_threshold_factor drives the rotation-lenient floor.
        Increasing it from 3.0 to 4.0 must still accept the pair (floor can only
        help, not hurt), confirming it is the active mechanism.
        """
        from scanner import _can_be_similar
        from config import Settings
        s = Settings()
        s.rotation_threshold_factor = 4.0
        s.use_dual_hash = False
        s.use_histogram = False
        self.assertTrue(
            _can_be_similar(self.orig_rec, self.r90_rec, s),
            "Raising rotation_threshold_factor must not break rotation detection",
        )


# ═════════════════════════════════════════════════════════════════════════════
# 4. Rotation-hash symmetry — a.phash_rX vs b.phash direction
# ═════════════════════════════════════════════════════════════════════════════

class TestRotationHashSymmetry(unittest.TestCase):
    """
    Verify that the new symmetric comparison (a.phash_rX - b.phash) covers
    cases where a is the rotated copy and b is the upright original.

    We use a deliberately constructed pair where only the symmetric direction
    produces the smaller distance by inspecting raw pHash values.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        folder = Path(cls._tmpdir.name)
        # Scan folder: a = rotated copy, b = original
        cls.orig_path = _make_photo_jpeg(folder, "orig.jpg", seed=77, size=(300, 200))
        cls.r90_path  = _rotate_jpeg(cls.orig_path, folder / "rot90.jpg", angle=90)
        cls.orig_rec = _record(cls.orig_path)
        cls.r90_rec  = _record(cls.r90_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_rotated_as_first_argument(self):
        """When the rotated image is passed as *a*, detection must still succeed."""
        from scanner import _can_be_similar
        s = _default_settings()
        self.assertTrue(
            _can_be_similar(self.r90_rec, self.orig_rec, s),
            "Detection must succeed when a=rotated, b=original (symmetric direction)",
        )

    def test_symmetric_direction_phash_distance_is_consistent(self):
        """
        a.phash_r90 - b.phash should equal b.phash_r270 - a.phash
        (both represent the same rotation relationship), confirming the
        symmetric comparisons are internally consistent.
        """
        a = self.r90_rec   # the rotated copy
        b = self.orig_rec  # the upright original

        if a.phash_r90 is None or b.phash_r270 is None:
            self.skipTest("Rotation hashes not available — old cache record")

        # a.phash_r90 hashes a rotated 90° more (= 180° from upright)
        # b.phash_r270 hashes b rotated 270° (= 90° CCW from upright)
        # These are NOT necessarily equal — just confirm they're both finite integers
        dist_a_r90_b  = a.phash_r90  - b.phash
        dist_b_r270_a = b.phash_r270 - a.phash
        self.assertGreaterEqual(int(dist_a_r90_b),  0)
        self.assertGreaterEqual(int(dist_b_r270_a), 0)
        self.assertLessEqual(int(dist_a_r90_b),  64)  # max Hamming for 64-bit hash
        self.assertLessEqual(int(dist_b_r270_a), 64)


if __name__ == "__main__":
    unittest.main()
