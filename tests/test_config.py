import pytest

"""Config isolation.

Python 3.14 rejects mutable dataclass defaults outright; older versions
silently share one instance across every object. Both are bugs, so these
tests assert the behaviour rather than the Python version.
"""

from dataclasses import fields

from erdle.app import AppConfig
from erdle.detect import BarThresholds
from erdle.state import DetectorConfig


def test_each_config_gets_its_own_detector():
    a, b = AppConfig(), AppConfig()
    assert a.detector is not b.detector


def test_each_config_gets_its_own_thresholds():
    a, b = AppConfig(), AppConfig()
    assert a.thresholds is not b.thresholds


def test_mutating_one_config_does_not_affect_another():
    a, b = AppConfig(), AppConfig()
    a.detector.enter_frames = 99
    assert b.detector.enter_frames != 99


def test_strip_config_is_also_isolated():
    a, b = AppConfig.for_hud_strip(), AppConfig.for_hud_strip()
    assert a.detector is not b.detector
    a.detector.exit_frames = 1234
    assert b.detector.exit_frames != 1234


def test_no_mutable_dataclass_defaults_remain():
    """Catches this class of bug for any field added later."""
    for spec in fields(AppConfig):
        default = spec.default
        if default is None or repr(default) == "<factory>":
            continue
        # Anything left as a plain default must be immutable (hashable).
        hash(default)


def test_defaults_are_the_documented_values():
    config = AppConfig()
    assert config.detector.enter_frames == DetectorConfig().enter_frames
    assert config.thresholds.health_min_red == BarThresholds().health_min_red


# --- overlay settings ------------------------------------------------------


def test_overlay_settings_round_trip():
    from erdle.config import Config

    config = Config()
    config.move_overlay(0.8, 0.05)
    config.overlay_scale = 1.5
    config.overlay_enabled = False

    restored = Config.from_dict(config.to_dict())
    assert (restored.overlay_fx, restored.overlay_fy) == (0.8, 0.05)
    assert restored.overlay_scale == 1.5
    assert restored.overlay_enabled is False


def test_overlay_position_defaults_to_unset():
    """None means "the default corner", resolved against the live screen.

    Storing a computed default would strand the window off-screen the first
    time someone unplugs a monitor.
    """
    from erdle.config import Config

    config = Config()
    assert config.overlay_fx is None and config.overlay_fy is None
    assert config.overlay_x is None and config.overlay_y is None
    assert config.overlay_enabled is True


def test_overlay_values_are_clamped_not_rejected():
    """A window the user cannot see is one they cannot drag back."""
    from erdle.config import Config

    config = Config.from_dict(
        {"overlay": {"scale": 40, "opacity": 0.0, "x": "nonsense"}}
    )
    assert 0.6 <= config.overlay_scale <= 3.0
    assert 0.25 <= config.overlay_opacity <= 1.0
    assert config.overlay_x is None


@pytest.mark.parametrize(
    "payload",
    [
        {"overlay": "garbage"},
        {"overlay": {}},
        {"overlay": {"scale": float("nan")}},
        {"overlay": {"scale": None, "opacity": None}},
        {},
    ],
)
def test_malformed_overlay_blocks_fall_back_to_defaults(payload):
    from erdle.config import Config

    config = Config.from_dict(payload)
    assert config.overlay_scale == 1.0
    assert config.overlay_opacity == 0.88


def test_overlay_position_is_stored_as_fractions():
    """Pixels are meaningless on a different display; fractions are not."""
    from erdle.config import Config

    config = Config()
    config.move_overlay(0.97, 0.04)
    restored = Config.from_dict(config.to_dict())
    assert restored.overlay_fx == pytest.approx(0.97)
    assert restored.overlay_fy == pytest.approx(0.04)


def test_moving_the_overlay_retires_the_legacy_pixels():
    from erdle.config import Config

    config = Config.from_dict({"overlay": {"x": 2900, "y": 60}})
    assert config.overlay_x == 2900
    config.move_overlay(0.5, 0.5)
    assert config.overlay_x is None and config.overlay_y is None


def test_legacy_pixel_position_survives_a_read(database=None):
    """An upgrade must not silently move someone's window."""
    from erdle.config import Config

    config = Config.from_dict({"overlay": {"x": 3400, "y": 108}})
    assert (config.overlay_x, config.overlay_y) == (3400, 108)
    assert config.overlay_fx is None


@pytest.mark.parametrize(
    "value,expected",
    [(2.5, 1.0), (-1, 0.0), (0.5, 0.5), ("nonsense", None), (None, None)],
)
def test_overlay_fractions_are_clamped_or_dropped(value, expected):
    from erdle.config import Config

    config = Config.from_dict({"overlay": {"fx": value}})
    if expected is None:
        assert config.overlay_fx is None
    else:
        assert config.overlay_fx == pytest.approx(expected)


def test_reset_overlay_position_clears_both_forms():
    from erdle.config import Config

    config = Config.from_dict({"overlay": {"x": 100, "y": 100, "fx": 0.5, "fy": 0.5}})
    config.reset_overlay_position()
    assert config.overlay_fx is None and config.overlay_x is None
