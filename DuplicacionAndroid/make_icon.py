#!/usr/bin/env python3
"""Solo para el build: PNG 1024×1024 (teléfono + reflejo, estilo Duplicación Android)."""

import sys
from pathlib import Path


def main() -> None:
    out = Path(sys.argv[1])
    from PIL import Image, ImageDraw

    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = int(size * 0.08)
    r = int(size * 0.18)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=r,
        fill=(46, 142, 79, 255),
        outline=(255, 255, 255, 200),
        width=max(3, size // 128),
    )
    mx = int(size * 0.28)
    my = int(size * 0.22)
    mw = size - 2 * mx
    mh = int(size * 0.56)
    draw.rounded_rectangle(
        [mx, my, mx + mw, my + mh],
        radius=int(size * 0.04),
        fill=(12, 52, 28, 255),
    )
    notch_w = int(mw * 0.22)
    notch_h = int(size * 0.022)
    nx = mx + (mw - notch_w) // 2
    ny = my + int(size * 0.018)
    draw.rounded_rectangle(
        [nx, ny, nx + notch_w, ny + notch_h],
        radius=notch_h // 2,
        fill=(26, 90, 48, 255),
    )

    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    rx = mx + int(mw * 0.52)
    ry = my + int(mh * 0.12)
    rw = int(mw * 0.38)
    rh = int(mh * 0.68)
    od.rounded_rectangle(
        [rx, ry, rx + rw, ry + rh],
        radius=int(size * 0.032),
        fill=(255, 255, 255, 70),
        outline=(255, 255, 255, 140),
        width=max(2, size // 220),
    )
    img = Image.alpha_composite(img, overlay)
    img.save(out, "PNG")


if __name__ == "__main__":
    main()
