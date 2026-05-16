# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Image Deduper** — a Windows desktop application for finding and removing duplicate photos. Tkinter + ttkbootstrap UI, 100% offline processing, distributed as a standalone `.exe` installer.

- **Python**: 3.11–3.12 (PyInstaller requires ≤3.12; 3.13 works for dev but not builds)
- **License**: MIT | **Publisher**: Katador.net

## Commands

### Run from source
```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### Tests
```bash
python -m pytest tests/ -q          # full suite (~3.5 min, ~214 tests)
python -m pytest tests/test_app.py  # single module
python -m pytest tests/ -v          # verbose
```

No linting tools are configured (no black/flake8/mypy in requirements).

### Build standalone installer
```bash
cd installer && build_installer.bat   # requires Inno Setup 6
# Output: installer/installer_output/ImageDeduper-Setup-x.x.x.exe
```

### Release via CI/CD
```bash
git tag v1.X.Y && git push origin v1.X.Y
# GitHub Actions builds EXE + installer and creates a GitHub Release automatically
```

**On every release, two files must be updated manually:**
- `about_tab.py` → `APP_VERSION = "X.Y.Z"`
- `installer/installer.iss` → `#define AppVersion "X.Y.Z"`

## Architecture

### Core pipeline: Scan → Hash → Group → Review → Trash

1. **Discovery** (`scanner.collect_images`): Walk folder tree, filter by extension.
2. **Hashing** (`scanner._hash_image` / `_hash_raw`): Parallel `ThreadPoolExecutor` (drive-aware thread cap). Computes pHash, dHash, histogram, brightness, and rotation hashes.
3. **Grouping** (`scanner.find_groups`): Union-find via BK-tree (n > 200) or brute O(n²). Runaway mega-groups are split via medoid-split safety net (`_split_oversized_bucket`). Groups are classified (exact-dup / series / cross-format / original+preview).
4. **Review** (`report_viewer.ReportViewer`): Paginated cards (default 20/page) with checkboxes for trash selection.
5. **Move** (`mover.move_groups`): Trash selected files to `<output>/trash/`, log ops to `operations_log.json`.
6. **Report** (`reporter.generate_report`): HTML summary.

### Module responsibilities

| File | Role |
|------|------|
| `main.py` | Tkinter `App` class, all tabs, scan orchestration |
| `scanner.py` | Image discovery, hashing, duplicate grouping (`ImageRecord`, `DuplicateGroup`) |
| `report_viewer.py` | Paginated group display + selection UI (`ReportViewer`, `GroupCard`) |
| `library.py` | Persistent hash cache (`Library`, `FolderEntry`) |
| `library_tab.py` | Library management UI, folder updates |
| `mover.py` | File operations: trash, revert, organize by date |
| `calibrator.py` | Automated threshold calibration (not shipped to users) |
| `config.py` | `Settings` dataclass + JSON persistence (`settings.json`) |
| `theme.py` | ttkbootstrap dark/light mode + Material Design 3 tokens |
| `progress_tracker.py` | Phase-based scan progress (`PhaseTracker`) |
| `metadata.py` | EXIF/date extraction (dates from EXIF tags and filenames) |

### Key settings (in `config.py`)

- `threshold: int = 2` — pHash Hamming distance for exact duplicates
- `preview_ratio: float = 0.90` — dimension ratio to classify as preview/thumbnail
- `cross_format_threshold_factor: float = 6.0` — RAW vs JPEG tolerance (effective threshold = `threshold × factor`)
- `max_group_size: int = 50` — triggers medoid-split safety net
- `keep_strategy: str = "pixels"` — `"pixels"` (larger res wins) or `"oldest"` (earlier mtime wins)
- `scan_threads: int = 0` — 0 = auto (HDD cap: 2 threads, SSD: unlimited)
- `raw_use_embedded_thumb: bool = False` — opt-in RAW fast path (~6× speedup, but invalidates existing cache)
- `dry_run: bool = True` — no actual file moves on startup

### Test infrastructure (`tests/conftest.py`)

All tests share a single session-wide withdrawn Tk root. ttkbootstrap image caching requires this — do not create new `Tk()` roots in tests. The conftest monkey-patches `Toplevel.__init__`, `deiconify`, and `grab_set` so no windows appear during pytest.

### RAW file support

`rawpy` decodes RAW sensor formats (CR2, NEF, ARW, DNG, etc.) and bundles LibRaw as a pre-compiled DLL — no extra setup needed. Enabled via `use_rawpy: bool = False` in settings.

### Sidecar files (`.xmp`, `.aae`)

Intentionally NOT moved when duplicates are trashed, to avoid losing RAW edit metadata.
