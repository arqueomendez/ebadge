"""Convert an ordinary image file into the pixel payload the badge wants.

Two target formats are supported (see ebadge/ru50.py for why there are two):

- RGB565: 16-bit little-endian per pixel, no header -- what we assumed the
  badge wanted originally, straight from PROTOCOL.md's description.
- RU50: the JieLi/BmpConvert proprietary container (magic + fixed header +
  ETC2-compressed texture + two CRC16s) that a 2026-05 fix to
  DynamicDevices/lcd-badge-ble (tested on our exact "LJ733" board) points to
  as what the real SuperBand app actually uploads. See ru50.py's module
  docstring for the full story; this module only handles getting a source
  image file into the right (width, height) PIL Image for either encoder.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image


def fit_image(im: Image.Image, width: int, height: int, *, fit: str = "cover") -> Image.Image:
    """Resize/crop `im` to exactly (width, height) in RGB mode.

    fit="cover" (default): scale to fill the frame and center-crop the
    overflow -- the natural choice for a round watchface. fit="contain":
    scale to fit entirely inside the frame, padding with black. fit="stretch":
    resize without preserving aspect ratio.
    """
    im = im.convert("RGB")
    if fit == "cover":
        return _resize_cover(im, width, height)
    if fit == "contain":
        return _resize_contain(im, width, height)
    if fit == "stretch":
        return im.resize((width, height))
    raise ValueError(f"unknown fit mode {fit!r}")


def load_fitted_image(path: str | Path, width: int, height: int, *, fit: str = "cover") -> Image.Image:
    """Open `path` and fit it to (width, height); the shared first step for
    both `image_to_rgb565` and `ebadge.ru50.image_to_ru50`.
    """
    with Image.open(path) as im:
        return fit_image(im, width, height, fit=fit)


def image_to_rgb565(path: str | Path, width: int, height: int, *, fit: str = "cover") -> bytes:
    """Resize/crop `path` to (width, height) and pack as little-endian RGB565."""
    im = load_fitted_image(path, width, height, fit=fit)
    return _encode_rgb565(im, width, height)


def solid_rgb565(width: int, height: int, color: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """Build a plain solid-color RGB565 frame, e.g. to blank out whatever the
    badge is currently showing without needing a source image file.
    """
    return _encode_rgb565(Image.new("RGB", (width, height), color), width, height)


def _encode_rgb565(im: Image.Image, width: int, height: int) -> bytes:
    try:
        import numpy as np

        arr = np.asarray(im, dtype=np.uint16)  # HxWx3, values 0..255
        r = (arr[:, :, 0] & 0xF8) << 8
        g = (arr[:, :, 1] & 0xFC) << 3
        b = arr[:, :, 2] >> 3
        value = (r | g | b).astype("<u2")  # little-endian u16
        return value.tobytes()
    except ImportError:
        return _pack_rgb565_pure_python(im, width, height)


def _pack_rgb565_pure_python(im: Image.Image, width: int, height: int) -> bytes:
    out = bytearray(width * height * 2)
    pixels = im.load()
    i = 0
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[i] = value & 0xFF
            out[i + 1] = (value >> 8) & 0xFF
            i += 2
    return bytes(out)


def _resize_cover(im: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = im.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return im.crop((left, top, left + width, top + height))


def _resize_contain(im: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = im.size
    scale = min(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(im, ((width - new_w) // 2, (height - new_h) // 2))
    return canvas
