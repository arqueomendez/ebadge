# BG02 JPEG transfer profile — LJ733_MB_V1.5 / V35922

Image upload now works from Linux on one BG02 unit after comparing a successful Android SuperBand transfer with the Python implementation. The badge returned the final success status (`2`), and the owner confirmed that the uploaded “LINUX TEST” image appeared on the physical display.

This is a verified profile for the device below; it does **not** establish that other DG01/LJ733 variants or 360×360 devices use the same format, or that their RU50 observations are incorrect.

## Tested configuration

- Advertised name: BG02
- Manufacturer characteristic: `LJ733_V1_BadgeOK`
- Hardware: `LJ733_MB_V1.5`
- Firmware: `V35922` (factory-shipped version according to the owner; no firmware update performed)
- Display: 240×240
- Stock app: SuperBand 2.1.25, `com.legend.smartwatch.electronicbadge.android`
- Phone OS: Android 12
- Linux client: Python 3.12 / Bleak 3.0.2 / BlueZ
- Python implementation base: `arqueomendez/ebadge` commit `e68d163e619ea93c520efc46b8b3f94ce8b9f225`, with local modifications

## Successful Android capture

The payload is a **big-endian u32 JPEG byte length followed by an ordinary JPEG**. The recovered JPEG opened as a valid 240×240 image. It was not RU50 in this transfer.

The captured JPEG was 11,185 bytes, so the complete payload was 11,189 bytes (`0x2bb5`):

```text
00 00 2b b1 ff d8 ff e0 00 10 4a 46 49 46 ...
```

Start frame:

```text
cd 00 16 1f 01 02 00 11
00 00 15 a2 02 08 00 00 00 00 00 2b b5 00 00 00 00
```

For an arbitrary payload, the 17-byte start body is:

```text
00 00 15 a2 02 08 00 00 00 | payload_length:u32be | 00 00 00 00
```

Chunks use cmd `31`, sub `1`:

```text
sequence:u16be | up to 5000 payload bytes | checksum:u32be
```

- Sequence starts at **1**.
- Checksum is the sum of the two sequence bytes and the chunk bytes, modulo 2^32.
- Captured chunk sizes were 5000, 5000, and 1189 bytes.
- Captured checksums were `0x0008bdf2`, `0x00097648`, and `0x0002267b`.
- The badge reported statuses 1000 (start), then 1001, 1002, and 1003.

Finish is cmd `31`, sub `3`, with the u32be sum of **all complete-payload bytes**, including the four-byte JPEG length:

```text
cd 00 09 1f 01 03 00 04 00 14 5a af
```

The badge then reported success:

```text
cd 00 09 20 01 01 00 04 00 00 00 02
```

The app acknowledged CD notifications with a short DC frame. For each 12-byte status notification:

```text
dc 00 05 20 01 00 0c 01
```

The general observed acknowledgment construction is:

```text
dc 00 05 | received_cmd | received_sub | received_frame_length:u16be | 01
```

The observed UART service was `7e400001-b5a3-f393-e0a9-e50e24dcca9d` (TX `...0002`, notify `...0003`). Android used ATT write values up to 482 bytes. The successful Linux run used conservative 20-byte writes with a 3 ms gap instead.

## Validation and limits

The captured start frame, all three data frames, and finish frame were reconstructed byte-for-byte. The recovered payload length, JPEG decoding, all chunk sums, and final sum were checked.

The modified Linux uploader then sent a newly generated test image, not a replay of the phone image: 8,774 total bytes in two chunks. It received the final success response and the owner confirmed the correct display. Four regression tests cover the start frame, 32-bit chunk checksum, notification acknowledgment, and JPEG length envelope.

The changes were tested together. We have not independently established which acknowledgment/timing details are strictly required, or whether other chunk sizes and start-field values are accepted. Those start fields are recorded exactly as observed; their semantic meanings remain unverified. RU50 and raw ETC2 attempts had previously failed on this same unit.

A separate Linux discovery fix resolves cached BlueZ devices before scanning: connected devices may stop advertising, making scan-only lookup fail despite an existing connection.

The implementation and this analysis were developed with Codex assistance; the successful phone capture, Linux completion response, and physical display check provide the evidence above.

No full Android bug report, phone identifiers, Bluetooth addresses, APK, or personal image is included in this report.

## Firmware update availability check

On 2026-10-29, the owner reported that SuperBand's update check indicated the installed firmware was already the latest. The app showed V35922 for the connected device. No firmware update was performed, so the successful Linux result remains a factory-firmware V35922 test; there is no post-update comparison. This observation is limited to this unit and the app's update offering at that time.

## Usage in this implementation

```bash
uv run python main.py upload-dial <MAC> image.png --format jpeg
```

The existing default (`--format ru50`) and legacy start/chunk framing remain unchanged. CD notification acknowledgments are enabled for JPEG uploads, including the initial dimension query. The JPEG profile selects 5000-byte chunks; `--chunk-size` continues to control legacy uploads only. No new dependencies are required.

Hardware-free tests also exercise the full upload/ACK loop, legacy framing preservation, the CLI default and JPEG encoder selection, and cached BlueZ lookup with a scan fallback.

## PR integration recheck

The PR version keeps JPEG explicitly opt-in. It was rerun against the same unit and received final success after reconnecting. The first integration recheck timed out waiting for the first chunk status; a fresh connection with the same code completed. Connection reliability is not established by this limited test, and the existing retry/debug options remain useful.
