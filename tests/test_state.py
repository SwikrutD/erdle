"""The state machine carries the whole project: runback timer, attempt
counter and health mirror all read its events. Hysteresis behaviour around
phase transitions is the part most likely to be subtly wrong."""

import pytest

from erdle.bossdb import BossDatabase, default_data_path
from erdle.matching import MatchResult
from erdle.state import DetectorConfig, EventKind, FightState, FightTracker


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def kinds(events):
    return [e.kind for e in events]


def feed(tracker, frames, present, fill=0.8, start=0.0, step=1 / 30):
    """Push `frames` identical observations; return every event emitted."""
    collected = []
    now = start
    for _ in range(frames):
        collected.extend(tracker.update(present, fill, now))
        now += step
    return collected


# --- entering --------------------------------------------------------------


def test_starts_idle():
    assert FightTracker().state is FightState.IDLE


def test_enters_fight_after_enough_frames():
    tracker = FightTracker(DetectorConfig(enter_frames=3))
    assert not feed(tracker, 2, present=True)
    assert tracker.state is FightState.IDLE
    events = feed(tracker, 1, present=True, start=1.0)
    assert EventKind.FIGHT_STARTED in kinds(events)
    assert tracker.state is FightState.FIGHTING


def test_single_flicker_does_not_start_a_fight():
    tracker = FightTracker(DetectorConfig(enter_frames=3))
    tracker.update(True, 1.0, 0.0)
    tracker.update(False, 0.0, 0.1)
    tracker.update(True, 1.0, 0.2)
    tracker.update(False, 0.0, 0.3)
    assert tracker.state is FightState.IDLE


def test_records_start_time():
    tracker = FightTracker(DetectorConfig(enter_frames=1))
    tracker.update(True, 1.0, 42.0)
    assert tracker.snapshot.started_at == 42.0


# --- leaving ---------------------------------------------------------------


def test_ends_fight_after_sustained_absence():
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=5))
    feed(tracker, 1, present=True)
    events = feed(tracker, 5, present=False, start=1.0)
    assert EventKind.FIGHT_ENDED in kinds(events)
    assert tracker.state is FightState.IDLE


def test_short_gap_does_not_end_fight():
    """The bar genuinely disappears during phase transitions."""
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=45))
    feed(tracker, 1, present=True)
    feed(tracker, 40, present=False, start=1.0)
    assert tracker.state is FightState.FIGHTING, "phase transition split the fight"


def test_reappearing_bar_resets_the_exit_countdown():
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=10))
    feed(tracker, 1, present=True)
    feed(tracker, 9, present=False, start=1.0)
    feed(tracker, 1, present=True, start=2.0)
    feed(tracker, 9, present=False, start=3.0)
    assert tracker.state is FightState.FIGHTING


def test_fight_ended_reports_duration_excluding_the_exit_timeout():
    """Duration must run to when the bar was last seen, not to when the
    exit was confirmed -- otherwise every fight is inflated by the timeout,
    which would quietly corrupt the runback timer built on top of this."""
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=3))
    tracker.update(True, 1.0, 100.0)
    tracker.update(True, 0.5, 130.0)   # last sighting
    tracker.update(False, 0.0, 140.0)
    tracker.update(False, 0.0, 150.0)
    events = tracker.update(False, 0.0, 160.0)
    ended = [e for e in events if e.kind is EventKind.FIGHT_ENDED]
    assert ended
    assert ended[0].duration == pytest.approx(30.0)
    assert ended[0].at == pytest.approx(160.0)


def test_fight_ended_reports_the_best_attempt():
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=2))
    tracker.update(True, 1.0, 0.0)
    tracker.update(True, 0.08, 1.0)
    tracker.update(False, 0.0, 2.0)
    events = tracker.update(False, 0.0, 3.0)
    ended = [e for e in events if e.kind is EventKind.FIGHT_ENDED][0]
    assert ended.lowest_fill == pytest.approx(0.08)


def test_duration_is_never_negative():
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=1))
    tracker.update(True, 1.0, 50.0)
    events = tracker.update(False, 0.0, 51.0)
    ended = [e for e in events if e.kind is EventKind.FIGHT_ENDED][0]
    assert ended.duration >= 0.0


def test_snapshot_resets_after_fight():
    tracker = FightTracker(DetectorConfig(enter_frames=1, exit_frames=2))
    feed(tracker, 1, present=True)
    feed(tracker, 2, present=False, start=1.0)
    assert tracker.snapshot.boss is None
    assert tracker.snapshot.started_at is None


# --- health ----------------------------------------------------------------


def test_health_updates_are_throttled_by_epsilon():
    tracker = FightTracker(
        DetectorConfig(enter_frames=1, health_update_epsilon=0.05)
    )
    tracker.update(True, 1.00, 0.0)
    assert not [e for e in tracker.update(True, 0.99, 0.1)
                if e.kind is EventKind.HEALTH_UPDATED]
    assert [e for e in tracker.update(True, 0.90, 0.2)
            if e.kind is EventKind.HEALTH_UPDATED]


def test_tracks_lowest_fill_as_best_attempt():
    tracker = FightTracker(DetectorConfig(enter_frames=1))
    tracker.update(True, 1.0, 0.0)
    tracker.update(True, 0.4, 0.1)
    tracker.update(True, 0.12, 0.2)
    tracker.update(True, 0.55, 0.3)  # boss healed / second phase
    assert tracker.snapshot.lowest_fill == pytest.approx(0.12)


def test_duration_advances_with_the_clock():
    tracker = FightTracker(DetectorConfig(enter_frames=1))
    tracker.update(True, 1.0, 10.0)
    tracker.update(True, 0.9, 25.0)
    assert tracker.snapshot.duration == pytest.approx(15.0)


# --- identification --------------------------------------------------------


def make_identifying_tracker(database, keys, **config_kwargs):
    """Tracker whose identify() walks a scripted list of boss keys."""
    sequence = list(keys)
    state = {"i": 0}

    def identify():
        i = state["i"]
        state["i"] += 1
        key = sequence[i] if i < len(sequence) else sequence[-1]
        if key is None:
            return None
        return MatchResult(key, database.require(key).name, 0.95, None, 0.3)

    config = DetectorConfig(enter_frames=1, **config_kwargs)
    return FightTracker(config, identify=identify, resolve_boss=database.get)


def test_identifies_boss_on_fight_start(database):
    tracker = make_identifying_tracker(database, ["margit"])
    events = tracker.update(True, 1.0, 0.0)
    assert EventKind.BOSS_IDENTIFIED in kinds(events)
    assert tracker.snapshot.boss.key == "margit"


def test_failed_identification_leaves_boss_unset(database):
    tracker = make_identifying_tracker(database, [None])
    events = tracker.update(True, 1.0, 0.0)
    assert EventKind.BOSS_IDENTIFIED not in kinds(events)
    assert tracker.snapshot.boss is None


def test_reidentification_is_rate_limited(database):
    tracker = make_identifying_tracker(
        database, [None, None, "margit"], reidentify_interval=2.0
    )
    tracker.update(True, 1.0, 0.0)
    tracker.update(True, 1.0, 0.5)   # too soon, no retry
    assert tracker.snapshot.identify_attempts == 1
    tracker.update(True, 1.0, 2.5)   # interval elapsed
    assert tracker.snapshot.identify_attempts == 2


def test_identification_attempts_are_capped(database):
    tracker = make_identifying_tracker(
        database, [None], reidentify_interval=0.0, max_identify_attempts=3
    )
    for i in range(20):
        tracker.update(True, 1.0, float(i))
    assert tracker.snapshot.identify_attempts == 3


def test_phase_transition_emits_boss_changed_not_a_new_fight(database):
    """Radagon handing off to the Elden Beast is one fight, two names."""
    tracker = make_identifying_tracker(
        database, ["radagon", "elden_beast"], reidentify_interval=0.0
    )
    tracker.update(True, 1.0, 0.0)
    events = tracker.update(True, 1.0, 1.0)
    assert EventKind.BOSS_CHANGED in kinds(events)
    assert EventKind.FIGHT_STARTED not in kinds(events)
    assert tracker.snapshot.boss.key == "elden_beast"
    changed = [e for e in events if e.kind is EventKind.BOSS_CHANGED][0]
    assert changed.previous_boss.key == "radagon"


def test_phase_transition_resets_best_attempt_watermark(database):
    tracker = make_identifying_tracker(
        database, ["radagon", "elden_beast"], reidentify_interval=1.0
    )
    tracker.update(True, 1.0, 0.0)           # identifies Radagon
    tracker.update(True, 0.05, 0.5)          # nearly killed phase one
    assert tracker.snapshot.lowest_fill == pytest.approx(0.05)
    tracker.update(True, 1.0, 1.5)           # phase two: full bar, new name
    assert tracker.snapshot.boss.key == "elden_beast"
    assert tracker.snapshot.lowest_fill == pytest.approx(1.0)


def test_identification_is_not_attempted_while_the_bar_is_gone(database):
    """Mid-handoff the name plate is absent; an OCR pass there is wasted."""
    tracker = make_identifying_tracker(
        database, ["radagon", "elden_beast"], reidentify_interval=0.5
    )
    tracker.update(True, 1.0, 0.0)
    attempts = tracker.snapshot.identify_attempts
    for i in range(1, 20):
        tracker.update(False, 0.0, float(i))
    assert tracker.snapshot.identify_attempts == attempts
    assert tracker.snapshot.boss.key == "radagon"


def test_same_boss_reidentified_emits_nothing(database):
    tracker = make_identifying_tracker(
        database, ["margit", "margit"], reidentify_interval=0.0
    )
    tracker.update(True, 1.0, 0.0)
    events = tracker.update(True, 1.0, 1.0)
    assert EventKind.BOSS_CHANGED not in kinds(events)


def test_history_accumulates(database):
    tracker = make_identifying_tracker(database, ["margit"])
    tracker.update(True, 1.0, 0.0)
    assert len(tracker.history) >= 2


def test_tracker_without_identify_never_sets_a_boss():
    tracker = FightTracker(DetectorConfig(enter_frames=1))
    tracker.update(True, 1.0, 0.0)
    assert tracker.snapshot.boss is None
