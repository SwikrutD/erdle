"""Auto-calibration: when to search, and when to leave well alone.

Field bug this file exists for: on a 3840x2160 display -- the exact
resolution the shipped regions were measured on -- the sweep found a red
band at 0.5596 screen height and moved the boss bar there. The panel then
read the wrong rows and never saw another boss.
"""

from __future__ import annotations

from erdle.autocal import AutoCalibrator, defaults_fit
from erdle.calibrate import find_bar
from erdle.config import Config
from erdle.detect import Frame
from erdle.geometry import BOSS_BAR, BOSS_NAME, HUD_STRIP

BACKDROP = (20, 20, 24)


def blank(width: int, height: int) -> Frame:
    return Frame(width, height, [BACKDROP] * (width * height))


def with_band(
    width: int, height: int, top: float, bottom: float, left: float, right: float
) -> Frame:
    pixels = [BACKDROP] * (width * height)
    for y in range(int(height * top), int(height * bottom)):
        for x in range(int(width * left), int(width * right)):
            pixels[y * width + x] = (150, 40, 40)
    return Frame(width, height, pixels)


# --- aspect gate -----------------------------------------------------------


def test_defaults_fit_only_on_the_reference_aspect():
    for width, height in ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 720)):
        assert defaults_fit(width, height), f"{width}x{height}"
    for width, height in ((3440, 1440), (1920, 1200), (2560, 1600), (0, 0)):
        assert not defaults_fit(width, height), f"{width}x{height}"


def test_sixteen_by_nine_adopts_defaults_without_sweeping():
    """Searching a 16:9 screen can only replace correct regions with a guess."""
    config = Config()
    calibrator = AutoCalibrator()

    # A frame that would have tempted the old sweep: a wide red band at
    # exactly the height the field bug latched onto.
    frame = with_band(384, 216, 0.5596, 0.5848, 0.287, 0.751)
    assert calibrator.attempt(frame, config, now=1.0) is True

    assert config.boss_bar == BOSS_BAR, "defaults were overwritten"
    assert config.boss_name == BOSS_NAME
    assert config.hud_strip == HUD_STRIP
    assert config.calibrated is True
    assert config.calibrated_for == "384x216"


def test_adopting_defaults_stops_further_attempts():
    config = Config()
    calibrator = AutoCalibrator()
    calibrator.attempt(blank(384, 216), config, now=1.0)
    assert calibrator.succeeded is True
    assert calibrator.should_attempt(1000.0, already_calibrated=True) is False


# --- off-aspect displays still search --------------------------------------


def test_off_aspect_display_still_sweeps():
    """An ultrawide gets no free ride. A blank frame yields nothing."""
    config = Config()
    calibrator = AutoCalibrator()
    assert calibrator.attempt(blank(344, 144), config, now=1.0) is False
    assert config.boss_bar == BOSS_BAR
    assert config.calibrated is False


# --- the sweep's own search window -----------------------------------------


def test_sweep_ignores_mid_screen_bands():
    """The false positive itself, rebuilt at 0.56 screen height."""
    frame = with_band(1280, 720, 0.5596, 0.5848, 0.287, 0.751)
    assert find_bar(frame) is None, "mid-screen band accepted as a boss bar"


def test_sweep_still_finds_a_bar_where_one_belongs():
    """Same band, moved to where Elden Ring actually draws it."""
    # 1280x720 so the 1.3%-tall bar is still ten rows deep; at 640x360 it
    # is thinner than the sweep's minimum band and nothing could match.
    frame = with_band(1280, 720, 0.800, 0.814, 0.239, 0.761)
    assert find_bar(frame) is not None, "real bar position rejected"
