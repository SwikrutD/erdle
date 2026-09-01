#!/usr/bin/env python3
"""Put something on the OLED. No game, no capture, no OCR.

This isolates the single piece that could not be verified without your
hardware: whether GameSense accepts our bitmap payload. If the panel
lights up, the whole output path is proven. If it doesn't, you know the
problem is here and not somewhere in the capture pipeline.

    python smoketest.py            # walk through several test screens
    python smoketest.py --pattern  # just the alignment grid, held
    python smoketest.py --verbose  # print every HTTP payload

Run SteelSeries GG first. Close anything else driving the OLED (GG's own
apps will fight you for the panel).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erdle.bossdb import BossDatabase, default_data_path  # noqa: E402
from erdle.canvas import Canvas, progress_bar  # noqa: E402
from erdle.gamesense import (  # noqa: E402
    GameSenseClient,
    GameSenseError,
    UrllibTransport,
    core_props_path,
    read_core_props,
)
from erdle.render import render_boss_screen, render_idle_screen  # noqa: E402


class LoggingTransport(UrllibTransport):
    """Prints each request before sending, so failures are diagnosable."""

    def post(self, url: str, payload: dict):
        body = json.dumps(payload)
        preview = body if len(body) < 300 else body[:300] + f"... [{len(body)} bytes]"
        print(f"  POST {url}\n       {preview}")
        result = super().post(url, payload)
        print(f"       -> {result!r}")
        return result


def alignment_grid() -> Canvas:
    """Border, centre crosshair and corner ticks.

    Every edge pixel should be visible. If the border is clipped, the panel
    is not 128x40 or the packing order is wrong.
    """
    canvas = Canvas()
    canvas.draw_rect(0, 0, canvas.width, canvas.height)
    canvas.hline(0, canvas.height // 2, canvas.width)
    canvas.fill_rect(canvas.width // 2, 0, 1, canvas.height)
    for x, y in ((0, 0), (canvas.width - 4, 0), (0, canvas.height - 4),
                 (canvas.width - 4, canvas.height - 4)):
        canvas.fill_rect(x, y, 4, 4)
    return canvas


def checkerboard() -> Canvas:
    """Worst case for a 1-bit panel; also proves bit packing is not shifted."""
    canvas = Canvas()
    for y in range(canvas.height):
        for x in range(canvas.width):
            canvas.set(x, y, (x + y) % 2)
    return canvas


def sweep_frames(steps: int = 21):
    """A health bar draining, to confirm updates actually reach the panel."""
    database = BossDatabase.load(default_data_path())
    entry = database.require("malenia")
    for i in range(steps):
        yield render_boss_screen(entry, fill_ratio=1.0 - i / (steps - 1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", action="store_true",
                        help="show only the alignment grid, and hold it")
    parser.add_argument("--verbose", action="store_true",
                        help="print every HTTP request and response")
    parser.add_argument("--hold", type=float, default=2.5,
                        help="seconds to display each screen (default: 2.5)")
    args = parser.parse_args()

    print(f"looking for coreProps.json at:\n  {core_props_path()}")
    try:
        core = read_core_props()
    except GameSenseError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("\nStart SteelSeries GG and try again. If GG is running and this "
              "still fails, the install path differs -- pass the real path to "
              "read_core_props().", file=sys.stderr)
        return 1
    print(f"  found GG at {core.base_url}\n")

    transport = LoggingTransport() if args.verbose else UrllibTransport()
    client = GameSenseClient(core, transport=transport)

    try:
        print("registering game...")
        client.register()
        print("  registered\n")
    except GameSenseError as exc:
        print(f"FAILED during registration: {exc}", file=sys.stderr)
        return 1

    if args.pattern:
        screens: list[tuple[str, Canvas]] = [
            ("alignment grid (held)", alignment_grid()),
        ]
    else:
        screens = [
            ("alignment grid -- all four edges should be visible", alignment_grid()),
            ("checkerboard -- should be evenly dithered, not striped", checkerboard()),
            ("solid fill -- whole panel lit", _solid()),
            ("blank -- whole panel dark", Canvas()),
            ("text -- idle screen", render_idle_screen("ERDLE")),
        ]

    try:
        for label, canvas in screens:
            print(f"showing: {label}")
            client.send_bitmap(canvas.pack())
            client.heartbeat(force=True)
            time.sleep(args.hold)

        if args.pattern:
            print("\nholding. ctrl-c to stop.")
            while True:
                client.heartbeat()
                time.sleep(1.0)

        print("\nshowing: draining health bar (Malenia)")
        for canvas in sweep_frames():
            client.send_bitmap(canvas.pack())
            time.sleep(0.08)
        client.heartbeat(force=True)
        time.sleep(args.hold)

    except GameSenseError as exc:
        print(f"\nFAILED while sending: {exc}", file=sys.stderr)
        print("\nRegistration worked but frames did not. The payload shape in "
              "gamesense.send_bitmap is the thing to question -- rerun with "
              "--verbose.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        try:
            client.remove_game()
            print("\nunregistered")
        except GameSenseError:
            pass

    print(
        "\nIf every screen appeared, the output path works and you can move on\n"
        "to calibration. If the panel never changed, see the notes at the\n"
        "bottom of smoketest.py."
    )
    return 0


def _solid() -> Canvas:
    canvas = Canvas()
    canvas.fill_rect(0, 0, canvas.width, canvas.height)
    return canvas


if __name__ == "__main__":
    raise SystemExit(main())


# If the panel never changes
# --------------------------
# Registration succeeding while frames do nothing points at the event
# payload, which is the part written from documentation rather than
# verified against hardware. Things to try, in order:
#
# 1. Rerun with --verbose and check GG returns 200 with no error body.
#
# 2. The screen handler may want the bitmap in the *binding* rather than
#    the event. Try re-binding per frame: move the packed bytes into
#    handlers[0].datas[0]["image-data"] in bind_screen_event and call it
#    each time instead of send_bitmap. Slower, but it is the older and
#    more widely-used route.
#
# 3. Device type. We declare "screened-128x40". Some firmware revisions
#    report "screened" or "rgb-per-key-zones" for the same panel. GG's
#    own logs list what it thinks is attached.
#
# 4. Another app may own the OLED. Quit GG's built-in apps and any other
#    OLED tool (GGSystemMonitor, gamesense-essentials) and retry.
