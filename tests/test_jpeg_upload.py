"""Exercise the real upload/ACK loop without Bluetooth hardware."""
import unittest
from unittest.mock import AsyncMock, MagicMock
from ebadge import jpeg, protocol as proto
from ebadge.ble import EBadge
from ebadge.cli import build_parser, _encode_payload


def status_frame(status):
    return proto.get_protocol(32, 1, status.to_bytes(4, 'big'))


class UploadProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_jpeg_upload_sends_captured_profile_and_acknowledges_statuses(self):
        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        badge = EBadge(client, 'tx', [], fragment_size=0)
        payload = bytes(range(256)) * 20
        badge._frames.put_nowait(bytes.fromhex('dc00051f02001901'))
        for status in (1000, 1001, 1002, 2):
            badge._frames.put_nowait(status_frame(status))
        await badge.upload_dial(payload, jpeg_profile=True, finish_delay_s=0)
        frames = [call.args[1] for call in client.write_gatt_char.await_args_list]
        self.assertEqual([f for f in frames if f[0] == 0xcd], [
            jpeg.start(len(payload)), jpeg.chunk(1, payload[:5000]),
            jpeg.chunk(2, payload[5000:]), proto.dial_finish_payload(payload),
        ])
        self.assertEqual([f for f in frames if f[0] == 0xdc],
                         [bytes.fromhex('dc00052001000c01')] * 4)
        self.assertTrue(badge._frames.empty())

    async def test_legacy_upload_keeps_original_wire_format(self):
        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        badge = EBadge(client, 'tx', [], fragment_size=0)
        payload = b'legacy'
        for status in (1000, 1001, 2):
            badge._frames.put_nowait(status_frame(status))
        await badge.upload_dial(payload, finish_delay_s=0)
        frames = [call.args[1] for call in client.write_gatt_char.await_args_list]
        self.assertEqual(frames, [proto.dial_start_extended(len(payload)),
                                 proto.uart_file_chunk(0, payload),
                                 proto.dial_finish_payload(payload)])

    async def test_dc_ack_does_not_get_an_ack_reply(self):
        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        badge = EBadge(client, 'tx', [], ack_notifications=True)
        frame = bytes.fromhex('dc00051f02001901')
        badge._frames.put_nowait(frame)
        self.assertEqual(await badge._next_frame(), frame)
        client.write_gatt_char.assert_not_awaited()


class CliJpegTest(unittest.TestCase):
    def test_existing_default_stays_ru50(self):
        args = build_parser().parse_args(['upload-dial', 'BG02', '--solid', '0,0,0'])
        self.assertEqual(args.format, 'ru50')

    def test_jpeg_format_can_encode_a_solid_image(self):
        args = build_parser().parse_args(['upload-dial', 'BG02', '--format', 'jpeg', '--solid', '0,0,0'])
        payload = _encode_payload(args, 240, 240)
        self.assertEqual(int.from_bytes(payload[:4], 'big'), len(payload)-4)
        self.assertEqual(payload[4:6], b'\xff\xd8')
