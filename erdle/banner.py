"""Detects Elden Ring's centre-screen banners.

    YOU DIED
    ENEMY FELLED  /  GREAT ENEMY FELLED  /  DEMIGOD FELLED

This is the primitive the interesting features are built on. A death
counter, an attempt tracker, a runback timer and a session recap all need
exactly one thing that did not exist before: knowing when you died and
when you won.

Two stages, because the banners are rare and OCR is not free:

1. A cheap gate on a subsampled frame -- is there bright, wide, roughly
   centred text here at all? Normal gameplay usually fails this outright.
2. Only if the gate passes, OCR and fuzzy-match against the known phrases.

Like the boss names, this is closed-vocabulary: there are four phrases,
so recognition is classification and OCR is allowed to be sloppy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .detect import RGB, Frame
from .matching import normalise, similarity
from .ocr import DEFAULT_INK_THRESHOLD, TextRecogniser, column_ink, _luma


class BannerKind(Enum):
    DEATH = "death"
    ENEMY_FELLED = "enemy_felled"
    GREAT_ENEMY_FELLED = "great_enemy_felled"
    DEMIGOD_FELLED = "demigod_felled"


# The tier banners double as a difficulty signal: "GREAT ENEMY FELLED" and
# "DEMIGOD FELLED" only appear for real bosses, so a kill's tier is free.
BANNER_PHRASES: dict[BannerKind, str] = {
    BannerKind.DEATH: "YOU DIED",
    BannerKind.ENEMY_FELLED: "ENEMY FELLED",
    BannerKind.GREAT_ENEMY_FELLED: "GREAT ENEMY FELLED",
    BannerKind.DEMIGOD_FELLED: "DEMIGOD FELLED",
}

VICTORY_KINDS = frozenset(
    {
        BannerKind.ENEMY_FELLED,
        BannerKind.GREAT_ENEMY_FELLED,
        BannerKind.DEMIGOD_FELLED,
    }
)

BOSS_TIER_KINDS = frozenset(
    {BannerKind.GREAT_ENEMY_FELLED, BannerKind.DEMIGOD_FELLED}
)


@dataclass(frozen=True)
class BannerThresholds:
    """Tuning for the cheap pre-OCR gate.

    The cutoff is *adaptive*, and has to be. "YOU DIED" is dark red --
    luma around 46 -- on a heavily vignetted screen, while "FELLED" is
    bright gold. A fixed brightness threshold tuned for one is blind to
    the other; 170, tuned for the white boss name, misses the death
    banner entirely. So the cutoff is derived per frame from the region's
    own median brightness.
    """

    # How far above the region's median a pixel must be to count as ink.
    ink_margin: int = 18
    # Floor and ceiling for the derived cutoff.
    min_ink_threshold: int = 30
    max_ink_threshold: int = 220
    # Kept for callers that want a fixed cutoff.
    ink_threshold: int = DEFAULT_INK_THRESHOLD
    # Overall lit fraction. Banner text is sparse strokes on a darkened
    # screen, so a *ceiling* matters as much as a floor -- a bright sky
    # fills the region and must not qualify.
    min_ink: float = 0.004
    max_ink: float = 0.30
    # The lit columns must span a wide, roughly central band.
    min_span: float = 0.15
    max_span: float = 0.98
    # How far the span's centre may sit from the region's centre.
    max_centre_offset: float = 0.22
    # Minimum similarity to accept a phrase, and the gap it needs over the
    # runner-up. "ENEMY FELLED" is a substring of "GREAT ENEMY FELLED", so
    # a bare threshold is not enough on its own.
    match_threshold: float = 0.66
    min_margin: float = 0.06


DEFAULT_BANNER_THRESHOLDS = BannerThresholds()


@dataclass(frozen=True)
class BannerObservation:
    present: bool
    ink: float
    span: float
    centre_offset: float
    threshold: int = DEFAULT_INK_THRESHOLD

    @property
    def worth_reading(self) -> bool:
        return self.present


def adaptive_threshold(
    frame: Frame, thresholds: BannerThresholds, sample_step: int = 3
) -> int:
    """Pick an ink cutoff from the region's own brightness distribution.

    Median plus a margin. On a death screen the median is near black and
    the dark red glyphs sit well above it; on a victory screen everything
    shifts up together and the gold text still stands out. A fixed number
    cannot do both.
    """
    samples = [
        _luma(frame.pixel(x, y))
        for y in range(0, frame.height, sample_step)
        for x in range(0, frame.width, sample_step)
    ]
    if not samples:
        return thresholds.min_ink_threshold
    samples.sort()
    median = samples[len(samples) // 2]
    cutoff = int(median) + thresholds.ink_margin
    return max(
        thresholds.min_ink_threshold, min(cutoff, thresholds.max_ink_threshold)
    )


def looks_like_banner(
    frame: Frame, thresholds: BannerThresholds = DEFAULT_BANNER_THRESHOLDS
) -> BannerObservation:
    """Cheap gate: could this region hold a centre banner?

    Deliberately permissive -- a false positive costs one OCR pass, a false
    negative loses a death. What it reliably rejects is the common case:
    ordinary gameplay, where the centre of the screen is either too dark,
    too uniformly bright, or lit off to one side.
    """
    if frame.width == 0 or frame.height == 0:
        return BannerObservation(False, 0.0, 0.0, 1.0)

    cutoff = adaptive_threshold(frame, thresholds)
    counts = column_ink(frame, threshold=cutoff)
    total = sum(counts)
    ink = total / (frame.width * frame.height)
    if not (thresholds.min_ink <= ink <= thresholds.max_ink):
        return BannerObservation(False, ink, 0.0, 1.0, cutoff)

    # Ignore columns holding only a stray pixel or two, so a speck at the
    # far edge cannot stretch the measured span across the whole region.
    floor = max(1, int(frame.height * 0.02))
    lit_columns = [x for x, count in enumerate(counts) if count >= floor]
    if not lit_columns:
        return BannerObservation(False, ink, 0.0, 1.0, cutoff)

    left, right = lit_columns[0], lit_columns[-1]
    span = (right - left + 1) / frame.width
    centre = (left + right) / 2 / frame.width
    offset = abs(centre - 0.5)

    present = (
        thresholds.min_span <= span <= thresholds.max_span
        and offset <= thresholds.max_centre_offset
    )
    return BannerObservation(present, ink, span, offset, cutoff)


def classify(
    text: str, thresholds: BannerThresholds = DEFAULT_BANNER_THRESHOLDS
) -> BannerKind | None:
    """Map OCR output onto one of the four phrases, or None.

    The tier phrases overlap by design, so the margin check matters: a
    clipped read of "GREAT ENEMY FELLED" that loses the first word is
    genuinely ambiguous, and reporting nothing beats reporting the wrong
    tier.
    """
    target = normalise(text)
    if not target:
        return None

    scored = sorted(
        (
            (similarity(target, normalise(phrase)), kind)
            for kind, phrase in BANNER_PHRASES.items()
        ),
        key=lambda pair: (-pair[0], pair[1].value),
    )
    best_score, best_kind = scored[0]
    margin = best_score - scored[1][0] if len(scored) > 1 else best_score

    if best_score < thresholds.match_threshold:
        return None
    if margin < thresholds.min_margin:
        return None
    return best_kind


def read_banner(
    frame: Frame,
    recogniser: TextRecogniser,
    *,
    thresholds: BannerThresholds = DEFAULT_BANNER_THRESHOLDS,
    ocr_thresholds: tuple[int, ...] = (200, 170, 230),
) -> BannerKind | None:
    """Full pipeline for one frame: gate, then OCR, then classify.

    The gate's own adaptive cutoff is tried first -- it was derived from
    this frame, so it beats any fixed value, and it is the only one that
    works on the dark red death text.
    """
    observation = looks_like_banner(frame, thresholds)
    if not observation.present:
        return None
    candidates = (observation.threshold,) + tuple(ocr_thresholds)
    for cutoff in candidates:
        try:
            text = recogniser.read(frame, cutoff)
        except TypeError:
            text = recogniser.read(frame)
        if not text.strip():
            continue
        kind = classify(text, thresholds)
        if kind is not None:
            return kind
    return None


def is_victory(kind: BannerKind | None) -> bool:
    return kind in VICTORY_KINDS


def is_boss_tier(kind: BannerKind | None) -> bool:
    """True for the tiers Elden Ring reserves for real bosses."""
    return kind in BOSS_TIER_KINDS


def make_banner_frame(
    width: int,
    height: int,
    *,
    text_span: float = 0.55,
    text_rows: float = 0.18,
    background: RGB = (14, 12, 12),
    text_colour: RGB = (226, 208, 176),
    stroke_density: int = 3,
) -> Frame:
    """Synthesise a centred banner, for tests.

    Approximates the real thing: wide, centred, sparse bright strokes on a
    darkened screen.
    """
    pixels = [background] * (width * height)
    span_px = int(width * text_span)
    left = (width - span_px) // 2
    rows = max(int(height * text_rows), 1)
    top = (height - rows) // 2
    for y in range(top, top + rows):
        for x in range(left, left + span_px):
            if (x // stroke_density + y // 4) % 3 == 0:
                pixels[y * width + x] = text_colour
    return Frame(width, height, pixels)
