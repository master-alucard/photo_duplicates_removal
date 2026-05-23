"""
_video_match_diag.py -- Diagnostic script for video duplicate matching.

Scans E:/MEDIA/test/video and shows, for every pair:
  - file sizes
  - thumbnail pHash distance (single-frame, current logic)
  - duration (via bundled ffmpeg)
  - multi-frame fingerprint distances (new content-based approach)
  - whether current size-first logic groups them
  - whether content-based logic would group them

Run:
    python _video_match_diag.py
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import imagehash
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

FOLDER = Path(r"E:\MEDIA\test\video")
FRAME_POSITIONS = (0.10, 0.30, 0.50, 0.70, 0.90)  # fractional positions
CONTENT_FRAME_THR = 10        # Hamming bits per frame — tight for same-content
CONTENT_MATCH_RATIO = 0.6     # at least 60% of frames must match
DUR_DIFF_PCT_MAX = 5.0        # durations must be within 5%


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


_EXE = _ffmpeg_exe()


def probe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [_EXE, "-hide_banner", "-i", str(path)],
            capture_output=True, timeout=10,
        )
        output = result.stderr.decode("utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", output)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return None


def extract_single_frame_hash(path: Path, seek_s: float = 1.0) -> imagehash.ImageHash | None:
    """Current logic: one frame at seek_s."""
    import numpy as np
    _zero = imagehash.ImageHash(np.zeros((8, 8), dtype=bool))
    try:
        result = subprocess.run(
            [_EXE, "-hide_banner", "-loglevel", "error",
             "-ss", f"{seek_s:.3f}", "-i", str(path),
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout:
            img = Image.open(io.BytesIO(result.stdout)).convert("RGB")
            from scanner import _downscaled_for_hashing
            work = _downscaled_for_hashing(img)
            return imagehash.phash(work)
    except Exception:
        pass
    return None


def extract_multi_frame_hashes(
    path: Path, dur: float, positions: tuple = FRAME_POSITIONS
) -> list[imagehash.ImageHash | None]:
    hashes: list[imagehash.ImageHash | None] = []
    for frac in positions:
        seek = dur * frac
        ph = None
        try:
            result = subprocess.run(
                [_EXE, "-hide_banner", "-loglevel", "error",
                 "-ss", f"{seek:.3f}", "-i", str(path),
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout:
                img = Image.open(io.BytesIO(result.stdout)).convert("RGB")
                # Use 32x32 for richer hash
                work = img.resize((32, 32), Image.LANCZOS)
                ph = imagehash.phash(work)
        except Exception:
            pass
        hashes.append(ph)
    return hashes


def content_match(
    hashes_a: list, hashes_b: list,
    dur_a: float | None, dur_b: float | None,
) -> tuple[bool, str]:
    """Return (is_match, reason)."""
    # Duration gate
    if dur_a is None or dur_b is None:
        return False, "duration unknown"
    max_dur = max(dur_a, dur_b)
    if max_dur < 0.001:
        return False, "zero duration"
    dur_diff_pct = abs(dur_a - dur_b) / max_dur * 100
    if dur_diff_pct > DUR_DIFF_PCT_MAX:
        return False, f"duration differs {dur_diff_pct:.1f}% > {DUR_DIFF_PCT_MAX}%"

    # Frame matching
    matched = valid = 0
    distances = []
    for ha, hb in zip(hashes_a, hashes_b):
        if ha is not None and hb is not None:
            valid += 1
            d = ha - hb
            distances.append(d)
            if d <= CONTENT_FRAME_THR:
                matched += 1

    if valid == 0:
        return False, "no valid frame pairs"

    ratio = matched / valid
    if ratio >= CONTENT_MATCH_RATIO:
        return True, f"{matched}/{valid} frames match (thr={CONTENT_FRAME_THR}), dur_diff={dur_diff_pct:.1f}%"
    return False, f"only {matched}/{valid} frames match (need ≥{CONTENT_MATCH_RATIO:.0%}), distances={distances}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not FOLDER.exists():
        print(f"ERROR: test folder not found: {FOLDER}")
        sys.exit(1)

    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".flv", ".webm"}
    files = sorted([f for f in FOLDER.iterdir() if f.suffix.lower() in video_exts])

    if not files:
        print(f"No video files found in {FOLDER}")
        return

    print(f"Found {len(files)} video files in {FOLDER}")
    print()

    # Collect per-file data
    print("=== Per-file fingerprints ===")
    records: dict[Path, dict] = {}
    for f in files:
        sz = f.stat().st_size
        dur = probe_duration(f)
        single_ph = extract_single_frame_hash(f)
        multi_hashes = extract_multi_frame_hashes(f, dur) if dur else []
        records[f] = {
            "size": sz,
            "dur": dur,
            "single_ph": single_ph,
            "multi": multi_hashes,
        }
        hash_strs = [str(h) if h else "FAIL" for h in multi_hashes]
        print(f"  {f.name}")
        print(f"    size={sz:,}  dur={f'{dur:.2f}s' if dur else 'unknown'}")
        print(f"    single_phash={single_ph}")
        print(f"    multi_phash={hash_strs}")
        print()

    # Pairwise
    print("=== Pairwise comparison ===")
    file_list = list(records.keys())
    for i in range(len(file_list)):
        for j in range(i + 1, len(file_list)):
            a, b = file_list[i], file_list[j]
            da, db = records[a], records[b]

            same_size = da["size"] == db["size"]

            # Single-frame distance (current logic input)
            single_dist: int | str
            if da["single_ph"] is not None and db["single_ph"] is not None:
                single_dist = da["single_ph"] - db["single_ph"]
            else:
                single_dist = "N/A"

            # What current logic does: same (ext if format-match) AND same size
            current_groups = same_size  # simplified — ignoring ext here

            # New content logic
            new_groups, reason = content_match(
                da["multi"], db["multi"],
                da["dur"], db["dur"],
            )

            # Was this a missed duplicate by current logic?
            missed = new_groups and not current_groups
            label = " *** MISSED BY CURRENT LOGIC ***" if missed else ""

            print(f"  {a.name}")
            print(f"    vs {b.name}")
            print(f"    same_size={same_size}  single_frame_dist={single_dist}")
            print(f"    current_logic_groups={current_groups}  new_content_logic_groups={new_groups}")
            print(f"    reason: {reason}{label}")
            print()


if __name__ == "__main__":
    main()
