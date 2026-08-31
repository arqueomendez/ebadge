"""Pure-logic tests -- no BLE hardware needed. Run with `uv run pytest`.

Byte vectors here come straight from PROTOCOL.md / the dg01-ble Rust source,
so a pass means our frame builder/parser matches the reference tool exactly;
it says nothing about whether a *particular* BG02/BW03 unit accepts these
frames (see README.md's "Known unknowns" section for what still needs a live
capture).
"""
from ebadge import protocol as proto


def test_dial_dims_request_matches_documented_bytes():
    # PROTOCOL.md: "0xCD 0x00 0x05 0x20 0x01 0x02 0x00 0x00"
    assert proto.dial_dims_request() == bytes.fromhex("cd0005200102 0000".replace(" ", ""))


def test_get_protocol_header_layout():
    frame = proto.get_protocol(0x1F, 0x02, b"\x01\x02\x03")
    assert frame[0] == 0xCD
    assert frame[1:3] == (5 + 3).to_bytes(2, "big")
    assert frame[3] == 0x1F
    assert frame[4] == 0x01
    assert frame[5] == 0x02
    assert frame[6:8] == (3).to_bytes(2, "big")
    assert frame[8:] == b"\x01\x02\x03"


def test_dial_start_extended_is_17_byte_payload_cmd31_sub2():
    frame = proto.dial_start_extended(259200)
    assert frame[3] == proto.CMD_DIAL_TRANSFER
    assert frame[5] == proto.SUB_DIAL_START
    payload = frame[8:]
    assert len(payload) == 17
    assert payload[0] == 0  # font_position default
    assert payload[1] == 1  # custom=True
    assert payload[2:6] == bytes.fromhex("15a20008")  # default mid4
    assert payload[6:9] == b"\xff\xff\xff"  # default rgb
    assert payload[9:13] == (259200).to_bytes(4, "big")
    assert payload[13:17] == b"\x00\x00\x00\x00"


def test_uart_file_chunk_checksum_is_wrapping_u16_sum():
    chunk = bytes([10, 20, 30])
    frame = proto.uart_file_chunk(seq=7, chunk=chunk)
    payload = frame[8:]
    assert payload[0:2] == (7).to_bytes(2, "big")
    assert payload[2:5] == chunk
    expected_checksum = (0 + 7 + 10 + 20 + 30) & 0xFFFF  # seq hi-byte(0) + lo-byte(7) + chunk bytes
    assert payload[5:7] == expected_checksum.to_bytes(2, "big")
    assert frame[3] == proto.CMD_DIAL_TRANSFER
    assert frame[5] == proto.SUB_DIAL_FILE


def test_dial_finish_payload_defaults_to_4_byte_sum_only():
    # Matches the 2026-05 upstream fix (dg01-ble commit "protocol fixes for
    # DG01/LJ733 firmware variant"), tested on our exact board model.
    data = bytes([1, 2, 3, 255])
    frame = proto.dial_finish_payload(data)
    assert frame[3] == proto.CMD_DIAL_TRANSFER
    assert frame[5] == proto.SUB_DIAL_FINISH
    payload = frame[8:]
    assert len(payload) == 4
    assert payload == sum(data).to_bytes(4, "big")


def test_dial_finish_payload_include_length_gives_legacy_8_byte_variant():
    data = bytes([1, 2, 3, 255])
    frame = proto.dial_finish_payload(data, include_length=True)
    payload = frame[8:]
    assert len(payload) == 8
    assert payload[0:4] == len(data).to_bytes(4, "big")
    assert payload[4:8] == sum(data).to_bytes(4, "big")


def test_reassembler_splits_and_rejoins_fragmented_notifies():
    frame = proto.get_protocol(0x20, 0x02, b"\x01\x02\x03\x04\x05\x06")
    asm = proto.CdNotifyAssembler()
    got = []
    for i in range(0, len(frame), 5):  # simulate 5-byte BLE notify fragments
        got.extend(asm.push(frame[i : i + 5]))
    assert got == [frame]


def test_reassembler_resyncs_after_garbage_byte():
    frame = proto.dial_dims_request()
    asm = proto.CdNotifyAssembler()
    out = asm.push(b"\x00\x00" + frame)  # two junk bytes before a real frame
    assert out == [frame]


def test_parse_dial_clock_info_full_round_trips_360x360():
    # Build a synthetic cmd32/sub2 notify: screen_type=1, grade=2, 360x360, no config tail.
    payload = bytes([1, 2]) + (360).to_bytes(2, "big") + (360).to_bytes(2, "big")
    frame = proto.get_protocol(proto.CMD_DIAL_NOTIFY, proto.SUB_DIAL_NOTIFY_CLOCK_INFO, payload)
    info = proto.parse_dial_clock_info_full(frame)
    assert info is not None
    assert (info.screen_type, info.grade, info.width, info.height) == (1, 2, 360, 360)
    assert info.rgb565_bytes == 360 * 360 * 2 == 259200


def test_parse_dial_watch_ack_status_recognizes_chunk_and_start_acks():
    chunk_ack = proto.get_protocol(proto.CMD_DIAL_NOTIFY, proto.SUB_DIAL_NOTIFY_FILE, (1005).to_bytes(4, "big", signed=True))
    assert proto.parse_dial_watch_ack_status(chunk_ack) == 1005

    start_ack = proto.get_protocol(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_START, (1).to_bytes(4, "big", signed=True))
    assert proto.parse_dial_watch_ack_status(start_ack) == 1

    unrelated = proto.get_protocol(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_FINISH, (2).to_bytes(4, "big", signed=True))
    assert proto.parse_dial_watch_ack_status(unrelated) is None  # finish is read with the loose parser instead


def test_parse_cd_notify_status_reads_finish_ack_loosely():
    finish_ack = proto.get_protocol(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_FINISH, (2).to_bytes(4, "big", signed=True))
    assert proto.parse_cd_notify_status(finish_ack) == proto.STATUS_OK


def test_parse_dc_short_reads_cmd_and_sub():
    # Real captured constant from dg01-ble (PREFLIGHT_UPLOAD2_DC1): the
    # exact same cmd=0x15/sub=0x0c "banner" this project logged as
    # unrelated noise on every 0xCD-framed run.
    banner = bytes([0xDC, 0x00, 0x05, 0x15, 0x0C, 0x00, 0x1E, 0x01])
    assert proto.parse_dc_short(banner) == (0x15, 0x0C)

    start_ack = bytes([0xDC, 0x00, 0x05, 0x1F, 0x02, 0x00, 0x19, 0x01])  # from upstream commit 096490d
    assert proto.parse_dc_short(start_ack) == (proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_START)


def test_parse_dc_short_rejects_non_dc_or_short_packets():
    assert proto.parse_dc_short(b"") is None
    assert proto.parse_dc_short(b"\xdc\x00\x00") is None  # too short
    assert proto.parse_dc_short(proto.dial_dims_request()) is None  # 0xCD, not 0xDC


def test_is_dc_ack_for_start_finish_and_chunk_conventions():
    # Start: cmd31/sub2 acks a want_status==1000 wait.
    assert proto.is_dc_ack_for(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_START, 1000) is True
    # Finish: cmd31/sub3 acks a want_status==STATUS_OK wait.
    assert proto.is_dc_ack_for(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_FINISH, proto.STATUS_OK) is True
    # Chunk: cmd0 with sub matching the chunk offset (want_status - 1000).
    assert proto.is_dc_ack_for(0, 7, 1007) is True
    assert proto.is_dc_ack_for(0, 0, 1000) is True  # cmd0/sub0 also covers the start-equivalent case
    # Mismatches must not be accepted.
    assert proto.is_dc_ack_for(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_START, proto.STATUS_OK) is False
    assert proto.is_dc_ack_for(proto.CMD_DIAL_TRANSFER, proto.SUB_DIAL_FINISH, 1000) is False
    assert proto.is_dc_ack_for(0, 3, 1007) is False
