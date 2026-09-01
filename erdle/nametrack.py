"""Fight tracking driven by the boss name alone.

The previous design had two independent systems: bar detection decided
whether a fight was happening, and OCR decided which boss it was. They
could disagree, and every field bug came from exactly that -- red terrain
tripped the bar detector, no name resolved, and the panel sat on "unknown
boss" while the player walked around Limgrave.

Here there is one signal. A boss name on screen *is* the fight. When the
name stops matching, the fight is over. The two things cannot contradict
each other because they are the same observation.

Secondary benefits, both of which cost real effort under the old design:

* Resolution independence comes free. OCR reads whatever size the glyphs
  are; nothing needs calibrating per display.
* No colour thresholds. The bar's palette was the one part that never
  generalised, and it is no longer load-bearing.

The bar is still read, but only to fill the progress bar on the panel. If
it fails you lose a graphic, not the feature.
"""

from __future__ import annotations

from dataclasses import dataclass

from .banner import BannerKind, is_victory
from .bossdb import BossEntry
from .matching import MatchResult
from .state import EventKind, FightEvent, FightSnapshot, FightState


@dataclass
class NameTrackerConfig:
    """Polling is in *observations*, not frames.

    OCR runs on a timer rather than every frame, so counting frames would
    make the timings depend on capture rate.
    """

    # A confidently matched name is immediate evidence of a fight; there is
    # nothing else on screen it could plausibly be.
    enter_hits: int = 1
    # Misses before declaring the fight over. Elden Ring hides the bar
    # during some phase transitions, and OCR occasionally drops a frame to
    # a particle effect, so this needs slack.
    # Four polls (~2.8s) rather than three. Field data shows OCR
    # occasionally returns nothing mid-fight even with ink present, and a
    # dropped read should not blank the panel.
    exit_misses: int = 4
    # A banner lingers for seconds; without a lockout one death is counted
    # once per poll.
    banner_lockout: float = 8.0
    # Ignore a name change unless the new match is at least this good.
    # Prevents a garbled read from renaming the boss mid-fight.
    switch_confidence: float = 0.75
    # There is deliberately no "unrecognised text is probably a boss"
    # path. One was tried, guarded by requiring consecutive reads to
    # agree, on the theory that OCR noise varies while a real name does
    # not. Field data killed it: a stationary player produces the *same*
    # garbage every poll, so the guard passed and a player message on the
    # ground started a fight. Agreement measures whether the scene is
    # static, not whether the text is a name.
    #
    # No match therefore means no fight. The cost is that a boss missing
    # from the table shows nothing, which is a data problem with a data
    # fix -- and far cheaper than the panel lighting up in an empty field.


class NameTracker:
    """Turns a stream of name observations into fight events."""

    def __init__(self, config: NameTrackerConfig | None = None) -> None:
        self.config = config or NameTrackerConfig()
        self.snapshot = FightSnapshot()
        self._hits = 0
        self._misses = 0
        self._last_banner_at: float | None = None
        self._history: list[FightEvent] = []

    @property
    def state(self) -> FightState:
        return self.snapshot.state

    @property
    def history(self) -> list[FightEvent]:
        return list(self._history)

    # --- the observation ---------------------------------------------------

    def observe(
        self,
        match: MatchResult | None,
        boss: BossEntry | None,
        now: float,
    ) -> list[FightEvent]:
        """Report one OCR poll.

        `match` is None when nothing resolved, which means no fight.
        """
        events: list[FightEvent] = []

        if match is not None and boss is not None:
            self._hits += 1
            self._misses = 0
            if self.snapshot.state is FightState.IDLE:
                if self._hits >= self.config.enter_hits:
                    events.extend(self._begin(boss, match, now))
            elif (
                self.snapshot.boss is not None
                and boss.key != self.snapshot.boss.key
                and match.confidence >= self.config.switch_confidence
            ):
                events.append(self._switch(boss, match, now))
        else:
            self._hits = 0
            if self.snapshot.state is FightState.FIGHTING:
                self._misses += 1
                if self._misses >= self.config.exit_misses:
                    events.append(self._end(now))

        if self.snapshot.started_at is not None:
            self.snapshot.duration = now - self.snapshot.started_at

        self._history.extend(events)
        return events

    def set_fill(self, ratio: float | None) -> None:
        """Record bar fill for display. Never affects fight state.

        Deliberately separate from `observe`: if bar detection could end a
        fight we would be back to two systems that can disagree.
        """
        if ratio is None or self.snapshot.state is not FightState.FIGHTING:
            return
        ratio = max(0.0, min(1.0, ratio))
        self.snapshot.fill_ratio = ratio
        self.snapshot.lowest_fill = min(self.snapshot.lowest_fill, ratio)

    # --- transitions -------------------------------------------------------

    def _begin(
        self, boss: BossEntry, match: MatchResult, now: float
    ) -> list[FightEvent]:
        self.snapshot = FightSnapshot(
            state=FightState.FIGHTING,
            boss=boss,
            started_at=now,
            fill_ratio=1.0,
            lowest_fill=1.0,
            identify_attempts=1,
        )
        self._misses = 0
        return [
            FightEvent(EventKind.FIGHT_STARTED, now, boss=boss),
            FightEvent(
                EventKind.BOSS_IDENTIFIED, now, boss=boss,
                confidence=match.confidence,
            ),
        ]

    def _switch(
        self, boss: BossEntry, match: MatchResult, now: float
    ) -> FightEvent:
        """A different name mid-fight is a phase transition.

        Radagon handing off to the Elden Beast is the canonical case: same
        fight, new name, and the best-attempt watermark restarts.
        """
        previous = self.snapshot.boss
        self.snapshot.boss = boss
        self.snapshot.lowest_fill = self.snapshot.fill_ratio
        return FightEvent(
            EventKind.BOSS_CHANGED, now, boss=boss,
            previous_boss=previous, confidence=match.confidence,
        )

    def _end(self, now: float) -> FightEvent:
        boss = self.snapshot.boss
        started = self.snapshot.started_at
        lowest = self.snapshot.lowest_fill
        duration = (now - started) if started is not None else 0.0
        self.snapshot = FightSnapshot(state=FightState.IDLE)
        self._misses = 0
        self._hits = 0
        return FightEvent(
            EventKind.FIGHT_ENDED, now, boss=boss,
            duration=max(duration, 0.0), lowest_fill=lowest,
        )

    # --- banners -----------------------------------------------------------

    def note_banner(self, kind: BannerKind | None, now: float) -> list[FightEvent]:
        """YOU DIED / FELLED. Emits DIED or VICTORY.

        Does not end the fight itself -- the name disappearing does that.
        Two things driving the exit would reintroduce the disagreement this
        design exists to remove.
        """
        if kind is None:
            return []
        if self._last_banner_at is not None:
            if (now - self._last_banner_at) < self.config.banner_lockout:
                return []

        self._last_banner_at = now
        started = self.snapshot.started_at
        event = FightEvent(
            EventKind.VICTORY if is_victory(kind) else EventKind.DIED,
            now,
            boss=self.snapshot.boss,
            banner=kind,
            fill_ratio=self.snapshot.fill_ratio,
            lowest_fill=self.snapshot.lowest_fill,
            duration=max((now - started) if started is not None else 0.0, 0.0),
        )
        self._history.append(event)
        return [event]
