import pytest

from erdle.bossdb import BossDatabase, default_data_path
from erdle.canvas import HEIGHT, WIDTH, Canvas
from erdle.font import text_width
from erdle.render import (
    advice_row,
    render_boss_screen,
    render_idle_screen,
    render_unknown_boss,
    short_name,
    status_row,
)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


# --- name shortening -------------------------------------------------------


@pytest.mark.parametrize(
    "full,expected",
    [
        ("Malenia, Blade of Miquella", "MALENIA"),
        ("Rennala, Queen of the Full Moon", "RENNALA"),
        ("Radagon of the Golden Order", "RADAGON"),
        ("Maliketh, the Black Blade", "MALIKETH"),
        ("Morgott, the Omen King", "MORGOTT"),
        ("Rykard, Lord of Blasphemy", "RYKARD"),
        ("Fire Giant", "FIRE GIANT"),
        ("Elden Beast", "ELDEN BEAST"),
        ("Godskin Noble", "GODSKIN NOBLE"),
    ],
)
def test_short_name_picks_the_distinguishing_part(full, expected):
    assert short_name(full) == expected


def test_short_name_keeps_titles_that_already_fit():
    assert short_name("Margit, the Fell Omen") == "MARGIT, THE FELL OMEN"


def test_short_name_truncates_unsplittable_monsters():
    result = short_name("Supercalifragilisticexpialidocious Knight")
    assert text_width(result) <= WIDTH


def test_every_shipped_name_fits_the_panel(database):
    for entry in database:
        assert text_width(short_name(entry.name)) <= WIDTH, entry.name


# --- rows ------------------------------------------------------------------


def test_status_row_marks_immunity_distinctly(database):
    row = status_row(database.require("erdtree_burial_watchdog"))
    assert "BLDx" in row and "ROTx" in row, row


def test_status_row_marks_resistance(database):
    assert "ROT-" in status_row(database.require("malenia"))


def test_status_row_always_answers_the_bleed_question(database):
    for entry in database:
        assert "BLD" in status_row(entry), entry.key


def test_status_row_fits_the_panel(database):
    for entry in database:
        assert text_width(status_row(entry)) <= WIDTH, entry.key


def test_advice_row_reports_best_and_worst(database):
    row = advice_row(database.require("elden_beast"))
    assert "+" in row and "-HOLY" in row


def test_advice_row_omits_absent_categories():
    from erdle.bossdb import parse_entry

    assert advice_row(parse_entry("x", {"name": "X", "poise": 55})) == "P55"


def test_advice_row_never_truncates_a_number(database):
    """Regression: "P100" was cut to "P10" on the Fire Giant. A wrong
    number is worse than no number."""
    import re

    for entry in database:
        match = re.search(r"P(\d+)", advice_row(entry))
        if match and entry.poise is not None:
            assert int(match.group(1)) == entry.poise, entry.name


def test_advice_row_keeps_only_whole_tokens(database):
    for entry in database:
        for token in advice_row(entry).split():
            assert token[0] in "+-P", f"{entry.key}: {token!r}"
            if token[0] == "P":
                assert token[1:].isdigit(), f"{entry.key}: {token!r}"


def test_advice_row_fits_the_panel(database):
    for entry in database:
        assert text_width(advice_row(entry)) <= WIDTH, entry.key


# --- full screens ----------------------------------------------------------


def test_boss_screen_has_panel_dimensions(database):
    canvas = render_boss_screen(database.require("malenia"), fill_ratio=0.5)
    assert (canvas.width, canvas.height) == (WIDTH, HEIGHT)
    assert len(canvas.pack()) == 640


def test_boss_screen_is_not_blank(database):
    canvas = render_boss_screen(database.require("radahn"), fill_ratio=1.0)
    assert sum(row.count("#") for row in canvas.to_rows()) > 50


def test_boss_screen_is_deterministic(database):
    entry = database.require("morgott")
    first = render_boss_screen(entry, fill_ratio=0.33)
    second = render_boss_screen(entry, fill_ratio=0.33)
    assert first.to_rows() == second.to_rows()


def test_health_bar_reflects_fill(database):
    entry = database.require("radahn")
    low = render_boss_screen(entry, fill_ratio=0.1)
    high = render_boss_screen(entry, fill_ratio=0.9)
    lit_low = sum(row.count("#") for row in low.to_rows())
    lit_high = sum(row.count("#") for row in high.to_rows())
    assert lit_high > lit_low


def test_health_bar_can_be_disabled(database):
    entry = database.require("radahn")
    with_bar = render_boss_screen(entry, fill_ratio=1.0)
    without = render_boss_screen(entry, fill_ratio=None)
    assert sum(r.count("#") for r in with_bar.to_rows()) > sum(
        r.count("#") for r in without.to_rows()
    )


def test_every_boss_renders_without_error(database):
    for entry in database:
        for ratio in (0.0, 0.5, 1.0):
            canvas = render_boss_screen(entry, fill_ratio=ratio)
            assert len(canvas.pack()) == 640


def test_no_pixels_bleed_outside_the_panel(database):
    for entry in database:
        rows = render_boss_screen(entry, fill_ratio=1.0).to_rows()
        assert len(rows) == HEIGHT
        assert all(len(row) == WIDTH for row in rows)


def test_idle_screen_renders():
    canvas = render_idle_screen("ERDLE")
    assert len(canvas.pack()) == 640
    assert any("#" in row for row in canvas.to_rows())


def test_unknown_boss_screen_still_shows_health():
    canvas = render_unknown_boss(fill_ratio=0.6)
    joined = canvas.to_rows()
    assert any("#" in row for row in joined[28:36]), "health bar missing"


def test_reused_canvas_is_cleared(database):
    canvas = Canvas()
    canvas.fill_rect(0, 0, WIDTH, HEIGHT)
    render_boss_screen(database.require("margit"), fill_ratio=0.0, canvas=canvas)
    assert any("." in row for row in canvas.to_rows()), "stale pixels survived"


# --- explicit short names (added for entries with no natural split point) ---


def test_display_name_prefers_the_explicit_short(database):
    from erdle.render import display_name

    watchdog = database.require("erdtree_burial_watchdog")
    assert display_name(watchdog) == "BURIAL WATCHDOG"


def test_display_name_falls_back_to_the_shortener(database):
    from erdle.render import display_name

    assert display_name(database.require("malenia")) == "MALENIA"


def test_every_display_name_fits_the_panel(database):
    from erdle.render import display_name

    for entry in database:
        assert text_width(display_name(entry)) <= WIDTH, entry.key


def test_explicit_short_is_still_truncated_if_absurd():
    from erdle.bossdb import parse_entry
    from erdle.render import display_name

    entry = parse_entry("x", {"name": "X", "short": "A" * 60})
    assert text_width(display_name(entry)) <= WIDTH
