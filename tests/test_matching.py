"""Fuzzy matching is the highest-risk component: everything downstream is
correct only if the right boss comes out of noisy OCR text."""

import pytest

from erdle.bossdb import BossDatabase, default_data_path
from erdle.matching import BossNameMatcher, levenshtein, normalise, similarity


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


@pytest.fixture(scope="module")
def matcher(database):
    return BossNameMatcher.from_entries(database, threshold=0.62, min_margin=0.03)


# --- primitives ------------------------------------------------------------


def test_levenshtein_identical():
    assert levenshtein("MALENIA", "MALENIA") == 0


def test_levenshtein_empty():
    assert levenshtein("", "ABC") == 3
    assert levenshtein("ABC", "") == 3
    assert levenshtein("", "") == 0


def test_levenshtein_known_distances():
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("flaw", "lawn") == 2


def test_levenshtein_is_symmetric():
    assert levenshtein("RADAHN", "RADAGON") == levenshtein("RADAGON", "RADAHN")


def test_similarity_bounds():
    assert similarity("ABC", "ABC") == 1.0
    assert 0.0 <= similarity("ABC", "XYZ") <= 1.0
    assert similarity("", "") == 1.0


def test_normalise_strips_punctuation_and_case():
    assert normalise("Malenia, Blade of Miquella") == "MAIENIABIADEOFMIQUEIIA"


def test_normalise_removes_whitespace():
    """Spacing is dropped, not collapsed.

    A word break has to be inferred from the gap between glyph boxes,
    and no threshold gets it right on every plate: two real captures
    need contradictory cutoffs. Since no two bosses differ only in where
    their spaces fall, the comparison ignores spacing entirely.
    """
    assert normalise("  Fire   Giant  ") == "FIREGIANT"
    assert normalise("Night s Cavalry") == normalise("Night's Cavalry")


def test_normalise_folds_confusable_glyphs():
    # 0/O and 1/I/L collapse together, which is the whole point.
    assert normalise("M0HG") == normalise("MOHG")
    assert normalise("MA1EN1A") == normalise("MALENIA")


# --- realistic OCR corruption ---------------------------------------------


@pytest.mark.parametrize(
    "observed,expected",
    [
        ("Malenia, Blade of Miquella", "malenia"),
        ("MALENIA BLADE OF MIQUELLA", "malenia"),
        ("Ma1enia, B1ade of Miquel1a", "malenia"),      # 1-for-l
        ("MARGIT THE FELL 0MEN", "margit"),             # 0-for-O
        ("Margit,  the  Fell  Omen", "margit"),         # spacing noise
        ("Starscourge Radahn", "radahn"),
        ("STARSC0URGE RADAHN", "radahn"),
        ("Godrick the Grafted", "godrick"),
        ("G0DRICK THE GRAFTED", "godrick"),
        ("Fire Giant", "fire_giant"),
        ("FlRE GlANT", "fire_giant"),                   # l-for-I
        ("Elden Beast", "elden_beast"),
        ("Rykard, Lord of Blasphemy", "rykard"),
        ("Mohg, Lord of Blood", "mohg"),
        ("Godskin Noble", "godskin_noble"),
        ("Radagon of the Golden Order", "radagon"),
        ("Maliketh, the Black Blade", "maliketh"),
        ("Morgott, the Omen King", "morgott"),
        ("Rennala, Queen of the Full Moon", "rennala"),
    ],
)
def test_matches_corrupted_names(matcher, observed, expected):
    result = matcher.match(observed)
    assert result is not None, f"no match for {observed!r}"
    assert result.key == expected, f"{observed!r} -> {result.key}, want {expected}"


def test_dropped_trailing_characters_still_match(matcher):
    # OCR frequently clips the right edge of the name plate.
    result = matcher.match("Malenia, Blade of Miqu")
    assert result is not None
    assert result.key == "malenia"


def test_alias_short_form_resolves(matcher):
    result = matcher.match("Malenia")
    assert result is not None
    assert result.key == "malenia"
    assert result.confidence == 1.0


def test_phase_two_alias_resolves(matcher):
    result = matcher.match("Malenia, Goddess of Rot")
    assert result is not None
    assert result.key == "malenia"


# --- rejection -------------------------------------------------------------


def test_empty_input_rejected(matcher):
    assert matcher.match("") is None
    assert matcher.match("   ") is None
    assert matcher.match("!!!") is None


def test_unrelated_text_rejected(matcher):
    for junk in ("LOADING", "PRESS ANY BUTTON", "SITE OF GRACE DISCOVERED"):
        assert matcher.match(junk) is None, f"{junk!r} should not match"


def test_unknown_boss_rejected(matcher):
    # A real boss that is not in the stub database must not snap onto a
    # superficially similar entry.
    assert matcher.match("Ornstein and Smough") is None


def test_threshold_is_respected():
    small = BossNameMatcher({"a": "Fire Giant"}, threshold=0.99)
    assert small.match("Fire Gant") is None
    lenient = BossNameMatcher({"a": "Fire Giant"}, threshold=0.5)
    assert lenient.match("Fire Gant") is not None


def test_margin_filters_ambiguous_pairs():
    # Two near-identical candidates: with a margin requirement, neither wins.
    ambiguous = BossNameMatcher(
        {"a": "Crucible Knight", "b": "Crucible Knights"},
        threshold=0.5,
        min_margin=0.2,
    )
    assert ambiguous.match("Crucible Knigh") is None


def test_result_reports_margin_and_runner_up(matcher):
    result = matcher.match("Starscourge Radahn")
    assert result is not None
    assert result.runner_up is not None
    assert result.margin > 0


def test_empty_matcher_rejected():
    with pytest.raises(ValueError):
        BossNameMatcher({})


def test_add_alias_unknown_key_raises():
    m = BossNameMatcher({"a": "Fire Giant"})
    with pytest.raises(KeyError):
        m.add_alias("nope", "x")


def test_every_database_name_matches_itself(database, matcher):
    """Guards against a future entry whose name collides with another."""
    for entry in database:
        result = matcher.match(entry.name)
        assert result is not None, f"{entry.name} did not match itself"
        assert result.key == entry.key, (
            f"{entry.name} matched {result.key} instead of {entry.key}"
        )


# --- a failing tutor must not end the run -----------------------------------


def _blank_frame(width=64, height=16):
    from erdle.detect import Frame

    # White ink on black, so the atlas finds something to segment and the
    # tutor is actually consulted.
    pixels = [(255, 255, 255) if (x // 4) % 2 else (0, 0, 0)
              for y in range(height) for x in range(width)]
    return Frame(width, height, pixels)


class ExplodingTutor:
    """A tutor that fails the way a malformed config does."""

    def __init__(self, exc=None):
        self.exc = exc or ValueError("No closing quotation")
        self.calls = 0

    def read(self, frame, threshold=None):
        self.calls += 1
        raise self.exc


def test_a_failing_tutor_is_retired_not_raised():
    """Regression: an OCR error propagated out and killed the capture loop.

    The traceback arrived mid-fight, which is both the worst moment and
    the least recoverable one. The tutor is optional -- the atlas reads
    plates on its own -- so a broken one must be dropped, not fatal.
    """
    from erdle.recognise import AtlasRecogniser
    from erdle.glyphs import GlyphAtlas

    tutor = ExplodingTutor()
    recogniser = AtlasRecogniser(atlas=GlyphAtlas(), fallback=tutor)
    assert recogniser.read(_blank_frame()) is not None
    assert recogniser.fallback is None
    assert "No closing quotation" in recogniser.fallback_error


def test_a_retired_tutor_is_not_called_again():
    """Retry at 15fps would bury the message and burn the frame budget."""
    from erdle.recognise import AtlasRecogniser
    from erdle.glyphs import GlyphAtlas

    tutor = ExplodingTutor()
    recogniser = AtlasRecogniser(atlas=GlyphAtlas(), fallback=tutor)
    for _ in range(5):
        recogniser.read(_blank_frame())
    assert tutor.calls == 1


def test_the_summary_says_why_the_tutor_stopped():
    from erdle.recognise import AtlasRecogniser
    from erdle.glyphs import GlyphAtlas

    recogniser = AtlasRecogniser(atlas=GlyphAtlas(), fallback=ExplodingTutor())
    recogniser.read(_blank_frame())
    assert "tutor retired" in recogniser.summary()
