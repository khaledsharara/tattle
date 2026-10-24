"""Draw the home-screen icon (apple-touch-icon.png) from the shapes in public/favicon.svg.

iPhones ignore SVG icons for "Add to Home Screen" and show a letter instead,
so they need a PNG. iOS rounds the corners itself and turns transparency
black, so the background fills the whole square.

    python scripts/make_touch_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 180
SUPERSAMPLE = 4
GRID = 32  # favicon.svg's viewBox
BACKGROUND = "#16181d"
BUBBLE = "#f0efec"
# The speech bubble: its body (x, y, width, height, corner radius) and its tail, as in favicon.svg.
BODY = (5, 6, 22, 17, 4)
TAIL = [(9, 22.5), (14.5, 22.5), (9, 27.5)]
# Bars inside it: (x, y, width, height, corner radius, colour) on the 32-unit grid.
BARS = [
    (9.5, 15, 3.2, 4, 1, BACKGROUND),
    (14.4, 12.5, 3.2, 6.5, 1, BACKGROUND),
    (19.3, 9.5, 3.2, 9.5, 1, "#3987e5"),
]


def draw(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    scale = big / GRID
    image = Image.new("RGB", (big, big), BACKGROUND)
    pen = ImageDraw.Draw(image)

    def box(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        return (x * scale, y * scale, (x + w) * scale - 1, (y + h) * scale - 1)

    x, y, w, h, r = BODY
    pen.rounded_rectangle(box(x, y, w, h), radius=r * scale, fill=BUBBLE)
    pen.polygon([(px * scale, py * scale) for px, py in TAIL], fill=BUBBLE)
    for x, y, w, h, r, colour in BARS:
        pen.rounded_rectangle(box(x, y, w, h), radius=r * scale, fill=colour)
    return image.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "public" / "apple-touch-icon.png"
    draw(SIZE).save(target, optimize=True)
    print(f"wrote {target}")
