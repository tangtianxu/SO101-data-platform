"""Build the Windows shortcut icon on E: using the existing Pillow install."""
from pathlib import Path
import os

from PIL import Image, ImageDraw, ImageFont


def main():
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (8 * scale, 8 * scale, 248 * scale, 248 * scale),
        radius=52 * scale,
        fill="#1c2b3d",
    )
    font_path=Path(os.environ.get('WINDIR',''))/'Fonts/segoeuib.ttf'
    font = ImageFont.truetype(str(font_path), 172 * scale) if font_path.is_file() else ImageFont.load_default(size=172*scale)
    draw.text((128 * scale, 183 * scale), "S", font=font, anchor="ms", fill="white")
    draw.rounded_rectangle(
        (178 * scale, 194 * scale, 216 * scale, 208 * scale),
        radius=7 * scale,
        fill="#27a895",
    )
    image = image.resize((256, 256), Image.Resampling.LANCZOS)
    image.save(assets / "so101.png")
    image.save(
        assets / "so101.ico",
        sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)],
    )
    with Image.open(assets / "so101.ico") as icon:
        sizes = sorted(icon.ico.sizes())
        assert sizes == [(size, size) for size in (16, 24, 32, 48, 64, 128, 256)]
    print(f"Built {assets / 'so101.ico'}: {sizes}")


if __name__ == "__main__":
    main()
