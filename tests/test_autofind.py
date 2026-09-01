"""Automatic bar discovery.

This is what rescues a user whose HUD sits somewhere the shipped defaults
don't expect -- so it has to work when the defaults are wrong, which is
exactly the case the rest of the suite cannot cover.
"""

import pytest

from erdle.calibrate import find_bar, longest_run, sample_bar_colours, suggest_regions
from erdle.detect import Frame, make_test_frame
from erdle.geometry import FractionalRect

# Where the bar actually sits, measured from 3840x2160 gameplay.
REAL_BAR = FractionalRect(left=0.232, top=0.792, right=0.768, bottom=0.811)


def frame_with_bar_at(region, width=1920, height=1080, fill=1.0, noisy=False):
    frame = make_test_frame(
        width, height, bar_fill=fill, bar_region=region,
        background=(96, 84, 52) if noisy else (18, 16, 14),
    )
    return frame


# --- run finding -----------------------------------------------------------


def test_longest_run_finds_the_red_span():
    pixels = [(20, 20, 20)] * 10 + [(150, 34, 30)] * 30 + [(20, 20, 20)] * 10
    frame = Frame(50, 1, pixels)
    length, start, _ = longest_run(frame, 0, step=1)
    assert length == pytest.approx(30, abs=2)
    assert start == pytest.approx(10, abs=2)


def test_longest_run_ignores_short_noise():
    pixels = [(150, 34, 30)] * 3 + [(20, 20, 20)] * 20 + [(150, 34, 30)] * 25
    frame = Frame(48, 1, pixels)
    length, start, _ = longest_run(frame, 0, step=1)
    assert length == pytest.approx(25, abs=2)
    assert start == pytest.approx(23, abs=2)


def test_longest_run_does_not_escape_into_dark_background():
    """Matching the empty track too would swallow any night sky or shadow."""
    pixels = [(12, 12, 12)] * 30 + [(150, 34, 30)] * 10 + [(12, 12, 12)] * 30
    frame = Frame(70, 1, pixels)
    length, start, _ = longest_run(frame, 0, step=1)
    assert length == pytest.approx(10, abs=2)
    assert start == pytest.approx(30, abs=2)


def test_longest_run_on_an_empty_row():
    frame = Frame(20, 1, [(10, 10, 10)] * 20)
    assert longest_run(frame, 0, step=1)[0] == 0


# --- discovery -------------------------------------------------------------


def test_finds_the_bar_where_it_really_is():
    frame = frame_with_bar_at(REAL_BAR, 3840, 2160)
    found = find_bar(frame)
    assert found is not None
    f = found["fractions"]
    assert f["top"] == pytest.approx(REAL_BAR.top, abs=0.006)
    assert f["bottom"] == pytest.approx(REAL_BAR.bottom, abs=0.006)
    assert f["left"] == pytest.approx(REAL_BAR.left, abs=0.01)
    assert f["right"] == pytest.approx(REAL_BAR.right, abs=0.01)


@pytest.mark.parametrize(
    "width,height", [(1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160)]
)
def test_finds_the_bar_at_any_resolution(width, height):
    found = find_bar(frame_with_bar_at(REAL_BAR, width, height))
    assert found is not None
    assert found["fractions"]["top"] == pytest.approx(REAL_BAR.top, abs=0.01)


def test_finds_a_bar_in_an_unexpected_place():
    """The whole point: it must not assume the shipped defaults are right."""
    odd = FractionalRect(left=0.15, top=0.70, right=0.85, bottom=0.72)
    found = find_bar(frame_with_bar_at(odd, 2560, 1440))
    assert found is not None
    assert found["fractions"]["top"] == pytest.approx(0.70, abs=0.01)
    assert found["fractions"]["left"] == pytest.approx(0.15, abs=0.02)


def test_returns_none_with_no_bar_on_screen():
    assert find_bar(make_test_frame(1920, 1080, bar_fill=None)) is None


def test_returns_none_on_a_dark_frame():
    assert find_bar(Frame(1920, 1080, [(0, 0, 0)] * (1920 * 1080))) is None


# Backgrounds that broke earlier versions. The first two are the actual
# colours reported by a real 4K capture in Limgrave, where red-dominant
# dirt matched a loose ratio and the "bar" swallowed half the screen.
TERRAIN = [
    ("sunlit dirt", (153, 115, 69)),
    ("false-health shade", (140, 79, 48)),
    ("red-brown cliff", (120, 70, 55)),
    ("dark cave", (28, 26, 30)),
    ("snow", (210, 215, 225)),
    ("grass", (96, 120, 52)),
]
BAR_RED = (150, 40, 40)


@pytest.mark.parametrize("label,background", TERRAIN)
def test_finds_the_bar_over_any_terrain(label, background):
    frame = make_test_frame(
        3840, 2160, bar_fill=1.0, bar_region=REAL_BAR,
        background=background, health_colour=BAR_RED,
    )
    found = find_bar(frame)
    assert found is not None, f"missed the bar over {label}"
    f = found["fractions"]
    assert f["top"] == pytest.approx(REAL_BAR.top, abs=0.01)
    assert f["left"] == pytest.approx(REAL_BAR.left, abs=0.01)
    assert f["right"] == pytest.approx(REAL_BAR.right, abs=0.01)


@pytest.mark.parametrize("label,background", TERRAIN)
def test_no_false_positive_over_any_terrain(label, background):
    """Red-dominant ground must never be mistaken for a health bar."""
    frame = make_test_frame(3840, 2160, bar_fill=None, background=background)
    assert find_bar(frame) is None, f"false positive over {label}"


def test_rejects_a_tall_block_of_red():
    """A wall of red is terrain; the bar is a thin strip."""
    tall = FractionalRect(left=0.232, top=0.60, right=0.768, bottom=0.90)
    frame = make_test_frame(
        1920, 1080, bar_fill=1.0, bar_region=tall, health_colour=BAR_RED
    )
    assert find_bar(frame) is None


def test_rejects_a_run_spanning_the_whole_screen():
    wide = FractionalRect(left=0.01, top=0.80, right=0.99, bottom=0.82)
    frame = make_test_frame(
        1920, 1080, bar_fill=1.0, bar_region=wide, health_colour=BAR_RED
    )
    assert find_bar(frame) is None


def test_reports_which_ratio_succeeded():
    """Tells the user what to set in detect.py rather than making them guess."""
    frame = make_test_frame(
        3840, 2160, bar_fill=1.0, bar_region=REAL_BAR,
        background=(153, 115, 69), health_colour=BAR_RED,
    )
    found = find_bar(frame)
    assert found["ratio_used"] >= 2.0, "saturated bar red should match strictly"


def test_a_washed_out_bar_still_found_at_a_looser_ratio():
    frame = make_test_frame(
        3840, 2160, bar_fill=1.0, bar_region=REAL_BAR,
        background=(20, 20, 22), health_colour=(110, 66, 62),
    )
    found = find_bar(frame)
    assert found is not None
    assert found["ratio_used"] < 2.0


def test_finds_a_partially_drained_bar():
    found = find_bar(frame_with_bar_at(REAL_BAR, 1920, 1080, fill=0.45))
    assert found is not None
    assert found["fractions"]["top"] == pytest.approx(REAL_BAR.top, abs=0.01)


def test_ignores_a_bar_starting_right_of_centre():
    """The boss bar is centred, so its left edge is never past the middle."""
    off_centre = FractionalRect(left=0.55, top=0.80, right=0.95, bottom=0.82)
    assert find_bar(frame_with_bar_at(off_centre, 1920, 1080)) is None


def test_ignores_a_band_too_thin_to_be_a_hud_element():
    sliver = FractionalRect(left=0.232, top=0.800, right=0.768, bottom=0.8015)
    assert find_bar(frame_with_bar_at(sliver, 1920, 1080)) is None


def test_recovers_full_width_from_a_half_drained_bar():
    """Measuring only the red would report a region far too narrow."""
    frame = frame_with_bar_at(REAL_BAR, 3840, 2160, fill=0.4)
    found = find_bar(frame)
    assert found is not None
    assert found["fractions"]["right"] == pytest.approx(REAL_BAR.right, abs=0.015)
    assert found["bar_looked_full"] is False
    assert found["estimated_fill"] == pytest.approx(0.4, abs=0.06)


def test_reports_a_full_bar_as_full():
    found = find_bar(frame_with_bar_at(REAL_BAR, 3840, 2160, fill=1.0))
    assert found["bar_looked_full"] is True


# --- suggestions -----------------------------------------------------------


def test_suggestions_are_valid_python_and_parse_back():
    found = find_bar(frame_with_bar_at(REAL_BAR, 3840, 2160))
    text = suggest_regions(found)
    namespace = {"FractionalRect": FractionalRect}
    exec(text, namespace)  # noqa: S102 - our own generated text
    assert isinstance(namespace["BOSS_BAR"], FractionalRect)
    assert isinstance(namespace["BOSS_NAME"], FractionalRect)
    assert isinstance(namespace["HUD_STRIP"], FractionalRect)


def test_suggested_strip_contains_both_regions():
    found = find_bar(frame_with_bar_at(REAL_BAR, 3840, 2160))
    namespace = {"FractionalRect": FractionalRect}
    exec(suggest_regions(found), namespace)  # noqa: S102
    strip, bar, name = (
        namespace["HUD_STRIP"], namespace["BOSS_BAR"], namespace["BOSS_NAME"]
    )
    for region in (bar, name):
        assert strip.left <= region.left and strip.right >= region.right
        assert strip.top <= region.top and strip.bottom >= region.bottom


def test_suggested_name_region_sits_above_the_bar():
    found = find_bar(frame_with_bar_at(REAL_BAR, 3840, 2160))
    namespace = {"FractionalRect": FractionalRect}
    exec(suggest_regions(found), namespace)  # noqa: S102
    assert namespace["BOSS_NAME"].bottom <= namespace["BOSS_BAR"].top


def test_suggested_regions_actually_detect_the_bar():
    """Closing the loop: the output must work when fed back in."""
    from erdle.detect import analyse_bar

    frame = frame_with_bar_at(REAL_BAR, 3840, 2160, fill=0.6)
    namespace = {"FractionalRect": FractionalRect}
    exec(suggest_regions(find_bar(frame)), namespace)  # noqa: S102
    result = analyse_bar(frame, region=namespace["BOSS_BAR"])
    assert result.present
    assert result.fill_ratio == pytest.approx(0.6, abs=0.06)


# --- colour sampling -------------------------------------------------------


def test_colour_sampling_separates_filled_from_empty():
    frame = frame_with_bar_at(REAL_BAR, 1920, 1080, fill=0.5)
    colours = sample_bar_colours(frame, find_bar(frame))
    assert colours["filled_avg"] == [150, 34, 30]
    assert colours["filled_pixels"] > 0
    assert colours["empty_pixels"] > 0


def test_colour_sampling_reports_threshold_agreement():
    frame = frame_with_bar_at(REAL_BAR, 1920, 1080)
    colours = sample_bar_colours(frame, find_bar(frame))
    assert colours["passes_current_thresholds"] == colours["filled_pixels"]
