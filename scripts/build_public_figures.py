#!/usr/bin/env python3
"""Regenerate the README SVGs from publishable aggregate CSV files."""

from __future__ import annotations

import csv
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "public_results"
ASSETS = ROOT / "assets"

INK = "#172033"
MUTED = "#5b6474"
BLUE = "#3867d6"
TEAL = "#20a39e"
ORANGE = "#f2994a"
GRID = "#dce2ea"
BG = "#f8fafc"


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def svg_text(x: float, y: float, value: str, **attrs: object) -> str:
    rendered = " ".join(f'{key.replace("_", "-")}="{val}"' for key, val in attrs.items())
    return f'<text x="{x}" y="{y}" {rendered}>{escape(value)}</text>'


def attribution_chart() -> str:
    rows = read_csv("attribution_summary.csv")
    width, height = 960, 520
    left, top, plot_w, plot_h = 120, 100, 760, 300
    maximum = 140.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" rx="18" fill="{BG}"/>',
        svg_text(48, 52, "What moved net GEX?", fill=INK, font_size=28, font_weight=700),
        svg_text(48, 78, "Median mechanical share; values above 100% mean the surface opposed the move", fill=MUTED, font_size=15),
    ]
    for tick in (0, 50, 100, 140):
        x = left + plot_w * tick / maximum
        parts.append(f'<line x1="{x}" y1="{top}" x2="{x}" y2="{top + plot_h}" stroke="{GRID}"/>')
        parts.append(svg_text(x, top + plot_h + 28, f"{tick}%", fill=MUTED, font_size=13, text_anchor="middle"))
    x100 = left + plot_w * 100 / maximum
    parts.append(f'<line x1="{x100}" y1="{top - 10}" x2="{x100}" y2="{top + plot_h + 5}" stroke="{ORANGE}" stroke-width="3" stroke-dasharray="7 7"/>')
    for index, row in enumerate(rows):
        y = top + 28 + index * 72
        value = float(row["median_mechanical_share_pct"])
        bar_w = plot_w * value / maximum
        color = TEAL if value >= 100 else BLUE
        parts.append(svg_text(left - 18, y + 21, row["bucket"], fill=INK, font_size=16, font_weight=600, text_anchor="end"))
        parts.append(f'<rect x="{left}" y="{y}" width="{bar_w}" height="30" rx="7" fill="{color}"/>')
        parts.append(svg_text(left + bar_w + 10, y + 21, f"{value:.0f}%", fill=INK, font_size=15, font_weight=700))
        parts.append(svg_text(left, y + 51, f'Surface opposed: {row["surface_opposes_count"]}/{row["observations"]}', fill=MUTED, font_size=13))
    parts.append(svg_text(48, 475, "Five sessions · two windows · four expiry buckets · descriptive, not inferential", fill=MUTED, font_size=14))
    parts.append("</svg>")
    return "\n".join(parts)


def crossing_chart() -> str:
    rows = read_csv("crossing_summary.csv")
    width, height = 960, 520
    left, top, plot_w, plot_h = 120, 110, 760, 290
    maximum = max(float(row["raw_crossings"]) for row in rows)
    group_w = plot_w / len(rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" rx="18" fill="{BG}"/>',
        svg_text(48, 52, "Raw crossings overstate the sample", fill=INK, font_size=28, font_weight=700),
        svg_text(48, 78, "Crossings separated by more than the 30-minute recross window count as episodes", fill=MUTED, font_size=15),
    ]
    for tick in (0, 10, 20, 30):
        y = top + plot_h - plot_h * tick / maximum
        parts.append(f'<line x1="{left}" y1="{y}" x2="{left + plot_w}" y2="{y}" stroke="{GRID}"/>')
        parts.append(svg_text(left - 14, y + 5, str(tick), fill=MUTED, font_size=13, text_anchor="end"))
    for index, row in enumerate(rows):
        center = left + group_w * (index + 0.5)
        raw = float(row["raw_crossings"])
        episodes = float(row["episodes"])
        raw_h = plot_h * raw / maximum
        episode_h = plot_h * episodes / maximum
        parts.append(f'<rect x="{center - 35}" y="{top + plot_h - raw_h}" width="28" height="{raw_h}" rx="5" fill="{BLUE}"/>')
        parts.append(f'<rect x="{center + 7}" y="{top + plot_h - episode_h}" width="28" height="{episode_h}" rx="5" fill="{ORANGE}"/>')
        parts.append(svg_text(center - 21, top + plot_h - raw_h - 8, f"{raw:.0f}", fill=INK, font_size=13, text_anchor="middle"))
        parts.append(svg_text(center + 21, top + plot_h - episode_h - 8, f"{episodes:.0f}", fill=INK, font_size=13, text_anchor="middle"))
        parts.append(svg_text(center, top + plot_h + 28, row["bucket"], fill=INK, font_size=14, font_weight=600, text_anchor="middle"))
    parts.append(f'<rect x="330" y="458" width="16" height="16" rx="3" fill="{BLUE}"/>')
    parts.append(svg_text(354, 471, "raw crossings", fill=MUTED, font_size=14))
    parts.append(f'<rect x="500" y="458" width="16" height="16" rx="3" fill="{ORANGE}"/>')
    parts.append(svg_text(524, 471, "independent episodes", fill=MUTED, font_size=14))
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "attribution_summary.svg").write_text(attribution_chart(), encoding="utf-8")
    (ASSETS / "crossing_summary.svg").write_text(crossing_chart(), encoding="utf-8")
    print("Wrote assets/attribution_summary.svg and assets/crossing_summary.svg")


if __name__ == "__main__":
    main()
