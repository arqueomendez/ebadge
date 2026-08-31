"""The 0xCD frame protocol used by DG01-style LCD badges (BG02/BW03/SuperBand).

This is a line-by-line Python port of the Rust reference implementation in
https://github.com/DynamicDevices/lcd-badge-ble (dg01-ble/src/main.rs and
dg01-ble/src/dial_upload.rs), not a guess from the prose docs -- offsets and
constants below were checked against that source on 2026-08-30. If a future
version of that project changes the wire format, re-diff against it.

Frame layout (`get_protocol` in the Rust source):

    offset  bytes  content
    0       1      0xCD frame marker
    1-2     2      big-endian u16: len(frame) - 3  (== 5 + len(payload))
    3       1      command id
    4       1      0x01 (fixed "key length")
    5       1      sub-key
    6-7     2      big-endian u16: len(payload)
    8+      N      payload
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

FRAME_MARKER = 0xCD
# A SECOND, entirely different notify format the badge can use, discovered
# 2026-08-31 by re-reading dg01-ble's Rust source instead of just its
# constants: an 8-byte frame starting with 0xDC (not 0xCD), cmd at byte[3],
# sub at byte[4] -- "BaseReceiveData"'s other notify branch, never merged
# with the 0xCD frame protocol. Confirmed real (not a guess) by dg01-ble's
# own captured constants (PREFLIGHT_UPLOAD2_DC1 = `dc 00 05 15 0c 00 1e 01`,
# i.e. cmd=0x15/sub=0x0c -- the SAME "banner" cmd/sub this project has been
# logging as unrelated noise every single run) and by upstream commit
# 096490d ("DC frame ACK: accept dc 00 05 1f 02 00 19 01 as valid start
# ACK"). This project's CdNotifyAssembler only ever recognized 0xCD-prefixed
# frames -- any 0xDC-prefixed notification was silently discarded with zero
# trace in the debug log (see CdNotifyAssembler.push: a leading byte that
# isn't FRAME_MARKER just gets skipped/dropped). If this firmware's finish
# ack for RU50 arrives as a short DC frame rather than a 0xCD status=2
# packet, EVERY test run so far would look exactly like "total silence" from
# this project's point of view regardless of what was inside the uploaded
# file -- which matches the fact that four different RU50 payload variants
# (see BITACORA.md) all produced byte-for-byte identical finish-silence.
FRAME_MARKER_DC = 0xDC

# --- command / sub-key constants (verbatim values from dial_upload.rs) -----
CMD_DIAL_TRANSFER = 31  # 0x1F -- host -> badge: start / chunk / finish an upload
CMD_DIAL_NOTIFY = 32  # 0x20  -- badge -> host notifies, and also the dial-dims *request* cmd
CMD_FILE_UART = 34  # 0x22  -- alternate plain-UART file transport (unused here)
CMD_FIND_DEVICE = 0x12  # 18 -- "find my badge" buzzer/flash toggle

SUB_DIAL_FILE = 1  # a file chunk (both the cmd31 request and the cmd32 ack use sub=1)
SUB_DIAL_START = 2  # upload start (cmd31) / clock-info (cmd32, see below)
SUB_DIAL_FINISH = 3  # upload finish (cmd31)
SUB_DIAL_NOTIFY_FILE = 1
SUB_DIAL_NOTIFY_CLOCK_INFO = 2  # dial-dims lives at cmd32/sub2, same sub value as SUB_DIAL_START
SUB_FIND_DEVICE = 0x0B

STATUS_OK = 2
# These labels are dg01-ble's/PROTOCOL.md's reading of the decompiled
# SuperBand/FitPro Android APK's constant names (ERROR_BATTERY_LOW,
# ERROR_CHARGE_BATTERY, etc.) -- NOT something confirmed against our own
# hardware. Treat them as a hint for the log, not a diagnosis: status=3
# ("low battery") fired once on a real run even though a same-session
# device-info battery read said 90%, and status=4 ("charging") fired
# repeatedly on 2026-08-31 with no cable anywhere near the badge. Two
# independent contradictions is enough to stop trusting this table for
# THIS firmware ("LJ733"/V35509) -- it may be a genuine per-firmware
# divergence from whatever APK build the reference project decompiled, or
# these codes may mean something else entirely here (e.g. "busy" rather
# than a specific hardware condition). An earlier version of this file used
# these to hard-abort the whole upload immediately on sight
# (`FATAL_STATUS_CODES` / `ble.DeviceRefused`) -- that was reverted the same
# day after the status=4/no-cable contradiction, since aborting outright on
# an unconfirmed label risks giving up on something a longer wait might have
# gotten past. See BITACORA.md's 2026-08-31 entries for the full story.
STATUS_ERRORS = {
    1: "check failed",
    3: "low battery (per APK's constant name -- seen contradicted by an actual 90% battery reading once)",
    4: "charging (per APK's constant name -- seen fire repeatedly with no cable connected)",
    5: "out of memory",
    7: "not ready / unknown (APK fallback code)",
}


def get_protocol(cmd: int, subkey: int, payload: bytes = b"") -> bytes:
    """Port of Rust `get_protocol`: build a full 0xCD frame with a payload."""
    total = 8 + len(payload)
    out = bytearray(total)
    out[0] = FRAME_MARKER
    len_field = total - 3
    out[1] = (len_field >> 8) & 0xFF
    out[2] = len_field & 0xFF
    out[3] = cmd
    out[4] = 1
    out[5] = subkey
    plen = len(payload)
    out[6] = (plen >> 8) & 0xFF
    out[7] = plen & 0xFF
    out[8:] = payload
    return bytes(out)


def get_no_value_protocol(cmd: int, subkey: int) -> bytes:
    """Port of Rust `get_no_value_protocol`: the fixed 8-byte no-payload frame."""
    return get_protocol(cmd, subkey, b"")


def dial_dims_request() -> bytes:
    """cmd32/sub2, no payload -- matches PROTOCOL.md's literal example:
    0xCD 00 05 20 01 02 00 00
    """
    return get_no_value_protocol(CMD_DIAL_NOTIFY, SUB_DIAL_NOTIFY_CLOCK_INFO)


def find_device_request(enable: bool = True) -> bytes:
    return get_protocol(CMD_FIND_DEVICE, SUB_FIND_DEVICE, bytes([1 if enable else 0]))


DEFAULT_MID4_HEX = "15a20008"  # dg01-ble's --dial-start-mid4 default


def dial_start_extended(
    file_len: int,
    *,
    font_position: int = 0,
    custom: bool = True,
    mid4: bytes = bytes.fromhex(DEFAULT_MID4_HEX),
    rgb: tuple[int, int, int] = (255, 255, 255),
) -> bytes:
    """Port of Rust `dial_start_extended`: the 17-byte cmd31/sub2 upload-start payload.

    font_position/custom/mid4/rgb are OEM-app fields whose exact meaning isn't
    documented; the defaults here are dg01-ble's own CLI defaults (0, True,
    "15a20008", 255/255/255), which is the closest thing to a "known-plausible"
    value we have without a live capture. `custom=True` marks this as a
    custom bitmap rather than a built-in font dial.
    """
    if len(mid4) != 4:
        raise ValueError("mid4 must be exactly 4 bytes")
    r, g, b = rgb
    payload = bytearray()
    payload.append(font_position & 0xFF)
    payload.append(1 if custom else 0)
    payload.extend(mid4)
    payload.extend([r & 0xFF, g & 0xFF, b & 0xFF])
    payload.extend(struct.pack(">I", file_len))
    payload.extend(b"\x00\x00\x00\x00")
    assert len(payload) == 17
    return get_protocol(CMD_DIAL_TRANSFER, SUB_DIAL_START, bytes(payload))


def checksum_seq_and_chunk(seq_plus_chunk: bytes) -> bytes:
    """Port of Rust `checksum_seq_and_chunk`: wrapping u16 sum, big-endian."""
    s = 0
    for byte in seq_plus_chunk:
        s = (s + byte) & 0xFFFF
    return struct.pack(">H", s)


def uart_file_chunk(seq: int, chunk: bytes, *, cmd: int = CMD_DIAL_TRANSFER) -> bytes:
    """Port of Rust `uart_file_chunk`: cmd?/sub1 frame carrying one file chunk."""
    body = bytearray()
    body.extend(struct.pack(">H", seq))
    body.extend(chunk)
    body.extend(checksum_seq_and_chunk(bytes(body)))
    return get_protocol(cmd, SUB_DIAL_FILE, bytes(body))


def dial_finish_payload(file_bytes: bytes, *, include_length: bool = False) -> bytes:
    """cmd31/sub3 finish payload: BE u32 sum of every byte in `file_bytes`,
    with an optional leading BE u32 length for the older (now believed
    wrong) 8-byte variant.

    This flip-flopped twice before landing here, so the history matters:

    1. dg01-ble's original Rust `dial_finish_payload` sent a 4-byte sum
       only. A real BW03 capture on 2026-08-30 rejected that instantly with
       status=1 ("check failed").
    2. We switched to an 8-byte length+sum trailer (matching what
       PROTOCOL.md's prose said the decompiled Android APK's
       `calculateFinishCheckcode` builds). That was ALSO rejected instantly
       with status=1, on multiple full uploads -- including ones where
       every one of the 576 chunks had already been acked successfully.
    3. On 2026-05-02/03, upstream dg01-ble (commit "protocol fixes for
       DG01/LJ733 firmware variant", PR #3) reverted back to the 4-byte
       sum-only payload, explicitly because a real iOS sysdiagnose capture
       of the official SuperBand app showed "device expects checksum only,
       not length+checksum" -- and that fix was tested against our *exact*
       board (model "LJ733", firmware close to ours). So the 8-byte variant
       this project briefly used was simply wrong; PROTOCOL.md's prose
       summary of the APK internals was the less reliable source here, same
       as elsewhere in this port.

    That said: the same upstream commit's author only got to "upload
    reaches 99%" with the 4-byte format, not full success -- their remaining
    blocker was the *payload format* (see ebadge/ru50.py), not this framing.
    So getting `include_length=False` (the default) right is necessary but
    may not be sufficient on its own; pass RU50-encoded bytes (not raw
    RGB565) as `file_bytes` for the other half of the fix.

    `include_length=True` is kept only as an escape hatch for comparison /
    debugging, not because there's current evidence it's correct.
    """
    total = sum(file_bytes) & 0xFFFFFFFF
    if include_length:
        length = len(file_bytes) & 0xFFFFFFFF
        payload = struct.pack(">II", length, total)
    else:
        payload = struct.pack(">I", total)
    return get_protocol(CMD_DIAL_TRANSFER, SUB_DIAL_FINISH, payload)


class CdNotifyAssembler:
    """Port of Rust `CdNotifyAssembler`: reassembles fragmented 0xCD notifies.

    BLE notifications are capped by the ATT MTU, so one logical 0xCD frame
    can arrive as several `notify` callbacks. Feed every callback's raw bytes
    to `push()`; it returns the list of any frames that became complete.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        out: list[bytes] = []
        while True:
            if not self._buf:
                break
            if self._buf[0] != FRAME_MARKER:
                idx = self._buf.find(FRAME_MARKER, 1)
                if idx == -1:
                    self._buf.clear()
                else:
                    del self._buf[:idx]
                continue
            if len(self._buf) < 3:
                break
            need = struct.unpack_from(">H", self._buf, 1)[0] + 3
            if len(self._buf) < need:
                break
            out.append(bytes(self._buf[:need]))
            del self._buf[:need]
        return out


def parse_dc_short(packet: bytes) -> tuple[int, int] | None:
    """Port of Rust `parse_dc_short`: an 8-byte 0xDC-marker notify, cmd at
    packet[3], sub at packet[4]. See the FRAME_MARKER_DC comment above for
    why this project never implemented this until 2026-08-31.
    """
    if not packet or packet[0] != FRAME_MARKER_DC or len(packet) < 6:
        return None
    return packet[3], packet[4]


def is_dc_ack_for(cmd: int, sub: int, want_status: int) -> bool:
    """Port of the DC-frame acceptance rule `dg01-ble`'s main.rs uses for
    both dial start and finish waits: a DC frame with cmd31/sub2 acks a
    start wait (`want_status == 1000`), cmd31/sub3 acks a finish wait
    (`want_status == STATUS_OK`), and cmd0 with sub equal to the
    chunk-index offset (`want_status - 1000`) acks that specific chunk --
    an alternate, cmd-zero convention some firmware apparently uses instead
    of (or alongside) the normal cmd32/sub1 chunk-ack counter.
    """
    if cmd == CMD_DIAL_TRANSFER and sub == SUB_DIAL_START and want_status == 1000:
        return True
    if cmd == CMD_DIAL_TRANSFER and sub == SUB_DIAL_FINISH and want_status == STATUS_OK:
        return True
    if cmd == 0 and (want_status == 1000 or sub == want_status - 1000):
        return True
    return False


def parse_cd_notify_status(packet: bytes) -> int | None:
    """Port of Rust `parse_cd_notify_status`: generic i32 BE status at payload[0:4]."""
    if not packet or packet[0] != FRAME_MARKER or len(packet) < 12:
        return None
    total_minus_3 = struct.unpack_from(">H", packet, 1)[0]
    if len(packet) < total_minus_3 + 3:
        return None
    p = packet[8:]
    if len(p) < 4:
        return None
    return struct.unpack_from(">i", p, 0)[0]


def parse_dial_watch_ack_status(packet: bytes) -> int | None:
    """Port of Rust `parse_dial_watch_ack_status`: only accept frames that look
    like a chunk ack (cmd32/sub1) or a start ack (cmd31/sub2) before decoding
    the status. Finish acks are NOT matched here -- dg01-ble reads those with
    the "loose" parser (`parse_cd_notify_status` directly), so do the same.
    """
    if len(packet) < 6:
        return None
    cmd = packet[3]
    sub = packet[5]
    if cmd not in (CMD_DIAL_NOTIFY, CMD_DIAL_TRANSFER):
        return None
    is_chunk_or_start_ack = sub == SUB_DIAL_NOTIFY_FILE or (
        cmd == CMD_DIAL_TRANSFER and sub == SUB_DIAL_START
    )
    if not is_chunk_or_start_ack:
        return None
    return parse_cd_notify_status(packet)


@dataclass
class DialClockInfo:
    screen_type: int
    grade: int
    width: int
    height: int
    config: int | None = None

    @property
    def rgb565_bytes(self) -> int:
        return self.width * self.height * 2


def parse_dial_clock_info_full(packet: bytes) -> DialClockInfo | None:
    """Port of Rust `parse_dial_clock_info_full`, byte-for-byte."""
    if not packet or packet[0] != FRAME_MARKER:
        return None
    if len(packet) < 8 + 6:
        return None
    if packet[3] != CMD_DIAL_NOTIFY:
        return None
    if packet[5] != SUB_DIAL_NOTIFY_CLOCK_INFO:
        return None
    plen = struct.unpack_from(">H", packet, 6)[0]
    if plen < 6 or len(packet) < 8 + plen:
        return None
    p = packet[8 : 8 + plen]
    screen_type = p[0]
    grade = p[1]
    width = struct.unpack_from(">H", p, 2)[0]
    height = struct.unpack_from(">H", p, 4)[0]
    config = None
    if len(p) > 6:
        lm = p[6]
        if len(p) > 7 + lm:
            main_len = p[7 + lm]
            if len(p) >= 8 + lm + main_len:
                i5 = 8 + lm + main_len
                if len(p) > i5:
                    config = p[i5]
    return DialClockInfo(screen_type=screen_type, grade=grade, width=width, height=height, config=config)
