"""Tests for the RU50 container format (ebadge/ru50.py).

These check the pure byte-layout/CRC logic against values confirmed on
2026-08-31 by disassembling the real `br35_bmp_to_res` / `Crc16` functions
in `libjl_bmp_convert.so` (extracted from the official
`Jieli-Tech/Android-JL_Bluetooth` repo's BmpConvert AAR) -- see the module
docstring in ebadge/ru50.py for the full story, including what the
earlier (upstream-ported) version of this file got wrong. They don't
prove a real badge accepts this format, only that our encoder matches the
vendor's own disassembled code byte-for-byte, same spirit as
test_protocol.py.
"""
import struct

import pytest

from ebadge import ru50

etcpak = pytest.importorskip("etcpak", reason="etcpak not installed; install with `uv add etcpak`")


def test_crc16_table_is_512_bytes():
    assert len(ru50.CRC16_TABLE) == 512


def test_crc16_of_empty_data_is_zero():
    # No bytes processed -> crc starts and stays at 0.
    assert ru50.crc16(b"") == 0


def test_scratch_bytes_matches_known_240x240_and_360x360_values():
    # 240x240 (our BW03's actual screen): ((240*2+6)&~7)*((240+3)//4) = 480*60.
    assert ru50.scratch_bytes(240, 240) == 480 * 60 == 28800
    # 360x360 (the DG01 reference unit's screen).
    assert ru50.scratch_bytes(360, 360) == ((360 * 2 + 6) & ~7) * ((360 + 3) // 4)


def test_build_ru50_blob_header_layout():
    payload = bytes(range(16)) * 4  # 64 arbitrary bytes, not a real ETC2 payload
    blob = ru50.build_ru50_blob(240, 240, payload)

    assert len(blob) == ru50.PAYLOAD_OFF + len(payload)
    magic = struct.unpack_from("<I", blob, 0)[0]
    assert magic == ru50.MAGIC_RU50
    assert bytes(blob[0:4]) == b"RU50"  # "RU50" read as little-endian ASCII == the magic constant

    # Field order confirmed by disassembly: crc_header, crc_payload, flags,
    # width, height, ETC2 payload length, then a fixed 0x450 constant --
    # NOT flags+packed-CRC followed by width/height/payload-length, which
    # is what earlier versions of this file (and its upstream source)
    # assumed.
    crc_header, crc_payload = struct.unpack_from("<HH", blob, 0x3C)
    flags = struct.unpack_from("<I", blob, 0x40)[0]
    width, height = struct.unpack_from("<HH", blob, 0x44)
    payload_len = struct.unpack_from("<I", blob, 0x48)[0]
    header_size_const = struct.unpack_from("<I", blob, 0x4C)[0]

    assert flags == ru50.HDR_FLAGS
    assert (width, height) == (240, 240)
    assert payload_len == len(payload)
    assert header_size_const == ru50.PAYLOAD_OFF  # fixed 0x450, not the payload length
    assert crc_payload == ru50.crc16(payload)
    assert crc_header == ru50.crc16(ru50._header_crc_slice(240, 240, len(payload), crc_payload))

    # [0x50, PAYLOAD_OFF) is the real vendor "reserved" span (confirmed by
    # disassembly: a memset+memcpy of a zeroed 0x400-byte scratch buffer to
    # exactly this offset) and must be zero-filled.
    reserved = blob[0x50 : ru50.PAYLOAD_OFF]
    assert reserved == bytes(len(reserved))
    assert len(reserved) == ru50.HEADER_RESERVED_LEN == 0x400

    assert blob[ru50.PAYLOAD_OFF :] == payload


def test_zero_unknown_fields_leaves_known_fields_intact():
    payload = bytes(range(16)) * 4
    normal = ru50.build_ru50_blob(240, 240, payload)
    zeroed = ru50.build_ru50_blob(240, 240, payload, zero_unknown_fields=True)

    # The 6 constant fields (values confirmed by disassembly, purpose still
    # unknown) differ when zeroed...
    assert normal[0x04:0x3C] != zeroed[0x04:0x3C]
    assert zeroed[0x04:0x3C] == bytes(0x3C - 0x04)
    # ...but magic, the two CRCs, flags, width/height, payload length, and
    # the fixed 0x450 constant must be identical either way -- those are
    # unaffected by this diagnostic flag.
    assert normal[0x00:0x04] == zeroed[0x00:0x04] == b"RU50"
    assert normal[0x3C:0x50] == zeroed[0x3C:0x50]
    assert normal[ru50.PAYLOAD_OFF:] == zeroed[ru50.PAYLOAD_OFF:] == payload


def test_magic_bytes_spell_ru50_little_endian():
    # 0x30355552 read as 4 little-endian bytes is b"RU50" -- a sanity check
    # that we didn't transpose the magic during the port.
    assert struct.pack("<I", ru50.MAGIC_RU50) == b"RU50"


def test_solid_color_round_trips_through_etc2_at_expected_size():
    blob = ru50.solid_ru50(16, 16, (255, 0, 0))
    expected_payload_len = ru50.scratch_bytes(16, 16)
    assert len(blob) == ru50.PAYLOAD_OFF + expected_payload_len
    payload_len_field = struct.unpack_from("<I", blob, 0x48)[0]
    assert payload_len_field == expected_payload_len
    header_size_const = struct.unpack_from("<I", blob, 0x4C)[0]
    assert header_size_const == ru50.PAYLOAD_OFF
