#!/usr/bin/env python3
"""Live entrypoint. Windows, with Elden Ring in borderless windowed.

    python run.py                 # normal operation
    python run.py --dry-run       # detect and print, send nothing to GG
    python run.py --no-ocr        # health mirror only, no name recognition

Reads the framebuffer and posts to GG's local HTTP server. Never opens a
handle to eldenring.exe.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erdle.app import AppConfig, ErdleApp  # noqa: E402
from erdle.autocal import strip_regions  # noqa: E402
from erdle.bossdb import BossDatabase, default_data_path  # noqa: E402
from erdle.config import Config  # noqa: E402
from erdle.overlay import OverlayDriver, build_content  # noqa: E402
from erdle.overlay_ui import build_overlay  # noqa: E402
from erdle.gamesense import (  # noqa: E402
    CoreProps,
    GameSenseClient,
    GameSenseError,
    RecordingTransport,
)
from erdle.ocr import NullRecogniser, TesseractRecogniser  # noqa: E402
from erdle.recognise import build_recogniser  # noqa: E402
from erdle.state import EventKind, FightState  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print events instead of driving the OLED")
    parser.add_argument("--no-ocr", action="store_true",
                        help="skip name recognition; health mirror only")
    parser.add_argument("--fps", type=float, default=15.0,
                        help="capture rate (default: 15)")
    parser.add_argument("--monitor", type=int, default=1,
                        help="mss monitor index (default: 1)")
    parser.add_argument("--data", type=Path, default=None,
                        help="path to bosses.json")
    parser.add_argument("--ocr-threshold", type=int, default=200,
                        help="brightness cutoff separating the name text "
                             "from the terrain behind it (default: 200). "
                             "Run diagnose.py to find the right value.")
    parser.add_argument(
        "--no-overlay", action="store_true",
        help="do not draw the on-screen overlay",
    )
    parser.add_argument(
        "--overlay-test", metavar="BOSS", nargs="?", const="margit",
        help="draw one boss on the overlay and wait, without the game",
    )
    parser.add_argument(
        "--overlay-detail", choices=("compact", "full"), default=None,
        help="override the saved detail level, for --overlay-test",
    )
    parser.add_argument(
        "--dump-plate", type=Path, default=None, metavar="DIR",
        help="save the name band whenever the atlas refuses to learn "
             "from it, so the segmentation can be looked at",
    )
    parser.add_argument("--verbose", action="store_true",
                        help="print every name poll: ink level, raw OCR text "
                             "and what it matched")
    parser.add_argument("--bar-driven", action="store_true",
                        help="use the older bar-driven detector instead of "
                             "detecting fights from the boss name")
    parser.add_argument("--poll", type=float, default=0.7,
                        help="seconds between name OCR polls (default: 0.7)")
    parser.add_argument("--banner-every", type=int, default=3,
                        help="check for YOU DIED / FELLED every Nth frame "
                             "(default: 3, so 5Hz at 15fps)")
    return parser.parse_args()



def overlay_test(settings, database, wanted: str) -> int:
    """Draw one boss and wait, so the overlay can be checked without a fight.

    "The overlay does not show up" has two very different causes -- the
    window never opens, or it opens and detection never names a boss --
    and they need opposite fixes. This separates them in ten seconds
    rather than a boss run.
    """
    import time

    want = wanted.strip().lower()
    entry = next(
        (e for e in database
         if e.key == want or e.name.lower() == want),
        None,
    ) or next(
        (e for e in database if want in e.name.lower()), None
    )
    if entry is None:
        entry = next(iter(database))
        print(f"no boss matching {wanted!r}; using {entry.name}")

    overlay = build_overlay(
        settings, on_move=lambda x, y: settings.move_overlay(x, y)
    )
    if not getattr(overlay, "available", False):
        print("overlay: off --",
              getattr(overlay, "unavailable_reason", "unknown"))
        return 1

    print(f"overlay: on -- drawing {entry.name} for 20s")
    print(f"  enabled : {settings.overlay_enabled}")
    print(f"  detail  : {settings.overlay_detail}")
    print(f"  screen  : {getattr(overlay, 'screen', None)}")
    print(f"  dpi     : {getattr(overlay, 'dpi_mode', '?')}"
          f" (scale {getattr(overlay, 'display_scale', 1.0)})")
    overlay.show(build_content(entry))
    time.sleep(1.0)
    print(f"  placed  : {getattr(overlay, 'last_placement', None)}")
    print("  drag it if you like; the move is saved on release")
    try:
        time.sleep(19.0)
    except KeyboardInterrupt:
        pass
    overlay.stop()
    settings.save()
    return 0



def _plate_dumper(directory):
    """Save each refused name band once, as a PNG beside a report.

    "segmented 35 glyphs, expected 20" says the split went wrong but not
    how. The picture says whether the band caught something besides the
    name, or whether single letters are being cut into pieces.
    """
    from PIL import Image

    seen = set()

    def dump(frame, name: str, boxes) -> None:
        if name in seen:
            return
        seen.add(name)
        safe = "".join(c if c.isalnum() else "-" for c in name)[:60]
        target = directory / f"{safe}.png"
        image = Image.new("RGB", (frame.width, frame.height))
        image.putdata([frame.pixel(x, y)
                       for y in range(frame.height)
                       for x in range(frame.width)])
        image.save(target)

        widths = sorted(b.width for b in boxes)
        report = directory / f"{safe}.txt"
        report.write_text(
            f"{name}\n"
            f"band       : {frame.width}x{frame.height}\n"
            f"segmented  : {len(boxes)} boxes\n"
            f"expected   : {len([c for c in name if not c.isspace()])}\n"
            f"box widths : {widths}\n"
            f"box spans  : {[(b.left, b.right) for b in boxes]}\n",
            encoding="utf-8",
        )
        print(f"  wrote {target.name} and {report.name}")

    return dump


def main() -> int:
    args = parse_args()

    database = BossDatabase.load(args.data or default_data_path())
    print(f"loaded {len(database)} bosses (source: {database.meta.get('source')})")

    if args.overlay_test:
        # Ahead of capture, OCR and GameSense on purpose: none of them
        # are needed to answer "does the window open at all".
        settings = Config.load()
        if args.overlay_detail:
            # Tweaking the layout means looking at both views; making that
            # a config edit each time is friction for no reason.
            settings.overlay_detail = args.overlay_detail
        return overlay_test(settings, database, args.overlay_test)

    if args.no_ocr:
        recogniser = NullRecogniser()
        print("name recognition disabled (--no-ocr); health mirror only")
    else:
        # The atlas reads the font directly. Tesseract, if present, is only
        # a tutor: it labels plates the atlas cannot yet read, and the atlas
        # learns from them until it no longer needs asking.
        ok, reason = TesseractRecogniser.availability()
        tutor = TesseractRecogniser(threshold=args.ocr_threshold) if ok else None
        recogniser = build_recogniser(fallback=tutor)
        print(recogniser.summary())
        if ok:
            print(reason)
        else:
            # Degrade rather than crash: the health mirror is still useful
            # without names, and failing here would be several minutes into
            # a fight rather than at startup.
            print(f"no OCR tutor: {reason}")
            if len(recogniser.atlas) == 0:
                print("and the glyph atlas is empty -- names will not resolve")
                print("until Tesseract labels a few, or an atlas is supplied\n")

    try:
        from erdle.sources import MSSSource
        source = MSSSource(args.monitor)
    except RuntimeError as exc:
        print(f"capture unavailable: {exc}", file=sys.stderr)
        return 1

    live = not args.dry_run
    if live:
        try:
            client = GameSenseClient.discover()
            client.register()
            print("registered with SteelSeries GG")
        except GameSenseError as exc:
            print(f"GameSense unavailable: {exc}", file=sys.stderr)
            print("falling back to dry-run", file=sys.stderr)
            live = False
    if not live:
        client = GameSenseClient(CoreProps("dry-run:0"), transport=RecordingTransport())

    def on_screen(canvas) -> None:
        try:
            client.send_bitmap(canvas.pack())
        except GameSenseError as exc:
            print(f"send failed: {exc}", file=sys.stderr)

    # Use whatever the tray app calibrated, if anything. Keeps the two
    # entrypoints from disagreeing about where the HUD is.
    settings = Config.load()
    if settings.calibrated:
        print(f"using saved calibration ({settings.calibrated_for})")
    bar_region, name_region, band_region = strip_regions(settings)

    # Capture only the HUD band, not the whole screen -- see sources.py.
    app = ErdleApp(
        database,
        recogniser,
        config=AppConfig.for_hud_strip(
            bar_region=bar_region,
            name_region=name_region,
            name_band=band_region,
            name_driven=not args.bar_driven,
            name_poll_interval=args.poll,
        ),
        on_screen=on_screen,
        on_poll=describe_poll if args.verbose else None,
    )

    if args.dump_plate:
        # Wired here rather than inside `app` so the capture path stays
        # free of debug branches. It writes at most one file per boss:
        # a refusal repeats fifteen times a second and would otherwise
        # fill a directory during a single fight.
        args.dump_plate.mkdir(parents=True, exist_ok=True)
        app.on_refusal = _plate_dumper(args.dump_plate)

    if args.no_overlay:
        settings.overlay_enabled = False
    overlay = build_overlay(settings, on_move=lambda x, y: settings.move_overlay(x, y))
    driver = OverlayDriver(overlay)
    if getattr(overlay, "available", False):
        print("overlay: on")
    else:
        # Saying only "off" turns four different causes into one
        # unactionable word.
        print("overlay: off --",
              getattr(overlay, "unavailable_reason", "unknown"))

    interval = 1.0 / max(args.fps, 1.0)
    mode = "bar-driven" if args.bar_driven else f"name-driven, polling every {args.poll:g}s"
    print(f"watching at {args.fps:g} fps ({mode}) -- ctrl-c to stop")

    frame_index = 0
    try:
        while True:
            started = time.monotonic()
            frame = source.grab_hud_strip(settings.hud_strip)

            # The centre of the screen costs far more to convert than the
            # HUD strip, and a death banner lingers for seconds, so there
            # is no reason to look every frame.
            banner_frame = None
            if args.banner_every > 0 and frame_index % args.banner_every == 0:
                banner_frame = source.grab_banner()
            frame_index += 1

            for event in app.step(frame, started, banner_frame):
                describe(event)

            snapshot = app.tracker.snapshot
            driver.update(
                fighting=snapshot.state is FightState.FIGHTING,
                boss=snapshot.boss,
            )
            try:
                client.heartbeat()
            except GameSenseError:
                pass
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        driver.close()
        source.close()
        if hasattr(recogniser, "flush"):
            if recogniser.flush():
                print(f"learned glyphs saved ({recogniser.summary()})")
            elif getattr(app, "glyphs_learned", 0) == 0:
                # A flat counter after a fight is the confusing case: the
                # boss was named, so it looks like learning should have
                # happened. Usually the plate segmented into the wrong
                # number of glyphs and was refused whole.
                from erdle.glyphs import learn_from_text
                why = getattr(learn_from_text, "last_refusal", None)
                print("no new glyphs learned"
                      + (f" -- {why}" if why else ""))
        if live:
            try:
                client.remove_game()
            except GameSenseError:
                pass
    return 0


def describe_poll(ink, text, match) -> None:
    """One line per OCR poll, so a bad stage is obvious."""
    verdict = f"{match.display_name} ({match.confidence:.0%})" if match else "no match"
    print(f"    poll: ink={ink:.4f}  read={text[:40]!r}  -> {verdict}")


def describe(event) -> None:
    name = event.boss.name if event.boss else "unknown boss"

    if event.kind is EventKind.FIGHT_STARTED:
        print("  fight started")
    elif event.kind is EventKind.BOSS_IDENTIFIED:
        print(f"  identified: {name} ({event.confidence:.0%})")
    elif event.kind is EventKind.BOSS_CHANGED:
        print(f"  phase change: {event.previous_boss.name} -> {name}")
    elif event.kind is EventKind.DIED:
        print(f"  died to {name}  ->  GIT GUD TARNISHED")
    elif event.kind is EventKind.VICTORY:
        print(f"  felled {name}  ->  GOOD JOB TARNISHED")
    elif event.kind is EventKind.FIGHT_ENDED:
        print(f"  fight ended: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
