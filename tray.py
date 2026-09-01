#!/usr/bin/env python3
"""ERDLE as a system tray application. No console window.

This is the shipped entrypoint. `run.py` still exists for development,
where seeing the event stream matters.

    pythonw tray.py          # no console
    python tray.py --debug   # console, verbose

Design notes for anyone reading this before touching it:

* The capture loop runs on a worker thread. pystray must own the main
  thread on Windows or the icon never appears.
* Nothing here raises into the user's face. There is no console to print
  to, so failures become a red icon and a tooltip.
* Regions calibrate themselves on first run -- see erdle.autocal. The
  shipped defaults were measured on one 4K display and would be wrong on
  an ultrawide.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erdle import __version__  # noqa: E402
from erdle.app import AppConfig, ErdleApp  # noqa: E402
from erdle.autocal import AutoCalibrator, strip_regions  # noqa: E402
from erdle.bossdb import BossDatabase, default_data_path  # noqa: E402
from erdle.config import Config, config_path  # noqa: E402
from erdle.gamesense import (  # noqa: E402
    CoreProps,
    GameSenseClient,
    GameSenseError,
    RecordingTransport,
)
from erdle.ocr import TesseractRecogniser  # noqa: E402
from erdle.overlay import OverlayDriver  # noqa: E402
from erdle.overlay_ui import NullOverlay, build_overlay  # noqa: E402
from erdle.recognise import build_recogniser  # noqa: E402
from erdle.state import EventKind, FightState  # noqa: E402

APP_NAME = "ERDLE"


class Status:
    STARTING = "starting"
    RUNNING = "running"
    CALIBRATING = "calibrating"
    NO_GG = "no_gg"
    NO_OCR = "no_ocr"
    ERROR = "error"
    STOPPED = "stopped"


STATUS_TEXT = {
    Status.STARTING: "Starting…",
    Status.RUNNING: "Watching for boss fights",
    Status.CALIBRATING: "Looking for the boss health bar…",
    # Not an error, and it must not read like one. Most users have no
    # SteelSeries hardware at all -- the overlay is the product and the
    # keyboard panel is the bonus -- so this states what is running
    # rather than what is missing.
    Status.NO_GG: "Overlay only — no SteelSeries OLED detected",
    Status.NO_OCR: "Cannot read boss names — see Show log",
    Status.ERROR: "Stopped — see Show log",
    Status.STOPPED: "Stopped",
}

STATUS_COLOUR = {
    Status.STARTING: (110, 110, 120),
    Status.RUNNING: (196, 158, 74),      # Elden Ring gold
    Status.CALIBRATING: (90, 140, 200),
    Status.NO_GG: (190, 140, 60),
    Status.NO_OCR: (180, 60, 60),
    Status.ERROR: (180, 60, 60),
    Status.STOPPED: (90, 90, 90),
}


def resource_path(*parts: str) -> Path:
    """Locate bundled data, whether frozen by PyInstaller or not."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


# Artwork states, named for their files: assets/tray-<state>.png.
ACTIVE = "active"            # gold -- watching for fights
IDLE = "idle"                # pale -- not watching
CALIBRATING = "calibrating"  # blue -- hunting for the boss bar
AMBER = "amber"              # orange -- running, but GG is not there
ERROR = "error"              # red -- stopped, something went wrong

#: Which artwork each status uses. Colour is doing real work here: the
#: whole point of a tray icon is that a glance tells you the state without
#: opening a menu, and "gold or not gold" is the first thing you read.
ICON_STATE = {
    Status.RUNNING: ACTIVE,
    Status.CALIBRATING: CALIBRATING,
    Status.NO_GG: AMBER,
    Status.ERROR: ERROR,
    Status.NO_OCR: ERROR,
    Status.STARTING: IDLE,
    Status.STOPPED: IDLE,
}

#: What to show when a state's artwork is missing. A partial icon set --
#: someone supplying only `icon-active.png` -- should degrade to the pale
#: mark rather than to a drawn rune that matches nothing else on screen.
ICON_FALLBACK = {
    CALIBRATING: ACTIVE,
    AMBER: ACTIVE,
    ERROR: IDLE,
    ACTIVE: IDLE,
}

ICON_STATES = (ACTIVE, IDLE, CALIBRATING, AMBER, ERROR)


def custom_icon_path(state: str) -> Path | None:
    """A user-supplied tray icon for one state, if they dropped one in.

    Looked up in the config directory rather than next to the exe: a
    frozen build may live somewhere unwritable, and the file should
    survive replacing ERDLE.exe with a newer one.

    A plain `icon.png` is honoured for both states, so someone who only
    wants one mark does not have to supply the same file twice.
    """
    folder = config_path().parent
    for name in (f"icon-{state}.png", f"icon-{state}.ico", "icon.png", "icon.ico"):
        candidate = folder / name
        if candidate.exists():
            return candidate
    return None


def bundled_icon_path(state: str) -> Path | None:
    """The artwork shipped inside the build."""
    candidate = resource_path("assets", f"tray-{state}.png")
    return candidate if candidate.exists() else None


def load_icon_image(state: str, size: int):
    """Artwork for one state, or None when there is none to be had.

    Walks the fallback chain, so a missing or corrupt file for one state
    borrows a neighbour's rather than dropping straight to the drawing.
    """
    from PIL import Image

    seen: set[str] = set()
    while state is not None and state not in seen:
        seen.add(state)
        for source in (custom_icon_path(state), bundled_icon_path(state)):
            if source is None:
                continue
            try:
                image = Image.open(source).convert("RGBA")
                return image.resize((size, size), Image.LANCZOS)
            except Exception:
                continue  # unreadable: try the next candidate
        state = ICON_FALLBACK.get(state)
    return None


def make_icon_image(status: str, size: int = 64):
    """The tray mark for a status.

    Prefers real artwork -- the user's own, else the bundled pair. Falls
    back to drawing a rune in the status colour when neither is readable,
    which keeps the old six-way colour coding rather than leaving someone
    with no icon at all.
    """
    from PIL import Image, ImageDraw

    artwork = load_icon_image(ICON_STATE.get(status, IDLE), size)
    if artwork is not None:
        return artwork

    colour = STATUS_COLOUR.get(status, (120, 120, 120))
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = 4
    draw.ellipse([pad, pad, size - pad, size - pad], fill=(24, 22, 20, 255))
    draw.ellipse([pad, pad, size - pad, size - pad], outline=colour + (255,), width=4)
    mid = size // 2
    draw.line([mid, pad + 10, mid, size - pad - 10], fill=colour + (255,), width=4)
    draw.line([pad + 12, mid, size - pad - 12, mid], fill=colour + (255,), width=4)
    return image


class Worker(threading.Thread):
    """Capture loop. Owns everything that can fail."""

    def __init__(self, config: Config, log: "Log", debug: bool = False) -> None:
        super().__init__(daemon=True, name="erdle-worker")
        self.config = config
        self.log = log
        self.debug = debug
        self.status = Status.STARTING
        # NOT `self._stop`. threading.Thread already has a private
        # method by that name, and shadowing it with an Event breaks
        # Thread.join() and Thread.is_alive(): both call self._stop()
        # internally once the thread has finished, and an Event is not
        # callable. It only blows up at shutdown, which is precisely when
        # the tray needs those two calls to work.
        self._stop_event = threading.Event()
        self._status_changed = threading.Event()
        self.overlay_driver = None
        self.overlay = None
        self._logged_placement = False
        self.can_read_names = True

    def request_stop(self) -> None:
        self._stop_event.set()

    def set_status(self, status: str) -> None:
        # NO_OCR is terminal for as long as it is true: the app is running
        # and will never detect anything, and a gold icon would say the
        # opposite.
        if self.status == Status.NO_OCR and not self.can_read_names:
            return
        if status != self.status:
            self.status = status
            self._status_changed.set()

    def take_status_change(self) -> bool:
        if self._status_changed.is_set():
            self._status_changed.clear()
            return True
        return False

    # --- the loop ---------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except Exception:
            self.log.write("worker crashed:\n" + traceback.format_exc())
            self.set_status(Status.ERROR)

    def _run(self) -> None:
        database = BossDatabase.load(default_data_path())
        self.log.write(f"loaded {len(database)} bosses")

        # Atlas first, Tesseract only as a tutor for glyphs it has not
        # seen. Over a few fights the atlas takes over entirely.
        ok, reason = TesseractRecogniser.availability()
        tutor = TesseractRecogniser(threshold=200) if ok else None
        recogniser = build_recogniser(fallback=tutor)
        self.log.write(recogniser.summary())
        self.log.write(reason if ok else f"no OCR: {reason}")

        # Detection is name-driven. With no reader and an atlas too thin
        # to carry the alphabet on its own, nothing will ever be detected
        # -- and the old behaviour was to run happily forever, looking
        # exactly like a working app that never sees a boss.
        self.can_read_names = ok or recogniser.atlas_is_usable
        if not self.can_read_names:
            self.log.write(
                "CANNOT READ BOSS NAMES. Tesseract is missing and the "
                "bundled glyph atlas is too small to read on its own.\n"
                "  This build should have shipped Tesseract inside it; if "
                "you are running from source, install it with\n"
                "    winget install UB-Mannheim.TesseractOCR"
            )
            self.set_status(Status.NO_OCR)

        try:
            from erdle.sources import MSSSource
            source = MSSSource(self.config.monitor)
        except RuntimeError as exc:
            self.log.write(f"capture unavailable: {exc}")
            self.set_status(Status.ERROR)
            return

        client = self._connect()
        app = self._build_app(database, recogniser, client)

        overlay = self._build_overlay()
        self.overlay = overlay
        driver = OverlayDriver(overlay)
        self.overlay_driver = driver

        calibrator = AutoCalibrator()
        if not self.config.calibrated:
            self.set_status(Status.CALIBRATING)
            self.log.write("no saved calibration; will search for the bar")
        else:
            self.set_status(Status.RUNNING)
            self.log.write(f"using saved regions ({self.config.calibrated_for})")

        interval = 1.0 / max(self.config.fps, 1.0)
        frame_index = 0

        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                frame = source.grab_hud_strip(self.config.hud_strip)
                banner_frame = None
                if frame_index % 3 == 0:
                    banner_frame = source.grab_banner()
                frame_index += 1

                for event in app.step(frame, started, banner_frame):
                    self._describe(event)

                snapshot = app.tracker.snapshot
                driver.update(
                    fighting=snapshot.state is FightState.FIGHTING,
                    boss=snapshot.boss,
                )
                if driver.showing is not None:
                    self._log_placement(overlay)

                if calibrator.should_attempt(
                    started, already_calibrated=self.config.calibrated
                ):
                    self._try_calibrate(source, calibrator, started, app)

                client.heartbeat()
            except GameSenseError as exc:
                self.log.write(f"GameSense: {exc}")
                self.set_status(Status.NO_GG)
                client = self._connect()
                app = self._build_app(database, recogniser, client)
            except Exception:
                self.log.write("loop error:\n" + traceback.format_exc())

            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.0, interval - elapsed))

        try:
            driver.close()
        except Exception:
            pass
        try:
            if hasattr(recogniser, "flush"):
                recogniser.flush()
            source.close()
            client.remove_game()
        except Exception:
            pass
        self.set_status(Status.STOPPED)

    # --- helpers ----------------------------------------------------------

    def _build_overlay(self):
        """The screen overlay, or a no-op stand-in.

        Wrapped because the overlay is a convenience and the keyboard panel
        is the product: no failure here may take the app down, and there is
        no console to report one to.
        """
        try:
            overlay = build_overlay(self.config, on_move=self._save_overlay_position)
        except Exception:
            self.log.write("overlay unavailable:\n" + traceback.format_exc())
            return NullOverlay()
        if not self.config.overlay_enabled:
            self.log.write("overlay disabled in settings")
        elif not getattr(overlay, "available", False):
            self.log.write("overlay unavailable (no Tk on this machine)")
        else:
            self.log.write(
                f"overlay ready: dpi={getattr(overlay, 'dpi_mode', '?')} "
                f"screen={getattr(overlay, 'screen', None)} "
                f"saved={self.config.overlay_fx},{self.config.overlay_fy}"
            )
        return overlay

    def _log_placement(self, overlay) -> None:
        """Report where the window actually landed, once."""
        placement = getattr(overlay, "last_placement", None)
        if placement is None or self._logged_placement:
            return
        self._logged_placement = True
        want_x, want_y, got_x, got_y, outcome = placement
        self.log.write(
            f"overlay placed: wanted {want_x},{want_y} "
            f"got {got_x},{got_y} ({outcome})"
        )

    def _save_overlay_position(self, fx: float, fy: float) -> None:
        self.config.move_overlay(fx, fy)
        try:
            self.config.save()
        except OSError:
            pass

    def _connect(self):
        try:
            client = GameSenseClient.discover()
            client.register()
            self.log.write("registered with SteelSeries GG")
            self.set_status(
                Status.RUNNING if self.config.calibrated else Status.CALIBRATING
            )
            return client
        except GameSenseError as exc:
            self.log.write(
                f"no SteelSeries OLED: {exc} -- overlay only, which is fine"
            )
            self.set_status(Status.NO_GG)
            return GameSenseClient(
                CoreProps("offline:0"), transport=RecordingTransport()
            )

    def _build_app(self, database, recogniser, client) -> ErdleApp:
        bar, name, band = strip_regions(self.config)
        return ErdleApp(
            database,
            recogniser,
            config=AppConfig.for_hud_strip(
                bar_region=bar, name_region=name, name_band=band,
                name_driven=True,
            ),
            on_screen=lambda canvas: self._send(client, canvas),
        )

    def _send(self, client, canvas) -> None:
        try:
            client.send_bitmap(canvas.pack())
        except GameSenseError as exc:
            self.log.write(f"send failed: {exc}")

    def _try_calibrate(self, source, calibrator, now, app) -> None:
        """One full-screen sweep for the boss bar."""
        try:
            full = source.grab()
        except Exception:
            return
        if not calibrator.attempt(full, self.config, now):
            return

        try:
            self.config.save()
        except OSError as exc:
            self.log.write(f"could not save config: {exc}")
        self.log.write(
            f"calibrated for {self.config.calibrated_for}: "
            f"bar {self.config.boss_bar}"
        )
        bar, name, band = strip_regions(self.config)
        app.config.bar_region = bar
        app.config.name_region = name
        self.set_status(Status.RUNNING)

    def _describe(self, event) -> None:
        name = event.boss.name if event.boss else "unknown boss"
        if event.kind is EventKind.BOSS_IDENTIFIED:
            self.log.write(f"identified: {name} ({event.confidence:.0%})")
        elif event.kind is EventKind.DIED:
            self.log.write(f"died to {name}")
        elif event.kind is EventKind.VICTORY:
            self.log.write(f"felled {name}")


class Log:
    """A small ring buffer written to disk. There is no console."""

    def __init__(self, path: Path, limit: int = 400, echo: bool = False) -> None:
        self.path = path
        self.limit = limit
        self.echo = echo
        self._lines: queue.deque = __import__("collections").deque(maxlen=limit)
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self._lock:
            self._lines.append(line)
            if self.echo:
                print(line, flush=True)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="echo the log")
    args = parser.parse_args()

    try:
        import pystray
    except ImportError:
        print("tray.py needs `pip install pystray pillow`", file=sys.stderr)
        return 1

    config = Config.load()
    log = Log(config_path().parent / "erdle.log", echo=args.debug)
    log.write(f"{APP_NAME} {__version__} starting")

    worker = Worker(config, log, debug=args.debug)
    worker.start()

    def refresh_menu(icon) -> None:
        """Redraw the menu after a toggle.

        pystray builds the Win32 menu once and caches it, so a `checked`
        callable is only consulted when the menu is rebuilt. Without this
        a toggle changes the setting but not the tick beside it -- the
        click looks ignored, so the user clicks again and puts the setting
        back where it started.
        """
        try:
            icon.update_menu()
        except Exception:
            pass

    def on_open_log(icon, item):
        import subprocess
        try:
            if sys.platform == "win32":
                subprocess.Popen(["notepad.exe", str(log.path)])
            else:
                subprocess.Popen(["xdg-open", str(log.path)])
        except OSError:
            pass

    def on_recalibrate(icon, item):
        config.reset_regions()
        try:
            config.save()
        except OSError:
            pass
        log.write("calibration cleared; will search again")
        worker.set_status(Status.CALIBRATING)
        refresh_menu(icon)

    def on_overlay(icon, item):
        """Show or hide the on-screen overlay, immediately.

        The window is built at startup whether or not the overlay is
        switched on, precisely so this can take effect without a restart.
        """
        config.overlay_enabled = not config.overlay_enabled
        try:
            config.save()
        except OSError:
            pass
        if worker.overlay is not None:
            try:
                worker.overlay.set_enabled(config.overlay_enabled)
            except Exception:
                pass
        refresh_menu(icon)
        log.write(
            f"overlay {'on' if config.overlay_enabled else 'off'}"
        )

    def on_detail(icon, item):
        """Switch between the compact and full overlay, immediately."""
        config.overlay_detail = (
            "full" if config.overlay_detail == "compact" else "compact"
        )
        try:
            config.save()
        except OSError:
            pass
        if worker.overlay is not None:
            try:
                worker.overlay.set_detail(config.overlay_detail)
            except Exception:
                pass
        refresh_menu(icon)
        log.write(f"overlay detail: {config.overlay_detail}")

    def on_autostart(icon, item):
        from erdle.autostart import set_autostart

        config.autostart = not config.autostart
        ok, message = set_autostart(config.autostart)
        log.write(message)
        if not ok:
            config.autostart = not config.autostart
        try:
            config.save()
        except OSError:
            pass
        refresh_menu(icon)

    quitting = threading.Event()

    def on_quit(icon, item):
        """Quit without blocking the tray's message loop.

        Three things went wrong with the obvious version:

        * ``worker.join()`` ran on the thread pumping window messages, so
          for as long as it blocked the tray could not repaint or respond,
          and the click looked ignored.
        * pystray's ``setup`` thread is not a daemon, so the interpreter
          waits for it. Anything that keeps it alive keeps the process
          alive with no window to close.
        * Windows leaves a ghost icon behind unless the notification area
          entry is removed before the process goes away. ``visible = False``
          is what actually removes it.
        """
        if quitting.is_set():
            return
        quitting.set()
        log.write("quitting")
        worker.request_stop()

        def finish():
            worker.join(timeout=4.0)
            try:
                icon.visible = False
            except Exception:
                pass
            try:
                icon.stop()
            except Exception:
                pass
            # Last resort. A stuck capture or HTTP read must not strand a
            # process the user has no way left to close.
            time.sleep(1.0)
            os._exit(0)

        threading.Thread(target=finish, name="erdle-quit", daemon=True).start()

    icon = pystray.Icon(
        APP_NAME,
        make_icon_image(Status.STARTING),
        f"{APP_NAME} {__version__} — starting",
        menu=pystray.Menu(
            pystray.MenuItem(
                lambda item: STATUS_TEXT.get(worker.status, ""), None, enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Screen overlay",
                on_overlay,
                checked=lambda item: config.overlay_enabled,
            ),
            pystray.MenuItem(
                "Overlay: full detail",
                on_detail,
                checked=lambda item: config.overlay_detail == "full",
            ),
            pystray.MenuItem("Recalibrate", on_recalibrate),
            pystray.MenuItem(
                "Start with Windows",
                on_autostart,
                checked=lambda item: config.autostart,
            ),
            pystray.MenuItem("Show log", on_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    def refresh(icon_obj):
        icon_obj.visible = True
        while worker.is_alive() and not quitting.is_set():
            if worker.take_status_change():
                icon_obj.icon = make_icon_image(worker.status)
                icon_obj.title = (
                    f"{APP_NAME} {__version__} — "
                    f"{STATUS_TEXT.get(worker.status, '')}"
                )
                # The status line in the menu is a callable, but pystray
                # builds the Win32 menu once and caches it. Without this
                # the menu keeps reporting "Starting..." forever, which
                # makes a working app look hung.
                try:
                    icon_obj.update_menu()
                except Exception:
                    pass
            time.sleep(0.4)

    icon.run(setup=refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
