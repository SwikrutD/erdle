"""Wires detector, matcher, state machine, renderer and GameSense together.

`ErdleApp.step()` processes exactly one frame and is fully synchronous
and deterministic given its inputs, so the entire pipeline can be driven
frame-by-frame in tests without threads, sleeps, or hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .banner import BannerThresholds, read_banner
from .bossdb import BossDatabase, BossEntry
from .canvas import Canvas
from .detect import BarThresholds, Frame, analyse_bar
from .geometry import (
    BOSS_BAR,
    BOSS_NAME,
    NAME_BAND,
    STRIP_BOSS_BAR,
    STRIP_BOSS_NAME,
    STRIP_NAME_BAND,
    FractionalRect,
)
from .matching import BossNameMatcher, MatchResult
from .nametrack import NameTracker, NameTrackerConfig
from .ocr import TextRecogniser, estimate_text_presence, region_ink_fraction
from .render import (
    render_boss_screen,
    render_defeat_screen,
    render_idle_screen,
    render_unknown_boss,
    render_victory_screen,
)
from .state import DetectorConfig, EventKind, FightState, FightTracker


@dataclass
class AppConfig:
    # Regions are frozen dataclasses, so sharing one instance is safe.
    bar_region: FractionalRect = BOSS_BAR
    name_region: FractionalRect = BOSS_NAME
    # These two are mutable, so each AppConfig needs its own. Python 3.14
    # rejects a mutable default outright; on older versions it silently
    # shared one object across every instance.
    thresholds: BarThresholds = field(default_factory=BarThresholds)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    # Below this fraction of lit pixels the name plate is assumed blank and
    # the OCR pass is skipped entirely.
    min_text_presence: float = 0.012
    match_threshold: float = 0.62
    min_match_margin: float = 0.03
    # Brightness cutoffs to try, best-first. Measured on a 4K capture: 200
    # cleanly separated white glyphs from sunlit grass; the others cover
    # darker and brighter backdrops.
    ocr_thresholds: tuple[int, ...] = (200, 170, 230, 150)
    # Stop retrying once a match is this good; saves the remaining passes.
    good_enough_confidence: float = 0.85
    banner: BannerThresholds = field(default_factory=BannerThresholds)
    # --- name-driven detection (the default) ---------------------------
    # The boss name on screen *is* the fight. One signal, so detection and
    # identification cannot disagree -- which is what produced "unknown
    # boss" while walking around. Set False for the older bar-driven path.
    name_driven: bool = True
    name_band: FractionalRect = NAME_BAND
    names: NameTrackerConfig = field(default_factory=NameTrackerConfig)
    # Seconds between OCR polls. The name is on screen for the whole
    # fight, so there is no need to look every frame, and OCR is the most
    # expensive thing in the loop.
    name_poll_interval: float = 0.7
    # Ink gate for the band. Much lower than `min_text_presence`, which
    # was tuned for a tight crop around the name: the band is several
    # times larger, so identical text is a far smaller fraction of it.
    # A permissive gate is safe -- it costs an OCR pass, and matching
    # still decides whether anything is really there.
    min_band_ink: float = 0.003
    # How long a death or victory screen holds the panel before returning
    # to normal. Long enough to read while the game is fading out.
    event_screen_seconds: float = 6.0
    show_health_bar: bool = True
    idle_message: str = "ERDLE"

    @classmethod
    def for_hud_strip(cls, **kwargs) -> "AppConfig":
        """Config for frames that are already cropped to `HUD_STRIP`.

        Pair with `MSSSource.grab_hud_strip()`. Same detection maths, ~30x
        fewer pixels converted per frame.
        """
        kwargs.setdefault("bar_region", STRIP_BOSS_BAR)
        kwargs.setdefault("name_region", STRIP_BOSS_NAME)
        kwargs.setdefault("name_band", STRIP_NAME_BAND)
        return cls(**kwargs)


class ErdleApp:
    def __init__(
        self,
        database: BossDatabase,
        recogniser: TextRecogniser,
        *,
        config: AppConfig | None = None,
        on_screen: Callable[[Canvas], None] | None = None,
        on_poll: Callable | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.database = database
        self.recogniser = recogniser
        self.matcher = BossNameMatcher.from_entries(
            database,
            threshold=self.config.match_threshold,
            min_margin=self.config.min_match_margin,
        )
        self._on_screen = on_screen
        self.on_poll = on_poll
        self._current_frame: Frame | None = None
        self._event_canvas: Canvas | None = None
        self._event_until = 0.0
        self.banners_seen = 0
        self.glyphs_learned = 0
        self._last_text = ""
        self.tracker = (
            NameTracker(self.config.names)
            if self.config.name_driven
            else FightTracker(
                self.config.detector,
                identify=self._identify_current,
                resolve_boss=self.database.get,
            )
        )
        self._last_name_poll: float | None = None
        self.last_canvas: Canvas | None = None
        self.frames_seen = 0
        self.ocr_calls = 0
        self._last_signature: tuple | None = None

    # --- main entry point --------------------------------------------------

    def step(
        self, frame: Frame, now: float, banner_frame: Frame | None = None
    ) -> list:
        """Process one frame. `banner_frame` is the centre-screen crop.

        The banner is optional and expected at a lower rate than the HUD:
        deaths are not time-critical to the millisecond, and grabbing the
        centre of the screen costs far more than the HUD strip.
        """
        self.frames_seen += 1
        self._current_frame = frame

        observation = analyse_bar(
            frame,
            region=self.config.bar_region,
            thresholds=self.config.thresholds,
        )

        if self.config.name_driven:
            events = self._poll_name(frame, now)
            # Bar fill is display only. It can no longer start or end a
            # fight, so a false reading costs a wrong progress bar at most.
            self.tracker.set_fill(
                observation.fill_ratio if observation.present else None
            )
        else:
            events = self.tracker.update(
                observation.present, observation.fill_ratio, now
            )

        if banner_frame is not None:
            events.extend(self._check_banner(banner_frame, now))

        for event in events:
            self._maybe_show_outcome(event, now)

        self._refresh_screen(now)
        return events

    # --- name polling ------------------------------------------------------

    def _poll_name(self, frame: Frame, now: float) -> list:
        """Read the name band on a timer and report what matched."""
        if self._last_name_poll is not None:
            if (now - self._last_name_poll) < self.config.name_poll_interval:
                return []
        self._last_name_poll = now

        rect = self.config.name_band.resolve(frame.width, frame.height)

        # Check for ink *before* building a crop. The band is 351k pixels
        # at 4K; materialising it costs more than the rest of the loop, and
        # is wasted whenever there is no name -- which is most of the time.
        # Clear the previous read first. Forgetting to do this left a
        # stale name in place whenever the gate failed, which kept a fight
        # alive after the boss bar had gone.
        self._last_text = ""

        match = None
        ink = region_ink_fraction(frame, rect)
        if ink >= self.config.min_band_ink:
            self._current_frame = frame.region(rect)
            match = self._identify_current()
        boss = self.database.get(match.key) if match else None

        if self.on_poll is not None:
            self.on_poll(ink, self._last_text, match)
        return self.tracker.observe(match, boss, now)

    # --- banners -----------------------------------------------------------

    def _check_banner(self, banner_frame: Frame, now: float) -> list:
        kind = read_banner(
            banner_frame,
            self.recogniser,
            thresholds=self.config.banner,
            ocr_thresholds=self.config.ocr_thresholds[:3],
        )
        if kind is None:
            return []
        self.banners_seen += 1
        return self.tracker.note_banner(kind, now)

    def _maybe_show_outcome(self, event, now: float) -> None:
        """Hold a win or lose message on the panel for a few seconds.

        Only DIED and VICTORY qualify. A fight that ends because the bar
        simply went away -- you walked off, or reloaded -- gets no message
        and drops straight back to idle.
        """
        if event.kind is EventKind.VICTORY:
            self._event_canvas = render_victory_screen()
        elif event.kind is EventKind.DIED:
            self._event_canvas = render_defeat_screen()
        else:
            return
        self._event_until = now + self.config.event_screen_seconds

    # --- identification ----------------------------------------------------

    def _identify_current(self) -> MatchResult | None:
        """Read the name plate, retrying at other brightness cutoffs.

        The right cutoff depends on what is behind the bar -- grass, stone,
        night sky -- and no single value wins everywhere. Rather than making
        the user tune one, try a few and keep the first that resolves to a
        real boss. Ordering matters: `ocr_thresholds[0]` should be the value
        that usually works, since the loop stops on the first success and
        normally costs exactly one OCR pass.
        """
        frame = self._current_frame
        if frame is None:
            return None
        if self.config.name_driven:
            # Already cropped to the name band by _poll_name.
            crop = frame
        else:
            rect = self.config.name_region.resolve(frame.width, frame.height)
            crop = frame.region(rect)
            if estimate_text_presence(crop) < self.config.min_text_presence:
                return None

        best: MatchResult | None = None
        self._last_text = ""
        for threshold in self.config.ocr_thresholds:
            self.ocr_calls += 1
            try:
                text = self.recogniser.read(crop, threshold)
            except TypeError:
                # A recogniser that predates the threshold argument.
                text = self.recogniser.read(crop)
            if not text.strip():
                continue
            # Remember the raw read: an unlisted boss still deserves the
            # health bar, and the tracker decides using it.
            if len(text.strip()) > len(self._last_text):
                self._last_text = text.strip()
            result = self.matcher.match(text)
            if result is None:
                continue
            if best is None or result.confidence > best.confidence:
                best = result
            if best.confidence >= self.config.good_enough_confidence:
                # A confidently matched name means every glyph on that
                # plate is now labelled. Hand it to the recogniser so the
                # atlas can learn the font and stop needing OCR at all.
                self._teach(crop, best.display_name, threshold)
                break
        return best

    def _teach(self, crop, name: str, threshold: int) -> None:
        """Teach the atlas using the name exactly as the game renders it.

        Case matters and must not be folded. Elden Ring draws "Tree
        Sentinel", not "TREE SENTINEL", so upper-casing the label files a
        lowercase 'e' under 'E' -- and worse, puts the capital T from
        "Tree" and the lowercase t from "Sentinel" under the same key,
        where two quite different shapes then compete.

        Downstream does not care: the boss matcher upper-cases before
        comparing anyway.
        """
        teach = getattr(self.recogniser, "teach", None)
        if teach is None:
            return
        try:
            learned = teach(crop, name, threshold=threshold)
            self.glyphs_learned += learned
            hook = getattr(self, "on_refusal", None)
            if learned == 0 and hook is not None:
                from .glyphs import segment_glyphs
                hook(crop, name, segment_glyphs(crop, threshold=threshold))
        except Exception:
            # Learning is a bonus. It must never break recognition.
            pass

    # --- output ------------------------------------------------------------

    def _refresh_screen(self, now: float = 0.0) -> None:
        # A death or victory screen holds the panel for a few seconds and
        # outranks everything else -- it is the moment worth looking at.
        if self._event_canvas is not None and now < self._event_until:
            if self._last_signature != ("event", id(self._event_canvas)):
                self._last_signature = ("event", id(self._event_canvas))
                self.last_canvas = self._event_canvas
                if self._on_screen is not None:
                    self._on_screen(self._event_canvas)
            return
        if self._event_canvas is not None:
            self._event_canvas = None
            self._last_signature = None

        snapshot = self.tracker.snapshot
        boss: BossEntry | None = snapshot.boss
        fill = snapshot.fill_ratio if self.config.show_health_bar else None

        if snapshot.state is FightState.IDLE:
            signature = ("idle",)
        elif boss is None:
            signature = ("unknown", round(snapshot.fill_ratio, 2))
        else:
            signature = ("boss", boss.key, round(snapshot.fill_ratio, 2))

        # Re-rendering an identical screen would spam GG's HTTP endpoint
        # at frame rate for no visible benefit.
        if signature == self._last_signature:
            return
        self._last_signature = signature

        if snapshot.state is FightState.IDLE:
            canvas = render_idle_screen(self.config.idle_message)
        elif boss is None:
            canvas = render_unknown_boss(fill_ratio=fill)
        else:
            canvas = render_boss_screen(boss, fill_ratio=fill)

        self.last_canvas = canvas
        if self._on_screen is not None:
            self._on_screen(canvas)


def build_app(
    data_path: str | None = None,
    recogniser: TextRecogniser | None = None,
    **kwargs,
) -> ErdleApp:
    from .bossdb import default_data_path
    from .ocr import NullRecogniser

    database = BossDatabase.load(data_path or default_data_path())
    return ErdleApp(database, recogniser or NullRecogniser(), **kwargs)
