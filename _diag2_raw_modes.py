"""
_diag2_raw_modes.py -- test different RAW hashing modes for set_032-035
Run from repo root: python _diag2_raw_modes.py
"""
from __future__ import annotations
import sys, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Settings
from scanner import _hash_image, _hash_raw, RAW_EXTENSIONS, _CF_BASE_THRESHOLD
import imagehash
from PIL import Image

RAW_GROUPS = Path(r"E:\MEDIA\test\Calibrate raw\groups")

PAIRS = [
    ("set_032", "Canon EOS M100 6000x4000_009271.cr2", "file 6000x4000_178665.jpg"),
    ("set_033", "Canon EOS M100 6000x4000_011233.cr2", "file 6000x4000_199933.jpg"),
    ("set_034", "Canon EOS M100 6000x4000_011235.cr2", "file 6000x4000_199951.jpg"),
    ("set_035", "Canon EOS M100 6000x4000_011236.cr2", "file 6000x4000_199960.jpg"),
]

def hash_raw_with_mode(path: Path, use_embedded: bool):
    """Hash a RAW file with specific thumb mode."""
    s = Settings()
    s.use_rawpy = True
    s.raw_use_embedded_thumb = use_embedded
    return _hash_raw(path, s)

def hash_raw_postprocess(path: Path):
    """Hash using rawpy postprocess (no embedded thumb)."""
    import rawpy
    with rawpy.imread(str(path)) as raw:
        sizes = raw.sizes
        full_w, full_h = int(sizes.width), int(sizes.height)
        rgb_array = raw.postprocess(use_camera_wb=True, output_bps=8)
        img = Image.fromarray(rgb_array)
    img_w = min(1024, img.size[0])
    img.thumbnail((img_w, img_w), Image.LANCZOS)
    ph = imagehash.phash(img)
    return ph, full_w, full_h

def hash_raw_embedded(path: Path):
    """Hash using embedded JPEG thumbnail only."""
    import rawpy
    with rawpy.imread(str(path)) as raw:
        sizes = raw.sizes
        full_w, full_h = int(sizes.width), int(sizes.height)
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                img.load()
                print(f"    embedded thumb size: {img.size}")
            else:
                img = Image.fromarray(thumb.data)
        except Exception as e:
            print(f"    thumb extraction failed: {e}")
            return None, full_w, full_h
    img.thumbnail((1024, 1024), Image.LANCZOS)
    ph = imagehash.phash(img)
    return ph, full_w, full_h

def main():
    for sname, raw_name, jpg_name in PAIRS:
        raw_path = RAW_GROUPS / sname / raw_name
        jpg_path = RAW_GROUPS / sname / jpg_name
        
        print(f"\n{'='*60}")
        print(f"{sname}: {raw_name} <-> {jpg_name}")
        
        # Hash JPEG
        s = Settings()
        s.use_rawpy = True
        rec_jpg = _hash_image(jpg_path, s)
        print(f"\n  JPEG phash: {rec_jpg.phash}")
        
        # RAW with embedded thumb
        print(f"\n  RAW (embedded thumb):")
        ph_emb, fw, fh = hash_raw_embedded(raw_path)
        if ph_emb is not None:
            dist = rec_jpg.phash - ph_emb
            print(f"    phash: {ph_emb}  dist_to_jpeg={dist}")
        
        # RAW with postprocess
        print(f"\n  RAW (postprocess/demosaic):")
        ph_pp, fw, fh = hash_raw_postprocess(raw_path)
        dist_pp = rec_jpg.phash - ph_pp
        print(f"    phash: {ph_pp}  dist_to_jpeg={dist_pp}")
        
        print(f"\n  JPEG phash: {rec_jpg.phash}")
        print(f"  Summary: embedded_dist={rec_jpg.phash - ph_emb if ph_emb else 'N/A'}  postprocess_dist={dist_pp}")

if __name__ == "__main__":
    main()
