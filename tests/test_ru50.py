"""Tests for the RU50 container format (ebadge/ru50.py).

These check the pure byte-layout/CRC logic against the values reverse-
engineered upstream (DynamicDevices/lcd-badge-ble's `ru50_convert.py`,
itself extracted from the vendor's own BmpConvert 1.6.0 binary) -- they
don't prove a real badge accepts this format, only that our port matches
the reference tool byte-for-byte, same spirit as test_protocol.py.
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

    width, height = struct.unpack_from("<HH", blob, 0x44)
    assert (width, height) == (240, 240)
    payload_len = struct.unpack_from("<I", blob, 0x4C)[0]
    assert payload_len == len(payload)

    flags, crc_word = struct.unpack_from("<II", blob, 0x3C)
    assert flags == ru50.HDR_FLAGS
    crc_payload = crc_word & 0xFFFF
    crc_header = (crc_word >> 16) & 0xFFFF
    assert crc_payload == ru50.crc16(payload)
    assert crc_header == ru50.crc16(ru50._header_crc_slice(240, 240, len(payload), crc_payload))

    # Everything after the last known field (payload length ends at 0x50)
    # up to the payload offset is genuinely unmapped/reserved and should be
    # zero-filled -- unlike bytes [0x14, 0x50), which nominally fall inside
    # the "reserved" range but are actually where flags/CRC/width/height/
    # payload-length live (see build_ru50_blob's docstring on the source
    # script's write-order bug this port fixes).
    tail_reserved = blob[0x50 : ru50.PAYLOAD_OFF]
    assert tail_reserved == bytes(len(tail_reserved))

    assert blob[ru50.PAYLOAD_OFF :] == payload


def test_zero_unknown_fields_leaves_known_fields_intact():
    payload = bytes(range(16)) * 4
    normal = ru50.build_ru50_blob(240, 240, payload)
    zeroed = ru50.build_ru50_blob(240, 240, payload, zero_unknown_fields=True)

    # The 6 genuinely-unknown constant fields differ...
    assert normal[0x04:0x3C] != zeroed[0x04:0x3C]
    assert zeroed[0x04:0x3C] == bytes(0x3C - 0x04)
    # ...but magic, width/height, payload length, flags, and both CRCs must
    # be identical either way -- those are the fields we're confident about.
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
    payload_len_field = struct.unpack_from("<I", blob, 0x4C)[0]
    assert payload_len_field == expected_payload_len
