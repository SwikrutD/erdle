"""Name-driven fight detection.

One signal: a boss name on screen is the fight. The old design had bar
detection and OCR as independent systems that could disagree, and every
field bug came from that -- red terrain tripped the bar, no name resolved,
and the panel showed "unknown boss" in an empty field.

These tests exist mostly to prove that class of bug is now impossible.
"""

import pytest

from erdle.app import AppConfig, ErdleApp
from erdle.banner import BannerKind, make_banner_frame
from erdle.bossdb import BossDatabase, default_data_path
from erdle.detect import Frame, make_test_frame
from erdle.geometry import NAME_BAND, BOSS_NAME, FractionalRect
from erdle.matching import MatchResult
from erdle.nametrack import NameTracker, NameTrackerConfig
from erdle.ocr import ScriptedRecogniser
from erdle.render import render_defeat_screen, render_idle_screen, render_victory_screen
from erdle.state import EventKind, FightState

# 720p keeps the suite fast; every region is fractional so the behaviour
# is identical at 4K. Frames are cached because building one in pure
# Python is the slowest thing in these tests.
FOUR_K = (1280, 720)
_CACHE: dict = {}
BANNER_W, BANNER_H = 576, 108
MEASURED_BAR = FractionalRect(0.2427, 0.8028, 0.7573, 0.8120)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def kinds(events):
    return [e.kind for e in events]


def hit(database, key, confidence=0.95):
    entry = database.require(key)
    return MatchResult(key, entry.name, confidence, None, 0.3), entry


# --- the tracker -----------------------------------------------------------


def test_starts_idle():
    assert NameTracker().state is FightState.IDLE


def test_a_matched_name_starts_a_fight(database):
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    events = tracker.observe(match, boss, 0.0)
    assert kinds(events) == [EventKind.FIGHT_STARTED, EventKind.BOSS_IDENTIFIED]
    assert tracker.state is FightState.FIGHTING
    assert tracker.snapshot.boss.key == "tree_sentinel"


def test_no_match_keeps_it_idle():
    tracker = NameTracker()
    for i in range(20):
        assert tracker.observe(None, None, float(i)) == []
    assert tracker.state is FightState.IDLE


def test_the_fight_ends_when_the_name_goes(database):
    tracker = NameTracker(NameTrackerConfig(exit_misses=3))
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    assert tracker.observe(None, None, 1.0) == []
    assert tracker.observe(None, None, 2.0) == []
    events = tracker.observe(None, None, 3.0)
    assert kinds(events) == [EventKind.FIGHT_ENDED]
    assert tracker.state is FightState.IDLE


def test_a_single_dropped_read_does_not_end_the_fight(database):
    """OCR occasionally loses a frame to a particle effect."""
    tracker = NameTracker(NameTrackerConfig(exit_misses=3))
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    tracker.observe(None, None, 1.0)
    tracker.observe(match, boss, 2.0)
    tracker.observe(None, None, 3.0)
    assert tracker.state is FightState.FIGHTING


def test_repeated_matches_do_not_restart_the_fight(database):
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    for i in range(1, 10):
        assert tracker.observe(match, boss, float(i)) == []


def test_a_new_name_is_a_phase_change_not_a_new_fight(database):
    tracker = NameTracker()
    first, radagon = hit(database, "radagon")
    second, beast = hit(database, "elden_beast")
    tracker.observe(first, radagon, 0.0)
    events = tracker.observe(second, beast, 1.0)
    assert kinds(events) == [EventKind.BOSS_CHANGED]
    assert EventKind.FIGHT_STARTED not in kinds(events)
    assert tracker.snapshot.boss.key == "elden_beast"
    assert events[0].previous_boss.key == "radagon"


def test_a_low_confidence_read_cannot_rename_the_boss(database):
    """A garbled read must not swap the boss mid-fight."""
    tracker = NameTracker(NameTrackerConfig(switch_confidence=0.75))
    first, radagon = hit(database, "radagon")
    tracker.observe(first, radagon, 0.0)
    shaky, beast = hit(database, "elden_beast", confidence=0.66)
    assert tracker.observe(shaky, beast, 1.0) == []
    assert tracker.snapshot.boss.key == "radagon"


def test_phase_change_resets_the_best_attempt(database):
    tracker = NameTracker()
    first, radagon = hit(database, "radagon")
    tracker.observe(first, radagon, 0.0)
    tracker.set_fill(0.04)
    assert tracker.snapshot.lowest_fill == pytest.approx(0.04)
    second, beast = hit(database, "elden_beast")
    tracker.observe(second, beast, 1.0)
    assert tracker.snapshot.lowest_fill == pytest.approx(tracker.snapshot.fill_ratio)


def test_fight_ended_reports_duration_and_best(database):
    tracker = NameTracker(NameTrackerConfig(exit_misses=1))
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 100.0)
    tracker.set_fill(0.11)
    event = tracker.observe(None, None, 160.0)[0]
    assert event.duration == pytest.approx(60.0)
    assert event.lowest_fill == pytest.approx(0.11)
    assert event.boss.key == "tree_sentinel"


# --- bar fill is display only ---------------------------------------------


def test_fill_never_starts_a_fight():
    """The whole point: the bar cannot put us into a fight any more."""
    tracker = NameTracker()
    for _ in range(50):
        tracker.set_fill(1.0)
    assert tracker.state is FightState.IDLE
    assert tracker.snapshot.boss is None


def test_fill_never_ends_a_fight(database):
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    for _ in range(50):
        tracker.set_fill(0.0)
    assert tracker.state is FightState.FIGHTING


def test_fill_tracks_the_best_attempt(database):
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    for value in (0.9, 0.4, 0.07, 0.5):
        tracker.set_fill(value)
    assert tracker.snapshot.lowest_fill == pytest.approx(0.07)


def test_fill_is_clamped(database):
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    tracker.set_fill(5.0)
    assert tracker.snapshot.fill_ratio == 1.0
    tracker.set_fill(-3.0)
    assert tracker.snapshot.fill_ratio == 0.0


def test_none_fill_is_ignored(database):
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    tracker.set_fill(0.5)
    tracker.set_fill(None)
    assert tracker.snapshot.fill_ratio == pytest.approx(0.5)


# --- banners ---------------------------------------------------------------


def test_death_and_victory(database):
    for kind, expected in (
        (BannerKind.DEATH, EventKind.DIED),
        (BannerKind.GREAT_ENEMY_FELLED, EventKind.VICTORY),
    ):
        tracker = NameTracker()
        match, boss = hit(database, "tree_sentinel")
        tracker.observe(match, boss, 0.0)
        assert kinds(tracker.note_banner(kind, 5.0)) == [expected]


def test_a_lingering_banner_fires_once(database):
    tracker = NameTracker(NameTrackerConfig(banner_lockout=8.0))
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    fired = []
    for i in range(60):
        fired.extend(tracker.note_banner(BannerKind.DEATH, 5.0 + i * 0.1))
    assert len(fired) == 1


def test_a_banner_does_not_end_the_fight(database):
    """The name disappearing does that. Two drivers would race."""
    tracker = NameTracker()
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    tracker.note_banner(BannerKind.DEATH, 3.0)
    assert tracker.state is FightState.FIGHTING


# --- through the whole app -------------------------------------------------


def name_frame(text_present=True, bar_fill=1.0):
    """A frame with ink in the name band when a name is showing."""
    key = (text_present, bar_fill)
    if key in _CACHE:
        return _CACHE[key]
    frame = _build_name_frame(text_present, bar_fill)
    _CACHE[key] = frame
    return frame


def _build_name_frame(text_present, bar_fill):
    frame = make_test_frame(
        *FOUR_K, bar_fill=bar_fill, bar_region=MEASURED_BAR,
        background=(120, 95, 60), health_colour=(80, 0, 0), with_name=False,
    )
    if not text_present:
        return frame
    pixels = [frame.pixel(x, y) for y in range(FOUR_K[1]) for x in range(FOUR_K[0])]
    rect = BOSS_NAME.resolve(*FOUR_K)
    for y in range(rect.top + 2, min(rect.top + 20, rect.bottom), 2):
        for x in range(rect.left + 5, min(rect.left + 150, rect.right), 2):
            pixels[y * FOUR_K[0] + x] = (238, 232, 218)
    return Frame(FOUR_K[0], FOUR_K[1], pixels)


def empty_field():
    """Textured red terrain -- what used to trip the bar detector."""
    import random

    if "field" in _CACHE:
        return _CACHE["field"]
    random.seed(4)
    width, height = FOUR_K
    frame = Frame(width, height, [
        tuple(max(0, min(255, c + random.randint(-45, 45))) for c in (153, 115, 69))
        for _ in range(width * height)
    ])
    _CACHE["field"] = frame
    return frame


def dark_banner():
    if "dark" not in _CACHE:
        _CACHE["dark"] = Frame(
            BANNER_W, BANNER_H, [(20, 20, 20)] * (BANNER_W * BANNER_H)
        )
    return _CACHE["dark"]


class RegionRecogniser:
    """Answers by which region it was handed.

    The app uses one recogniser for both the name band and the centre
    banner, so a fixture that returns a fixed sequence interleaves the two
    and tests nothing useful. Real OCR distinguishes them by their content;
    here the frame's shape stands in for that.
    """

    def __init__(self, name="", banner=""):
        self.name = name
        self.banner = banner
        self.name_calls = 0
        self.banner_calls = 0

    def read(self, frame, threshold=None):
        if frame.height == BANNER_H:
            self.banner_calls += 1
            return self.banner
        self.name_calls += 1
        return self.name


def build(database, texts, **kwargs):
    kwargs.setdefault("name_poll_interval", 0.0)
    return ErdleApp(database, ScriptedRecogniser(texts), config=AppConfig(**kwargs))


def build_regional(database, name="", banner="", **kwargs):
    kwargs.setdefault("name_poll_interval", 0.0)
    recogniser = RegionRecogniser(name, banner)
    app = ErdleApp(database, recogniser, config=AppConfig(**kwargs))
    return app, recogniser


def test_walking_around_never_shows_a_boss(database):
    """The reported bug, now structurally impossible.

    With no name there is nothing to match, so no fight can begin -- no
    matter what the terrain does to the bar detector.
    """
    app = build(database, [])
    field = empty_field()
    for i in range(30):
        app.step(field, i * 0.5)
    assert app.tracker.state is FightState.IDLE
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_a_red_bar_without_a_name_is_not_a_fight(database):
    """Even a perfect bar reading cannot start a fight on its own."""
    app = build(database, [""])
    for i in range(10):
        app.step(name_frame(text_present=False, bar_fill=1.0), i * 0.5)
    assert app.tracker.state is FightState.IDLE


def test_a_name_starts_the_fight_and_shows_weaknesses(database):
    from erdle.render import render_boss_screen

    app = build(database, ["Tree Sentinel"])
    app.step(name_frame(), 0.0)
    assert app.tracker.snapshot.boss.key == "tree_sentinel"
    expected = render_boss_screen(
        database.require("tree_sentinel"),
        fill_ratio=app.tracker.snapshot.fill_ratio,
    )
    assert app.last_canvas.to_rows() == expected.to_rows()


def test_the_name_going_away_returns_to_idle(database):
    app = build(database, ["Tree Sentinel", "", "", "", ""])
    app.step(name_frame(), 0.0)
    assert app.tracker.state is FightState.FIGHTING
    for i in range(1, 6):
        app.step(name_frame(text_present=False), i * 1.0)
    assert app.tracker.state is FightState.IDLE
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()



def test_die_then_return_to_idle(database):
    app, _ = build_regional(
        database, name="Tree Sentinel", banner="YOU DIED",
        event_screen_seconds=4.0,
    )
    app.step(name_frame(), 0.0, dark_banner())
    assert app.tracker.state is FightState.FIGHTING

    app.step(name_frame(), 1.0, make_banner_frame(BANNER_W, BANNER_H))
    assert app.last_canvas.to_rows() == render_defeat_screen().to_rows()

    # Name gone, message expired -> idle.
    app.recogniser.name = ""
    for i in range(8):
        app.step(name_frame(text_present=False), 20.0 + i)
    assert app.tracker.state is FightState.IDLE
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_win_then_return_to_idle(database):
    app, _ = build_regional(
        database, name="Tree Sentinel", banner="GREAT ENEMY FELLED",
        event_screen_seconds=4.0,
    )
    app.step(name_frame(), 0.0, dark_banner())
    app.step(name_frame(), 1.0, make_banner_frame(BANNER_W, BANNER_H))
    assert app.last_canvas.to_rows() == render_victory_screen().to_rows()


def test_the_banner_and_the_name_do_not_consume_each_other(database):
    """One recogniser serves two regions; each must get its own answer."""
    app, recogniser = build_regional(
        database, name="Tree Sentinel", banner="YOU DIED"
    )
    app.step(name_frame(), 0.0, make_banner_frame(BANNER_W, BANNER_H))
    assert recogniser.name_calls >= 1
    assert recogniser.banner_calls >= 1
    assert app.tracker.snapshot.boss.key == "tree_sentinel"


def test_ocr_is_polled_on_a_timer_not_every_frame(database):
    """OCR is the most expensive thing in the loop."""
    recogniser = ScriptedRecogniser(["Tree Sentinel"])
    app = ErdleApp(
        database, recogniser,
        config=AppConfig(name_poll_interval=1.0),
    )
    frame = name_frame()
    for i in range(150):          # 10 seconds at 15fps
        app.step(frame, i / 15.0)
    assert recogniser.calls <= 12, f"{recogniser.calls} OCR passes in 10s"
    assert recogniser.calls >= 8, "should still be polling regularly"


def test_a_blank_band_costs_no_ocr(database):
    recogniser = ScriptedRecogniser(["Tree Sentinel"])
    app = ErdleApp(database, recogniser, config=AppConfig(name_poll_interval=0.0))
    for i in range(20):
        app.step(name_frame(text_present=False), i * 0.5)
    assert recogniser.calls == 0


def test_the_old_bar_driven_path_still_works(database):
    """Kept behind a flag in case OCR polling is too slow somewhere."""
    from erdle.state import DetectorConfig, FightTracker

    app = ErdleApp(
        database, ScriptedRecogniser(["Tree Sentinel"]),
        config=AppConfig(
            name_driven=False,
            detector=DetectorConfig(enter_frames=2, exit_frames=10),
        ),
    )
    assert isinstance(app.tracker, FightTracker)
    app.step(name_frame(), 0.0)
    app.step(name_frame(), 0.1)
    assert app.tracker.snapshot.boss.key == "tree_sentinel"


def test_the_name_band_needs_no_calibration(database):
    """Fractions, not pixels: the band must work at any resolution."""
    for width, height in [(1280, 720), (1920, 1080), (2560, 1440),
                          (3440, 1440), (3840, 2160)]:
        rect = NAME_BAND.resolve(width, height)
        assert rect.width > 100 and rect.height > 20, f"{width}x{height}"
        measured = BOSS_NAME.resolve(width, height)
        assert rect.left <= measured.left and rect.right >= measured.right
        assert rect.top <= measured.top and rect.bottom >= measured.bottom


# --- cost of polling -------------------------------------------------------
# Polling OCR is the price of the name-driven design. It has to stay off
# the per-frame critical path.


def test_the_ink_gate_does_not_build_a_crop():
    """Measuring the band by cropping it first cost 295ms at 4K."""
    from erdle.ocr import region_ink_fraction

    reads = {"n": 0}

    class Counting(bytes):
        def __getitem__(self, item):
            reads["n"] += 1
            return super().__getitem__(item)

    width, height = 1767, 199
    frame = Frame.from_bgra(
        Counting(bytes([0, 0, 80, 255]) * (width * height)), width, height
    )
    from erdle.geometry import FractionalRect

    rect = FractionalRect(0.0, 0.0, 1.0, 1.0).resolve(width, height)
    region_ink_fraction(frame, rect, step=3)
    assert reads["n"] < width * height, "should not touch every pixel"


def test_the_ink_gate_still_detects_text():
    from erdle.geometry import FractionalRect
    from erdle.ocr import region_ink_fraction

    width, height = 300, 60
    pixels = [(20, 18, 16)] * (width * height)
    for y in range(20, 40):
        for x in range(10, 120):
            pixels[y * width + x] = (240, 235, 220)
    frame = Frame(width, height, pixels)
    rect = FractionalRect(0.0, 0.0, 1.0, 1.0).resolve(width, height)
    assert region_ink_fraction(frame, rect) > 0.05


def test_the_ink_gate_reports_nothing_on_a_dark_band():
    from erdle.geometry import FractionalRect
    from erdle.ocr import region_ink_fraction

    frame = Frame(200, 50, [(20, 18, 16)] * 10000)
    rect = FractionalRect(0.0, 0.0, 1.0, 1.0).resolve(200, 50)
    assert region_ink_fraction(frame, rect) == 0.0


def test_the_strip_still_contains_everything_the_loop_reads():
    """The strip grew to cover the name band; it must cover all three."""
    from erdle.geometry import BOSS_BAR, BOSS_NAME, HUD_STRIP, NAME_BAND

    for region in (BOSS_BAR, BOSS_NAME, NAME_BAND):
        assert HUD_STRIP.left <= region.left and HUD_STRIP.right >= region.right
        assert HUD_STRIP.top <= region.top and HUD_STRIP.bottom >= region.bottom


def test_hud_strip_config_remaps_the_name_band():
    from erdle.geometry import STRIP_NAME_BAND

    assert AppConfig.for_hud_strip().name_band == STRIP_NAME_BAND
    assert AppConfig().name_band != STRIP_NAME_BAND


# --- from a real capture ---------------------------------------------------
# Log excerpt while standing still in the world:
#
#   read='OUFNOPSeWHAITTeMSTadeSLUCH Spaces'  -> no match   (x5 identical)
#   fight started
#
# A player message on the ground drifted into the band. An earlier build
# treated consistent-but-unmatched text as an uncatalogued boss, guarded by
# requiring consecutive reads to agree. A stationary player produces the
# same garbage every poll, so the guard passed.


REAL_GARBAGE = [
    "OUPNOrSeWHATTeMISTdeSUCH Spaces",
    "OUFNOPSeWHAITTeMSTadeSLUCH Spaces",
    "OUFNOPSeWHAITTeMSTadeSLUCH Spaces",
    "OUFNOPSeWHAITTeMSTadeSLUCH Spaces",
    "OUFNOPSeWHAITTeMSTadeSLUCH Spaces",
    "a re cou",
    "J",
]


@pytest.mark.parametrize("text", REAL_GARBAGE)
def test_real_ocr_garbage_matches_no_boss(database, text):
    """First line of defence: none of it resolves to a boss."""
    from erdle.matching import BossNameMatcher

    matcher = BossNameMatcher.from_entries(database, threshold=0.62, min_margin=0.03)
    assert matcher.match(text) is None, text


def test_repeated_identical_garbage_never_starts_a_fight(database):
    """The exact reported sequence, replayed."""
    app, recogniser = build_regional(database, name="")
    for i, text in enumerate(REAL_GARBAGE * 3):
        recogniser.name = text
        app.step(name_frame(), i * 0.7)
    assert app.tracker.state is FightState.IDLE
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_no_match_means_no_fight_full_stop(database):
    """No 'probably a boss' path exists any more."""
    tracker = NameTracker()
    for i in range(50):
        assert tracker.observe(None, None, float(i)) == []
    assert tracker.state is FightState.IDLE


def test_a_misread_name_still_matches(database):
    """OCR read 'PreeSentinel' for 'Tree Sentinel' and matched at 92%."""
    from erdle.matching import BossNameMatcher

    matcher = BossNameMatcher.from_entries(database, threshold=0.62, min_margin=0.03)
    for text in ("TreeSentinel", "PreeSentinel", "PreeSentine"):
        result = matcher.match(text)
        assert result is not None and result.key == "tree_sentinel", text


def test_a_dropped_read_mid_fight_does_not_blank_the_panel(database):
    """Field data shows OCR returning nothing occasionally with ink present."""
    tracker = NameTracker(NameTrackerConfig(exit_misses=4))
    match, boss = hit(database, "tree_sentinel")
    tracker.observe(match, boss, 0.0)
    for i in range(1, 4):
        tracker.observe(None, None, float(i))
    assert tracker.state is FightState.FIGHTING
    tracker.observe(match, boss, 4.0)
    assert tracker.state is FightState.FIGHTING


def test_a_stale_read_cannot_keep_a_fight_alive(database):
    """Regression: the last OCR result was not cleared when the ink gate
    failed, so a fight persisted after the name had gone."""
    app, recogniser = build_regional(database, name="Tree Sentinel")
    app.step(name_frame(), 0.0)
    assert app.tracker.state is FightState.FIGHTING

    # Band goes blank. The gate short-circuits before OCR runs, so nothing
    # overwrites the previous read unless it is explicitly cleared.
    for i in range(1, 8):
        app.step(name_frame(text_present=False), i * 1.0)
    assert app.tracker.state is FightState.IDLE


def test_the_band_gate_has_real_margin():
    """The band is larger than the old tight name plate, so identical text
    is a smaller fraction of it.

    Measured on a real 4K capture, the tight plate read 4.7% ink. Diluted
    across the band that lands near the old 0.012 gate -- close enough
    that subsampling variance can push a real name under it. The gate is
    now set well below, since a permissive gate only costs an OCR pass
    while matching still decides whether text is really there.
    """
    from erdle.geometry import BOSS_NAME, NAME_BAND

    band = NAME_BAND.resolve(3840, 2160)
    plate = BOSS_NAME.resolve(3840, 2160)
    diluted = (plate.width * plate.height * 0.047) / (band.width * band.height)

    gate = AppConfig().min_band_ink
    assert diluted > gate * 3, (
        f"real text reads {diluted:.4f}; gate {gate} leaves too little margin"
    )
    assert gate < 0.012, "gate must sit below the old tight-crop value"


def test_the_band_is_not_needlessly_large():
    """Every extra pixel is terrain for OCR to be distracted by."""
    from erdle.geometry import BOSS_NAME, NAME_BAND

    band = NAME_BAND.resolve(3840, 2160)
    plate = BOSS_NAME.resolve(3840, 2160)
    assert band.width * band.height < plate.width * plate.height * 3


def test_poll_callback_reports_each_stage(database):
    """--verbose has to show ink, raw text and the match, or it is useless."""
    seen = []
    app = ErdleApp(
        database, RegionRecogniser(name="Tree Sentinel"),
        config=AppConfig(name_poll_interval=0.0),
        on_poll=lambda ink, text, match: seen.append((ink, text, match)),
    )
    app.step(name_frame(), 0.0)
    assert seen, "callback never fired"
    ink, text, match = seen[-1]
    assert ink > 0
    assert "Tree" in text
    assert match is not None and match.key == "tree_sentinel"


# --- the same behaviour on every display -----------------------------------


@pytest.mark.parametrize(
    "width,height,label",
    [(1280, 720, "720p"), (1920, 1080, "1080p"), (2560, 1440, "1440p"),
     (3440, 1440, "ultrawide"), (3840, 2160, "4K")],
)
def test_the_whole_flow_on_every_common_display(database, width, height, label):
    """idle -> boss -> died -> idle -> boss -> won, at five resolutions.

    Nothing here is calibrated: the band is a fraction of the frame and OCR
    reads whatever size the glyphs are. This is the property that lets one
    build ship to everyone.
    """
    from erdle.detect import make_test_frame
    from erdle.geometry import BOSS_NAME, FractionalRect
    from erdle.render import render_defeat_screen, render_victory_screen

    bar = FractionalRect(0.2427, 0.8028, 0.7573, 0.8120)
    hud = make_test_frame(
        width, height, bar_fill=1.0, bar_region=bar,
        background=(120, 95, 60), health_colour=(150, 40, 40), with_name=False,
    )
    pixels = [hud.pixel(x, y) for y in range(height) for x in range(width)]
    rect = BOSS_NAME.resolve(width, height)
    # Ink scaled to the frame, exactly as real text is. A fixed-pixel patch
    # would dilute at 4K and test the fixture rather than the app.
    for y in range(rect.top + int(rect.height * 0.15),
                   rect.top + int(rect.height * 0.85), 2):
        for x in range(rect.left + int(rect.width * 0.02),
                       rect.left + int(rect.width * 0.30), 2):
            pixels[y * width + x] = (238, 232, 218)
    named = Frame(width, height, pixels)
    blank = make_test_frame(width, height, bar_fill=None, background=(153, 115, 69))

    recogniser = RegionRecogniser()
    app = ErdleApp(
        database, recogniser,
        config=AppConfig(name_poll_interval=0.0, event_screen_seconds=3.0),
    )
    clock = [0.0]

    def step(frame, banner=None, times=1):
        for _ in range(times):
            app.step(frame, clock[0], banner or dark_banner())
            clock[0] += 0.6

    step(blank, times=3)
    assert app.tracker.state is FightState.IDLE, label

    recogniser.name = "Tree Sentinel"
    step(named, times=2)
    assert app.tracker.snapshot.boss.key == "tree_sentinel", label

    recogniser.banner = "YOU DIED"
    step(named, make_banner_frame(BANNER_W, BANNER_H))
    assert app.last_canvas.to_rows() == render_defeat_screen().to_rows(), label

    recogniser.banner = ""
    recogniser.name = ""
    clock[0] += 10
    step(blank, times=6)
    assert app.tracker.state is FightState.IDLE, label
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows(), label

    recogniser.name = "Malenia, Blade of Miquella"
    step(named, times=2)
    assert app.tracker.snapshot.boss.key == "malenia", label

    recogniser.banner = "GREAT ENEMY FELLED"
    step(named, make_banner_frame(BANNER_W, BANNER_H))
    assert app.last_canvas.to_rows() == render_victory_screen().to_rows(), label


def test_ink_fraction_is_resolution_independent():
    """The band gate must behave identically on every display.

    Both the band and the text scale with the frame, so the ratio holds.
    If it drifted, the gate would need per-resolution tuning -- which is
    exactly the calibration burden this design removed.
    """
    from erdle.detect import make_test_frame
    from erdle.geometry import BOSS_NAME, NAME_BAND
    from erdle.ocr import region_ink_fraction

    readings = []
    for width, height in [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]:
        frame = make_test_frame(width, height, bar_fill=1.0, with_name=False)
        pixels = [frame.pixel(x, y) for y in range(height) for x in range(width)]
        rect = BOSS_NAME.resolve(width, height)
        for y in range(rect.top + int(rect.height * 0.15),
                       rect.top + int(rect.height * 0.85), 2):
            for x in range(rect.left + int(rect.width * 0.02),
                           rect.left + int(rect.width * 0.30), 2):
                pixels[y * width + x] = (238, 232, 218)
        readings.append(
            region_ink_fraction(
                Frame(width, height, pixels), NAME_BAND.resolve(width, height)
            )
        )
    assert max(readings) / min(readings) < 1.5, readings
    assert min(readings) > AppConfig().min_band_ink * 3, readings
