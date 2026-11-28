from pathlib import Path
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "static" / "sivdhi-logo.jpg"
BACKGROUND = "#faf9fc"
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}


def square_logo(source: Image.Image, size: int) -> Image.Image:
    return ImageOps.fit(
        source.convert("RGB"),
        (size, size),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def save_jpeg(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=95, optimize=True, progressive=True)


def round_logo(source: Image.Image, size: int) -> Image.Image:
    logo = square_logo(source, size)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    canvas = Image.new("RGB", (size, size), BACKGROUND)
    canvas.paste(logo, (0, 0), mask)
    return canvas


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Missing source logo: {SOURCE}")

    with Image.open(SOURCE) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")

    if source.width != source.height:
        raise SystemExit(f"Expected a square source logo, got {source.width}x{source.height}")

    generated = []
    for audience in ("customer", "workpartner"):
        for size in (192, 512):
            path = ROOT / "static" / "pwa" / audience / f"icon-{size}.jpg"
            save_jpeg(square_logo(source, size), path)
            generated.append(path.relative_to(ROOT))

    for flavor in ("customer", "workpartner"):
        for density, size in DENSITIES.items():
            folder = ROOT / "android-apk" / "app" / "src" / flavor / "res" / f"mipmap-{density}"
            standard = folder / "ic_launcher.jpg"
            rounded = folder / "ic_launcher_round.jpg"
            save_jpeg(square_logo(source, size), standard)
            save_jpeg(round_logo(source, size), rounded)
            generated.extend((standard.relative_to(ROOT), rounded.relative_to(ROOT)))

    for path in generated:
        with Image.open(ROOT / path) as image:
            image.verify()
    print(f"Generated and verified {len(generated)} Sivdhi icon assets from {SOURCE.name}.")


if __name__ == "__main__":
    main()
