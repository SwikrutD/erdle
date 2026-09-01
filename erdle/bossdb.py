"""Boss weakness database: schema, loading, validation, and display ranking.

Effectiveness is stored on a deliberately coarse 0-3 scale:

    0 = immune       (does nothing at all)
    1 = resistant    (works, but poorly -- bring something else)
    2 = normal
    3 = weak         (notably effective)

The coarseness is intentional. A 128x40 monochrome panel cannot render
fourteen precise negation percentages, and the only decision the numbers
actually drive -- "what do I bring to this fight" -- is answered fine by
four buckets. Exact param values can be layered in later via the optional
`negation` field without changing the display path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMMUNE, RESISTANT, NORMAL, WEAK = 0, 1, 2, 3

EFFECTIVENESS_LABELS = {
    IMMUNE: "immune",
    RESISTANT: "resistant",
    NORMAL: "normal",
    WEAK: "weak",
}

# Status effects, in the order they earn their pixels on screen. Bleed first
# because "does bleed work" is the single most actionable question in an
# Elden Ring boss fight.
STATUS_ORDER = ("bleed", "frost", "rot", "poison", "sleep", "madness")
STATUS_ABBREV = {
    "bleed": "BLD",
    "frost": "FRS",
    "rot": "ROT",
    "poison": "PSN",
    "sleep": "SLP",
    "madness": "MAD",
}

DAMAGE_TYPES = (
    "standard",
    "slash",
    "strike",
    "pierce",
    "magic",
    "fire",
    "lightning",
    "holy",
)

VALID_EFFECTIVENESS = set(EFFECTIVENESS_LABELS)


class BossDataError(ValueError):
    """Raised when the boss database is structurally invalid."""


@dataclass(frozen=True)
class BossEntry:
    key: str
    name: str
    statuses: dict[str, int]
    damage: dict[str, int]
    poise: int | None = None
    note: str | None = None
    confidence: str = "unverified"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # Optional hand-written display name. The automatic shortener splits on
    # commas and "of"/"the", which handles most Elden Ring titles, but names
    # like "Erdtree Burial Watchdog" have no split point and would be
    # truncated mid-word.
    short: str | None = None
    # Finer ordering within a coarse bucket, higher is better to bring.
    # Several damage types can all be "resistant" on the 0-3 scale while
    # differing a lot in practice; this decides which one is worth the
    # panel space without widening the displayed scale.
    severity: dict[str, int] = field(default_factory=dict)

    def status(self, name: str) -> int:
        """Effectiveness of a status, defaulting to NORMAL when unrecorded."""
        return self.statuses.get(name, NORMAL)

    def damage_effectiveness(self, name: str) -> int:
        return self.damage.get(name, NORMAL)

    def best_damage_types(self, limit: int = 2) -> list[str]:
        """Damage types worth mentioning, best first.

        Only returns types strictly better than normal; there is no value in
        telling the player that standard damage is standard.
        """
        ranked = [
            (score, self.severity.get(name, 3), name)
            for name, score in self.damage.items()
            if score > NORMAL and name in DAMAGE_TYPES
        ]
        if not ranked and self.severity:
            # Some bosses resist everything -- the Burial Watchdog resists
            # all eight types. Reporting nothing is honest but useless: the
            # player still has to hit it with something, and "least bad" is
            # the advice they actually want. Only ever one, so the panel
            # cannot imply a resisted type is genuinely good.
            best = max(self.severity.items(), key=lambda kv: kv[1])
            return [best[0]] if best[0] in DAMAGE_TYPES else []
        ranked.sort(key=lambda t: (-t[0], -t[1], DAMAGE_TYPES.index(t[2])))
        return [name for _, _, name in ranked[:limit]]

    def worst_damage_types(self, limit: int = 2) -> list[str]:
        """Damage types to avoid, worst first."""
        ranked = [
            (score, self.severity.get(name, 3), name)
            for name, score in self.damage.items()
            if score < NORMAL and name in DAMAGE_TYPES
        ]
        ranked.sort(key=lambda t: (t[0], t[1], DAMAGE_TYPES.index(t[2])))
        return [name for _, _, name in ranked[:limit]]

    def status_summary(self, limit: int = 4) -> list[tuple[str, int]]:
        """The statuses worth showing, in fixed display order.

        Only statuses that carry information appear: telling the player
        that poison is exactly as effective as usual costs four characters
        of a nineteen-character line and changes nothing. Bleed is the one
        exception and is always shown, because "bleed does normal damage
        here" is itself the answer to the question players ask most.

        Returns fewer than `limit` entries when there is less to say.
        """
        informative = [
            (name, self.status(name))
            for name in STATUS_ORDER
            if name in self.statuses and self.status(name) != NORMAL
        ]
        if not any(name == "bleed" for name, _ in informative):
            informative.append(("bleed", self.status("bleed")))
        ordered = sorted(informative, key=lambda pair: STATUS_ORDER.index(pair[0]))
        return ordered[:limit]


def _validate_effectiveness_map(
    raw: Any, allowed: tuple[str, ...], where: str
) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BossDataError(f"{where}: expected an object, got {type(raw).__name__}")
    result: dict[str, int] = {}
    for name, value in raw.items():
        if name not in allowed:
            raise BossDataError(f"{where}: unknown key {name!r}")
        if value not in VALID_EFFECTIVENESS:
            raise BossDataError(
                f"{where}.{name}: effectiveness must be 0-3, got {value!r}"
            )
        result[name] = int(value)
    return result


def parse_entry(key: str, raw: dict[str, Any]) -> BossEntry:
    if not isinstance(raw, dict):
        raise BossDataError(f"{key}: entry must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BossDataError(f"{key}: missing display name")

    poise = raw.get("poise")
    if poise is not None and (not isinstance(poise, int) or poise < 0):
        raise BossDataError(f"{key}.poise: expected a non-negative integer")

    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise BossDataError(f"{key}.aliases: expected a list of strings")

    return BossEntry(
        key=key,
        name=name,
        statuses=_validate_effectiveness_map(
            raw.get("statuses"), STATUS_ORDER, f"{key}.statuses"
        ),
        damage=_validate_effectiveness_map(
            raw.get("damage"), DAMAGE_TYPES, f"{key}.damage"
        ),
        poise=poise,
        note=raw.get("note"),
        confidence=raw.get("confidence", "unverified"),
        aliases=tuple(aliases),
        short=raw.get("short"),
        severity={
            k: int(v) for k, v in (raw.get("severity") or {}).items()
            if k in DAMAGE_TYPES and isinstance(v, (int, float))
        },
    )


class BossDatabase:
    def __init__(self, entries: dict[str, BossEntry], meta: dict[str, Any] | None = None):
        self._entries = entries
        self.meta = meta or {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self):
        return iter(self._entries.values())

    def get(self, key: str) -> BossEntry | None:
        return self._entries.get(key)

    def require(self, key: str) -> BossEntry:
        entry = self._entries.get(key)
        if entry is None:
            raise KeyError(f"no boss with key {key!r}")
        return entry

    def names(self) -> dict[str, str]:
        return {key: entry.name for key, entry in self._entries.items()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BossDatabase":
        if not isinstance(payload, dict):
            raise BossDataError("top level of boss data must be an object")
        raw_entries = payload.get("bosses")
        if not isinstance(raw_entries, dict) or not raw_entries:
            raise BossDataError("boss data must contain a non-empty 'bosses' object")

        entries: dict[str, BossEntry] = {}
        seen_names: dict[str, str] = {}
        for key, raw in raw_entries.items():
            entry = parse_entry(key, raw)
            lowered = entry.name.lower()
            if lowered in seen_names:
                raise BossDataError(
                    f"duplicate display name {entry.name!r} "
                    f"({key} and {seen_names[lowered]})"
                )
            seen_names[lowered] = key
            entries[key] = entry

        # An alias equal to another boss's real name would shadow it:
        # exact alias hits score 1.0, so the wrong entry wins outright.
        for entry in entries.values():
            for alias in entry.aliases:
                owner = seen_names.get(alias.strip().lower())
                if owner is not None and owner != entry.key:
                    raise BossDataError(
                        f"{entry.key}: alias {alias!r} shadows the real name "
                        f"of {owner}"
                    )

        meta = {k: v for k, v in payload.items() if k != "bosses"}
        return cls(entries, meta)

    @classmethod
    def load(cls, path: str | Path) -> "BossDatabase":
        text = Path(path).read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BossDataError(f"{path}: invalid JSON -- {exc}") from exc
        return cls.from_dict(payload)


def default_data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "bosses.json"
