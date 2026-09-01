"""Font-specific recognition: segmentation, learning, matching.

The fixture renders text with the project's own 5x7 font at various
scales. That exercises the thing that actually matters -- the same string
at 1x and 5x must reduce to the same normalised bits -- without needing
Elden Ring screenshots.
"""

import json

from pathlib import Path

import pytest

from erdle.detect import Frame
from erdle.font import ADVANCE, GLYPH_HEIGHT, GLYPH_WIDTH, glyph_for
from erdle.glyphs import (
    CELL_BITS,
    DEFAULT_MAX_DISTANCE,
    GlyphAtlas,
    hamming,
    learn_from_text,
    normalise_glyph,
    read_text,
    segment_glyphs,
)
from erdle.recognise import AtlasRecogniser

INK = (240, 235, 220)
PAPER = (18, 16, 14)


def render(text, scale=3, pad=6, ink=INK, paper=PAPER):
    """Draw text into a Frame using the built-in font, scaled up.

    Lowercase is drawn at x-height and sat on the baseline, as a real font
    does. The built-in 5x7 font has no lowercase glyphs of its own -- it
    folds case -- so without this a fixture cannot tell 'T' from 't', and
    the case-handling tests would be vacuous.
    """
    width = pad * 2 + len(text) * ADVANCE * scale
    height = pad * 2 + GLYPH_HEIGHT * scale
    pixels = [paper] * (width * height)
    cursor = pad
    for char in text:
        glyph = glyph_for(char)
        lower = char.islower()
        rows = GLYPH_HEIGHT * 2 // 3 if lower else GLYPH_HEIGHT
        drop = (GLYPH_HEIGHT - rows) * scale          # sit on the baseline
        for row in range(GLYPH_HEIGHT):
            for col in range(GLYPH_WIDTH):
                if not glyph[row][col]:
                    continue
                # Squash the glyph into the x-height band.
                target = (row * rows) // GLYPH_HEIGHT if lower else row
                for dy in range(scale if not lower else max(1, scale * rows // GLYPH_HEIGHT)):
                    for dx in range(scale):
                        x = cursor + col * scale + dx
                        y = pad + drop + target * scale + dy
                        if 0 <= x < width and 0 <= y < height:
                            pixels[y * width + x] = ink
        cursor += ADVANCE * scale
    return Frame(width, height, pixels)


def trained_atlas(samples=("TREE SENTINEL", "MALENIA", "GODRICK"), scale=3):
    atlas = GlyphAtlas()
    for text in samples:
        learn_from_text(render(text, scale=scale), text, atlas)
    return atlas


# --- segmentation ----------------------------------------------------------


def test_segments_one_box_per_character():
    boxes = segment_glyphs(render("MALENIA"))
    assert len(boxes) == 7


def test_ignores_spaces_when_counting_boxes():
    boxes = segment_glyphs(render("TREE SENTINEL"))
    assert len(boxes) == len("TREESENTINEL")


def test_marks_where_the_spaces_were():
    boxes = segment_glyphs(render("TREE SENTINEL"))
    spaced = [i for i, b in enumerate(boxes) if b.space_before]
    assert spaced == [4], "space should sit between TREE and SENTINEL"


def test_handles_multiple_spaces():
    boxes = segment_glyphs(render("A B C"))
    assert [b.space_before for b in boxes] == [False, True, True]


def test_boxes_are_ordered_left_to_right():
    boxes = segment_glyphs(render("ABCDEF"))
    assert all(boxes[i].right <= boxes[i + 1].left for i in range(len(boxes) - 1))


def test_boxes_have_real_extent():
    for box in segment_glyphs(render("MALENIA")):
        assert box.width > 0 and box.height > 0


def test_blank_plate_segments_to_nothing():
    assert segment_glyphs(Frame(40, 20, [PAPER] * 800)) == []


def test_empty_frame_is_safe():
    assert segment_glyphs(Frame(1, 1, [PAPER])) == []


@pytest.mark.parametrize("scale", [1, 2, 3, 5, 8])
def test_segmentation_survives_every_scale(scale):
    boxes = segment_glyphs(render("TREE SENTINEL", scale=scale))
    assert len(boxes) == len("TREESENTINEL"), scale


# --- normalisation ---------------------------------------------------------


def test_signature_has_the_expected_size():
    boxes = segment_glyphs(render("A"))
    assert len(normalise_glyph(render("A"), boxes[0])) == CELL_BITS


def test_signature_is_not_blank():
    frame = render("A")
    signature = normalise_glyph(frame, segment_glyphs(frame)[0])
    assert sum(signature) > 0


def test_the_same_letter_is_identical_at_the_same_scale():
    """What actually matters: a player's resolution does not change."""
    for char in "MALENIABCDEFGO":
        a, b = render(char), render(char)
        sig_a = normalise_glyph(a, segment_glyphs(a)[0])
        sig_b = normalise_glyph(b, segment_glyphs(b)[0])
        assert hamming(sig_a, sig_b) == 0, char


def test_different_letters_are_far_apart_at_the_same_scale():
    """Separation at fixed scale is what the threshold is set from."""
    import itertools
    import string

    signatures = {}
    for char in string.ascii_uppercase:
        frame = render(char)
        signatures[char] = normalise_glyph(frame, segment_glyphs(frame)[0])
    worst = min(
        hamming(signatures[a], signatures[b])
        for a, b in itertools.combinations(signatures, 2)
    )
    assert worst > DEFAULT_MAX_DISTANCE, (
        f"closest pair is {worst}, threshold is {DEFAULT_MAX_DISTANCE}"
    )


def test_different_letters_normalise_differently():
    pairs = [("A", "B"), ("E", "F"), ("O", "Q"), ("M", "N")]
    for first, second in pairs:
        fa, fb = render(first), render(second)
        sig_a = normalise_glyph(fa, segment_glyphs(fa)[0])
        sig_b = normalise_glyph(fb, segment_glyphs(fb)[0])
        assert hamming(sig_a, sig_b) > 2, f"{first} vs {second} too close"


# --- the atlas -------------------------------------------------------------


def test_learning_files_every_character():
    atlas = GlyphAtlas()
    learned = learn_from_text(render("MALENIA"), "MALENIA", atlas)
    assert learned > 0
    for char in "MALENI":
        assert char in atlas


def test_learning_ignores_spaces():
    atlas = GlyphAtlas()
    learn_from_text(render("A B"), "A B", atlas)
    assert " " not in atlas


def test_a_second_identical_sample_teaches_nothing():
    atlas = GlyphAtlas()
    first = learn_from_text(render("MALENIA"), "MALENIA", atlas)
    second = learn_from_text(render("MALENIA"), "MALENIA", atlas)
    assert first > 0 and second == 0


def test_mismatched_text_is_refused():
    """One mislabelled sample would stay in the atlas forever."""
    atlas = GlyphAtlas()
    assert learn_from_text(render("MALENIA"), "GODRICK THE GRAFTED", atlas) == 0
    assert len(atlas) == 0


def test_samples_per_character_are_capped():
    atlas = GlyphAtlas(max_samples_per_char=2)
    for scale in (2, 3, 4, 5, 6, 7):
        learn_from_text(render("A", scale=scale), "A", atlas)
    assert len(atlas.samples["A"]) <= 2


def test_match_returns_none_for_something_unlike_anything():
    atlas = trained_atlas()
    assert atlas.match(tuple([3] * CELL_BITS), max_distance=5)[0] is None


# --- reading ---------------------------------------------------------------


def test_reads_back_what_it_learned():
    atlas = GlyphAtlas()
    learn_from_text(render("TREE SENTINEL"), "TREE SENTINEL", atlas)
    text, coverage = read_text(render("TREE SENTINEL"), atlas)
    assert text == "TREE SENTINEL"
    assert coverage == 1.0


@pytest.mark.parametrize(
    "name",
    ["MALENIA", "GODRICK", "FIRE GIANT", "TREE SENTINEL", "ELDEN BEAST",
     "RADAGON", "MORGOTT", "RENNALA"],
)
def test_reads_boss_names_it_has_seen(name):
    atlas = GlyphAtlas()
    learn_from_text(render(name), name, atlas)
    assert read_text(render(name), atlas)[0] == name


def test_reads_a_name_built_from_letters_learned_elsewhere():
    """The point of learning an alphabet rather than whole names."""
    atlas = GlyphAtlas()
    for text in ("MALENIA", "GODRICK", "FIRE GIANT", "TREE SENTINEL"):
        learn_from_text(render(text), text, atlas)
    # "MARGIT" was never learned, but every letter in it was.
    text, coverage = read_text(render("MARGIT"), atlas)
    assert text == "MARGIT"
    assert coverage == 1.0


def test_reads_at_a_nearby_scale():
    """Small scale differences -- antialiasing, HUD scaling -- are fine."""
    atlas = GlyphAtlas()
    learn_from_text(render("TREE SENTINEL", scale=4), "TREE SENTINEL", atlas)
    assert read_text(render("TREE SENTINEL", scale=4), atlas)[0] == "TREE SENTINEL"


def test_declines_rather_than_guessing_at_a_very_different_scale():
    """A wrong letter is far more expensive than a question mark.

    At a large scale change the signature genuinely is ambiguous, so the
    atlas refuses. Downstream that means a fallback read and a fresh
    sample learned at the new size -- the app heals itself rather than
    confidently reporting the wrong boss.
    """
    atlas = GlyphAtlas()
    learn_from_text(render("MALENIA", scale=2), "MALENIA", atlas)
    text, coverage = read_text(render("MALENIA", scale=8), atlas)
    assert coverage < 1.0
    assert "?" in text


def test_learning_at_the_new_scale_restores_it():
    atlas = GlyphAtlas()
    learn_from_text(render("MALENIA", scale=2), "MALENIA", atlas)
    learn_from_text(render("MALENIA", scale=8), "MALENIA", atlas)
    assert read_text(render("MALENIA", scale=8), atlas)[0] == "MALENIA"
    assert read_text(render("MALENIA", scale=2), atlas)[0] == "MALENIA"


def test_unknown_glyphs_become_question_marks():
    atlas = GlyphAtlas()
    learn_from_text(render("ABC"), "ABC", atlas)
    text, coverage = read_text(render("ABZ"), atlas, max_distance=6)
    assert text.startswith("AB")
    assert "?" in text
    assert coverage < 1.0


def test_reading_a_blank_plate_is_safe():
    assert read_text(Frame(40, 20, [PAPER] * 800), trained_atlas()) == ("", 0.0)


# --- persistence -----------------------------------------------------------


def test_atlas_round_trips_through_a_file(tmp_path):
    path = tmp_path / "glyphs.json"
    atlas = trained_atlas()
    atlas.path = path
    atlas.save()

    loaded = GlyphAtlas.load(path)
    assert loaded.alphabet == atlas.alphabet
    assert read_text(render("TREE SENTINEL"), loaded)[0] == "TREE SENTINEL"


def test_saved_atlas_is_readable_json(tmp_path):
    path = tmp_path / "glyphs.json"
    atlas = trained_atlas()
    atlas.path = path
    atlas.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cell"] == {"width": 8, "height": 12}
    assert "T" in payload["glyphs"]


def test_a_corrupt_atlas_loads_empty(tmp_path):
    path = tmp_path / "glyphs.json"
    path.write_text("{{{ not json", encoding="utf-8")
    assert len(GlyphAtlas.load(path)) == 0


def test_an_atlas_from_a_different_grid_is_discarded():
    """Old bits mean nothing if the cell size changed."""
    stale = {"cell": {"width": 4, "height": 4}, "glyphs": {"A": ["1010"]}}
    assert len(GlyphAtlas.from_dict(stale)) == 0


def test_missing_atlas_file_is_fine(tmp_path):
    assert len(GlyphAtlas.load(tmp_path / "nope.json")) == 0


# --- the recogniser --------------------------------------------------------


class FakeTesseract:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def read(self, frame, threshold=None):
        self.calls += 1
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


def test_uses_the_atlas_and_never_calls_the_fallback():
    fallback = FakeTesseract(["should not be needed"])
    # min_alphabet=0: this exercises the reading path with a small atlas.
    # The gate itself is covered by test_a_sparse_atlas_is_skipped_entirely.
    recogniser = AtlasRecogniser(
        atlas=trained_atlas(), fallback=fallback, autosave=False, min_alphabet=0
    )
    assert recogniser.read(render("TREE SENTINEL")) == "TREE SENTINEL"
    assert fallback.calls == 0
    assert recogniser.is_self_sufficient


def test_falls_back_when_the_atlas_is_empty():
    fallback = FakeTesseract(["Tree Sentinel"])
    recogniser = AtlasRecogniser(
        atlas=GlyphAtlas(), fallback=fallback, autosave=False, min_alphabet=0
    )
    assert recogniser.read(render("TREE SENTINEL")) == "Tree Sentinel"
    assert fallback.calls == 1


def test_teaching_makes_the_fallback_unnecessary():
    """The intended lifecycle: bootstrap from Tesseract, then outgrow it."""
    fallback = FakeTesseract(["Tree Sentinel"])
    recogniser = AtlasRecogniser(
        atlas=GlyphAtlas(), fallback=fallback, autosave=False, min_alphabet=0
    )
    plate = render("TREE SENTINEL")

    assert recogniser.read(plate) == "Tree Sentinel"
    assert fallback.calls == 1

    recogniser.teach(plate, "TREE SENTINEL")

    before = fallback.calls
    assert recogniser.read(plate) == "TREE SENTINEL"
    assert fallback.calls == before, "should not have needed the fallback again"


def test_partial_reads_do_not_count_as_self_sufficient():
    recogniser = AtlasRecogniser(
        atlas=GlyphAtlas(), fallback=None, autosave=False, min_alphabet=0
    )
    recogniser.read(render("MALENIA"))
    assert not recogniser.is_self_sufficient


def test_works_with_no_fallback_at_all():
    recogniser = AtlasRecogniser(
        atlas=trained_atlas(), fallback=None, autosave=False, min_alphabet=0
    )
    assert recogniser.read(render("MALENIA")) == "MALENIA"


def test_autosave_writes_learned_glyphs(tmp_path):
    atlas = GlyphAtlas()
    atlas.path = tmp_path / "glyphs.json"
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=None, autosave=True, min_alphabet=0
    )
    recogniser.teach(render("MALENIA"), "MALENIA")
    assert atlas.path.exists()
    assert len(GlyphAtlas.load(atlas.path)) > 0


def test_summary_is_informative():
    recogniser = AtlasRecogniser(
        atlas=trained_atlas(), fallback=None, autosave=False, min_alphabet=0
    )
    recogniser.read(render("MALENIA"))
    assert "characters" in recogniser.summary()


# --- learning during play --------------------------------------------------


def test_the_app_teaches_the_atlas_when_a_name_resolves():
    """The lifecycle in one test: OCR reads it, the atlas learns it."""
    from erdle.app import AppConfig, ErdleApp
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.detect import make_test_frame
    from erdle.state import DetectorConfig

    database = BossDatabase.load(default_data_path())
    atlas = GlyphAtlas()
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=FakeTesseract(["Tree Sentinel"]),
        autosave=False, min_alphabet=0,
    )
    app = ErdleApp(
        database,
        recogniser,
        config=AppConfig(detector=DetectorConfig(enter_frames=2)),
    )
    frame = make_test_frame(1920, 1080, bar_fill=1.0, with_name=True)
    app.step(frame, 0.0)
    app.step(frame, 0.1)

    assert app.tracker.snapshot.boss.key == "tree_sentinel"
    # The synthetic name plate is a stipple, not real glyphs, so the count
    # will not line up and nothing should be learned -- which is exactly
    # the guard that stops a bad segmentation poisoning the atlas.
    assert app.glyphs_learned == 0


def test_learning_is_refused_when_segmentation_disagrees():
    atlas = GlyphAtlas()
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=None, autosave=False, min_alphabet=0
    )
    # Plate says MALENIA; we claim it says something else entirely.
    assert recogniser.teach(render("MALENIA"), "GODRICK THE GRAFTED") == 0
    assert len(atlas) == 0


def test_teaching_accepts_a_correct_label():
    atlas = GlyphAtlas()
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=None, autosave=False, min_alphabet=0
    )
    assert recogniser.teach(render("MALENIA"), "MALENIA") > 0
    assert recogniser.read(render("MALENIA")) == "MALENIA"


# --- case must not be folded ----------------------------------------------
# Real capture, "Tree Sentinel": upper-casing the label filed lowercase
# shapes under capitals, and put capital T and lowercase t under one key.


def test_upper_and_lower_are_separate_entries():
    atlas = GlyphAtlas()
    learn_from_text(render("Tt"), "Tt", atlas)
    assert "T" in atlas and "t" in atlas


def test_case_survives_a_round_trip():
    atlas = GlyphAtlas()
    learn_from_text(render("Tree Sentinel"), "Tree Sentinel", atlas)
    assert read_text(render("Tree Sentinel"), atlas)[0] == "Tree Sentinel"


def test_folding_case_would_collide():
    """Documents the bug: one key cannot hold two different shapes."""
    folded = GlyphAtlas()
    learn_from_text(render("Tt"), "TT", folded)          # the old behaviour
    assert "t" not in folded
    assert len(folded.samples["T"]) == 2, "two shapes crammed into one key"


def test_the_app_teaches_without_upper_casing():
    import inspect

    from erdle import app

    source = inspect.getsource(app.ErdleApp._teach)
    assert ".upper()" not in source, "case must reach the atlas intact"


def test_matcher_still_matches_mixed_case_output():
    """Preserving case downstream is safe -- the matcher normalises."""
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.matching import BossNameMatcher

    database = BossDatabase.load(default_data_path())
    matcher = BossNameMatcher.from_entries(database)
    assert matcher.match("Tree Sentinel").key == "tree_sentinel"
    assert matcher.match("TREE SENTINEL").key == "tree_sentinel"


# --- detecting a stale, case-folded atlas ----------------------------------
# A real capture showed 'R' and 'r' holding byte-identical shapes, because
# an atlas learned before case was preserved was never cleared.


def test_detects_upper_and_lower_holding_the_same_shape():
    import tools.atlas as atlas_tool

    atlas = GlyphAtlas()
    frame = render("R")
    from erdle.glyphs import normalise_glyph, segment_glyphs

    signature = normalise_glyph(frame, segment_glyphs(frame)[0])
    atlas.learn("R", signature, 20)
    atlas.learn("r", signature, 20)      # the poisoned state
    assert atlas_tool.find_case_collisions(atlas) == ["R"]


def test_a_correctly_learned_atlas_reports_no_collisions():
    import tools.atlas as atlas_tool

    atlas = GlyphAtlas()
    learn_from_text(render("Tree Sentinel"), "Tree Sentinel", atlas)
    assert atlas_tool.find_case_collisions(atlas) == []


def test_reset_removes_the_learned_atlas(tmp_path, monkeypatch):
    import tools.atlas as atlas_tool

    path = tmp_path / "glyphs.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(atlas_tool, "learned_path", lambda: path)

    class Args:
        yes = False
        shipped = False

    assert atlas_tool.cmd_reset(Args()) == 1
    assert path.exists(), "must not delete without confirmation"

    Args.yes = True
    assert atlas_tool.cmd_reset(Args()) == 0
    assert not path.exists()


def test_reset_can_clear_the_shipped_atlas_too(tmp_path, monkeypatch):
    """Clearing one and leaving the other is what caused the confusion."""
    import erdle.glyphs as glyphs
    import tools.atlas as atlas_tool

    learned = tmp_path / "learned.json"
    shipped = tmp_path / "shipped.json"
    for path in (learned, shipped):
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(atlas_tool, "learned_path", lambda: learned)
    monkeypatch.setattr(glyphs, "default_atlas_path", lambda: shipped)

    class Args:
        yes = True
        shipped = True

    assert atlas_tool.cmd_reset(Args()) == 0
    assert not learned.exists()
    assert not shipped.exists()


# --- planning which fights fill the atlas fastest ---------------------------


def test_plan_reports_reachable_characters(capsys):
    import tools.atlas as atlas_tool

    atlas_tool.cmd_plan(object())
    output = capsys.readouterr().out
    assert "distinct characters" in output
    assert "teaches" in output


def test_plan_orders_by_how_much_each_boss_teaches(capsys):
    """Greedy set cover: the first suggestion must be the biggest win."""
    import re

    import tools.atlas as atlas_tool

    atlas_tool.cmd_plan(object())
    gains = [int(n) for n in re.findall(r"teaches (\d+):", capsys.readouterr().out)]
    assert gains == sorted(gains, reverse=True)


# --- learning from screenshots ---------------------------------------------
# Filling the atlas by fighting every boss is slow. Screenshots work, as
# long as they are near the resolution you actually play at.


def test_learn_tool_reads_a_full_screenshot(tmp_path):
    """End to end: locate the plate in a whole frame, then learn from it."""
    pytest.importorskip("PIL")
    from PIL import Image

    import tools.learn as learn_tool
    from erdle.detect import make_test_frame
    from erdle.geometry import BOSS_NAME, FractionalRect

    bar = FractionalRect(0.2427, 0.8028, 0.7573, 0.8120)
    width, height = 1920, 1080
    frame = make_test_frame(
        width, height, bar_fill=1.0, bar_region=bar,
        background=(120, 95, 60), health_colour=(150, 40, 40), with_name=False,
    )
    pixels = [frame.pixel(x, y) for y in range(height) for x in range(width)]

    # Draw a name above the bar.
    rect = BOSS_NAME.resolve(width, height)
    plate = render("Fire Giant", scale=3)
    for y in range(plate.height):
        for x in range(plate.width):
            if plate.pixel(x, y) == INK:
                px, py = rect.left + 10 + x, rect.top + 6 + y
                if 0 <= px < width and 0 <= py < height:
                    pixels[py * width + px] = (238, 232, 218)

    image = Image.new("RGB", (width, height))
    image.putdata(pixels)
    path = tmp_path / "Fire Giant.png"
    image.save(path)

    atlas = GlyphAtlas()
    ok, message = learn_tool.learn_one(
        path, "Fire Giant", atlas, threshold=170, dry_run=False
    )
    assert ok, message
    assert "F" in atlas and "i" in atlas


def test_learn_tool_refuses_a_mismatched_name(tmp_path):
    """The guard that stops a wrong label poisoning the atlas."""
    pytest.importorskip("PIL")
    from PIL import Image

    import tools.learn as learn_tool

    plate = render("Fire Giant")
    image = Image.new("RGB", (plate.width, plate.height))
    image.putdata(
        [plate.pixel(x, y) for y in range(plate.height) for x in range(plate.width)]
    )
    path = tmp_path / "shot.png"
    image.save(path)

    atlas = GlyphAtlas()
    ok, message = learn_tool.learn_one(
        path, "Rykard, Lord of Blasphemy", atlas, threshold=170, dry_run=False
    )
    assert not ok
    assert "mismatch" in message
    assert len(atlas) == 0


def test_learn_tool_dry_run_changes_nothing(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    import tools.learn as learn_tool

    plate = render("Fire Giant")
    image = Image.new("RGB", (plate.width, plate.height))
    image.putdata(
        [plate.pixel(x, y) for y in range(plate.height) for x in range(plate.width)]
    )
    path = tmp_path / "shot.png"
    image.save(path)

    atlas = GlyphAtlas()
    ok, message = learn_tool.learn_one(
        path, "Fire Giant", atlas, threshold=170, dry_run=True
    )
    assert ok and "would learn" in message
    assert len(atlas) == 0, "dry run must not modify the atlas"


# --- shipping one atlas for every display ----------------------------------
# Normalising a glyph to its own bounding box necessarily destroys the size
# difference between 'C' and 'c' -- in a real font they are the same shape.
# So the matcher cannot be made fully scale-invariant without losing case.
# The way out is to cover resolutions directly: downscaling one 4K capture
# closely approximates what the game renders at 1080p.


def test_ladder_produces_samples_at_several_sizes(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    import tools.learn as learn_tool
    from erdle.detect import make_test_frame
    from erdle.geometry import BOSS_NAME, FractionalRect

    bar = FractionalRect(0.2427, 0.8028, 0.7573, 0.8120)
    width, height = 3840, 2160
    frame = make_test_frame(
        width, height, bar_fill=1.0, bar_region=bar,
        background=(120, 95, 60), health_colour=(150, 40, 40), with_name=False,
    )
    pixels = [frame.pixel(x, y) for y in range(height) for x in range(width)]
    rect = BOSS_NAME.resolve(width, height)
    plate = render("Fire Giant", scale=5)
    for y in range(plate.height):
        for x in range(plate.width):
            if plate.pixel(x, y) == INK:
                px, py = rect.left + 20 + x, rect.top + 18 + y
                if 0 <= px < width and 0 <= py < height:
                    pixels[py * width + px] = (238, 232, 218)
    image = Image.new("RGB", (width, height))
    image.putdata(pixels)
    path = tmp_path / "Fire Giant.png"
    image.save(path)

    atlas = GlyphAtlas()
    ok, message = learn_tool.learn_one(
        path, "Fire Giant", atlas, threshold=170, dry_run=False, ladder=True
    )
    assert ok, message

    heights = {h for samples in atlas.samples.values() for _, h in samples}
    assert len(heights) > 1, f"ladder produced only one size: {heights}"
    assert min(heights) < max(heights) / 1.5, "sizes should span a real range"


def test_ladder_can_be_disabled(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    import tools.learn as learn_tool

    plate = render("Fire Giant", scale=5)
    image = Image.new("RGB", (plate.width, plate.height))
    image.putdata(
        [plate.pixel(x, y) for y in range(plate.height) for x in range(plate.width)]
    )
    path = tmp_path / "shot.png"
    image.save(path)

    atlas = GlyphAtlas()
    learn_tool.learn_one(
        path, "Fire Giant", atlas, threshold=170, dry_run=False, ladder=False
    )
    heights = {h for samples in atlas.samples.values() for _, h in samples}
    assert len(heights) <= 2, "no ladder means one source size"


def test_band_report_names_the_displays_covered():
    import tools.atlas as atlas_tool

    # Heights typical of 1080p and 4K captures.
    assert "1080p" in atlas_tool.describe_bands([16])
    assert "4K" in atlas_tool.describe_bands([30])
    assert atlas_tool.describe_bands([]) .startswith("none")


def test_band_report_spans_when_the_ladder_was_used():
    import tools.atlas as atlas_tool

    served = atlas_tool.describe_bands([9, 12, 16, 22, 30, 35])
    for label in ("720p", "1080p", "1440p", "4K"):
        assert label in served, served


# --- the atlas must not cost more than it saves -----------------------------
# Measured at 4K: consulting an empty atlas cost 181ms per poll, twelve
# times the rest of the loop, segmenting the whole name band for an atlas
# that could never return anything.


def test_a_sparse_atlas_is_skipped_entirely():
    from erdle.recognise import AtlasRecogniser

    class Counting:
        calls = 0
        def read(self, frame, threshold=None):
            Counting.calls += 1
            return "Tree Sentinel"

    fallback = Counting()
    recogniser = AtlasRecogniser(
        atlas=GlyphAtlas(), fallback=fallback, autosave=False
    )
    assert recogniser.read(render("Tree Sentinel"), 200) == "Tree Sentinel"
    assert fallback.calls == 1
    assert recogniser.atlas_reads == 0


def test_a_sparse_atlas_with_no_fallback_returns_nothing():
    from erdle.recognise import AtlasRecogniser

    recogniser = AtlasRecogniser(atlas=GlyphAtlas(), fallback=None, autosave=False)
    assert recogniser.read(render("Tree Sentinel"), 200) == ""


def test_a_full_atlas_is_still_used():
    from erdle.recognise import AtlasRecogniser

    atlas = GlyphAtlas()
    # Enough distinct characters to clear the alphabet gate.
    for text in ("Tree Sentinel", "Godrick the Grafted", "Malenia",
                 "Fire Giant", "Rykard Lord of Blasphemy", "Ancient Hero"):
        learn_from_text(render(text), text, atlas)
    class Never:
        def read(self, frame, threshold=None):
            raise AssertionError("fallback should not be needed")

    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=Never(), autosave=False, min_alphabet=0
    )
    assert recogniser.read(render("Tree Sentinel"), 200) == "Tree Sentinel"
    assert recogniser.atlas_reads == 1


def test_the_atlas_does_not_crop_before_segmenting():
    """Regression: cropping to the densest ink block first truncated names.

    crop_to_ink keeps only the densest column run, so a letter gap a shade
    wider than its tolerance splits the name and the tail is discarded --
    "MALENIA" came back as "MALEN". A truncated name is a wrong answer,
    and the alphabet gate already removes the cost that prompted it.
    """
    from erdle.recognise import AtlasRecogniser

    atlas = GlyphAtlas()
    learn_from_text(render("MALENIA"), "MALENIA", atlas)
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=None, autosave=False, min_alphabet=0
    )
    assert recogniser.read(render("MALENIA")) == "MALENIA"


@pytest.mark.parametrize(
    "name", ["MALENIA", "TREE SENTINEL", "GODRICK", "FIRE GIANT", "RADAGON"]
)
def test_no_name_loses_its_tail(name):
    from erdle.recognise import AtlasRecogniser

    atlas = GlyphAtlas()
    learn_from_text(render(name), name, atlas)
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=None, autosave=False, min_alphabet=0
    )
    assert recogniser.read(render(name)) == name


def test_reading_a_wide_band_is_not_ruinous():
    """A name inside a band four times its width must still be cheap."""
    import time

    from erdle.recognise import AtlasRecogniser

    atlas = GlyphAtlas()
    for text in ("Tree Sentinel", "Godrick the Grafted", "Malenia", "Fire Giant"):
        learn_from_text(render(text), text, atlas)

    plate = render("Tree Sentinel", scale=3)
    wide = Frame(
        plate.width * 3, plate.height,
        [
            plate.pixel(x, y) if x < plate.width else PAPER
            for y in range(plate.height)
            for x in range(plate.width * 3)
        ],
    )
    recogniser = AtlasRecogniser(
        atlas=atlas, fallback=None, autosave=False, min_alphabet=0
    )
    start = time.perf_counter()
    recogniser.read(wide, 170)
    assert (time.perf_counter() - start) < 0.5


# --- learning from screenshots ----------------------------------------------


def _plate(name, width=1651, height=138, size=34, clutter=False):
    """A name plate rendered the way the game draws one."""
    from PIL import Image, ImageDraw, ImageFont

    from erdle.detect import Frame

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
        )
    except OSError:                       # pragma: no cover
        import pytest
        pytest.skip("no scalable font available")

    image = Image.new("RGB", (width, height), (30, 28, 26))
    draw = ImageDraw.Draw(image)
    draw.text((10, 30), name, fill=(235, 230, 215), font=font)
    if clutter:
        # A sliver of the boss health bar clipping into the band.
        draw.rectangle([0, height - 4, width - 1, height - 1], fill=(200, 60, 55))
    try:
        pixels = list(image.get_flattened_data())
    except AttributeError:
        pixels = list(image.getdata())
    return Frame(width, height, pixels)


def test_a_multi_word_name_is_learned():
    """Spaces are not glyphs, and the count check has to know that."""
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    name = "Crucible Knight Ordovis"
    assert learn_from_text(_plate(name), name, atlas, threshold=170) > 0
    assert {"C", "K", "O", "b", "c", "s"} <= set(atlas.samples)


def test_case_is_preserved_when_learning():
    """Folding case files a lowercase 'a' under 'A' and ruins both."""
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    name = "Ancient Hero of Zamor"
    assert learn_from_text(_plate(name), name, atlas, threshold=170) > 0
    assert "A" in atlas.samples and "a" in atlas.samples
    assert atlas.samples["A"][0][0] != atlas.samples["a"][0][0]


def test_touching_letters_are_refused_rather_than_mislearned():
    """Two glyphs whose boxes touch segment as one, so the count drops.

    Refusing is the right answer -- filing "Tr" under "T" would poison
    the atlas permanently -- but it is worth pinning, because it is the
    reason a name that reads perfectly can still teach nothing.
    """
    from erdle.glyphs import GlyphAtlas, learn_from_text, segment_glyphs

    frame = _plate("Tree Sentinel")
    boxes = segment_glyphs(frame, threshold=170)
    expected = [c for c in "Tree Sentinel" if not c.isspace()]
    assert len(boxes) < len(expected), "expected the test font to merge Tr"

    atlas = GlyphAtlas()
    assert learn_from_text(frame, "Tree Sentinel", atlas, threshold=170) == 0
    assert not atlas.samples


def test_a_wrong_name_is_refused():
    """One mislabelled sample stays in the atlas forever."""
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    learned = learn_from_text(
        _plate("Crucible Knight Ordovis"), "Margit", atlas, threshold=170
    )
    assert learned == 0
    assert not atlas.samples


def test_learning_survives_hud_clutter_in_the_band():
    """The band is deliberately generous and can catch the bar's top edge."""
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    name = "Ancient Hero of Zamor"
    learned = learn_from_text(
        _plate(name, clutter=True), name, atlas, threshold=170
    )
    assert learned > 0, "extra ink in the band blocked all learning"


def test_screenshots_can_be_named_after_the_boss():
    """So a folder of shots can be learned without typing each name."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import atlas as atlas_tool

    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.matching import BossNameMatcher

    matcher = BossNameMatcher.from_entries(
        BossDatabase.load(default_data_path())
    )
    for stem, expected in (
        ("Crucible_Knight_Ordovis", "Crucible Knight Ordovis"),
        ("Ancient Hero of Zamor", "Ancient Hero of Zamor"),
        ("decaying-ekzykes", "Decaying Ekzykes"),
    ):
        assert atlas_tool.name_from_filename(Path(f"{stem}.png"), matcher) == expected

    assert atlas_tool.name_from_filename(Path("screenshot_0042.png"), matcher) is None


# --- the learn command ------------------------------------------------------


def _atlas_tool():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import atlas

    return atlas


def _write_shot(path, name, full_screen=True):
    """A screenshot: either a full 4K grab or an already-cropped plate."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40
        )
    except OSError:                       # pragma: no cover
        import pytest
        pytest.skip("no scalable font available")

    if full_screen:
        width, height = 3840, 2160
        image = Image.new("RGB", (width, height), (40, 60, 35))
        draw = ImageDraw.Draw(image)
        draw.text((int(width * 0.2387), int(height * 0.7538)), name,
                  fill=(240, 236, 222), font=font)
        draw.rectangle(
            [int(width * 0.2387), int(height * 0.8008),
             int(width * 0.7613), int(height * 0.8140)],
            fill=(150, 40, 38),
        )
    else:
        image = Image.new("RGB", (900, 70), (30, 28, 26))
        ImageDraw.Draw(image).text((8, 12), name, fill=(240, 236, 222), font=font)
    image.save(path)


def test_wildcards_are_expanded_by_the_tool(tmp_path):
    """PowerShell does not glob for the program it launches.

    `atlas.py learn shots\\*.png` arrives as one literal, unmatchable
    path, and the command silently does nothing.
    """
    atlas_tool = _atlas_tool()
    for index in range(3):
        (tmp_path / f"shot{index}.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")

    found = atlas_tool.expand([str(tmp_path / "*.png")])
    assert len(found) == 3
    assert all(p.suffix == ".png" for p in found)


def test_expand_accepts_plain_paths_too(tmp_path):
    atlas_tool = _atlas_tool()
    target = tmp_path / "one.png"
    target.write_bytes(b"x")
    assert atlas_tool.expand([str(target)]) == [target]


def test_a_full_screenshot_uses_the_calibrated_band(tmp_path):
    atlas_tool = _atlas_tool()
    from erdle.config import Config

    shot = tmp_path / "Crucible Knight Ordovis.png"
    _write_shot(shot, "Crucible Knight Ordovis", full_screen=True)

    crop, where = atlas_tool.find_plate(atlas_tool.load_frame(shot), Config())
    assert "calibrated name band" in where
    assert crop.height < 400, "the whole 4K image was returned"


def test_a_cropped_screenshot_falls_back_to_the_whole_image(tmp_path):
    """A wiki crop has no HUD geometry; the band would land on nothing."""
    atlas_tool = _atlas_tool()
    from erdle.config import Config

    shot = tmp_path / "Ancient Hero of Zamor.png"
    _write_shot(shot, "Ancient Hero of Zamor", full_screen=False)

    frame = atlas_tool.load_frame(shot)
    crop, where = atlas_tool.find_plate(frame, Config())
    assert "whole image" in where
    assert (crop.width, crop.height) == (frame.width, frame.height)


def test_learn_teaches_from_both_kinds_of_screenshot(tmp_path, monkeypatch):
    atlas_tool = _atlas_tool()
    import argparse

    from erdle.glyphs import GlyphAtlas

    learned_to = tmp_path / "glyphs.json"
    monkeypatch.setattr(atlas_tool, "learned_path", lambda: learned_to)

    _write_shot(tmp_path / "Crucible Knight Ordovis.png",
                "Crucible Knight Ordovis", full_screen=True)
    _write_shot(tmp_path / "Ancient Hero of Zamor.png",
                "Ancient Hero of Zamor", full_screen=False)

    args = argparse.Namespace(
        images=[str(tmp_path / "*.png")], name=None, threshold=None,
        dump=False, verbose=False,
    )
    assert atlas_tool.cmd_learn(args) == 0

    atlas = GlyphAtlas.load(learned_to)
    assert {"C", "K", "O", "Z", "H"} <= set(atlas.samples)


def test_learn_reports_when_nothing_matches(tmp_path, capsys):
    atlas_tool = _atlas_tool()
    import argparse

    args = argparse.Namespace(
        images=[str(tmp_path / "nothing-here-*.png")], name=None,
        threshold=None, dump=False, verbose=False,
    )
    assert atlas_tool.cmd_learn(args) == 1
    assert "no image files matched" in capsys.readouterr().err


# --- more than one line in the band -----------------------------------------


def _stacked(names, width=1100, height=92, size=26):
    """A band holding several lines, as a duo fight draws them."""
    from PIL import Image, ImageDraw, ImageFont

    from erdle.detect import Frame

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
        )
    except OSError:                       # pragma: no cover
        import pytest
        pytest.skip("no scalable font available")

    image = Image.new("RGB", (width, height), (28, 26, 24))
    draw = ImageDraw.Draw(image)
    for index, name in enumerate(names):
        draw.text((8, 4 + index * 34), name, fill=(255, 255, 255), font=font)
    try:
        pixels = list(image.get_flattened_data())
    except AttributeError:
        pixels = list(image.getdata())
    return Frame(width, height, pixels)


def test_text_lines_splits_a_stacked_band():
    from erdle.glyphs import text_lines

    frame = _stacked(["Crucible Knight", "Crucible Knight Ordovis"])
    assert len(text_lines(frame, threshold=170)) == 2


def test_text_lines_finds_one_line_in_a_plain_plate():
    from erdle.glyphs import text_lines

    assert len(text_lines(_stacked(["Margit"]), threshold=170)) == 1


def test_text_lines_on_an_empty_frame():
    from erdle.detect import Frame
    from erdle.glyphs import text_lines

    assert text_lines(Frame(10, 10, [(0, 0, 0)] * 100), threshold=170) == []


def test_a_duo_fight_is_learned_from_the_right_line():
    """Elden Ring stacks two plates for a duo, and the band holds both.

    `segment_glyphs` counts runs of inked *columns*, so two stacked names
    merge into boxes spanning both rows. In the field that gave 38 glyphs
    for a 21-character name and taught nothing.

    The subtler danger is the opposite: the merged count can match the
    expected length by coincidence, and then the atlas quietly learns half
    of one letter stacked on half of another. So lines are separated
    first, never after.
    """
    from erdle.glyphs import GlyphAtlas, learn_from_any_line, learn_from_text

    name = "Crucible Knight Ordovis"
    frame = _stacked(["Crucible Knight", name])

    # What the strict function does on its own with a stacked band.
    naive = GlyphAtlas()
    learn_from_text(frame, name, naive, threshold=170)

    split = GlyphAtlas()
    assert learn_from_any_line(frame, name, split, threshold=170) > 0
    assert {"C", "K", "O", "b", "c", "s"} <= set(split.samples)

    # If the naive path produced a "C" at all, it must not be the one the
    # line-split path found -- that is the poisoned sample.
    if "C" in naive.samples:
        assert naive.samples["C"][0][0] != split.samples["C"][0][0], (
            "whole-band segmentation produced the same glyph as the "
            "line-split one; the fixture no longer reproduces the hazard"
        )


def test_glyphs_learned_from_a_stacked_band_can_read_a_clean_plate():
    """The real test of a learned shape is whether it reads the letter.

    Comparing signatures byte-for-byte would fail on a pixel of row
    alignment and prove nothing: the atlas matches by distance precisely
    because samples vary.
    """
    from erdle.glyphs import GlyphAtlas, learn_from_any_line, read_text

    name = "Crucible Knight Ordovis"
    atlas = GlyphAtlas()
    learn_from_any_line(
        _stacked(["Crucible Knight", name]), name, atlas, threshold=170
    )

    text, coverage = read_text(_stacked([name]), atlas, threshold=170)
    assert coverage > 0.9, f"only read {coverage:.0%}: {text!r}"

    # Not a character-perfect match. A descender that touches the line
    # below can be clipped by the split, so a 'g' or 'y' learned from a
    # stacked band is occasionally a slightly different shape. The
    # matcher tolerates it -- it accepts at 0.62 similarity -- and more
    # samples accumulate with play. What matters is that nothing is
    # learned *wrong*.
    wrong = sum(
        1 for read, expected in zip(text, name)
        if read != expected and read != "?"
    )
    assert wrong == 0, f"misread letters: {text!r} vs {name!r}"
    assert text.count("?") <= 1, text


def test_caption_clutter_does_not_block_learning():
    """Screenshots from videos carry captions and watermarks."""
    from erdle.glyphs import GlyphAtlas, learn_from_any_line

    name = "Crucible Knight Ordovis"
    frame = _stacked(["SUBSCRIBE!", name, "part 3"])
    atlas = GlyphAtlas()
    assert learn_from_any_line(frame, name, atlas, threshold=170) > 0


def test_a_wrong_name_is_still_refused_line_by_line():
    """Trying each line must not become a way to smuggle in bad samples."""
    from erdle.glyphs import GlyphAtlas, learn_from_any_line

    atlas = GlyphAtlas()
    frame = _stacked(["Crucible Knight", "Crucible Knight Ordovis"])
    assert learn_from_any_line(frame, "Margit", atlas, threshold=170) == 0
    assert not atlas.samples


def test_the_single_line_case_is_unchanged():
    """The fast path still runs first; lines are only a fallback."""
    from erdle.glyphs import GlyphAtlas, learn_from_any_line, learn_from_text

    name = "Ancient Hero of Zamor"
    frame = _stacked([name])
    direct = learn_from_text(frame, name, GlyphAtlas(), threshold=170)
    assert direct > 0
    assert learn_from_any_line(frame, name, GlyphAtlas(), threshold=170) == direct


# --- the atlas report has to match what the app reads -----------------------


def test_the_report_merges_the_shipped_atlas(tmp_path, monkeypatch):
    """Regression: `plan` sent you after letters you already had.

    It read only the per-user file. That was right while nothing shipped,
    and wrong the moment `data/glyphs.json` was seeded -- `build_recogniser`
    merges shipped and learned at run time, so a report that looks at one
    of them describes an atlas the app never uses.
    """
    import tools.atlas as atlas_tool
    from erdle.glyphs import GlyphAtlas, default_atlas_path

    empty = tmp_path / "glyphs.json"
    merged, shipped, learned = atlas_tool.effective_atlas(empty)

    on_disk = GlyphAtlas.load(default_atlas_path())
    assert shipped == len(on_disk) > 0
    assert learned == 0
    assert len(merged) == shipped


def test_learned_characters_add_to_the_shipped_ones(tmp_path):
    import json

    import tools.atlas as atlas_tool
    from erdle.glyphs import CELL_HEIGHT, CELL_WIDTH, GlyphAtlas

    extra = GlyphAtlas()
    extra.learn("§", [0] * (CELL_WIDTH * CELL_HEIGHT), 20)
    path = tmp_path / "glyphs.json"
    extra.path = path
    extra.save()
    assert json.loads(path.read_text(encoding="utf-8"))

    merged, shipped, learned = atlas_tool.effective_atlas(path)
    assert learned == 1
    assert len(merged) == shipped + 1
    assert "§" in merged.samples


def test_a_one_pixel_glyph_is_refused():
    """Regression: seven noise samples made `R` collide with `r`.

    A one-pixel-tall strip normalised onto the 8x12 grid becomes a
    near-uniform smear, which sits close to every letter at once. It
    causes *wrong* reads rather than missing ones, which is much harder
    to notice from the outside.
    """
    from erdle.glyphs import CELL_HEIGHT, CELL_WIDTH, GlyphAtlas

    atlas = GlyphAtlas()
    signature = [1] * (CELL_WIDTH * CELL_HEIGHT)
    assert atlas.learn("R", signature, 1) is False
    assert atlas.learn("R", signature, 2) is False
    assert "R" not in atlas.samples
    assert atlas.learn("R", signature, 20) is True


def test_an_unmeasured_height_is_still_accepted():
    """Height 0 means "not tracked", not "zero pixels tall"."""
    from erdle.glyphs import CELL_HEIGHT, CELL_WIDTH, GlyphAtlas

    atlas = GlyphAtlas()
    assert atlas.learn("R", [1] * (CELL_WIDTH * CELL_HEIGHT), 0) is True


def test_pruning_keeps_the_good_samples():
    from erdle.glyphs import CELL_HEIGHT, CELL_WIDTH, GlyphAtlas

    atlas = GlyphAtlas()
    good = [1] * (CELL_WIDTH * CELL_HEIGHT)
    bad = [2] * (CELL_WIDTH * CELL_HEIGHT)
    atlas.samples["A"] = [(good, 20), (bad, 1)]
    atlas.samples["B"] = [(bad, 2)]

    assert atlas.prune() == 2
    assert atlas.samples["A"] == [(good, 20)]
    # A character with nothing left is removed, not left empty: an empty
    # list would count towards the alphabet and report coverage it does
    # not have.
    assert "B" not in atlas.samples


def test_the_shipped_atlas_holds_no_noise():
    """What ships must be clean, or it teaches every user the same bug."""
    from erdle.glyphs import GlyphAtlas, default_atlas_path

    atlas = GlyphAtlas.load(default_atlas_path())
    # Punctuation is exempt: a hyphen on a 4K plate really is two pixels
    # tall, which is the same height as the noise this guards against.
    tiny = {char: [h for _, h in samples if 0 < h < GlyphAtlas.MIN_GLYPH_HEIGHT]
            for char, samples in atlas.samples.items()
            if char not in GlyphAtlas.SHORT_BY_NATURE}
    assert not any(tiny.values()), \
        f"run tools/atlas.py prune: {[c for c, v in tiny.items() if v]}"


def test_a_refused_plate_records_why():
    """A flat glyph counter after a successful detection is confusing.

    The plate was named, so learning looks like it should have happened.
    Usually segmentation produced the wrong number of boxes and the whole
    plate was discarded -- names with a hyphen or apostrophe are the
    common casualties, and those are the characters that stay missing
    longest.
    """
    from erdle.detect import Frame
    from erdle.glyphs import GlyphAtlas, learn_from_text

    blank = Frame(24, 12, [(0, 0, 0)] * (24 * 12))
    atlas = GlyphAtlas()
    assert learn_from_text(blank, "God-Devouring Serpent", atlas) == 0
    assert "expected 20" in learn_from_text.last_refusal
    assert "God-Devouring" in learn_from_text.last_refusal


def test_the_refusal_hook_fires_with_the_boxes():
    """The counter said learning failed; only the picture says why.

    "segmented 35 glyphs, expected 20" distinguishes nothing between a
    band that caught extra text and single letters being cut into
    pieces, and those need opposite fixes.
    """
    from erdle.app import ErdleApp

    calls = []

    class Recogniser:
        def teach(self, crop, name, threshold=None):
            return 0

    app = ErdleApp.__new__(ErdleApp)
    app.recogniser = Recogniser()
    app.glyphs_learned = 0
    app.on_refusal = lambda frame, name, boxes: calls.append((name, len(boxes)))

    from erdle.detect import Frame
    blank = Frame(8, 8, [(0, 0, 0)] * 64)
    app._teach(blank, "Margit", 200)

    assert calls and calls[0][0] == "Margit"


def test_no_hook_means_no_error():
    """The hook is a debug flag; its absence is the normal case."""
    from erdle.app import ErdleApp
    from erdle.detect import Frame

    class Recogniser:
        def teach(self, crop, name, threshold=None):
            return 0

    app = ErdleApp.__new__(ErdleApp)
    app.recogniser = Recogniser()
    app.glyphs_learned = 0
    app._teach(Frame(8, 8, [(0, 0, 0)] * 64), "Margit", 200)
    assert app.glyphs_learned == 0


# --- a real plate, end to end -----------------------------------------------
#
# Every synthetic test in this file renders clean text on a clean
# background, which is exactly the case that always worked. This one is a
# 4K capture of a live fight: bright scenery inside the band, a hyphen two
# pixels tall, and three letters broken at their arches. It segmented into
# 40 boxes for 20 characters and taught the atlas nothing.

PLATE = Path(__file__).resolve().parent / "plates" / "God-Devouring-Serpent.png"
PLATE_NAME = "God-Devouring Serpent"


def _real_plate():
    pytest.importorskip("PIL")
    from PIL import Image

    from erdle.detect import Frame

    image = Image.open(PLATE).convert("RGB")
    return Frame(image.width, image.height, list(image.getdata()))


@pytest.mark.skipif(not PLATE.exists(), reason="capture not present")
def test_a_real_plate_segments_into_exactly_its_letters():
    from erdle.glyphs import DEFAULT_INK_THRESHOLD, segment_glyphs

    boxes = segment_glyphs(_real_plate(), threshold=DEFAULT_INK_THRESHOLD)
    expected = len([c for c in PLATE_NAME if not c.isspace()])
    assert len(boxes) == expected


@pytest.mark.skipif(not PLATE.exists(), reason="capture not present")
def test_a_real_plate_teaches_every_letter():
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    learned = learn_from_text(_real_plate(), PLATE_NAME, atlas)
    assert learned == 20
    # The hyphen is the point: it is the character this fight was for,
    # and it is two pixels tall.
    assert "-" in atlas.samples


@pytest.mark.skipif(not PLATE.exists(), reason="capture not present")
def test_what_was_taught_can_be_read_back():
    """The real check. A plate that teaches wrong letters still 'learns'."""
    from erdle.glyphs import DEFAULT_INK_THRESHOLD, GlyphAtlas, learn_from_text, read_text

    frame = _real_plate()
    atlas = GlyphAtlas()
    learn_from_text(frame, PLATE_NAME, atlas)
    text, coverage = read_text(frame, atlas, threshold=DEFAULT_INK_THRESHOLD)

    assert coverage == 1.0
    assert "?" not in text
    from erdle.matching import normalise
    assert normalise(text) == normalise(PLATE_NAME)


@pytest.mark.skipif(not PLATE.exists(), reason="capture not present")
def test_scenery_inside_the_band_is_discarded():
    """15 of the 40 boxes were lava and scales, 250px past the last letter."""
    from erdle.glyphs import DEFAULT_INK_THRESHOLD, segment_glyphs

    frame = _real_plate()
    raw = segment_glyphs(frame, threshold=DEFAULT_INK_THRESHOLD,
                         on_text_line=False, join_fragments=False)
    clean = segment_glyphs(frame, threshold=DEFAULT_INK_THRESHOLD)
    assert len(raw) > len(clean)
    assert max(b.right for b in clean) < 600
    assert max(b.right for b in raw) > 1000


# --- the second real plate --------------------------------------------------

#: A frame lifted from a YouTube video, kept deliberately. It is the
#: hard case: compression has softened the strokes, so an `h` splits at
#: its arch and the plate segments into 15 pieces for 14 letters. Useful
#: for exercising the rejoin, and useless as a reference plate -- it was
#: one for a while, and it rejected genuine screenshots of the game.
CAVALRY = Path(__file__).resolve().parent / "plates" / "Night's Cavalry1.png"


def _cavalry_band():
    pytest.importorskip("PIL")
    import sys

    from PIL import Image

    from erdle.detect import Frame

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import learn

    image = Image.open(CAVALRY).convert("RGB")
    frame = Frame(image.width, image.height, list(image.getdata()))
    # The fixtures are cropped bands now -- a whole 4K screenshot was
    # 12 MB of git history for 90 useful rows.
    if image.height > 400:
        frame = learn.locate_name_plate(frame) or frame
    return frame


@pytest.mark.skipif(not CAVALRY.exists(), reason="capture not present")
def test_a_broken_letter_is_rejoined_using_the_label():
    """A compressed video frame segments into 15 pieces for 14 letters.

    An `h` is split at its arch. No geometric rule fixed it: the widths
    that allowed the 19px rejoin also merged real letters, and tightening
    them went straight from 15 boxes to 13 without ever landing on 14.

    The label settles what geometry could not -- close the tightest gaps
    until the count agrees with a name that is already known.
    """
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    assert learn_from_text(_cavalry_band(), "Night's Cavalry", atlas) == 14
    assert atlas.alphabet == "'CNaghilrstvy"


@pytest.mark.skipif(not CAVALRY.exists(), reason="capture not present")
def test_the_apostrophe_survives():
    """`'` is 11px tall and sits above the baseline. Both filters spared it."""
    from erdle.glyphs import GlyphAtlas, learn_from_text

    atlas = GlyphAtlas()
    learn_from_text(_cavalry_band(), "Night's Cavalry", atlas)
    assert "'" in atlas.samples


@pytest.mark.skipif(not CAVALRY.exists(), reason="capture not present")
def test_the_plate_reads_back_as_the_boss_it_is():
    """Spacing is still imperfect; identity no longer depends on it."""
    from erdle.glyphs import DEFAULT_INK_THRESHOLD, GlyphAtlas, learn_from_text, read_text
    from erdle.matching import normalise

    band = _cavalry_band()
    atlas = GlyphAtlas()
    learn_from_text(band, "Night's Cavalry", atlas)
    text, _ = read_text(band, atlas, threshold=DEFAULT_INK_THRESHOLD)
    assert normalise(text) == normalise("Night's Cavalry")


def test_merging_cannot_force_an_arbitrary_segmentation_to_fit():
    """Enough merges would make any picture match any label.

    That is the corruption the count check exists to stop, so the licence
    is bounded -- one join per eight boxes, and only on gaps tighter than
    the plate's median.
    """
    from erdle.glyphs import GlyphBox, _merge_to_fit

    boxes = [GlyphBox(i * 10, 0, i * 10 + 6, 20, False) for i in range(40)]
    assert len(_merge_to_fit(boxes, 4)) > 4


def test_a_filename_counter_is_stripped_before_it_becomes_a_label():
    """"Night's Cavalry1" is not a boss, and the digit did real damage.

    The filename is a label the atlas trusts absolutely. The plate holds
    14 characters, segmentation found 15, and the trailing "1" made the
    string 15 long as well -- so the count check passed by coincidence
    and every glyph after the break was filed one position out.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from learn import name_from_filename

    roster = {"Night's Cavalry", "Margit, the Fell Omen"}
    for stem in ("Night's Cavalry", "Night's Cavalry1", "Night's Cavalry 2",
                 "Night's Cavalry (3)", "Night's Cavalry_04"):
        assert name_from_filename(stem, roster)[0] == "Night's Cavalry", stem


def test_a_filename_that_names_no_boss_is_refused():
    """Better to skip a file than to teach the atlas a fiction."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from learn import name_from_filename

    assert name_from_filename("Screenshot 2026-08-30", {"Margit"})[0] is None
    assert name_from_filename("IMG_4821", {"Margit"})[0] is None


def test_reset_warns_about_the_atlas_it_does_not_clear():
    """Two atlases, and clearing one silently leaves the other.

    Reported: after `reset --yes`, `plan` still said 46 characters. The
    reset had worked -- the count was the *shipped* atlas, which had
    already been seeded from the corrupted one and is merged on top of
    whatever is learned next. Saying so is the difference between a
    confusing number and an obvious next step.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import atlas as atlas_tool

    source = Path(atlas_tool.__file__).read_text(encoding="utf-8")
    assert "--shipped" in source
    assert "is merged on top of" in source


def test_learning_rolls_back_a_file_that_breaks_the_references():
    """Matching glyph counts is not proof of correct alignment.

    "Starscourge Radahn" segments into exactly 17 boxes for its 17
    letters, passes every check a single plate can offer, and its samples
    then turn a perfect read of the serpent plate into
    'God-D?vou?ing S??pen?'. One letter had split and another merged, so
    the count survived while the labels between them moved.

    Reading back a plate whose text is known is the only thing that
    catches it, so each file is checked and rolled back if it makes the
    references worse.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import learn as learn_tool

    from erdle.glyphs import CELL_HEIGHT, CELL_WIDTH, GlyphAtlas

    atlas = GlyphAtlas()
    atlas.learn("A", [1] * (CELL_WIDTH * CELL_HEIGHT), 20)
    saved = learn_tool.snapshot(atlas)

    atlas.learn("B", [2] * (CELL_WIDTH * CELL_HEIGHT), 20)
    assert "B" in atlas.samples

    learn_tool.restore(atlas, saved)
    assert "B" not in atlas.samples
    assert "A" in atlas.samples


def test_learning_does_not_hunt_for_a_threshold_that_fits():
    """Trying cutoffs until the count matches finds coincidences.

    Measured on sixteen real screenshots: a brightness ladder turned two
    usable plates into nine that each passed the count check and then
    failed the reference plates. One cutoff is correct here even though
    the *reading* path rightly tries several.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / "tools" / "learn.py").read_text(encoding="utf-8")
    assert "THRESHOLD_LADDER" not in source
    assert "hunting for a coincidence" in source
