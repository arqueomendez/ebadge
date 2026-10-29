"""BG02/LJ733 JPEG transfer, verified against a successful SuperBand capture."""
import io
import struct
from pathlib import Path
from PIL import Image, ImageOps
from .image import fit_image
from .protocol import get_protocol


def encode(
    path: str | Path | None,
    width: int,
    height: int,
    *,
    fit: str = "cover",
    quality: int = 85,
    solid: tuple[int, int, int] | None = None,
) -> bytes:
    if solid is not None:
        im = Image.new('RGB', (width, height), solid)
    else:
        with Image.open(path) as source:
            im = fit_image(ImageOps.exif_transpose(source), width, height, fit=fit)
    out = io.BytesIO()
    im.save(out, format='JPEG', quality=quality, progressive=False)
    data = out.getvalue()
    return struct.pack('>I', len(data)) + data


def start(length: int) -> bytes:
    # Captured: position=0, custom=0, mid4=15a20208, RGB=0/0/0.
    payload = bytes.fromhex('000015a20208000000') + struct.pack('>I', length) + bytes(4)
    return get_protocol(31, 2, payload)


def chunk(sequence: int, data: bytes) -> bytes:
    body = struct.pack('>H', sequence) + data
    return get_protocol(31, 1, body + struct.pack('>I', sum(body) & 0xffffffff))


def ack(frame: bytes) -> bytes:
    return bytes.fromhex('dc0005') + bytes([frame[3], frame[5]]) + struct.pack('>H', len(frame)) + b'\x01'
