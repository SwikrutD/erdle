#!/usr/bin/env python3
"""Find out exactly which stage of name recognition is failing.

    python diagnose.py

"Unknown boss" can mean four different things. This walks the pipeline one
stage at a time and reports which one broke, instead of leaving you to
guess:

    1. Is Tesseract actually installed?
    2. Is the name region pointing at the text?
    3. Does OCR read anything at all?
    4. Does what it read match a boss in the table?

Writes name_region.png and name_region_ocr.png so you can see stages 2
and 3 with your own eyes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erdle.bossdb import BossDatabase, default_data_path  # noqa: E402
from erdle.detect import Frame, analyse_bar  # noqa: E402
from erdle.geometry import BOSS_BAR, BOSS_NAME  # noqa: E402
from erdle.matching import BossNameMatcher, normalise, similarity  # noqa: E402
from erdle.ocr import (  # noqa: E402
    TesseractRecogniser,
    binarise,
    crop_to_ink,
    estimate_text_presence,
    ink_bounds,
)

OK = "  [ok]  "
BAD = "  [FAIL]"
WARN = "  [warn]"


def save_grey(
    frame: Frame, path: Path, *, binarised: bool = False, threshold: int = 130
) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    image = Image.new("L", (frame.width, frame.height))
    if binarised:
        bits = binarise(frame, threshold=threshold, light_text=True)
        image.putdata([0 if bit else 255 for row in bits for bit in row])
    else:
        image.putdata(
            [
                int(0.2126 * r + 0.7152 * g + 0.0722 * b)
                for y in range(frame.height)
                for (r, g, b) in [frame.pixel(x, y) for x in range(frame.width)]
            ]
        )
    image.save(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=10.0)
    parser.add_argument("--monitor", type=int, default=1)
    args = parser.parse_args()

    database = BossDatabase.load(default_data_path())
    matcher = BossNameMatcher.from_entries(database, threshold=0.62, min_margin=0.03)

    # --- stage 1: is Tesseract there at all? ------------------------------
    print("=" * 62)
    print("STAGE 1  Tesseract installed?")
    print("=" * 62)
    available, reason = TesseractRecogniser.availability()
    if available:
        print(f"{OK} Tesseract is available")
    else:
        print(f"{BAD} {reason}")
        print("\nThis alone explains 'unknown boss': with no OCR the name can")
        print("never resolve, so every fight falls back to the health mirror.")
        print("Install it, then run this again:")
        print("\n    winget install UB-Mannheim.TesseractOCR")
        print("\nClose and reopen your terminal afterwards so PATH updates.")
        return 1

    # --- capture ----------------------------------------------------------
    try:
        from erdle.sources import MSSSource
    except RuntimeError as exc:
        print(f"{BAD} capture unavailable: {exc}")
        return 1

    source = MSSSource(args.monitor)
    print("\nSwitch to Elden Ring and get a boss bar on screen.")
    remaining = args.delay
    while remaining > 0:
        print(f"  capturing in {remaining:.0f}...", end="\r", flush=True)
        time.sleep(min(1.0, remaining))
        remaining -= 1.0
    print(" " * 40, end="\r")
    frame = source.grab()
    source.close()
    print(f"captured {frame.width}x{frame.height}\n")

    # --- stage 2: is the bar there, and is the name region on the text? ---
    print("=" * 62)
    print("STAGE 2  Regions")
    print("=" * 62)
    bar = analyse_bar(frame, region=BOSS_BAR)
    if bar.present:
        print(f"{OK} boss bar detected, {bar.percent}% health")
    else:
        print(f"{BAD} no boss bar. Was one on screen? Re-run erdle.calibrate.")
        return 1

    name_rect = BOSS_NAME.resolve(frame.width, frame.height)
    plate = frame.region(name_rect)
    ink = estimate_text_presence(plate)
    print(f"        name region {name_rect.width}x{name_rect.height} px, ink={ink:.4f}")
    if ink < 0.012:
        print(f"{BAD} name region looks blank -- it is not on the text")
        return 1
    print(f"{OK} name region has ink in it")

    bounds = ink_bounds(plate)
    cropped = crop_to_ink(plate)
    print(f"        text bounding box {bounds}")
    print(f"        cropped to {cropped.width}x{cropped.height} for OCR")
    if cropped.width < 40 or cropped.height < 8:
        print(f"{WARN} that is very small; OCR may struggle")

    if save_grey(plate, Path("name_region.png")):
        print("        wrote name_region.png       (what the region contains)")
        save_grey(cropped, Path("name_region_ocr.png"), binarised=True)
        print("        wrote name_region_ocr.png   (what OCR sees at 130)")
        for probe in (170, 200, 230):
            save_grey(
                crop_to_ink(plate, threshold=probe),
                Path(f"name_region_t{probe}.png"),
                binarised=True,
                threshold=probe,
            )
        print("        wrote name_region_t170/200/230.png  (higher cutoffs)")

    # --- stage 3: what does OCR read, at each threshold? ------------------
    print()
    print("=" * 62)
    print("STAGE 3  OCR threshold sweep")
    print("=" * 62)
    print("        Boss names are white text, but they sit over whatever is")
    print("        behind the bar. Over sunlit grass a low threshold keeps")
    print("        the terrain too, and OCR reads foliage. Sweeping finds")
    print("        the cutoff that isolates the glyphs.\n")
    print(f"        {'thresh':>6}  {'ink%':>6}  {'crop':>12}  match      raw text")
    print(f"        {'-'*6}  {'-'*6}  {'-'*12}  {'-'*9}  {'-'*24}")

    attempts: list[tuple[int, str, object]] = []
    for threshold in (130, 150, 170, 185, 200, 215, 230):
        cropped_at = crop_to_ink(plate, threshold=threshold)
        ink_at = estimate_text_presence(plate, threshold=threshold)
        try:
            text = TesseractRecogniser(threshold=threshold).read(plate)
        except Exception as exc:
            print(f"        {threshold:>6}  {'':>6}  OCR error: {exc}")
            continue
        hit = matcher.match(text)
        label = hit.display_name[:9] if hit else "-"
        shown = text.replace("\n", " ")[:24]
        print(
            f"        {threshold:>6}  {ink_at:>6.3f}  "
            f"{cropped_at.width:>5}x{cropped_at.height:<6}  {label:<9}  {shown!r}"
        )
        attempts.append((threshold, text, hit))

    matched = [a for a in attempts if a[2] is not None]
    if matched:
        best = max(matched, key=lambda a: a[2].confidence)
        print(f"\n{OK} best threshold is {best[0]} -> {best[2].display_name}")
        print("\nRun with that threshold:")
        print(f"\n    python run.py --ocr-threshold {best[0]}")
        print("\nIf that works, I'll make it the default.")
        return 0

    readable = [a for a in attempts if a[1].strip()]
    if not readable:
        print(f"\n{BAD} OCR read nothing at any threshold.")
        print("        Open name_region_ocr.png. If the text is not legible")
        print("        there, the region or the crop is wrong, not Tesseract.")
        return 1

    print(f"\n{BAD} OCR read text, but nothing matched a boss.")
    raw = max(readable, key=lambda a: len(a[1]))[1]
    print(f"        longest read: {raw!r}")
    print(f"        normalised:   {normalise(raw)!r}")

    # --- stage 4: does it match? ------------------------------------------
    print()
    print("=" * 62)
    print("STAGE 4  Matching")
    print("=" * 62)
    target = normalise(raw)
    scored = sorted(
        ((similarity(target, normalise(e.name)), e) for e in database),
        key=lambda pair: -pair[0],
    )
    print("        closest candidates:")
    for score, entry in scored[:5]:
        mark = ">>" if score >= matcher.threshold else "  "
        print(f"        {mark} {score:.3f}  {entry.name}")

    result = matcher.match(raw)
    if result is None:
        best = scored[0]
        print(f"\n{BAD} nothing cleared the {matcher.threshold:.2f} threshold")
        if best[0] > 0.4:
            print(f"        closest was {best[1].name!r} at {best[0]:.3f}")
            print("        OCR is working but inaccurate. Paste this whole")
            print("        output and I can widen the alias list.")
        else:
            print("        Nothing is close. Is this boss in the table yet?")
            print(f"        The table currently holds {len(database)} bosses.")
        return 1

    print(f"\n{OK} matched: {result.display_name}")
    print(f"        confidence {result.confidence:.3f}, margin {result.margin:.3f}")
    print("\nName recognition is working. If the keyboard still shows")
    print("'unknown boss', re-run `python run.py` -- this was a stale build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
