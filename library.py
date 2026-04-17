"""
library.py — Persistent image hash library (scan cache).

Stores per-file hash records keyed by folder so future scans can reuse
them and skip re-hashing unchanged files.

Storage layout (never inside the app install dir — survives reinstalls):
  {library_dir}/
    index.json               ← FolderEntry metadata for every tracked folder
    <folder_id>/
      hashes.json            ← FileRecord cache for that folder
                               (folder_id = first 16 hex chars of SHA-256(abs_path))
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import string
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# ── Version stamps ─────────────────────────────────────────────────────────────

_INDEX_VERSION      = 1
_CACHE_VERSION      = 1
_FINGERPRINT_FILES  = 50   # number of filenames used for folder fingerprint


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class DriveInfo:
    """Drive type and volume identifier for the volume containing a path."""
    drive_type: str                   # "fixed" | "removable" | "network" | "cdrom" | "unknown"
    volume_serial: Optional[int]      # platform-specific volume serial (None if unavailable)


@dataclass
class DriveStatus:
    """Result of checking whether a library folder's drive is accessible."""
    state: str                        # "ok" | "moved" | "missing"
    new_path: Optional[str] = None    # remapped absolute path when state == "moved"


@dataclass
class FolderEntry:
    """Index-level metadata for one tracked folder."""
    path: str                         # resolved absolute path string
    drive_type: str                   # from DriveInfo.drive_type
    volume_serial: Optional[int]      # from DriveInfo.volume_serial
    folder_fingerprint: str           # SHA-256 of first N sorted filenames
    last_updated: str                 # ISO-8601 datetime string
    file_count: int                   # number of cached file records

    @classmethod
    def from_dict(cls, d: dict) -> "FolderEntry":
        return cls(
            path             = d["path"],
            drive_type       = d.get("drive_type", "unknown"),
            volume_serial    = d.get("volume_serial"),
            folder_fingerprint = d.get("folder_fingerprint", ""),
            last_updated     = d.get("last_updated", ""),
            file_count       = d.get("file_count", 0),
        )


@dataclass
class FileRecord:
    """Serialisable per-file hash record stored in the folder cache."""
    path:           str
    mtime:          float      # stat.st_mtime (actual last-write time, not creation time)
    size:           int        # st_size in bytes
    phash:          str        # imagehash hex string
    dhash:          str        # imagehash hex string
    histogram:      list       # 96-float normalised histogram (may be [])
    brightness:     float      # mean pixel brightness 0.0-255.0
    width:          int
    height:         int
    metadata_count: int = 0
    phash_r90:      str = ""   # rotation hashes for rotation-aware comparison
    phash_r180:     str = ""
    phash_r270:     str = ""

    # ── Construction helpers ───────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "FileRecord":
        return cls(
            path           = d["path"],
            mtime          = d["mtime"],
            size           = d["size"],
            phash          = d.get("phash", ""),
            dhash          = d.get("dhash", ""),
            histogram      = d.get("histogram", []),
            brightness     = d.get("brightness", 128.0),
            width          = d.get("width", 0),
            height         = d.get("height", 0),
            metadata_count = d.get("metadata_count", 0),
            phash_r90      = d.get("phash_r90",  ""),
            phash_r180     = d.get("phash_r180", ""),
            phash_r270     = d.get("phash_r270", ""),
        )

    @classmethod
    def from_image_record(cls, rec, st_mtime: Optional[float] = None) -> "FileRecord":
        """Convert a scanner.ImageRecord to a serialisable FileRecord.

        Args:
            rec:      The ImageRecord produced by the scanner.
            st_mtime: The raw ``stat.st_mtime`` of the file.  Pass this
                      explicitly so the library uses the actual modification
                      time for cache-invalidation rather than the scanner's
                      ``min(st_mtime, st_ctime)`` formula, which on Windows
                      returns the creation time and never changes on overwrite.
        """
        return cls(
            path           = str(rec.path),
            mtime          = st_mtime if st_mtime is not None else rec.mtime,
            size           = rec.file_size,
            phash          = str(rec.phash),
            dhash          = str(rec.dhash),
            histogram      = list(rec.histogram),
            brightness     = rec.brightness,
            width          = rec.width,
            height         = rec.height,
            metadata_count = rec.metadata_count,
            phash_r90      = str(rec.phash_r90)  if rec.phash_r90  is not None else "",
            phash_r180     = str(rec.phash_r180) if rec.phash_r180 is not None else "",
            phash_r270     = str(rec.phash_r270) if rec.phash_r270 is not None else "",
        )

    # ── Conversion back to scanner type ───────────────────────────────────

    def to_image_record(self):
        """Reconstruct a scanner.ImageRecord from this cached record."""
        from scanner import ImageRecord
        import imagehash as _ih
        import numpy as _np

        _zero = _ih.ImageHash(_np.zeros((8, 8), dtype=bool))
        ph = _ih.hex_to_hash(self.phash) if self.phash else _zero
        dh = _ih.hex_to_hash(self.dhash) if self.dhash else _zero

        return ImageRecord(
            path           = Path(self.path),
            width          = self.width,
            height         = self.height,
            file_size      = self.size,
            phash          = ph,
            dhash          = dh,
            mtime          = self.mtime,
            brightness     = self.brightness,
            histogram      = list(self.histogram),
            metadata_count = self.metadata_count,
            phash_r90  = _ih.hex_to_hash(self.phash_r90)  if self.phash_r90  else None,
            phash_r180 = _ih.hex_to_hash(self.phash_r180) if self.phash_r180 else None,
            phash_r270 = _ih.hex_to_hash(self.phash_r270) if self.phash_r270 else None,
        )

    # ── Staleness check ────────────────────────────────────────────────────

    def is_stale(self, path: Path) -> bool:
        """Return True if the file has changed or been deleted since caching.

        Uses ``stat.st_mtime`` (actual last-write time) rather than the
        scanner's ``min(st_mtime, st_ctime)`` formula so that overwritten
        files are always detected as changed on Windows.
        """
        try:
            stat = path.stat()
            return stat.st_mtime != self.mtime or stat.st_size != self.size
        except (FileNotFoundError, OSError):
            return True


# ── Library directory ──────────────────────────────────────────────────────────

def get_library_dir() -> Path:
    """Return the OS-appropriate user-data directory for library storage."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Katador" / "ImageDeduper" / "library"


# ── Drive utilities ────────────────────────────────────────────────────────────

def get_drive_info(path: Path) -> DriveInfo:
    """Return drive type and volume serial for the volume that contains path."""
    if sys.platform == "win32":
        return _drive_info_win(path)
    return _drive_info_posix(path)


def _drive_info_win(path: Path) -> DriveInfo:
    import ctypes
    import ctypes.wintypes
    _TYPE_MAP = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}
    try:
        root = str(path.resolve().anchor)
        dt   = ctypes.windll.kernel32.GetDriveTypeW(root)
        drive_type = _TYPE_MAP.get(dt, "unknown")

        serial = ctypes.wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, ctypes.byref(serial), None, None, None, 0
        )
        volume_serial = serial.value if ok else None
    except Exception:
        drive_type, volume_serial = "unknown", None
    return DriveInfo(drive_type=drive_type, volume_serial=volume_serial)


def _drive_info_posix(path: Path) -> DriveInfo:
    try:
        st = os.stat(path.resolve().anchor)
        return DriveInfo(drive_type="fixed", volume_serial=st.st_dev)
    except Exception:
        return DriveInfo(drive_type="unknown", volume_serial=None)


def find_drive_by_serial(serial: int) -> Optional[str]:
    """Search every Windows drive letter for one whose volume serial matches.

    Returns the root path (e.g. ``"F:\\\\"``), or ``None`` if not found or
    not on Windows.
    """
    if sys.platform != "win32" or not serial:
        return None
    import ctypes
    import ctypes.wintypes
    try:
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            s  = ctypes.wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                root, None, 0, ctypes.byref(s), None, None, None, 0
            )
            if ok and s.value == serial:
                return root
    except Exception:
        pass
    return None


# ── Folder fingerprint ─────────────────────────────────────────────────────────

def compute_folder_fingerprint(folder: Path, max_files: int = _FINGERPRINT_FILES) -> str:
    """SHA-256 of the sorted names of up to *max_files* direct files in *folder*.

    Used to verify that a re-mounted removable drive still contains the
    expected folder content (not a different drive that happens to have the
    same letter or serial).
    """
    try:
        names = sorted(p.name for p in folder.iterdir() if p.is_file())[:max_files]
    except (PermissionError, OSError):
        names = []
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


# ── Library class ──────────────────────────────────────────────────────────────

class Library:
    """Manages the persistent folder index and per-folder file-hash caches."""

    def __init__(self, library_dir: Path) -> None:
        self._dir   = library_dir
        self._index: dict[str, FolderEntry] = {}   # key = resolved absolute path

    # ── Loading ────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, library_dir: Optional[Path] = None) -> "Library":
        """Load (or initialise) the library from *library_dir* (defaults to
        :func:`get_library_dir`)."""
        lib = cls(library_dir or get_library_dir())
        lib._dir.mkdir(parents=True, exist_ok=True)
        index_path = lib._dir / "index.json"
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                for path_str, entry_data in data.get("folders", {}).items():
                    lib._index[path_str] = FolderEntry.from_dict(entry_data)
            except Exception:
                pass   # corrupt index — start fresh
        return lib

    # ── Saving ────────────────────────────────────────────────────────────

    def save(self) -> None:
        """Persist the folder index to disk."""
        self._dir.mkdir(parents=True, exist_ok=True)
        index_path = self._dir / "index.json"
        data = {
            "version": _INDEX_VERSION,
            "folders": {k: asdict(v) for k, v in self._index.items()},
        }
        index_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _norm(path: str) -> str:
        """Normalise a path to a consistent resolved absolute string."""
        return str(Path(path).resolve())

    def _folder_id(self, norm_path: str) -> str:
        """Short stable identifier for a folder, used as the cache sub-directory name."""
        return hashlib.sha256(norm_path.encode("utf-8")).hexdigest()[:16]

    def _cache_path(self, norm_path: str) -> Path:
        return self._dir / self._folder_id(norm_path) / "hashes.json"

    # ── Folder entry CRUD ──────────────────────────────────────────────────

    @property
    def folders(self) -> list[FolderEntry]:
        """All tracked folder entries."""
        return list(self._index.values())

    def get_folder(self, path: str) -> Optional[FolderEntry]:
        return self._index.get(self._norm(path))

    def set_folder(self, entry: FolderEntry) -> None:
        """Insert or replace a folder entry and save the index.

        When adding a folder, any existing entries that are strict sub-folders
        of the new path are removed automatically — they are now fully covered
        by the parent and would appear as duplicates in the UI.
        """
        key = self._norm(entry.path)
        entry.path = key
        key_with_sep = key + os.sep
        redundant = [k for k in self._index if k != key and k.startswith(key_with_sep)]
        for r in redundant:
            self._index.pop(r, None)
        self._index[key] = entry
        self.save()

    def remove_folder(self, path: str) -> None:
        """Remove a folder entry and delete its on-disk cache."""
        key = self._norm(path)
        self._index.pop(key, None)
        cache_dir = self._cache_path(key).parent
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        self.save()

    # ── File-record cache ──────────────────────────────────────────────────

    def load_cache(self, path: str) -> dict[str, FileRecord]:
        """Load the file-record cache for *path*. Returns ``{}`` if not found."""
        cache_path = self._cache_path(self._norm(path))
        if not cache_path.exists():
            return {}
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return {k: FileRecord.from_dict(v) for k, v in data.get("files", {}).items()}
        except Exception:
            return {}

    def load_cache_merged(self, folder_path: str) -> "dict[str, FileRecord]":
        """Load file-record cache for *folder_path*, pulling in records from any
        tracked ancestor folder whose recursive cache covers files inside
        *folder_path*.

        Use this instead of :meth:`load_cache` before a scan so that scanning a
        sub-folder (e.g. ``C:\\Photos\\Vacation``) automatically benefits from a
        parent folder's cache (``C:\\Photos``) even if the sub-folder was never
        tracked separately.

        Priority rule: exact-folder records override ancestor records, so a
        previously cached sub-folder scan always wins over a stale parent entry.
        """
        norm = self._norm(folder_path)
        norm_with_sep = norm + os.sep
        merged: "dict[str, FileRecord]" = {}

        # Pull matching records from every tracked ancestor
        for tracked_path in list(self._index):
            if tracked_path == norm:
                continue  # handled by exact load below
            # Is tracked_path a proper ancestor of norm?
            if norm.startswith(tracked_path + os.sep):
                for file_key, record in self.load_cache(tracked_path).items():
                    # Keep only files that live inside our target folder
                    if file_key.startswith(norm_with_sep):
                        merged[file_key] = record

        # Exact folder cache overrides ancestor records
        merged.update(self.load_cache(norm))
        return merged

    def save_cache(self, path: str, cache: dict[str, FileRecord]) -> None:
        """Persist the file-record cache for *path*."""
        norm       = self._norm(path)
        cache_path = self._cache_path(norm)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": _CACHE_VERSION,
            "files":   {k: asdict(v) for k, v in cache.items()},
        }
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # ── Drive status ───────────────────────────────────────────────────────

    def check_drive_status(self, entry: FolderEntry) -> DriveStatus:
        """Check whether a folder's drive is accessible, moved, or missing.

        For removable / network drives the method first tries to find the
        volume by serial number across all drive letters before giving up.
        Fixed-drive folders are considered simply missing when not found.
        Removable / network entries are **never** auto-deleted.
        """
        p = Path(entry.path)
        if p.exists():
            return DriveStatus(state="ok")

        # Removable / network: try to locate by volume serial
        if entry.drive_type in ("removable", "network") and entry.volume_serial:
            new_root = find_drive_by_serial(entry.volume_serial)
            if new_root:
                old_root = p.anchor
                rel      = str(p)[len(old_root):]
                new_path = str(Path(new_root) / rel)
                return DriveStatus(state="moved", new_path=new_path)
            return DriveStatus(state="missing")

        return DriveStatus(state="missing")

    # ── Path remapping ─────────────────────────────────────────────────────

    def update_path(self, old_path: str, new_path: str) -> None:
        """Remap a folder entry from *old_path* to *new_path*.

        Also moves the on-disk cache directory so no hashes are lost.
        Call this after the user confirms a drive-letter change.
        """
        old_key = self._norm(old_path)
        new_key = self._norm(new_path)
        entry   = self._index.pop(old_key, None)
        if entry is None:
            return

        old_cache_dir = self._cache_path(old_key).parent
        new_cache_dir = self._cache_path(new_key).parent

        if old_cache_dir.exists() and old_cache_dir != new_cache_dir:
            new_cache_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_cache_dir), str(new_cache_dir))

        entry.path      = new_key
        self._index[new_key] = entry
        self.save()

    # ── Fingerprint verification ───────────────────────────────────────────

    def verify_fingerprint(self, path: str) -> bool:
        """Return ``True`` if the folder at *path* still matches its stored fingerprint."""
        entry = self.get_folder(path)
        if not entry or not entry.folder_fingerprint:
            return False
        try:
            current = compute_folder_fingerprint(Path(self._norm(path)))
            return current == entry.folder_fingerprint
        except Exception:
            return False


# ── update_folder ──────────────────────────────────────────────────────────────

def update_folder(
    library:     Library,
    folder_path: "str | Path",
    settings,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
    stop_flag:   Optional[list] = None,
) -> FolderEntry:
    """Hash all images in *folder_path*, reusing cached records for unchanged files.

    Args:
        library:     :class:`Library` instance to read from / write to.
        folder_path: Folder to scan and hash.
        settings:    Scanner :class:`~config.Settings` (controls which hash
                     guards are active, ``recursive``, etc.).
        progress_cb: Optional ``(filename, index, total)`` callback per file.
        stop_flag:   Optional ``[bool]``; set ``stop_flag[0] = True`` to abort.

    Returns:
        Updated :class:`FolderEntry` written to the library index.

    Unchanged files (matching mtime **and** size) are served from cache.
    Deleted files are silently dropped from the cache.
    New or modified files are hashed fresh.

    Parallelism: uses the same drive-aware thread count as the main scan
    (``scanner._resolve_thread_count``) — HDDs are limited to
    ``settings.hdd_thread_cap`` concurrent readers by default so library
    updates don't seek-thrash a spinning disk.  Stop is honoured both at the
    submit loop and inside each worker (via early-exit flag checks).
    """
    from scanner import (
        IMAGE_EXTENSIONS, RAW_EXTENSIONS, _hash_image, _hash_raw,
        _resolve_thread_count,
    )

    folder   = Path(folder_path).resolve()
    path_str = str(folder)
    cache    = library.load_cache(path_str)

    # Collect candidate image files (JPEG/PNG/… always; RAW only if enabled)
    want_raw = getattr(settings, "use_rawpy", False)
    exts_wanted = IMAGE_EXTENSIONS | (RAW_EXTENSIONS if want_raw else set())

    try:
        if getattr(settings, "recursive", True):
            all_files = [p for p in folder.rglob("*")
                         if p.is_file() and p.suffix.lower() in exts_wanted]
        else:
            all_files = [p for p in folder.iterdir()
                         if p.is_file() and p.suffix.lower() in exts_wanted]
    except (PermissionError, OSError):
        all_files = []

    all_files.sort()
    new_cache: dict[str, FileRecord] = {}

    # ── figure out which files need hashing (cache hits are handled inline) ──
    to_hash: list[tuple[int, Path, float]] = []   # (original idx, path, st_mtime)
    for i, file_path in enumerate(all_files):
        if stop_flag and stop_flag[0]:
            break
        file_key = str(file_path)
        try:
            stat     = file_path.stat()
            st_mtime = stat.st_mtime
            size     = stat.st_size
        except OSError:
            continue

        existing = cache.get(file_key)
        if existing is not None and existing.mtime == st_mtime and existing.size == size:
            new_cache[file_key] = existing
            continue
        to_hash.append((i, file_path, st_mtime))

    # ── parallel hashing of changed/new files ───────────────────────────────
    n_threads = _resolve_thread_count(settings, folder)
    total_to_hash = len(to_hash)

    def _hash_one_lib(job):
        idx, fp, st_mtime_ = job
        if stop_flag and stop_flag[0]:
            return idx, fp, st_mtime_, None
        try:
            ext = fp.suffix.lower()
            if ext in RAW_EXTENSIONS and want_raw:
                img_rec = _hash_raw(fp, settings)
            else:
                img_rec = _hash_image(fp, settings)
            return idx, fp, st_mtime_, img_rec
        except Exception:
            return idx, fp, st_mtime_, None

    if n_threads > 1 and total_to_hash > 0:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        completed = 0
        pool = ThreadPoolExecutor(max_workers=n_threads)
        try:
            futures = [pool.submit(_hash_one_lib, job) for job in to_hash]
            interrupted = False
            for fut in as_completed(futures):
                if stop_flag and stop_flag[0]:
                    interrupted = True
                    break
                try:
                    idx, fp, st_mtime_, img_rec = fut.result()
                except Exception:
                    completed += 1
                    continue
                completed += 1
                if progress_cb:
                    progress_cb(fp.name, completed, total_to_hash)
                if img_rec is not None:
                    new_cache[str(fp)] = FileRecord.from_image_record(img_rec, st_mtime=st_mtime_)
            if interrupted:
                pool.shutdown(wait=False, cancel_futures=True)
        finally:
            pool.shutdown(wait=True)
    else:
        for ji, (_, fp, st_mtime_) in enumerate(to_hash):
            if stop_flag and stop_flag[0]:
                break
            if progress_cb:
                progress_cb(fp.name, ji, total_to_hash)
            try:
                ext = fp.suffix.lower()
                if ext in RAW_EXTENSIONS and want_raw:
                    img_rec = _hash_raw(fp, settings)
                else:
                    img_rec = _hash_image(fp, settings)
                if img_rec is not None:
                    new_cache[str(fp)] = FileRecord.from_image_record(img_rec, st_mtime=st_mtime_)
            except Exception:
                pass

    library.save_cache(path_str, new_cache)

    fingerprint = compute_folder_fingerprint(folder)
    drive_info  = get_drive_info(folder)
    entry = FolderEntry(
        path               = path_str,
        drive_type         = drive_info.drive_type,
        volume_serial      = drive_info.volume_serial,
        folder_fingerprint = fingerprint,
        last_updated       = datetime.now().isoformat(),
        file_count         = len(new_cache),
    )
    library.set_folder(entry)
    return entry
