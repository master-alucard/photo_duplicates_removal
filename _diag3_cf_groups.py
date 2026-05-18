"""
_diag3_cf_groups.py -- verify CF groups work with postprocess RAW hashing
"""
from __future__ import annotations
import sys, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Settings
from scanner import _hash_image, _hash_raw, _can_be_similar, RAW_EXTENSIONS, _CF_BASE_THRESHOLD
import imagehash
from PIL import Image

CF_GROUPS = Path(r"E:\MEDIA\test\calibration_cf\groups")
CF_SINGLES = Path(r"E:\MEDIA\test\calibration_cf\singles")

def hash_file(p: Path, settings: Settings):
    ext = p.suffix.lower()
    if ext in RAW_EXTENSIONS:
        return _hash_raw(p, settings)
    else:
        return _hash_image(p, settings)

def main():
    s_emb = Settings()
    s_emb.use_rawpy = True
    s_emb.keep_all_formats = False
    s_emb.raw_use_embedded_thumb = True
    
    s_pp = Settings()
    s_pp.use_rawpy = True
    s_pp.keep_all_formats = False
    s_pp.raw_use_embedded_thumb = False
    
    cf_factor = getattr(s_pp, "cross_format_threshold_factor", 6.0)
    cf_abs_thr = int(_CF_BASE_THRESHOLD * cf_factor)
    
    print(f"CF abs threshold: {cf_abs_thr}")
    print()
    
    # Test first 5 CF groups with both modes
    print("=== CF GROUPS (postprocess vs embedded) ===")
    for i in range(1, 11):
        sname = f"set_{i:03d}"
        d = CF_GROUPS / sname
        if not d.exists():
            continue
        files = sorted(d.iterdir())
        raws = [f for f in files if f.suffix.lower() in RAW_EXTENSIONS]
        jpgs = [f for f in files if f.suffix.lower() not in RAW_EXTENSIONS]
        
        for rf in raws:
            for jf in jpgs:
                r_pp = hash_file(rf, s_pp)
                r_jpg = hash_file(jf, s_pp)
                r_emb = hash_file(rf, s_emb)
                
                dist_pp = r_pp.phash - r_jpg.phash if r_pp and r_jpg else -1
                dist_emb = r_emb.phash - r_jpg.phash if r_emb and r_jpg else -1
                
                sim_pp = _can_be_similar(r_pp, r_jpg, s_pp) if r_pp and r_jpg else False
                sim_emb = _can_be_similar(r_emb, r_jpg, s_emb) if r_emb and r_jpg else False
                
                status_pp = "PASS" if dist_pp <= cf_abs_thr else "FAIL"
                status_emb = "PASS" if dist_emb <= cf_abs_thr else "FAIL"
                print(f"  {sname}: {rf.name[:30]} <-> {jf.name[:30]}")
                print(f"    postprocess: dist={dist_pp:2d} {status_pp}  can_sim={sim_pp}")
                print(f"    embedded:    dist={dist_emb:2d} {status_emb}  can_sim={sim_emb}")
    
    print()
    print("=== CF SINGLES cross-check (should NOT group) ===")
    # Singles: CR2s and JPEGs that should not be grouped together
    singles_files = sorted(CF_SINGLES.iterdir())
    raws_s = [f for f in singles_files if f.suffix.lower() in RAW_EXTENSIONS]
    jpgs_s = [f for f in singles_files if f.suffix.lower() not in RAW_EXTENSIONS]
    
    for rf in raws_s:
        for jf in jpgs_s:
            r_pp = hash_file(rf, s_pp)
            r_jpg = hash_file(jf, s_pp)
            r_emb = hash_file(rf, s_emb)
            
            dist_pp = r_pp.phash - r_jpg.phash if r_pp and r_jpg else -1
            dist_emb = r_emb.phash - r_jpg.phash if r_emb and r_jpg else -1
            
            sim_pp = _can_be_similar(r_pp, r_jpg, s_pp) if r_pp and r_jpg else False
            sim_emb = _can_be_similar(r_emb, r_jpg, s_emb) if r_emb and r_jpg else False
            
            print(f"  {rf.name[:35]} <-> {jf.name[:35]}")
            print(f"    postprocess: dist={dist_pp:2d}  can_sim={sim_pp}")
            print(f"    embedded:    dist={dist_emb:2d}  can_sim={sim_emb}")

if __name__ == "__main__":
    main()
