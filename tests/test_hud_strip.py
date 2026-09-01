"""The HUD-strip fast path must produce results identical to full-frame
capture, or the optimisation is a correctness bug."""

import pytest

from erdle.app import AppConfig, ErdleApp
from erdle.bossdb import BossDatabase, default_data_path
from erdle.detect import Frame, analyse_bar, make_test_frame
from erdle.geometry import (
    BOSS_BAR,
    BOSS_NAME,
    HUD_STRIP,
    STRIP_BOSS_BAR,
    STRIP_BOSS_NAME,
    FractionalRect,
    remap,
)
from erdle.ocr import ScriptedRecogniser, estimate_text_presence
from erdle.state import DetectorConfig, FightState

FULL = (2560, 1440)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def crop_to_strip(frame: Frame) -> Frame:
    return frame.region(HUD_STRIP.resolve(frame.width, frame.height))


# --- remap -----------------------------------------------------------------


def test_remap_is_identity_for_the_whole_frame():
    whole = FractionalRect(0.0, 0.0, 1.0, 1.0)
    assert remap(whole, BOSS_BAR) == BOSS_BAR


def test_remap_expands_the_inner_rect():
    local = remap(HUD_STRIP, BOSS_BAR)
    assert local.right - local.left > BOSS_BAR.right - BOSS_BAR.left
    assert local.bottom - local.top > BOSS_BAR.bottom - BOSS_BAR.top


def test_remap_rejects_an_inner_rect_that_escapes():
    outside = FractionalRect(0.0, 0.0, 0.1, 0.1)
    with pytest.raises(ValueError, match="not contained"):
        remap(HUD_STRIP, outside)


def test_hud_strip_contains_both_regions():
    for region in (BOSS_BAR, BOSS_NAME):
        assert HUD_STRIP.left <= region.left
        assert HUD_STRIP.top <= region.top
        assert HUD_STRIP.right >= region.right
        assert HUD_STRIP.bottom >= region.bottom


def test_strip_is_a_large_pixel_saving():
    """Capture cost is the bottleneck; the strip must stay a small slice.

    The strip grew when name-driven detection arrived -- it now has to
    contain the name band as well as the bar, since the band is what the
    loop reads every poll. Still under a tenth of the screen.
    """
    full = FULL[0] * FULL[1]
    rect = HUD_STRIP.resolve(*FULL)
    assert rect.width * rect.height < full / 10


# --- equivalence -----------------------------------------------------------


@pytest.mark.parametrize("fill", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_strip_detection_matches_full_frame(fill):
    frame = make_test_frame(*FULL, bar_fill=fill)
    full_result = analyse_bar(frame, region=BOSS_BAR)
    strip_result = analyse_bar(crop_to_strip(frame), region=STRIP_BOSS_BAR)
    assert strip_result.present == full_result.present
    assert strip_result.fill_ratio == pytest.approx(full_result.fill_ratio, abs=0.03)


def test_strip_rejects_a_frame_with_no_bar():
    frame = make_test_frame(*FULL, bar_fill=None)
    assert not analyse_bar(crop_to_strip(frame), region=STRIP_BOSS_BAR).present


def test_strip_name_region_still_finds_the_plate():
    frame = make_test_frame(*FULL, bar_fill=1.0, with_name=True)
    strip = crop_to_strip(frame)
    rect = STRIP_BOSS_NAME.resolve(strip.width, strip.height)
    assert estimate_text_presence(strip.region(rect)) > 0.012


def test_strip_name_region_is_blank_without_a_plate():
    frame = make_test_frame(*FULL, bar_fill=1.0, with_name=False)
    strip = crop_to_strip(frame)
    rect = STRIP_BOSS_NAME.resolve(strip.width, strip.height)
    assert estimate_text_presence(strip.region(rect)) < 0.012


@pytest.mark.parametrize(
    "width,height", [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]
)
def test_strip_works_at_every_resolution(width, height):
    frame = make_test_frame(width, height, bar_fill=0.6)
    result = analyse_bar(crop_to_strip(frame), region=STRIP_BOSS_BAR)
    assert result.present
    assert result.fill_ratio == pytest.approx(0.6, abs=0.04)


# --- through the whole app -------------------------------------------------


def test_app_produces_the_same_screen_either_way(database):
    config_kwargs = {"detector": DetectorConfig(enter_frames=2, exit_frames=10)}
    full_app = ErdleApp(
        database, ScriptedRecogniser(["Starscourge Radahn"]),
        config=AppConfig(**config_kwargs),
    )
    strip_app = ErdleApp(
        database, ScriptedRecogniser(["Starscourge Radahn"]),
        config=AppConfig.for_hud_strip(**config_kwargs),
    )

    for i in range(4):
        frame = make_test_frame(*FULL, bar_fill=1.0)
        full_app.step(frame, i / 30)
        strip_app.step(crop_to_strip(frame), i / 30)

    assert full_app.tracker.state is strip_app.tracker.state is FightState.FIGHTING
    assert full_app.tracker.snapshot.boss.key == strip_app.tracker.snapshot.boss.key
    assert full_app.last_canvas.to_rows() == strip_app.last_canvas.to_rows()


def test_for_hud_strip_allows_overrides():
    config = AppConfig.for_hud_strip(idle_message="TARNISHED")
    assert config.idle_message == "TARNISHED"
    assert config.bar_region == STRIP_BOSS_BAR
