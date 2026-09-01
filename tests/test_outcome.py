"""The whole user-facing flow.

    idle  ->  boss identified, weaknesses shown  ->  win or lose message

Nothing is counted or persisted; the message is the entire payoff.
"""

# These tests cover the ORIGINAL bar-driven detector, which is still
# available behind `AppConfig(name_driven=False)`. The default is now
# name-driven -- see tests/test_nametrack.py -- so they opt in explicitly
# rather than silently testing whatever the default happens to be.


import pytest

from erdle.app import AppConfig, ErdleApp
from erdle.banner import BannerKind, make_banner_frame
from erdle.bossdb import BossDatabase, default_data_path
from erdle.canvas import HEIGHT, WIDTH
from erdle.detect import Frame, make_test_frame
from erdle.matching import MatchResult
from erdle.ocr import ScriptedRecogniser
from erdle.render import (
    DEFEAT_LINES,
    VICTORY_LINES,
    render_boss_screen,
    render_defeat_screen,
    render_idle_screen,
    render_message_screen,
    render_victory_screen,
)
from erdle.state import DetectorConfig, EventKind, FightState, FightTracker

FOUR_K = (3840, 2160)
BANNER_W, BANNER_H = 576, 108


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def kinds(events):
    return [e.kind for e in events]


def hud(fill=1.0):
    return make_test_frame(*FOUR_K, bar_fill=fill, with_name=True)


def blank_hud():
    return make_test_frame(*FOUR_K, bar_fill=None)


def banner():
    return make_banner_frame(BANNER_W, BANNER_H)


def dark_banner():
    return Frame(BANNER_W, BANNER_H, [(20, 20, 20)] * (BANNER_W * BANNER_H))


def build(database, texts, **config_kwargs):
    config_kwargs.setdefault(
        "detector", DetectorConfig(enter_frames=2, exit_frames=10)
    )
    return ErdleApp(
        database, ScriptedRecogniser(texts), config=AppConfig(name_driven=False, **config_kwargs)
    )


# --- the message screens ---------------------------------------------------


def test_victory_message_is_good_job_tarnished():
    canvas = render_victory_screen()
    assert (canvas.width, canvas.height) == (WIDTH, HEIGHT)
    assert canvas.to_rows() == render_message_screen(*VICTORY_LINES).to_rows()
    assert VICTORY_LINES == ("GOOD JOB", "TARNISHED")


def test_defeat_message_is_git_gud_tarnished():
    canvas = render_defeat_screen()
    assert canvas.to_rows() == render_message_screen(*DEFEAT_LINES).to_rows()
    assert DEFEAT_LINES == ("GIT GUD", "TARNISHED")


def test_the_two_messages_differ():
    assert render_victory_screen().to_rows() != render_defeat_screen().to_rows()


@pytest.mark.parametrize("render", [render_victory_screen, render_defeat_screen])
def test_message_screens_pack_correctly(render):
    canvas = render()
    assert len(canvas.pack()) == 640
    assert sum(row.count("#") for row in canvas.to_rows()) > 40


@pytest.mark.parametrize("render", [render_victory_screen, render_defeat_screen])
def test_message_screens_stay_inside_the_panel(render):
    rows = render().to_rows()
    assert len(rows) == HEIGHT
    assert all(len(row) == WIDTH for row in rows)


def test_message_screen_handles_one_line():
    canvas = render_message_screen("ERDLE")
    assert any("#" in row for row in canvas.to_rows())


def test_message_screen_truncates_something_absurd():
    render_message_screen("A" * 200, "B" * 200)  # must not raise


def test_message_screens_are_vertically_balanced():
    """Both lines should sit in the middle band, not hug an edge."""
    rows = render_victory_screen().to_rows()
    lit = [y for y, row in enumerate(rows) if "#" in row]
    assert min(lit) >= 8 and max(lit) <= 32


# --- state machine ---------------------------------------------------------


def tracker_with_boss(database, key="tree_sentinel", **config):
    entry = database.require(key)
    return FightTracker(
        DetectorConfig(enter_frames=1, **config),
        identify=lambda: MatchResult(key, entry.name, 0.95, None, 0.3),
        resolve_boss=database.get,
    )


def test_death_emits_died(database):
    tracker = tracker_with_boss(database)
    tracker.update(True, 1.0, 0.0)
    assert kinds(tracker.note_banner(BannerKind.DEATH, 5.0)) == [EventKind.DIED]


@pytest.mark.parametrize(
    "kind",
    [BannerKind.ENEMY_FELLED, BannerKind.GREAT_ENEMY_FELLED,
     BannerKind.DEMIGOD_FELLED],
)
def test_every_felled_tier_emits_victory(database, kind):
    tracker = tracker_with_boss(database)
    tracker.update(True, 1.0, 0.0)
    assert kinds(tracker.note_banner(kind, 5.0)) == [EventKind.VICTORY]


def test_a_lingering_banner_fires_once(database):
    """The banner sits on screen for seconds; we poll several times a second."""
    tracker = tracker_with_boss(database, banner_lockout=8.0)
    tracker.update(True, 1.0, 0.0)
    fired = []
    for i in range(60):
        fired.extend(tracker.note_banner(BannerKind.DEATH, 5.0 + i * 0.1))
    assert len(fired) == 1


def test_a_later_banner_fires_again(database):
    tracker = tracker_with_boss(database, banner_lockout=8.0)
    tracker.update(True, 1.0, 0.0)
    assert len(tracker.note_banner(BannerKind.DEATH, 5.0)) == 1
    assert tracker.note_banner(BannerKind.DEATH, 9.0) == []
    assert len(tracker.note_banner(BannerKind.DEATH, 40.0)) == 1


def test_banner_does_not_end_the_fight(database):
    """The bar disappearing does that; two drivers would race."""
    tracker = tracker_with_boss(database)
    tracker.update(True, 1.0, 0.0)
    tracker.note_banner(BannerKind.DEATH, 3.0)
    assert tracker.state is FightState.FIGHTING


def test_outcome_fires_without_an_identified_boss():
    tracker = FightTracker(DetectorConfig(enter_frames=1))
    tracker.update(True, 1.0, 0.0)
    assert kinds(tracker.note_banner(BannerKind.DEATH, 3.0)) == [EventKind.DIED]


# --- the full flow ---------------------------------------------------------


def test_idle_then_boss_then_defeat(database):
    app = build(database, ["Tree Sentinel", "YOU DIED"], event_screen_seconds=5.0)

    app.step(blank_hud(), 0.0, dark_banner())
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()

    app.step(hud(1.0), 0.1, dark_banner())
    app.step(hud(1.0), 0.2, dark_banner())
    assert app.tracker.snapshot.boss.key == "tree_sentinel"
    expected = render_boss_screen(
        database.require("tree_sentinel"),
        fill_ratio=app.tracker.snapshot.fill_ratio,
    )
    assert app.last_canvas.to_rows() == expected.to_rows()

    app.step(hud(0.2), 1.0, banner())
    assert app.last_canvas.to_rows() == render_defeat_screen().to_rows()


def test_idle_then_boss_then_victory(database):
    app = build(
        database, ["Tree Sentinel", "GREAT ENEMY FELLED"], event_screen_seconds=5.0
    )
    app.step(hud(1.0), 0.0, dark_banner())
    app.step(hud(1.0), 0.1, dark_banner())
    app.step(hud(0.02), 1.0, banner())
    assert app.last_canvas.to_rows() == render_victory_screen().to_rows()


def test_message_holds_then_returns_to_idle(database):
    app = build(database, ["Tree Sentinel", "YOU DIED"], event_screen_seconds=5.0)
    app.step(hud(1.0), 0.0, dark_banner())
    app.step(hud(1.0), 0.1, dark_banner())
    app.step(hud(0.2), 1.0, banner())

    app.step(hud(0.2), 3.0, dark_banner())
    assert app.last_canvas.to_rows() == render_defeat_screen().to_rows()

    for i in range(14):                     # bar goes away, message expires
        app.step(blank_hud(), 10.0 + i * 0.1, dark_banner())
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_walking_away_shows_no_message(database):
    """A fight that just ends gets no verdict -- straight back to idle."""
    app = build(database, ["Tree Sentinel"])
    app.step(hud(1.0), 0.0, dark_banner())
    app.step(hud(1.0), 0.1, dark_banner())
    for i in range(14):
        app.step(blank_hud(), 1.0 + i * 0.1, dark_banner())
    assert app.tracker.state is FightState.IDLE
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_unknown_boss_still_gets_a_verdict(database):
    app = build(database, ["Ornstein and Smough", "YOU DIED"])
    app.step(hud(1.0), 0.0, dark_banner())
    app.step(hud(1.0), 0.1, dark_banner())
    assert app.tracker.snapshot.boss is None
    app.step(hud(0.2), 1.0, banner())
    assert app.last_canvas.to_rows() == render_defeat_screen().to_rows()


def test_die_retry_win(database):
    app = build(
        database,
        ["Tree Sentinel", "YOU DIED", "Tree Sentinel", "DEMIGOD FELLED"],
        event_screen_seconds=1.0,
    )
    now = 0.0
    seen = []
    screens = []
    for _ in range(2):
        for _ in range(3):
            app.step(hud(1.0), now, dark_banner())
            now += 0.1
        seen.extend(kinds(app.step(hud(0.1), now, banner())))
        # Snapshot the panel at the moment the outcome fires; the message
        # is transient by design and will have expired by the next round.
        screens.append(app.last_canvas.to_rows())
        now += 30.0
        for _ in range(12):
            app.step(blank_hud(), now, dark_banner())
            now += 0.1
        now += 30.0

    assert EventKind.DIED in seen
    assert EventKind.VICTORY in seen
    assert screens[0] == render_defeat_screen().to_rows()
    assert screens[1] == render_victory_screen().to_rows()
    # And after everything expires we are back to idle.
    assert app.last_canvas.to_rows() == render_idle_screen("ERDLE").to_rows()


def test_app_takes_no_stats_argument(database):
    """Counting was removed; the constructor should not accept it."""
    with pytest.raises(TypeError):
        ErdleApp(database, ScriptedRecogniser([]), stats=object())


def test_banner_frame_remains_optional(database):
    app = build(database, ["Tree Sentinel"])
    app.step(hud(1.0), 0.0)
    app.step(hud(1.0), 0.1)
    assert app.tracker.snapshot.boss.key == "tree_sentinel"
    assert app.banners_seen == 0
