"""
make_ico.py — Convert icon_source.png → app.ico (multi-resolution)

Run from the assets/ folder:
    python make_ico.py
"""
from pathlib import Path
from PIL import Image

SRC  = Path(__file__).parent / "icon_source.png"
DEST = Path(__file__).parent / "app.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

img = Image.open(SRC).convert("RGBA")

frames = [img.resize((s, s), Image.LANCZOS) for s in SIZES]

frames[0].save(
    DEST,
    format="ICO",
    sizes=[(s, s) for s in SIZES],
    append_images=frames[1:],
)

print(f"Saved {DEST}  ({', '.join(str(s) for s in SIZES)} px)")
