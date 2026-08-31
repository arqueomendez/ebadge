"""Command-line interface: `ebadge scan|dial-dims|upload-dial|find`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import ble, image, ru50


def build_parser() -> argparse.ArgumentParser:
    # --debug lives on a shared "parent" parser so it works both before AND
    # after the subcommand: `ebadge --debug dial-dims ADDR` and
    # `ebadge dial-dims ADDR --debug` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="log every raw BLE frame sent/received")

    parser = argparse.ArgumentParser(
        prog="ebadge",
        description="Talk to a BG02/BW03 LCD e-badge without the SuperBand app.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list nearby BLE devices", parents=[common])
    p_scan.add_argument("--timeout", type=float, default=6.0)

    p_dims = sub.add_parser("dial-dims", help="query the badge's screen resolution", parents=[common])
    p_dims.add_argument("device", help="BLE address (Windows: e.g. AA:BB:CC:DD:EE:FF) or a substring of its advertised name")

    p_upload = sub.add_parser("upload-dial", help="convert an image (RU50/ETC2 by default, or raw RGB565) and upload it as the dial face", parents=[common])
    p_upload.add_argument("device", help="BLE address or name substring")
    p_upload.add_argument("image", nargs="?", help="path to a JPG/PNG/etc image to upload (omit if using --solid)")
    p_upload.add_argument(
        "--solid",
        metavar="R,G,B",
        help="upload a plain solid color instead of a file, e.g. --solid 0,0,0 for black -- useful to blank out "
        "whatever the badge is currently showing if it turns out there's no separate 'delete' command (there "
        "isn't one in the reverse-engineered protocol; this device most likely just overwrites one active image)",
    )
    p_upload.add_argument("--width", type=int, help="override the dial width instead of querying it")
    p_upload.add_argument("--height", type=int, help="override the dial height instead of querying it")
    p_upload.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    p_upload.add_argument(
        "--format",
        choices=["ru50", "rgb565"],
        default="ru50",
        help="wire payload format. 'ru50' (default) builds the proprietary RU50/ETC2 container that a "
        "2026-05 upstream fix (tested on our exact LJ733 board) points to as what the real SuperBand app "
        "sends -- requires the 'etcpak' package. 'rgb565' is the original raw-pixel guess, kept only for "
        "comparison; every real upload we've run with it was rejected at the finish step.",
    )
    p_upload.add_argument(
        "--finish-length-prefix",
        action="store_true",
        help="use the older 8-byte (length+sum) finish checksum instead of the 4-byte sum-only format "
        "upstream now believes is correct -- debugging escape hatch only, not recommended",
    )
    p_upload.add_argument("--chunk-size", type=int, default=ble.DEFAULT_CHUNK_SIZE)
    p_upload.add_argument("--retries", type=int, default=3, help="resend a chunk this many times if the badge goes quiet before giving up")
    p_upload.add_argument(
        "--fragment-size",
        type=int,
        default=ble.DEFAULT_FRAGMENT_SIZE,
        help="split each frame into physical BLE writes of this many bytes (0 = one write per frame, relying on the negotiated MTU; default 20 matches the OEM app)",
    )
    p_upload.add_argument("--fragment-gap-ms", type=float, default=ble.DEFAULT_FRAGMENT_GAP_S * 1000, help="pause between fragments, in milliseconds")
    p_upload.add_argument("--chunk-timeout", type=float, default=ble.NOTIFY_TIMEOUT_S, help="seconds to wait for a chunk's ack before resending it")
    p_upload.add_argument("--inter-chunk-delay-ms", type=float, default=0.0, help="extra pause after each acked chunk, before sending the next one")
    p_upload.add_argument("--finish-timeout", type=float, default=45.0, help="seconds to wait for the final ack after all chunks are sent")
    p_upload.add_argument("--finish-retries", type=int, default=2, help="how many times to resend the finish frame if it never gets a reply")
    p_upload.add_argument("--finish-delay-ms", type=float, default=1000.0, help="pause after the last chunk's ack before sending the finish frame")
    p_upload.add_argument("--save-payload", metavar="PATH", help="also save the exact encoded bytes sent (RU50 blob or raw RGB565), for debugging")
    p_upload.add_argument(
        "--pad-to-screen-bytes",
        action="store_true",
        help="DIAGNOSTIC ONLY, not a real fix: append zero bytes after the encoded payload until its total "
        "length equals width*height*2 (the raw-RGB565 size for this screen). Tests the hypothesis that the "
        "badge's cmd31 finish never replies because it's silently waiting for that many bytes to arrive, "
        "regardless of what length --format ru50 (or anything else) actually declared -- see BITACORA.md's "
        "2026-08-31 '5 minutos de silencio total' entry for why this came up (RESULT: falsified -- padding to "
        "115200 bytes still got silence, not the explicit reject RGB565 gets, so this flag is unlikely to help "
        "on its own; kept for reference)",
    )
    p_upload.add_argument(
        "--ru50-zero-unknown-fields",
        action="store_true",
        help="DIAGNOSTIC ONLY (--format ru50 only): zero out the RU50 header's 6 fields with genuinely unknown "
        "meaning (lifted verbatim from one specific vendor-tool binary extraction) instead of their captured "
        "values, to test whether they're real fixed requirements or artifacts of that one extraction. See "
        "ru50.build_ru50_blob's docstring and BITACORA.md's 2026-08-31 entries",
    )
    p_upload.add_argument(
        "--upload-attempts",
        type=int,
        default=1,
        help="if the badge goes silent mid-transfer, disconnect, reconnect, and redo the WHOLE upload from scratch, up to this many times (the protocol has no way to resume mid-stream). Defaults to 1 (no auto-retry) on purpose: while debugging an unfamiliar failure mode, retrying by default hides how many attempts actually failed and why -- pass a higher value once a failure is understood to be transient",
    )
    p_upload.add_argument(
        "--upload-attempt-delay",
        type=float,
        default=15.0,
        help="seconds to wait before a whole-upload retry (default 15, up from an earlier 2s) -- if the badge is "
        "still busy digesting the previous attempt's data (real firmware writes/verification can be slow), "
        "reconnecting too fast just gets an immediate refusal every time; see BITACORA.md's 2026-08-31 entries",
    )

    p_find = sub.add_parser("find", help="trigger the badge's find-me buzzer/flash", parents=[common])
    p_find.add_argument("device", help="BLE address or name substring")

    p_services = sub.add_parser(
        "services",
        help="list every advertised GATT service/characteristic (read-only, no writes -- useful to spot a DFU/OTA service)",
        parents=[common],
    )
    p_services.add_argument("device", help="BLE address or name substring")

    p_info = sub.add_parser(
        "device-info",
        help="read manufacturer/model/firmware/hardware/software revision and battery level (standard BLE reads, no writes)",
        parents=[common],
    )
    p_info.add_argument("device", help="BLE address or name substring")

    return parser


async def _cmd_scan(args: argparse.Namespace) -> int:
    devices = await ble.scan(timeout=args.timeout)
    if not devices:
        print("No BLE devices found. Make sure the badge is on and nearby.")
        return 1
    for d in devices:
        print(f"{d.address}  {d.name or '(no name)'}")
    return 0


async def _cmd_dial_dims(args: argparse.Namespace) -> int:
    async with await ble.EBadge.connect(args.device) as badge:
        info = await badge.dial_dims()
        print(f"screen_type={info.screen_type} grade={info.grade} width={info.width} height={info.height}")
        print(f"expected RGB565 payload size: {info.rgb565_bytes} bytes")
    return 0


def _parse_rgb(spec: str) -> tuple[int, int, int]:
    parts = spec.split(",")
    if len(parts) != 3:
        raise ValueError(f"expected R,G,B (e.g. 0,0,0), got {spec!r}")
    r, g, b = (int(p.strip()) for p in parts)
    return r, g, b


def _encode_payload(args: argparse.Namespace, width: int, height: int) -> bytes:
    if args.format == "ru50":
        zero_unknown = args.ru50_zero_unknown_fields
        suffix = ", header unknown-fields=zeroed (diagnostic)" if zero_unknown else ""
        if args.solid:
            color = _parse_rgb(args.solid)
            payload = ru50.solid_ru50(width, height, color, zero_unknown_fields=zero_unknown)
            print(f"encoded solid color {color} -> {len(payload)} bytes RU50/ETC2 ({width}x{height}{suffix})")
        else:
            payload = ru50.image_to_ru50(args.image, width, height, fit=args.fit, zero_unknown_fields=zero_unknown)
            print(f"encoded {args.image} -> {len(payload)} bytes RU50/ETC2 ({width}x{height}, fit={args.fit}{suffix})")
        return payload

    if args.solid:
        color = _parse_rgb(args.solid)
        payload = image.solid_rgb565(width, height, color)
        print(f"encoded solid color {color} -> {len(payload)} bytes RGB565 ({width}x{height})")
    else:
        payload = image.image_to_rgb565(args.image, width, height, fit=args.fit)
        print(f"encoded {args.image} -> {len(payload)} bytes RGB565 ({width}x{height}, fit={args.fit})")
    return payload


async def _cmd_upload_dial(args: argparse.Namespace) -> int:
    if bool(args.image) == bool(args.solid):
        print("error: pass exactly one of IMAGE or --solid R,G,B", file=sys.stderr)
        return 2

    if args.format == "ru50":
        try:
            import etcpak  # noqa: F401
        except ImportError:
            print(
                "error: --format ru50 (the default) needs the 'etcpak' package for ETC2 texture "
                "compression. Install it with `uv add etcpak` (or `pip install etcpak`), or pass "
                "--format rgb565 to fall back to the old raw-pixel format instead.",
                file=sys.stderr,
            )
            return 2

    width, height = args.width, args.height
    payload: bytes | None = None
    last_exc: Exception | None = None

    for attempt in range(1, args.upload_attempts + 1):
        if attempt > 1:
            print(
                f"\nretrying whole upload from scratch (attempt {attempt}/{args.upload_attempts}) "
                f"in {args.upload_attempt_delay:.0f}s..."
            )
            await asyncio.sleep(args.upload_attempt_delay)
        try:
            async with await ble.EBadge.connect(
                args.device, fragment_size=args.fragment_size, fragment_gap_s=args.fragment_gap_ms / 1000
            ) as badge:
                if width is None or height is None:
                    info = await badge.dial_dims()
                    width, height = info.width, info.height
                    print(f"queried dial size: {width}x{height}")

                if payload is None:  # only encode once, reuse across attempts
                    payload = _encode_payload(args, width, height)
                    if args.pad_to_screen_bytes:
                        target = width * height * 2
                        if len(payload) < target:
                            pad = target - len(payload)
                            print(f"padding payload with {pad} zero bytes to reach {target} (diagnostic only)")
                            payload = payload + bytes(pad)
                        elif len(payload) > target:
                            print(f"warning: payload ({len(payload)} bytes) is already larger than {target}, not padding", file=sys.stderr)
                    if args.save_payload:
                        with open(args.save_payload, "wb") as f:
                            f.write(payload)

                def on_progress(p: ble.UploadProgress) -> None:
                    pct = 100 * p.sent_bytes / p.total_bytes
                    print(f"\ruploading: chunk {p.chunk_index}/{p.chunk_count} ({pct:5.1f}%)", end="", flush=True)

                await badge.upload_dial(
                    payload,
                    chunk_size=args.chunk_size,
                    chunk_retries=args.retries,
                    chunk_timeout_s=args.chunk_timeout,
                    inter_chunk_delay_s=args.inter_chunk_delay_ms / 1000,
                    finish_timeout_s=args.finish_timeout,
                    finish_retries=args.finish_retries,
                    finish_delay_s=args.finish_delay_ms / 1000,
                    finish_include_length=args.finish_length_prefix,
                    on_progress=on_progress,
                )
                print("\nupload complete.")
            return 0
        except (ble.ProtocolError, OSError, RuntimeError, TimeoutError) as exc:
            # Broad on purpose: bleak/WinRT can raise plain OSErrors (seen on
            # a real low-battery badge: a reconnect got MTU=23 instead of
            # 517, then start_notify raised a raw WinRT error) that aren't
            # our own ProtocolError -- those still need to hit the retry
            # loop instead of crashing the whole script.
            last_exc = exc
            print(f"\nattempt {attempt}/{args.upload_attempts} failed: {exc}", file=sys.stderr)

    print(f"gave up after {args.upload_attempts} full upload attempts: {last_exc}", file=sys.stderr)
    return 2


async def _cmd_find(args: argparse.Namespace) -> int:
    async with await ble.EBadge.connect(args.device) as badge:
        await badge.find_device(True)
        print("find-me signal sent.")
    return 0


async def _cmd_services(args: argparse.Namespace) -> int:
    print(await ble.dump_services(args.device))
    return 0


async def _cmd_device_info(args: argparse.Namespace) -> int:
    info = await ble.read_device_info(args.device)
    if not info:
        print("no Device Information / Battery characteristics found")
        return 1
    width = max(len(k) for k in info)
    for k, v in info.items():
        print(f"{k.ljust(width)} : {v}")
    return 0


_HANDLERS = {
    "scan": _cmd_scan,
    "dial-dims": _cmd_dial_dims,
    "upload-dial": _cmd_upload_dial,
    "find": _cmd_find,
    "services": _cmd_services,
    "device-info": _cmd_device_info,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    debug = getattr(args, "debug", False)
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_HANDLERS[args.command](args))
    except ble.ProtocolError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
