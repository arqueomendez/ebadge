"""The "RU50" proprietary image container used by this badge's firmware
(JieLi-derived, board "LJ733") instead of the plain RGB565 this project
started with.

## Status as of 2026-08-31: layout independently confirmed by disassembly

Everything in this module was rewritten on 2026-08-31 after directly
disassembling the REAL vendor library -- not trusting a second-hand port
anymore. Earlier revisions of this file were ported from
`DynamicDevices/lcd-badge-ble`'s `third_party/bmpconvert_extract/tools/
ru50_convert.py`, whose own header-field offsets turned out to be
partially wrong (see "History" below). This version was built by:

1. Cloning `Jieli-Tech/Android-JL_Bluetooth` (the vendor's own public
   Android SDK repo) and extracting
   `libs/BmpConvert_V1.6.0_10605-release.aar` (a plain zip) to get
   `jni/x86_64/libjl_bmp_convert.so` -- the actual native library Android
   apps (including, presumably, SuperBand/FitPro's own BmpConvert use)
   ship and call.
2. Disassembling it (`objdump -d`, `readelf`) and reading the real x86_64
   machine code of `br35_bmp_to_res` (the encoder path used by JieLi's
   "707N" chip line / TYPE_707N_* / high-res packed dial blobs -- the
   family this RU50 tag belongs to) and `Crc16`, byte for byte.
3. Cross-checking the vendor's 512-byte CRC16 nibble table by dumping
   `.rodata` at file offset 0x9460 directly from this .so (confirmed
   byte-identical to what the earlier port already had -- that part was
   right) and by re-deriving the CRC16 algorithm from `Crc16`'s
   disassembly (confirmed: process each byte high-nibble-then-low-nibble,
   `crc = ((crc << 4) & 0xFFFF) ^ table[((crc >> 12) ^ nibble) & 0xF]`,
   exactly what was already implemented here).

## What was actually wrong before

The earlier port had the right general shape (magic + fixed header +
ETC2 payload + two CRC16s) and most of the "constant, meaning unknown"
qwords were byte-for-byte correct, but got two things wrong that this
rewrite fixes:

- **`HDR_QW_04` was wrong.** Real disassembly:
  `movabs rax, 0x0000000100010500` written at offset 0x04. The earlier
  port had `0x0000000100050100` -- the two middle 16-bit halves were
  swapped.
- **The field layout from 0x3C onward was wrong**, and this is the more
  likely explanation for the silent `finish` hangs seen against real
  hardware: the real code writes, in order, `crc_header` (u16) at 0x3C,
  `crc_payload` (u16) at 0x3E, the flags word (u32) at 0x40, width (u16)
  at 0x44, height (u16) at 0x46, **the actual ETC2 payload length** (u32)
  at 0x48, and then a **fixed constant equal to the header size (0x450)**
  -- NOT the payload length again -- at 0x4C. The earlier port put flags
  at 0x3C, a packed `(crc_header<<16)|crc_payload` word at 0x40, and
  (wrongly) the payload length at 0x4C instead of the 0x450 constant.
  If the on-device parser mirrors this same layout (which, given
  everything else here matches disassembly exactly, seems likely), every
  RU50 blob this project uploaded before this fix had its declared
  payload length in the wrong byte position and a bogus "length" value
  where the firmware expected a fixed marker -- plausibly enough to make
  a parser wait for a very different amount of data than we ever sent,
  which matches the observed symptom (chunks all ack normally, then
  total, indefinite silence at `finish` -- never an explicit reject).
- **The "reserved" zero-filled region is at `[0x50, 0x450)`, not
  `[0x14, 0x414)`.** The real code `memset`s a 0x400-byte scratch buffer
  and `memcpy`s it to output+0x50 (right after the last populated header
  field, which now ends cleanly at 0x50), immediately followed by the
  ETC2 payload at 0x450. The earlier port's `[0x14, 0x414)` span
  overlapped several populated fields; our own fix for that (write
  fields first, redundant-zero first) happened to avoid clobbering them,
  but it was still zeroing the wrong span for the wrong reason -- there
  never was a real overlap in the vendor's own code once the fields are
  in their correct final positions, because they now end exactly where
  the reserved region begins.
- **The 18-byte slice fed into the second `Crc16` call (`crc_header`) is
  different too.** Real order: `crc_payload` (u16), flags (u32), width
  (u16), height (u16), payload length (u32), the 0x450 constant (u32) --
  18 bytes total, all real values, no zero padding. The earlier port
  started with flags and ended with a zeroed placeholder u16 instead of
  the 0x450 constant.

None of the other constants changed: `MAGIC_RU50`, `HDR_QW_18/20/28/30`,
`HDR_DW_38`, the CRC16 table, and `PAYLOAD_OFF` (0x450) were all
confirmed byte-identical against the real disassembly.

One thing this rewrite does NOT resolve: the flags word at 0x40. The
disassembly shows it's actually computed as `(A + B + 1)` from a pair of
constants chosen by a branch on the image's pixel format / a buffer-size
comparison -- `(0x900000, 0x20000) -> 0x00920001` on one path, or
`(0x200000, 0x28000) -> 0x00228001` on another. Every real-hardware test
this project has run used `0x00920001` (`HDR_FLAGS` below) without any
evidence it was the wrong branch, so it stays as the default -- but if a
real-hardware test with the fixes above still hangs, trying
`0x00228001` next is a well-motivated next guess, not a blind one.

## History (why earlier versions of this file looked different)

On 2026-05-02/03, `DynamicDevices/lcd-badge-ble` gained a commit
("protocol fixes for DG01/LJ733 firmware variant", PR #3 from contributor
jackghx) explicitly tested against our exact board: "model LJ733, firmware
V32399. Upload reaches 99% successfully. Remaining blocker is RU50
proprietary image format." That commit's transport evidence (17-byte
start, 4-byte finish sum) is real, from an iOS sysdiagnose capture on
identical hardware -- but that same author says the RU50 image body itself
remained unsolved even with that capture in hand. Three days later, a
different contributor added `ru50_convert.py` (co-authored by an AI coding
agent, "Cursor"), claiming to reverse-engineer the same vendor library --
but without committing the library itself, referencing a spec file that
was never committed either, and (per a 2026-08-31 audit of that project's
full git history) shipping a header-writing bug present unchanged since
its first commit -- strong evidence nobody had run it and looked at its
own output, let alone tested it against a real badge. See `BITACORA.md`'s
2026-08-31 entries for the full writeup of that audit and everything
tried before this rewrite (padding to a fixed byte count, zeroing the six
"unknown" constants) -- both ideas this new evidence shows were aimed at
the wrong part of the format.

## Byte layout (confirmed 2026-08-31 by disassembling `br35_bmp_to_res`)

    offset   bytes  content
    0x00     4      magic "RU50" as little-endian u32 (0x30355552)
    0x04     8      constant 0x0000000100010500
    0x0C     4      zero
    0x10     8      constant 0x0000001800000000
    0x18     8      constant 0x0054000100000030
    0x20     8      constant 0x0000003c00000000
    0x28     8      constant 0x0000000000500001
    0x30     8      constant 0x0000005000000100
    0x38     4      constant 0x00000400
    0x3C     2      crc_header (u16 LE)
    0x3E     2      crc_payload (u16 LE)
    0x40     4      flags word (u32 LE), 0x00920001 by default
    0x44     2      width (u16 LE)
    0x46     2      height (u16 LE)
    0x48     4      ETC2 payload length (u32 LE)
    0x4C     4      constant 0x00000450 (== header size / PAYLOAD_OFF)
    0x50     1024   reserved, zero-filled
    0x450    N      ETC2-compressed RGB payload (see `scratch_bytes`)

`crc_payload` is the vendor CRC16 over the compressed payload bytes.
`crc_header` is the vendor CRC16 over an 18-byte slice: crc_payload (u16),
flags (u32), width (u16), height (u16), payload length (u32), and the
0x450 constant (u32) -- see `_header_crc_slice`.
"""
from __future__ import annotations

import struct

from PIL import Image

from .image import fit_image

MAGIC_RU50 = 0x30355552
HEADER_RESERVED_OFF = 0x50
HEADER_RESERVED_LEN = 0x400
PAYLOAD_OFF = 0x450
assert HEADER_RESERVED_OFF + HEADER_RESERVED_LEN == PAYLOAD_OFF

HDR_QW_04 = 0x0000000100010500
HDR_QW_18 = 0x0054000100000030
HDR_QW_20 = 0x0000003C00000000
HDR_QW_28 = 0x0000000000500001
HDR_QW_30 = 0x0000005000000100
HDR_DW_38 = 0x400
HDR_FLAGS = 0x00920001

# Vendor Crc16 nibble table -- dumped directly from .rodata @ file offset
# 0x9460 of jni/x86_64/libjl_bmp_convert.so, extracted from the official
# `Jieli-Tech/Android-JL_Bluetooth` repo's
# libs/BmpConvert_V1.6.0_10605-release.aar (2026-08-31). 256 little-endian
# u16 values (512 bytes). Copied verbatim; do not "simplify" this into a
# standard CRC16 variant -- it's a vendor-specific table, not CRC16-CCITT
# or -MODBUS.
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
    as two nibbles, high nibble first. Re-derived directly from disassembly
    of the real `Crc16` function on 2026-08-31 -- matches what was already
    implemented here.
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
    """18 bytes fed to the second CRC16 pass (`crc_header`). Order confirmed
    2026-08-31 by disassembly: crc_payload, flags, width, height, payload
    length, then the fixed 0x450 (PAYLOAD_OFF) constant -- all real values,
    no zero padding (earlier versions of this file had this wrong: flags
    first, a zeroed placeholder last, and no 0x450 constant at all).
    """
    out = bytearray(18)
    struct.pack_into("<H", out, 0, crc_payload & 0xFFFF)
    struct.pack_into("<I", out, 2, HDR_FLAGS)
    struct.pack_into("<H", out, 6, width & 0xFFFF)
    struct.pack_into("<H", out, 8, height & 0xFFFF)
    struct.pack_into("<I", out, 10, payload_len)
    struct.pack_into("<I", out, 14, PAYLOAD_OFF)
    return bytes(out)


def build_ru50_blob(width: int, height: int, payload: bytes, *, zero_unknown_fields: bool = False) -> bytes:
    """Wrap an already-ETC2-compressed `payload` in the full RU50 container.

    Field order and offsets confirmed 2026-08-31 by disassembling the real
    `br35_bmp_to_res` in `libjl_bmp_convert.so` (see the module docstring
    for the full story of what changed from earlier versions of this file).
    The reserved region `[0x50, 0x450)` is left zero, which is already true
    of a fresh `bytearray` -- no separate zero-fill needed now that every
    populated field ends exactly at 0x50 and none of them overlap it.

    `zero_unknown_fields`: DIAGNOSTIC ONLY, kept for comparison with earlier
    tests. HDR_QW_04/18/20/28/30 and HDR_DW_38 are no longer "unknown
    meaning" constants in the sense of being unverified -- their VALUES are
    now confirmed byte-for-byte against real disassembly (their semantic
    *purpose* is still unknown, but that's a different thing). A real
    upload on 2026-08-31 with these fields zeroed instead of at their real
    values got the exact same finish-silence as leaving them alone, which
    already ruled them out as the cause before this rewrite -- the far more
    likely cause was the field-order bug fixed here. Kept as an option only
    so old test commands keep working; there is no live hypothesis left
    that motivates using it.
    """
    crc_payload = crc16(payload)
    crc_header = crc16(_header_crc_slice(width, height, len(payload), crc_payload))

    total = PAYLOAD_OFF + len(payload)
    buf = bytearray(total)  # zero-initialized; covers the [0x50, 0x450) reserved region for free
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
    struct.pack_into("<H", buf, 0x3C, crc_header & 0xFFFF)
    struct.pack_into("<H", buf, 0x3E, crc_payload & 0xFFFF)
    struct.pack_into("<I", buf, 0x40, HDR_FLAGS)
    struct.pack_into("<H", buf, 0x44, width & 0xFFFF)
    struct.pack_into("<H", buf, 0x46, height & 0xFFFF)
    struct.pack_into("<I", buf, 0x48, len(payload))
    struct.pack_into("<I", buf, 0x4C, PAYLOAD_OFF)
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


def image_to_etc2_nopack(path: str, width: int, height: int, *, fit: str = "cover") -> bytes:
    """Open, fit, and ETC2-compress an image with NO RU50 wrapper at all --
    just the raw compressed texture bytes (`scratch_bytes(width, height)`
    long), byte-for-byte what `br35_bmp_to_res` writes when its "pack" flag
    is false (`br35_bmp_to_res_path_nopack` / the `test BYTE PTR
    [rbp-0x450],0x0 ; je ...` branch found in the same 2026-08-31
    disassembly that fixed `build_ru50_blob`'s field order -- see that
    function's docstring). Added after a real-hardware test with the
    corrected RU50 header still hung identically at `finish`: if this
    firmware doesn't want the RU50 wrapper at all, this is the other
    concrete, disassembly-backed thing worth trying, not another guess at
    the wrapper's internals. See BITACORA.md's 2026-08-31 entries.
    """
    with Image.open(path) as im:
        fitted = fit_image(im, width, height, fit=fit)
    return compress_etc2(fitted, width, height)


def solid_etc2_nopack(width: int, height: int, color: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """`image_to_etc2_nopack`'s solid-color counterpart."""
    im = Image.new("RGB", (width, height), color)
    return compress_etc2(im, width, height)
