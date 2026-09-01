"""Interactive calibration for the HUD regions and colour thresholds.

    python -m erdle.calibrate

The fractional regions in `geometry.py` and the colour cutoffs in
`detect.py` are the only numbers in this project that cannot be verified
without a real Elden Ring frame. Everything else is covered by tests.

Stand in front of a boss with the bar on screen and run this. It reports
what the detector currently sees and suggests adjustments.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .detect import (
    DEFAULT_THRESHOLDS,
    RGB,
    BarThresholds,
    Frame,
    analyse_bar,
    is_depleted_pixel,
    is_health_pixel,
)
from .geometry import BOSS_BAR, BOSS_NAME, HUD_STRIP, FractionalRect
from .ocr import estimate_text_presence


def describe_frame(frame: Frame) -> dict:
    """Report what the detector sees, and why."""
    bar_rect = BOSS_BAR.resolve(frame.width, frame.height)
    name_rect = BOSS_NAME.resolve(frame.width, frame.height)
    observation = analyse_bar(frame)

    line = frame.scanline(bar_rect.center_y, bar_rect.left, bar_rect.right)
    health = [px for px in line if is_health_pixel(px)]
    depleted = [px for px in line if is_depleted_pixel(px)]
    unclassified = [
        px for px in line
        if not is_health_pixel(px) and not is_depleted_pixel(px)
    ]

    return {
        "resolution": f"{frame.width}x{frame.height}",
        "bar_region_px": [bar_rect.left, bar_rect.top, bar_rect.right, bar_rect.bottom],
        "present": observation.present,
        "fill_percent": observation.percent,
        "coverage": round(observation.coverage, 3),
        "scanline": {
            "health": len(health),
            "depleted": len(depleted),
            "unclassified": len(unclassified),
        },
        "sample_health_colour": _mean(health),
        "sample_depleted_colour": _mean(depleted),
        "sample_unclassified_colour": _mean(unclassified),
        "name_plate_ink": round(estimate_text_presence(frame.region(name_rect)), 4),
    }


def advise(report: dict) -> list[str]:
    notes: list[str] = []
    scan = report["scanline"]
    total = sum(scan.values()) or 1

    if scan["unclassified"] / total > 0.25:
        notes.append(
            f"{scan['unclassified'] / total:.0%} of the scanline matched neither "
            "class. The bar region is probably misaligned, or the colour "
            f"cutoffs need widening. Unclassified average: "
            f"{report['sample_unclassified_colour']}."
        )
    if not report["present"]:
        notes.append(
            "No bar detected. If one was on screen, adjust BOSS_BAR in "
            "geometry.py -- the region is a fraction of the frame, so nudge "
            "`top`/`bottom` first."
        )
    if report["present"] and scan["health"] == 0:
        notes.append(
            "Bar found but no health pixels. Lower health_min_red or "
            "health_red_ratio in detect.BarThresholds. Observed bar colour: "
            f"{report['sample_unclassified_colour'] or report['sample_depleted_colour']}."
        )
    ink = report["name_plate_ink"]
    if ink < 0.012:
        notes.append(
            f"Name plate looks blank (ink={ink}). If a name was visible, "
            "shift BOSS_NAME upward or lower AppConfig.min_text_presence."
        )
    elif ink > 0.4:
        notes.append(
            f"Name plate is very bright (ink={ink}); the region may be "
            "overlapping the bar itself."
        )
    if not notes:
        notes.append("Everything looks consistent. No changes suggested.")
    return notes


def _mean(pixels: list) -> list[int] | None:
    if not pixels:
        return None
    count = len(pixels)
    return [round(sum(px[i] for px in pixels) / count) for i in range(3)]


# --- automatic region discovery -------------------------------------------
# Guessing coordinates from screenshots is unreliable. Instead, find the
# bar by its defining property: it is the only thing on screen that is a
# very long, horizontally continuous run of red-dominant pixels.


def _bar_like(rgb: RGB, ratio: float = 2.4) -> bool:
    """Is this pixel saturated red, as the health bar is?

    The ratio matters enormously. Sunlit Limgrave dirt is (153, 115, 69) --
    red-dominant, so a loose ratio like 1.2 matches the entire ground and
    the search finds a "bar" covering half the screen. Bar red has *low*
    green and blue (roughly (150, 40, 40), a ratio near 4), so demanding
    real saturation separates them cleanly.
    """
    red, green, blue = rgb
    return red > 45 and red > green * ratio and red > blue * ratio


# Tried strictest first. A bar that only shows up at a loose ratio is
# suspicious, and the ratio that worked tells us what to set in detect.py.
_RATIO_LADDER = (3.2, 2.8, 2.4, 2.0, 1.7, 1.45)

# The boss bar is a thin, wide HUD element. Anything outside these bounds
# is terrain, no matter how red it is.
_MAX_BAND_HEIGHT = 0.045   # of screen height
_MAX_RUN_WIDTH = 0.80      # of screen width


def longest_run(
    frame: Frame, y: int, step: int = 2, ratio: float = 2.4
) -> tuple[int, int, int]:
    """Longest horizontal run of bar-red pixels in row `y`.

    Only the *filled* portion is matched. Trying to also match the empty
    track fails badly: the empty track is dark and near-grey, and so is a
    night sky, a cave wall, or any shadow, so the run escapes the bar and
    swallows the row. The right edge is recovered by symmetry instead --
    see `find_bar`.

    Returns (length, start_x, end_x) in pixels. `step` subsamples for
    speed; a 4K scan is otherwise millions of checks per row.
    """
    best_len = best_start = best_end = 0
    current_len = 0
    current_start = 0
    for x in range(0, frame.width, step):
        if _bar_like(frame.pixel(x, y), ratio):
            if current_len == 0:
                current_start = x
            current_len += step
            if current_len > best_len:
                best_len, best_start, best_end = current_len, current_start, x
        else:
            current_len = 0
    return best_len, best_start, best_end


def find_bar(
    frame: Frame,
    *,
    # The bar sits at ~0.80 of screen height on every aspect ratio
    # tested. 0.55 was wide enough to admit mid-screen scenery, which is
    # how a 4K display ended up calibrated to a red band at 0.5596.
    search_from: float = 0.70,
    search_to: float = 0.92,
    min_width: float = 0.10,
    row_step: int = 2,
) -> dict | None:
    """Locate the boss bar by scanning rows in the lower screen.

    Tries progressively looser red ratios and returns the first that yields
    a band shaped like a HUD element rather than like ground. Reports which
    ratio succeeded, so the detection threshold can be set from evidence.

    `min_width` is low enough to still find a bar at ~20% health, because
    people will calibrate mid-fight.
    """
    for ratio in _RATIO_LADDER:
        found = _find_bar_at_ratio(
            frame,
            search_from=search_from,
            search_to=search_to,
            min_width=min_width,
            row_step=row_step,
            ratio=ratio,
        )
        if found is not None:
            return found
    return None


def _find_bar_at_ratio(
    frame: Frame,
    *,
    search_from: float,
    search_to: float,
    min_width: float,
    row_step: int,
    ratio: float,
) -> dict | None:
    top = int(frame.height * search_from)
    bottom = int(frame.height * search_to)
    threshold = frame.width * min_width
    max_run = frame.width * _MAX_RUN_WIDTH
    centre = frame.width / 2

    rows: list[tuple[int, int, int]] = []
    for y in range(top, bottom, row_step):
        length, start, end = longest_run(frame, y, ratio=ratio)
        # The bar is centred, so its left edge is always left of middle;
        # and it never spans nearly the whole screen.
        if threshold <= length <= max_run and start < centre:
            rows.append((y, start, end))

    if not rows:
        return None

    # Keep the largest contiguous block of qualifying rows -- transient
    # matches from terrain will be isolated, the bar will be a solid band.
    blocks: list[list[tuple[int, int, int]]] = [[rows[0]]]
    for entry in rows[1:]:
        if entry[0] - blocks[-1][-1][0] <= row_step * 3:
            blocks[-1].append(entry)
        else:
            blocks.append([entry])
    band = max(blocks, key=len)

    if len(band) < 3:
        return None  # too thin to be a HUD element

    ys = [r[0] for r in band]
    band_height = (max(ys) - min(ys)) / frame.height
    if band_height > _MAX_BAND_HEIGHT:
        return None  # a tall block of red is terrain, not a health bar

    starts = [r[1] for r in band]
    ends = [r[2] for r in band]
    # Median edges: robust against a row clipped by a particle effect.
    left = sorted(starts)[len(starts) // 2]
    observed_right = sorted(ends)[len(ends) // 2]

    # The bar is centred on screen, so its right edge mirrors its left.
    # Deriving it this way means a half-drained bar still yields the true
    # full extent -- measuring the red alone would report a region far too
    # narrow and make every later fill reading wrong.
    mirrored_right = frame.width - left
    right = max(mirrored_right, observed_right)
    fill_estimate = (observed_right - left) / max(right - left, 1)

    return {
        "rows_matched": len(band),
        "ratio_used": ratio,
        "bar_looked_full": fill_estimate > 0.95,
        "estimated_fill": round(fill_estimate, 3),
        "pixels": {
            "top": min(ys), "bottom": max(ys) + row_step,
            "left": left, "right": min(right, frame.width),
        },
        "fractions": {
            "top": round(min(ys) / frame.height, 4),
            "bottom": round((max(ys) + row_step) / frame.height, 4),
            "left": round(left / frame.width, 4),
            "right": round(min(right, frame.width) / frame.width, 4),
        },
    }


def suggest_regions(found: dict, *, name_height: float = 0.042) -> str:
    """Emit ready-to-paste replacements for geometry.py."""
    f = found["fractions"]
    # Small vertical padding only. The detector samples three scanlines
    # spread across the region, so generous padding would push the outer
    # two off the bar and onto whatever is behind it.
    pad_y = 0.0015
    bar_top = max(f["top"] - pad_y, 0.0)
    bar_bottom = min(f["bottom"] + pad_y, 1.0)
    left = max(f["left"] - 0.004, 0.0)
    right = min(f["right"] + 0.004, 1.0)

    name_bottom = max(bar_top - 0.003, 0.0)
    name_top = max(name_bottom - name_height, 0.0)

    strip_top = max(name_top - 0.013, 0.0)
    strip_bottom = min(bar_bottom + 0.014, 1.0)
    strip_left = max(left - 0.03, 0.0)
    strip_right = min(right + 0.03, 1.0)

    return (
        "BOSS_BAR = FractionalRect("
        f"left={left:.4f}, top={bar_top:.4f}, "
        f"right={right:.4f}, bottom={bar_bottom:.4f})\n"
        "BOSS_NAME = FractionalRect("
        f"left={left:.4f}, top={name_top:.4f}, "
        f"right={right:.4f}, bottom={name_bottom:.4f})\n"
        "HUD_STRIP = FractionalRect("
        f"left={strip_left:.4f}, top={strip_top:.4f}, "
        f"right={strip_right:.4f}, bottom={strip_bottom:.4f})"
    )


def sample_bar_colours(frame: Frame, found: dict) -> dict:
    """Average colour of the filled and empty halves of the located bar."""
    px = found["pixels"]
    y = (px["top"] + px["bottom"]) // 2
    line = frame.scanline(y, px["left"], min(px["right"], frame.width))
    if not line:
        return {}
    lit = [c for c in line if _bar_like(c)]
    dark = [c for c in line if not _bar_like(c)]
    return {
        "filled_avg": _mean(lit),
        "empty_avg": _mean(dark),
        "filled_pixels": len(lit),
        "empty_pixels": len(dark),
        "passes_current_thresholds": sum(1 for c in lit if is_health_pixel(c)),
    }


def save_debug_png(frame: Frame, path: Path, scale: int = 1) -> bool:
    """Write what we captured, so you can see it rather than guess."""
    try:
        from PIL import Image
    except ImportError:
        return False
    image = Image.new("RGB", (frame.width, frame.height))
    image.putdata(
        [frame.pixel(x, y) for y in range(frame.height) for x in range(frame.width)]
    )
    if scale != 1:
        image = image.resize(
            (frame.width * scale, frame.height * scale), Image.NEAREST
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return True


def main() -> int:  # pragma: no cover - needs a live display
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay", type=float, default=10.0,
        help="seconds to wait before capturing, so you can switch back to "
             "the game (default: 10)",
    )
    parser.add_argument(
        "--monitor", type=int, default=1, help="mss monitor index (default: 1)"
    )
    parser.add_argument(
        "--save", type=Path, default=Path("calibration.png"),
        help="write the captured HUD strip here (default: calibration.png)",
    )
    args = parser.parse_args()

    try:
        from .sources import MSSSource
    except RuntimeError as exc:
        print(f"capture unavailable: {exc}", file=sys.stderr)
        return 1

    source = MSSSource(args.monitor)

    print("Switch back to Elden Ring NOW and stand in front of a boss so the")
    print("health bar is on screen. Capturing automatically -- do not alt-tab")
    print("back here until it finishes.\n")
    remaining = args.delay
    while remaining > 0:
        print(f"  capturing in {remaining:.0f}...", end="\r", flush=True)
        time.sleep(min(1.0, remaining))
        remaining -= 1.0
    print(" " * 40, end="\r")

    frame = source.grab()
    source.close()
    print(f"captured {frame.width}x{frame.height}\n")

    # Save the full frame first -- if the capture missed the game entirely,
    # a crop of the wrong place is useless for diagnosis.
    if save_debug_png(frame, args.save):
        print(f"full screenshot -> {args.save.resolve()}")

    print("\n=== searching for the boss bar ===")
    found = find_bar(frame)
    if found is None:
        print("No bar found anywhere in the lower half of the screen.")
        print("Either the bar was not on screen, or the capture missed the")
        print(f"game -- open {args.save.name} and check what was captured.")
        return 1

    f = found["fractions"]
    print(f"found across {found['rows_matched']} rows")
    print(f"  vertical   {f['top']:.4f} - {f['bottom']:.4f} of screen height")
    print(f"  horizontal {f['left']:.4f} - {f['right']:.4f} of screen width")
    print(f"  pixels     {found['pixels']}")
    if not found["bar_looked_full"]:
        print(f"  note: boss was at ~{found['estimated_fill']:.0%} health; the")
        print("        right edge was derived by symmetry. Recalibrate against")
        print("        a full bar if the result looks off.")

    colours = sample_bar_colours(frame, found)
    if colours:
        print(f"\n  filled colour  {colours['filled_avg']} "
              f"({colours['filled_pixels']} px)")
        print(f"  empty colour   {colours['empty_avg']} "
              f"({colours['empty_pixels']} px)")
        passing = colours["passes_current_thresholds"]
        print(f"  of the filled pixels, {passing} pass the current thresholds")
        if passing < colours["filled_pixels"] * 0.5:
            print("  -> thresholds need loosening; see BarThresholds in detect.py")
        else:
            print("  -> thresholds look fine")

    print("\n=== paste these into erdle/geometry.py ===\n")
    print(suggest_regions(found))

    print("\n=== current settings, for comparison ===")
    report = describe_frame(frame)
    print(json.dumps(report, indent=2))
    for note in advise(report):
        print(f"* {note}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def parse_suggestion_regions(found: dict):
    """Regions for a located bar, as FractionalRects.

    Shared by autocal and the screenshot learner so the two cannot drift.
    """
    from .autocal import parse_suggestion

    return parse_suggestion(suggest_regions(found))
