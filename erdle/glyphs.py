"""Font-specific text recognition, to replace Tesseract.

Elden Ring draws boss names in one fixed font. General OCR throws that
away and solves a much harder problem badly: on a real capture Tesseract
returned nothing at three of seven brightness cutoffs and 63% confidence
at a fourth.

The observation that makes this tractable: we do not need to pre-render
165 *names*. Across every boss in the game there are only about forty
distinct *characters*. Learn those once and any name becomes readable,
including bosses the atlas has never seen.

Nor do we need FromSoftware's font file. The glyphs are learned from the
game's own output: whenever Tesseract does read a name confidently, the
plate is segmented and each glyph is filed under the letter it must be.
After a handful of fights the atlas is complete and Tesseract is never
consulted again. Users who never had it installed can be shipped the
resulting atlas as ordinary JSON.

Pipeline:

    plate -> column projection -> glyph boxes -> normalise each to a
    fixed grid -> nearest neighbour in the atlas -> string

Normalising to a fixed grid is what makes it resolution-independent: a
30px-tall glyph at 4K and a 15px one at 1080p reduce to the same bits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .detect import Frame
from .geometry import PixelRect
from .ocr import DEFAULT_INK_THRESHOLD, _luma

# Grid every glyph is squashed onto before comparison. Small enough that
# antialiasing differences wash out, large enough to keep E/F, O/Q and
# I/l apart.
CELL_WIDTH = 8
CELL_HEIGHT = 12
CELL_BITS = CELL_WIDTH * CELL_HEIGHT
# Coverage levels per cell. Four is enough to keep scale changes from
# flipping cells wholesale, without inflating the stored atlas.
LEVELS = 4
MAX_DISTANCE_PER_CELL = LEVELS - 1
# Set from measurement: see tools/measure_glyphs.py. Same-letter
# distances across scales sit well below this, different letters well
# above.
DEFAULT_MAX_DISTANCE = 10
# Samples are only compared when their glyph heights are within this
# factor of each other. Measured: at a fixed scale, the same letter
# matches at distance 0 while the closest different pair (M/N) sits at
# 14, so separation is total. Across a 4x scale change the two ranges
# overlap. Since a player's resolution does not change mid-session, the
# fix is to compare like with like rather than to loosen the threshold.
MAX_HEIGHT_RATIO = 1.5


@dataclass(frozen=True)
class GlyphBox:
    left: int
    top: int
    right: int
    bottom: int
    space_before: bool = False

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def column_ink_counts(frame: Frame, threshold: int) -> list[int]:
    counts = [0] * frame.width
    for y in range(frame.height):
        for x in range(frame.width):
            if _luma(frame.pixel(x, y)) >= threshold:
                counts[x] += 1
    return counts


def segment_glyphs(
    frame: Frame,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
    space_ratio: float = 0.7,
    min_width: int = 1,
    on_text_line: bool = True,
    join_fragments: bool = True,
) -> list[GlyphBox]:
    """Split a name plate into character boxes.

    Columns holding ink form runs; the gaps between runs are either
    letter spacing or word spacing. Rather than hard-code a pixel width --
    which would break at every resolution -- the split is decided relative
    to the median gap actually present in this plate.
    """
    if frame.width == 0 or frame.height == 0:
        return []

    counts = column_ink_counts(frame, threshold)

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x, count in enumerate(counts):
        if count > 0 and start is None:
            start = x
        elif count == 0 and start is not None:
            if x - start >= min_width:
                runs.append((start, x))
            start = None
    if start is not None and frame.width - start >= min_width:
        runs.append((start, frame.width))

    if not runs:
        return []

    boxes: list[GlyphBox] = []
    for left, right in runs:
        top, bottom = _vertical_extent(frame, left, right, threshold)
        if bottom <= top:
            continue
        boxes.append(GlyphBox(left, top, right, bottom, False))

    # Discard scenery before measuring anything. Word spacing is derived
    # from median glyph width, and on a bright scene the band caught
    # fifteen one-to-eight-pixel specks of lava that dragged that median
    # from 18 down to 6 -- which made an ordinary letter gap look like a
    # word space, and stopped broken letters from being rejoined.
    if on_text_line:
        boxes = _on_the_text_line(boxes)
        boxes = _largest_cluster(boxes)
    if join_fragments:
        boxes = _join_fragments(boxes, frame, threshold)

    return _mark_spaces(boxes, space_ratio, frame.height)


def _mark_spaces(
    boxes: list[GlyphBox], space_ratio: float, fallback: int
) -> list[GlyphBox]:
    """Flag the gaps wide enough to be word breaks.

    Judged against glyph width, not against other gaps. Measuring
    relative to the median gap breaks on "A B C", where every gap is a
    word space and so the median is one too -- nothing then looks wide
    enough and the spaces all disappear. Character width is a stable
    reference regardless of how many spaces the string happens to have.

    The ratio is 0.7 rather than 0.5 because letter gaps are wider than
    they look. On a real 4K plate the gaps flanking a narrow "i" reached
    11px against a word space of 13, so a 0.5 threshold of 9 split
    "Devouring" and "Serpent" in half. 0.7 puts the line at 12, between
    the two.

    That is a two-pixel margin, and it is the least comfortable number in
    this file. A wrong space costs one edit of distance to the matcher
    rather than a wrong letter, so it degrades gently -- but a font with
    tighter word spacing than Elden Ring's would need this revisited.
    """
    if not boxes:
        return boxes
    widths = sorted(box.width for box in boxes)
    median_width = widths[len(widths) // 2] or fallback
    space_gap = max(int(median_width * space_ratio), 2)

    marked = [boxes[0]]
    for index in range(1, len(boxes)):
        box = boxes[index]
        gap = box.left - boxes[index - 1].right
        marked.append(GlyphBox(box.left, box.top, box.right, box.bottom,
                               gap >= space_gap))
    return marked


#: A box whose baseline sits further than this from the plate's own
#: baseline, as a fraction of glyph height, is not part of the name.
BASELINE_TOLERANCE = 0.5


def _on_the_text_line(boxes: list[GlyphBox]) -> list[GlyphBox]:
    """Drop everything that does not sit on the name's baseline.

    The name band is a fixed rectangle, so on a bright scene it catches
    scenery as well as text -- a real capture of "God-Devouring Serpent"
    segmented into 40 boxes, of which 15 were lava and scales off to the
    right. Every one of those was filed as a glyph candidate, and the
    count mismatch then threw the whole plate away.

    Text shares a baseline; background does not. Boxes are kept when
    their bottom edge is near the most common bottom edge, which needs no
    threshold tuning and no assumption about where in the band the name
    sits. Descenders (g, p, y) hang below and are kept by the tolerance.
    """
    if len(boxes) < 3:
        return boxes

    counts: dict[int, int] = {}
    for box in boxes:
        # Round to a small bucket so near-identical baselines agree.
        counts[box.bottom // 4] = counts.get(box.bottom // 4, 0) + 1
    baseline = max(counts, key=lambda k: (counts[k], k)) * 4 + 2

    heights = sorted(box.height for box in boxes)
    typical = heights[len(heights) // 2] or 1
    slack = max(4, int(typical * BASELINE_TOLERANCE))
    # Descenders reach below the baseline by more than they rise above it.
    kept = [box for box in boxes
            if -slack <= box.bottom - baseline <= slack * 2]
    return kept or boxes


#: A gap this many times the plate's median gap is not letter spacing;
#: it is a different thing on the screen altogether.
ISOLATION = 8


def _largest_cluster(boxes: list[GlyphBox]) -> list[GlyphBox]:
    """Keep the run of boxes that actually forms the name.

    The baseline test alone let one piece of scenery through, because it
    happened to end at the same height as the hyphen. Distance settles
    what height cannot: the stray sat 276px past the last letter on a
    plate whose letters are five pixels apart. Letters in a name are
    close together, and a word space is nowhere near this wide.
    """
    if len(boxes) < 4:
        return boxes

    gaps = sorted(boxes[i + 1].left - boxes[i].right
                  for i in range(len(boxes) - 1))
    median_gap = max(gaps[len(gaps) // 2], 1)
    limit = median_gap * ISOLATION

    groups: list[list[GlyphBox]] = [[boxes[0]]]
    for box in boxes[1:]:
        if box.left - groups[-1][-1].right > limit:
            groups.append([box])
        else:
            groups[-1].append(box)
    # Widest, not longest: a name is the thing occupying the plate.
    return max(groups, key=lambda g: (len(g), g[-1].right - g[0].left))


def _join_fragments(
    boxes: list[GlyphBox], frame: Frame, threshold: int
) -> list[GlyphBox]:
    """Rejoin letters split at a thin horizontal stroke.

    `n`, `u` and `m` are two stems bridged by an arch. When the arch is
    anti-aliased below the ink threshold the column projection sees two
    runs, and one word can lose three characters that way -- "God-
    Devouring Serpent" segmented as 23 glyphs for 20 letters, with `u`
    once and `n` twice split in half.

    A fragment is recognised by its gap, not its shape: the pieces of a
    broken letter sit far closer together than two real letters do. The
    comparison is against this plate's own median gap, so it holds at any
    resolution.
    """
    if len(boxes) < 4:
        return boxes

    gaps = sorted(boxes[i + 1].left - boxes[i].right
                  for i in range(len(boxes) - 1))
    median_gap = gaps[len(gaps) // 2]
    if median_gap <= 0:
        return boxes
    # Measured on a real 4K plate: the three broken letters sat at gaps
    # of 2, 2 and 3 against a median of 5, while every genuine letter
    # pair was 4 or more. The rule has to admit 3 and refuse 4, which
    # puts the fraction between 0.55 and 0.72; 0.6 sits in the middle of
    # that window. At 0.8 it merged "ri" and "nt" as well and the plate
    # came out three letters short.
    limit = median_gap * 0.6

    # A second condition, because a gap alone is circumstantial: the
    # halves of a broken letter must add up to something a letter's
    # width. Deliberately cautious -- this path runs when nothing is
    # known about the text, so a wrong merge is a wrong letter with no
    # way to notice. `learn_from_text` knows the answer and can afford to
    # be bolder; see `_merge_to_fit`.
    widths = sorted(box.width for box in boxes)
    width_ceiling = widths[len(widths) // 2] * 1.5

    joined: list[GlyphBox] = [boxes[0]]
    for box in boxes[1:]:
        previous = joined[-1]
        merged_width = box.right - previous.left
        if (box.left - previous.right <= limit
                and merged_width <= width_ceiling):
            joined[-1] = GlyphBox(
                previous.left, min(previous.top, box.top),
                box.right, max(previous.bottom, box.bottom),
                previous.space_before,
            )
        else:
            joined.append(box)
    return joined


def _vertical_extent(
    frame: Frame, left: int, right: int, threshold: int
) -> tuple[int, int]:
    top, bottom = frame.height, 0
    for y in range(frame.height):
        for x in range(left, right):
            if _luma(frame.pixel(x, y)) >= threshold:
                top = min(top, y)
                bottom = max(bottom, y + 1)
                break
    return top, bottom


def normalise_glyph(
    frame: Frame,
    box: GlyphBox,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
    width: int = CELL_WIDTH,
    height: int = CELL_HEIGHT,
) -> tuple[int, ...]:
    """Squash one glyph onto a fixed grid of bits.

    Area sampling rather than nearest neighbour: at low resolution a thin
    stroke can fall between sample points and vanish entirely, which turns
    E into F.
    """
    if box.width <= 0 or box.height <= 0:
        return tuple([0] * (width * height))

    bits: list[int] = []
    for row in range(height):
        y0 = box.top + (box.height * row) // height
        y1 = box.top + (box.height * (row + 1)) // height
        y1 = max(y1, y0 + 1)
        for col in range(width):
            x0 = box.left + (box.width * col) // width
            x1 = box.left + (box.width * (col + 1)) // width
            x1 = max(x1, x0 + 1)

            lit = total = 0
            for y in range(y0, min(y1, frame.height)):
                for x in range(x0, min(x1, frame.width)):
                    total += 1
                    if _luma(frame.pixel(x, y)) >= threshold:
                        lit += 1
            # Quantised coverage, not a bit. Thresholding each cell to 0/1
            # throws away exactly the information that makes the signature
            # survive a scale change: a stroke covering 40% of a cell at
            # one resolution and 60% at another flips the bit, but barely
            # moves the coverage. Measured, this is the difference between
            # same-letter and different-letter distances overlapping and
            # being cleanly separable.
            coverage = (lit / total) if total else 0.0
            bits.append(min(LEVELS - 1, int(coverage * LEVELS)))
    return tuple(bits)


def _comparable(a: int, b: int) -> bool:
    """Are two glyph heights close enough to be worth comparing?"""
    if a <= 0 or b <= 0:
        return True          # unknown height: fall back to comparing anyway
    lo, hi = (a, b) if a <= b else (b, a)
    return (hi / lo) <= MAX_HEIGHT_RATIO


def hamming(a: Sequence[int], b: Sequence[int]) -> int:
    """Distance between two signatures.

    Named for history; it is now an L1 distance over quantised coverage,
    which is the same thing when the levels are 0 and 1.
    """
    return sum(abs(x - y) for x, y in zip(a, b))


@dataclass
class GlyphAtlas:
    """Learned character shapes.

    A character may have several stored samples -- different resolutions
    and antialiasing produce slightly different bits -- and matching takes
    the nearest across all of them.
    """

    # char -> list of (signature, glyph height in source pixels)
    samples: dict[str, list[tuple[tuple[int, ...], int]]] = field(
        default_factory=dict
    )
    path: Path | None = None
    max_samples_per_char: int = 6

    def __len__(self) -> int:
        return len(self.samples)

    def __contains__(self, char: object) -> bool:
        return char in self.samples

    def prune(self, min_height: int | None = None) -> int:
        """Drop degenerate samples. Returns how many went.

        Kept separate from `learn` because an atlas already on disk needs
        cleaning too, and resetting it would throw away every good sample
        to remove seven bad ones.
        """
        floor = self.MIN_GLYPH_HEIGHT if min_height is None else min_height
        removed = 0
        for char in list(self.samples):
            if char in self.SHORT_BY_NATURE:
                continue
            kept = [(sig, h) for sig, h in self.samples[char]
                    if not (0 < h < floor)]
            removed += len(self.samples[char]) - len(kept)
            if kept:
                self.samples[char] = kept
            else:
                del self.samples[char]
        return removed

    @property
    def alphabet(self) -> str:
        return "".join(sorted(self.samples))

    @property
    def total_samples(self) -> int:
        return sum(len(v) for v in self.samples.values())

    #: Below this many pixels tall, a "glyph" is segmentation noise: a
    #: stray row of anti-aliasing or a bar edge clipped into the band. The
    #: shipped atlas had seven such samples, one to three pixels high, and
    #: they are worse than useless -- normalised onto the 8x12 grid they
    #: become near-uniform smears that sit close to *everything*, so `R`
    #: ended up holding the same shape as `r`.
    MIN_GLYPH_HEIGHT = 6

    #: Exempt from the height floor, because they really are that short.
    #: A hyphen on a 4K name plate is two pixels tall -- the same height
    #: as the noise the floor exists to reject -- so a blanket rule threw
    #: away the one character "God-Devouring Serpent" was being fought
    #: for. Height cannot separate these; the label can.
    SHORT_BY_NATURE = frozenset("-_,.'\"`~:;")

    def learn(self, char: str, signature: tuple[int, ...], height: int = 0) -> bool:
        """File a sample. Returns True if it was new information."""
        if not char or len(char) != 1 or char == " ":
            return False
        # `height` is 0 for callers that do not track it (older atlases,
        # synthetic tests), and those are left alone.
        if (0 < height < self.MIN_GLYPH_HEIGHT
                and char not in self.SHORT_BY_NATURE):
            return False
        existing = self.samples.setdefault(char, [])
        # A sample almost identical to one already held at a similar size
        # teaches nothing.
        for other, other_height in existing:
            if _comparable(height, other_height) and hamming(signature, other) <= 3:
                return False
        if len(existing) >= self.max_samples_per_char:
            existing.pop(0)
        existing.append((signature, int(height)))
        return True

    def match(
        self,
        signature: tuple[int, ...],
        *,
        height: int = 0,
        max_distance: int = DEFAULT_MAX_DISTANCE,
    ) -> tuple[str | None, int]:
        """Nearest character, and its distance."""
        best_char: str | None = None
        best_distance = CELL_BITS * MAX_DISTANCE_PER_CELL + 1
        for char, stored in self.samples.items():
            for other, other_height in stored:
                if not _comparable(height, other_height):
                    continue
                distance = hamming(signature, other)
                if distance < best_distance:
                    best_distance, best_char = distance, char
        if best_char is None or best_distance > max_distance:
            return None, best_distance
        return best_char, best_distance

    # --- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "cell": {"width": CELL_WIDTH, "height": CELL_HEIGHT},
            "glyphs": {
                char: [
                    {"bits": "".join(f"{v:x}" for v in sig), "h": h}
                    for sig, h in sigs
                ]
                for char, sigs in sorted(self.samples.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "GlyphAtlas":
        atlas = cls()
        if not isinstance(payload, dict):
            return atlas
        cell = payload.get("cell", {})
        if isinstance(cell, dict):
            expected = cell.get("width", CELL_WIDTH), cell.get("height", CELL_HEIGHT)
            if expected != (CELL_WIDTH, CELL_HEIGHT):
                # Grid changed since this file was written; the bits are
                # meaningless now. Better empty than confidently wrong.
                return atlas
        glyphs = payload.get("glyphs", {})
        if not isinstance(glyphs, dict):
            return atlas
        for char, sigs in glyphs.items():
            if not isinstance(char, str) or len(char) != 1:
                continue
            if not isinstance(sigs, list):
                continue
            for item in sigs:
                if isinstance(item, dict):
                    text, height = item.get("bits", ""), int(item.get("h", 0))
                else:
                    text, height = item, 0
                if not isinstance(text, str) or len(text) != CELL_BITS:
                    continue
                try:
                    values = tuple(int(c, 16) for c in text)
                except ValueError:
                    continue
                if all(0 <= v < LEVELS for v in values):
                    atlas.samples.setdefault(char, []).append((values, height))
        return atlas

    @classmethod
    def load(cls, path: str | Path) -> "GlyphAtlas":
        target = Path(path)
        if not target.exists():
            atlas = cls()
            atlas.path = target
            return atlas
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            atlas = cls()
            atlas.path = target
            return atlas
        atlas = cls.from_dict(payload)
        atlas.path = target
        return atlas

    def save(self, path: str | Path | None = None) -> Path:
        import os
        import tempfile

        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("no path to save to")
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(target.parent),
            prefix=target.name, suffix=".tmp", delete=False,
        )
        try:
            with handle as stream:
                json.dump(self.to_dict(), stream, indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, target)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        self.path = target
        return target


def read_text(
    frame: Frame,
    atlas: GlyphAtlas,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> tuple[str, float]:
    """Read a plate with the atlas. Returns (text, fraction recognised).

    Unrecognised glyphs become '?' rather than being dropped, so the
    fuzzy name matcher downstream still sees the right string length --
    it copes with substitutions far better than with missing characters.
    """
    boxes = segment_glyphs(frame, threshold=threshold)
    if not boxes:
        return "", 0.0

    characters: list[str] = []
    recognised = 0
    index = 0
    while index < len(boxes):
        box = boxes[index]
        if box.space_before:
            characters.append(" ")
        signature = normalise_glyph(frame, box, threshold=threshold)
        char, _ = atlas.match(
            signature, height=box.height, max_distance=max_distance
        )

        if char is None and index + 1 < len(boxes):
            # Nothing matched. Before giving up, try this box joined to
            # the next one: a letter broken at a thin stroke -- `n`, `u`,
            # `v`, `h` -- presents as two fragments that match nothing,
            # while the pair together matches perfectly.
            #
            # The atlas validates the merge, which is what makes this
            # safe. `_join_fragments` has to decide from geometry alone
            # and gets "Night's Cavalry" wrong in both directions; here a
            # wrong merge simply fails to match and is discarded.
            merged = _join(box, boxes[index + 1])
            if not boxes[index + 1].space_before:
                candidate, _ = atlas.match(
                    normalise_glyph(frame, merged, threshold=threshold),
                    height=merged.height, max_distance=max_distance,
                )
                if candidate is not None:
                    characters.append(candidate)
                    recognised += 1
                    index += 2
                    continue

        characters.append("?" if char is None else char)
        recognised += char is not None
        index += 1
    return "".join(characters), recognised / max(len(boxes), 1)


def _join(left: GlyphBox, right: GlyphBox) -> GlyphBox:
    return GlyphBox(
        left.left, min(left.top, right.top),
        right.right, max(left.bottom, right.bottom),
        left.space_before,
    )


def learn_from_text(
    frame: Frame,
    text: str,
    atlas: GlyphAtlas,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
) -> int:
    """Teach the atlas using a plate whose text is already known.

    Only accepts a sample when the segmentation produces exactly as many
    glyphs as the text has non-space characters. A mismatch means the
    split went wrong somewhere, and filing those under the wrong letters
    would poison the atlas permanently.
    """
    boxes = segment_glyphs(frame, threshold=threshold)
    expected = [c for c in text if not c.isspace()]
    if len(boxes) > len(expected):
        # The label is the ground truth here, which the unlabelled path
        # never has. Rather than tune a merge rule until it happens to
        # produce the right count everywhere, close the gap directly:
        # join the tightest pairs until the count agrees.
        boxes = _merge_to_fit(boxes, len(expected))
    if not boxes or len(boxes) != len(expected):
        # All-or-nothing on purpose: a mismatch means the split went
        # wrong, and filing glyphs under the wrong letters would poison
        # the atlas permanently. The cost is that one awkward mark can
        # discard a whole plate -- names with a hyphen or apostrophe are
        # the usual casualties, and those are exactly the characters that
        # stay missing longest. `last_refusal` records it so
        # `tools/atlas.py` can say why nothing was learned instead of
        # leaving the counter mysteriously flat.
        learn_from_text.last_refusal = (
            f"segmented {len(boxes)} glyphs, expected {len(expected)} "
            f"for {text!r}"
        )
        return 0

    learn_from_text.last_refusal = None

    learned = 0
    for box, char in zip(boxes, expected):
        signature = normalise_glyph(frame, box, threshold=threshold)
        if atlas.learn(char, signature, box.height):
            learned += 1
    return learned



learn_from_text.last_refusal = None


def _merge_to_fit(boxes: list[GlyphBox], wanted: int) -> list[GlyphBox]:
    """Join the closest pairs until there are as many boxes as letters.

    A letter broken at a thin stroke leaves its halves far closer
    together than two real letters ever are, so the tightest gaps are the
    breaks -- but *how* tight varies with the font, the resolution and
    which letters are involved, and every fixed threshold tried here was
    wrong on one plate or another. "Night's Cavalry" needed a broken `h`
    of 19px rejoined while a median glyph was 11; loosening the rule
    enough to allow that merged real letters on the same plate.

    Knowing the answer removes the guesswork. Merge the smallest gap,
    repeat, stop when the counts agree. If the result is wrong the count
    check downstream still has to pass, and a plate that needs more
    merges than it has small gaps is refused as before.
    """
    boxes = list(boxes)
    # Merging enough times will make *any* segmentation fit the label,
    # including a plate that was never text. That is precisely the
    # corruption the count check exists to prevent, so the licence is
    # bounded: a real plate needs one or two joins, and a synthetic
    # stipple that segments into twenty pieces for a twelve-letter name
    # stays refused.
    budget = max(1, len(boxes) // 8)
    all_gaps = sorted(boxes[i + 1].left - boxes[i].right
                      for i in range(len(boxes) - 1))
    median_gap = all_gaps[len(all_gaps) // 2] if all_gaps else 0

    while len(boxes) > wanted and budget > 0:
        gaps = [(boxes[i + 1].left - boxes[i].right, i)
                for i in range(len(boxes) - 1)]
        # Never merge across a word space: those gaps are the widest on
        # the plate and joining them would splice two words together.
        # And never merge a gap that is not unusually tight -- a break in
        # a letter is always narrower than the letter spacing around it.
        gaps = [(gap, i) for gap, i in gaps
                if not boxes[i + 1].space_before and gap <= median_gap]
        if not gaps:
            break
        budget -= 1
        _, index = min(gaps)
        boxes[index:index + 2] = [_join(boxes[index], boxes[index + 1])]
    return boxes


def text_lines(
    frame: Frame,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
    min_rows: int = 4,
    min_gap: int = 2,
) -> list[tuple[int, int]]:
    """Row spans that contain ink, one per line of text.

    The name band is deliberately generous, so it can hold more than the
    name: a duo fight stacks two health bars and two names, and a
    screenshot from a video may carry a caption or a watermark. Segmenting
    the whole band then finds far more glyphs than the name has, and the
    sample is refused -- correctly, but for a fixable reason.

    Splitting into lines first lets each be tried on its own.
    """
    if frame.width == 0 or frame.height == 0:
        return []

    inked = []
    for y in range(frame.height):
        row = frame.scanline(y, 0, frame.width)
        inked.append(any(_luma(pixel) >= threshold for pixel in row))

    spans: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for y, has_ink in enumerate(inked):
        if has_ink:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap:
                if y - gap - start >= min_rows:
                    spans.append((start, y - gap))
                start = None
                gap = 0
    if start is not None and frame.height - start >= min_rows:
        spans.append((start, frame.height))
    return spans


def learn_from_any_line(
    frame: Frame,
    text: str,
    atlas: "GlyphAtlas",
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
) -> int:
    """Learn from whichever line of the frame fits the text.

    Lines are separated *first* when there is more than one, never after.
    `segment_glyphs` counts vertical runs of inked columns, so two stacked
    names merge into boxes spanning both rows -- and the resulting count
    can match the expected length by coincidence, at which point the atlas
    learns half of one letter stacked on half of another and is silently
    poisoned. Trying the whole frame first was exactly that mistake; a
    test caught it holding a "C" made of two different glyphs.
    """
    lines = text_lines(frame, threshold=threshold)
    if len(lines) <= 1:
        return learn_from_text(frame, text, atlas, threshold=threshold)

    for top, bottom in lines:
        line = frame.region(PixelRect(0, top, frame.width, bottom))
        learned = learn_from_text(line, text, atlas, threshold=threshold)
        if learned:
            return learned
    return 0


def default_atlas_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "glyphs.json"
