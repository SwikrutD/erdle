"""Centre-banner detection: YOU DIED and the FELLED tiers.

The gate is deliberately permissive -- a false positive costs one OCR
pass, a false negative loses a death. What it must reliably reject is
ordinary gameplay.
"""

import pytest

from erdle.banner import (
    BANNER_PHRASES,
    DEFAULT_BANNER_THRESHOLDS,
    BannerKind,
    BannerThresholds,
    classify,
    is_boss_tier,
    is_victory,
    looks_like_banner,
    make_banner_frame,
    read_banner,
)
from erdle.detect import Frame
from erdle.ocr import ScriptedRecogniser

# The subsampled centre region at 4K (step 4).
W, H = 576, 108


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("YOU DIED", BannerKind.DEATH),
        ("Y0U DIED", BannerKind.DEATH),
        ("YOU DlED", BannerKind.DEATH),
        ("you died", BannerKind.DEATH),
        ("YOU  DIED ", BannerKind.DEATH),
        ("ENEMY FELLED", BannerKind.ENEMY_FELLED),
        ("ENEMV FELLED", BannerKind.ENEMY_FELLED),
        ("GREAT ENEMY FELLED", BannerKind.GREAT_ENEMY_FELLED),
        ("GREAT ENEMV FELLEO", BannerKind.GREAT_ENEMY_FELLED),
        ("DEMIGOD FELLED", BannerKind.DEMIGOD_FELLED),
        ("DEMIG0D FELLED", BannerKind.DEMIGOD_FELLED),
    ],
)
def test_classifies_corrupted_ocr(text, expected):
    assert classify(text) == expected


@pytest.mark.parametrize(
    "junk", ["", "   ", "!!!", "LOADING", "SITE OF GRACE DISCOVERED",
             "Tree Sentinel", "GREAT", "FELLED"]
)
def test_rejects_non_banner_text(junk):
    assert classify(junk) is None


def test_every_phrase_classifies_as_itself():
    for kind, phrase in BANNER_PHRASES.items():
        assert classify(phrase) == kind


def test_tier_phrases_do_not_collide():
    """'ENEMY FELLED' is a substring of 'GREAT ENEMY FELLED'."""
    assert classify("ENEMY FELLED") == BannerKind.ENEMY_FELLED
    assert classify("GREAT ENEMY FELLED") == BannerKind.GREAT_ENEMY_FELLED


def test_margin_rejects_a_genuinely_ambiguous_read():
    strict = BannerThresholds(min_margin=0.9)
    assert classify("ENEMY FELLED", strict) is None


def test_victory_and_tier_helpers():
    assert is_victory(BannerKind.ENEMY_FELLED)
    assert is_victory(BannerKind.DEMIGOD_FELLED)
    assert not is_victory(BannerKind.DEATH)
    assert not is_victory(None)

    assert is_boss_tier(BannerKind.GREAT_ENEMY_FELLED)
    assert is_boss_tier(BannerKind.DEMIGOD_FELLED)
    assert not is_boss_tier(BannerKind.ENEMY_FELLED)
    assert not is_boss_tier(None)


# --- the cheap gate --------------------------------------------------------


def test_gate_accepts_a_banner():
    assert looks_like_banner(make_banner_frame(W, H)).present


@pytest.mark.parametrize("span", [0.35, 0.5, 0.7, 0.9])
def test_gate_accepts_banners_of_varying_width(span):
    frame = make_banner_frame(W, H, text_span=span)
    assert looks_like_banner(frame).present, span


def test_gate_rejects_dark_gameplay():
    assert not looks_like_banner(Frame(W, H, [(30, 28, 26)] * (W * H))).present


def test_gate_rejects_a_bright_sky():
    """Uniform brightness fills the region; the ink ceiling catches it."""
    assert not looks_like_banner(Frame(W, H, [(210, 215, 225)] * (W * H))).present


def test_gate_rejects_off_centre_light():
    pixels = [(20, 20, 20)] * (W * H)
    for y in range(40, 70):
        for x in range(10, 120):
            if x % 3:
                pixels[y * W + x] = (230, 220, 200)
    observation = looks_like_banner(Frame(W, H, pixels))
    assert not observation.present
    assert observation.centre_offset > DEFAULT_BANNER_THRESHOLDS.max_centre_offset


def test_gate_rejects_a_narrow_bright_spot():
    pixels = [(20, 20, 20)] * (W * H)
    for y in range(50, 60):
        for x in range(280, 300):
            pixels[y * W + x] = (240, 235, 220)
    assert not looks_like_banner(Frame(W, H, pixels)).present


def test_gate_handles_an_empty_frame():
    assert not looks_like_banner(Frame(1, 1, [(0, 0, 0)])).present


def test_gate_reports_its_measurements():
    observation = looks_like_banner(make_banner_frame(W, H, text_span=0.6))
    assert observation.span == pytest.approx(0.6, abs=0.05)
    assert observation.centre_offset < 0.05
    assert 0 < observation.ink < 0.3


# --- full read -------------------------------------------------------------


def test_read_banner_end_to_end():
    frame = make_banner_frame(W, H)
    recogniser = ScriptedRecogniser(["YOU DIED"])
    assert read_banner(frame, recogniser) is BannerKind.DEATH


def test_read_banner_skips_ocr_when_the_gate_fails():
    dark = Frame(W, H, [(20, 20, 20)] * (W * H))
    recogniser = ScriptedRecogniser(["YOU DIED"])
    assert read_banner(dark, recogniser) is None
    assert recogniser.calls == 0, "OCR ran on a frame with no banner"


def test_read_banner_retries_across_cutoffs():
    frame = make_banner_frame(W, H)
    recogniser = ScriptedRecogniser(["", "", "GREAT ENEMY FELLED"])
    assert read_banner(frame, recogniser) is BannerKind.GREAT_ENEMY_FELLED
    assert recogniser.calls == 3


def test_read_banner_gives_up_on_unreadable_text():
    frame = make_banner_frame(W, H)
    recogniser = ScriptedRecogniser(["qqqq", "zzzz", "wwww"])
    assert read_banner(frame, recogniser) is None
