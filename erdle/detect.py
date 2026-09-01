"""Boss health bar detection from captured frames.

Everything here is pure: it takes a frame (a row-major sequence of RGB
tuples plus dimensions) and returns observations. No screen capture, no
game process, no I/O. That keeps the risky, hardware-dependent part of the
system trivially testable with synthetic frames.

The colour thresholds are the one genuinely uncertain part of this module.
They were chosen from Elden Ring's boss bar palette (a dark desaturated
red on near-black) but MUST be confirmed against real captures. See
`erdle.calibrate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .geometry import BOSS_BAR, FractionalRect, PixelRect

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class BarThresholds:
    """Tunable classification limits for boss-bar pixels.

    Defaults verified against a real 3840x2160 capture, where the filled
    bar measured (80, 0, 0) -- considerably darker and far more saturated
    than it looks on screen. `health_min_red` is set below that with
    margin; the ratio is generous because green and blue are effectively
    zero, so anything with real colour in it is not the bar.
    """

    # A "health" pixel: red-dominant and bright enough to not be shadow.
    health_min_red: int = 48
    health_red_ratio: float = 1.8  # red must exceed green and blue by this
    # A "depleted" pixel: dark, low saturation -- the empty bar track.
    # Only used as a fallback; fill is measured from the health run.
    depleted_max_channel: int = 70
    # Fraction of the scanline that must classify as bar (health or
    # depleted) before an all-dark region is believed to be an empty bar.
    min_bar_coverage: float = 0.90
    # Guard against a fully-black frame (loading screen) reading as a bar.
    min_health_or_structure: float = 0.02
    # How far into the region the health run may start, as a fraction of
    # region width -- absorbs the bar's ornamental end cap.
    left_edge_tolerance: float = 0.06
    # The health run must be at least this fraction of the region wide.
    # Without a floor, one reddish terrain pixel near the left edge is
    # enough to declare a boss fight -- which is exactly what happened.
    min_fill_run: float = 0.018
    # How many sampled scanlines must independently find a similar run.
    # A real bar is identical row to row; terrain is not, so agreement is
    # the strongest cheap discriminator between them.
    min_row_agreement: int = 2
    row_agreement_tolerance: float = 0.10
    # Largest gap the run may bridge, as a fraction of region width --
    # wide enough for a spell effect drawn across the bar. Density, not
    # this, is what keeps scattered terrain from chaining into a run.
    max_run_gap: float = 0.02
    # Fraction of the run that must actually be health-coloured. This is
    # the real discriminator: noise bridges gaps but is never dense.
    min_run_density: float = 0.85


DEFAULT_THRESHOLDS = BarThresholds()


@dataclass(frozen=True)
class BarObservation:
    present: bool
    fill_ratio: float
    coverage: float
    health_pixels: int
    depleted_pixels: int
    sampled_pixels: int

    @property
    def percent(self) -> int:
        return int(round(self.fill_ratio * 100))


class Frame:
    """A captured frame, addressable as row-major RGB triples.

    Two backings. A plain list of tuples (used by tests and by crops), or
    a raw BGRA buffer straight from the capture API.

    The buffer form exists because building tuples is what actually costs
    money: a 4K HUD strip is 432k pixels and converting all of them takes
    ~100ms, which alone blows the frame budget. But `analyse_bar` reads
    three scanlines, and OCR reads one small crop -- a few thousand pixels
    between them. Deferring tuple construction to the pixels that are
    genuinely read turns that 100ms into under 2ms.
    """

    __slots__ = ("width", "height", "_pixels", "_buffer", "_stride")

    def __init__(self, width: int, height: int, pixels: Sequence[RGB]) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        if len(pixels) != width * height:
            raise ValueError(
                f"expected {width * height} pixels, got {len(pixels)}"
            )
        self.width = width
        self.height = height
        self._pixels: Sequence[RGB] | None = pixels
        self._buffer: bytes | None = None
        self._stride = 0

    @classmethod
    def from_bgra(
        cls, data, width: int, height: int, stride: int | None = None
    ) -> "Frame":
        """Wrap a raw BGRA buffer without converting anything yet."""
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        stride = stride if stride is not None else width * 4
        if len(data) < stride * height:
            raise ValueError(
                f"buffer holds {len(data)} bytes, need {stride * height}"
            )
        frame = cls.__new__(cls)
        frame.width = width
        frame.height = height
        frame._pixels = None
        frame._buffer = data
        frame._stride = stride
        return frame

    @property
    def is_lazy(self) -> bool:
        return self._buffer is not None

    def pixel(self, x: int, y: int) -> RGB:
        if self._pixels is not None:
            return self._pixels[y * self.width + x]
        i = y * self._stride + x * 4
        buffer = self._buffer
        return (buffer[i + 2], buffer[i + 1], buffer[i])

    def scanline(self, y: int, left: int, right: int) -> list[RGB]:
        if self._pixels is not None:
            base = y * self.width
            return list(self._pixels[base + left : base + right])
        buffer = self._buffer
        start = y * self._stride + left * 4
        stop = y * self._stride + right * 4
        return [
            (buffer[i + 2], buffer[i + 1], buffer[i])
            for i in range(start, stop, 4)
        ]

    def region(self, rect: PixelRect) -> "Frame":
        pixels: list[RGB] = []
        for y in range(rect.top, rect.bottom):
            pixels.extend(self.scanline(y, rect.left, rect.right))
        return Frame(rect.width, rect.height, pixels)


def is_health_pixel(rgb: RGB, thresholds: BarThresholds = DEFAULT_THRESHOLDS) -> bool:
    red, green, blue = rgb
    if red < thresholds.health_min_red:
        return False
    ratio = thresholds.health_red_ratio
    return red >= green * ratio and red >= blue * ratio


def is_depleted_pixel(rgb: RGB, thresholds: BarThresholds = DEFAULT_THRESHOLDS) -> bool:
    return all(channel <= thresholds.depleted_max_channel for channel in rgb)


def analyse_bar(
    frame: Frame,
    *,
    region: FractionalRect = BOSS_BAR,
    thresholds: BarThresholds = DEFAULT_THRESHOLDS,
    scanlines: int = 3,
) -> BarObservation:
    """Decide whether a boss bar is on screen, and how full it is.

    Samples several horizontal scanlines through the bar and takes the
    median fill, which shrugs off the bar's gradient edges and the odd
    particle effect drawn over it.
    """
    rect = region.resolve(frame.width, frame.height)
    if rect.height <= 0 or rect.width <= 0:
        return BarObservation(False, 0.0, 0.0, 0, 0, 0)

    rows = _scanline_rows(rect, scanlines)

    per_row_fill: list[float] = []
    total_health = 0
    total_depleted = 0
    total_sampled = 0

    for y in rows:
        line = frame.scanline(y, rect.left, rect.right)
        if not line:
            continue
        health = sum(1 for px in line if is_health_pixel(px, thresholds))
        depleted = sum(
            1
            for px in line
            if not is_health_pixel(px, thresholds) and is_depleted_pixel(px, thresholds)
        )
        total_health += health
        total_depleted += depleted
        total_sampled += len(line)

        # Fill comes from the length of the health run anchored at the
        # left edge, NOT from health/(health+depleted). The empty track's
        # colour varies with whatever the bar is drawn over, so counting
        # on it to classify as "depleted" makes detection fail the moment
        # the boss takes damage -- the exact case that matters.
        run = _left_anchored_run(line, thresholds)
        if run is not None:
            per_row_fill.append(run / len(line))

    if total_sampled == 0:
        return BarObservation(False, 0.0, 0.0, 0, 0, 0)

    coverage = (total_health + total_depleted) / total_sampled

    # A run only counts if it is long enough to be a health bar rather
    # than a speck of red dirt.
    candidates = [f for f in per_row_fill if f >= thresholds.min_fill_run]

    # Cannot demand agreement between more rows than were sampled.
    required = max(1, min(thresholds.min_row_agreement, len(rows)))

    present = False
    fill = 0.0
    if len(candidates) >= required:
        # Require the rows to agree. The bar is drawn identically on every
        # row it covers; terrain varies between them, so agreement rejects
        # scenery far more reliably than any colour threshold can.
        reference = _median(candidates)
        agreeing = [
            f
            for f in candidates
            if abs(f - reference) <= thresholds.row_agreement_tolerance
        ]
        if len(agreeing) >= required:
            present = True
            fill = _median(agreeing)

    # There is deliberately no fallback for a bar showing zero health.
    # Such a bar exists for a fraction of a second before the boss dies,
    # but "region is uniformly dark" also describes every cave and night
    # sky in the game -- and reporting a fight in a dark room is the
    # far more expensive mistake.

    return BarObservation(
        present=present,
        fill_ratio=fill,
        coverage=coverage,
        health_pixels=total_health,
        depleted_pixels=total_depleted,
        sampled_pixels=total_sampled,
    )


def _left_anchored_run(
    line: list[RGB], thresholds: BarThresholds
) -> int | None:
    """Length of the *dense* health run starting at the bar's left edge.

    Returns None if there is nothing convincing there. Two properties
    separate a real bar from red scenery:

    * Contiguity. The gap tolerance is small -- enough to bridge a spell
      effect drawn over the bar, not enough to chain scattered terrain
      pixels into one long imaginary run. An earlier version allowed 2% of
      the width, which was plenty for noise to look like a bar.
    * Density. Within the run, nearly every pixel must be health-coloured.
      Textured ground produces sparse hits that a gap tolerance alone
      would happily join up.
    """
    if not line:
        return None
    width = len(line)
    lead_in = max(int(width * thresholds.left_edge_tolerance), 2)

    start = None
    for index in range(min(lead_in, width)):
        if is_health_pixel(line[index], thresholds):
            start = index
            break
    if start is None:
        return None

    length = 0
    health = 0
    gap = 0
    max_gap = max(int(width * thresholds.max_run_gap), 2)
    for index in range(start, width):
        if is_health_pixel(line[index], thresholds):
            length = index - start + 1
            health += 1
            gap = 0
        else:
            gap += 1
            if gap > max_gap:
                break

    if length <= 0:
        return None
    if (health / length) < thresholds.min_run_density:
        return None
    return length


def _scanline_rows(rect: PixelRect, scanlines: int) -> list[int]:
    """Evenly spaced sample rows inside the rect, always at least one."""
    count = max(1, min(scanlines, rect.height))
    if count == 1:
        return [rect.center_y]
    step = rect.height / (count + 1)
    return [rect.top + int(round(step * (i + 1))) for i in range(count)]


def _has_structure(
    frame: Frame, rect: PixelRect, thresholds: BarThresholds, margin: int = 4
) -> bool:
    """Look for the bar's ornamental border above or below it.

    Only consulted when a bar-shaped region contains no health pixels, to
    tell a genuinely empty bar apart from a black screen. Probes a band
    rather than two exact rows, so it does not depend on the border sitting
    at one precise offset -- that offset varies with resolution.
    """
    bands = list(range(rect.top - margin, rect.top))
    bands += list(range(rect.bottom, rect.bottom + margin))
    for y in bands:
        if not (0 <= y < frame.height):
            continue
        line = frame.scanline(y, rect.left, rect.right)
        if not line:
            continue
        if max(max(px) for px in line) > thresholds.depleted_max_channel:
            return True
    return False


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def make_test_frame(
    width: int,
    height: int,
    *,
    background: RGB = (18, 16, 14),
    bar_fill: float | None = None,
    bar_region: FractionalRect = BOSS_BAR,
    name_region: FractionalRect | None = None,
    with_name: bool = True,
    health_colour: RGB = (150, 34, 30),
    empty_colour: RGB = (24, 20, 18),
    border_colour: RGB = (120, 108, 88),
    name_colour: RGB = (230, 220, 200),
) -> Frame:
    """Synthesise a frame with (or without) a boss bar, for tests.

    When a bar is drawn, a stipple of bright pixels is also laid into the
    name-plate region so the OCR presence gate behaves as it would on a
    real frame. Pass `with_name=False` to simulate a nameless health bar.
    """
    from .geometry import BOSS_NAME

    pixels = [background] * (width * height)
    if bar_fill is not None:
        rect = bar_region.resolve(width, height)
        filled_cols = int(round(rect.width * max(0.0, min(1.0, bar_fill))))
        for y in range(rect.top, rect.bottom):
            base = y * width
            for offset in range(rect.width):
                colour = health_colour if offset < filled_cols else empty_colour
                pixels[base + rect.left + offset] = colour
        # Draw the ornate border the real HUD has, so structure probes hit.
        for y in (rect.top - 2, rect.bottom + 1):
            if 0 <= y < height:
                base = y * width
                for offset in range(rect.width):
                    pixels[base + rect.left + offset] = border_colour

        if with_name:
            plate = (name_region or BOSS_NAME).resolve(width, height)
            span = min(plate.width, max(plate.width // 3, 20))
            for y in range(plate.top + 2, max(plate.bottom - 2, plate.top + 3), 2):
                base = y * width
                for x in range(plate.left + 10, plate.left + span, 2):
                    if x < width:
                        pixels[base + x] = name_colour
    return Frame(width, height, pixels)
