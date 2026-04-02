"""
reporter.py — Generate a self-contained HTML report with embedded thumbnails.
v2: accepts Settings object, supports series badges, extended metadata, and
    per-image EXIF sections.
"""
from __future__ import annotations

import base64
import html
import io
from pathlib import Path
from typing import List

from PIL import Image as PILImage

from config import Settings


# ── thumbnail helpers ─────────────────────────────────────────────────────────

def _thumb_b64(path: Path, max_px: int = 400) -> str:
    """Base64 data-URI thumbnail, resized to fit max_px. Returns '' on error."""
    try:
        with PILImage.open(path) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.thumbnail((max_px, max_px), PILImage.LANCZOS)
            buf = io.BytesIO()
            fmt = "PNG" if path.suffix.lower() == ".png" else "JPEG"
            img.save(buf, format=fmt, quality=82)
        data = base64.b64encode(buf.getvalue()).decode()
        mime = "png" if fmt == "PNG" else "jpeg"
        return f"data:image/{mime};base64,{data}"
    except Exception:
        return ""


def _img_tag(path: Path, max_w: int, max_h: int) -> str:
    src = _thumb_b64(path, max(max_w, max_h))
    name = html.escape(path.name)
    if not src:
        return f'<div class="no-img">! {name}</div>'
    return (
        f'<img src="{src}" '
        f'style="max-width:{max_w}px;max-height:{max_h}px;object-fit:contain;" '
        f'title="{html.escape(str(path))}" />'
    )


def _exif_section_html(path: Path) -> str:
    """Return a collapsible HTML block showing all EXIF fields."""
    try:
        from metadata import read_exif
        exif = read_exif(path)
    except Exception:
        return ""
    if not exif:
        return '<div class="exif-empty">No EXIF data</div>'

    rows = ""
    for k, v in sorted(exif.items()):
        if k in ("MakerNote", "UserComment", "PrintImageMatching"):
            continue  # Skip large binary blobs
        v_str = html.escape(str(v)[:200])
        k_str = html.escape(str(k))
        rows += f'<tr><td class="exif-key">{k_str}</td><td class="exif-val">{v_str}</td></tr>'

    uid = abs(hash(str(path))) % 1_000_000
    return f"""
    <details class="exif-details">
      <summary>EXIF ({len(exif)} fields)</summary>
      <table class="exif-table" id="exif-{uid}">
        <tbody>{rows}</tbody>
      </table>
    </details>"""


def _orig_card(rec, extended: bool = False) -> str:
    ext_badge = f'<span class="ext-badge">{rec.path.suffix.upper().lstrip(".")}</span>'
    companions_html = ""
    if rec.companions:
        names = ", ".join(c.name for c in rec.companions[:3])
        companions_html = f'<span class="companions">+ RAW: {html.escape(names)}</span>'
    exif_html = _exif_section_html(rec.path) if extended else ""
    return f"""
      <div class="orig-item">
        <div class="orig-thumb">{_img_tag(rec.path, 280, 240)}</div>
        <div class="ometa">
          {ext_badge}
          <span class="fname" title="{html.escape(str(rec.path))}">{html.escape(rec.path.name)}</span>
          <span class="dim">{rec.dim_label()}</span>
          <span class="sz">{rec.size_label()}</span>
          <span class="dt">{rec.date_label()}</span>
          {companions_html}
          {exif_html}
        </div>
      </div>"""


def _preview_card(rec, extended: bool = False) -> str:
    ext_badge = f'<span class="ext-badge trash-ext">{rec.path.suffix.upper().lstrip(".")}</span>'
    exif_html = _exif_section_html(rec.path) if extended else ""
    return f"""
            <div class="preview-card">
              <div class="thumb">{_img_tag(rec.path, 160, 140)}</div>
              <div class="pmeta">
                {ext_badge}
                <span class="fname" title="{html.escape(str(rec.path))}">{html.escape(rec.path.name)}</span>
                <span class="dim">{rec.dim_label()}</span>
                <span class="sz">{rec.size_label()}</span>
                <span class="dt">{rec.date_label()}</span>
                {exif_html}
              </div>
            </div>"""


# ── main entry ────────────────────────────────────────────────────────────────

_THUMB_GROUP_LIMIT = 500  # groups above this get no embedded thumbnails


def generate_report(
    groups: List,
    output_folder: Path,
    source_folder: Path,
    total_scanned: int,
    settings: Settings,
) -> Path:
    report_path = output_folder / "report.html"

    total_previews = sum(len(g.previews) for g in groups)
    total_originals = sum(len(g.originals) for g in groups)
    total_bytes_saved = sum(sum(p.file_size for p in g.previews) for g in groups)
    saved_mb = total_bytes_saved / (1024 * 1024)
    series_count = sum(1 for g in groups if g.is_series)

    strategy_label = "Oldest file date" if settings.keep_strategy == "oldest" else "Largest pixels"
    format_label = "Yes" if settings.keep_all_formats else "No"
    dry_run = settings.dry_run

    # For very large scans, skip embedded base64 thumbnails to keep the HTML
    # file size manageable.  Thumbnails are still viewable in the in-app viewer.
    embed_thumbs = len(groups) <= _THUMB_GROUP_LIMIT

    def _orig_card_maybe(rec, extended=False):
        if embed_thumbs:
            return _orig_card(rec, extended=extended)
        name = html.escape(rec.path.name)
        return (
            f'<div class="img-wrap">'
            f'<div class="no-img" title="{html.escape(str(rec.path))}">{name}</div>'
            f'<div class="meta"><span class="sz">{rec.size_label()}</span>'
            f'<span class="dt">{rec.date_label()}</span></div></div>'
        )

    def _preview_card_maybe(rec, extended=False):
        if embed_thumbs:
            return _preview_card(rec, extended=extended)
        name = html.escape(rec.path.name)
        return (
            f'<div class="img-wrap">'
            f'<div class="no-img" title="{html.escape(str(rec.path))}">{name}</div>'
            f'<div class="meta"><span class="sz">{rec.size_label()}</span>'
            f'<span class="dt">{rec.date_label()}</span></div></div>'
        )

    # ── group cards ──────────────────────────────────────────────────────
    cards: list[str] = []
    for idx, group in enumerate(groups, 1):
        is_ambiguous = getattr(group, "is_ambiguous", False)
        orig_html = "".join(_orig_card_maybe(o, extended=settings.extended_report) for o in group.originals)
        prev_html = "".join(_preview_card_maybe(p, extended=settings.extended_report) for p in group.previews)

        n_kept = len(group.originals)
        n_trashed = len(group.previews)
        kept_lbl = f"{n_kept} original{'s' if n_kept != 1 else ''} kept"
        trash_lbl = f"{n_trashed} preview{'s' if n_trashed != 1 else ''} removed"

        badges = ""
        if group.is_series:
            badges += '<span class="series-badge">SERIES \u2014 all images kept</span>'
        if is_ambiguous:
            badges += '<span class="ambig-badge">\u26a0 UNCERTAIN MATCH \u2014 review manually, no files moved</span>'

        group_id = getattr(group, "group_id", f"g{idx:04d}")
        card_class = "card card-ambiguous" if is_ambiguous else "card"

        if is_ambiguous:
            body_html = f"""
          <div class="original-col" style="width:100%">
            <div class="col-label ambig">Similar images &mdash; check before deleting</div>
            <div class="orig-grid">{orig_html}</div>
          </div>"""
        else:
            body_html = f"""
          <div class="original-col">
            <div class="col-label keep">Kept &rarr; results/</div>
            <div class="orig-grid">{orig_html}</div>
          </div>

          <div class="previews-col">
            <div class="col-label trash">Moved &rarr; trash/</div>
            <div class="preview-grid">{prev_html}</div>
          </div>"""

        cards.append(f"""
      <div class="{card_class}" id="{group_id}">
        <div class="card-head">
          <span class="gnum">#{idx}</span>
          <span class="gid">({group_id})</span>
          <span class="gsub">{kept_lbl} &nbsp;&middot;&nbsp; {trash_lbl}</span>
          {badges}
        </div>
        <div class="card-body">{body_html}
        </div>
      </div>""")

    cards_html = "\n".join(cards) if cards else (
        '<div class="empty">No duplicate groups found. All images appear unique.</div>'
    )

    dry_tag = (
        '<span class="tag dry">DRY RUN &mdash; no files were moved</span>'
        if dry_run else
        '<span class="tag live">FILES MOVED</span>'
    )

    no_thumbs_note = (
        f'<div style="background:#fff8e1;border-left:4px solid #f9a825;'
        f'padding:10px 18px;margin:0 32px 16px;border-radius:4px;font-size:.85rem;">'
        f'&#9432; Thumbnails are not embedded in this report because the scan produced '
        f'{len(groups):,} groups (limit: {_THUMB_GROUP_LIMIT:,}). '
        f'Use the <strong>In-App Viewer</strong> to browse groups with full previews.</div>'
    ) if not embed_thumbs else ""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Image Deduper Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI",Arial,sans-serif;background:#f0f2f5;color:#1f1f1f;font-size:14px}}

.hdr{{background:#1a73e8;color:#fff;padding:22px 32px}}
.hdr h1{{font-size:1.5rem;font-weight:700}}
.hdr .src{{opacity:.8;font-size:.85rem;margin-top:4px;word-break:break-all}}
.hdr .opts{{font-size:.8rem;opacity:.75;margin-top:6px}}

.stats{{display:flex;flex-wrap:wrap;gap:14px;padding:20px 32px}}
.stat{{background:#fff;border-radius:10px;padding:14px 22px;
       box-shadow:0 1px 4px rgba(0,0,0,.1);min-width:130px}}
.stat .val{{font-size:1.9rem;font-weight:700;color:#1a73e8}}
.stat .lbl{{font-size:.75rem;color:#777;margin-top:2px}}

.tag{{display:inline-block;padding:3px 12px;border-radius:99px;font-size:.8rem;
      font-weight:700;margin-left:10px;vertical-align:middle}}
.tag.dry{{background:#f59e0b;color:#fff}}
.tag.live{{background:#22c55e;color:#fff}}

.series-badge{{display:inline-block;padding:2px 10px;border-radius:99px;
               background:#7c3aed;color:#fff;font-size:.75rem;font-weight:700;
               margin-left:10px;vertical-align:middle}}
.ambig-badge{{display:inline-block;padding:2px 10px;border-radius:99px;
              background:#d97706;color:#fff;font-size:.75rem;font-weight:700;
              margin-left:10px;vertical-align:middle}}
.card-ambiguous .card-head{{background:#fef3c7}}
.col-label.ambig{{background:#fef3c7;color:#92400e}}

.content{{padding:8px 32px 48px}}

.card{{background:#fff;border-radius:12px;box-shadow:0 1px 5px rgba(0,0,0,.1);
       margin-bottom:24px;overflow:hidden}}
.card-head{{background:#e8f0fe;padding:10px 20px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.gnum{{font-weight:700;color:#1a73e8;font-size:1rem}}
.gid{{color:#999;font-size:.8rem}}
.gsub{{color:#555;font-size:.85rem}}

.card-body{{display:flex}}
.original-col{{padding:20px;flex:1;border-right:1px solid #eee;min-width:0}}
.previews-col{{padding:20px;flex:1;min-width:0}}

.col-label{{font-size:.78rem;font-weight:700;letter-spacing:.4px;
            text-transform:uppercase;margin-bottom:12px;padding:4px 10px;
            border-radius:4px;display:inline-block}}
.col-label.keep{{background:#dcfce7;color:#16a34a}}
.col-label.trash{{background:#fee2e2;color:#dc2626}}

.orig-grid{{display:flex;flex-wrap:wrap;gap:16px}}
.orig-item{{display:flex;flex-direction:column;align-items:center;max-width:300px}}
.orig-thumb{{text-align:center;margin-bottom:8px}}
.orig-thumb img{{border-radius:6px;max-width:100%}}

.preview-grid{{display:flex;flex-wrap:wrap;gap:14px}}
.preview-card{{background:#fafafa;border:1px solid #e5e7eb;border-radius:8px;
               padding:10px;display:flex;flex-direction:column;align-items:center;width:190px}}
.preview-card .thumb{{text-align:center;margin-bottom:8px}}
.preview-card img{{border-radius:4px}}

.ometa,.pmeta{{display:flex;flex-direction:column;gap:3px;align-items:center;text-align:center;width:100%}}
.fname{{font-weight:600;font-size:.82rem;white-space:nowrap;overflow:hidden;
        text-overflow:ellipsis;max-width:100%;display:block}}
.dim{{color:#555;font-size:.8rem}}
.sz{{color:#888;font-size:.79rem}}
.dt{{color:#aaa;font-size:.75rem;font-style:italic}}
.companions{{color:#7c3aed;font-size:.72rem;font-style:italic}}

.ext-badge{{display:inline-block;background:#1a73e8;color:#fff;font-size:.7rem;
            font-weight:700;padding:1px 7px;border-radius:99px;margin-bottom:4px}}
.trash-ext{{background:#dc2626}}

.no-img{{color:#aaa;font-size:.8rem;text-align:center;padding:12px}}
.empty{{text-align:center;padding:60px;color:#888;font-size:1.1rem}}

/* EXIF collapsible */
.exif-details{{margin-top:6px;width:100%;text-align:left}}
.exif-details summary{{cursor:pointer;font-size:.75rem;color:#1a73e8;padding:2px 0}}
.exif-table{{width:100%;border-collapse:collapse;font-size:.72rem;margin-top:4px}}
.exif-table tr:nth-child(even){{background:#f9fafb}}
.exif-key{{color:#555;padding:1px 6px 1px 0;white-space:nowrap;font-weight:600}}
.exif-val{{color:#333;padding:1px 0;word-break:break-all}}
.exif-empty{{color:#aaa;font-size:.75rem;font-style:italic}}

@media(max-width:700px){{
  .card-body{{flex-direction:column}}
  .original-col{{border-right:none;border-bottom:1px solid #eee}}
}}
</style>
</head>
<body>

<div class="hdr">
  <h1>Image Deduper Report {dry_tag}</h1>
  <div class="src">Source: {html.escape(str(source_folder))}</div>
  <div class="opts">
    Keep strategy: <strong>{strategy_label}</strong>
    &nbsp;&middot;&nbsp; Keep all formats: <strong>{format_label}</strong>
    &nbsp;&middot;&nbsp; AR tolerance: <strong>{settings.ar_tolerance_pct}%</strong>
    &nbsp;&middot;&nbsp; Dual hash: <strong>{"ON" if settings.use_dual_hash else "OFF"}</strong>
    &nbsp;&middot;&nbsp; Histogram: <strong>{"ON" if settings.use_histogram else "OFF"}</strong>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val">{total_scanned}</div><div class="lbl">Images scanned</div></div>
  <div class="stat"><div class="val">{len(groups)}</div><div class="lbl">Duplicate groups</div></div>
  <div class="stat"><div class="val">{series_count}</div><div class="lbl">Series groups</div></div>
  <div class="stat"><div class="val">{total_originals}</div><div class="lbl">Originals kept</div></div>
  <div class="stat"><div class="val">{total_previews}</div><div class="lbl">Previews removed</div></div>
  <div class="stat"><div class="val">{saved_mb:.1f} MB</div><div class="lbl">Space reclaimed</div></div>
</div>

{no_thumbs_note}
<div class="content">
{cards_html}
</div>

</body>
</html>"""

    output_folder.mkdir(parents=True, exist_ok=True)
    report_path.write_text(page, encoding="utf-8")
    return report_path
