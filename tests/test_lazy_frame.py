"""Lazy BGRA-backed frames.

At 4K the HUD strip is 432k pixels and converting all of them to tuples
costs ~100ms -- more than the entire frame budget at 15fps. The detector
reads three scanlines out of it. Deferring conversion to the pixels
actually read is the difference between 9fps and 15.
"""

import pytest

from erdle.detect import Frame, analyse_bar, make_test_frame
from erdle.geometry import BOSS_BAR, FractionalRect


def bgra_buffer(width, height, colour=(80, 0, 0)):
    """Build a BGRA byte buffer of a single colour."""
    red, green, blue = colour
    return bytes([blue, green, red, 255]) * (width * height)


def test_lazy_frame_reports_itself():
    frame = Frame.from_bgra(bgra_buffer(4, 3), 4, 3)
    assert frame.is_lazy
    assert not Frame(2, 2, [(0, 0, 0)] * 4).is_lazy


def test_lazy_pixel_matches_eager():
    buffer = bgra_buffer(4, 3, (10, 20, 30))
    lazy = Frame.from_bgra(buffer, 4, 3)
    eager = Frame(4, 3, [(10, 20, 30)] * 12)
    for y in range(3):
        for x in range(4):
            assert lazy.pixel(x, y) == eager.pixel(x, y)


def test_lazy_channel_order_is_bgra_to_rgb():
    # One pixel: B=1, G=2, R=3
    frame = Frame.from_bgra(bytes([1, 2, 3, 255]), 1, 1)
    assert frame.pixel(0, 0) == (3, 2, 1)


def test_lazy_scanline_matches_eager():
    buffer = bytearray()
    for y in range(3):
        for x in range(5):
            buffer += bytes([x, y, x + y, 255])       # B, G, R
    lazy = Frame.from_bgra(bytes(buffer), 5, 3)
    assert lazy.scanline(1, 1, 4) == [(2, 1, 1), (3, 1, 2), (4, 1, 3)]


def test_lazy_region_produces_an_eager_crop():
    frame = Frame.from_bgra(bgra_buffer(20, 10, (7, 8, 9)), 20, 10)
    crop = frame.region(FractionalRect(0.2, 0.2, 0.6, 0.8).resolve(20, 10))
    assert not crop.is_lazy
    assert crop.pixel(0, 0) == (7, 8, 9)


def test_lazy_respects_stride():
    """mss can hand back rows padded beyond width * 4."""
    width, height, stride = 3, 2, 4 * 4      # one pixel of padding per row
    buffer = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            i = y * stride + x * 4
            buffer[i:i + 4] = bytes([0, 0, x + y * 10, 255])
    frame = Frame.from_bgra(bytes(buffer), width, height, stride=stride)
    assert frame.pixel(2, 1) == (12, 0, 0)


def test_lazy_rejects_a_short_buffer():
    with pytest.raises(ValueError, match="need"):
        Frame.from_bgra(bytes(10), 10, 10)


def test_lazy_rejects_bad_dimensions():
    with pytest.raises(ValueError):
        Frame.from_bgra(bytes(16), 0, 4)


# --- the detector must not care which backing it gets ---------------------


def eager_to_bgra(frame: Frame) -> bytes:
    buffer = bytearray()
    for y in range(frame.height):
        for x in range(frame.width):
            r, g, b = frame.pixel(x, y)
            buffer += bytes([b, g, r, 255])
    return bytes(buffer)


@pytest.mark.parametrize("fill", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_detection_is_identical_on_both_backings(fill):
    eager = make_test_frame(1280, 720, bar_fill=fill)
    lazy = Frame.from_bgra(eager_to_bgra(eager), 1280, 720)

    a = analyse_bar(eager, region=BOSS_BAR)
    b = analyse_bar(lazy, region=BOSS_BAR)
    assert a.present == b.present
    assert a.fill_ratio == pytest.approx(b.fill_ratio)
    assert a.health_pixels == b.health_pixels


def test_no_false_positive_on_a_lazy_frame():
    eager = make_test_frame(1280, 720, bar_fill=None)
    lazy = Frame.from_bgra(eager_to_bgra(eager), 1280, 720)
    assert not analyse_bar(lazy, region=BOSS_BAR).present


def test_lazy_frame_touches_only_what_it_reads():
    """The whole point: reading a bar must not cost the whole frame."""
    reads = {"count": 0}

    class CountingBuffer(bytes):
        def __getitem__(self, item):
            reads["count"] += 1
            return super().__getitem__(item)

    width, height = 2000, 91
    buffer = CountingBuffer(bgra_buffer(width, height, (80, 0, 0)))
    frame = Frame.from_bgra(buffer, width, height)
    analyse_bar(frame, region=FractionalRect(0.0, 0.0, 1.0, 1.0), scanlines=3)

    # Three scanlines of 2000 pixels, three byte reads each.
    assert reads["count"] < width * height, "converted the whole frame"
    assert reads["count"] < 3 * width * 4
