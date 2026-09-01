"""Detector tests run entirely on synthesised frames.

These verify the *logic* -- geometry, classification, ratio maths,
resolution independence. They cannot verify the colour thresholds, which
depend on real Elden Ring output. See README for the calibration step.
"""

import pytest

from erdle.detect import (
    DEFAULT_THRESHOLDS,
    BarThresholds,
    Frame,
    analyse_bar,
    is_depleted_pixel,
    is_health_pixel,
    make_test_frame,
)
from erdle.geometry import BOSS_BAR, FractionalRect

RESOLUTIONS = [(1280, 720), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160)]


# --- pixel classification --------------------------------------------------


def test_health_pixel_recognises_bar_red():
    assert is_health_pixel((150, 34, 30))
    assert is_health_pixel((120, 20, 22))


def test_health_pixel_rejects_grey_and_dark():
    assert not is_health_pixel((90, 90, 90))
    assert not is_health_pixel((20, 5, 5))       # too dark
    assert not is_health_pixel((150, 140, 30))   # yellow, not red enough


def test_depleted_pixel_recognises_empty_track():
    assert is_depleted_pixel((24, 20, 18))
    assert is_depleted_pixel((0, 0, 0))


def test_depleted_pixel_rejects_bright():
    assert not is_depleted_pixel((150, 34, 30))
    assert not is_depleted_pixel((200, 200, 200))


def test_classifications_are_mutually_exclusive():
    for colour in [(150, 34, 30), (24, 20, 18), (0, 0, 0), (255, 255, 255)]:
        assert not (is_health_pixel(colour) and is_depleted_pixel(colour))


# --- presence --------------------------------------------------------------


def test_detects_bar_when_present():
    frame = make_test_frame(1920, 1080, bar_fill=1.0)
    result = analyse_bar(frame)
    assert result.present


def test_no_bar_on_plain_gameplay_frame():
    frame = make_test_frame(1920, 1080, bar_fill=None)
    assert not analyse_bar(frame).present


def test_all_black_frame_is_not_a_bar():
    """A loading screen is uniformly dark and would otherwise pass the
    coverage test, since every pixel classifies as 'depleted'."""
    black = Frame(1920, 1080, [(0, 0, 0)] * (1920 * 1080))
    assert not analyse_bar(black).present


def test_all_white_frame_is_not_a_bar():
    white = Frame(800, 600, [(255, 255, 255)] * (800 * 600))
    assert not analyse_bar(white).present


def test_nearly_dead_boss_still_registers_as_present():
    frame = make_test_frame(1920, 1080, bar_fill=0.03)
    result = analyse_bar(frame)
    assert result.present, "bar must not vanish at low health"


def test_a_sliver_below_the_floor_is_given_up():
    """Deliberate trade-off.

    A run shorter than `min_fill_run` is indistinguishable from a speck of
    red scenery, and treating specks as boss fights was a real bug: the
    panel sat on "unknown boss" through ordinary exploration. A boss under
    2% health dies within a second or two, and the exit hysteresis holds
    the screen until the victory banner takes over.
    """
    assert not analyse_bar(make_test_frame(1920, 1080, bar_fill=0.005)).present


def test_zero_health_bar_is_not_claimed():
    """There is no fallback for an empty bar; see analyse_bar.

    "Uniformly dark region" describes a depleted bar, but also every cave
    and night sky in the game -- and a false fight in a dark room is much
    more expensive than missing the last instant of a real one.
    """
    assert not analyse_bar(make_test_frame(1920, 1080, bar_fill=0.0)).present


# --- fill ratio ------------------------------------------------------------


@pytest.mark.parametrize("expected", [0.05, 0.25, 0.5, 0.75, 1.0])
def test_fill_ratio_tracks_bar(expected):
    frame = make_test_frame(1920, 1080, bar_fill=expected)
    result = analyse_bar(frame)
    assert result.present
    assert result.fill_ratio == pytest.approx(expected, abs=0.02)


def test_percent_helper_rounds():
    frame = make_test_frame(1920, 1080, bar_fill=0.5)
    assert analyse_bar(frame).percent == pytest.approx(50, abs=2)


def test_absent_bar_reports_zero_fill():
    frame = make_test_frame(1920, 1080, bar_fill=None)
    assert analyse_bar(frame).fill_ratio == 0.0


# --- resolution independence ----------------------------------------------


@pytest.mark.parametrize("width,height", RESOLUTIONS)
def test_detection_survives_every_common_resolution(width, height):
    frame = make_test_frame(width, height, bar_fill=0.6)
    result = analyse_bar(frame)
    assert result.present, f"missed bar at {width}x{height}"
    assert result.fill_ratio == pytest.approx(0.6, abs=0.03)


@pytest.mark.parametrize("width,height", RESOLUTIONS)
def test_no_false_positive_at_every_resolution(width, height):
    frame = make_test_frame(width, height, bar_fill=None)
    assert not analyse_bar(frame).present, f"false positive at {width}x{height}"


# --- robustness ------------------------------------------------------------


def test_scanline_count_is_clamped_to_region_height():
    frame = make_test_frame(320, 180, bar_fill=0.5)
    result = analyse_bar(frame, scanlines=99)
    assert result.present


def test_single_scanline_still_works():
    """Row agreement cannot demand more rows than were sampled."""
    frame = make_test_frame(1920, 1080, bar_fill=0.4)
    result = analyse_bar(frame, scanlines=1)
    assert result.present
    assert result.fill_ratio == pytest.approx(0.4, abs=0.03)


def test_thresholds_are_tunable():
    """A washed-out red reads as an empty bar under default thresholds.

    The bar is still *detected* -- an empty bar is a bar -- but none of it
    counts as health until the thresholds are loosened. This is exactly the
    failure mode calibration exists to fix.
    """
    frame = make_test_frame(1920, 1080, bar_fill=1.0, health_colour=(70, 60, 58))
    # Under default thresholds none of it reads as health, so there is no
    # run to anchor on and the bar is not claimed at all.
    assert not analyse_bar(frame).present

    loose = BarThresholds(health_min_red=60, health_red_ratio=1.05)
    tuned = analyse_bar(frame, thresholds=loose)
    assert tuned.present
    assert tuned.fill_ratio == pytest.approx(1.0, abs=0.01)


def test_frame_rejects_mismatched_pixel_count():
    with pytest.raises(ValueError, match="expected"):
        Frame(10, 10, [(0, 0, 0)] * 99)


def test_frame_rejects_zero_dimensions():
    with pytest.raises(ValueError):
        Frame(0, 10, [])


def test_region_crop_has_expected_size():
    frame = make_test_frame(1920, 1080, bar_fill=1.0)
    rect = BOSS_BAR.resolve(1920, 1080)
    crop = frame.region(rect)
    assert crop.width == rect.width
    assert crop.height == rect.height


# --- geometry --------------------------------------------------------------


def test_fractional_rect_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        FractionalRect(0.8, 0.1, 0.2, 0.9)
    with pytest.raises(ValueError):
        FractionalRect(0.1, 0.9, 0.9, 0.2)


def test_fractional_rect_rejects_out_of_range():
    with pytest.raises(ValueError):
        FractionalRect(-0.1, 0.1, 0.5, 0.5)


def test_resolve_never_produces_empty_rect():
    tiny = FractionalRect(0.5, 0.5, 0.5001, 0.5001)
    rect = tiny.resolve(100, 100)
    assert rect.width >= 1 and rect.height >= 1


def test_resolve_stays_inside_frame():
    rect = FractionalRect(0.0, 0.0, 1.0, 1.0).resolve(640, 480)
    assert rect.left >= 0 and rect.top >= 0
    assert rect.right <= 640 and rect.bottom <= 480


def test_resolve_rejects_bad_frame_size():
    with pytest.raises(ValueError):
        BOSS_BAR.resolve(0, 100)
