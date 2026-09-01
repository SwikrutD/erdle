"""Calibration is how a user fixes the one thing tests cannot verify, so
its diagnostics need to be right or they will send people the wrong way."""

from erdle.calibrate import advise, describe_frame
from erdle.detect import Frame, make_test_frame


def test_reports_a_healthy_detection():
    frame = make_test_frame(1920, 1080, bar_fill=0.75)
    report = describe_frame(frame)
    assert report["present"] is True
    assert report["fill_percent"] == 75
    assert report["resolution"] == "1920x1080"
    assert advise(report) == ["Everything looks consistent. No changes suggested."]


def test_reports_missing_bar():
    frame = make_test_frame(1920, 1080, bar_fill=None)
    report = describe_frame(frame)
    assert report["present"] is False
    assert any("No bar detected" in note for note in advise(report))


def test_flags_unclassifiable_colours():
    """The signature of a misaligned region or wrong thresholds."""
    frame = make_test_frame(
        1920, 1080, bar_fill=1.0, health_colour=(130, 120, 40)
    )
    report = describe_frame(frame)
    assert report["scanline"]["unclassified"] > 0
    assert any("neither" in note for note in advise(report))


def test_flags_a_bar_whose_colour_is_not_recognised():
    """Thresholds too strict: the bar is on screen but reads as nothing."""
    frame = make_test_frame(
        1920, 1080, bar_fill=1.0, health_colour=(40, 38, 36)
    )
    report = describe_frame(frame)
    assert report["scanline"]["health"] == 0
    assert not report["present"]
    assert any("No bar detected" in note for note in advise(report))


def test_flags_blank_name_plate():
    frame = make_test_frame(1920, 1080, bar_fill=1.0, with_name=False)
    report = describe_frame(frame)
    assert report["name_plate_ink"] < 0.012
    assert any("blank" in note for note in advise(report))


def test_name_plate_ink_lands_in_the_expected_band():
    """Real plates sit around 0.03-0.25; the gate is set below that."""
    frame = make_test_frame(1920, 1080, bar_fill=1.0, with_name=True)
    assert 0.012 < describe_frame(frame)["name_plate_ink"] < 0.4


def test_flags_overlapping_name_region():
    white = Frame(1920, 1080, [(255, 255, 255)] * (1920 * 1080))
    report = describe_frame(white)
    assert any("very bright" in note for note in advise(report))


def test_sample_colours_are_reported_for_tuning():
    frame = make_test_frame(1920, 1080, bar_fill=0.5)
    report = describe_frame(frame)
    assert report["sample_health_colour"] == [150, 34, 30]
    assert report["sample_depleted_colour"] == [24, 20, 18]


def test_report_is_json_serialisable():
    import json

    json.dumps(describe_frame(make_test_frame(1920, 1080, bar_fill=0.5)))
