#!/usr/bin/env python3
"""Teach the glyph atlas from screenshots, without fighting the boss.

    python tools/learn.py shot.png "Rykard, Lord of Blasphemy"
    python tools/learn.py --dir screenshots/
    python tools/learn.py --dir screenshots/ --dry-run

In `--dir` mode the boss name comes from the filename, so
`Rykard, Lord of Blasphemy.png` needs no second argument.

USE YOUR OWN CAPTURES. A frame lifted from a YouTube video looks right
and is not: compression softens the thin strokes until letters merge or
break, so the plate segments into a different number of pieces than it
has letters. Sixteen such frames yielded two usable plates here, and the
samples they did contribute disagreed with the game's own rendering
badly enough to reject genuine screenshots of Starscourge Radahn.

RESOLUTION MATTERS TOO. Glyphs are only compared against stored samples
of a similar size, so a screenshot must be near the resolution you
actually play at. At 3840x2160 a boss name is roughly 20-33px tall; at 1920x1080
it is 10-16px. Those will not match each other, and the tool says so
rather than quietly filling the atlas with useless samples.

The name plate is located automatically using the same bar search as
calibration, so any full screenshot works. A pre-cropped name plate is
also accepted.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erdle.calibrate import find_bar, parse_suggestion_regions  # noqa: E402
from erdle.config import Config  # noqa: E402
from erdle.detect import Frame  # noqa: E402
from erdle.glyphs import (  # noqa: E402
    GlyphAtlas,
    learn_from_text,
    segment_glyphs,
)
from erdle.ocr import DEFAULT_INK_THRESHOLD  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# Heights to synthesise from each screenshot. One capture at 4K becomes
# samples for every common display, because downscaling a 2160p frame is a
# very close approximation of what the game renders at 1080p -- same
# geometry, same anti-aliasing, just fewer pixels.
#
# This is the practical answer to "how do I ship one atlas for everyone".
# Rather than chase perfect scale-invariance in the matcher -- which fails
# because normalising a glyph to its own bounding box destroys the size
# difference between 'C' and 'c' -- cover the resolutions directly.
RESOLUTION_LADDER = (2160, 1440, 1200, 1080, 900, 768)


def downscale(frame, target_height: int):
    """Resample a frame to a given height, preserving aspect."""
    from PIL import Image

    if target_height >= frame.height:
        return None
    image = Image.new("RGB", (frame.width, frame.height))
    image.putdata(
        [frame.pixel(x, y) for y in range(frame.height) for x in range(frame.width)]
    )
    width = max(1, round(frame.width * target_height / frame.height))
    resized = image.resize((width, target_height), Image.LANCZOS)
    getter = getattr(resized, "get_flattened_data", None) or resized.getdata
    return Frame(resized.width, resized.height, list(getter()))


def load_frame(path: Path) -> Frame:
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("needs `pip install pillow`")
    image = Image.open(path).convert("RGB")
    getter = getattr(image, "get_flattened_data", None) or image.getdata
    return Frame(image.width, image.height, list(getter()))


def locate_name_plate(frame: Frame) -> Frame | None:
    """Find the boss name plate in a full screenshot.

    Returns None if no boss bar is visible -- which usually means the
    screenshot was taken outside a fight, or is cropped oddly.
    """
    found = find_bar(frame)
    if found is None:
        return None
    regions = parse_suggestion_regions(found)
    if regions is None:
        return None
    _, name_region, _ = regions
    return frame.region(name_region.resolve(frame.width, frame.height))


#: Deliberately *not* a ladder of brightness cutoffs. The app tries
#: several when reading, because a wrong read costs one poll. Learning is
#: different: trying cutoffs until the glyph count matches the label is
#: hunting for a coincidence, and the coincidences are wrong. Measured on
#: sixteen real screenshots, a ladder turned two usable plates into nine
#: that each passed the count check and then failed the reference plates.
#: One cutoff, and a plate that does not segment cleanly is skipped.


def learn_frame(
    frame, name: str, atlas: GlyphAtlas, *, threshold: int, dry_run: bool
) -> tuple[bool, str]:
    """Learn from one already-loaded frame."""
    plate = locate_name_plate(frame)
    located = "auto-located"
    if plate is None:
        # Maybe it is already a cropped name plate.
        plate = frame
        located = "using whole image"

    boxes = segment_glyphs(plate, threshold=threshold)
    expected = [c for c in name if not c.isspace()]
    heights = sorted(b.height for b in boxes)

    detail = (
        f"{len(boxes)} glyphs, expected {len(expected)}"
        + (f", heights {heights[0]}-{heights[-1]}px" if boxes else "")
    )

    if not boxes:
        return False, f"no text found ({located})"
    if dry_run:
        fits = len(boxes) == len(expected)
        verdict = "would learn" if fits else "would probably refuse"
        return fits, f"{verdict} -- {detail} ({located})"

    # No count check here. `learn_from_text` does its own, after trying to
    # rejoin letters broken at a thin stroke -- a plate that segments into
    # 15 pieces for 14 characters is usually one `h` split at its arch,
    # and rejecting it here threw those away before anything could look.
    learned = learn_from_text(plate, name, atlas, threshold=threshold)
    # Zero is ambiguous: it means "refused" *or* "every glyph was already
    # known", and treating both as failure rejected the very plates the
    # atlas was seeded from. `last_refusal` distinguishes them.
    if learn_from_text.last_refusal is not None:
        return False, f"segmentation mismatch -- {detail} ({located})"
    return True, f"learned {learned} new -- {detail} ({located})"



def snapshot(atlas: GlyphAtlas) -> dict:
    """A restorable copy of what the atlas holds."""
    return {char: list(samples) for char, samples in atlas.samples.items()}


def restore(atlas: GlyphAtlas, saved: dict) -> None:
    atlas.samples.clear()
    atlas.samples.update(saved)


def breaks_the_references(atlas: GlyphAtlas) -> list[str]:
    """Reference-plate failures, or [] when the atlas still reads them.

    Matching glyph counts is not proof of correct alignment, which is the
    lesson that cost the most here. "Starscourge Radahn" segments into
    exactly 17 boxes for its 17 letters and is accepted -- and its
    samples then turn a perfect read of the serpent plate into
    'God-D?vou?ing S??pen?'. One letter had split and another merged, so
    the count survived while every label between them moved.

    No property of a single plate catches that. Reading back a plate
    whose text is already known does, so every file is checked against
    the references and rolled back if it makes them worse.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import seed_atlas

    return seed_atlas.can_read_the_reference_plates(atlas)


def learn_one(
    path: Path,
    name: str,
    atlas: GlyphAtlas,
    *,
    threshold: int,
    dry_run: bool,
    ladder: bool = True,
) -> tuple[bool, str]:
    """Learn from a screenshot, and from downscaled copies of it.

    One 4K capture yields samples for 1440p, 1080p and below, so a single
    contributor can produce an atlas that works on everyone's display.
    """
    frame = load_frame(path)
    ok, message = learn_frame(
        frame, name, atlas, threshold=threshold, dry_run=dry_run
    )
    if not ok or not ladder:
        return ok, message

    extra = []
    for target in RESOLUTION_LADDER:
        smaller = downscale(frame, target)
        if smaller is None:
            continue
        sub_ok, _ = learn_frame(
            smaller, name, atlas, threshold=threshold, dry_run=dry_run
        )
        if sub_ok:
            extra.append(str(target) + "p")
    if extra:
        message += f" + {','.join(extra)}"
    return True, message


def check_resolution(paths: list[Path]) -> None:
    """Warn when screenshots are unlikely to match the play resolution."""
    config = Config.load()
    if not config.calibrated or not config.calibrated_for:
        return
    try:
        target_h = int(config.calibrated_for.split("x")[1])
    except (IndexError, ValueError):
        return

    for path in paths[:1]:
        try:
            frame = load_frame(path)
        except SystemExit:
            raise
        except Exception:
            return
        ratio = max(frame.height, target_h) / max(1, min(frame.height, target_h))
        if ratio > 1.5:
            print(
                f"!! screenshots are {frame.width}x{frame.height} but you play "
                f"at {config.calibrated_for}."
            )
            print(
                "   Glyphs are only matched against similarly-sized samples, "
                "so these\n   will be learned but never used. Find screenshots "
                "nearer your resolution.\n"
            )



#: Trailing counters people add when saving several shots of one boss:
#: "Night's Cavalry2", "Margit (3)", "Radahn_04", "Rykard - 2".
_COUNTER = re.compile(r"[\s._\-]*[\(\[]?\d+[\)\]]?$")


def name_from_filename(stem: str, roster: set[str]) -> tuple[str | None, str]:
    """The boss name a file is named after, or why it is not one.

    A filename is a label the atlas trusts absolutely: every glyph on the
    plate is filed under the corresponding character, in order. So a
    wrong label does not fail loudly, it teaches wrong letters.

    "Night's Cavalry1" is the case that bit. The plate holds 14
    characters, the segmenter found 15, and the trailing "1" made the
    string 15 long too -- so the count check passed by coincidence and
    every glyph after the extra box was filed one position out. That is
    exactly the corruption `learn_from_text` refuses to allow, defeated
    by a digit in a filename.

    Two guards: strip a trailing counter, then require the result to be a
    boss that actually exists. The roster is right there; not consulting
    it was the mistake.
    """
    candidate = stem.strip()
    if candidate in roster:
        return candidate, candidate
    stripped = _COUNTER.sub("", candidate).strip()
    if stripped in roster:
        return stripped, stripped
    lowered = {name.lower(): name for name in roster}
    for form in (candidate.lower(), stripped.lower()):
        if form in lowered:
            return lowered[form], lowered[form]
    return None, stripped or candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="screenshot file")
    parser.add_argument("name", nargs="?", help="boss name, exactly as rendered")
    parser.add_argument("--dir", help="folder of screenshots named after bosses")
    parser.add_argument("--threshold", type=int, default=DEFAULT_INK_THRESHOLD)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be learned, change nothing")
    parser.add_argument(
        "--no-verify", action="store_true",
        help="skip the reference-plate check after each file (not advised)",
    )
    parser.add_argument("--no-ladder", action="store_true",
                        help="learn only at the screenshot's own resolution")
    args = parser.parse_args()

    from erdle.config import config_dir

    atlas_path = config_dir() / "glyphs.json"
    # Start from the shipped atlas merged with anything already learned,
    # which is what `build_recogniser` assembles at run time. Checking a
    # new file against a learned-only atlas is meaningless -- on a fresh
    # machine that atlas is empty, so nothing can read the reference
    # plates and every file gets rejected.
    from erdle.glyphs import default_atlas_path

    atlas = GlyphAtlas.load(default_atlas_path())
    learned_already = GlyphAtlas.load(atlas_path)
    for char, samples in learned_already.samples.items():
        for signature, height in samples:
            atlas.learn(char, signature, height)
    before = len(atlas)

    jobs: list[tuple[Path, str]] = []
    if args.dir:
        folder = Path(args.dir)
        if not folder.is_dir():
            print(f"not a folder: {folder}", file=sys.stderr)
            return 1
        from erdle.bossdb import BossDatabase, default_data_path

        roster = {e.name for e in BossDatabase.load(default_data_path())}
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            name, tried = name_from_filename(path.stem, roster)
            if name is None:
                print(f"  [skip] {path.name}: {tried!r} is not a boss "
                      f"-- rename it, or pass --name")
                continue
            jobs.append((path, name))
    elif args.image and args.name:
        jobs.append((Path(args.image), args.name))
    else:
        parser.error("give an image and a name, or --dir")

    if not jobs:
        print("no images found")
        return 1


    ok = rejected = 0
    verify = not args.no_verify and not args.dry_run
    for path, name in jobs:
        saved = snapshot(atlas) if verify else None
        try:
            success, message = learn_one(
                path, name, atlas,
                threshold=args.threshold, dry_run=args.dry_run,
                ladder=not args.no_ladder,
            )
        except Exception as exc:
            success, message = False, f"error: {exc}"

        if success and verify:
            failures = breaks_the_references(atlas)
            if failures:
                restore(atlas, saved)
                success = False
                rejected += 1
                message = ("rejected -- its samples stop the reference "
                           "plates reading:\n         "
                           + "\n         ".join(failures))

        mark = "ok " if success else "-- "
        ok += success
        print(f"  [{mark}] {name!r}: {message}")

    print(f"\n{ok}/{len(jobs)} usable"
          + (f", {rejected} rejected by the reference check" if rejected else ""))
    if args.dry_run:
        print("(dry run -- nothing saved)")
        return 0
    if len(atlas) != before or ok:
        atlas.path = atlas_path
        atlas.save()
        print(f"atlas now {len(atlas)} characters -> {atlas.alphabet}")
        print(f"saved to {atlas_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
