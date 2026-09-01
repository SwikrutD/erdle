"""What the on-screen overlay shows. No UI code lives here.

The OLED panel has 128x40 monochrome pixels, so `render.py` spends most of
its effort deciding what to *drop*: eight damage types collapse to a best
and a worst, six statuses collapse to four abbreviations, and the full
boss name becomes "MALENIA". That editing is the right call for a
keyboard, and the wrong call for a screen overlay, which has room for
everything the database actually knows.

So the overlay does not mirror the panel. It reports the whole row.

This module is deliberately free of tkinter. The window lives in
`overlay_ui.py`, which cannot be imported on a machine without Tk; the
content model can, so it is testable everywhere and the two can be
reasoned about separately.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bossdb import (
    DAMAGE_TYPES,
    EFFECTIVENESS_LABELS,
    IMMUNE,
    NORMAL,
    RESISTANT,
    STATUS_ORDER,
    WEAK,
    BossEntry,
)

# Display names. The database keys are lowercase engine terms; "standard"
# in particular means nothing to a player, who calls it physical.
DAMAGE_LABELS = {
    "standard": "Physical",
    "slash": "Slash",
    "strike": "Strike",
    "pierce": "Pierce",
    "magic": "Magic",
    "fire": "Fire",
    "lightning": "Lightning",
    "holy": "Holy",
}

STATUS_LABELS = {
    "bleed": "Bleed",
    "frost": "Frost",
    "rot": "Scarlet Rot",
    "poison": "Poison",
    "sleep": "Sleep",
    "madness": "Madness",
}

# How full the little bar next to each row is drawn, 0.0 to 1.0. Not a
# linear map of the 0-3 scale: "immune" has to read as empty, and the gap
# between resistant and normal matters more to a player than the gap
# between normal and weak.
EFFECT_WEIGHT = {
    IMMUNE: 0.0,
    RESISTANT: 0.3,
    NORMAL: 0.65,
    WEAK: 1.0,
}

# The 0-3 effectiveness scale exists for a 128x40 panel. It is far too
# coarse here: across the whole database only two of the four buckets are
# ever used for damage, so 526 of 615 rows would read "resistant" and the
# overlay would be a wall of identical words.
#
# `severity` carries the sheet's original six-way split, so damage rows use
# that instead. The wording is deliberately player-facing -- the source
# says "very strong against", which is boss-relative and reads backwards
# when what you want to know is whether to bring the weapon.
SEVERITY_WORDS = {
    5: "excellent",
    4: "good",
    3: "neutral",
    2: "poor",
    1: "bad",
    0: "useless",
}
MAX_SEVERITY = 5


@dataclass(frozen=True)
class Row:
    """One damage type or status effect, ready to draw."""

    key: str
    label: str
    effectiveness: int
    #: False when the database has no entry and NORMAL is an assumption
    #: rather than a measurement. The overlay dims these instead of
    #: silently presenting a default as a fact.
    known: bool
    #: Finer ordering inside a coarse bucket; None when unrecorded.
    severity: int | None = None

    @property
    def description(self) -> str:
        """The word shown on the right of the row.

        Damage rows report the fine-grained severity; statuses have no
        severity in the source and report the coarse bucket, where
        "immune" and "weak" are the right words anyway.
        """
        if self.severity is not None:
            return SEVERITY_WORDS.get(self.severity, EFFECTIVENESS_LABELS[self.effectiveness])
        return EFFECTIVENESS_LABELS[self.effectiveness]

    @property
    def weight(self) -> float:
        """Bar fill, 0.0 to 1.0."""
        if self.severity is not None:
            return max(0.0, min(1.0, self.severity / MAX_SEVERITY))
        return EFFECT_WEIGHT[self.effectiveness]

    @property
    def tone(self) -> int:
        """Which of the four colours this row should take.

        Separate from `effectiveness` because the colour has to follow the
        finer severity scale: "poor" and "useless" both land in the coarse
        RESISTANT bucket, and painting them the same orange throws away the
        one distinction a player would act on. Returns an effectiveness
        constant so the UI keeps a single palette rather than two.
        """
        if self.severity is None:
            return self.effectiveness
        if self.severity >= 4:
            return WEAK
        if self.severity == 3:
            return NORMAL
        if self.severity >= 1:
            return RESISTANT
        return IMMUNE

    @property
    def is_notable(self) -> bool:
        """Worth shouting about.

        Only the extremes. Marking every resistant row as notable bolded
        five sixths of the panel, which is the same as bolding none of it.
        """
        return self.known and self.effectiveness in (WEAK, IMMUNE)


@dataclass(frozen=True)
class OverlayContent:
    """Everything the overlay draws for one boss."""

    name: str
    damage: tuple[Row, ...]
    statuses: tuple[Row, ...]
    poise: int | None = None
    note: str | None = None
    confidence: str = "unverified"

    @property
    def measured_damage(self) -> tuple[Row, ...]:
        """Only the rows the database actually has values for."""
        return tuple(row for row in self.damage if row.known)

    @property
    def weaknesses(self) -> tuple[Row, ...]:
        """Every damage type the boss is genuinely weak to.

        Plural on purpose. Margit is weak to both slash and holy, and an
        earlier version of this reported neither: it treated the tie as
        ambiguity and stayed silent. Two answers is better news than one,
        not worse.
        """
        return tuple(
            row for row in self.damage
            if row.known and row.effectiveness == WEAK
        )

    @property
    def best_damage(self) -> Row | None:
        """The type to bring, or None when nothing stands out.

        Two different questions hide here. If something is a real weakness
        it is worth naming even if another type ties it. If the top row is
        merely the least bad of a resistant set, naming it would send the
        player off to farm a weapon for a four-point difference -- so it
        only counts when it clearly beats the runner-up.
        """
        measured = self.measured_damage
        if not measured:
            return None
        top = measured[0]
        if top.effectiveness > NORMAL:
            return top
        rest = measured[1:]
        if rest and _rank(top) == _rank(rest[0]):
            return None
        return top

    @property
    def worst_damage(self) -> Row | None:
        measured = self.measured_damage
        if not measured:
            return None
        bottom = measured[-1]
        return bottom if bottom.effectiveness <= RESISTANT else None

    @property
    def immunities(self) -> tuple[Row, ...]:
        """Statuses that do literally nothing. The most actionable fact."""
        return tuple(
            row for row in self.statuses
            if row.known and row.effectiveness == IMMUNE
        )

    @property
    def weak_statuses(self) -> tuple[Row, ...]:
        return tuple(
            row for row in self.statuses
            if row.known and row.effectiveness == WEAK
        )

    def damage_highlights(self, limit: int = 4) -> tuple[Row, ...]:
        """The damage rows worth screen space, in the compact view.

        Eight rows answer a question nobody asked. The player wants two
        things: what to bring, and what to leave at home. Everything in
        between is the same shrug in different words.

        So: every genuine weakness, or the least-bad type when there is no
        weakness, plus the worst type when it is actually bad enough to
        matter. Usually two rows.
        """
        picked: list[Row] = list(self.weaknesses)
        if not picked:
            best = self.best_damage
            if best is not None:
                picked.append(best)

        worst = self.worst_damage
        if (
            worst is not None
            and worst.key not in {row.key for row in picked}
            and worst.severity is not None
            and worst.severity <= 1
        ):
            picked.append(worst)
        return tuple(picked[:limit])

    def status_highlights(self, limit: int = 4) -> tuple[Row, ...]:
        """Statuses worth showing, keeping the fixed display order.

        Same rule the OLED uses: anything that is not normal, plus bleed
        whatever its value, because "does bleed work here" is the question
        players actually ask and a missing row would read as no answer
        rather than as a normal one.
        """
        notable = [row for row in self.statuses if row.is_notable]
        if not any(row.key == "bleed" for row in notable):
            bleed = next((r for r in self.statuses if r.key == "bleed"), None)
            if bleed is not None:
                notable.append(bleed)

        order = {row.key: index for index, row in enumerate(self.statuses)}
        notable.sort(key=lambda row: order[row.key])
        return tuple(notable[:limit])

    @property
    def headline(self) -> str:
        """One line summarising the fight.

        Ordered by what changes a player's next decision most: learning
        that something is useless outranks learning that something is good,
        because it stops them wasting a fight finding out.
        """
        parts: list[str] = []
        if self.immunities:
            parts.append("immune to " + _join(self.immunities))

        weak = self.weaknesses
        best = self.best_damage
        if weak:
            parts.append("weak to " + _join(weak))
        elif best is not None:
            parts.append(f"{best.label.lower()} works best")

        weak_statuses = self.weak_statuses
        if weak_statuses:
            verb = " lands well" if len(weak_statuses) == 1 else " land well"
            parts.append(_join(weak_statuses) + verb)

        if not parts:
            # "No notable weaknesses" is a claim. For an entry added by
            # name alone it would be a false one -- the difference between
            # "we checked and it is unremarkable" and "we have no idea".
            if not self.measured_damage and not any(
                row.known for row in self.statuses
            ):
                return "no data recorded for this boss"
            parts.append("no notable weaknesses")
        return "; ".join(parts)


class OverlayDriver:
    """Decides when the overlay appears, so both entrypoints agree.

    Takes plain booleans rather than a FightSnapshot on purpose: it keeps
    this module free of the state machine, and makes the show/hide rules
    testable without building a fight.

    The rule is simply "visible exactly while a named boss is on screen".
    Redraws only on a boss change -- a phase transition like Radagon into
    the Elden Beast is a change, but the other fourteen polls a second are
    not, and repainting on each would flicker.
    """

    def __init__(self, overlay) -> None:
        self._overlay = overlay
        self._showing: str | None = None

    @property
    def showing(self) -> str | None:
        return self._showing

    def update(self, *, fighting: bool, boss: BossEntry | None) -> None:
        if fighting and boss is not None:
            if boss.key != self._showing:
                self._showing = boss.key
                self._overlay.show(build_content(boss))
            return
        if self._showing is not None:
            self._showing = None
            self._overlay.hide()

    def close(self) -> None:
        self._showing = None
        self._overlay.stop()


def _join(rows: tuple[Row, ...]) -> str:
    labels = [row.label.lower() for row in rows]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _rank(row: Row) -> tuple[int, int]:
    """Sort weight for a row, coarse bucket first then the tie-break."""
    return (row.effectiveness, row.severity if row.severity is not None else 0)


def build_content(entry: BossEntry) -> OverlayContent:
    """Turn a database row into a full, ordered overlay payload."""
    return OverlayContent(
        name=entry.name,
        damage=_damage_rows(entry),
        statuses=_status_rows(entry),
        poise=entry.poise,
        note=entry.note,
        confidence=entry.confidence,
    )


def _damage_rows(entry: BossEntry) -> tuple[Row, ...]:
    """All eight types, best first, with unrecorded ones last.

    Unrecorded types sort to the bottom regardless of value. They default
    to NORMAL, which on a boss that resists everything would float an
    assumption above eight measured values and read as the recommendation.
    Malenia has no fire entry, and that is exactly what happened: the
    overlay's top line said Fire.

    Below that, ties break on `severity` then canonical order, so the list
    is stable -- a player learns where to look instead of re-reading.
    """
    rows = [
        Row(
            key=name,
            label=DAMAGE_LABELS[name],
            effectiveness=entry.damage_effectiveness(name),
            # `damage` is pruned on import: NORMAL entries are dropped
            # because `damage_effectiveness` already defaults to NORMAL.
            # So absence there does not mean absence of data -- 233 values
            # in the shipped database are recorded neutrals, and treating
            # them as unknown reported "no data" for a third of the sheet.
            # `severity` is not pruned, so it is the reliable witness.
            known=name in entry.damage or name in entry.severity,
            severity=entry.severity.get(name),
        )
        for name in DAMAGE_TYPES
    ]
    rows.sort(
        key=lambda row: (
            not row.known,
            -row.effectiveness,
            -(row.severity if row.severity is not None else 0),
            DAMAGE_TYPES.index(row.key),
        )
    )
    return tuple(rows)


def _status_rows(entry: BossEntry) -> tuple[Row, ...]:
    """Statuses in fixed display order -- deliberately not sorted by value.

    Damage types are a shopping list, so ranking them helps. Statuses are
    a lookup: the player wants to know about bleed specifically, and
    moving it around by boss would make it slower to find, not faster.
    """
    return tuple(
        Row(
            key=name,
            label=STATUS_LABELS[name],
            effectiveness=entry.status(name),
            known=name in entry.statuses,
        )
        for name in STATUS_ORDER
    )
