"""Generate assets/icon.ico for the Plex Converter app."""
from pathlib import Path


def make_icon(output: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    # Draw at 256x256 then let Pillow downsample for the ICO's smaller sizes.
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    m = 8
    draw.rounded_rectangle(
        [m, m, size - m - 1, size - m - 1],
        radius=44,
        fill=(30, 100, 180, 255),
    )

    font_size = 158
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    for path in ("arial.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except OSError:
            pass
    else:
        font = ImageFont.load_default(size=font_size)

    letter = "P"
    bbox = draw.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), letter, fill=(255, 255, 255, 255), font=font)

    # Pillow's ICO saver accepts a single image + sizes= to generate all layers.
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(
        str(output),
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)],
    )
    kb = output.stat().st_size // 1024
    print(f"Icon written: {output} ({kb} KB)")


if __name__ == "__main__":
    make_icon(Path(__file__).parent / "assets" / "icon.ico")
