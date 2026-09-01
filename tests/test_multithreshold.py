"""Retrying OCR at several brightness cutoffs.

The right cutoff depends on what is behind the bar. A real 4K capture
matched at 200 and returned empty strings at 150/170/185, but a darker
backdrop inverts that. Rather than making the user tune a number, the app
tries a few and keeps the first that resolves.
"""

# These tests cover the ORIGINAL bar-driven detector, which is still
# available behind `AppConfig(name_driven=False)`. The default is now
# name-driven -- see tests/test_nametrack.py -- so they opt in explicitly
# rather than silently testing whatever the default happens to be.


import pytest

from erdle.app import AppConfig, ErdleApp
from erdle.bossdb import BossDatabase, default_data_path
from erdle.detect import Frame, make_test_frame
from erdle.state import DetectorConfig, FightState

FOUR_K = (3840, 2160)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


class ThresholdRecogniser:
    """Returns text only at specific cutoffs, as the real one does."""

    def __init__(self, by_threshold: dict[int, str], default: str = "") -> None:
        self._by_threshold = by_threshold
        self._default = default
        self.calls: list[int | None] = []

    def read(self, frame: Frame, threshold: int | None = None) -> str:
        self.calls.append(threshold)
        return self._by_threshold.get(threshold, self._default)


class LegacyRecogniser:
    """Predates the threshold argument; must still work."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def read(self, frame: Frame) -> str:
        self.calls += 1
        return self.text


def run(app, frames, start=0.0):
    events = []
    now = start
    for frame in frames:
        events.extend(app.step(frame, now))
        now += 1 / 30
    return events


def bar(fill=1.0):
    return make_test_frame(*FOUR_K, bar_fill=fill, with_name=True)


def config(**kwargs):
    kwargs.setdefault("detector", DetectorConfig(enter_frames=2, exit_frames=10))
    return AppConfig(name_driven=False, **kwargs)


# --- retry behaviour -------------------------------------------------------


def test_matches_on_the_first_threshold_that_works(database):
    """The real capture: empty at 170, readable at 200."""
    recogniser = ThresholdRecogniser({200: "Tree Sentinel"}, default="")
    app = ErdleApp(database, recogniser, config=config())
    run(app, [bar()] * 3)
    assert app.tracker.snapshot.boss.key == "tree_sentinel"


def test_stops_early_when_the_first_cutoff_is_good(database):
    recogniser = ThresholdRecogniser({200: "Tree Sentinel"}, default="Tree Sentinel")
    app = ErdleApp(database, recogniser, config=config())
    run(app, [bar()] * 3)
    assert recogniser.calls == [200], "should not have tried the rest"


def test_tries_every_cutoff_before_giving_up(database):
    recogniser = ThresholdRecogniser({}, default="")
    app = ErdleApp(database, recogniser, config=config())
    run(app, [bar()] * 3)
    assert recogniser.calls == list(AppConfig(name_driven=False).ocr_thresholds)
    assert app.tracker.snapshot.boss is None


def test_keeps_the_highest_confidence_result(database):
    """A garbled read must not win over a clean one at another cutoff."""
    recogniser = ThresholdRecogniser(
        {200: "Tr3e S3nt1nel", 170: "Tree Sentinel", 230: "", 150: ""}
    )
    app = ErdleApp(
        database, recogniser, config=config(good_enough_confidence=0.99)
    )
    run(app, [bar()] * 3)
    assert app.tracker.snapshot.boss.key == "tree_sentinel"
    assert 170 in recogniser.calls


def test_ignores_a_cutoff_that_reads_a_different_boss(database):
    recogniser = ThresholdRecogniser({200: "", 170: "Fire Giant"})
    app = ErdleApp(database, recogniser, config=config())
    run(app, [bar()] * 3)
    assert app.tracker.snapshot.boss.key == "fire_giant"


def test_blank_name_plate_costs_no_ocr_at_all(database):
    recogniser = ThresholdRecogniser({200: "Tree Sentinel"})
    app = ErdleApp(database, recogniser, config=config())
    run(app, [make_test_frame(*FOUR_K, bar_fill=1.0, with_name=False)] * 4)
    assert recogniser.calls == []
    assert app.tracker.state is FightState.FIGHTING


def test_legacy_recogniser_without_threshold_still_works(database):
    recogniser = LegacyRecogniser("Tree Sentinel")
    app = ErdleApp(database, recogniser, config=config())
    run(app, [bar()] * 3)
    assert app.tracker.snapshot.boss.key == "tree_sentinel"


def test_thresholds_are_ordered_best_first():
    """200 is the measured winner, so it must be tried first."""
    assert AppConfig(name_driven=False).ocr_thresholds[0] == 200


def test_threshold_list_covers_a_useful_range():
    thresholds = AppConfig(name_driven=False).ocr_thresholds
    assert min(thresholds) <= 150
    assert max(thresholds) >= 230
    assert len(set(thresholds)) == len(thresholds)


def test_retry_cost_is_bounded_per_fight(database):
    """Worst case must stay bounded: 4 cutoffs x the attempt cap."""
    recogniser = ThresholdRecogniser({}, default="")
    app = ErdleApp(
        database, recogniser,
        config=config(
            detector=DetectorConfig(
                enter_frames=1, reidentify_interval=0.0, max_identify_attempts=3
            )
        ),
    )
    run(app, [bar()] * 200)
    assert len(recogniser.calls) <= 3 * len(AppConfig(name_driven=False).ocr_thresholds)
