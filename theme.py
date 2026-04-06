"""
theme.py — Material Design 3 light and dark colour palettes.

Each palette is a plain dict keyed by semantic token names.
Modules import this and use ``apply_theme()`` to overwrite their
module-level colour constants before the UI is built.
"""

from __future__ import annotations

LIGHT = {
    "ACCENT":        "#1565C0",
    "ACCENT_DARK":   "#0D47A1",
    "ACCENT_TINT":   "#EBF2FA",
    "BG":            "#FFFFFF",
    "CARD_BG":       "#FFFFFF",
    "SUCCESS":       "#2E7D32",
    "ERROR":         "#C62828",
    "WARNING":       "#E65100",
    "AMBER":         "#F57F17",
    "DIVIDER":       "#E5E7EB",
    "TEXT1":         "#1A1A1A",
    "TEXT2":         "#555555",
    "TEXT3":         "#888888",
    "DISABLED":      "#C4C7C5",
    "SURFACE1":      "#FAFAFA",
    "SURFACE2":      "#F5F5F5",
    "SURFACE3":      "#EEEEEE",
    "ON_PRIMARY":    "#FFFFFF",
    # button backgrounds (saturated, always works with white text)
    "BTN_PRIMARY":   "#1565C0",
    "BTN_SUCCESS":   "#2E7D32",
    "BTN_ERROR":     "#C62828",
    "BTN_WARNING":   "#E65100",
    "BTN_SECONDARY": "#546E7A",
    # semantic aliases used by sub-modules
    "PRIMARY_TINT":  "#EBF2FA",
    "SUCCESS_TINT":  "#F0F8F0",
    "ERROR_TINT":    "#FFF5F5",
    "WARNING_TINT":  "#FFF8F0",
    # caption / hint text
    "HINT":          "#666666",
    "HINT2":         "#555555",
    "HINT3":         "#888888",
    "HINT4":         "#999999",
    "HINT5":         "#AAAAAA",
    # header
    "HEADER_BG":       "#1565C0",
    "HEADER_SUBTITLE": "#B3D4F0",
    # banner colours
    "INFO_BG":       "#F0F8F0",
    "INFO_FG":       "#1B5E20",
    "INFO_BORDER":   "#2E7D32",
    # developer card
    "DEV_BG":        "#FFFCF0",
    "DEV_BORDER":    "#FFD54F",
    "DEV_TITLE_FG":  "#E65100",
    "DEV_BODY_FG":   "#795548",
    # misc
    "DETAIL_BG":     "#FAFAFA",
    "PURPLE":        "#7c3aed",
    "NOT_INSTALLED": "#e03030",
    # disabled button text
    "DISABLED_FG":   "#AAAAAA",
    # slider canvas
    "SLIDER_REC_BAND": "#DEF0DE",   # green recommended zone
    "SLIDER_TRACK":    "#D0D0D0",   # track line
    "SLIDER_THUMB":    "#1565C0",   # thumb knob fill
    "SLIDER_THUMB_OL": "#FFFFFF",   # thumb outline
    # about hero card
    "HERO_BG":         "#1565C0",   # identity card background
    "HERO_NAME_FG":    "#FFFFFF",   # app name
    "HERO_VERSION_FG": "#BBDEFB",   # version text
    "HERO_SUBTLE_FG":  "#90CAF9",   # copyright / email
    "HERO_BTN_BG":     "#0D47A1",   # hero action buttons
    "PRIVACY_BG":      "#FFFFFF",   # privacy policy text widget
    # report viewer
    "RV_REVERT_BG":    "#455A64",   # revert buttons
    "RV_CALIB_BG":     "#5C6BC0",   # calibrate button
    "RV_SELECT_BG":    "#FFFFFF",   # select all/none bg
    "RV_SELECT_FG":    "#1565C0",   # select all/none fg
    "RV_HEADER_STATS": "#BBDEFB",   # header stats text
}

DARK = {
    "ACCENT":        "#90CAF9",
    "ACCENT_DARK":   "#64B5F6",
    "ACCENT_TINT":   "#1A2733",
    "BG":            "#121214",
    "CARD_BG":       "#1E1E22",
    "SUCCESS":       "#66BB6A",
    "ERROR":         "#EF5350",
    "WARNING":       "#FFA726",
    "AMBER":         "#FFCA28",
    "DIVIDER":       "#3A3A40",
    "TEXT1":         "#E6E1E5",
    "TEXT2":         "#CAC4D0",
    "TEXT3":         "#938F99",
    "DISABLED":      "#49454F",
    "SURFACE1":      "#1A1A1E",
    "SURFACE2":      "#252528",
    "SURFACE3":      "#2E2E34",
    "ON_PRIMARY":    "#FFFFFF",
    # button backgrounds (saturated, always works with white text)
    "BTN_PRIMARY":   "#1976D2",
    "BTN_SUCCESS":   "#388E3C",
    "BTN_ERROR":     "#D32F2F",
    "BTN_WARNING":   "#F57C00",
    "BTN_SECONDARY": "#607D8B",
    # semantic aliases
    "PRIMARY_TINT":  "#1A2733",
    "SUCCESS_TINT":  "#1B2E1B",
    "ERROR_TINT":    "#2E1515",
    "WARNING_TINT":  "#2E2210",
    # caption / hint text
    "HINT":          "#938F99",
    "HINT2":         "#A09CA6",
    "HINT3":         "#7A7680",
    "HINT4":         "#6A6670",
    "HINT5":         "#605C66",
    # header
    "HEADER_BG":       "#1A1A2E",
    "HEADER_SUBTITLE": "#7BAAD4",
    # banner colours
    "INFO_BG":       "#1B2E1B",
    "INFO_FG":       "#81C784",
    "INFO_BORDER":   "#66BB6A",
    # developer card
    "DEV_BG":        "#2E2A1A",
    "DEV_BORDER":    "#A08030",
    "DEV_TITLE_FG":  "#FFA726",
    "DEV_BODY_FG":   "#BCAAA4",
    # misc
    "DETAIL_BG":     "#252528",
    "PURPLE":        "#B388FF",
    "NOT_INSTALLED": "#EF5350",
    # disabled button text
    "DISABLED_FG":   "#605C66",
    # slider canvas
    "SLIDER_REC_BAND": "#1B3A1B",   # dark green recommended zone
    "SLIDER_TRACK":    "#49454F",   # track line
    "SLIDER_THUMB":    "#90CAF9",   # thumb knob fill (light blue)
    "SLIDER_THUMB_OL": "#1E1E22",   # thumb outline (card surface)
    # about hero card
    "HERO_BG":         "#1A1A2E",   # dark navy card
    "HERO_NAME_FG":    "#E6E1E5",   # app name
    "HERO_VERSION_FG": "#90CAF9",   # version text
    "HERO_SUBTLE_FG":  "#7BAAD4",   # copyright / email
    "HERO_BTN_BG":     "#1976D2",   # hero action buttons
    "PRIVACY_BG":      "#1A1A1E",   # privacy policy text widget
    # report viewer
    "RV_REVERT_BG":    "#607D8B",   # revert buttons
    "RV_CALIB_BG":     "#7986CB",   # calibrate button
    "RV_SELECT_BG":    "#2E2E34",   # select all/none bg
    "RV_SELECT_FG":    "#90CAF9",   # select all/none fg
    "RV_HEADER_STATS": "#7BAAD4",   # header stats text
}


def get_palette(dark: bool = False) -> dict[str, str]:
    """Return the active palette dict."""
    return DARK if dark else LIGHT
