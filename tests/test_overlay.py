"""The screen overlay: content, ranking, show/hide rules and drawing.

Tk is not importable in CI, so the window is exercised through a recording
canvas rather than a real one. That still covers the part most likely to
break -- layout arithmetic and colour selection -- and leaves only the Tk
plumbing untested, which is the part that fails loudly rather than subtly.
"""

from __future__ import annotations

import pytest

from erdle.bossdb import (
    IMMUNE,
    NORMAL,
    RESISTANT,
    WEAK,
    BossDatabase,
    default_data_path,
    parse_entry,
)
from erdle.overlay import (
    OverlayContent,
    OverlayDriver,
    Row,
    build_content,
)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())



@pytest.fixture
def no_tk(monkeypatch):
    """Force the no-Tk path.

    Without this the test depends on whether the machine happens to have
    Tk -- passing in CI and building a real window on Windows.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("no tkinter for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    yield


class FakeCanvas:
    """Records drawing calls, and measures wrapped text like Tk would.

    `bbox` is the important part. The real bug this file now guards
    against came from estimating wrapped height by character count; the
    double therefore has to wrap the way Tk does -- on word boundaries,
    with wider glyphs for uppercase -- or the test would pass on the same
    wrong arithmetic the code used to use.
    """

    #: Rough widths per character, as a fraction of the font's pixel size.
    #: Uppercase is the case that broke.
    UPPER, LOWER = 0.78, 0.55

    #: Simulates Windows display scaling for point-sized fonts.
    dpi_scale = 1.0

    def __init__(self) -> None:
        self.items: list[tuple] = []
        self.config: dict = {}
        self._boxes: dict[int, tuple] = {}

    def delete(self, *args) -> None:
        self.items.clear()
        self._boxes.clear()

    def configure(self, **kwargs) -> None:
        self.config.update(kwargs)

    def create_text(self, x, y, **kwargs):
        self.items.append(("text", x, y, kwargs))
        item = len(self.items)
        self._boxes[item] = self._extent(x, y, kwargs)
        return item

    def create_rectangle(self, x0, y0, x1, y1, **kwargs):
        self.items.append(("rect", x0, y0, x1, y1, kwargs))
        return len(self.items)

    def bbox(self, item):
        return self._boxes.get(item)

    def _text_width(self, text, size):
        upper = sum(1 for c in text if c.isupper())
        return size * (upper * self.UPPER + (len(text) - upper) * self.LOWER)

    def _pixel_size(self, spec):
        """Tk: a negative size means pixels, a positive one means points.

        Modelling this is the whole point -- treating points as pixels is
        what hid the overlap, because it made the text a third smaller
        than it renders at 96 DPI and half the size it renders at 150%.
        """
        size = spec[1]
        if size < 0:
            return -size
        return round(size * 96 / 72 * self.dpi_scale)

    def _extent(self, x, y, kwargs):
        text = kwargs.get("text", "")
        size = self._pixel_size(kwargs["font"])
        wrap = kwargs.get("width")
        line_height = round(size * 1.35)
        if not wrap:
            return (x, y, x + round(self._text_width(text, size)), y + line_height)

        lines, current = 1, ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if self._text_width(trial, size) > wrap and current:
                lines += 1
                current = word
            else:
                current = trial
        return (x, y, x + wrap, y + lines * line_height)

    @property
    def texts(self) -> list[tuple]:
        return [i for i in self.items if i[0] == "text"]

    @property
    def rects(self) -> list[tuple]:
        return [i for i in self.items if i[0] == "rect"]

    def strings(self) -> list[str]:
        return [i[3]["text"] for i in self.texts]


class FakeOverlay:
    available = True

    def __init__(self) -> None:
        self.shown: list[OverlayContent] = []
        self.hides = 0
        self.stopped = False

    def show(self, content) -> None:
        self.shown.append(content)

    def hide(self) -> None:
        self.hides += 1

    def stop(self) -> None:
        self.stopped = True


# --- content ---------------------------------------------------------------


def test_every_boss_builds_content(database):
    for entry in database:
        content = build_content(entry)
        assert content.name == entry.name
        assert len(content.damage) == 8
        assert len(content.statuses) == 6
        assert content.headline


def test_damage_is_ordered_best_first(database):
    for entry in database:
        rows = build_content(entry).damage
        measured = [row for row in rows if row.known]
        ranks = [
            (row.effectiveness, row.severity if row.severity is not None else 0)
            for row in measured
        ]
        assert ranks == sorted(ranks, reverse=True), entry.key


def test_unrecorded_damage_sorts_last(database):
    """An assumed NORMAL must never outrank eight measured values.

    Regression: Malenia has no fire row, so the default floated to the top
    and the overlay recommended fire against a boss the sheet says nothing
    about.
    """
    for entry in database:
        rows = build_content(entry).damage
        known_flags = [row.known for row in rows]
        assert known_flags == sorted(known_flags, reverse=True), entry.key


def test_pruned_normals_count_as_known(database):
    """`damage` drops NORMAL on import; `severity` still records it.

    Reading only `damage` reported "no data" for 233 values the sheet
    actually contains.
    """
    entry = database.require("margit")
    rows = {row.key: row for row in build_content(entry).damage}
    assert "standard" not in entry.damage
    assert "standard" in entry.severity
    assert rows["standard"].known is True
    assert rows["standard"].description == "neutral"


def test_weaknesses_are_reported_even_when_tied(database):
    """Two equally good answers is better news than one, not worse.

    Margit used to serve here because the old spreadsheet listed him weak
    to both slash and holy. The game's own numbers say holy is negated by
    40% -- he is a demigod, and the sheet was wrong -- so the tie now
    needs a boss that really has one.
    """
    tied = None
    for entry in database:
        content = build_content(entry)
        if len(content.weaknesses) >= 2:
            tied = content
            break
    assert tied is not None, "no boss with two weaknesses to test with"
    for row in tied.weaknesses[:2]:
        assert row.label.lower() in tied.headline


def test_least_bad_is_not_claimed_when_nothing_stands_out():
    entry = parse_entry(
        "x",
        {
            "name": "Equally Bad",
            "damage": {name: RESISTANT for name in
                       ("standard", "slash", "strike", "pierce",
                        "magic", "fire", "lightning", "holy")},
            "severity": {name: 1 for name in
                         ("standard", "slash", "strike", "pierce",
                          "magic", "fire", "lightning", "holy")},
            "statuses": {},
        },
    )
    content = build_content(entry)
    assert content.best_damage is None
    assert content.headline == "no notable weaknesses"


def test_least_bad_is_claimed_when_it_stands_alone(database):
    content = build_content(database.require("flying_dragon_agheel"))
    assert content.best_damage is not None
    assert content.best_damage.label == "Pierce"
    assert "pierce works best" in content.headline


def test_immunities_lead_the_headline(database):
    content = build_content(database.require("erdtree_burial_watchdog"))
    assert content.headline.startswith("immune to ")
    assert {row.label for row in content.immunities} >= {"Bleed", "Scarlet Rot"}


def test_status_order_is_fixed_not_ranked(database):
    """Statuses are a lookup, so bleed is always first."""
    for entry in database:
        labels = [row.key for row in build_content(entry).statuses]
        assert labels == ["bleed", "frost", "rot", "poison", "sleep", "madness"]


# --- row presentation ------------------------------------------------------


def test_severity_drives_the_word_and_the_bar():
    row = Row(key="fire", label="Fire", effectiveness=RESISTANT,
              known=True, severity=0)
    assert row.description == "useless"
    assert row.weight == 0.0

    better = Row(key="fire", label="Fire", effectiveness=WEAK,
                 known=True, severity=5)
    assert better.description == "excellent"
    assert better.weight == 1.0


def test_rows_without_severity_fall_back_to_the_coarse_scale():
    row = Row(key="bleed", label="Bleed", effectiveness=IMMUNE, known=True)
    assert row.description == "immune"
    assert row.weight == 0.0


def test_only_extremes_are_notable():
    """Bolding every resistant row is the same as bolding none of them."""
    assert not Row("a", "A", RESISTANT, known=True, severity=1).is_notable
    assert not Row("a", "A", NORMAL, known=True, severity=3).is_notable
    assert Row("a", "A", WEAK, known=True, severity=4).is_notable
    assert Row("a", "A", IMMUNE, known=True).is_notable
    assert not Row("a", "A", WEAK, known=False, severity=4).is_notable


def test_tone_separates_poor_from_useless():
    assert Row("a", "A", RESISTANT, known=True, severity=2).tone == RESISTANT
    assert Row("a", "A", RESISTANT, known=True, severity=0).tone == IMMUNE
    assert Row("a", "A", NORMAL, known=True, severity=3).tone == NORMAL


# --- the driver ------------------------------------------------------------


def test_driver_shows_on_a_boss_and_hides_when_it_ends(database):
    overlay = FakeOverlay()
    driver = OverlayDriver(overlay)
    boss = database.require("margit")

    driver.update(fighting=False, boss=None)
    assert overlay.shown == [] and overlay.hides == 0

    driver.update(fighting=True, boss=boss)
    assert len(overlay.shown) == 1
    assert overlay.shown[0].name == boss.name

    driver.update(fighting=False, boss=None)
    assert overlay.hides == 1


def test_driver_does_not_redraw_the_same_boss(database):
    overlay = FakeOverlay()
    driver = OverlayDriver(overlay)
    boss = database.require("margit")
    for _ in range(50):
        driver.update(fighting=True, boss=boss)
    assert len(overlay.shown) == 1, "overlay repainted at poll rate"


def test_driver_redraws_on_a_phase_change(database):
    """Radagon handing off to the Elden Beast is a new boss, same fight."""
    overlay = FakeOverlay()
    driver = OverlayDriver(overlay)
    driver.update(fighting=True, boss=database.require("radagon"))
    driver.update(fighting=True, boss=database.require("elden_beast"))
    assert [c.name for c in overlay.shown] == [
        database.require("radagon").name,
        database.require("elden_beast").name,
    ]


def test_driver_hides_only_once(database):
    overlay = FakeOverlay()
    driver = OverlayDriver(overlay)
    driver.update(fighting=True, boss=database.require("margit"))
    for _ in range(10):
        driver.update(fighting=False, boss=None)
    assert overlay.hides == 1


def test_driver_ignores_a_fight_with_no_identified_boss(database):
    overlay = FakeOverlay()
    driver = OverlayDriver(overlay)
    driver.update(fighting=True, boss=None)
    assert overlay.shown == []
    assert driver.showing is None


# --- drawing ---------------------------------------------------------------


@pytest.fixture
def window():
    """Default view: compact."""
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    return OverlayWindow(style=OverlayStyle(scale=1.0))


@pytest.fixture
def full_window():
    from erdle.overlay_ui import FULL, OverlayStyle, OverlayWindow

    return OverlayWindow(style=OverlayStyle(scale=1.0, detail=FULL))


def test_full_view_emits_a_row_for_everything(database, full_window):
    canvas = FakeCanvas()
    content = build_content(database.require("malenia"))
    full_window._draw(canvas, content)
    strings = canvas.strings()
    for row in content.damage + content.statuses:
        assert any(row.label in s for s in strings), row.label


def test_compact_view_draws_only_the_highlights(database, window):
    canvas = FakeCanvas()
    content = build_content(database.require("malenia"))
    window._draw(canvas, content)
    strings = canvas.strings()

    for row in content.damage_highlights() + content.status_highlights():
        assert any(row.label in s for s in strings), row.label
    # Physical is measured but unremarkable, so it must not take a line.
    assert not any(s.endswith("Physical") for s in strings), strings


def test_compact_view_is_materially_shorter(database, window, full_window):
    for key in ("malenia", "erdtree_burial_watchdog", "margit"):
        content = build_content(database.require(key))
        compact = window._draw(FakeCanvas(), content)
        full = full_window._draw(FakeCanvas(), content)
        assert compact < full * 0.8, key


def test_draw_keeps_everything_inside_the_panel(database, window):
    for key in ("malenia", "erdtree_burial_watchdog", "margit"):
        canvas = FakeCanvas()
        height = window._draw(canvas, build_content(database.require(key)))
        assert height > 0
        lowest = max(item[2] for item in canvas.texts)
        assert lowest < height, f"{key}: text drawn past the panel bottom"


def test_draw_marks_the_recommended_row(database, window):
    canvas = FakeCanvas()
    content = build_content(database.require("flying_dragon_agheel"))
    window._draw(canvas, content)
    marked = [s for s in canvas.strings() if s.startswith("▸ ")]
    assert marked == ["▸ Pierce"], marked


def test_draw_adds_no_marker_when_nothing_is_recommended(window):
    entry = parse_entry(
        "x",
        {
            "name": "Equally Bad",
            "damage": {n: RESISTANT for n in ("standard", "slash")},
            "severity": {n: 1 for n in ("standard", "slash")},
            "statuses": {},
        },
    )
    canvas = FakeCanvas()
    window._draw(canvas, build_content(entry))
    assert not [s for s in canvas.strings() if s.startswith("▸ ")]


def test_draw_dims_unknown_rows(database, full_window):
    from erdle.overlay_ui import UNKNOWN_FG, UNKNOWN_TEXT

    # No shipped boss has an unrecorded status any more, so the dimming
    # has to be shown on a constructed entry.
    entry = parse_entry(
        "x",
        {"name": "Half Known", "damage": {}, "severity": {},
         "statuses": {"bleed": NORMAL}},
    )
    canvas = FakeCanvas()
    full_window._draw(canvas, build_content(entry))
    unknown = [i for i in canvas.texts if i[3]["text"] == UNKNOWN_TEXT]
    assert unknown, f"{entry.name}: unrecorded statuses should say so"
    assert all(i[3]["fill"] == UNKNOWN_FG for i in unknown)


def test_draw_scales(database, window):
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    content = build_content(database.require("malenia"))
    small = FakeCanvas()
    big = FakeCanvas()
    window._draw(small, content)
    OverlayWindow(style=OverlayStyle(scale=2.0))._draw(big, content)
    assert max(i[2] for i in big.texts) > max(i[2] for i in small.texts)


def test_every_boss_draws_without_error(database, window):
    for entry in database:
        canvas = FakeCanvas()
        height = window._draw(canvas, build_content(entry))
        assert height > 0, entry.key


# --- safety ----------------------------------------------------------------


def test_build_overlay_returns_a_no_op_without_tk(no_tk):
    """A machine with no Tk gets the keyboard panel and no complaints."""
    from erdle.config import Config
    from erdle.overlay_ui import build_overlay

    overlay = build_overlay(Config())
    assert overlay.available is False
    # Must accept the full protocol without complaint.
    overlay.show(None)
    overlay.hide()
    overlay.set_detail("full")
    overlay.set_enabled(True)
    overlay.stop()


def test_null_overlay_satisfies_the_driver():
    from erdle.overlay_ui import NullOverlay

    driver = OverlayDriver(NullOverlay())
    driver.update(fighting=True, boss=None)
    driver.update(fighting=False, boss=None)
    driver.close()


# --- end to end ------------------------------------------------------------


def test_overlay_follows_a_real_fight_through_the_app(database):
    """Drives ErdleApp exactly as tray.py and run.py do.

    The wiring in both entrypoints is four lines each and easy to get
    subtly wrong -- reading the snapshot before `step`, or forgetting that
    an unidentified fight has no boss. This runs the real pipeline.
    """
    from erdle.app import AppConfig, ErdleApp
    from erdle.detect import make_test_frame
    from erdle.ocr import ScriptedRecogniser
    from erdle.state import DetectorConfig, FightState

    width, height = 320, 180
    config = AppConfig(
        name_driven=False,
        detector=DetectorConfig(enter_frames=3, exit_frames=10),
    )
    app = ErdleApp(
        database,
        ScriptedRecogniser(["Malenia, Blade of Miquella"]),
        config=config,
    )
    overlay = FakeOverlay()
    driver = OverlayDriver(overlay)

    def pump(frame, now):
        app.step(frame, now)
        snapshot = app.tracker.snapshot
        driver.update(
            fighting=snapshot.state is FightState.FIGHTING,
            boss=snapshot.boss,
        )

    now = 0.0
    idle = make_test_frame(width, height, bar_fill=None)
    fighting = make_test_frame(width, height, bar_fill=0.8, with_name=True)

    for _ in range(10):          # exploring
        pump(idle, now)
        now += 1 / 30
    assert overlay.shown == [], "overlay appeared outside a fight"

    for _ in range(20):          # the fight
        pump(fighting, now)
        now += 1 / 30
    assert len(overlay.shown) == 1
    assert overlay.shown[0].name == "Malenia, Blade of Miquella"
    assert overlay.hides == 0, "overlay flickered during the fight"

    for _ in range(30):          # fight over
        pump(idle, now)
        now += 1 / 30
    assert overlay.hides == 1
    assert driver.showing is None


# --- compact selection -----------------------------------------------------


def test_highlights_pick_weaknesses_over_the_least_bad(database):
    content = build_content(database.require("margit"))
    labels = [row.label for row in content.damage_highlights()]
    assert labels[:2] == ["Slash", "Holy"]


def test_highlights_fall_back_to_the_least_bad(database):
    """No weakness means the player still has to hit it with something."""
    content = build_content(database.require("flying_dragon_agheel"))
    labels = [row.label for row in content.damage_highlights()]
    assert labels[0] == "Pierce"


def test_highlights_name_a_type_worth_avoiding(database):
    content = build_content(database.require("malenia"))
    labels = [row.label for row in content.damage_highlights()]
    assert "Holy" in labels, "the worst type is the other half of the advice"


def test_highlights_omit_a_worst_that_is_not_bad_enough(database):
    """A boss that resists nothing has nothing to warn about."""
    entry = parse_entry(
        "x",
        {
            "name": "Resists Nothing",
            "damage": {"slash": WEAK},
            "severity": {"slash": 4, "standard": 3, "strike": 3},
            "statuses": {},
        },
    )
    content = build_content(entry)
    assert content.worst_damage is None
    assert [row.label for row in content.damage_highlights()] == ["Slash"]


def test_status_highlights_always_answer_the_bleed_question(database):
    for entry in database:
        rows = build_content(entry).status_highlights()
        assert any(row.key == "bleed" for row in rows), entry.key


def test_status_highlights_drop_unremarkable_rows():
    """Only what is not ordinary, plus bleed whatever its value."""
    entry = parse_entry(
        "x",
        {
            "name": "Ordinary",
            "damage": {},
            "severity": {},
            "statuses": {"bleed": NORMAL, "poison": NORMAL, "rot": NORMAL},
        },
    )
    content = build_content(entry)
    assert [row.key for row in content.status_highlights()] == ["bleed"]


def test_status_highlights_keep_display_order(database):
    content = build_content(database.require("malenia"))
    order = [row.key for row in content.statuses]
    picked = [row.key for row in content.status_highlights()]
    assert picked == sorted(picked, key=order.index)


def test_highlights_are_bounded(database):
    for entry in database:
        content = build_content(entry)
        assert len(content.damage_highlights()) <= 4
        assert len(content.status_highlights()) <= 4


def test_compact_panels_stay_reasonably_small(database, window):
    """The whole point of compact is that it does not eat the screen.

    The tallest case is a boss with four damage highlights, four unusual
    statuses and a poise value -- Demi-Human Chief, at 398px. That is
    about a quarter of a 1440p screen. The bound is a budget: if a change
    pushes any boss past it, the compact view has stopped being compact.

    It rose when the game data landed: NpcParam records sleep and madness
    immunities that the spreadsheet never had, so more bosses now have
    four notable statuses instead of one or two.
    """
    heights = [
        window._draw(FakeCanvas(), build_content(entry)) for entry in database
    ]
    assert max(heights) <= 420, max(heights)
    assert sum(heights) / len(heights) <= 300


# --- position ---------------------------------------------------------------


def _dropped_at(window, x, y, screen=(3840, 2160)):
    root = FakeRoot(screen=screen)
    root.x, root.y = x, y
    window._root = root
    window._drop(None)
    return window._wanted


def test_dragging_stores_a_fraction_not_a_pixel():
    """Hiding clears `_visible`, so the next show re-reads `_wanted`.

    Without updating it on drop, the window snapped back to its start-up
    position on the next fight and silently undid the drag. It is stored
    as a fraction so the same drag means the same place on any display.
    """
    from erdle.overlay_ui import OverlayStyle, OverlayWindow, PANEL_WIDTH

    saved = []
    window = OverlayWindow(
        style=OverlayStyle(),
        position=(0.1, 0.1),
        on_move=lambda fx, fy: saved.append((fx, fy)),
    )
    free = 3840 - (PANEL_WIDTH + 2)
    fx, fy = _dropped_at(window, free, 216)      # flush right, 10% down

    assert fx == pytest.approx(1.0)
    assert fy == pytest.approx(0.1)
    assert saved == [(fx, fy)]


def test_drop_clears_any_legacy_pixel_position():
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow(
        style=OverlayStyle(), position=(None, None), legacy_pixels=(2900, 60)
    )
    _dropped_at(window, 100, 100)
    assert window._legacy == (None, None)


def test_drop_without_a_callback_still_remembers():
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow(style=OverlayStyle(), position=(0.0, 0.0))
    fx, fy = _dropped_at(window, 900, 12)
    assert 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0


# --- resolution independence ------------------------------------------------


def test_the_same_fraction_lands_proportionally_on_every_display():
    """A drag on a 4K screen must mean the same thing on a 1080p one."""
    from erdle.overlay_ui import OverlayStyle, OverlayWindow, PANEL_WIDTH

    window = OverlayWindow(style=OverlayStyle(), position=(1.0, 0.05))
    for screen in ((3840, 2160), (2560, 1440), (1920, 1080), (1366, 768)):
        window._visible = False
        root = FakeRoot(screen=screen)
        x = window._x(root, 250)
        y = window._y(root, 250)
        # Flush right on every one of them, and fully on screen.
        assert x + PANEL_WIDTH + 2 <= screen[0]
        assert x == screen[0] - (PANEL_WIDTH + 2)
        assert y == pytest.approx(screen[1] * 0.05, abs=1)


def test_a_panel_can_never_hang_off_a_small_screen():
    from erdle.overlay_ui import OverlayStyle, OverlayWindow, PANEL_WIDTH

    window = OverlayWindow(style=OverlayStyle(), position=(1.0, 1.0))
    window._visible = False
    root = FakeRoot(screen=(1280, 720))
    x, y = window._x(root, 300), window._y(root, 300)
    assert 0 <= x <= 1280 - (PANEL_WIDTH + 2)
    assert 0 <= y <= 720 - 300, "a tall panel was allowed off the bottom"


def test_vertical_clamp_follows_the_panel_height():
    """A position that fits a short panel must not drop a tall one off."""
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow(style=OverlayStyle(), position=(0.5, 0.95))
    root = FakeRoot(screen=(1920, 1080))

    window._visible = False
    short = window._y(root, 150)
    window._visible = False
    tall = window._y(root, 400)

    assert short + 150 <= 1080
    assert tall + 400 <= 1080
    assert tall < short


# --- migration from the old pixel positions ---------------------------------


def test_legacy_pixels_are_converted_on_first_use():
    """Upgrading must not silently move someone's window."""
    from erdle.overlay_ui import OverlayStyle, OverlayWindow, PANEL_WIDTH

    window = OverlayWindow(
        style=OverlayStyle(), position=(None, None), legacy_pixels=(3400, 108)
    )
    window._visible = False
    root = FakeRoot(screen=(3840, 2160))

    assert window._x(root, 250) == 3400
    assert window._y(root, 250) == 108

    free = 3840 - (PANEL_WIDTH + 2)
    assert window._wanted[0] == pytest.approx(3400 / free)
    assert window._wanted[1] == pytest.approx(108 / 2160)


def test_no_position_at_all_still_gets_the_default_corner():
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow(style=OverlayStyle(), position=(None, None))
    window._visible = False
    root = FakeRoot(screen=(3840, 2160))
    assert window._x(root, 250) > 3840 * 0.8
    assert window._y(root, 250) < 100


# --- placement --------------------------------------------------------------


class FakeRoot:
    """A root that can pretend the window manager ignored us.

    `obeys_after` is how many `geometry` calls it takes before the window
    actually lands where it was asked -- 0 for a well-behaved manager, 1
    for Windows placing an overrideredirect window itself on first map,
    and a large number for one that never complies.
    """

    def __init__(self, obeys_after: int = 0, screen=(3840, 2160)):
        self.obeys_after = obeys_after
        self.calls = 0
        self.x, self.y = 640, 0          # somewhere unhelpful, e.g. top-middle
        self.screen = screen
        self.geometries: list[str] = []

    def update_idletasks(self):
        pass

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def winfo_screenwidth(self):
        return self.screen[0]

    def winfo_screenheight(self):
        return self.screen[1]

    def geometry(self, spec):
        self.geometries.append(spec)
        self.calls += 1
        if self.calls > self.obeys_after:
            _, _, position = spec.partition("+")
            x_text, _, y_text = position.partition("+")
            self.x, self.y = int(x_text), int(y_text)


def test_place_accepts_a_window_that_lands_correctly(window):
    root = FakeRoot(obeys_after=0)
    root.x, root.y = 2900, 60
    result = window.place(root, 2900, 60, 302, 250)
    assert result == (2900, 60)
    assert root.geometries == [], "no correction needed, none should be sent"
    assert window.last_placement[4].startswith("ok")


def test_place_corrects_a_window_the_manager_moved(window):
    """The reported bug: the panel appeared top-middle, not where asked."""
    root = FakeRoot(obeys_after=0)
    root.x, root.y = 640, 0
    result = window.place(root, 2900, 60, 302, 250)
    assert result == (2900, 60), "placement was not corrected"
    assert root.geometries, "no correction attempted"
    assert window.last_placement[4].startswith("ok")


def test_place_gives_up_and_records_the_drift(window):
    root = FakeRoot(obeys_after=99)
    window.place(root, 2900, 60, 302, 250)
    wanted_x, wanted_y, got_x, got_y, outcome = window.last_placement
    assert (wanted_x, wanted_y) == (2900, 60)
    assert (got_x, got_y) == (640, 0)
    assert outcome == "drifted"
    assert len(root.geometries) <= 3, "must not retry forever"


def test_place_survives_a_root_that_raises(window):
    class Broken:
        def update_idletasks(self): raise RuntimeError("gone")
    result = window.place(Broken(), 10, 20, 302, 250)
    assert result == (10, 20)
    assert window.last_placement[4] == "unreadable"


def test_default_corner_is_top_right(window):
    root = FakeRoot(screen=(3840, 2160))
    window._wanted = (None, None)
    window._legacy = (None, None)
    window._visible = False
    x = window._x(root, 250)
    assert x > 3840 * 0.8, f"default corner landed at {x}, not on the right"
    assert window._y(root, 250) < 100


def test_saved_fraction_resolves_against_the_live_screen(window):
    from erdle.overlay_ui import PANEL_WIDTH

    root = FakeRoot(screen=(3840, 2160))
    window._wanted = (0.5, 0.25)
    window._visible = False
    free = 3840 - (PANEL_WIDTH + 2)
    assert window._x(root, 250) == pytest.approx(free * 0.5, abs=1)
    assert window._y(root, 250) == pytest.approx(2160 * 0.25, abs=1)


def test_dpi_awareness_is_reported_not_raised():
    from erdle.overlay_ui import enable_dpi_awareness

    assert isinstance(enable_dpi_awareness(), str)


# --- long names -------------------------------------------------------------


def test_no_two_rows_overlap_for_any_boss(database, window):
    """The reported bug: long names ran into the line underneath.

    Checked for every boss rather than a chosen few, because which names
    wrap depends on glyph widths and is not obvious from their length.
    """
    for entry in database:
        canvas = FakeCanvas()
        window._draw(canvas, build_content(entry))
        boxes = sorted(
            (canvas.bbox(index + 1) for index, item in enumerate(canvas.items)
             if item[0] == "text"),
            key=lambda box: box[1],
        )
        for upper, lower in zip(boxes, boxes[1:]):
            # Rows sharing a baseline (label and value) are fine; what
            # must not happen is one row's text starting above the bottom
            # of the row before it.
            if lower[1] == upper[1]:
                continue
            assert lower[1] >= upper[3], (
                f"{entry.name}: text at y={lower[1]} overlaps "
                f"the item ending at y={upper[3]}"
            )


def _entry_named(name):
    return parse_entry(
        "x",
        {
            "name": name,
            "damage": {"slash": WEAK},
            "severity": {"slash": 4},
            "statuses": {"bleed": NORMAL},
        },
    )


def test_a_wrapping_name_makes_a_taller_panel(window):
    """Identical content, different name lengths.

    Comparing two real bosses would not work: they have different numbers
    of rows, so the panel height difference says nothing about the title.
    """
    short = window._draw(FakeCanvas(), build_content(_entry_named("Margit")))
    wrapped = window._draw(
        FakeCanvas(),
        build_content(_entry_named("Sir Gideon Ofnir, the All-Knowing")),
    )
    assert wrapped > short, "a two-line title did not grow the panel"


def test_measurement_falls_back_when_the_canvas_cannot_measure():
    from erdle.overlay_ui import _measure

    class Unmeasurable:
        def bbox(self, item):
            raise RuntimeError("no font metrics here")

    assert _measure(Unmeasurable(), 1, fallback=42) == 42


def test_measurement_falls_back_on_an_empty_box():
    from erdle.overlay_ui import _measure

    class Empty:
        def bbox(self, item):
            return None

    assert _measure(Empty(), 1, fallback=17) == 17


# --- display scaling --------------------------------------------------------


class ScaledCanvas(FakeCanvas):
    """A canvas on a 150%-scaled display, like a 4K monitor at default."""

    dpi_scale = 1.5


def _overlaps(canvas):
    boxes = sorted(
        (canvas.bbox(index + 1) for index, item in enumerate(canvas.items)
         if item[0] == "text"),
        key=lambda box: box[1],
    )
    return [
        (upper, lower) for upper, lower in zip(boxes, boxes[1:])
        if lower[1] != upper[1] and lower[1] < upper[3]
    ]


def test_no_overlap_at_150_percent_display_scaling(database, window):
    """The case that actually broke on a 4K monitor.

    Tk sizes point-based fonts against the display DPI, so at 150% the
    text grew by half while every layout constant stayed at 96-DPI pixels.
    Long names suffered first: a title that needed two lines at 96 DPI
    needed three. Fonts are now specified in pixels, so this cannot drift.
    """
    for entry in database:
        canvas = ScaledCanvas()
        window._draw(canvas, build_content(entry))
        assert not _overlaps(canvas), entry.name


def test_fonts_are_specified_in_pixels_not_points():
    """Points are DPI-relative; the layout constants are not."""
    from erdle.overlay_ui import OverlayStyle

    family, size, weight = OverlayStyle().font(12, "bold")
    assert size < 0, "a positive Tk size means points, which the DPI scales"
    assert weight == "bold"


def test_scaling_grows_text_and_layout_together():
    """If one grows and the other does not, rows collide."""
    from erdle.overlay_ui import OverlayStyle

    normal = OverlayStyle(scale=1.0)
    doubled = OverlayStyle(scale=2.0)
    assert -doubled.font(12)[1] == pytest.approx(-normal.font(12)[1] * 2, abs=1)
    assert doubled.px(23) == pytest.approx(normal.px(23) * 2, abs=1)


# --- surface vs drawing -----------------------------------------------------


class FakeRootWithDpi(FakeRoot):
    """A root that reports a scaled display, like 4K Windows at 150%."""

    def __init__(self, dpi=144.0, **kwargs):
        super().__init__(**kwargs)
        self.dpi = dpi
        self.geometries = []

    def winfo_fpixels(self, spec):
        return self.dpi

    def deiconify(self): pass
    def withdraw(self): pass
    def attributes(self, *args): pass


def test_display_scaling_grows_the_canvas_too(database):
    """The surface must be as wide as the drawing.

    Field bug: the display scale was applied after the canvas was built,
    so the canvas stayed 300px while everything was drawn at 450px. The
    boss name was cut off mid-word and the whole value column fell off the
    right-hand edge.
    """
    from erdle.overlay_ui import PANEL_WIDTH, OverlayStyle, OverlayWindow

    window = OverlayWindow(style=OverlayStyle(), position=(1.0, 0.05))
    root = FakeRootWithDpi(dpi=144.0)
    window._apply_display_scale(root)
    assert window.display_scale == pytest.approx(1.5)

    canvas = FakeCanvas()
    window._root, window._canvas = root, canvas
    window._do_show(build_content(database.require("flying_dragon_agheel")))

    drawn_width = window.style.px(PANEL_WIDTH)
    assert canvas.config["width"] == drawn_width, (
        f"canvas is {canvas.config['width']}px but the drawing is {drawn_width}px"
    )
    assert drawn_width == pytest.approx(PANEL_WIDTH * 1.5, abs=2)


def test_nothing_is_drawn_past_the_panel_edge(database):
    """Every right-anchored value has to land inside the canvas."""
    from erdle.overlay_ui import PANEL_WIDTH, OverlayStyle, OverlayWindow

    for scale in (1.0, 1.5, 2.0):
        window = OverlayWindow(style=OverlayStyle(scale=scale))
        width = window.style.px(PANEL_WIDTH)
        for key in ("flying_dragon_agheel", "malenia", "erdtree_burial_watchdog"):
            canvas = FakeCanvas()
            window._draw(canvas, build_content(database.require(key)))
            for item in canvas.items:
                x = item[1] if item[0] == "text" else item[3]
                assert x <= width, f"scale {scale}, {key}: drawn at x={x} of {width}"


def test_display_scale_is_bounded():
    """A misreported DPI must not produce a full-screen panel."""
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    for dpi, expected in ((96.0, 1.0), (48.0, 1.0), (144.0, 1.5), (9600.0, 3.0)):
        window = OverlayWindow(style=OverlayStyle())
        window._apply_display_scale(FakeRootWithDpi(dpi=dpi))
        assert window.display_scale == pytest.approx(expected)


def test_a_root_that_cannot_report_dpi_is_left_alone():
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    class Mute:
        def winfo_fpixels(self, spec):
            raise RuntimeError("no display")

    window = OverlayWindow(style=OverlayStyle(scale=1.0))
    window._apply_display_scale(Mute())
    assert window.style.scale == 1.0
    assert window.display_scale == 1.0


def test_an_empty_section_draws_no_heading(database, window):
    """Seven bosses have completely flat damage -- nothing to recommend.

    A "DAMAGE" heading over blank space reads as a rendering fault. The
    summary line already says there is nothing notable.
    """
    flat = [e for e in database if not build_content(e).damage_highlights()]
    assert flat, "expected at least one boss with no damage highlights"

    for entry in flat:
        canvas = FakeCanvas()
        window._draw(canvas, build_content(entry))
        assert "DAMAGE" not in canvas.strings(), entry.name


def test_sections_with_rows_still_get_their_heading(database, window):
    canvas = FakeCanvas()
    window._draw(canvas, build_content(database.require("flying_dragon_agheel")))
    strings = canvas.strings()
    assert "DAMAGE" in strings and "STATUS" in strings


def test_no_gap_is_left_where_a_section_was_skipped(database, window):
    """Dropping the section must not leave its spacing behind."""
    flat = next(e for e in database if not build_content(e).damage_highlights())
    canvas = FakeCanvas()
    window._draw(canvas, build_content(flat))
    heights = [canvas.bbox(i + 1) for i, item in enumerate(canvas.items)
               if item[0] == "text"]
    gaps = [b[1] - a[3] for a, b in zip(heights, heights[1:]) if b[1] > a[3]]
    assert not gaps or max(gaps) < 40, f"suspicious gap: {max(gaps)}px"


# --- live settings ----------------------------------------------------------


def _wired_window(detail="compact", enabled=True):
    """A window with a fake root and canvas, ready to draw."""
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow(
        style=OverlayStyle(detail=detail), position=(1.0, 0.05), enabled=enabled
    )
    window._root = FakeRootWithDpi(dpi=96.0)
    window._canvas = FakeCanvas()
    return window


def _drain(window):
    """Run whatever is queued, the way the Tk pump would."""
    import queue as _queue

    while True:
        try:
            action, payload = window._commands.get_nowait()
        except _queue.Empty:
            return
        # The real dispatch, not the individual handlers -- otherwise a
        # command dropped from the pump would go unnoticed.
        window.dispatch(action, payload)


def test_switching_detail_repaints_immediately(database):
    """The reported bug: the view only changed after a restart.

    A setting that needs a restart looks broken, so the user clicks it
    again -- which puts it straight back where it started.
    """
    from erdle.overlay_ui import COMPACT, FULL

    window = _wired_window(detail=COMPACT)
    content = build_content(database.require("malenia"))
    window.show(content)
    _drain(window)
    compact_rows = len(window._canvas.strings())

    window.set_detail(FULL)
    _drain(window)
    full_rows = len(window._canvas.strings())

    assert window.style.detail == FULL
    assert full_rows > compact_rows, "the panel did not repaint in full detail"


def test_switching_detail_back_repaints_again(database):
    from erdle.overlay_ui import COMPACT, FULL

    window = _wired_window(detail=FULL)
    window.show(build_content(database.require("malenia")))
    _drain(window)
    full_rows = len(window._canvas.strings())

    window.set_detail(COMPACT)
    _drain(window)
    assert len(window._canvas.strings()) < full_rows


def test_setting_the_same_detail_is_a_no_op(database):
    from erdle.overlay_ui import COMPACT

    window = _wired_window(detail=COMPACT)
    window.show(build_content(database.require("malenia")))
    _drain(window)
    before = list(window._canvas.items)

    window.set_detail(COMPACT)
    _drain(window)
    assert len(window._canvas.items) == len(before)


def test_detail_changes_while_hidden_apply_on_the_next_show(database):
    from erdle.overlay_ui import COMPACT, FULL

    window = _wired_window(detail=COMPACT)
    window.set_detail(FULL)
    _drain(window)
    assert window.style.detail == FULL

    window.show(build_content(database.require("malenia")))
    _drain(window)
    strings = window._canvas.strings()
    assert "Lightning" in strings, "full detail was not used on the next show"


def test_disabling_hides_the_panel_without_a_restart(database):
    window = _wired_window()
    window.show(build_content(database.require("margit")))
    _drain(window)
    assert window._visible

    window.set_enabled(False)
    _drain(window)
    assert not window._visible
    assert window.enabled is False


def test_re_enabling_repaints_the_fight_already_in_progress(database):
    window = _wired_window(enabled=False)
    window.show(build_content(database.require("margit")))
    _drain(window)
    assert not window._visible, "a disabled overlay must not appear"

    window.set_enabled(True)
    _drain(window)
    assert window._visible, "re-enabling did not bring the panel back"
    assert "MARGIT, THE FELL OMEN" in window._canvas.strings()


def test_a_disabled_overlay_still_tracks_the_current_boss(database):
    """So that switching it on mid-fight shows the right boss."""
    window = _wired_window(enabled=False)
    window.show(build_content(database.require("fire_giant")))
    _drain(window)
    assert window._last_content is not None
    assert window._last_content.name == "Fire Giant"


def test_the_window_is_built_even_when_the_overlay_is_off(monkeypatch):
    """Otherwise the tray toggle has nothing to turn back on.

    Uses a stand-in for the window rather than the real thing: building a
    genuine Tk root inside the test suite leaves a live GUI thread behind,
    and on Windows the interpreter dies at shutdown trying to tear it down.
    """
    import sys
    import types

    import erdle.overlay_ui as overlay_ui
    from erdle.config import Config

    # Pretend Tk is importable, so the test runs the same way on a build
    # machine with it and a CI box without.
    monkeypatch.setitem(sys.modules, "tkinter", types.ModuleType("tkinter"))

    built = {}

    class SpyWindow:
        available = True

        def __init__(self, **kwargs):
            built.update(kwargs)
            self._root = object()      # pretend Tk came up

        def start(self):
            built["started"] = True

    monkeypatch.setattr(overlay_ui, "OverlayWindow", SpyWindow)

    config = Config()
    config.overlay_enabled = False
    overlay = overlay_ui.build_overlay(config)

    assert isinstance(overlay, SpyWindow), "a disabled overlay was not built"
    assert built["enabled"] is False, "it should start switched off"
    assert built.get("started") is True


def test_the_gui_thread_guard_actually_fires():
    """Proof the conftest guard works, without needing Tk.

    The guard is what turns "the interpreter died during the build" into
    a named failing test, so it is worth knowing it is wired up.
    """
    import threading

    from conftest import GUI_THREADS

    assert "erdle-overlay" in GUI_THREADS

    stop = threading.Event()
    stray = threading.Thread(
        target=stop.wait, name="erdle-overlay", daemon=True
    )
    stray.start()
    try:
        alive = [t.name for t in threading.enumerate()
                 if t.name in GUI_THREADS and t.is_alive()]
        assert alive == ["erdle-overlay"]
    finally:
        stop.set()
        stray.join(timeout=2.0)


# --- why there is no overlay ------------------------------------------------


def test_null_overlay_carries_a_reason():
    """"off" alone was unactionable: four causes, one word.

    A user reporting "the overlay doesn't show up" could mean Tk is
    missing, the constructor raised, the Tk thread died, or the toggle is
    off -- and the fixes are all different.
    """
    from erdle.overlay_ui import NullOverlay

    assert NullOverlay().unavailable_reason
    assert NullOverlay("Tk exploded").unavailable_reason == "Tk exploded"


def test_build_overlay_reports_a_missing_tkinter(no_tk):
    from erdle.overlay_ui import build_overlay

    class Settings:
        overlay_enabled = True

    overlay = build_overlay(Settings())
    assert overlay.available is False
    assert "tkinter" in overlay.unavailable_reason


@pytest.fixture
def fake_tk(monkeypatch):
    """Make `import tkinter` succeed regardless of the machine.

    Without this the two tests below pass for the wrong reason on any box
    without Tk -- they would stop at the import guard and never reach the
    path they exist to cover.
    """
    import sys
    import types

    monkeypatch.setitem(sys.modules, "tkinter", types.ModuleType("tkinter"))
    yield


def test_build_overlay_reports_a_dead_tk_thread(monkeypatch, fake_tk):
    """A window that never opens must say so, not time out silently."""
    import erdle.overlay_ui as ui

    class DeadWindow:
        available = True

        def __init__(self, **kwargs):
            self.start_error = "Tk thread died: DisplayError"
            self._root = None

        def start(self):
            pass

    monkeypatch.setattr(ui, "OverlayWindow", DeadWindow)

    class Settings:
        overlay_enabled = True

    overlay = ui.build_overlay(Settings())
    assert overlay.available is False
    assert "DisplayError" in overlay.unavailable_reason


def test_build_overlay_reports_a_raising_constructor(monkeypatch, fake_tk):
    import erdle.overlay_ui as ui

    def explode(**kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(ui, "OverlayWindow", explode)

    class Settings:
        overlay_enabled = True

    overlay = ui.build_overlay(Settings())
    assert overlay.available is False
    assert "RuntimeError: no display" == overlay.unavailable_reason


def test_stopping_releases_the_canvas_as_well_as_the_root():
    """Regression: "Tcl_AsyncDelete ... wrong thread" on every exit.

    Clearing `_root` alone left the canvas holding the Tcl interpreter
    alive, so it was finalised from the main thread at interpreter
    shutdown -- which is exactly what Tk refuses to allow.
    """
    from erdle.overlay_ui import OverlayWindow

    window = OverlayWindow.__new__(OverlayWindow)
    window._root = object()
    window._canvas = object()
    window._visible = True
    window._commands = __import__("queue").Queue()
    window._commands.put(("stop", None))

    class Root:
        destroyed = False

        def quit(self):
            pass

        def destroy(self):
            Root.destroyed = True

        def after(self, *args):
            raise AssertionError("must not reschedule after stopping")

    window._root = Root()
    window._pump()

    assert Root.destroyed
    assert window._root is None
    assert window._canvas is None
    assert window._visible is False


# --- the summary line -------------------------------------------------------


def test_the_headline_is_not_drawn_when_there_are_rows(database):
    """It restated the two sections beneath it and cost two lines.

    Every fact in "immune to frost; weak to slash" is already in the
    DAMAGE and STATUS rows, in a form that is faster to read.
    """
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow.__new__(OverlayWindow)
    window.style = OverlayStyle()
    canvas = FakeCanvas()
    content = build_content(database.require("margit"))
    window._draw(canvas, content)

    assert content.headline
    assert content.headline not in canvas.strings()
    assert content.name.upper() in canvas.strings()


def test_the_headline_survives_when_there_is_nothing_else(database):
    """A panel showing only a name reads as a rendering fault.

    Seven bosses have completely flat stats, so neither section has a row
    and the summary is the only thing left to say.
    """
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    content = OverlayContent(name="Equally Bad", damage=(), statuses=())
    assert not content.damage_highlights()
    assert not content.status_highlights()

    window = OverlayWindow.__new__(OverlayWindow)
    window.style = OverlayStyle()
    canvas = FakeCanvas()
    window._draw(canvas, content)
    assert content.headline in canvas.strings()


def test_the_panel_got_shorter(database):
    """Guards the size the user actually asked for.

    Locks in a ceiling rather than an exact number, so the layout can be
    tuned without rewriting the test -- but a change that quietly grows
    the panel back fails.
    """
    from erdle.overlay_ui import OverlayStyle, OverlayWindow

    window = OverlayWindow.__new__(OverlayWindow)
    window.style = OverlayStyle()
    tallest = 0
    for entry in database:
        canvas = FakeCanvas()
        tallest = max(tallest, window._draw(canvas, build_content(entry)))
    assert tallest <= 280, f"compact panel grew to {tallest}px"
