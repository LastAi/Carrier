# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Colour themes, including presets for colour-vision deficiencies.

Both front ends read the same palette: the terminal UI turns it into Rich
styles, the desktop app into CSS variables. So a theme (or a custom colour)
looks the same everywhere.

Design rules
------------
* **Colour is never the only signal.** Added/removed lines always carry a
  ``+``/``−`` marker, removed lines can be struck through, and severities
  show their name ("HIGH"), so every theme stays usable in monochrome.
* The colour-blind presets use the Okabe–Ito palette, which was designed to
  stay distinguishable for the common forms of colour blindness:

  - ``red-green`` — deuteranopia / protanopia (most common): blue vs orange.
  - ``blue-yellow`` — tritanopia: red/pink vs cyan/teal.
  - ``high-contrast`` — maximum luminance difference, bold markers.
  - ``monochrome`` — no hue at all; relies on markers, bold and strikethrough.

Users can override any single key in their settings (``custom_colors``), on
top of any preset.
"""

from __future__ import annotations

from typing import Any

# Every key a palette must define. Hex colours (#rrggbb). Backgrounds are
# kept dark-ish and foregrounds light, for dark UIs.
KEYS: dict[str, str] = {
    "added_fg": "text of added lines",
    "added_bg": "background of added lines",
    "removed_fg": "text of removed lines",
    "removed_bg": "background of removed lines",
    "context_fg": "unchanged lines",
    "hunk_fg": "@@ separators",
    "file_fg": "file headers",
    "line_no_fg": "line numbers",
    "selection_bg": "highlighted (selected) lines",
    "sev_high": "HIGH severity flags",
    "sev_medium": "MEDIUM severity flags",
    "sev_low": "LOW severity flags",
    "accent": "headings and focus",
}

PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "label": "Default (red / green)",
        "colors": {
            "added_fg": "#b5f5b5", "added_bg": "#123d1f",
            "removed_fg": "#ffc2c2", "removed_bg": "#4a1717",
            "context_fg": "#c9d1d9", "hunk_fg": "#79c0ff", "file_fg": "#e3b341",
            "line_no_fg": "#6e7681", "selection_bg": "#264f78",
            "sev_high": "#ff6b6b", "sev_medium": "#f2cc60", "sev_low": "#79c0ff",
            "accent": "#e3b341",
        },
        "strike_removed": False,
    },
    "red-green": {
        "label": "Red–green safe (deuteranopia / protanopia)",
        "colors": {
            "added_fg": "#9fd3ff", "added_bg": "#0b2e4f",  # Okabe-Ito blue family
            "removed_fg": "#ffd08a", "removed_bg": "#4a3000",  # Okabe-Ito orange family
            "context_fg": "#d0d0d0", "hunk_fg": "#56b4e9", "file_fg": "#f0e442",
            "line_no_fg": "#8a8a8a", "selection_bg": "#5a4a7a",
            "sev_high": "#e69f00", "sev_medium": "#f0e442", "sev_low": "#56b4e9",
            "accent": "#f0e442",
        },
        "strike_removed": True,
    },
    "blue-yellow": {
        "label": "Blue–yellow safe (tritanopia)",
        "colors": {
            "added_fg": "#9ff0ee", "added_bg": "#063f3e",  # teal
            "removed_fg": "#ffb3c7", "removed_bg": "#4d0f22",  # red / pink
            "context_fg": "#d0d0d0", "hunk_fg": "#ff8fab", "file_fg": "#ffffff",
            "line_no_fg": "#8a8a8a", "selection_bg": "#4a4a4a",
            "sev_high": "#ff4d6d", "sev_medium": "#ffffff", "sev_low": "#2ec4b6",
            "accent": "#ff8fab",
        },
        "strike_removed": True,
    },
    "high-contrast": {
        "label": "High contrast",
        "colors": {
            "added_fg": "#ffffff", "added_bg": "#003d00",
            "removed_fg": "#ffffff", "removed_bg": "#5c0000",
            "context_fg": "#ffffff", "hunk_fg": "#00ffff", "file_fg": "#ffff00",
            "line_no_fg": "#bbbbbb", "selection_bg": "#0000aa",
            "sev_high": "#ff4040", "sev_medium": "#ffff00", "sev_low": "#00ffff",
            "accent": "#ffff00",
        },
        "strike_removed": True,
    },
    "monochrome": {
        "label": "Monochrome (no colour)",
        "colors": {
            "added_fg": "#ffffff", "added_bg": "#2a2a2a",
            "removed_fg": "#9a9a9a", "removed_bg": "#1a1a1a",
            "context_fg": "#cfcfcf", "hunk_fg": "#ffffff", "file_fg": "#ffffff",
            "line_no_fg": "#7a7a7a", "selection_bg": "#555555",
            "sev_high": "#ffffff", "sev_medium": "#cfcfcf", "sev_low": "#9a9a9a",
            "accent": "#ffffff",
        },
        "strike_removed": True,
    },
}

DEFAULT_THEME = "default"


def _valid_hex(v: object) -> bool:
    return (isinstance(v, str) and len(v) == 7 and v.startswith("#")
            and all(c in "0123456789abcdefABCDEF" for c in v[1:]))


def resolve(theme: str, custom: dict[str, str] | None = None) -> dict[str, Any]:
    """Return ``{"name", "label", "colors", "strike_removed"}`` for a theme.

    Unknown themes fall back to the default; invalid custom colours are
    ignored rather than breaking the UI.
    """
    preset = PRESETS.get(theme) or PRESETS[DEFAULT_THEME]
    colors = dict(preset["colors"])
    for k, v in (custom or {}).items():
        if k in KEYS and _valid_hex(v):
            colors[k] = v.lower()
    return {
        "name": theme if theme in PRESETS else DEFAULT_THEME,
        "label": preset["label"],
        "colors": colors,
        "strike_removed": preset["strike_removed"],
    }


def list_presets() -> list[dict[str, str]]:
    return [{"name": n, "label": p["label"]} for n, p in PRESETS.items()]
