# Image Deduper

Find and safely remove duplicate photos from your library.
Built with Python + Tkinter — runs entirely offline on your PC.

**Publisher:** [Katador.net](https://katador.net)
**Contact:** office@katador.net
**License:** MIT

---

## Table of contents

1. [Install on Windows (end-user)](#install-on-windows-end-user)
2. [Run from source (developers)](#run-from-source-developers)
3. [Requirements](#requirements)
4. [Project structure](#project-structure)
5. [How it works](#how-it-works)

---

## Install on Windows (end-user)

1. Go to the [Releases page](https://github.com/master-alucard/photo_duplicates_removal/releases).
2. Download the latest **`ImageDeduper-Setup-x.x.x.exe`**.
3. Run the installer — **no admin rights required** (installs per-user).
4. Launch *Image Deduper* from the Start menu or the desktop shortcut.

> **Auto-update:** the app checks GitHub for new releases on startup.
> You can turn this off in the **About** tab.

---

## Run from source (developers)

### 1. Prerequisites

| Tool | Minimum version | Download |
|------|----------------|---------|
| Python | 3.11 or 3.12 | https://python.org/downloads |
| Git | any | https://git-scm.com |

> Python 3.13+ is **not recommended** if you plan to build the installer —
> PyInstaller does not yet support 3.13/3.14.
> For running from source only, 3.13 works fine.

### 2. Clone the repository

```bash
git clone https://github.com/master-alucard/photo_duplicates_removal.git
cd photo_duplicates_removal
```

### 3. Create a virtual environment (recommended)

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Launch the app

```bash
python main.py
```

---

## Requirements

All required packages are listed in `requirements.txt`:

```
Pillow>=10.0.0
imagehash>=4.3.1
piexif>=1.1.3
PyWavelets
numpy
scipy
rawpy>=0.18.0
```

rawpy is now a standard dependency — it includes the pre-compiled
**LibRaw DLL** so no extra setup is required on Windows.

Install everything at once:

```bash
pip install -r requirements.txt
```

### Verify the installation

```bash
python -c "
from PIL import Image
import imagehash, piexif, pywt, rawpy
print('All dependencies OK — rawpy', rawpy.__version__)
"
```

You should see `All dependencies OK — rawpy x.x.x`.
If you get a `ModuleNotFoundError`, reinstall requirements:

```bash
pip install --upgrade -r requirements.txt
```

> **RAW files:** rawpy supports `.CR2`, `.NEF`, `.ARW`, `.DNG`, and 200+ other
> RAW formats. Enable it in the **Settings → RAW Files** section after launch.

---

## Project structure

```
photo_duplicates_removal/
├── main.py                  # Application entry point & main UI
├── report_viewer.py         # Duplicate review window
├── scanner.py               # Image discovery & duplicate detection
├── mover.py                 # File move / trash / revert operations
├── reporter.py              # HTML report generation
├── calibration_window.py    # Calibration UI (panel + window wrapper)
├── config.py                # Settings dataclass & persistence
├── about_tab.py             # About page (version, update check, privacy)
├── progress_tracker.py      # Phase-based progress tracking
├── info_texts.py            # Tooltip / help text content
├── requirements.txt         # Python dependencies
├── image_deduper.spec       # PyInstaller bundle recipe
├── installer.iss            # Inno Setup installer recipe
└── .github/
    └── workflows/
        └── build.yml        # GitHub Actions CI/CD pipeline
```

---

## How it works

1. **Scan** — discovers all image files in the source folder (optionally recursive).
2. **Hash** — computes perceptual hashes (pHash + optional dHash) for each image.
3. **Compare** — groups images whose hashes are within the similarity threshold.
4. **Review** — opens the result viewer where you inspect each group, confirm
   correct matches, mark wrong ones, and select which duplicates to trash.
5. **Trash** — moves only the selected duplicate files to a `trash/` subfolder
   inside your output folder. Originals are never touched.
6. **Revert** — every operation is logged; you can restore files at any time
   using the *Revert* buttons in the review window.
7. **Calibrate** — the built-in calibration tool analyses your specific photo
   library to find the optimal threshold and ratio settings automatically.

### Privacy

All processing happens **locally on your device**. No images, paths, or
metadata are ever sent to any server. The only optional network request is
a version check against the GitHub API on startup (can be disabled in the
**About** tab).

---

## Contact

| | |
|---|---|
| General / support | office@katador.net |
| Privacy questions | privacy@katador.net |
| Bug reports | [GitHub Issues](https://github.com/master-alucard/photo_duplicates_removal/issues) |
