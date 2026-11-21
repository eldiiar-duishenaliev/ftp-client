#!/usr/bin/env python3
"""Generate the app icon (PNG + .icns) without external assets."""

from __future__ import annotations

import os
import subprocess
import sys

from PIL import Image, ImageDraw

SIZE = 1024
TOP = (64, 148, 255)
BOTTOM = (8, 74, 205)
CORNER = 228


def _gradient() -> Image.Image:
    column = Image.new("RGB", (1, SIZE))
    pixels = column.load()
    for y in range(SIZE):
        ratio = y / (SIZE - 1)
        pixels[0, y] = tuple(
            round(TOP[i] + (BOTTOM[i] - TOP[i]) * ratio) for i in range(3)
        )
    return column.resize((SIZE, SIZE))


def _arrow(draw: ImageDraw.ImageDraw, shaft, head, colour) -> None:
    draw.rounded_rectangle(shaft, radius=34, fill=colour)
    draw.polygon(head, fill=colour)


def build_icon(output_dir: str) -> str:
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SIZE - 1, SIZE - 1], radius=CORNER, fill=255
    )
    icon.paste(_gradient(), (0, 0), mask)

    draw = ImageDraw.Draw(icon)
    shadow = (6, 42, 120, 70)
    white = (255, 255, 255, 245)

    # Up arrow (upload), left side.
    _arrow(
        draw,
        (338, 428, 468, 720),
        [(250, 438), (556, 438), (403, 246)],
        shadow,
    )
    _arrow(
        draw,
        (326, 416, 456, 708),
        [(238, 426), (544, 426), (391, 234)],
        white,
    )

    # Down arrow (download), right side.
    _arrow(
        draw,
        (568, 330, 698, 622),
        [(510, 606), (816, 606), (663, 798)],
        shadow,
    )
    _arrow(
        draw,
        (556, 318, 686, 610),
        [(498, 594), (804, 594), (651, 786)],
        white,
    )

    os.makedirs(output_dir, exist_ok=True)
    png_path = os.path.join(output_dir, "icon.png")
    icon.save(png_path, "PNG")

    iconset = os.path.join(output_dir, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)
    variants = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in variants.items():
        icon.resize((size, size), Image.LANCZOS).save(
            os.path.join(iconset, name), "PNG"
        )

    icns_path = os.path.join(output_dir, "AppIcon.icns")
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", iconset, "-o", icns_path],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        icns_path = png_path
    return icns_path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    print(build_icon(target))
