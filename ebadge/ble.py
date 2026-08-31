"""Async BLE client for DG01-style LCD e-badges (BG02/BW03, SuperBand app),
built on `bleak` so it runs natively on Windows -- no Linux/BlueZ required.

Two UUID sets exist in the wild (see dg01-ble's --apk-uart flag): real DG01
hardware exposes a "7e40..." Nordic-UART-shaped service, while some units /
the phone app itself talk to a "6e40..." variant. `connect()` tries both.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as proto

log = logging.getLogger("ebadge.ble")

# (service, tx-write, rx-notify) candidates, tried in order.
UUID_CANDIDATES = [
    (
        "7e400001-b5a3-f393-e0a9-e50e24dcca9d",
        "7e400002-b5a3-f393-e0a9-e50e24dcca9d",
        "7e400003-b5a3-f393-e0a9-e50e24dcca9d",
    ),
    (
        "6e400001-b5a3-f393-e0a9-e50e24dcca9d",
        "6e400002-b5a3-f393-e0a9-e50e24dcca9d",
        "6e400003-b5a3-f393-e0a9-e50e24dcca9d",
    ),
]
# dg01-ble also subscribes to this "secondary" notify characteristic when present.
SECONDARY_NOTIFY = "7e400004-b5a3-f393-e0a9-e50e24dcca9d"

DEFAULT_CHUNK_SIZE = 200  # bytes of image data per cmd31/sub1 frame
DEFAULT_FRAGMENT_SIZE = 20  # bytes per raw BLE write (matches FitPro's CommandPool)
DEFAULT_FRAGMENT_GAP_S = 0.003
NOTIFY_TIMEOUT_S = 5.0


async def scan(timeout: float = 6.0) -> list[BLEDevice]:
    """List nearby BLE devices. Look for a name like BG02/BW03/DG01/SuperBand."""
    return await BleakScanner.discover(timeout=timeout)


# Standard BLE SIG characteristics -- safe, read-only, no vendor protocol needed.
DIS_CHARACTERISTICS = {
    "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
    "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
    "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
    "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "Hardware Revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "Software Revision",
    "00002a23-0000-1000-8000-00805f9b34fb": "System ID",
    "00002a50-0000-1000-8000-00805f9b34fb": "PnP ID",
}
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


async def read_device_info(address_or_name: str, timeout: float = 15.0) -> dict[str, str]:
    """Read the standard Device Information + Battery services. Pure BLE
    reads, no vendor protocol, no writes -- safe on hardware you don't fully
    trust yet, and the fastest way to identify the actual chip/firmware
    without opening the device.
    """
    device = await _find_device(address_or_name, timeout)
    if device is None:
        raise RuntimeError(f"no BLE device found matching {address_or_name!r}")
    client = BleakClient(device)
    await client.connect(timeout=timeout)
    try:
        available = {c.uuid.lower() for s in client.services for c in s.characteristics}
        results: dict[str, str] = {}
        for uuid, label in DIS_CHARACTERISTICS.items():
            if uuid not in available:
                continue
            try:
                raw = await client.read_gatt_char(uuid)
                if uuid == "00002a23-0000-1000-8000-00805f9b34fb":  # System ID: binary, not text
                    results[label] = raw.hex()
                elif uuid == "00002a50-0000-1000-8000-00805f9b34fb":  # PnP ID: binary VID/PID
                    results[label] = raw.hex()
                else:
                    results[label] = raw.decode("utf-8", errors="replace").strip("\x00")
            except Exception as exc:
                results[label] = f"(read failed: {exc})"
        if BATTERY_LEVEL_UUID in available:
            try:
                raw = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                results["Battery Level"] = f"{raw[0]}%" if raw else "(empty)"
            except Exception as exc:
                results["Battery Level"] = f"(read failed: {exc})"
        return results
    finally:
        await client.disconnect()


async def dump_services(address_or_name: str, timeout: float = 15.0) -> str:
    """Connect and list every advertised GATT service/characteristic, with
    properties (read/write/notify/...). Read-only, makes no writes -- safe to
    run even on a device you don't fully trust yet. Useful to spot a DFU/OTA
    service (a name like "1530"/"1531"/"fe59"/"8ec9..." or similar
    Nordic/Telink-style UUIDs) alongside the dial UART service we already
    know about, without needing any physical access to the hardware.
    """
    device = await _find_device(address_or_name, timeout)
    if device is None:
        raise RuntimeError(f"no BLE device found matching {address_or_name!r}")
    client = BleakClient(device)
    await client.connect(timeout=timeout)
    try:
        lines = []
        for service in client.services:
            lines.append(f"[service] {service.uuid}  ({service.description or 'no description'})")
            for char in service.characteristics:
                props = ",".join(char.properties)
                lines.append(f"    {char.uuid}  [{props}]  ({char.description or 'no description'})")
                for desc in char.descriptors:
                    lines.append(f"        descriptor {desc.uuid}")
        return "\n".join(lines)
    finally:
        await client.disconnect()


@dataclass
class UploadProgress:
    sent_bytes: int
    total_bytes: int
    chunk_index: int
    chunk_count: int


class ProtocolError(RuntimeError):
    pass


class EBadge:
    """One connected session with a badge. Use `async with EBadge.connect(...)`."""

    def __init__(
        self,
        client: BleakClient,
        tx_uuid: str,
        rx_uuids: list[str],
        *,
        fragment_size: int = DEFAULT_FRAGMENT_SIZE,
        fragment_gap_s: float = DEFAULT_FRAGMENT_GAP_S,
    ) -> None:
        self._client = client
        self._tx_uuid = tx_uuid
        self._rx_uuids = rx_uuids
        self._fragment_size = fragment_size
        self._fragment_gap_s = fragment_gap_s
        self._asm = proto.CdNotifyAssembler()
        self._frames: asyncio.Queue[bytes] = asyncio.Queue()

    @classmethod
    async def connect(
        cls,
        address_or_name: str,
        timeout: float = 15.0,
        *,
        fragment_size: int = DEFAULT_FRAGMENT_SIZE,
        fragment_gap_s: float = DEFAULT_FRAGMENT_GAP_S,
    ) -> "EBadge":
        device = await _find_device(address_or_name, timeout)
        if device is None:
            raise RuntimeError(
                f"no BLE device found matching {address_or_name!r} "
                "(try `ebadge scan` first and pass the exact MAC address or name)"
            )

        client = BleakClient(device)
        await client.connect(timeout=timeout)

        service_uuids = {s.uuid.lower() for s in client.services}
        tx_uuid = None
        rx_uuids: list[str] = []
        for service_uuid, tx, rx in UUID_CANDIDATES:
            if service_uuid.lower() in service_uuids:
                tx_uuid = tx
                rx_uuids = [rx]
                if SECONDARY_NOTIFY.lower() in {c.uuid.lower() for s in client.services for c in s.characteristics}:
                    rx_uuids.append(SECONDARY_NOTIFY)
                break
        if tx_uuid is None:
            await client.disconnect()
            found = ", ".join(sorted(service_uuids)) or "(none)"
            raise RuntimeError(
                "could not find the badge's UART-style service "
                f"(looked for 7e400001.../6e400001...; device advertised: {found})"
            )

        badge = cls(client, tx_uuid, rx_uuids, fragment_size=fragment_size, fragment_gap_s=fragment_gap_s)
        try:
            for rx in rx_uuids:
                await client.start_notify(rx, badge._on_notify)
        except Exception as exc:
            # Leave no half-connected client behind for the next retry to
            # trip over (seen on a real run: a low-battery badge answered a
            # reconnect with MTU=23 instead of 517, then start_notify raised
            # a raw WinRT OSError -- not a ProtocolError, so it would have
            # skipped straight past any `except ProtocolError` retry logic).
            try:
                await client.disconnect()
            except Exception:
                pass
            raise RuntimeError(f"connected but failed to subscribe to notifications: {exc}") from exc
        log.info("connected: tx=%s rx=%s", tx_uuid, rx_uuids)
        return badge

    async def __aenter__(self) -> "EBadge":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        for rx in self._rx_uuids:
            try:
                await self._client.stop_notify(rx)
            except Exception:
                pass
        try:
            await self._client.disconnect()
        except Exception:
            pass

    def _on_notify(self, _handle, data: bytearray) -> None:
        for frame in self._asm.push(bytes(data)):
            log.debug("<< %s", frame.hex())
            self._frames.put_nowait(frame)

    async def _write_frame(self, frame: bytes) -> None:
        fragment, gap_s = self._fragment_size, self._fragment_gap_s
        log.debug(">> %s", frame.hex())
        if fragment <= 0 or len(frame) <= fragment:
            await self._client.write_gatt_char(self._tx_uuid, frame, response=False)
            return
        for i in range(0, len(frame), fragment):
            if i > 0 and gap_s > 0:
                await asyncio.sleep(gap_s)
            await self._client.write_gatt_char(self._tx_uuid, frame[i : i + fragment], response=False)

    async def _next_frame(self, timeout: float = NOTIFY_TIMEOUT_S) -> bytes:
        try:
            return await asyncio.wait_for(self._frames.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProtocolError(f"badge did not reply within {timeout:.0f}s") from exc

    async def dial_dims(self) -> proto.DialClockInfo:
        await self._write_frame(proto.dial_dims_request())
        # skip any unrelated frames the badge might interleave (e.g. battery)
        for _ in range(5):
            frame = await self._next_frame()
            info = proto.parse_dial_clock_info_full(frame)
            if info is not None:
                return info
        raise ProtocolError("no dial-dims response recognized; last frames logged at DEBUG level")

    async def find_device(self, enable: bool = True) -> None:
        await self._write_frame(proto.find_device_request(enable))

    async def upload_dial(
        self,
        image_bytes: bytes,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_retries: int = 3,
        chunk_timeout_s: float = NOTIFY_TIMEOUT_S,
        inter_chunk_delay_s: float = 0.0,
        finish_timeout_s: float = 45.0,
        finish_retries: int = 2,
        finish_delay_s: float = 1.0,
        finish_include_length: bool = False,
        on_progress=None,
    ) -> None:
        """Push `image_bytes` (already RGB565-encoded, no header) as the dial image.

        Ack counter, confirmed against a real BW03 capture on 2026-08-30: the
        badge replies with `1000 + chunks_successfully_received_so_far`, NOT
        `1000 + the sequence number we sent`. The start ack is exactly 1000
        (0 chunks received yet); after the first chunk it's 1001, and so on.
        The same capture also showed one spurious/out-of-order status=1
        ("check failed") notify arrive just before the real 1001 ack for that
        same chunk -- cause unknown (possibly a stale notify, possibly the
        badge briefly rejecting a still-incomplete fragmented write before
        the rest of it arrived) -- so `_await_ack` doesn't trust the first
        recognizable frame; it keeps reading until it sees the exact status
        it asked for, logging anything else as a warning.
        """
        status = await self._send_with_retry(
            lambda: proto.dial_start_extended(len(image_bytes)), want_status=1000, retries=chunk_retries
        )
        log.info("upload start ack: status=%s", status)

        total = len(image_bytes)
        chunk_count = (total + chunk_size - 1) // chunk_size
        for seq, offset in enumerate(range(0, total, chunk_size)):
            chunk = image_bytes[offset : offset + chunk_size]
            want = 1000 + seq + 1  # 1-indexed count of chunks received, not `seq`
            await self._send_with_retry(
                lambda c=chunk, s=seq: proto.uart_file_chunk(s, c),
                want_status=want,
                retries=chunk_retries,
                timeout=chunk_timeout_s,
            )
            if on_progress:
                on_progress(UploadProgress(offset + len(chunk), total, seq + 1, chunk_count))
            if inter_chunk_delay_s > 0:
                await asyncio.sleep(inter_chunk_delay_s)

        # The finish ack seems to behave differently from chunk acks: on a
        # real capture, resending it after "check failed" got pure silence
        # both times, rather than the transient-noise-then-correct-ack
        # pattern chunks show. Both finish payload formats (4-byte sum only,
        # and 8-byte length+sum) were rejected identically and instantly with
        # RGB565 content -- see proto.dial_finish_payload's docstring and
        # ebadge/ru50.py for why the leading suspect is now the *payload
        # format* (raw RGB565 vs the badge's actual proprietary RU50/ETC2
        # container), not this checksum framing. Still worth a moment to let
        # the badge settle before asking it to verify anything.
        if finish_delay_s > 0:
            await asyncio.sleep(finish_delay_s)
        status = await self._send_with_retry(
            lambda: proto.dial_finish_payload(image_bytes, include_length=finish_include_length),
            want_status=proto.STATUS_OK,
            loose=True,
            timeout=finish_timeout_s,
            retries=finish_retries,
            retry_delay_s=2.0,
        )
        log.info("upload finished: status=%s (ok)", status)

    async def _send_with_retry(
        self,
        build_frame,
        *,
        want_status: int,
        loose: bool = False,
        timeout: float = NOTIFY_TIMEOUT_S,
        retries: int = 3,
        retry_delay_s: float = 1.0,
    ) -> int:
        """Write a frame and wait for its ack; if the badge times out or goes
        quiet mid-transfer (seen on a real BW03 around chunk 97/576 -- no ack,
        no error, just silence for 5s+), resend the SAME frame instead of
        aborting the whole upload. Cheap MCUs like this one can drop a write
        or a notify under sustained BLE traffic; a resend is usually enough
        to get past a transient hiccup.
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                await self._write_frame(build_frame())
                return await self._await_ack(want_status=want_status, loose=loose, timeout=timeout)
            except ProtocolError as exc:
                last_exc = exc
                if attempt < retries:
                    log.warning("attempt %d/%d waiting for status=%d failed (%s), resending", attempt, retries, want_status, exc)
                    await asyncio.sleep(retry_delay_s)
        raise ProtocolError(f"gave up after {retries} attempts waiting for status={want_status}: {last_exc}")

    async def _await_ack(
        self, *, want_status: int, loose: bool = False, timeout: float = NOTIFY_TIMEOUT_S, max_frames: int = 15
    ) -> int:
        """Read frames until one has exactly `want_status`, tolerating any
        interleaved/out-of-order/unrecognized notifies along the way.
        """
        for _ in range(max_frames):
            frame = await self._next_frame(timeout=timeout)
            status = proto.parse_cd_notify_status(frame) if loose else proto.parse_dial_watch_ack_status(frame)
            if status is None:
                log.debug("skipping unparseable frame while waiting for status=%d: %s", want_status, frame.hex())
                continue
            if status != want_status:
                # Don't treat a mismatched status as fatal here, even a known
                # "error" code from STATUS_ERRORS: this project tried exactly
                # that on 2026-08-31 (hard-abort on status in {3,4,5,7}) and
                # it was wrong within the hour -- status=4 ("charging" per
                # the reference APK) fired five times in a row with no cable
                # anywhere near the badge. Combined with status=3 ("low
                # battery") once firing despite a 90%-battery reading, these
                # numeric-code-to-meaning labels are not trustworthy enough
                # on this firmware to justify giving up early on. If the
                # expected status genuinely never shows up, this loop still
                # fails via the "gave up waiting" error below once
                # max_frames or the timeout is exhausted -- that's the
                # signal to act on, not the specific code in between.
                note = f" ({proto.STATUS_ERRORS[status]})" if status in proto.STATUS_ERRORS else ""
                log.warning("got status=%d%s while waiting for %d, ignoring and reading on: %s", status, note, want_status, frame.hex())
                continue
            return status
        raise ProtocolError(f"gave up waiting for status={want_status} after {max_frames} frames")


async def _find_device(address_or_name: str, timeout: float) -> BLEDevice | None:
    try:
        device = await BleakScanner.find_device_by_address(address_or_name, timeout=timeout)
        if device is not None:
            return device
    except Exception:
        pass
    devices = await BleakScanner.discover(timeout=timeout)
    needle = address_or_name.lower()
    for d in devices:
        if d.name and needle in d.name.lower():
            return d
    return None
