"""End-to-end: synthetic frames in, GameSense payloads out.

Drives the whole pipeline -- detector, OCR, matcher, state machine,
renderer, transport -- with no hardware, no game, and no clock.
"""

# These tests cover the ORIGINAL bar-driven detector, which is still
# available behind `AppConfig(name_driven=False)`. The default is now
# name-driven -- see tests/test_nametrack.py -- so they opt in explicitly
# rather than silently testing whatever the default happens to be.


import pytest

from erdle.app import AppConfig, ErdleApp
from erdle.bossdb import BossDatabase, default_data_path
from erdle.canvas import Canvas
from erdle.detect import Frame, make_test_frame
from erdle.gamesense import CoreProps, GameSenseClient, RecordingTransport
from erdle.ocr import ScriptedRecogniser
from erdle.render import short_name
from erdle.sources import ReplaySource
from erdle.state import DetectorConfig, EventKind, FightState

WIDTH, HEIGHT = 1920, 1080
FPS = 30.0


def blank_frame():
    return make_test_frame(WIDTH, HEIGHT, bar_fill=None)


def bar_frame(fill, *, with_name=True):
    """A frame with a boss bar, optionally with ink on the name plate.

    The plate's content is irrelevant -- ScriptedRecogniser supplies the
    text -- but the app's brightness gate must see something there.
    """
    return make_test_frame(WIDTH, HEIGHT, bar_fill=fill, with_name=with_name)


def run(app, frames, start=0.0):
    events = []
    now = start
    for frame in frames:
        events.extend(app.step(frame, now))
        now += 1 / FPS
    return events


def kinds(events):
    return [e.kind for e in events]


@pytest.fixture
def database():
    return BossDatabase.load(default_data_path())


@pytest.fixture
def config():
    return AppConfig(name_driven=False, detector=DetectorConfig(enter_frames=3, exit_frames=10))


# --- the happy path --------------------------------------------------------


def test_full_fight_lifecycle(database, config):
    app = ErdleApp(database, ScriptedRecogniser(["Malenia, Blade of Miquella"]), config=config)

    run(app, [blank_frame()] * 5)
    assert app.tracker.state is FightState.IDLE

    events = run(app, [bar_frame(1.0)] * 5, start=1.0)
    assert EventKind.FIGHT_STARTED in kinds(events)
    assert EventKind.BOSS_IDENTIFIED in kinds(events)
    assert app.tracker.snapshot.boss.key == "malenia"

    run(app, [bar_frame(f) for f in (0.8, 0.6, 0.4, 0.2, 0.05)], start=2.0)
    assert app.tracker.snapshot.lowest_fill == pytest.approx(0.05, abs=0.03)

    events = run(app, [blank_frame()] * 12, start=3.0)
    assert EventKind.FIGHT_ENDED in kinds(events)
    assert app.tracker.state is FightState.IDLE


def test_screen_reflects_the_identified_boss(database, config):
    app = ErdleApp(database, ScriptedRecogniser(["Starscourge Radahn"]), config=config)
    run(app, [bar_frame(1.0)] * 4)
    assert app.last_canvas is not None
    # The rendered screen should equal a direct render of the same boss.
    from erdle.render import render_boss_screen

    expected = render_boss_screen(
        database.require("radahn"), fill_ratio=app.tracker.snapshot.fill_ratio
    )
    assert app.last_canvas.to_rows() == expected.to_rows()


def test_unknown_boss_still_drives_a_screen(database, config):
    app = ErdleApp(database, ScriptedRecogniser(["Ornstein and Smough"]), config=config)
    run(app, [bar_frame(0.7)] * 5)
    assert app.tracker.snapshot.boss is None
    assert app.last_canvas is not None
    lit = sum(row.count("#") for row in app.last_canvas.to_rows())
    assert lit > 0, "should still show the health mirror"


def test_phase_transition_is_one_continuous_fight(database, config):
    app = ErdleApp(
        database,
        ScriptedRecogniser(["Radagon of the Golden Order", "Elden Beast"]),
        config=AppConfig(name_driven=False, 
            detector=DetectorConfig(
                enter_frames=2, exit_frames=45, reidentify_interval=0.5
            )
        ),
    )
    events = run(app, [bar_frame(1.0)] * 3)
    assert app.tracker.snapshot.boss.key == "radagon"

    # Bar drops out briefly during the handoff, then returns.
    events = run(app, [blank_frame()] * 20, start=1.0)
    assert EventKind.FIGHT_ENDED not in kinds(events)

    events = run(app, [bar_frame(1.0)] * 3, start=2.0)
    assert EventKind.BOSS_CHANGED in kinds(events)
    assert EventKind.FIGHT_STARTED not in kinds(events)
    assert app.tracker.snapshot.boss.key == "elden_beast"


# --- efficiency ------------------------------------------------------------


def test_ocr_is_skipped_while_idle(database, config):
    recogniser = ScriptedRecogniser(["Fire Giant"])
    app = ErdleApp(database, recogniser, config=config)
    run(app, [blank_frame()] * 60)
    assert recogniser.calls == 0, "OCR ran with no bar on screen"


def test_ocr_is_skipped_when_the_name_plate_is_blank(database, config):
    recogniser = ScriptedRecogniser(["Fire Giant"])
    app = ErdleApp(database, recogniser, config=config)
    run(app, [bar_frame(1.0, with_name=False)] * 10)
    assert recogniser.calls == 0
    assert app.tracker.state is FightState.FIGHTING


def test_ocr_is_rate_limited_during_a_fight(database):
    """A name that never resolves is the expensive case.

    Each identification attempt now retries across several brightness
    cutoffs, so the bound is attempts x thresholds rather than attempts.
    It still has to be bounded -- an unrecognised boss must not turn into
    an OCR pass every frame for the length of the fight.
    """
    recogniser = ScriptedRecogniser(["Ornstein and Smough"])  # never matches
    config = AppConfig(name_driven=False, 
        detector=DetectorConfig(
            enter_frames=1, reidentify_interval=2.0, max_identify_attempts=4
        )
    )
    app = ErdleApp(database, recogniser, config=config)
    run(app, [bar_frame(1.0)] * 300)  # 10 seconds at 30fps

    ceiling = 4 * len(config.ocr_thresholds)
    assert recogniser.calls <= ceiling, f"{recogniser.calls} passes exceeds {ceiling}"
    assert recogniser.calls < 300, "must not be running every frame"


def test_a_recognised_boss_costs_a_single_ocr_pass(database):
    """The common case must not pay for the retry ladder."""
    recogniser = ScriptedRecogniser(["Starscourge Radahn"])
    app = ErdleApp(
        database,
        recogniser,
        config=AppConfig(name_driven=False, detector=DetectorConfig(enter_frames=1)),
    )
    run(app, [bar_frame(1.0)] * 30)
    assert recogniser.calls == 1, f"{recogniser.calls} passes for a clean read"


def test_screen_is_not_redrawn_for_identical_state(database, config):
    sent = []
    app = ErdleApp(
        database,
        ScriptedRecogniser(["Fire Giant"]),
        config=config,
        on_screen=sent.append,
    )
    run(app, [bar_frame(0.5)] * 60)
    assert len(sent) <= 4, f"{len(sent)} redraws for a static screen"


def test_health_changes_do_trigger_a_redraw(database, config):
    sent = []
    app = ErdleApp(
        database, ScriptedRecogniser(["Fire Giant"]), config=config, on_screen=sent.append
    )
    run(app, [bar_frame(f / 20) for f in range(20, 0, -1)])
    assert len(sent) > 5


# --- transport wiring ------------------------------------------------------


def test_frames_reach_gamesense_as_valid_payloads(database, config):
    transport = RecordingTransport()
    client = GameSenseClient(CoreProps("127.0.0.1:51248"), transport=transport)
    client.register()

    app = ErdleApp(
        database,
        ScriptedRecogniser(["Mohg, Lord of Blood"]),
        config=config,
        on_screen=lambda canvas: client.send_bitmap(canvas.pack()),
    )
    run(app, [bar_frame(1.0)] * 5)

    events = [c for c in transport.calls if c[0].endswith("/game_event")]
    assert events, "nothing was sent to GameSense"
    for _, payload in events:
        assert len(payload["data"]["frame"]["image-data-128x40"]) == 640


def test_pipeline_output_decodes_back_to_the_boss_screen(database, config):
    transport = RecordingTransport()
    client = GameSenseClient(CoreProps("127.0.0.1:51248"), transport=transport)
    app = ErdleApp(
        database,
        ScriptedRecogniser(["Godskin Noble"]),
        config=config,
        on_screen=lambda canvas: client.send_bitmap(canvas.pack()),
    )
    run(app, [bar_frame(1.0)] * 5)

    last = [c for c in transport.calls if c[0].endswith("/game_event")][-1][1]
    decoded = Canvas.from_packed(last["data"]["frame"]["image-data-128x40"])
    assert decoded.to_rows() == app.last_canvas.to_rows()


# --- sources ---------------------------------------------------------------


def test_replay_source_holds_on_the_last_frame():
    source = ReplaySource([blank_frame(), bar_frame(1.0)])
    source.grab()
    second = source.grab()
    assert source.grab() is second


def test_replay_source_rejects_empty():
    with pytest.raises(ValueError):
        ReplaySource([])


def test_replay_source_loops_when_asked():
    a, b = blank_frame(), bar_frame(1.0)
    source = ReplaySource([a, b], loop=True)
    source.grab()
    source.grab()
    assert source.grab() is a
