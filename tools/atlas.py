#!/usr/bin/env python3
"""Inspect, merge and export glyph atlases.

    python tools/atlas.py show                 # what has been learned
    python tools/atlas.py show --render         # draw each glyph
    python tools/atlas.py merge a.json b.json   # combine contributions
    python tools/atlas.py ship                  # copy learned -> data/

`ship` is how a complete atlas gets into a release: play until the atlas
covers the alphabet, then bake it into data/glyphs.json so users who
never install Tesseract still get boss names.
"""

from __future__ import annotations

import argparse
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erdle.config import config_dir  # noqa: E402
from erdle.glyphs import (  # noqa: E402
    CELL_HEIGHT,
    CELL_WIDTH,
    LEVELS,
    GlyphAtlas,
    default_atlas_path,
)

def expected_alphabet() -> set:
    """Exactly the characters the shipped boss names contain.

    Measuring against `string.ascii_letters` was wrong in both directions:
    it demanded I, J, X, Y and j, which appear in no boss name, so the
    count could never reach 100% and there was no way to tell when the
    atlas was finished. And it ignored `&`, which "Nox Swordstress & Nox
    Priest" needs.

    Spaces are excluded: the segmenter finds glyphs by the gaps between
    them, so a space is never a glyph to learn.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    try:
        database = BossDatabase.load(default_data_path())
    except Exception:
        # Better a slightly wrong target than a tool that will not run.
        return set(string.ascii_letters) | set("',-")

    characters = set()
    for entry in database:
        characters |= set(entry.name)
    return characters - {" "}


EXPECTED = expected_alphabet()


def learned_path() -> Path:
    return config_dir() / "glyphs.json"


def effective_atlas(path: Path | None = None) -> tuple[GlyphAtlas, int, int]:
    """What the app actually reads with: shipped, plus what you learned.

    Returns (merged, shipped_characters, learned_characters).

    Reporting only the learned file was right when nothing shipped, and
    became wrong the moment `data/glyphs.json` was seeded: `plan` would
    send you after letters the bundled atlas already had, and `show`
    would say 0 characters on a build that reads fine. `build_recogniser`
    merges the two at run time, so the report has to as well.
    """
    from erdle.glyphs import default_atlas_path

    shipped = GlyphAtlas.load(default_atlas_path())
    learned = GlyphAtlas.load(path or learned_path())
    shipped_chars, learned_chars = len(shipped), len(learned)
    for char, signatures in learned.samples.items():
        for signature, height in signatures:
            shipped.learn(char, signature, height)
    return shipped, shipped_chars, learned_chars


def report_merge(merged, shipped: int, learned: int) -> None:
    """Print the merge, and explain it when the arithmetic looks wrong.

    "shipped 33 + learned 34 -> 33" reads as a bug. It is not: the
    learned file predates pruning and its extra character is a degenerate
    sample the merge now refuses. Saying so beats leaving the reader to
    work out that 33 + 34 = 33.
    """
    print("effective atlas (what ERDLE reads with)")
    print(f"  shipped {shipped} + learned {learned} -> {len(merged)}")
    if learned and len(merged) <= shipped:
        print("  (the learned file adds nothing new -- either its "
              "characters are")
        print("   already shipped, or they are degenerate samples the "
              "merge refuses.")
        print("   Run `atlas.py prune --path <learned>` to clean it.)")


def render_signature(signature) -> list[str]:
    shades = " .:#"
    rows = []
    for row in range(CELL_HEIGHT):
        line = "".join(
            shades[min(signature[row * CELL_WIDTH + col], LEVELS - 1)]
            for col in range(CELL_WIDTH)
        )
        rows.append(line)
    return rows



def cmd_show(args) -> int:
    if args.path:
        path = Path(args.path)
        atlas = GlyphAtlas.load(path)
        print(f"{path}")
    else:
        path = learned_path()
        atlas, shipped, learned = effective_atlas()
        report_merge(atlas, shipped, learned)
        print(f"  learning into {path}")
    print(f"  {len(atlas)} characters, {atlas.total_samples} samples")
    if not len(atlas):
        print("  (empty -- play a few boss fights with Tesseract installed)")
        return 0

    have = set(atlas.samples)
    print(f"  alphabet: {atlas.alphabet}")
    missing = sorted(EXPECTED - have)
    if missing:
        print(f"  missing:  {''.join(missing)}")
        print(f"  coverage: {len(have & EXPECTED)}/{len(EXPECTED)}")
        print("  run `python tools/atlas.py plan` for which bosses to fight")
    else:
        # Counting characters is not a quality check, and saying "can be
        # shipped" on the strength of a count is how a corrupted atlas
        # got promoted twice. Ask the same question `seed_atlas` asks.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from seed_atlas import can_read_the_reference_plates
            failures = can_read_the_reference_plates(atlas)
        except Exception:
            failures = []
        if failures:
            print("  coverage: complete, but it CANNOT READ the reference "
                  "plates --")
            for failure in failures:
                print(f"    {failure}")
            print("  Mislabelled samples read confidently and wrongly. "
                  "Not shippable.")
        else:
            print("  coverage: complete, and the reference plates read back "
                  "-- shippable")

    # Which display sizes this atlas can actually serve.
    heights = sorted({h for samples in atlas.samples.values() for _, h in samples if h})
    if heights:
        bands = describe_bands(heights)
        print(f"  glyph heights: {heights[0]}-{heights[-1]}px")
        print(f"  serves: {bands}")

    collisions = find_case_collisions(atlas)
    if collisions:
        print()
        print(f"  !! {''.join(collisions)} hold the same shape as their lowercase form.")
        print("     These are stale entries from before case was preserved,")
        print("     and they will cause wrong letters. Fix with:")
        print()
        print("         python tools/atlas.py reset --yes")

    if args.render:
        for char in sorted(atlas.samples, key=lambda c: (c.lower(), c)):
            signature, height = atlas.samples[char][0]
            print(f"\n  '{char}'  (h={height}px, {len(atlas.samples[char])} samples)")
            for line in render_signature(signature):
                print(f"    |{line}|")
    return 0


def find_case_collisions(atlas) -> list[str]:
    """Characters whose upper and lower forms hold the same shape.

    A real font never draws 'R' and 'r' identically, so a match here means
    the atlas contains entries from before case was preserved -- a
    lowercase shape filed under a capital. Those samples are actively
    harmful: they make the wrong letter win.
    """
    from erdle.glyphs import hamming

    collisions = []
    for char, samples in atlas.samples.items():
        if not char.isupper():
            continue
        other = atlas.samples.get(char.lower())
        if not other:
            continue
        for signature, _ in samples:
            if any(hamming(signature, s) <= 2 for s, _ in other):
                collisions.append(char)
                break
    return sorted(collisions)


def cmd_reset(args) -> int:
    """Delete the learned atlas. Shell-agnostic, unlike rm/del.

    There are two atlases and this used to clear only one, which was
    confusing in exactly the wrong situation: after resetting to escape a
    corrupted atlas, `plan` still reported 46 characters, because the
    corrupted copy had already been seeded into `data/glyphs.json` and
    that is the one the app merges on top of. `--shipped` clears both.
    """
    from erdle.glyphs import default_atlas_path

    targets = [learned_path()]
    if args.shipped:
        targets.append(default_atlas_path())

    present = [path for path in targets if path.exists()]
    if not present:
        print("nothing to remove at:")
        for path in targets:
            print(f"  {path}")
        return 0
    if not args.yes:
        print("about to delete:")
        for path in present:
            print(f"  {path}")
        print("re-run with --yes to confirm")
        return 1
    for path in present:
        path.unlink()
        print(f"removed {path}")
    if not args.shipped:
        shipped = default_atlas_path()
        if shipped.exists():
            characters = len(GlyphAtlas.load(shipped))
            print(f"\nnote: {shipped} still holds {characters} characters "
                  f"and is merged on top of\n      whatever is learned next. "
                  f"Add --shipped to clear that too.")
    print("the atlas will rebuild from scratch on the next few fights")
    return 0


# A glyph is only matched against samples within MAX_HEIGHT_RATIO, so an
# atlas covers a display only if it holds samples of a similar size.
COMMON_DISPLAYS = {
    "720p": 768, "900p": 900, "1080p": 1080,
    "1200p": 1200, "1440p": 1440, "4K": 2160,
}


def describe_bands(heights: list[int]) -> str:
    """Which common resolutions this atlas has samples for.

    Boss-name glyphs run roughly 1.0-1.6% of screen height, so a display
    of H pixels produces glyphs of about H/70 to H/45.
    """
    from erdle.glyphs import MAX_HEIGHT_RATIO

    served = []
    for label, screen in sorted(COMMON_DISPLAYS.items(), key=lambda kv: kv[1]):
        low, high = screen / 90, screen / 40
        if any(
            low / MAX_HEIGHT_RATIO <= h <= high * MAX_HEIGHT_RATIO
            for h in heights
        ):
            served.append(label)
    return ", ".join(served) if served else "none -- run tools/learn.py --dir"


def cmd_prune(args) -> int:
    """Remove noise samples, keeping everything real.

    The alternative the tool used to offer for a case collision was
    `reset --yes`, which throws away every good sample to remove a few
    bad ones. Pruning is what you actually want.
    """
    path = Path(args.path) if args.path else learned_path()
    atlas = GlyphAtlas.load(path)
    if not len(atlas):
        print(f"nothing at {path}")
        return 1
    before = atlas.total_samples
    dropped = atlas.prune()
    if not dropped:
        print(f"{path}\n  nothing to prune ({before} samples)")
        return 0
    atlas.path = path
    atlas.save()
    print(f"{path}\n  dropped {dropped} of {before} samples; "
          f"{len(atlas)} characters remain")
    return 0


def cmd_plan(args) -> int:
    """Suggest which bosses to fight next to fill the atlas fastest.

    Greedy set cover. "Play until it is complete" is useless advice when
    the alphabet is 50-odd characters; naming the three fights that teach
    the most is actionable.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    atlas, shipped, learned = effective_atlas()
    known = set(atlas.samples)
    database = BossDatabase.load(default_data_path())
    print(f"shipped {shipped} + learned {learned} characters")

    # Only characters that actually appear in the roster are reachable.
    reachable = set()
    for entry in database:
        reachable |= set(entry.name)
    reachable.discard(" ")

    missing = reachable - known
    print(f"atlas has {len(known)} characters")
    print(f"the {len(database)} bosses in the table use {len(reachable)} distinct characters")
    if not missing:
        print("\nnothing left to learn from this roster -- run `ship`")
        return 0
    print(f"still missing: {''.join(sorted(missing))}\n")

    remaining = set(missing)
    have = set(known)
    step = 1
    while remaining:
        best, gain = None, 0
        for entry in database:
            new = (set(entry.name) - {" "}) - have
            if len(new) > gain:
                best, gain = entry, len(new)
        if best is None:
            break
        new = sorted((set(best.name) - {" "}) - have)
        print(f"  {step}. {best.name}")
        print(f"     teaches {gain}: {''.join(new)}")
        have |= set(best.name)
        remaining -= set(best.name)
        step += 1

    if remaining:
        print(f"\n  unreachable from this roster: {''.join(sorted(remaining))}")
        print("  (add more bosses to data/bosses.json)")
    return 0


def load_frame(path: Path):
    """A screenshot as a Frame, whatever size it is."""
    from PIL import Image

    from erdle.detect import Frame

    image = Image.open(path).convert("RGB")
    try:
        pixels = list(image.get_flattened_data())     # Pillow 11+
    except AttributeError:
        pixels = list(image.getdata())
    return Frame(image.width, image.height, pixels)


def cmd_learn(args) -> int:
    """Teach the atlas from a screenshot instead of from live play.

    The capture path cannot reach content the player has not got to yet,
    and posing for a boss you are twenty hours away from is a poor use of
    an evening. A screenshot has the same pixels.

    Reports *why* a sample was rejected. `learn_from_text` refuses
    anything whose glyph count disagrees with the name, which is correct
    -- one mislabelled sample lives in the atlas forever -- but silent,
    and a silent refusal is indistinguishable from a broken tool.
    """
    from erdle.autocal import band_for
    from erdle.config import Config
    from erdle.glyphs import learn_from_any_line, segment_glyphs, text_lines
    from erdle.matching import BossNameMatcher
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.ocr import DEFAULT_INK_THRESHOLD, TesseractRecogniser

    config = Config.load()
    database = BossDatabase.load(default_data_path())
    atlas = GlyphAtlas.load(learned_path())
    atlas.path = learned_path()

    matcher = BossNameMatcher.from_entries(database)
    before = len(atlas)
    thresholds = [args.threshold] if args.threshold else [200, 170, 230, 150]

    files = expand(args.images)
    if not files:
        print("no image files matched", file=sys.stderr)
        return 1

    total = 0
    for path in files:
        full = load_frame(path)
        crop, where = find_plate(full, config, args.dump and path)
        if crop is None:
            print(f"{path.name}: no text found in the name band or anywhere "
                  f"else in the image")
            continue
        if args.verbose or args.dump:
            print(f"{path.name}: {full.width}x{full.height}, using {where}")

        name = args.name
        source = "given"
        if name is None:
            name = identify(crop, matcher, thresholds)
            source = "read"
        if name is None:
            # Falling back to the filename makes batch learning trivial:
            # save the shots as "Crucible Knight Ordovis.png" and no
            # typing is needed. It also works with no Tesseract at all,
            # which is the position anyone new to the project is in.
            name = name_from_filename(path, matcher)
            source = "filename"
        if name is None:
            print(f"{path.name}: no boss name. Either name the file after "
                  f"the boss, or pass --name.")
            continue
        if source != "given":
            print(f"{path.name}: {source} -> {name!r}")

        learned_here = 0
        expected = [c for c in name if not c.isspace()]
        for threshold in thresholds:
            gained = learn_from_any_line(crop, name, atlas, threshold=threshold)
            if gained:
                learned_here += gained
                print(f"{path.name}: {name!r} -> learned {gained} glyphs "
                      f"at cutoff {threshold}")
                break
            boxes = segment_glyphs(crop, threshold=threshold)
            lines = text_lines(crop, threshold=threshold)
            print(f"{path.name}: cutoff {threshold} found {len(boxes)} glyphs "
                  f"across {len(lines)} line(s), {name!r} needs "
                  f"{len(expected)} -- rejected")
        total += learned_here

    if total:
        atlas.save()
        print(f"\n{len(atlas)} characters now ({len(atlas) - before} new), "
              f"saved to {learned_path()}")
    else:
        print("\nnothing learned")
        print("  Each line in the band was tried on its own, so extra text")
        print("  is not the problem. What refuses a sample now is glyphs")
        print("  that touch: they segment as one box and the count drops.")
        print("  Try --dump and look at the band, or a cleaner screenshot.")
    return 0


def identify(crop, matcher, thresholds):
    """Best-effort read of the name in a crop, for when --name is omitted."""
    from erdle.ocr import TesseractRecogniser

    ok, reason = TesseractRecogniser.availability()
    if not ok:
        return None
    reader = TesseractRecogniser()
    best = None
    for threshold in thresholds:
        try:
            text = reader.read(crop, threshold)
        except Exception:
            continue
        result = matcher.match(text)
        if result is not None and (best is None or result.confidence > best.confidence):
            best = result
    return best.display_name if best is not None else None



def name_from_filename(path: Path, matcher):
    """Match a screenshot's filename against the boss table."""
    stem = path.stem.replace("_", " ").replace("-", " ")
    result = matcher.match(stem)
    return result.display_name if result is not None else None



def expand(patterns) -> list:
    """Resolve arguments to real files, expanding wildcards ourselves.

    PowerShell does not glob for the program it launches, so
    `atlas.py learn shots\\*.png` arrives as one literal, unmatchable
    path. Doing it here means the command behaves the same in every
    shell.
    """
    import glob

    files = []
    for pattern in patterns:
        matches = [Path(m) for m in sorted(glob.glob(str(pattern)))]
        if matches:
            files.extend(m for m in matches if m.is_file())
        elif Path(pattern).is_file():
            files.append(Path(pattern))
        else:
            print(f"{pattern}: no such file", file=sys.stderr)
    return files


def find_plate(full, config, dump_to=None):
    """The crop containing the boss name, and how it was found.

    Tries the calibrated band first. Falls back to the whole image,
    because a screenshot is not always a full-screen grab -- a crop from
    a wiki, or a phone photo of a monitor, has no HUD geometry at all and
    the band would land on nothing.
    """
    from erdle.autocal import band_for
    from erdle.ocr import region_ink_fraction

    band = band_for(config).resolve(full.width, full.height)
    crop = full.region(band)
    ink = region_ink_fraction(full, band)
    if ink >= 0.002:
        if dump_to:
            save_crop(crop, dump_to)
        return crop, f"the calibrated name band (ink {ink:.3f})"

    if dump_to:
        save_crop(crop, dump_to)
    # No HUD where it should be: treat the file as an already-cropped plate.
    return full, f"the whole image (band was empty, ink {ink:.3f})"


def save_crop(frame, path: Path) -> None:
    """Write the crop out so a wrong region is visible, not guessed at."""
    from PIL import Image

    target = path.with_name(path.stem + "-band.png")
    image = Image.new("RGB", (frame.width, frame.height))
    image.putdata([frame.pixel(x, y)
                   for y in range(frame.height)
                   for x in range(frame.width)])
    image.save(target)
    print(f"  wrote {target.name} -- open it and check the name is in there")


def cmd_merge(args) -> int:
    merged = GlyphAtlas()
    for source in args.sources:
        atlas = GlyphAtlas.load(source)
        added = 0
        for char, samples in atlas.samples.items():
            for signature, height in samples:
                if merged.learn(char, signature, height):
                    added += 1
        print(f"  {source}: {len(atlas)} chars, {added} new samples")
    merged.path = Path(args.out)
    merged.save()
    print(f"\nwrote {args.out}: {len(merged)} characters, {merged.total_samples} samples")
    return 0


def cmd_ship(args) -> int:
    source = GlyphAtlas.load(learned_path())
    if not len(source):
        print("nothing learned yet", file=sys.stderr)
        return 1

    missing = sorted(EXPECTED - set(source.samples))
    if missing and not args.force:
        print(f"atlas is incomplete, missing: {''.join(missing)}", file=sys.stderr)
        print("play more, or pass --force", file=sys.stderr)
        return 1

    target = default_atlas_path()
    existing = GlyphAtlas.load(target)
    for char, samples in source.samples.items():
        for signature, height in samples:
            existing.learn(char, signature, height)
    existing.path = target
    existing.save()
    print(f"shipped to {target}: {len(existing)} characters")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="report what has been learned")
    show.add_argument("path", nargs="?", default=None)
    show.add_argument("--render", action="store_true", help="draw each glyph")
    show.set_defaults(func=cmd_show)

    merge = sub.add_parser("merge", help="combine several atlases")
    merge.add_argument("sources", nargs="+")
    merge.add_argument("--out", default="data/glyphs.json")
    merge.set_defaults(func=cmd_merge)

    plan = sub.add_parser("plan", help="which bosses to fight next")
    plan.set_defaults(func=cmd_plan)

    learn = sub.add_parser(
        "learn", help="teach the atlas from screenshots of boss name plates"
    )
    learn.add_argument("images", nargs="+", help="screenshot files")
    learn.add_argument("--name", help="the boss name, if OCR cannot read it")
    learn.add_argument("--threshold", type=int, help="one brightness cutoff")
    learn.add_argument("--dump", action="store_true",
                       help="write out the cropped band for inspection")
    learn.add_argument("--verbose", action="store_true",
                       help="report the region used for each file")
    learn.set_defaults(func=cmd_learn)

    prune = sub.add_parser(
        "prune", help="drop degenerate samples without losing the rest")
    prune.add_argument("--path", default=None)
    prune.set_defaults(func=cmd_prune)

    reset = sub.add_parser("reset", help="delete the learned atlas")
    reset.add_argument(
        "--shipped", action="store_true",
        help="also clear data/glyphs.json, the atlas the exe bundles",
    )
    reset.add_argument("--yes", action="store_true", help="confirm")
    reset.set_defaults(func=cmd_reset)

    ship = sub.add_parser("ship", help="bake the learned atlas into data/")
    ship.add_argument("--force", action="store_true")
    ship.set_defaults(func=cmd_ship)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
