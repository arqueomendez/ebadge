from PIL import Image

from ebadge.image import image_to_rgb565


def test_solid_color_encodes_to_expected_rgb565(tmp_path):
    # Pure red (255,0,0) -> R:11111 G:000000 B:00000 -> 0xF800 -> LE bytes 00 F8
    path = tmp_path / "red.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(path)
    data = image_to_rgb565(path, 4, 4, fit="stretch")
    assert len(data) == 4 * 4 * 2
    assert data[0:2] == bytes([0x00, 0xF8])


def test_output_size_matches_target_resolution_regardless_of_source_size(tmp_path):
    path = tmp_path / "src.png"
    Image.new("RGB", (1000, 400), (10, 20, 30)).save(path)
    data = image_to_rgb565(path, 360, 360, fit="cover")
    assert len(data) == 360 * 360 * 2
