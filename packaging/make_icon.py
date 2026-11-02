"""Draw the application icon (a page with a watermark being lifted off it).

Writes icon.png and icon.ico next to this script. Run it once; the build
scripts pick the files up if they exist and carry on without them if not.
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
PAPER = (250, 250, 248)
INK = (60, 66, 78)
MARK = (96, 130, 176)
ACCENT = (226, 90, 74)


def draw(size: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 1024.0

    def r(*v):
        return [x * u for x in v]

    # rounded page
    d.rounded_rectangle(r(196, 96, 828, 928), radius=48 * u, fill=PAPER,
                        outline=INK, width=int(20 * u))
    # body text lines
    for i, y in enumerate(range(232, 560, 74)):
        w = 520 if i % 3 else 380
        d.rounded_rectangle(r(280, y, 280 + w, y + 26), radius=13 * u, fill=INK)
    # the watermark, half lifted away: solid on the left, fading to nothing
    for i in range(9):
        x = 280 + i * 52
        alpha = int(235 * (1 - i / 9.0) ** 1.6)
        d.rounded_rectangle(r(x, 690, x + 38, 762), radius=10 * u,
                            fill=MARK + (alpha,))
    # the eraser stroke sweeping across it
    d.line(r(232, 806, 792, 646), fill=ACCENT, width=int(34 * u), joint="curve")
    d.ellipse(r(756, 610, 828, 682), fill=ACCENT)
    return img


def main() -> None:
    img = draw()
    img.save(HERE / "icon.png")
    img.resize((256, 256), Image.LANCZOS).save(
        HERE / "icon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", HERE / "icon.png", "and", HERE / "icon.ico")


if __name__ == "__main__":
    main()
