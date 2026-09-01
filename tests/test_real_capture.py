"""Regression tests built from a real 3840x2160 Elden Ring capture.

Every constant here was measured, not guessed:

    filled bar      (80, 0, 0)      -- dark and fully saturated
    sunlit dirt     (153, 115, 69)  -- red-dominant, a false-positive trap
    misread shade   (140, 79, 48)   -- passed the original thresholds
    bar position    y 0.8028-0.8120, x 0.2427-0.7573

The bug these exist to prevent: fill used to be computed as
health/(health+depleted), which requires the empty track to classify
correctly. It does not, because the track is drawn over arbitrary
terrain. Detection worked at full health and broke the instant the boss
took damage -- the only case that matters.
"""

import pytest

from erdle.detect import (
    BarThresholds,
    Frame,
    analyse_bar,
    is_health_pixel,
    make_test_frame,
)
from erdle.geometry import BOSS_BAR, BOSS_NAME, HUD_STRIP, FractionalRect

MEASURED_BAR = FractionalRect(left=0.2427, top=0.8028, right=0.7573, bottom=0.8120)
BAR_FILLED = (80, 0, 0)
SUNLIT_DIRT = (153, 115, 69)
MISREAD_SHADE = (140, 79, 48)
FOUR_K = (3840, 2160)

# The empty track is drawn over whatever is behind the bar, so its colour
# is genuinely unpredictable. Detection must not depend on it.
EMPTY_TRACK_VARIANTS = [
    ("dark translucent", (34, 28, 26)),
    ("over bright dirt", (96, 78, 58)),
    ("over grass", (70, 84, 44)),
    ("over sky", (120, 130, 140)),
]


def real_frame(fill, *, background=SUNLIT_DIRT, empty=(34, 28, 26)):
    return make_test_frame(
        *FOUR_K, bar_fill=fill, bar_region=MEASURED_BAR,
        background=background, health_colour=BAR_FILLED, empty_colour=empty,
    )


# --- the measured colour must classify correctly ---------------------------


def test_measured_bar_colour_reads_as_health():
    assert is_health_pixel(BAR_FILLED)


def test_measured_dirt_does_not_read_as_health():
    """This is what produced 417 phantom health pixels."""
    assert not is_health_pixel(SUNLIT_DIRT)


def test_the_shade_that_fooled_the_old_thresholds_is_rejected():
    assert not is_health_pixel(MISREAD_SHADE)


def test_health_threshold_has_margin_below_the_measured_colour():
    assert BarThresholds().health_min_red < BAR_FILLED[0]


# --- shipped regions must match the real capture ---------------------------


def test_shipped_bar_region_covers_the_measured_bar():
    assert BOSS_BAR.top <= MEASURED_BAR.top
    assert BOSS_BAR.bottom >= MEASURED_BAR.bottom
    assert BOSS_BAR.left <= MEASURED_BAR.left + 0.006
    assert BOSS_BAR.right >= MEASURED_BAR.right - 0.006


def test_shipped_name_region_sits_above_the_bar():
    assert BOSS_NAME.bottom <= BOSS_BAR.top


def test_shipped_strip_contains_both():
    for region in (BOSS_BAR, BOSS_NAME):
        assert HUD_STRIP.top <= region.top and HUD_STRIP.bottom >= region.bottom
        assert HUD_STRIP.left <= region.left and HUD_STRIP.right >= region.right


def test_scanlines_land_inside_the_real_bar():
    """Padding the region too generously puts the outer scanlines off it."""
    from erdle.detect import _scanline_rows

    rect = BOSS_BAR.resolve(*FOUR_K)
    real = MEASURED_BAR.resolve(*FOUR_K)
    for y in _scanline_rows(rect, 3):
        assert real.top <= y < real.bottom, f"scanline {y} outside {real}"


# --- the bug: detection across the whole health range ----------------------


@pytest.mark.parametrize("fill", [1.0, 0.9, 0.75, 0.5, 0.25, 0.1, 0.05, 0.03])
def test_detects_a_damaged_boss(fill):
    """Used to work only at full health."""
    result = analyse_bar(real_frame(fill), region=BOSS_BAR)
    assert result.present, f"lost the bar at {fill:.0%} health"
    assert result.fill_ratio == pytest.approx(fill, abs=0.05)


@pytest.mark.parametrize("label,empty", EMPTY_TRACK_VARIANTS)
def test_fill_is_correct_whatever_the_empty_track_looks_like(label, empty):
    result = analyse_bar(real_frame(0.4, empty=empty), region=BOSS_BAR)
    assert result.present, f"lost the bar with {label} track"
    assert result.fill_ratio == pytest.approx(0.4, abs=0.05), label


@pytest.mark.parametrize("label,background", [
    ("sunlit dirt", SUNLIT_DIRT),
    ("misread shade", MISREAD_SHADE),
    ("grass", (96, 120, 52)),
    ("cave", (28, 26, 30)),
])
def test_no_false_positive_without_a_bar(label, background):
    frame = make_test_frame(*FOUR_K, bar_fill=None, background=background)
    assert not analyse_bar(frame, region=BOSS_BAR).present, label


def test_fill_decreases_monotonically():
    previous = 1.01
    for step in range(20, -1, -1):
        fill = step / 20
        result = analyse_bar(real_frame(fill), region=BOSS_BAR)
        assert result.fill_ratio <= previous + 0.02
        previous = max(result.fill_ratio, 0.0)


def test_particles_over_the_bar_do_not_split_the_run():
    """A spell effect drawn across the bar must not halve the reading."""
    frame = real_frame(0.8)
    rect = BOSS_BAR.resolve(*FOUR_K)
    pixels = [frame.pixel(x, y) for y in range(frame.height) for x in range(frame.width)]
    # Punch a narrow bright gap through the middle of the filled portion.
    gap_start = rect.left + int(rect.width * 0.3)
    for y in range(rect.top, rect.bottom):
        for x in range(gap_start, gap_start + int(rect.width * 0.015)):
            pixels[y * frame.width + x] = (240, 230, 200)
    punched = Frame(frame.width, frame.height, pixels)
    result = analyse_bar(punched, region=BOSS_BAR)
    assert result.present
    assert result.fill_ratio == pytest.approx(0.8, abs=0.06)


def test_an_empty_bar_is_deliberately_not_claimed():
    """See analyse_bar: a uniformly dark region is also every cave."""
    result = analyse_bar(real_frame(0.0, empty=(34, 28, 26)), region=BOSS_BAR)
    assert not result.present
    assert result.fill_ratio == 0.0


# --- through the HUD strip fast path ---------------------------------------


@pytest.mark.parametrize("fill", [1.0, 0.6, 0.2])
def test_strip_path_agrees_at_every_health_level(fill):
    from erdle.geometry import STRIP_BOSS_BAR

    frame = real_frame(fill)
    strip = frame.region(HUD_STRIP.resolve(*FOUR_K))
    full_result = analyse_bar(frame, region=BOSS_BAR)
    strip_result = analyse_bar(strip, region=STRIP_BOSS_BAR)
    assert strip_result.present == full_result.present
    assert strip_result.fill_ratio == pytest.approx(full_result.fill_ratio, abs=0.04)
