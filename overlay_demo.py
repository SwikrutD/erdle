#!/usr/bin/env python3
"""Show the overlay on its own, without needing a boss fight.

    python overlay_demo.py                 # a boss with plenty to show
    python overlay_demo.py malenia         # a specific one
    python overlay_demo.py --list          # what you can ask for
    python overlay_demo.py --seconds 60    # leave it up longer

Use this to check the overlay renders on your machine, and to drag it
where you want it -- the position is saved, so ERDLE will put it there
during a real fight. Finding a boss just to reposition a window is a poor
way to spend an evening.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erdle.bossdb import BossDatabase, default_data_path  # noqa: E402
from erdle.config import Config  # noqa: E402
from erdle.overlay import build_content  # noqa: E402
from erdle.overlay_ui import build_overlay  # noqa: E402

# Immune to all four statuses and a clear best damage type, so every part
# of the layout has something in it.
DEFAULT_BOSS = "erdtree_burial_watchdog"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boss", nargs="?", default=DEFAULT_BOSS)
    parser.add_argument("--list", action="store_true", help="list boss keys")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--cycle", action="store_true",
                        help="rotate through several bosses")
    parser.add_argument("--full", action="store_true",
                        help="show every damage type and status, not just "
                             "the ones worth acting on")
    parser.add_argument("--at", metavar="FX,FY",
                        help="place it at a fraction of the screen instead "
                             "of dragging, e.g. 1,0.05 for the top-right")
    parser.add_argument("--reset-position", action="store_true",
                        help="forget the saved position and use the default "
                             "top-right corner")
    args = parser.parse_args()

    database = BossDatabase.load(default_data_path())

    if args.list:
        for entry in database:
            print(f"  {entry.key:<38} {entry.name}")
        return 0

    entry = database.get(args.boss)
    if entry is None:
        print(f"no boss called {args.boss!r}. Try --list.", file=sys.stderr)
        return 1

    config = Config.load()
    config.overlay_enabled = True
    if args.full:
        config.overlay_detail = "full"

    if args.reset_position:
        config.reset_overlay_position()
        config.save()
        print("saved position cleared; using the default corner")
    elif args.at:
        try:
            x_text, y_text = args.at.split(",")
            config.move_overlay(float(x_text), float(y_text))
        except ValueError:
            print(
                "--at wants two fractions of the screen, like --at 1.0,0.05\n"
                "  0,0   top-left        1,0   top-right\n"
                "  0,0.9 bottom-left     1,0.9 bottom-right",
                file=sys.stderr,
            )
            return 1
        config.save()
        print(f"position set to {config.overlay_fx},{config.overlay_fy}")

    def remember(fx: float, fy: float) -> None:
        config.move_overlay(fx, fy)
        try:
            config.save()
        except OSError as exc:
            print(f"could not save position: {exc}", file=sys.stderr)
        else:
            print(f"position saved: {fx:.3f},{fy:.3f} of the screen")

    overlay = build_overlay(config, on_move=remember)
    if not getattr(overlay, "available", False):
        print(
            "The overlay could not start on this machine.\n"
            "It needs Tk, which ships with the standard Windows Python "
            "installer. ERDLE still works -- you just get the keyboard "
            "panel only.",
            file=sys.stderr,
        )
        return 2

    if args.cycle:
        keys = [args.boss, "malenia", "margit", "flying_dragon_agheel"]
        entries = [e for e in (database.get(k) for k in keys) if e is not None]
    else:
        entries = [entry]

    print("Drag the panel to move it. Ctrl-C to stop.")
    deadline = time.monotonic() + args.seconds
    index = 0
    reported = False
    try:
        while time.monotonic() < deadline:
            current = entries[index % len(entries)]
            print(f"showing: {current.name}")
            overlay.show(build_content(current))
            index += 1
            time.sleep(min(6.0, max(0.5, deadline - time.monotonic())))
            if not reported:
                reported = True
                report(overlay, config)
    except KeyboardInterrupt:
        print()
    finally:
        overlay.stop()
    return 0


def report(overlay, config) -> None:
    """Print where the window was asked to go and where it ended up.

    Position bugs on Windows are all coordinate-space mismatches, and no
    amount of staring at the code distinguishes them. These four numbers
    do.
    """
    print()
    print("--- placement ---")
    print(f"  DPI mode        : {getattr(overlay, 'dpi_mode', '?')}")
    print(f"  Tk screen size  : {getattr(overlay, 'screen', None)}")
    print(f"  saved (fraction): {config.overlay_fx}, {config.overlay_fy}")
    print(f"  legacy pixels   : {config.overlay_x}, {config.overlay_y}")
    placement = getattr(overlay, "last_placement", None)
    if placement is None:
        print("  placement       : never ran")
    else:
        want_x, want_y, got_x, got_y, outcome = placement
        print(f"  asked for       : {want_x}, {want_y}")
        print(f"  ended up at     : {got_x}, {got_y}   ({outcome})")
        if outcome == "drifted":
            print("  ^ the window manager overrode the position.")
    print("-----------------")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
