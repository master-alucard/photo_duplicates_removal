"""
tests/test_cache_merge.py — Tests for cache-merge / browse-always-cached behaviour.

Covers three guarantees introduced in v1.0.4+:

  1. load_cache_merged — subfolder scans reuse a parent folder's cache
  2. collect_images with library_cache — no re-hashing when cache is warm
  3. Stale detection — changed files are always re-hashed
  4. Browse-mode always gets cache — no UI-mode gate on cache loading

Run with:
    python -m pytest tests/test_cache_merge.py -v
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import textwrap

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from library import Library, FileRecord, FolderEntry
from config import Settings


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_image(path: Path, color=(200, 100, 50), size=(16, 16)) -> Path:
    """Create a minimal valid JPEG and return its Path."""
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def _make_folder_entry(path: str, file_count: int = 0) -> FolderEntry:
    return FolderEntry(
        path=path, drive_type="fixed", volume_serial=None,
        folder_fingerprint="", last_updated="2026-01-01T00:00:00",
        file_count=file_count,
    )


def _make_file_record(path: str, mtime: float = 1_700_000_000.0,
                      size: int = 1024) -> FileRecord:
    return FileRecord(
        path=path, mtime=mtime, size=size,
        phash="a" * 16, dhash="b" * 16,
        histogram=[0.1] * 96, brightness=128.0,
        width=16, height=16,
    )


def _real_record(img_path: Path) -> FileRecord:
    """Hash a real image and return its FileRecord (mtime from actual stat)."""
    from scanner import _hash_image
    rec = _hash_image(img_path, Settings())
    st  = img_path.stat()
    return FileRecord.from_image_record(rec, st_mtime=st.st_mtime)


# ═════════════════════════════════════════════════════════════════════════════
# 1. load_cache_merged — unit tests (synthetic records, no real images)
# ═════════════════════════════════════════════════════════════════════════════

class TestLoadCacheMerged:
    """Verify load_cache_merged logic across ancestor/sibling/unrelated paths."""

    def _lib(self, tmp_path: Path) -> Library:
        return Library.load(tmp_path / "lib")

    def test_empty_library_returns_empty(self, tmp_path):
        lib    = self._lib(tmp_path)
        result = lib.load_cache_merged(str(tmp_path / "photos"))
        assert result == {}

    def test_exact_folder_only(self, tmp_path):
        """Cache stored under the exact folder is returned."""
        lib    = self._lib(tmp_path)
        folder = str(tmp_path / "photos")
        key    = str(tmp_path / "photos" / "img.jpg")
        rec    = _make_file_record(key)
        lib.save_cache(folder, {key: rec})

        result = lib.load_cache_merged(folder)
        assert key in result
        assert result[key] == rec

    def test_subfolder_gets_parent_records(self, tmp_path):
        """Scanning a sub-folder finds records stored under the parent."""
        lib    = self._lib(tmp_path)
        parent = str(tmp_path / "photos")
        sub    = str(tmp_path / "photos" / "vacation")

        # Two files: one in sub, one elsewhere in parent
        key_in_sub    = str(tmp_path / "photos" / "vacation" / "img1.jpg")
        key_elsewhere = str(tmp_path / "photos" / "family"   / "img2.jpg")
        lib.set_folder(_make_folder_entry(parent))   # register in index
        lib.save_cache(parent, {
            key_in_sub:   _make_file_record(key_in_sub),
            key_elsewhere: _make_file_record(key_elsewhere),
        })

        result = lib.load_cache_merged(sub)
        # Only the file inside the sub-folder should be returned
        assert key_in_sub    in result
        assert key_elsewhere not in result

    def test_exact_folder_overrides_parent(self, tmp_path):
        """Per-file: exact-folder cache takes priority over ancestor records."""
        lib    = self._lib(tmp_path)
        parent = str(tmp_path / "photos")
        sub    = str(tmp_path / "photos" / "vacation")
        key    = str(tmp_path / "photos" / "vacation" / "img.jpg")

        parent_rec = _make_file_record(key, mtime=1000.0)
        exact_rec  = _make_file_record(key, mtime=2000.0)  # newer
        lib.save_cache(parent, {key: parent_rec})
        lib.save_cache(sub,    {key: exact_rec})

        result = lib.load_cache_merged(sub)
        assert result[key].mtime == 2000.0   # exact wins

    def test_sibling_folder_not_included(self, tmp_path):
        """Records from a sibling (not ancestor) folder must not bleed in."""
        lib     = self._lib(tmp_path)
        sibling = str(tmp_path / "photos" / "family")
        target  = str(tmp_path / "photos" / "vacation")
        key     = str(tmp_path / "photos" / "family" / "img.jpg")
        lib.save_cache(sibling, {key: _make_file_record(key)})

        result = lib.load_cache_merged(target)
        assert key not in result

    def test_grandparent_included(self, tmp_path):
        """Records from a grandparent folder are included for deep sub-paths."""
        lib  = self._lib(tmp_path)
        root = str(tmp_path / "photos")
        sub  = str(tmp_path / "photos" / "vacation" / "2024")
        key  = str(tmp_path / "photos" / "vacation" / "2024" / "img.jpg")
        lib.set_folder(_make_folder_entry(root))   # register in index
        lib.save_cache(root, {key: _make_file_record(key)})

        result = lib.load_cache_merged(sub)
        assert key in result

    def test_parent_and_grandparent_both_contribute(self, tmp_path):
        """Both parent and grandparent caches contribute their matching records."""
        lib         = self._lib(tmp_path)
        grandparent = str(tmp_path / "photos")
        parent      = str(tmp_path / "photos" / "vacation")
        target      = str(tmp_path / "photos" / "vacation" / "2024")
        key_gp      = str(tmp_path / "photos" / "vacation" / "2024" / "from_gp.jpg")
        key_p       = str(tmp_path / "photos" / "vacation" / "2024" / "from_p.jpg")
        lib.set_folder(_make_folder_entry(grandparent))   # register both in index
        lib.set_folder(_make_folder_entry(parent))
        lib.save_cache(grandparent, {key_gp: _make_file_record(key_gp)})
        lib.save_cache(parent,      {key_p:  _make_file_record(key_p)})

        result = lib.load_cache_merged(target)
        assert key_gp in result
        assert key_p  in result

    def test_unrelated_folder_not_included(self, tmp_path):
        """Completely unrelated folder does not leak into results."""
        lib        = self._lib(tmp_path)
        other      = str(tmp_path / "documents")
        target     = str(tmp_path / "photos")
        key        = str(tmp_path / "documents" / "img.jpg")
        lib.save_cache(other, {key: _make_file_record(key)})

        result = lib.load_cache_merged(target)
        assert key not in result

    def test_empty_parent_cache_returns_empty(self, tmp_path):
        """If parent cache exists but is empty, merged result is empty."""
        lib    = self._lib(tmp_path)
        parent = str(tmp_path / "photos")
        sub    = str(tmp_path / "photos" / "vacation")
        lib.save_cache(parent, {})

        result = lib.load_cache_merged(sub)
        assert result == {}


# ═════════════════════════════════════════════════════════════════════════════
# 2. collect_images — cache hit / miss with real images
# ═════════════════════════════════════════════════════════════════════════════

class TestCollectImagesCache:
    """
    Integration tests: create real tiny images, build a warm cache, then verify
    collect_images serves records from cache without calling _hash_image.
    """

    def test_warm_cache_skips_hashing(self, tmp_path):
        """When every file is in the cache (fresh mtime+size), _hash_image is never called."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img    = _make_image(folder / "a.jpg", color=(255, 0, 0))

        # Build a warm cache dict keyed by resolved absolute path
        rec    = _real_record(img)
        cache  = {str(img.resolve()): rec}

        # Patch _hash_image to a bomb — if it's called, the test fails
        with patch("scanner._hash_image", side_effect=AssertionError("should not hash")) as mock_h:
            records = collect_images(folder, set(), Settings(),
                                     library_cache=cache, trust_library=False)

        assert mock_h.call_count == 0
        assert len(records) == 1
        assert records[0].path.resolve() == img.resolve()

    def test_stale_cache_triggers_rehash(self, tmp_path):
        """A cache entry with wrong mtime is NOT trusted — the file is re-hashed."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img    = _make_image(folder / "a.jpg", color=(0, 255, 0))
        st     = img.stat()

        # Stale: mtime is off by 10 s
        stale_rec = _make_file_record(str(img.resolve()),
                                      mtime=st.st_mtime - 10,
                                      size=st.st_size)
        cache = {str(img.resolve()): stale_rec}

        hash_count = [0]
        original_hash_image = None

        def _counting_hash(path, settings):
            # import here to avoid circular
            import scanner as _sc
            hash_count[0] += 1
            return _sc.__dict__["_hash_raw"](path, settings) if hasattr(_sc, "_hash_raw") else None

        # Use a real call but track that it IS called
        with patch("scanner._hash_image", wraps=__import__("scanner")._hash_image) as mock_h:
            records = collect_images(folder, set(), Settings(),
                                     library_cache=cache, trust_library=False)

        assert mock_h.call_count >= 1   # stale entry must trigger a fresh hash

    def test_size_mismatch_triggers_rehash(self, tmp_path):
        """A cache entry with wrong size is treated as stale and re-hashed."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img    = _make_image(folder / "a.jpg", color=(100, 100, 100))
        st     = img.stat()

        # Correct mtime but wrong size
        stale_rec = _make_file_record(str(img.resolve()),
                                      mtime=st.st_mtime,
                                      size=st.st_size + 9999)
        cache = {str(img.resolve()): stale_rec}

        with patch("scanner._hash_image", wraps=__import__("scanner")._hash_image) as mock_h:
            collect_images(folder, set(), Settings(),
                           library_cache=cache, trust_library=False)

        assert mock_h.call_count >= 1

    def test_trust_library_skips_staleness_check(self, tmp_path):
        """trust_library=True serves stale records without re-hashing."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img    = _make_image(folder / "a.jpg", color=(50, 50, 200))

        # Deliberately stale record
        stale_rec = _make_file_record(str(img.resolve()), mtime=0.0, size=1)
        cache = {str(img.resolve()): stale_rec}

        with patch("scanner._hash_image", side_effect=AssertionError("should not hash")) as mock_h:
            records = collect_images(folder, set(), Settings(),
                                     library_cache=cache, trust_library=True)

        assert mock_h.call_count == 0
        assert len(records) == 1

    def test_multiple_files_partial_cache(self, tmp_path):
        """Files in cache are served without hashing; new files are hashed once."""
        from scanner import collect_images

        folder = tmp_path / "photos"
        img_cached = _make_image(folder / "cached.jpg", color=(255,   0,   0))
        img_new    = _make_image(folder / "new.jpg",    color=(  0, 255,   0))

        # Only cache img_cached
        rec   = _real_record(img_cached)
        cache = {str(img_cached.resolve()): rec}

        hashed_paths: list[Path] = []
        original = __import__("scanner")._hash_image
        def _tracking(path, settings):
            hashed_paths.append(path)
            return original(path, settings)

        with patch("scanner._hash_image", side_effect=_tracking):
            records = collect_images(folder, set(), Settings(),
                                     library_cache=cache, trust_library=False)

        assert len(records) == 2
        # Only the new file should have been hashed
        hashed_resolved = [p.resolve() for p in hashed_paths]
        assert img_new.resolve()    in hashed_resolved
        assert img_cached.resolve() not in hashed_resolved


# ═════════════════════════════════════════════════════════════════════════════
# 3. Subfolder scan reuses parent cache end-to-end
# ═════════════════════════════════════════════════════════════════════════════

class TestSubfolderCacheReuse:
    """
    End-to-end: scan a parent folder, cache its hashes, then scan a subfolder.
    The subfolder scan must reuse the parent cache via load_cache_merged and
    produce identical records without calling _hash_image at all.
    """

    def test_subfolder_reuses_parent_cache(self, tmp_path):
        from scanner import collect_images

        parent = tmp_path / "photos"
        sub    = parent / "vacation"

        # Create images in multiple subdirectories
        img_sub  = _make_image(sub / "beach.jpg",      color=(  0, 100, 200))
        img_other = _make_image(parent / "portrait.jpg", color=(200, 100,   0))

        # ── Step 1: warm cache by scanning parent ─────────────────────────
        lib_dir = tmp_path / "lib"
        lib     = Library.load(lib_dir)

        parent_records = collect_images(parent, set(), Settings())
        assert len(parent_records) == 2

        # Build and save the cache for parent
        parent_cache: dict[str, FileRecord] = {}
        for r in parent_records:
            st = r.path.stat()
            parent_cache[str(r.path.resolve())] = FileRecord.from_image_record(
                r, st_mtime=st.st_mtime)
        lib.save_cache(str(parent.resolve()), parent_cache)
        lib.set_folder(FolderEntry(
            path=str(parent.resolve()), drive_type="fixed",
            volume_serial=None, folder_fingerprint="",
            last_updated="2026-01-01T00:00:00",
            file_count=len(parent_cache),
        ))

        # ── Step 2: scan sub-folder using merged cache ────────────────────
        merged_cache = lib.load_cache_merged(str(sub.resolve()))

        # The merged cache must contain the sub-folder's image
        assert str(img_sub.resolve()) in merged_cache
        # Must NOT contain the parent-only image
        assert str(img_other.resolve()) not in merged_cache

        # ── Step 3: collect_images must not call _hash_image ──────────────
        with patch("scanner._hash_image",
                   side_effect=AssertionError("should use cache")) as mock_h:
            sub_records = collect_images(sub, set(), Settings(),
                                         library_cache=merged_cache,
                                         trust_library=False)

        assert mock_h.call_count == 0
        assert len(sub_records) == 1
        assert sub_records[0].path.resolve() == img_sub.resolve()

    def test_parent_and_subfolder_cache_produce_same_phash(self, tmp_path):
        """Records from parent-merged cache must have identical phash to fresh scan."""
        from scanner import collect_images

        parent = tmp_path / "photos"
        sub    = parent / "vacation"
        img    = _make_image(sub / "beach.jpg", color=(0, 0, 255))

        # ── Fresh scan of sub ─────────────────────────────────────────────
        fresh_records = collect_images(sub, set(), Settings())
        assert len(fresh_records) == 1
        fresh_phash = str(fresh_records[0].phash)

        # ── Cache via parent scan, then use load_cache_merged ─────────────
        lib = Library.load(tmp_path / "lib")
        parent_records = collect_images(parent, set(), Settings())
        parent_cache: dict[str, FileRecord] = {}
        for r in parent_records:
            st = r.path.stat()
            parent_cache[str(r.path.resolve())] = FileRecord.from_image_record(
                r, st_mtime=st.st_mtime)
        lib.save_cache(str(parent.resolve()), parent_cache)
        lib.set_folder(FolderEntry(
            path=str(parent.resolve()), drive_type="fixed",
            volume_serial=None, folder_fingerprint="",
            last_updated="2026-01-01T00:00:00",
            file_count=len(parent_cache),
        ))

        merged_cache = lib.load_cache_merged(str(sub.resolve()))
        cached_records = collect_images(sub, set(), Settings(),
                                        library_cache=merged_cache,
                                        trust_library=False)

        assert len(cached_records) == 1
        cached_phash = str(cached_records[0].phash)
        assert fresh_phash == cached_phash


# ═════════════════════════════════════════════════════════════════════════════
# 4. Browse-mode always reads the cache (no UI-mode gate)
# ═════════════════════════════════════════════════════════════════════════════

class TestBrowseModeAlwaysUsesCache:
    """
    Verify that the cache-loading path in main._worker is unconditional.
    We test this by inspecting the source code of main._worker rather than
    launching a real App (which requires a display and threading).
    """

    def test_worker_loads_cache_without_use_library_gate(self):
        """
        The cache load must be unconditional — never inside an `if use_library:`
        (or similar) guard, or browse mode would silently re-hash everything.

        Since the Stage 2 refactor the load lives in library.load_scan_cache, so
        this walks both sides of that boundary: _worker must call the helper
        ungated, and the helper itself must load ungated.
        """
        import ast, inspect
        import main as _main
        import library as _library

        def _calls_named(tree, name):
            found = []

            class _V(ast.NodeVisitor):
                def visit_Call(self, node):
                    fn = node.func
                    if ((isinstance(fn, ast.Attribute) and fn.attr == name)
                            or (isinstance(fn, ast.Name) and fn.id == name)):
                        found.append(node)
                    self.generic_visit(node)

            _V().visit(tree)
            return found

        def _gated_by_use_library(tree, name):
            gated = []

            class _G(ast.NodeVisitor):
                def __init__(self):
                    self._inside = False

                def visit_If(self, node):
                    was = self._inside
                    if "use_library" in ast.unparse(node.test):
                        self._inside = True
                    self.generic_visit(node)
                    self._inside = was

                def visit_Call(self, node):
                    fn = node.func
                    if self._inside and (
                            (isinstance(fn, ast.Attribute) and fn.attr == name)
                            or (isinstance(fn, ast.Name) and fn.id == name)):
                        gated.append(node)
                    self.generic_visit(node)

            _G().visit(tree)
            return gated

        # ── 1. _worker reaches the cache, ungated ─────────────────────────
        worker_tree = ast.parse(textwrap.dedent(inspect.getsource(_main.App._worker)))
        loads = _calls_named(worker_tree, "load_scan_cache")
        assert len(loads) >= 1, (
            "_worker must load the library cache (via library.load_scan_cache)"
        )
        gated = _gated_by_use_library(worker_tree, "load_scan_cache")
        assert not gated, (
            "the cache load must not be gated behind `if use_library`; "
            f"found {len(gated)} gated call(s)"
        )

        # ── 2. the helper itself loads unconditionally ────────────────────
        helper_tree = ast.parse(
            textwrap.dedent(inspect.getsource(_library.load_scan_cache)))
        merged = _calls_named(helper_tree, "load_cache_merged")
        assert len(merged) >= 1, (
            "library.load_scan_cache must call load_cache_merged"
        )
        assert not _gated_by_use_library(helper_tree, "load_cache_merged"), (
            "load_scan_cache must not re-introduce a use_library gate"
        )

    def test_effective_trust_requires_both_flags(self):
        """
        Staleness checks may be skipped only when Library mode is active AND
        "trust library" is enabled. Browse mode (use=False) must never bypass
        them, or a file edited since the last scan would be matched against a
        stale hash.

        Since Stage 3 this rule lives in LibraryOptions.effective_trust (it was
        previously spelled out separately in each worker). Asserted behaviorally
        across all four combinations rather than by inspecting expression text,
        which is both stronger and refactor-stable.
        """
        from scan_request import LibraryOptions

        assert LibraryOptions(use=True,  trust=True ).effective_trust is True
        assert LibraryOptions(use=True,  trust=False).effective_trust is False
        assert LibraryOptions(use=False, trust=True ).effective_trust is False, (
            "browse mode must not bypass staleness checks even if trust was "
            "left enabled from a previous Library-mode session"
        )
        assert LibraryOptions(use=False, trust=False).effective_trust is False
        assert LibraryOptions().effective_trust is False, "default must be safe"

    def test_worker_sources_trust_from_the_request(self):
        """_worker must derive trust from the request's library options rather
        than re-deriving it locally, so the rule cannot drift between the two
        scan paths again."""
        import ast, inspect
        import main as _main

        tree = ast.parse(textwrap.dedent(inspect.getsource(_main.App._worker)))
        assigns: list[str] = []

        class _V(ast.NodeVisitor):
            def visit_Assign(self, node):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_effective_trust":
                        assigns.append(ast.unparse(node.value))
                self.generic_visit(node)

        _V().visit(tree)
        assert assigns, "_effective_trust assignment not found in _worker"
        assert "effective_trust" in assigns[0], (
            f"_worker must use the shared effective_trust rule, got: {assigns[0]}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 5. Cache write-back after browse scan
# ═════════════════════════════════════════════════════════════════════════════

class TestCacheWriteBack:
    """
    After scanning a folder (browse mode), results are written back to the library
    so the next scan — whether browse or library mode — benefits from the cache.
    """

    def test_update_folder_writes_cache_for_subfolder_reuse(self, tmp_path):
        """
        update_folder on a parent should write a cache that load_cache_merged
        can later serve to a sub-folder scan.
        """
        from library import update_folder

        parent = tmp_path / "photos"
        sub    = parent / "vacation"
        img    = _make_image(sub / "beach.jpg", color=(0, 200, 100))

        lib = Library.load(tmp_path / "lib")
        update_folder(lib, parent, Settings())   # scan + cache parent

        # Now merged cache for sub-folder must contain the image
        merged = lib.load_cache_merged(str(sub.resolve()))
        assert str(img.resolve()) in merged

    def test_second_scan_of_same_browse_folder_uses_cache(self, tmp_path):
        """
        Two consecutive browse scans of the same folder: the second must not
        call _hash_image for unchanged files.
        """
        from library import update_folder
        from scanner import collect_images

        folder = tmp_path / "photos"
        img    = _make_image(folder / "a.jpg", color=(128, 0, 255))

        lib = Library.load(tmp_path / "lib")
        # First scan — warms the cache
        update_folder(lib, folder, Settings())

        # Reload library (simulate what main._worker does)
        lib2   = Library.load(lib._dir)
        cache2 = lib2.load_cache_merged(str(folder.resolve()))

        with patch("scanner._hash_image",
                   side_effect=AssertionError("should use cache")) as mock_h:
            records = collect_images(folder, set(), Settings(),
                                     library_cache=cache2, trust_library=False)

        assert mock_h.call_count == 0
        assert len(records) == 1
