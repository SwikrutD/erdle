"""Resolution-independent screen region math.

Elden Ring's HUD scales proportionally with resolution, so every region of
interest is stored as a fractional rectangle (0.0-1.0) and resolved against
the actual captured frame size at runtime.

The fractions below are STARTING VALUES measured against 16:9 gameplay
footage. They must be confirmed with `python -m erdle.calibrate` on real
hardware before the detector will be reliable -- see README.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FractionalRect:
    """A rectangle expressed as fractions of the frame's width/height."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.left < self.right <= 1.0):
            raise ValueError(f"bad horizontal extent: {self.left}..{self.right}")
        if not (0.0 <= self.top < self.bottom <= 1.0):
            raise ValueError(f"bad vertical extent: {self.top}..{self.bottom}")

    def resolve(self, width: int, height: int) -> "PixelRect":
        """Convert to integer pixel coordinates for a given frame size."""
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        left = int(round(self.left * width))
        top = int(round(self.top * height))
        right = int(round(self.right * width))
        bottom = int(round(self.bottom * height))
        # Guarantee at least one pixel in each axis even on tiny frames.
        right = max(right, left + 1)
        bottom = max(bottom, top + 1)
        return PixelRect(left, top, min(right, width), min(bottom, height))


@dataclass(frozen=True)
class PixelRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center_y(self) -> int:
        return (self.top + self.bottom) // 2


def remap(outer: FractionalRect, inner: FractionalRect) -> FractionalRect:
    """Express `inner` in coordinates local to a crop of `outer`.

    Lets the capture layer grab one small strip of the screen while the
    detector keeps working in fractional coordinates. Raises if `inner` is
    not fully contained in `outer`.
    """
    width = outer.right - outer.left
    height = outer.bottom - outer.top
    if width <= 0 or height <= 0:
        raise ValueError("outer rect has no area")
    try:
        return FractionalRect(
            (inner.left - outer.left) / width,
            (inner.top - outer.top) / height,
            (inner.right - outer.left) / width,
            (inner.bottom - outer.top) / height,
        )
    except ValueError as exc:
        raise ValueError(f"{inner} is not contained within {outer}") from exc


# --- Default regions -------------------------------------------------------
# Measured against 3840x2160 gameplay. Elden Ring's HUD scales
# proportionally, so these should hold for any 16:9 resolution -- but
# confirm with `python -m erdle.calibrate --auto` before trusting them.
#
# Boss health bar: bottom-centre, spanning roughly the middle half.
BOSS_BAR = FractionalRect(left=0.2387, top=0.8008, right=0.7613, bottom=0.8140)

# Boss name plate: immediately above the bar, left-aligned to its start.
#
# Deliberately much narrower than the bar. Names are left-aligned and even
# the longest ("Malenia, Blade of Miquella") reaches only about a third of
# the way across, so the remaining two thirds contribute nothing but the
# terrain specks that were wrecking OCR.
BOSS_NAME = FractionalRect(left=0.2387, top=0.7538, right=0.5200, bottom=0.7958)

# One contiguous strip containing both, with margin for the bar's border.
# Capturing this instead of the full screen is roughly a 30x reduction in
# pixels -- the difference between a viable capture loop and an unusable
# one, since converting a 4K framebuffer to Python tuples costs seconds.
# Must contain BOSS_BAR, BOSS_NAME *and* NAME_BAND: with name-driven
# detection the band is what the loop reads every poll, so cropping it
# away would defeat the whole optimisation.
HUD_STRIP = FractionalRect(left=0.1700, top=0.7120, right=0.8000, bottom=0.8320)

STRIP_BOSS_BAR = remap(HUD_STRIP, BOSS_BAR)
STRIP_BOSS_NAME = remap(HUD_STRIP, BOSS_NAME)

# Centre banner: where "YOU DIED" and "<tier> ENEMY FELLED" appear. Both
# land in the same place, so one region covers them. Deliberately generous
# vertically -- the two banners sit at slightly different heights and the
# text is large, so precision buys nothing here.
CENTRE_BANNER = FractionalRect(left=0.20, top=0.40, right=0.80, bottom=0.60)

# Where the boss name is looked for when the name drives fight detection.
# Deliberately looser than BOSS_NAME: nothing here has to be calibrated,
# because OCR does not care exactly where the text sits inside the band,
# only that it is in there and that the band excludes other HUD text.
#
# Measured at 3840x2160 the name occupies y 0.754-0.796 starting at
# x 0.239. The margins below absorb aspect-ratio differences. Item pickup
# toasts, the flask name and the rune counter all sit below y 0.90, and
# interaction prompts above y 0.70, so this band stays clean.
NAME_BAND = FractionalRect(left=0.19, top=0.742, right=0.62, bottom=0.806)

DEFAULT_REGIONS = {
    "boss_bar": BOSS_BAR,
    "boss_name": BOSS_NAME,
    "hud_strip": HUD_STRIP,
}


STRIP_NAME_BAND = remap(HUD_STRIP, NAME_BAND)
