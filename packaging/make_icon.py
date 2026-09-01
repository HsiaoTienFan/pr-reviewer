"""Render the PR Reviewer app icon with no third-party dependencies.

Draws a 1024px master PNG (rounded squircle, diff gutter, add/remove rows)
using signed-distance fields for anti-aliasing, then lets `sips`/`iconutil`
produce the .icns. Usage: python3 make_icon.py <output-dir>
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SIZE = 1024

# palette
BG_TOP = (0.40, 0.42, 0.93)
BG_BOTTOM = (0.17, 0.16, 0.48)
NEUTRAL = (0.82, 0.86, 0.94)
GREEN = (0.29, 0.78, 0.36)
RED = (0.97, 0.38, 0.35)

Color = tuple[float, float, float]


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = (x - edge0) / (edge1 - edge0)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


def rounded_rect_sdf(px: float, py: float, cx: float, cy: float,
                     half_w: float, half_h: float, radius: float) -> float:
    """Signed distance to a rounded rectangle; negative inside."""
    dx = abs(px - cx) - (half_w - radius)
    dy = abs(py - cy) - (half_h - radius)
    ax, ay = max(dx, 0.0), max(dy, 0.0)
    return math.hypot(ax, ay) + min(max(dx, dy), 0.0) - radius


def blend(dst: tuple[float, float, float, float], src: Color,
          alpha: float) -> tuple[float, float, float, float]:
    """Source-over compositing onto a straight-alpha destination."""
    if alpha <= 0.0:
        return dst
    dr, dg, db, da = dst
    sr, sg, sb = src
    out_a = alpha + da * (1.0 - alpha)
    if out_a <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    r = (sr * alpha + dr * da * (1.0 - alpha)) / out_a
    g = (sg * alpha + dg * da * (1.0 - alpha)) / out_a
    b = (sb * alpha + db * da * (1.0 - alpha)) / out_a
    return (r, g, b, out_a)


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter type: none
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def render(size: int) -> bytearray:
    s = float(size)
    aa = s / 1024.0 * 1.6  # anti-alias width in px

    # squircle body
    margin = 0.076 * s
    half = (s - 2 * margin) / 2.0
    centre = s / 2.0
    corner = 0.224 * s

    # gutter divider
    div_x, div_w = 0.293 * s, 0.008 * s
    div_top, div_bot = 0.255 * s, 0.762 * s

    bar_h = 0.070 * s
    bar_r = bar_h / 2.0
    stub_x, stub_w = 0.163 * s, 0.082 * s
    bar_x = 0.330 * s

    # (centre-y, width, colour) — two neutral context rows framing add/remove
    rows = [
        (0.313 * s, 0.365 * s, NEUTRAL),
        (0.443 * s, 0.430 * s, GREEN),
        (0.573 * s, 0.335 * s, RED),
        (0.703 * s, 0.268 * s, NEUTRAL),
    ]

    pixels = bytearray(size * size * 4)
    for y in range(size):
        py = y + 0.5
        row_base = y * size * 4
        # vertical gradient sampled once per row
        t = py / s
        bg = (BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t,
              BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t,
              BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)

        for x in range(size):
            px = x + 0.5
            body = rounded_rect_sdf(px, py, centre, centre, half, half, corner)
            body_a = 1.0 - smoothstep(-aa, aa, body)
            if body_a <= 0.0:
                continue

            acc: tuple[float, float, float, float] = blend((0.0, 0.0, 0.0, 0.0), bg, body_a)

            # soft top highlight for a little depth
            hi = (1.0 - smoothstep(0.0, 0.42 * s, py)) * 0.13 * body_a
            acc = blend(acc, (1.0, 1.0, 1.0), hi)

            # gutter divider
            d = rounded_rect_sdf(px, py, div_x, (div_top + div_bot) / 2.0,
                                 div_w / 2.0, (div_bot - div_top) / 2.0, div_w / 2.0)
            acc = blend(acc, (1.0, 1.0, 1.0), (1.0 - smoothstep(-aa, aa, d)) * 0.26 * body_a)

            for cy, width, colour in rows:
                stub = rounded_rect_sdf(px, py, stub_x + stub_w / 2.0, cy,
                                        stub_w / 2.0, bar_h / 2.0, bar_r)
                a = (1.0 - smoothstep(-aa, aa, stub)) * body_a
                if a > 0.0:
                    dim = 0.55 if colour is NEUTRAL else 1.0
                    acc = blend(acc, colour, a * dim)

                bar = rounded_rect_sdf(px, py, bar_x + width / 2.0, cy,
                                       width / 2.0, bar_h / 2.0, bar_r)
                a = (1.0 - smoothstep(-aa, aa, bar)) * body_a
                if a > 0.0:
                    acc = blend(acc, colour, a * (0.80 if colour is NEUTRAL else 1.0))

            i = row_base + x * 4
            pixels[i] = int(max(0.0, min(1.0, acc[0])) * 255 + 0.5)
            pixels[i + 1] = int(max(0.0, min(1.0, acc[1])) * 255 + 0.5)
            pixels[i + 2] = int(max(0.0, min(1.0, acc[2])) * 255 + 0.5)
            pixels[i + 3] = int(max(0.0, min(1.0, acc[3])) * 255 + 0.5)
    return pixels


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "build")
    out_dir.mkdir(parents=True, exist_ok=True)

    master = out_dir / "icon_1024.png"
    print(f"rendering {SIZE}x{SIZE} master ...")
    write_png(master, SIZE, SIZE, render(SIZE))

    iconset = out_dir / "AppIcon.iconset"
    if iconset.exists():
        for f in iconset.iterdir():
            f.unlink()
    iconset.mkdir(parents=True, exist_ok=True)

    # (pixel size, filename) per Apple's iconset naming
    variants = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    for px, name in variants:
        subprocess.run(["sips", "-z", str(px), str(px), str(master),
                        "--out", str(iconset / name)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    icns = out_dir / "AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    print(f"wrote {icns} ({icns.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
