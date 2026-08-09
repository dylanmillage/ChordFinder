"""Generate the ChordFinder app icons (home-screen / favicon).

A chord diagram in the app's own colours: light nut bar, muted grid,
accent-orange finger dots. Legible down to ~32px.

    python cloud/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "public"
BG = "#0f1014"
GRID = "#3a3f4d"
NUT = "#e2e4ea"
DOT = "#ffb454"

# Em-ish shape: (string index 0-5, fret 1-4); None = open/muted marker skipped
DOTS = [(1, 2), (2, 2)]


def draw_icon(size: int, padding_ratio: float = 0.18) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    pad = size * padding_ratio
    w = size - 2 * pad                     # grid width
    h = size - 2 * pad - size * 0.06       # leave room under the nut
    x0, y0 = pad, pad + size * 0.06
    strings, frets = 6, 4
    sx = w / (strings - 1)
    sy = h / frets
    line = max(1, round(size * 0.012))

    # nut (thick bar across the top)
    d.rectangle([x0 - line, y0 - size * 0.045, x0 + w + line, y0 - size * 0.045 + line * 2.6],
                fill=NUT)
    for s in range(strings):
        x = x0 + s * sx
        d.line([(x, y0), (x, y0 + h)], fill=GRID, width=line)
    for f in range(frets + 1):
        y = y0 + f * sy
        d.line([(x0, y), (x0 + w, y)], fill=GRID, width=line)

    r = size * 0.062
    for s, f in DOTS:
        cx = x0 + s * sx
        cy = y0 + (f - 0.5) * sy
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOT)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in [("icon-192.png", 192), ("icon-512.png", 512),
                       ("apple-touch-icon.png", 180), ("favicon-32.png", 32)]:
        # tiny sizes need less padding to stay readable
        img = draw_icon(size, 0.12 if size <= 32 else 0.18)
        img.save(OUT / name)
        print(f"  wrote {name} ({size}x{size})")


if __name__ == "__main__":
    main()
