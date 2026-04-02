# -*- mode: python ; coding: utf-8 -*-
# ── PyInstaller spec for Image Deduper ───────────────────────────────────────
# Build with:  pyinstaller image_deduper.spec
# Requires Python 3.11 or 3.12 (PyInstaller does not support 3.13+ yet)
# ─────────────────────────────────────────────────────────────────────────────

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# ── rawpy: collect the LibRaw DLL that ships inside the rawpy wheel ───────────
# collect_dynamic_libs finds libraw.dll (and any other .dll/.so rawpy needs)
# collect_data_files picks up any non-Python data files in the rawpy package
try:
    rawpy_binaries = collect_dynamic_libs('rawpy')
    rawpy_datas    = collect_data_files('rawpy')
except Exception:
    rawpy_binaries = []
    rawpy_datas    = []

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=rawpy_binaries,
    datas=[
        *rawpy_datas,
        ('assets/app.ico', 'assets'),   # app icon used by the About tab / installer
    ],
    hiddenimports=[
        # Pillow internals that PyInstaller misses on some builds
        'PIL._tkinter_finder',
        'PIL.ImageTk',
        # imagehash and its hash backends
        'imagehash',
        # PyWavelets — required by imagehash.whash(); easy to miss
        'pywt',
        'pywt._extensions._cwt',
        # piexif
        'piexif',
        # rawpy Python extension + C backend
        'rawpy',
        'rawpy._rawpy',
        # scipy / numpy sub-modules sometimes missed by the auto-analyser
        'scipy.special._ufuncs_cxx',
        'scipy.linalg.cython_blas',
        'scipy.linalg.cython_lapack',
        'scipy._lib.messagestream',
        'numpy.core._dtype_ctypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the bundle small — exclude things we never use
        'matplotlib',
        'IPython',
        'jupyter',
        'notebook',
        'pandas',
        'sklearn',
        'tensorflow',
        'torch',
        'cv2',
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ImageDeduper',
    icon='assets/app.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX compression causes Windows App Control to block DLLs
    console=False,              # no black console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,                  # disabled — UPX-packed DLLs are blocked by Windows App Control
    name='ImageDeduper',
)
