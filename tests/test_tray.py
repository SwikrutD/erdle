"""Guards on the tray worker's shutdown path.

The tray has no console and no window. If Quit does not work there is no
second way to close the app, so the shutdown path gets its own tests even
though the rest of tray.py is untestable without a display.
"""

from __future__ import annotations

import os
import threading

import pytest

tray = pytest.importorskip("tray")

from erdle.config import Config  # noqa: E402


@pytest.fixture
def worker(tmp_path):
    log = tray.Log(tmp_path / "erdle.log")
    return tray.Worker(Config(), log)


def test_worker_does_not_shadow_thread_internals(worker):
    """A Thread subclass must not assign over its base class's methods.

    The original code did `self._stop = threading.Event()`. On Python 3.10
    through 3.12 `Thread._stop` is a real method that `join()` and
    `is_alive()` call once a thread has finished, so the assignment turned
    both into "'Event' object is not callable" -- at shutdown only, which
    is exactly when the tray needs them.

    CPython 3.13 removed `Thread._stop`, so the bug is invisible there.
    That is the reason this test enumerates whatever the running
    interpreter actually defines instead of naming attributes: a guard
    that only fires on the version you happen to test on is not a guard.
    """
    for attribute in dir(threading.Thread):
        base = getattr(threading.Thread, attribute, None)
        if not callable(base):
            continue
        actual = getattr(worker, attribute, base)
        assert callable(actual), (
            f"Worker shadowed Thread.{attribute} with a "
            f"{type(actual).__name__}, which will break at shutdown"
        )


def test_worker_uses_a_distinct_stop_event(worker):
    assert isinstance(worker._stop_event, threading.Event)
    assert not worker._stop_event.is_set()


def test_request_stop_then_join_does_not_raise(tmp_path):
    """The exact call sequence the Quit menu item performs."""

    class Looping(tray.Worker):
        def _run(self) -> None:
            while not self._stop_event.is_set():
                self._stop_event.wait(0.01)

    worker = Looping(Config(), tray.Log(tmp_path / "erdle.log"))
    worker.start()
    worker.request_stop()
    worker.join(timeout=5.0)      # raised TypeError before the fix
    assert not worker.is_alive()  # and so did this


def test_status_transitions_are_reported_once(worker):
    worker.set_status(tray.Status.RUNNING)
    assert worker.take_status_change() is True
    assert worker.take_status_change() is False
    worker.set_status(tray.Status.RUNNING)
    assert worker.take_status_change() is False, "no-op status change signalled"


# --- tray icon --------------------------------------------------------------


def _average_colour(image):
    """Mean RGB over the pixels that are actually drawn."""
    pixels = image.load()
    total = [0, 0, 0]
    counted = 0
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            if a > 60:
                total[0] += r
                total[1] += g
                total[2] += b
                counted += 1
    if not counted:
        return (0, 0, 0)
    return tuple(round(channel / counted) for channel in total)


def _dominant(image):
    """Which channel wins: 'r', 'g', 'b', or 'neutral'."""
    r, g, b = _average_colour(image)
    if max(r, g, b) - min(r, g, b) < 25:
        return "neutral"
    return "rgb"[[r, g, b].index(max(r, g, b))]


def test_every_state_has_bundled_artwork():
    """Shipping without it degrades to a drawn rune, which looks broken."""
    for state in tray.ICON_STATES:
        assert tray.bundled_icon_path(state) is not None, state


def test_every_status_maps_to_a_known_state():
    """Adding a Status without an icon would silently fall back to idle."""
    for name, status in vars(tray.Status).items():
        if name.startswith("__"):
            continue
        assert status in tray.ICON_STATE, f"Status.{name} has no icon state"
        assert tray.ICON_STATE[status] in tray.ICON_STATES, name


@pytest.mark.parametrize(
    "status,expected",
    [
        (tray.Status.RUNNING, "r"),        # gold: red-dominant
        (tray.Status.ERROR, "r"),          # red
        (tray.Status.CALIBRATING, "b"),    # blue
        (tray.Status.STARTING, "neutral"), # pale
        (tray.Status.STOPPED, "neutral"),
    ],
)
def test_each_status_looks_the_way_it_should(status, expected):
    """Measured from the pixels, not asserted from the filename."""
    assert _dominant(tray.make_icon_image(status, 32)) == expected


def test_the_six_statuses_are_visually_distinguishable():
    """Four distinct marks; only the two 'off' states may look alike."""
    colours = {
        status: _average_colour(tray.make_icon_image(status, 32))
        for status in (
            tray.Status.RUNNING, tray.Status.CALIBRATING,
            tray.Status.NO_GG, tray.Status.ERROR, tray.Status.STOPPED,
        )
    }
    assert len(set(colours.values())) == 5, colours


def test_gold_and_amber_are_actually_different():
    """The pair most at risk of collapsing into one orange smudge."""
    running = _average_colour(tray.make_icon_image(tray.Status.RUNNING, 32))
    no_gg = _average_colour(tray.make_icon_image(tray.Status.NO_GG, 32))
    distance = sum(abs(a - b) for a, b in zip(running, no_gg))
    assert distance > 60, f"{running} vs {no_gg}"


def test_an_unknown_status_is_treated_as_idle():
    unknown = tray.make_icon_image("something-added-later", 32)
    idle = tray.make_icon_image(tray.Status.STOPPED, 32)
    assert _average_colour(unknown) == _average_colour(idle)


def test_icons_are_produced_at_the_requested_size():
    for size in (16, 32, 64, 256):
        image = tray.make_icon_image(tray.Status.RUNNING, size)
        assert image.size == (size, size)
        assert image.mode == "RGBA"


# --- fallback chain ---------------------------------------------------------


def test_a_missing_state_borrows_a_neighbour(monkeypatch):
    """A partial icon set must not drop to the drawn rune."""
    real = tray.bundled_icon_path
    monkeypatch.setattr(
        tray, "bundled_icon_path",
        lambda state: None if state == tray.CALIBRATING else real(state),
    )
    image = tray.make_icon_image(tray.Status.CALIBRATING, 32)
    assert _average_colour(image) == _average_colour(
        tray.make_icon_image(tray.Status.RUNNING, 32)
    )


def test_the_fallback_chain_cannot_loop(monkeypatch):
    monkeypatch.setattr(tray, "bundled_icon_path", lambda state: None)
    monkeypatch.setattr(tray, "custom_icon_path", lambda state: None)
    monkeypatch.setattr(
        tray, "ICON_FALLBACK", {tray.ACTIVE: tray.IDLE, tray.IDLE: tray.ACTIVE}
    )
    image = tray.make_icon_image(tray.Status.RUNNING, 32)   # must terminate
    assert image.size == (32, 32)


def test_the_drawn_fallback_still_works_with_no_art_at_all(monkeypatch):
    monkeypatch.setattr(tray, "bundled_icon_path", lambda state: None)
    monkeypatch.setattr(tray, "custom_icon_path", lambda state: None)

    image = tray.make_icon_image(tray.Status.ERROR, 32)
    assert image.size == (32, 32)
    red = _average_colour(image)
    assert red[0] > red[2], "the drawn fallback should keep the status colour"


# --- user overrides ---------------------------------------------------------


def test_a_supplied_icon_overrides_the_bundled_one(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(tray, "config_path", lambda: tmp_path / "config.json")
    Image.new("RGBA", (200, 200), (0, 255, 0, 255)).save(tmp_path / "icon-active.png")

    assert tray.make_icon_image(tray.Status.RUNNING, 64).getpixel((32, 32))[:3] == (0, 255, 0)
    # ...and only for the state it was supplied for.
    assert tray.make_icon_image(tray.Status.ERROR, 64).getpixel((32, 32))[:3] != (0, 255, 0)


def test_a_plain_icon_png_covers_every_state(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(tray, "config_path", lambda: tmp_path / "config.json")
    Image.new("RGBA", (64, 64), (0, 255, 0, 255)).save(tmp_path / "icon.png")

    for status in (tray.Status.RUNNING, tray.Status.ERROR, tray.Status.STOPPED):
        assert tray.make_icon_image(status, 32).getpixel((16, 16))[:3] == (0, 255, 0)


def test_a_state_specific_icon_beats_the_generic_one(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(tray, "config_path", lambda: tmp_path / "config.json")
    Image.new("RGBA", (64, 64), (0, 255, 0, 255)).save(tmp_path / "icon.png")
    Image.new("RGBA", (64, 64), (0, 0, 255, 255)).save(tmp_path / "icon-error.png")

    assert tray.make_icon_image(tray.Status.ERROR, 32).getpixel((16, 16))[:3] == (0, 0, 255)
    assert tray.make_icon_image(tray.Status.RUNNING, 32).getpixel((16, 16))[:3] == (0, 255, 0)


def test_an_unreadable_icon_falls_through_to_the_bundled_art(tmp_path, monkeypatch):
    """A corrupt file must not leave the user with no icon at all."""
    monkeypatch.setattr(tray, "config_path", lambda: tmp_path / "config.json")
    (tmp_path / "icon-active.png").write_text("this is not a png")

    image = tray.make_icon_image(tray.Status.RUNNING, 32)
    assert _dominant(image) == "r", "expected the gold artwork"


def test_custom_icons_are_looked_up_beside_the_config(tmp_path, monkeypatch):
    """Not next to the exe: that may be unwritable, and replacing the exe
    with a newer build must not throw the user's icon away."""
    monkeypatch.setattr(tray, "config_path", lambda: tmp_path / "config.json")
    assert tray.custom_icon_path(tray.ACTIVE) is None

    (tmp_path / "icon-active.png").write_bytes(b"placeholder")
    assert tray.custom_icon_path(tray.ACTIVE) == tmp_path / "icon-active.png"


# --- the exe icon -----------------------------------------------------------


def test_the_built_ico_carries_every_size_windows_asks_for():
    """Pillow drops sizes larger than the source without a word.

    The artwork is 144px, so the 256 entry vanished and Explorer's large
    views scaled the 128 instead -- which reads as "the icon didn't
    change".
    """
    import struct
    from pathlib import Path

    ico = Path(tray.__file__).resolve().parent / "assets" / "erdle.ico"
    if not ico.exists():
        pytest.skip("icon not built yet")

    data = ico.read_bytes()
    _, _, count = struct.unpack_from("<HHH", data, 0)
    sizes = set()
    for index in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + index * 16)
        sizes.add((width or 256, height or 256))
    assert (256, 256) in sizes, sorted(sizes)
    assert (16, 16) in sizes, sorted(sizes)


# --- menu toggles -----------------------------------------------------------


class FakeOverlayWindow:
    def __init__(self):
        self.detail = "compact"
        self.enabled = True

    def set_detail(self, detail):
        self.detail = detail

    def set_enabled(self, enabled):
        self.enabled = enabled


class TrayHarness:
    """`tray.main()` run far enough to have a menu, with Tk stubbed out.

    The handlers are closures over `main`'s locals, so the only honest way
    to reach the config and worker they actually mutate is through those
    closures. Doing it once, here, keeps it out of the tests.
    """

    def __init__(self, icon):
        self.icon = icon
        self.items = {
            item.text: item
            for item in icon.menu.items
            if isinstance(getattr(item, "text", None), str)
        }
        self.config = None
        self.worker = None
        for item in icon.menu.items:
            action = getattr(item, "action", None)
            if action is None:
                continue
            for cell in (action.__closure__ or ()):
                try:
                    value = cell.cell_contents
                except ValueError:      # an empty cell
                    continue
                if isinstance(value, Config):
                    self.config = value
                elif isinstance(value, tray.Worker):
                    self.worker = value
        assert self.config is not None and self.worker is not None

    def click(self, label):
        item = self.items[label]
        item.action(self.icon, item)
        return item


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import sys
    import types

    icons = []
    pystray = types.ModuleType("pystray")

    class MenuItem:
        def __init__(self, text, action, enabled=True, checked=None, **kwargs):
            self.text, self.action, self.checked = text, action, checked

    class Menu:
        SEPARATOR = "---"

        def __init__(self, *items):
            self.items = items

    class Icon:
        def __init__(self, name, image, title, menu=None):
            self.name, self.icon, self.title, self.menu = name, image, title, menu
            self.menu_updates = 0
            self.visible = False
            icons.append(self)

        def update_menu(self):
            self.menu_updates += 1

        def stop(self):
            pass

        def run(self, setup=None):
            raise SystemExit(0)     # stop before the message loop

    pystray.MenuItem, pystray.Menu, pystray.Icon = MenuItem, Menu, Icon
    import erdle.config

    monkeypatch.setitem(sys.modules, "pystray", pystray)
    # Both, and for different reasons: tray.config_path places the log,
    # while Config.load resolves the one in erdle.config. Patching only
    # the first sends settings to the real %APPDATA% during tests.
    monkeypatch.setattr(tray, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(
        erdle.config, "config_path", lambda: tmp_path / "config.json"
    )
    monkeypatch.setattr(tray.Worker, "start", lambda self: None)
    monkeypatch.setattr(sys, "argv", ["tray.py"])

    with pytest.raises(SystemExit):
        tray.main()
    return TrayHarness(icons[-1])


@pytest.mark.parametrize(
    "label", ["Screen overlay", "Overlay: full detail", "Start with Windows"]
)
def test_toggles_redraw_the_menu(harness, label):
    """The tick beside a toggle never refreshed, so clicking looked inert.

    pystray caches the Win32 menu and only consults `checked` when the
    menu is rebuilt. The user clicks, nothing changes, so they click again
    -- and the setting ends up back where it started.
    """
    before = harness.icon.menu_updates
    harness.click(label)
    assert harness.icon.menu_updates > before, f"{label} did not redraw the menu"


def test_the_tick_follows_the_setting(harness):
    item = harness.items["Overlay: full detail"]
    assert item.checked(item) is False

    harness.click("Overlay: full detail")
    assert harness.config.overlay_detail == "full"
    assert item.checked(item) is True

    harness.click("Overlay: full detail")
    assert harness.config.overlay_detail == "compact"
    assert item.checked(item) is False


def test_detail_reaches_the_live_window(harness):
    """Not just the config file -- the window on screen has to change."""
    window = FakeOverlayWindow()
    harness.worker.overlay = window

    harness.click("Overlay: full detail")
    assert window.detail == "full"
    harness.click("Overlay: full detail")
    assert window.detail == "compact"


def test_enabling_reaches_the_live_window(harness):
    window = FakeOverlayWindow()
    harness.worker.overlay = window

    harness.click("Screen overlay")
    assert window.enabled is False
    harness.click("Screen overlay")
    assert window.enabled is True


def test_toggling_survives_a_window_that_is_not_there_yet(harness):
    """The worker builds the overlay asynchronously; clicking early is fine."""
    harness.worker.overlay = None
    harness.click("Overlay: full detail")
    harness.click("Screen overlay")
    assert harness.config.overlay_detail == "full"
    assert harness.config.overlay_enabled is False


def test_settings_survive_a_restart(harness, tmp_path):
    harness.click("Overlay: full detail")
    harness.click("Screen overlay")

    reloaded = Config.load(tmp_path / "config.json")
    assert reloaded.overlay_detail == "full"
    assert reloaded.overlay_enabled is False


# --- bundled Tesseract ------------------------------------------------------


def test_the_bundled_binary_wins_over_a_system_install(tmp_path, monkeypatch):
    """A build must behave the same on every machine.

    Falling through to whatever Tesseract the user happens to have makes
    the shipped app's behaviour depend on their PATH.
    """
    import sys

    from erdle.ocr import TesseractRecogniser

    from erdle import ocr

    name = "tesseract.exe" if ocr.IS_WINDOWS else "tesseract"
    fake = tmp_path / "tesseract" / name
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"not really")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("TESSERACT_CMD", "C:\\somewhere\\else\\tesseract.exe")

    assert TesseractRecogniser.bundled_binary() == fake
    assert TesseractRecogniser.locate_binary() == str(fake)


def test_without_a_bundle_the_system_copy_is_used(monkeypatch):
    from erdle.ocr import TesseractRecogniser

    monkeypatch.setattr(TesseractRecogniser, "bundled_binary",
                        classmethod(lambda cls: None))
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    # Whatever this machine has, or None -- either is a valid answer; the
    # point is that it does not raise and does not return the bundle.
    located = TesseractRecogniser.locate_binary()
    assert located is None or "vendor" not in str(located)


def test_tessdata_is_pointed_at_the_bundle(tmp_path, monkeypatch):
    """A bundled binary has no install directory to fall back on.

    Without TESSDATA_PREFIX it starts, fails to load `eng`, and reports
    an error that says nothing about the real cause.
    """
    from erdle.ocr import TesseractRecogniser

    binary = tmp_path / "tesseract.exe"
    binary.write_bytes(b"x")
    (tmp_path / "tessdata").mkdir()
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    TesseractRecogniser.prepare_environment(str(binary))
    assert os.environ["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")


def test_an_existing_tessdata_prefix_is_respected(tmp_path, monkeypatch):
    from erdle.ocr import TesseractRecogniser

    binary = tmp_path / "tesseract.exe"
    binary.write_bytes(b"x")
    (tmp_path / "tessdata").mkdir()
    monkeypatch.setenv("TESSDATA_PREFIX", "/somewhere/chosen")

    TesseractRecogniser.prepare_environment(str(binary))
    assert os.environ["TESSDATA_PREFIX"] == "/somewhere/chosen"


# --- failing loudly ---------------------------------------------------------


def test_no_reader_is_a_red_icon_not_a_gold_one():
    """Detection is name-driven: no reader means nothing is ever detected.

    The old behaviour was to log one line and run happily forever,
    indistinguishable from a working app that has not met a boss yet.
    """
    assert tray.Status.NO_OCR in tray.STATUS_TEXT
    assert tray.ICON_STATE[tray.Status.NO_OCR] == tray.ERROR
    assert "annot read" in tray.STATUS_TEXT[tray.Status.NO_OCR]


def test_the_no_ocr_status_cannot_be_overwritten(tmp_path):
    """Nothing later should be able to paint it gold again."""
    worker = tray.Worker(Config(), tray.Log(tmp_path / "log"))
    worker.can_read_names = False
    worker.set_status(tray.Status.NO_OCR)
    worker.set_status(tray.Status.RUNNING)
    assert worker.status == tray.Status.NO_OCR


def test_a_working_reader_leaves_the_status_alone(tmp_path):
    worker = tray.Worker(Config(), tray.Log(tmp_path / "log"))
    assert worker.can_read_names is True
    worker.set_status(tray.Status.RUNNING)
    assert worker.status == tray.Status.RUNNING


def test_a_thin_atlas_does_not_count_as_a_reader():
    from erdle.glyphs import GlyphAtlas
    from erdle.recognise import AtlasRecogniser

    thin = AtlasRecogniser(atlas=GlyphAtlas())
    assert thin.atlas_is_usable is False

    import string

    full = GlyphAtlas()
    alphabet = string.ascii_letters[:AtlasRecogniser.MIN_USABLE_ALPHABET]
    for index, char in enumerate(alphabet):
        # A distinct signature per character; an identical one would be
        # rejected as a duplicate and the atlas would stay empty.
        signature = tuple((index + position) % 4 for position in range(96))
        full.learn(char, signature, 20)
    assert len(full) == AtlasRecogniser.MIN_USABLE_ALPHABET
    assert AtlasRecogniser(atlas=full).atlas_is_usable is True


# --- version ----------------------------------------------------------------


def test_the_version_reaches_the_log_and_the_tooltip():
    """A pasted log has to identify which build produced it."""
    from pathlib import Path

    from erdle import __version__

    source = Path(tray.__file__).read_text(encoding="utf-8")
    assert "__version__" in source
    assert source.count("__version__") >= 3, "version is not on both surfaces"
    assert __version__


def test_the_no_gg_status_does_not_read_as_a_failure():
    """The overlay is the product; the keyboard panel is the bonus.

    Most users own no SteelSeries hardware, so an amber icon reading
    "SteelSeries GG not running" tells them something is broken when
    nothing is.
    """
    import tray

    label = tray.STATUS_TEXT[tray.Status.NO_GG]
    assert "Overlay only" in label
    for alarming in ("not running", "unavailable", "failed", "error"):
        assert alarming not in label.lower()
