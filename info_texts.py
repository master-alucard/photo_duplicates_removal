"""
info_texts.py — Help text for every settings parameter shown in the UI.
"""
from __future__ import annotations

# Each entry: key -> (title, detailed_explanation)
INFO_TEXTS: dict[str, tuple[str, str]] = {
    "threshold": (
        "Similarity Threshold",
        "Controls how visually similar two images must be to be grouped together.\n\n"
        "Lower = stricter (fewer false positives, may miss some real duplicates)\n"
        "Higher = looser (catches more duplicates, risks grouping different images)\n\n"
        "Example: At threshold=10, a 4000x3000 original and its 800x600 preview "
        "will match. At threshold=5, only very close copies match.\n\n"
        "Recommended: 8-12 for typical photo libraries."
    ),
    "preview_ratio": (
        "Preview Size Ratio",
        "The per-dimension ratio used to decide if an image is a 'preview' of another.\n\n"
        "An image is only considered a preview if BOTH its width AND height are strictly "
        "smaller than the original's width and height (multiplied by this ratio).\n\n"
        "Example: ratio=0.90 means an image must be less than 90% of the original in "
        "BOTH dimensions to be classified as a preview.\n\n"
        "This prevents burst shots and color-edited copies (same resolution) from ever "
        "being trashed — only genuine small previews are removed.\n\n"
        "Recommended: 0.85-0.95."
    ),
    "series_tolerance_pct": (
        "Series Dimension Tolerance",
        "Percentage tolerance when comparing image dimensions for series detection.\n\n"
        "Images with the same resolution (within this tolerance) are treated as a 'series' "
        "— burst shots, color edits, bracketed exposures — and ALL are kept as originals.\n\n"
        "Example: tolerance=2% means a 4000x3000 and a 3992x2994 image are treated as "
        "the same resolution.\n\n"
        "Set to 0 for exact matching. Raise to 2-3% for cameras with slight variation.\n\n"
        "Recommended: 0-2%."
    ),
    "series_threshold_factor": (
        "Series Grouping Leniency",
        "Multiplier applied to the similarity threshold when comparing two images that "
        "have the same dimensions (potential series/burst shots).\n\n"
        "Same-size images from the same camera session may have slightly different "
        "hashes due to JPEG quality variation or minor edits. A factor >1 allows "
        "them to be grouped together, after which series detection keeps ALL of them "
        "as originals — nothing is trashed.\n\n"
        "Example: threshold=10, factor=2.0 → same-size images are compared at "
        "threshold=20 for grouping purposes.\n\n"
        "Lower values are safer (fewer grouping false-positives).\n"
        "Recommended: 1.5–2.5."
    ),
    "ar_tolerance_pct": (
        "Aspect Ratio Tolerance",
        "Maximum allowed difference in aspect ratio between two images before they are "
        "excluded from comparison entirely.\n\n"
        "This is a critical false-positive guard: a 6000x4000 landscape (AR=1.50) and "
        "a 3264x1832 portrait (AR=1.78) would produce similar pHash values due to "
        "rescaling, but their aspect ratio difference of 16% would exceed the default "
        "5% tolerance and prevent a false match.\n\n"
        "Formula: |AR_A - AR_B| / max(AR_A, AR_B) <= tolerance%\n\n"
        "Recommended: 3-8%."
    ),
    "dark_protection": (
        "Dark Image Protection",
        "Enable automatic tightening of the similarity threshold for dark images.\n\n"
        "Dark images (low mean brightness) tend to produce false matches because "
        "pHash has less visual information to work with when most pixels are near-black.\n\n"
        "When enabled: if either image's mean brightness is below the Dark Threshold, "
        "the pHash threshold is multiplied by the Dark Tighten Factor (e.g., 0.5 = half).\n\n"
        "Recommended: ON for general photo libraries."
    ),
    "dark_threshold": (
        "Dark Image Threshold",
        "Mean pixel brightness (0-255) below which an image is considered 'dark' and "
        "triggers the tighter similarity threshold.\n\n"
        "Example: threshold=40 means images with average brightness below 40/255 "
        "(about 16% brightness) are considered dark.\n\n"
        "Only used when Dark Protection is enabled.\n\n"
        "Recommended: 30-50."
    ),
    "dark_tighten_factor": (
        "Dark Tighten Factor",
        "Multiplier applied to the pHash threshold when a dark image is detected.\n\n"
        "Example: factor=0.5 with threshold=10 means dark images must have a pHash "
        "distance of 5 or less to be grouped (instead of 10).\n\n"
        "Lower values = stricter matching for dark images.\n"
        "Range: 0.1 (very strict) to 1.0 (no tightening)\n\n"
        "Recommended: 0.4-0.6."
    ),
    "use_dual_hash": (
        "Dual Hash (dHash)",
        "Enable a second perceptual hash (dHash) as an additional verification step.\n\n"
        "dHash detects gradient changes between adjacent pixels, making it sensitive to "
        "different kinds of visual differences than pHash.\n\n"
        "When enabled: both pHash AND dHash must pass their thresholds for two images "
        "to be considered similar. This significantly reduces false positives with minimal "
        "impact on true positive detection.\n\n"
        "dHash threshold = pHash threshold * 1.5\n\n"
        "Recommended: ON."
    ),
    "use_histogram": (
        "Histogram Intersection Guard",
        "Enable color histogram comparison as a final verification step.\n\n"
        "Computes a 96-bin RGB histogram (32 bins per channel) for each image and "
        "measures how similar the color distributions are.\n\n"
        "This prevents false matches between images that have similar hashes but "
        "very different color palettes (e.g., a photo of snow vs. a photo of a white wall).\n\n"
        "Intersection similarity must be >= Minimum Histogram Similarity to proceed.\n\n"
        "Recommended: ON."
    ),
    "hist_min_similarity": (
        "Minimum Histogram Similarity",
        "Minimum color histogram intersection similarity (0.0-1.0) required for "
        "two images to be considered duplicates.\n\n"
        "Histogram intersection = sum(min(bin_A, bin_B)) / total_pixels\n\n"
        "1.0 = identical color distributions\n"
        "0.7 = moderately similar (default)\n"
        "0.5 = only broadly similar colors required\n\n"
        "Example: A photo of a sunset (orange/red dominant) and a photo of a forest "
        "(green dominant) would score ~0.3 and be correctly excluded.\n\n"
        "Recommended: 0.65-0.80."
    ),
    "brightness_max_diff": (
        "Max Brightness Difference",
        "Maximum allowed difference in mean pixel brightness (0-255) between two images.\n\n"
        "This guard prevents matching a bright outdoor photo with a dark indoor one, "
        "even if their structural hashes happen to be similar.\n\n"
        "Example: diff=60 means two images must have mean brightness within 60 units "
        "of each other. An image with mean brightness 200 won't match one with 130.\n\n"
        "Recommended: 40-80."
    ),
    "use_rawpy": (
        "RAW File Support (rawpy)",
        "Enable full perceptual hashing for RAW camera files using the rawpy library.\n\n"
        "Supported formats: CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2, PEF, SRW, X3F, 3FR\n\n"
        "When DISABLED (default): RAW files are paired with their JPEG/TIFF companions "
        "by matching filename stems (e.g., IMG_1234.CR2 + IMG_1234.jpg).\n\n"
        "When ENABLED: rawpy decodes the RAW file and computes an actual perceptual hash, "
        "allowing RAW files to participate in duplicate detection independently.\n\n"
        "Note: rawpy must be installed separately. Adds ~30% processing time.\n\n"
        "Recommended: OFF unless you have unmatched RAW files."
    ),
    "keep_strategy": (
        "Keep Strategy",
        "Determines which image is kept as the 'original' when duplicates are found.\n\n"
        "'pixels' (default): Keep the image with the most pixels (highest resolution). "
        "Best for quality-focused libraries where you always want the largest version.\n\n"
        "'oldest': Keep the image with the oldest file date (creation/modification time). "
        "Best for chronological organization where the first version is the 'master'.\n\n"
        "Both strategies keep ALL images that pass the series detection check "
        "(same-resolution burst shots are always kept regardless of this setting)."
    ),
    "keep_all_formats": (
        "Keep All Formats",
        "When enabled, keeps the best representative of each file format, but ONLY for "
        "images that have the same (largest) resolution as the group's best image.\n\n"
        "Example: If a group contains IMG_1234.jpg (4000x3000), IMG_1234.png (4000x3000), "
        "and IMG_1234_thumb.jpg (800x600):\n"
        "  - OFF: Keep only IMG_1234.jpg, trash the others\n"
        "  - ON: Keep both IMG_1234.jpg AND IMG_1234.png (same full size), "
        "trash only the thumbnail\n\n"
        "Smaller versions in alternative formats are still treated as previews "
        "and moved to trash regardless of this setting.\n\n"
        "Useful when you intentionally keep a PNG for editing and a JPEG for sharing.\n\n"
        "Recommended: ON (default)."
    ),
    "prefer_rich_metadata": (
        "Prefer Rich Metadata",
        "When two images have identical resolution and format, prefer the one with "
        "more EXIF metadata fields (camera model, GPS, lens info, etc.).\n\n"
        "This helps keep the more 'complete' original when choosing between copies "
        "that are otherwise equal in quality.\n\n"
        "Tie-breaking order: resolution (if pixels strategy) or date (if oldest), "
        "then metadata count if equal.\n\n"
        "Recommended: ON."
    ),
    "collect_metadata": (
        "Collect EXIF Metadata",
        "Read EXIF metadata from each image during scanning.\n\n"
        "Collected data includes: camera model, lens, ISO, aperture, shutter speed, "
        "GPS coordinates, date/time, and other technical details.\n\n"
        "Used for:\n"
        "  - Rich metadata display in reports and the in-app viewer\n"
        "  - Prefer Rich Metadata tie-breaking\n"
        "  - Metadata export (CSV)\n"
        "  - Sort by EXIF date\n\n"
        "Adds ~15% to scan time. Recommended: ON."
    ),
    "export_csv": (
        "Export Metadata CSV",
        "Write a metadata_export.csv file to the output folder after scanning.\n\n"
        "The CSV contains one row per image in each duplicate group, with columns for:\n"
        "group_id, role (original/preview), filename, path, width, height, "
        "file_size_kb, date_taken, camera_model, lens, iso, aperture, "
        "shutter_speed, and more.\n\n"
        "Useful for batch review in Excel or other spreadsheet tools.\n\n"
        "Recommended: ON."
    ),
    "extended_report": (
        "Extended HTML Report",
        "Include a full EXIF metadata section for each image in the HTML report.\n\n"
        "When enabled, each image card in the report shows a collapsible metadata "
        "panel with all available EXIF fields.\n\n"
        "This makes the report significantly larger (file size) and slower to generate "
        "for large libraries, but provides complete technical information inline.\n\n"
        "Recommended: OFF for large libraries, ON for detailed review."
    ),
    "sort_by_filename_date": (
        "Sort by Filename Date",
        "Extract dates embedded in filenames and use them for sorting within groups.\n\n"
        "Detected patterns include:\n"
        "  - 2024-03-15 or 20240315\n"
        "  - IMG_20240315_120000\n"
        "  - DSC_20240315\n"
        "  - Screenshot_2024-03-15\n\n"
        "When enabled, images within a group are sorted chronologically by filename date "
        "before applying the keep strategy.\n\n"
        "Recommended: ON for phone camera libraries."
    ),
    "sort_by_exif_date": (
        "Sort by EXIF Date",
        "Use the DateTimeOriginal EXIF field (actual capture time) for sorting within groups.\n\n"
        "When enabled and EXIF date is available, images within a group are sorted "
        "by capture time. Combined with 'oldest' keep strategy, this ensures the "
        "first-captured image is always kept.\n\n"
        "Requires 'Collect EXIF Metadata' to be enabled.\n\n"
        "Takes precedence over filename date sorting if both are enabled.\n\n"
        "Recommended: ON for DSLR/mirrorless camera libraries."
    ),
    "min_dimension": (
        "Minimum Dimension Filter",
        "Skip images where the longest dimension is smaller than this value (in pixels).\n\n"
        "Example: min_dimension=200 will skip 150x100 thumbnails but include "
        "300x200 images.\n\n"
        "Useful for ignoring tiny icons, favicons, or decorative images that are "
        "too small to be meaningful originals.\n\n"
        "Set to 0 to disable (include all sizes).\n\n"
        "Recommended: 100-300 for photo libraries, 0 for general use."
    ),
    "recursive": (
        "Recursive Subfolder Scan",
        "When enabled, scans all subfolders of the source directory.\n\n"
        "When disabled, only scans the top-level source directory.\n\n"
        "Most photo libraries are organized into subfolders by date, event, or album, "
        "so recursive scanning is usually the right choice.\n\n"
        "The output folder and its subfolders (results/, trash/) are automatically "
        "excluded from scanning even in recursive mode.\n\n"
        "Recommended: ON."
    ),
    "skip_names": (
        "Skip Folder Names",
        "Comma-separated list of folder names to skip during scanning.\n\n"
        "Any folder matching one of these names (case-sensitive) will be completely "
        "skipped, along with all its subfolders.\n\n"
        "Default: .thumbnails, thumbs, @eaDir, Thumbs\n\n"
        "Common additions:\n"
        "  - .Trash (Linux trash)\n"
        "  - .DS_Store (macOS)\n"
        "  - __pycache__ (Python)\n"
        "  - node_modules (JavaScript)\n\n"
        "Note: The output folder is always skipped automatically."
    ),
    "dry_run": (
        "Dry Run Mode",
        "When enabled, performs the full scan and generates the report but does NOT "
        "move any files.\n\n"
        "Workflow:\n"
        "  1. Enable Dry Run and click Start Scan\n"
        "  2. Review the HTML report — it shows exactly what would be kept vs trashed\n"
        "  3. If the results look correct, click 'Accept & Move' in the toolbar "
        "to apply the changes (no need to rescan)\n"
        "  4. Or adjust settings and scan again\n\n"
        "Files remain in their original locations until you click 'Accept & Move'.\n\n"
        "Recommended: ON for first-time use of a new source folder."
    ),
    "organize_by_date": (
        "Organize Output by Date",
        "When enabled, moved files are placed into date-named subfolders inside "
        "results/ and trash/ instead of a flat folder.\n\n"
        "The date is taken from (in order of preference):\n"
        "  1. EXIF DateTimeOriginal (actual capture time)\n"
        "  2. Date embedded in the filename (e.g. IMG_20240315_...)\n"
        "  3. File modification time (fallback)\n\n"
        "Example with format '%Y-%m':\n"
        "  results/2024-03/IMG_1234.jpg\n"
        "  results/2024-05/IMG_5678.jpg\n"
        "  trash/2024-03/IMG_1234_thumb.jpg\n\n"
        "Useful for building a date-organized photo library from a messy folder.\n\n"
        "Recommended: OFF unless you want to reorganize by date."
    ),
    "date_folder_format": (
        "Date Folder Format",
        "strftime format string used to name the date subfolders.\n\n"
        "Common formats:\n"
        "  %Y-%m      → 2024-03  (year-month, one folder per month)\n"
        "  %Y/%m      → 2024/03  (nested year/month folders)\n"
        "  %Y-%m-%d   → 2024-03-15  (one folder per day)\n"
        "  %Y         → 2024  (one folder per year)\n\n"
        "Only used when 'Organize Output by Date' is enabled."
    ),
    "ambiguous_detection": (
        "Ambiguous Match Detection",
        "When enabled, images that are similar but not confidently duplicates are "
        "grouped into separate 'uncertain' groups for manual review.\n\n"
        "A pair is flagged as ambiguous when their pHash distance is BETWEEN the normal "
        "Similarity Threshold and (Threshold × Ambiguous Factor).\n\n"
        "Example: threshold=12, factor=1.5 → pairs with pHash distance 13–18 are "
        "flagged as ambiguous.\n\n"
        "Ambiguous groups are shown with an orange warning badge in the report. "
        "No files are moved from ambiguous groups — you must decide manually.\n\n"
        "Only singletons (images not already in a regular duplicate group) are checked.\n\n"
        "Recommended: OFF for automated runs, ON for careful manual review."
    ),
    "ambiguous_threshold_factor": (
        "Ambiguous Threshold Factor",
        "Multiplier on the Similarity Threshold that defines the upper bound of the "
        "'uncertain' similarity zone.\n\n"
        "Pairs with pHash distance in (threshold, threshold × factor] are marked ambiguous.\n\n"
        "Example: threshold=12, factor=1.5 → ambiguous zone is distance 13–18.\n\n"
        "Lower factor = narrower ambiguous zone (fewer borderline matches flagged).\n"
        "Higher factor = wider zone (more potential matches surfaced for review).\n\n"
        "Only active when Ambiguous Match Detection is enabled.\n\n"
        "Recommended: 1.3–2.0."
    ),
    "mode": (
        "Quick vs Advanced Mode",
        "Quick mode shows only the essential options: source folder, output folder, "
        "and dry run checkbox. All other settings use their saved defaults.\n\n"
        "Advanced mode exposes all detection parameters, keep strategy, metadata "
        "options, and filter settings.\n\n"
        "Your settings are preserved when switching between modes."
    ),
}
