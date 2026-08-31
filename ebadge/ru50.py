"""The "RU50" proprietary image container -- the format this badge's firmware
(JieLi-derived, board "LJ733") most likely actually expects, instead of the
plain RGB565 this project started with.

## Why this exists

Every real BW03 upload attempt we ran completed all 576 chunks (a 240x240x2
= 115200-byte raw RGB565 file at 200 bytes/chunk) and then had the `finish`
step rejected instantly with status=1 ("check failed"), regardless of
whether the finish payload was 4 bytes (sum only) or 8 bytes (length+sum).
Trying both byte-formats and getting the identical instant rejection both
times pointed away from "wrong finish framing" and towards "the uploaded
*content* itself is structurally wrong", independent of how its checksum is
framed.

On 2026-05-02/03, DynamicDevices/lcd-badge-ble gained a commit
("protocol fixes for DG01/LJ733 firmware variant", PR #3 from contributor
jackghx) explicitly tested against our exact board: "model LJ733, firmware
V32399. Upload reaches 99% successfully. Remaining blocker is RU50
proprietary image format." The same repo's `third_party/bmpconvert_extract/
tools/ru50_convert.py` documents that format, reverse-engineered from the
vendor's own Android JNI image converter (`BmpConvert 1.6.0`, JieLi
`libjl_bmp_convert.so`): a small fixed header (magic "RU50", constants
specific to that vendor tool, width/height, payload length) wrapping an
**ETC2-compressed texture**, not raw RGB565 -- plus two CRC16 checksums
(vendor nibble-table algorithm, not the whole-file u32 sum we'd been
computing) covering the payload and part of the header.

This is a strong, evidence-based hypothesis, not a confirmed fix: the
upstream project's own tracking issue (#2, "blocked on image format (RU50)")
is still open, and nobody has reported one of these badges actually
displaying a custom image successfully yet. Treat `--format ru50` as the
most promising thing to try next, not a guarantee.

## Byte layout (from `ru50_convert.py`, ported line-for-line)

    offset   bytes  content
    0x00     4      magic "RU50" as little-endian u32 (0x30355552)
    0x04     8      constant (vendor tool internals, meaning unknown)
    0x0C     4      zero
    0x10     8      constant 0x1800000000
    0x18     8      constant (vendor tool internals)
    0x20     8      constant (vendor tool internals)
    0x28     8      constant (vendor tool internals)
    0x30     8      constant (vendor tool internals)
    0x38     4      constant 0x400
    0x3C     4      flags word, fixed 0x00920001
    0x40     4      (crc_header << 16) | crc_payload, both u16
    0x44     2      width (u16 LE)
    0x46     2      height (u16 LE)
    0x4C     4      payload length (u32 LE)
    0x14     1024   reserved, zero-filled
    0x450    N      ETC2-compressed RGB payload (see `scratch_bytes`)

`crc_payload` is the vendor CRC16 over the compressed payload bytes.
`crc_header` is the vendor CRC16 over an 18-byte slice built from the flags
word, width, height, payload length, and crc_payload (with a trailing zero
u16 in place of itself) -- see `_header_crc_slice`.
"""
from __future__ import annotations

import struct

from PIL import Image

from .image import fit_image

MAGIC_RU50 = 0x30355552
HEADER_RESERVED_OFF = 0x14
HEADER_RESERVED_LEN = 0x400
PAYLOAD_OFF = 0x450
HDR_QW_04 = 0x0000000100050100
HDR_QW_18 = 0x54000100000030
HDR_QW_20 = 0x3C00000000
HDR_QW_28 = 0x500001
HDR_QW_30 = 0x5000000100
HDR_DW_38 = 0x400
HDR_FLAGS = 0x00920001

# Vendor Crc16 nibble table -- extracted from BmpConvert 1.6.0 (x86_64 .rodata
# @ 0x9460) by DynamicDevices/lcd-badge-ble. 256 little-endian u16 values
# (512 bytes). Copied verbatim; do not "simplify" this into a standard CRC16
# variant -- it's a vendor-specific table, not CRC16-CCITT or -MODBUS.
_CRC16_TABLE_HEX = (
    "00002110422063308440a550c660e770088129914aa16bb18cc1add1cee1eff1020000000300"
    "00000100000000000000f8fffffffeffffff0200000008000000f8fffffffeffffff02000000"
    "08000000effffffffbffffff0500000011000000effffffffbffffff0500000011000000e3ff"
    "fffff7ffffff090000001d000000e3fffffff7ffffff090000001d000000d6fffffff3ffffff"
    "0d0000002a000000d6fffffff3ffffff0d0000002a000000c4ffffffeeffffff120000003c00"
    "0000c4ffffffeeffffff120000003c000000b0ffffffe8ffffff1800000050000000b0ffffff"
    "e8ffffff180000005000000096ffffffdfffffff210000006a00000096ffffffdfffffff2100"
    "00006a00000049ffffffd1ffffff2f000000b700000049ffffffd1ffffff2f000000b7000000"
    "00000101ffffffff00004043000088418716993e08080808727272720000003fc39d0b3d0e0e"
    "0e0ec0803e4aaeb9333ed578e93d040404040000803da245163fe79c03414e0c893d1e1e1e1e"
    "ffff0000023c01005a3c01003d3c01009d3c0100623c0100b33c0100773c0100023c0100453f"
    "01001d3f0100253f0100433f01002f3f0100353f01003d3f01002d3f01002042010037420100"
    "244201004d420100314201003f420100474201002e42010020450100f8440100004501001e45"
    "01000a450100104501001845010008450100"
)
CRC16_TABLE: bytes = bytes.fromhex(_CRC16_TABLE_HEX)
assert len(CRC16_TABLE) == 512, f"vendor CRC16 table must be 512 bytes, got {len(CRC16_TABLE)}"
_CRC16_TABLE_U16 = struct.unpack("<256H", CRC16_TABLE)


class Ru50Error(RuntimeError):
    pass


def crc16(data: bytes) -> int:
    """Vendor nibble-wise CRC16 (`Crc16` in BmpConvert). Processes each byte
    as two nibbles, high nibble first.
    """
    crc = 0
    tbl = _CRC16_TABLE_U16
    for byte in data:
        for nibble in ((byte >> 4) & 0x0F, byte & 0x0F):
            idx = ((crc >> 12) ^ nibble) & 0x0F
            crc = ((crc << 4) & 0xFFFF) ^ tbl[idx]
    return crc & 0xFFFF


def scratch_bytes(width: int, height: int) -> int:
    """Expected ETC2 payload size for a `width`x`height` image (4 bits/pixel,
    rounded to the vendor's block layout). For our badge's 240x240 screen:
    ((240*2+6) & ~7) * ((240+3)//4) = 480 * 60 = 28800 bytes -- versus
    115200 bytes for the same frame as raw RGB565 (exactly 4x smaller, which
    matches ETC2's usual 4bpp rate for an RGB-only format).
    """
    return ((width * 2 + 6) & ~7) * ((height + 3) // 4)


def _bgra_bytes(im: Image.Image) -> bytes:
    """etcpak's ETC-family compressors expect BGRA-ordered bytes (see the
    K0lb3/etcpak README) -- build that by swapping R/B channels and adding a
    fully-opaque alpha, then packing as RGBA (so the byte order on the wire
    is B,G,R,A).
    """
    rgb = im.convert("RGB")
    r, g, b = rgb.split()
    a = Image.new("L", rgb.size, 255)
    return Image.merge("RGBA", (b, g, r, a)).tobytes("raw", "RGBA")


def compress_etc2(im: Image.Image, width: int, height: int) -> bytes:
    """Compress `im` (already exactly width x height) to an ETC2 RGB8
    payload matching what the vendor's native encoder would produce.
    """
    try:
        import etcpak
    except ImportError as exc:
        raise Ru50Error(
            "the 'etcpak' package is required for --format ru50 (ETC2 texture "
            "compression); install it with `uv add etcpak` or `pip install "
            "etcpak --break-system-packages`, or fall back to --format rgb565"
        ) from exc
    if im.size != (width, height):
        raise Ru50Error(f"image must already be exactly {width}x{height}, got {im.size}")
    payload = etcpak.compress_etc2_rgb(_bgra_bytes(im), width, height)
    expected = scratch_bytes(width, height)
    if len(payload) != expected:
        raise Ru50Error(f"ETC2 payload length mismatch: got {len(payload)}, expected {expected} for {width}x{height}")
    return payload


def _header_crc_slice(width: int, height: int, payload_len: int, crc_payload: int) -> bytes:
    """18 bytes fed to the second CRC16 pass (header integrity check), with
    its own would-be checksum field zeroed out.
    """
    out = bytearray(18)
    struct.pack_into("<I", out, 0, HDR_FLAGS)
    struct.pack_into("<HH", out, 4, width & 0xFFFF, height & 0xFFFF)
    struct.pack_into("<I", out, 8, payload_len)
    struct.pack_into("<HH", out, 12, crc_payload & 0xFFFF, 0)
    return bytes(out)


def build_ru50_blob(width: int, height: int, payload: bytes, *, zero_unknown_fields: bool = False) -> bytes:
    """Wrap an already-ETC2-compressed `payload` in the full RU50 container.

    NOTE on a bug in the source this was ported from: `ru50_convert.py`
    writes the header fields (flags/CRC at 0x3C, width/height at 0x44,
    payload length at 0x4C) and only *afterwards* zero-fills the "reserved"
    region at [0x14, 0x14+0x400) = [0x14, 0x414) -- which overlaps every one
    of those fields (they all fall inside [0x14, 0x50)) and so would zero
    them right back out. `bytearray(total)` is already zero-initialized, so
    the explicit zero-fill is redundant anyway; here it runs FIRST (a no-op
    against the fresh buffer) and the real fields are written after, so they
    survive. This preserves the field *values* the original script clearly
    intended to produce -- it's the write order that looks like a copy/paste
    slip, not the field layout itself.

    `zero_unknown_fields`: DIAGNOSTIC ONLY. HDR_QW_04/18/20/28/30 and
    HDR_DW_38 are constants with genuinely unknown meaning -- lifted
    verbatim from one specific extraction of the vendor's BmpConvert 1.6.0
    binary (see the module docstring), not derived from any spec. A real
    upload with them at their captured values (2026-08-31) reached 100% of
    the chunks but got total silence on `finish`, even after padding the
    total length to rule out a byte-count mismatch -- so the leading
    remaining suspect is that something about the header content itself
    (most likely one of these unexplained constants) sends the firmware's
    RU50 parser down a path it never returns an ack from. Setting this to
    True leaves those 6 fields as zero instead, to test whether they're
    truly fixed requirements or artifacts specific to whatever file the
    extraction was taken from. This is a guess, not a confirmed fix --
    see BITACORA.md's 2026-08-31 entries.
    """
    crc_payload = crc16(payload)
    crc_header = crc16(_header_crc_slice(width, height, len(payload), crc_payload))

    total = PAYLOAD_OFF + len(payload)
    buf = bytearray(total)
    buf[HEADER_RESERVED_OFF : HEADER_RESERVED_OFF + HEADER_RESERVED_LEN] = bytes(HEADER_RESERVED_LEN)
    struct.pack_into("<I", buf, 0x00, MAGIC_RU50)
    if not zero_unknown_fields:
        struct.pack_into("<Q", buf, 0x04, HDR_QW_04)
        struct.pack_into("<I", buf, 0x0C, 0)
        struct.pack_into("<Q", buf, 0x10, 0x1800000000)
        struct.pack_into("<Q", buf, 0x18, HDR_QW_18)
        struct.pack_into("<Q", buf, 0x20, HDR_QW_20)
        struct.pack_into("<Q", buf, 0x28, HDR_QW_28)
        struct.pack_into("<Q", buf, 0x30, HDR_QW_30)
        struct.pack_into("<I", buf, 0x38, HDR_DW_38)
    struct.pack_into("<I", buf, 0x4C, len(payload))
    struct.pack_into("<HH", buf, 0x44, width & 0xFFFF, height & 0xFFFF)
    w1 = ((crc_header & 0xFFFF) << 16) | (crc_payload & 0xFFFF)
    struct.pack_into("<II", buf, 0x3C, HDR_FLAGS, w1)
    buf[PAYLOAD_OFF:] = payload
    return bytes(buf)


def image_to_ru50(path: str, width: int, height: int, *, fit: str = "cover", zero_unknown_fields: bool = False) -> bytes:
    """Open, fit, ETC2-compress, and wrap an image file as an RU50 blob."""
    with Image.open(path) as im:
        fitted = fit_image(im, width, height, fit=fit)
    payload = compress_etc2(fitted, width, height)
    return build_ru50_blob(width, height, payload, zero_unknown_fields=zero_unknown_fields)


def solid_ru50(width: int, height: int, color: tuple[int, int, int] = (0, 0, 0), *, zero_unknown_fields: bool = False) -> bytes:
    """Build an RU50 blob for a plain solid color, e.g. to blank the screen."""
    im = Image.new("RGB", (width, height), color)
    payload = compress_etc2(im, width, height)
    return build_ru50_blob(width, height, payload, zero_unknown_fields=zero_unknown_fields)
