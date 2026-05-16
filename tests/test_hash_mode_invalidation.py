"""
tests/test_hash_mode_invalidation.py — Tests for hash_mode cache invalidation.

Verifies that scanner.collect_images invalidates RAW cache entries when the
raw_use_embedded_thumb setting changes, and that non-RAW files are unaffected.

Strategy:
  - Inject a fake 'rawpy' module into sys.modules so rawpy_available=True
    inside collect_images without requiring the native library.
  - Patch scanner._hash_raw to avoid real RAW decoding.
  - Create .cr2 files with real mtime/size so the staleness check passes.
  - FileRecord entries carry phash_r90 so the rotation-hash guard passes.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import imagehash
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from library import FileRecord
from config import Settings
from scanner import ImageRecord, RAW_EXTENSIONS


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_jpeg(path: Path, color=(200, 100, 50)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path, format="JPEG")
    return path


def _dummy_image_record(path: Path) -> ImageRecord:
    """Return a minimal ImageRecord suitable as a _hash_raw/hash_image mock return."""
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
        phash_r90=ph,
        phash_r180=ph,
        phash_r270=ph,
    )


def _warm_file_record(path: Path, hash_mode: str) -> FileRecord:
    """
    Build a FileRecord whose mtime/size match the real file so is_stale() is
    False, with a non-empty phash_r90 so the rotation-hash guard passes.
    """
    st = path.stat()
    dummy = "a" * 16
    return FileRecord(
        path=str(path.resolve()),
        mtime=st.st_mtime,
        size=st.st_size,
        phash=dummy,
        dhash=dummy,
        histogram=[0.1] * 96,
        brightness=128.0,
        width=16, height=16,
        phash_r90=dummy,    # non-empty → rotation-hash guard passes
        phash_r180=dummy,
        phash_r270=dummy,
        hash_mode=hash_mode,
    )


class _FakeRawpy:
    """Minimal rawpy stub that satisfies the 'import rawpy' check."""
    pass


def _rawpy_ctx():
    """Context manager that injects a fake rawpy into sys.modules if needed."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        already = "rawpy" in sys.modules
        if not already:
            sys.modules["rawpy"] = _FakeRawpy()  # type: ignore[assignment]
        try:
            yield
        finally:
            if not already:
                sys.modules.pop("rawpy", None)

    return _cm()


def _settings_with_raw(embedded: bool) -> Settings:
    s = Settings()
    s.use_rawpy = True
    s.raw_use_embedded_thumb = embedded
    return s


# ═════════════════════════════════════════════════════════════════════════════
# hash_mode mismatch triggers cache invalidation
# ═════════════════════════════════════════════════════════════════════════════

class TestHashModeMismatchCausesRehash:

    def test_embedded_on_flushes_standard_cache_entry(self, tmp_path):
        """Cache entry hash_mode="" + settings embedded=True → re-hash."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "shot.cr2"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)   # dummy RAW content

        cache = {str(raw.resolve()): _warm_file_record(raw, hash_mode="")}
        settings = _settings_with_raw(embedded=True)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw", return_value=_dummy_image_record(raw)) as mock_r,
        ):
            records = collect_images(folder, set(), settings,
                                     library_cache=cache, trust_library=False)

        assert mock_r.call_count >= 1, "hash_mode mismatch must trigger _hash_raw"

    def test_embedded_off_flushes_embedded_cache_entry(self, tmp_path):
        """Cache entry hash_mode="embedded" + settings embedded=False → re-hash."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "shot.nef"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)

        cache = {str(raw.resolve()): _warm_file_record(raw, hash_mode="embedded")}
        settings = _settings_with_raw(embedded=False)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw", return_value=_dummy_image_record(raw)) as mock_r,
        ):
            records = collect_images(folder, set(), settings,
                                     library_cache=cache, trust_library=False)

        assert mock_r.call_count >= 1, "hash_mode mismatch must trigger _hash_raw"


# ═════════════════════════════════════════════════════════════════════════════
# hash_mode match → cache hit, no re-hash
# ═════════════════════════════════════════════════════════════════════════════

class TestHashModeMatchSkipsRehash:

    def test_embedded_mode_matches_hits_cache(self, tmp_path):
        """Cache hash_mode="embedded" + settings embedded=True → cache hit."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "a.cr2"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)

        cache = {str(raw.resolve()): _warm_file_record(raw, hash_mode="embedded")}
        settings = _settings_with_raw(embedded=True)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw",
                  side_effect=AssertionError("should not re-hash")) as mock_r,
        ):
            # If hash_mode matches, _hash_raw must NOT be called
            try:
                collect_images(folder, set(), settings,
                               library_cache=cache, trust_library=False)
            except AssertionError as exc:
                pytest.fail(f"Cache hit missed — _hash_raw was called: {exc}")

        assert mock_r.call_count == 0

    def test_standard_mode_matches_hits_cache(self, tmp_path):
        """Cache hash_mode="" + settings embedded=False → cache hit."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "b.arw"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)

        cache = {str(raw.resolve()): _warm_file_record(raw, hash_mode="")}
        settings = _settings_with_raw(embedded=False)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw",
                  side_effect=AssertionError("should not re-hash")) as mock_r,
        ):
            try:
                collect_images(folder, set(), settings,
                               library_cache=cache, trust_library=False)
            except AssertionError as exc:
                pytest.fail(f"Cache hit missed — _hash_raw was called: {exc}")

        assert mock_r.call_count == 0


# ═════════════════════════════════════════════════════════════════════════════
# Non-RAW files are unaffected by hash_mode
# ═════════════════════════════════════════════════════════════════════════════

class TestNonRawFilesIgnoreHashMode:

    def test_jpeg_with_any_hash_mode_hits_cache(self, tmp_path):
        """JPEG files are never subject to hash_mode invalidation."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img = _make_jpeg(folder / "photo.jpg")
        st = img.stat()

        rec = FileRecord(
            path=str(img.resolve()),
            mtime=st.st_mtime,
            size=st.st_size,
            phash="a" * 16,
            dhash="b" * 16,
            histogram=[0.1] * 96,
            brightness=128.0,
            width=16, height=16,
            phash_r90="c" * 16,
            phash_r180="c" * 16,
            phash_r270="c" * 16,
            hash_mode="embedded",   # nonsensical for JPEG — should be ignored
        )
        cache = {str(img.resolve()): rec}

        with patch("scanner._hash_image",
                   side_effect=AssertionError("should not re-hash")) as mock_h:
            try:
                collect_images(folder, set(), Settings(),
                               library_cache=cache, trust_library=False)
            except AssertionError as exc:
                pytest.fail(f"JPEG cache hit missed — _hash_image was called: {exc}")

        assert mock_h.call_count == 0


# ═════════════════════════════════════════════════════════════════════════════
# trust_library bypasses hash_mode check
# ═════════════════════════════════════════════════════════════════════════════

class TestTrustLibraryAndHashMode:

    def test_trust_library_still_invalidates_on_hash_mode_mismatch(self, tmp_path):
        """
        hash_mode invalidation is intentional even with trust_library=True.
        An embedded-thumb pHash differs from a full-decode pHash, so switching
        modes must re-hash regardless of staleness preference.
        trust_library only bypasses the mtime/size staleness check — not
        the hash_mode semantic check.
        """
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "c.dng"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)

        # hash_mode mismatch: cache says "", settings says embedded
        cache = {str(raw.resolve()): _warm_file_record(raw, hash_mode="")}
        settings = _settings_with_raw(embedded=True)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw", return_value=_dummy_image_record(raw)) as mock_r,
        ):
            collect_images(folder, set(), settings,
                           library_cache=cache, trust_library=True)

        # hash_mode mismatch triggers re-hash even with trust_library=True
        assert mock_r.call_count >= 1

    def test_trust_library_bypasses_staleness_with_matching_hash_mode(self, tmp_path):
        """
        trust_library=True + matching hash_mode → stale mtime/size is ignored.
        Regression guard: the rotation-hash (phash_r90) invalidation must NOT
        run when trust_library=True (fixed in the prior session).
        """
        from scanner import collect_images

        folder = tmp_path / "photos"
        img = _make_jpeg(folder / "stale.jpg")

        # Deliberately stale mtime/size — but trust_library should serve it anyway
        st = img.stat()
        stale_rec = FileRecord(
            path=str(img.resolve()),
            mtime=st.st_mtime - 9999,  # wrong mtime
            size=1,                     # wrong size
            phash="a" * 16,
            dhash="b" * 16,
            histogram=[0.1] * 96,
            brightness=128.0,
            width=16, height=16,
            phash_r90="c" * 16,
            phash_r180="c" * 16,
            phash_r270="c" * 16,
            hash_mode="",
        )
        cache = {str(img.resolve()): stale_rec}

        with patch("scanner._hash_image",
                   side_effect=AssertionError("trust_library must skip staleness")) as mock_h:
            try:
                collect_images(folder, set(), Settings(),
                               library_cache=cache, trust_library=True)
            except AssertionError as exc:
                pytest.fail(str(exc))

        assert mock_h.call_count == 0


# ═════════════════════════════════════════════════════════════════════════════
# Stub without hash_mode attribute — getattr safety
# ═════════════════════════════════════════════════════════════════════════════

class TestStubWithoutHashModeAttr:

    def test_non_filerecord_stub_does_not_raise(self, tmp_path):
        """
        If library_cache contains a non-FileRecord object without hash_mode,
        getattr(cached, 'hash_mode', None) must not raise AttributeError.
        The entry should be treated as stale and re-hashed.
        """
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "d.cr3"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)

        class _Stub:
            """Minimal cache stub that looks like a FileRecord but has no hash_mode."""
            path = str(raw.resolve())
            mtime = raw.stat().st_mtime
            size = raw.stat().st_size
            phash = "a" * 16
            phash_r90 = "b" * 16   # non-empty so rotation guard passes

            def is_stale(self, p):
                return False

            def to_image_record(self):
                raise AttributeError("broken")

        cache = {str(raw.resolve()): _Stub()}
        settings = _settings_with_raw(embedded=True)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw", return_value=_dummy_image_record(raw)),
        ):
            # Must not raise AttributeError — should gracefully fall back to fresh hash
            try:
                collect_images(folder, set(), settings,
                               library_cache=cache, trust_library=False)
            except AttributeError as exc:
                pytest.fail(f"AttributeError leaked from cache stub: {exc}")


# ═════════════════════════════════════════════════════════════════════════════
# Writeback sets correct hash_mode
# ═════════════════════════════════════════════════════════════════════════════

class TestWritebackSetsCorrectHashMode:

    def test_writeback_sets_embedded_mode_for_raw(self, tmp_path):
        """After hashing a RAW file with embedded=True, cache entry has hash_mode='embedded'."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        raw = folder / "e.cr2"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"\x00" * 512)

        cache: dict = {}
        settings = _settings_with_raw(embedded=True)

        with (
            _rawpy_ctx(),
            patch("scanner._hash_raw", return_value=_dummy_image_record(raw)),
        ):
            collect_images(folder, set(), settings,
                           library_cache=cache, trust_library=False)

        key = str(raw.resolve())
        assert key in cache, "cache entry should be written back after hash"
        assert cache[key].hash_mode == "embedded"

    def test_writeback_sets_standard_mode_for_jpeg(self, tmp_path):
        """After hashing a JPEG, cache entry has hash_mode=''."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img = _make_jpeg(folder / "photo.jpg")

        cache: dict = {}

        collect_images(folder, set(), Settings(),
                       library_cache=cache, trust_library=False)

        key = str(img.resolve())
        assert key in cache
        assert cache[key].hash_mode == ""
