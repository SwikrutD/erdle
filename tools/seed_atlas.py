#!/usr/bin/env python3
"""Promote the atlas you have learned into the one that ships.

    python tools/seed_atlas.py            # copy if it is an improvement
    python tools/seed_atlas.py --check    # report, change nothing
    python tools/seed_atlas.py --force    # copy even if it is worse

ERDLE learns glyphs as it reads name plates, and writes them to a
per-user file (`%APPDATA%/erdle/glyphs.json`). That file is where all the
learning accumulates -- but it is *yours*, and a fresh install has none of
it. Without a seeded `data/glyphs.json`, every new user starts with a
zero-character alphabet and leans entirely on Tesseract until they have
fought enough bosses to teach the thing, which is the slowest and most
confusing possible first hour.

This copies the learned atlas over the shipped one so that first hour
starts from wherever you got to.

Refuses to make the shipped atlas worse by default. Merging the two would
be the obvious alternative, but the shipped file is meant to be a
reviewable artifact rather than an ever-growing accretion of every
machine it has ever run on -- and `build_recogniser` already merges the
user's own learning on top at run time, so nothing is lost either way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erdle.config import config_dir  # noqa: E402
from erdle.glyphs import GlyphAtlas, default_atlas_path  # noqa: E402

#: The point at which names resolve with no OCR help at all. Below this
#: the atlas is a head start; at or above it, Tesseract is optional.
SELF_SUFFICIENT = 45



#: Real captures kept as reference plates. An atlas that cannot read
#: these cannot read the game.
PLATES = {
    "God-Devouring-Serpent.png": "God-Devouring Serpent",
    "Malenia, Blade of Miquella.png": "Malenia, Blade of Miquella",
    "Starscourge Radahn.png": "Starscourge Radahn",
}

#: All three are captures of the game running on the machine ERDLE runs
#: on. That is not incidental. A "Night's Cavalry" frame lifted from a
#: YouTube video sat here first, and it rejected genuine screenshots --
#: Starscourge Radahn among them -- because video compression softens
#: thin strokes until the letterforms no longer agree with what the game
#: draws. The gate was measuring against the wrong thing, and the symptom
#: looked exactly like the screenshots being at fault.


def can_read_the_reference_plates(atlas) -> list[str]:
    """Which reference plates this atlas fails to read. Empty is good.

    A character count says nothing about correctness. An atlas of 46
    characters shipped that could not read either reference plate --
    coverage 0.40 on one and 0.00 on the other -- because a filename with
    a trailing digit had made the label one character longer than the
    plate, and every glyph after the first break was filed under the
    wrong letter. It looked like progress the whole way.

    Reading a known plate back is the only check that catches that.
    """
    plates = Path(__file__).resolve().parent.parent / "tests" / "plates"
    if not plates.is_dir():
        return []
    try:
        from PIL import Image
    except ImportError:
        return []

    from erdle.detect import Frame
    from erdle.glyphs import DEFAULT_INK_THRESHOLD, read_text
    from erdle.matching import normalise

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import learn as learn_tool

    failures = []
    for filename, name in PLATES.items():
        path = plates / filename
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        frame = Frame(image.width, image.height, list(image.getdata()))
        # The fixtures are cropped name bands, not whole screenshots: a
        # 4K capture is 3.8 MB and only 90 rows of it are the plate, so
        # committing four of them meant 19 MB of git history for pixels
        # nothing reads. Full screenshots are still handled, since
        # anything collected by `--dump-plate` arrives cropped anyway.
        if image.height > 400:                 # a full screenshot
            frame = learn_tool.locate_name_plate(frame) or frame
        text, _ = read_text(frame, atlas, threshold=DEFAULT_INK_THRESHOLD)
        if normalise(text) != normalise(name):
            failures.append(f"{name}: read {text!r}")
    return failures


def describe(path: Path) -> tuple[int, int]:
    """(characters, samples) for an atlas that may not exist."""
    if not path.exists():
        return 0, 0
    atlas = GlyphAtlas.load(path)
    return len(atlas), atlas.total_samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report and change nothing")
    parser.add_argument("--force", action="store_true",
                        help="copy even if it would lose characters")
    parser.add_argument("--source", type=Path, default=None,
                        help="learned atlas (default: the per-user one)")
    args = parser.parse_args()

    source = args.source or (config_dir() / "glyphs.json")
    target = default_atlas_path()

    learned_chars, learned_samples = describe(source)
    shipped_chars, shipped_samples = describe(target)

    print(f"learned : {source}")
    print(f"          {learned_chars} characters, {learned_samples} samples")
    print(f"shipped : {target}")
    print(f"          {shipped_chars} characters, {shipped_samples} samples")

    if learned_chars == 0:
        print("\nnothing learned yet -- play a few fights first", file=sys.stderr)
        return 1

    if learned_chars < shipped_chars and not args.force:
        print(f"\nthe shipped atlas already knows more "
              f"({shipped_chars} > {learned_chars}); --force to overwrite anyway",
              file=sys.stderr)
        return 1

    if args.check:
        print("\n(--check: nothing written)")
        return 0

    atlas = GlyphAtlas.load(source)
    dropped = atlas.prune()

    failures = can_read_the_reference_plates(atlas)
    if failures and not args.force:
        print("\nthis atlas cannot read the reference plates:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("\nNot shipping it. Mislabelled samples are worse than none --"
              "\nthey produce confident wrong reads. Start again with:"
              "\n    python tools/atlas.py reset --yes"
              "\n    python tools/learn.py --dir screenshots/"
              "\n--force overrides.", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    atlas.path = target
    atlas.save()
    print(f"\ncopied -> {target}")
    if dropped:
        # Worth naming. These are one- and two-pixel "glyphs" from a bad
        # segmentation; normalised onto the grid they sit close to every
        # letter at once, so they cause wrong reads rather than missing
        # ones -- which is the harder failure to notice.
        print(f"dropped {dropped} degenerate sample(s) on the way")

    if learned_chars < SELF_SUFFICIENT:
        # Worth saying plainly. A partial atlas still helps, but the
        # difference between 29 and 45 is the difference between "needs
        # Tesseract" and "does not".
        print(f"note: {learned_chars} of {SELF_SUFFICIENT} characters. "
              f"Users will still need the bundled Tesseract until the "
              f"atlas covers the alphabet.")
    else:
        print(f"{learned_chars} characters -- names resolve without OCR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
