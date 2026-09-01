"""Regressions for three symptoms reported from live play.

    1. The panel sat on "UNKNOWN BOSS / NO DATA" during ordinary
       exploration instead of ERDLE.
    2. Deaths and wins were never detected; it stayed on the boss screen.
    3. The loading screen after a death briefly showed ERDLE, then went
       back to "UNKNOWN BOSS".

(1) and (3) are the same bug seen from two angles: the bar detector fired
on red scenery, and a black loading screen was the only thing that made it
stop. (2) is separate -- "YOU DIED" is dark red, and the banner gate was
looking for pixels brighter than 170.
"""

# These pin the ORIGINAL bar-driven detector, kept behind
# `AppConfig(name_driven=False)`. They are still worth running: the flag
# exists as a fallback for machines where polling OCR is too slow, and it
# must stay correct.
#
# The current default sidesteps all three symptoms by construction -- with
# no boss name there is nothing to match, so no fight can start regardless
# of what the terrain does. See tests/test_nametrack.py.


import random

import pytest

from erdle.app import AppConfig, ErdleApp
from erdle.banner import BannerKind, adaptive_threshold, looks_like_banner
from erdle.bossdb import BossDatabase, default_data_path
from erdle.detect import Frame, analyse_bar, make_test_frame
from erdle.geometry import BOSS_BAR, FractionalRect
from erdle.ocr import ScriptedRecogniser, _luma
from erdle.render import render_defeat_screen, render_idle_screen, render_victory_screen
from erdle.state import DetectorConfig, FightState

W, H = 1280, 720
BANNER_W, BANNER_H = 576, 108
MEASURED_BAR = FractionalRect(0.2427, 0.8028, 0.7573, 0.8120)

# Ground colours from a real 4K Limgrave capture.
TERRAIN = [
    ("sunlit dirt", (153, 115, 69)),
    ("red-brown rock", (120, 70, 55)),
    ("dark red mud", (95, 48, 40)),
    ("grass", (96, 120, 52)),
    ("cave", (28, 26, 30)),
    ("blood-red cliff", (140, 45, 40)),
    ("sunset haze", (180, 90, 60)),
    ("pitch black", (4, 4, 4)),
    ("snow", (210, 215, 225)),
    ("torchlit stone", (70, 55, 45)),
]

# Elden Ring's banner colours. YOU DIED is much darker than it looks.
DEATH_TEXT = (110, 30, 30)      # luma ~47
FELLED_TEXT = (208, 178, 96)    # luma ~178


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def textured(base, seed, noise=45, width=W, height=H):
    """Noisy ground. Uniform fills are far too easy a test."""
    random.seed(seed)
    return Frame(
        width, height,
        [
            tuple(max(0, min(255, c + random.randint(-noise, noise))) for c in base)
            for _ in range(width * height)
        ],
    )


def banner_frame(text_colour, background, span=0.55):
    pixels = [background] * (BANNER_W * BANNER_H)
    left = int(BANNER_W * (1 - span) / 2)
    for y in range(BANNER_H // 2 - 9, BANNER_H // 2 + 9):
        for x in range(left, left + int(BANNER_W * span)):
            if (x // 3 + y // 4) % 3 == 0:
                pixels[y * BANNER_W + x] = text_colour
    return Frame(BANNER_W, BANNER_H, pixels)


# --- bug 1 and 3: false fights during exploration -------------------------


@pytest.mark.parametrize("label,base", TERRAIN)
def test_scenery_is_not_a_boss_fight(label, base):
    frame = textured(base, seed=hash(label) % 1000)
    assert not analyse_bar(frame, region=BOSS_BAR).present, label


@pytest.mark.parametrize("label,base", TERRAIN)
def test_the_panel_stays_idle_while_exploring(database, label, base):
    """The user-visible symptom: ERDLE, not UNKNOWN BOSS."""
    app = ErdleApp(
        database,
        ScriptedRecogniser([]),
        config=AppConfig(name_driven=False, detector=DetectorConfig(enter_frames=3, exit_frames=10)),
    )
    frame = textured(base, seed=hash(label) % 1000)
    for i in range(10):
        app.step(frame, i * 0.1)
    assert app.tracker.state is FightState.IDLE, label
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_a_single_red_speck_is_not_a_fight():
    """The precise defect: one health-coloured pixel near the left edge."""
    pixels = [(20, 18, 16)] * (W * H)
    rect = BOSS_BAR.resolve(W, H)
    for y in range(rect.top, rect.bottom):
        pixels[y * W + rect.left + 2] = (80, 0, 0)
    assert not analyse_bar(Frame(W, H, pixels), region=BOSS_BAR).present


def test_sparse_red_pixels_do_not_chain_into_a_run():
    """Gap-bridging alone would join these; density is what rejects them."""
    pixels = [(20, 18, 16)] * (W * H)
    rect = BOSS_BAR.resolve(W, H)
    for y in range(rect.top, rect.bottom):
        for x in range(rect.left, rect.left + int(rect.width * 0.6), 7):
            pixels[y * W + x] = (80, 0, 0)
    assert not analyse_bar(Frame(W, H, pixels), region=BOSS_BAR).present


def test_a_real_bar_over_the_same_terrain_is_still_found():
    """The fix must not have simply switched detection off."""
    for label, base in TERRAIN:
        frame = make_test_frame(
            W, H, bar_fill=0.6, bar_region=MEASURED_BAR,
            background=base, health_colour=(80, 0, 0),
        )
        result = analyse_bar(frame, region=BOSS_BAR)
        assert result.present, label
        assert result.fill_ratio == pytest.approx(0.6, abs=0.05), label


# --- bug 2: the death banner was invisible to the gate --------------------


def test_death_text_is_darker_than_the_old_fixed_cutoff():
    """Documents why deaths were never seen."""
    assert _luma(DEATH_TEXT) < 170


def test_the_gate_sees_the_dark_death_banner():
    frame = banner_frame(DEATH_TEXT, (8, 6, 6))
    observation = looks_like_banner(frame)
    assert observation.present
    assert observation.threshold < _luma(DEATH_TEXT)


def test_the_gate_still_sees_the_bright_victory_banner():
    assert looks_like_banner(banner_frame(FELLED_TEXT, (30, 26, 22))).present


def test_the_cutoff_adapts_between_the_two():
    dark = adaptive_threshold(banner_frame(DEATH_TEXT, (8, 6, 6)), _thresholds())
    bright = adaptive_threshold(
        banner_frame(FELLED_TEXT, (90, 84, 76)), _thresholds()
    )
    assert dark < bright, "cutoff should track the scene's own brightness"


def _thresholds():
    from erdle.banner import DEFAULT_BANNER_THRESHOLDS

    return DEFAULT_BANNER_THRESHOLDS


@pytest.mark.parametrize("label,base", TERRAIN)
def test_scenery_does_not_trigger_the_banner_gate(label, base):
    frame = textured(base, seed=hash(label) % 500, width=BANNER_W, height=BANNER_H)
    assert not looks_like_banner(frame).present, label


# --- the whole reported sequence ------------------------------------------


def test_explore_fight_die_reload_explore(database):
    """Walk the exact path the user described, end to end."""
    app = ErdleApp(
        database,
        ScriptedRecogniser(["Tree Sentinel", "YOU DIED"]),
        config=AppConfig(name_driven=False, 
            detector=DetectorConfig(enter_frames=2, exit_frames=8),
            event_screen_seconds=4.0,
        ),
    )
    dirt = textured((153, 115, 69), seed=1)
    dark = Frame(BANNER_W, BANNER_H, [(20, 20, 20)] * (BANNER_W * BANNER_H))
    death = banner_frame(DEATH_TEXT, (8, 6, 6))
    fight = make_test_frame(
        W, H, bar_fill=1.0, bar_region=MEASURED_BAR,
        background=(153, 115, 69), health_colour=(80, 0, 0), with_name=True,
    )
    black = Frame(W, H, [(2, 2, 2)] * (W * H))

    now = 0.0

    # exploring
    for _ in range(8):
        app.step(dirt, now, dark)
        now += 0.1
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()

    # a fight begins
    for _ in range(4):
        app.step(fight, now, dark)
        now += 0.1
    assert app.tracker.snapshot.boss.key == "tree_sentinel"

    # death
    app.step(fight, now, death)
    assert app.last_canvas.to_rows() == render_defeat_screen().to_rows()
    now += 1.0

    # loading screen
    for _ in range(10):
        app.step(black, now, dark)
        now += 0.5

    # back to exploring -- this is where it used to revert to UNKNOWN BOSS
    for _ in range(10):
        app.step(dirt, now, dark)
        now += 0.1
    assert app.tracker.state is FightState.IDLE
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_explore_fight_win(database):
    app = ErdleApp(
        database,
        ScriptedRecogniser(["Tree Sentinel", "GREAT ENEMY FELLED"]),
        config=AppConfig(name_driven=False, 
            detector=DetectorConfig(enter_frames=2, exit_frames=8),
            event_screen_seconds=4.0,
        ),
    )
    dark = Frame(BANNER_W, BANNER_H, [(20, 20, 20)] * (BANNER_W * BANNER_H))
    fight = make_test_frame(
        W, H, bar_fill=0.4, bar_region=MEASURED_BAR,
        background=(96, 120, 52), health_colour=(80, 0, 0), with_name=True,
    )
    now = 0.0
    for _ in range(4):
        app.step(fight, now, dark)
        now += 0.1
    app.step(fight, now, banner_frame(FELLED_TEXT, (30, 26, 22)))
    assert app.last_canvas.to_rows() == render_victory_screen().to_rows()
