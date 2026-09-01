"""Fight state machine.

Turns a stream of noisy per-frame observations into clean fight
boundaries. This is the piece the runback timer, attempt tracker and
health mirror all sit on top of, so it is deliberately conservative.

Two pieces of hysteresis do the real work:

* Entering a fight needs the bar present for several consecutive frames,
  which rejects HUD flicker and menu transitions.
* Leaving needs it absent for a much longer stretch, because the bar
  genuinely disappears mid-fight during phase transitions -- Radagon
  handing off to the Elden Beast, Godskin Duo swapping bodies. Exiting
  eagerly there would split one fight into several.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .banner import BannerKind, is_victory
from .bossdb import BossEntry
from .matching import MatchResult


class FightState(Enum):
    IDLE = "idle"
    FIGHTING = "fighting"


class EventKind(Enum):
    FIGHT_STARTED = "fight_started"
    FIGHT_ENDED = "fight_ended"
    BOSS_IDENTIFIED = "boss_identified"
    BOSS_CHANGED = "boss_changed"
    HEALTH_UPDATED = "health_updated"
    DIED = "died"
    VICTORY = "victory"


@dataclass(frozen=True)
class FightEvent:
    kind: EventKind
    at: float
    boss: BossEntry | None = None
    previous_boss: BossEntry | None = None
    fill_ratio: float = 0.0
    confidence: float = 0.0
    duration: float = 0.0       # set on FIGHT_ENDED
    lowest_fill: float = 1.0    # best attempt, set on FIGHT_ENDED
    banner: "BannerKind | None" = None  # set on DIED / VICTORY


@dataclass
class FightSnapshot:
    state: FightState = FightState.IDLE
    boss: BossEntry | None = None
    fill_ratio: float = 0.0
    lowest_fill: float = 1.0
    started_at: float | None = None
    duration: float = 0.0
    identify_attempts: int = 0


@dataclass
class DetectorConfig:
    enter_frames: int = 3
    exit_frames: int = 45          # ~1.5s at 30Hz; survives phase handoffs
    reidentify_interval: float = 2.0
    max_identify_attempts: int = 8
    health_update_epsilon: float = 0.01
    # A banner stays on screen for several seconds, so without a lockout
    # one death would be counted a dozen times.
    banner_lockout: float = 8.0


class FightTracker:
    """Consumes (bar_present, fill_ratio) per frame, emits FightEvents.

    Name identification is injected as a callable so this class never
    touches OCR, screen capture, or the clock directly.
    """

    def __init__(
        self,
        config: DetectorConfig | None = None,
        *,
        identify: Callable[[], MatchResult | None] | None = None,
        resolve_boss: Callable[[str], BossEntry | None] | None = None,
    ) -> None:
        self.config = config or DetectorConfig()
        self._identify = identify
        self._resolve_boss = resolve_boss
        self.snapshot = FightSnapshot()
        self._present_streak = 0
        self._absent_streak = 0
        self._last_identify_at: float | None = None
        self._last_present_at: float | None = None
        self._last_banner_at: float | None = None
        self._last_reported_fill = -1.0
        self._history: list[FightEvent] = []

    @property
    def history(self) -> list[FightEvent]:
        return list(self._history)

    @property
    def state(self) -> FightState:
        return self.snapshot.state

    def note_banner(self, kind: BannerKind | None, now: float) -> list[FightEvent]:
        """Feed in a centre-screen banner. Emits DIED or VICTORY.

        Attributed to whichever boss is current, and locked out afterwards
        because the banner lingers for several seconds -- otherwise a
        single death would be counted once per frame.

        Deliberately does not end the fight itself. The bar vanishing does
        that, and letting two independent signals both drive the exit would
        make the ordering of events depend on frame timing.
        """
        if kind is None:
            return []
        if self._last_banner_at is not None:
            if (now - self._last_banner_at) < self.config.banner_lockout:
                return []

        self._last_banner_at = now
        boss = self.snapshot.boss
        lowest = self.snapshot.lowest_fill
        started = self.snapshot.started_at
        duration = (now - started) if started is not None else 0.0

        event = FightEvent(
            EventKind.VICTORY if is_victory(kind) else EventKind.DIED,
            now,
            boss=boss,
            banner=kind,
            fill_ratio=self.snapshot.fill_ratio,
            lowest_fill=lowest,
            duration=max(duration, 0.0),
        )
        self._history.append(event)
        return [event]

    def update(self, present: bool, fill_ratio: float, now: float) -> list[FightEvent]:
        events: list[FightEvent] = []

        if present:
            self._present_streak += 1
            self._absent_streak = 0
            self._last_present_at = now
        else:
            self._absent_streak += 1
            self._present_streak = 0

        if self.snapshot.state is FightState.IDLE:
            if self._present_streak >= self.config.enter_frames:
                events.extend(self._begin_fight(now, fill_ratio))
        else:
            if self._absent_streak >= self.config.exit_frames:
                events.extend(self._end_fight(now))
            else:
                events.extend(self._continue_fight(now, present, fill_ratio))

        self._history.extend(events)
        return events

    # --- transitions -------------------------------------------------------

    def _begin_fight(self, now: float, fill_ratio: float) -> list[FightEvent]:
        self.snapshot = FightSnapshot(
            state=FightState.FIGHTING,
            fill_ratio=fill_ratio,
            lowest_fill=fill_ratio,
            started_at=now,
        )
        self._last_reported_fill = fill_ratio
        self._last_identify_at = None
        events = [FightEvent(EventKind.FIGHT_STARTED, now, fill_ratio=fill_ratio)]
        events.extend(self._try_identify(now))
        return events

    def _end_fight(self, now: float) -> list[FightEvent]:
        boss = self.snapshot.boss
        started = self.snapshot.started_at
        lowest = self.snapshot.lowest_fill
        # The exit is only confirmed after `exit_frames` of absence, so the
        # fight really ended when the bar was last seen, not now. Reporting
        # `now` would inflate every fight by the whole exit timeout.
        ended_at = self._last_present_at if self._last_present_at is not None else now
        duration = (ended_at - started) if started is not None else 0.0
        self.snapshot = FightSnapshot(state=FightState.IDLE)
        self._last_reported_fill = -1.0
        self._last_identify_at = None
        self._last_present_at = None
        return [
            FightEvent(
                EventKind.FIGHT_ENDED,
                now,
                boss=boss,
                duration=max(duration, 0.0),
                lowest_fill=lowest,
            )
        ]

    def _continue_fight(
        self, now: float, present: bool, fill_ratio: float
    ) -> list[FightEvent]:
        events: list[FightEvent] = []
        started = self.snapshot.started_at
        if started is not None:
            self.snapshot.duration = now - started

        if present:
            self.snapshot.fill_ratio = fill_ratio
            self.snapshot.lowest_fill = min(self.snapshot.lowest_fill, fill_ratio)
            if abs(fill_ratio - self._last_reported_fill) >= self.config.health_update_epsilon:
                self._last_reported_fill = fill_ratio
                events.append(
                    FightEvent(
                        EventKind.HEALTH_UPDATED,
                        now,
                        boss=self.snapshot.boss,
                        fill_ratio=fill_ratio,
                    )
                )

        # Only re-identify while the bar is actually on screen. During a
        # phase handoff the name plate is gone too, so an OCR pass there
        # reads empty pixels at best and stale ones at worst.
        if present and self._should_reidentify(now):
            events.extend(self._try_identify(now))
        return events

    def _should_reidentify(self, now: float) -> bool:
        if self._identify is None:
            return False
        if self.snapshot.identify_attempts >= self.config.max_identify_attempts:
            return False
        if self._last_identify_at is None:
            return True
        return (now - self._last_identify_at) >= self.config.reidentify_interval

    def _try_identify(self, now: float) -> list[FightEvent]:
        if self._identify is None:
            return []
        self._last_identify_at = now
        self.snapshot.identify_attempts += 1

        result = self._identify()
        if result is None:
            return []
        entry = self._resolve_boss(result.key) if self._resolve_boss else None
        if entry is None:
            return []

        previous = self.snapshot.boss
        if previous is not None and previous.key == entry.key:
            return []

        self.snapshot.boss = entry
        # A mid-fight name change is a phase transition, not a new fight --
        # Radagon into the Elden Beast being the canonical case. Reset the
        # best-attempt watermark so the second phase is scored on its own.
        if previous is not None:
            self.snapshot.lowest_fill = self.snapshot.fill_ratio
            return [
                FightEvent(
                    EventKind.BOSS_CHANGED,
                    now,
                    boss=entry,
                    previous_boss=previous,
                    confidence=result.confidence,
                )
            ]
        return [
            FightEvent(
                EventKind.BOSS_IDENTIFIED,
                now,
                boss=entry,
                confidence=result.confidence,
            )
        ]
