"""Self-calibration, so nobody else has to run a terminal command.

The shipped regions were measured on one 3840x2160 display. Fractional
coordinates survive a resolution change at the same aspect ratio, but a
21:9 ultrawide or a 16:10 laptop puts the boss bar somewhere else
entirely -- and the first thing a new user would see is a panel that
never leaves ERDLE.

So: when the detector has seen nothing for a while but the screen is
clearly showing something, sweep for a boss bar. If one is found, adopt
those regions and save them. It costs one full-screen grab occasionally,
and only until it succeeds once.
"""

from __future__ import annotations

from dataclasses import dataclass

from .calibrate import find_bar, suggest_regions
from .config import Config
from .geometry import (
    BOSS_BAR,
    BOSS_NAME,
    HUD_STRIP,
    FractionalRect,
    remap,
)


# The shipped regions were measured on 3840x2160. Fractional coordinates
# are resolution-independent but not aspect-independent, so they remain
# correct on any 16:9 display and only need re-measuring off that aspect.
REFERENCE_ASPECT = 16 / 9
ASPECT_TOLERANCE = 0.015


def defaults_fit(width: int, height: int) -> bool:
    """True when the shipped regions already describe this display."""
    if width <= 0 or height <= 0:
        return False
    aspect = width / height
    return abs(aspect - REFERENCE_ASPECT) <= REFERENCE_ASPECT * ASPECT_TOLERANCE



@dataclass
class AutoCalibrator:
    """Decides when to attempt a sweep, and applies the result."""

    # Never sweep more often than this. The scan is expensive and there is
    # no hurry -- the user is walking around, not mid-fight.
    interval: float = 20.0
    # Give up after this many failures. If it has not found a bar in this
    # many tries the user has probably not met a boss yet, and retrying
    # forever would burn CPU on every idle machine.
    max_attempts: int = 40

    attempts: int = 0
    last_attempt: float | None = None
    succeeded: bool = False

    def should_attempt(self, now: float, *, already_calibrated: bool) -> bool:
        if already_calibrated or self.succeeded:
            return False
        if self.attempts >= self.max_attempts:
            return False
        if self.last_attempt is None:
            return True
        return (now - self.last_attempt) >= self.interval

    def attempt(self, frame, config: Config, now: float) -> bool:
        """Sweep one full-screen frame. Returns True if regions were found."""
        self.attempts += 1
        self.last_attempt = now
        resolution = f"{frame.width}x{frame.height}"

        # Do not go looking for something we already know the position of.
        # Searching a 16:9 screen could only ever replace correct regions
        # with a guess, and on one 4K display it did exactly that: it found
        # a red band at 56% screen height and moved the bar there, so the
        # panel then read the wrong rows forever. Sweeping is for displays
        # whose aspect the shipped numbers do not describe.
        if defaults_fit(frame.width, frame.height):
            config.apply_regions(
                BOSS_BAR, BOSS_NAME, HUD_STRIP, resolution=resolution
            )
            self.succeeded = True
            return True

        found = find_bar(frame)
        if found is None:
            return False

        regions = parse_suggestion(suggest_regions(found))
        if regions is None:
            return False

        bar, name, strip = regions
        config.apply_regions(bar, name, strip, resolution=resolution)
        self.succeeded = True
        return True


def parse_suggestion(
    text: str,
) -> tuple[FractionalRect, FractionalRect, FractionalRect] | None:
    """Turn `suggest_regions` output into rectangles.

    That function emits pasteable Python. Rather than duplicate the
    geometry logic here -- and risk the two drifting apart -- evaluate it
    in a namespace holding nothing but FractionalRect.
    """
    namespace: dict = {"FractionalRect": FractionalRect, "__builtins__": {}}
    try:
        exec(text, namespace)  # noqa: S102 - our own generated text
        bar = namespace["BOSS_BAR"]
        name = namespace["BOSS_NAME"]
        strip = namespace["HUD_STRIP"]
    except (KeyError, ValueError, SyntaxError, NameError):
        return None

    if not all(isinstance(r, FractionalRect) for r in (bar, name, strip)):
        return None

    # The strip has to contain the other two, or the fast capture path
    # would be cropping away the very thing it is looking for.
    for region in (bar, name):
        if not (
            strip.left <= region.left
            and strip.right >= region.right
            and strip.top <= region.top
            and strip.bottom >= region.bottom
        ):
            return None
    return bar, name, strip


def band_for(config: Config, margin: float = 0.006) -> FractionalRect:
    """A generous search band around the calibrated name plate.

    Name-driven detection reads this band rather than the tight plate, so
    it has to follow calibration. Leaving it at the module default meant
    that on any display needing calibration the band pointed at the wrong
    rows entirely -- the exact users calibration exists for.
    """
    strip = config.hud_strip
    plate = config.boss_name
    return FractionalRect(
        left=max(plate.left - margin, strip.left),
        top=max(plate.top - margin, strip.top),
        right=min(plate.right + margin * 4, strip.right),
        bottom=min(plate.bottom + margin, strip.bottom),
    )


def strip_regions(
    config: Config,
) -> tuple[FractionalRect, FractionalRect, FractionalRect]:
    """Bar, name plate and search band, inside the HUD strip crop."""
    return (
        remap(config.hud_strip, config.boss_bar),
        remap(config.hud_strip, config.boss_name),
        remap(config.hud_strip, band_for(config)),
    )
