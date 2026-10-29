import io
import struct
import unittest
from PIL import Image
from ebadge import jpeg

class JpegProtocolTest(unittest.TestCase):
    def test_start_matches_successful_phone_capture(self):
        self.assertEqual(jpeg.start(11189).hex(),
            'cd00161f01020011000015a2020800000000002bb500000000')

    def test_chunk_checksum_is_32_bit_and_includes_sequence(self):
        data = bytes([255])*5000
        frame = jpeg.chunk(1, data)
        self.assertEqual(frame[:10].hex(), 'cd13931f0101138e0001')
        self.assertEqual(int.from_bytes(frame[-4:], 'big'), 1275001)
        self.assertEqual(frame[10:-4], data)

    def test_notification_ack_matches_phone_capture(self):
        response=bytes.fromhex('cd00092001010004000003e9')
        self.assertEqual(jpeg.ack(response).hex(),'dc00052001000c01')

    def test_jpeg_envelope(self):
        blob=jpeg.encode(None,240,240,solid=(12,34,56))
        self.assertEqual(struct.unpack('>I',blob[:4])[0],len(blob)-4)
        with Image.open(io.BytesIO(blob[4:])) as im:
            self.assertEqual(im.format,'JPEG')
            self.assertEqual(im.size,(240,240))
            im.load()

if __name__=='__main__':unittest.main()
