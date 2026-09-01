"""Fuzzy matching of OCR output against the closed set of boss names.

The central insight of this project: we are not doing open-vocabulary OCR.
Elden Ring has a fixed, known roster of named bosses, so recognition is a
*classification* problem over ~165 candidates. That means the OCR layer is
allowed to be sloppy -- we only need enough signal to pick the nearest
entry in a known list.

Normalised Levenshtein distance handles the characteristic OCR failure
modes well (O/0, I/1/l, rn/m, dropped punctuation).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

# Characters OCR engines routinely confuse. Folding them into a single
# representative before comparison buys accuracy for free.
_CONFUSION_FOLD = str.maketrans(
    {
        "0": "O",
        "1": "I",
        "L": "I",
        "5": "S",
        "8": "B",
        "2": "Z",
        "6": "G",
    }
)

_NON_ALNUM = re.compile(r"[^A-Z ]+")
_WHITESPACE = re.compile(r"\s+")
#: Removed outright rather than turned into a space. An apostrophe sits
#: *inside* a word -- "O'Neil", "Fia's" -- so spacing it would make
#: "COMMANDER O NEIL", which no longer equals the "COMMANDER ONEIL" that
#: comes back from an engine not allowed to emit the character.
_INTRAWORD = re.compile(r"['\u2018\u2019\u02bc`]")


def normalise(text: str) -> str:
    """Canonicalise a name for comparison.

    Uppercases, strips accents and punctuation, folds commonly-confused
    glyphs, and removes whitespace entirely. Deliberately lossy.

    Spaces go because they cannot be read reliably and do not identify
    anything. A word break is inferred from the gap between glyph boxes,
    and the threshold that gets it right on one plate gets it wrong on
    another: measured on two real captures, "God-Devouring Serpent" has a
    word space of 13px with letter gaps up to 11, while "Night's Cavalry"
    has a word space of 14px with a letter gap of 13. No single cutoff
    satisfies both, and no ratio of glyph width or height does either.

    Rather than keep tuning a number that cannot work, spacing is dropped
    from the comparison. No two bosses differ only in where their spaces
    fall, so nothing is lost -- and a plate that reads "Night s Cavalry"
    now matches exactly instead of costing two edits of distance.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    upper = ascii_only.upper()
    folded = upper.translate(_CONFUSION_FOLD)
    joined = _INTRAWORD.sub("", folded)
    stripped = _NON_ALNUM.sub(" ", joined)
    return _WHITESPACE.sub("", stripped)


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance, iterative with a single row of state."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Keep the inner loop over the shorter string.
    if len(a) < len(b):
        a, b = b, a

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (ca != cb)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """Normalised similarity in [0.0, 1.0]; 1.0 means identical."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - (levenshtein(a, b) / longest)


@dataclass(frozen=True)
class MatchResult:
    key: str
    display_name: str
    confidence: float
    runner_up: str | None = None
    margin: float = 0.0


class BossNameMatcher:
    """Maps noisy OCR text onto a known boss.

    `margin` -- the gap between best and second-best candidate -- is tracked
    separately from raw confidence. A high-confidence match that barely beats
    its runner-up is a coin flip between two similarly-named bosses, and the
    caller may want to treat it as unresolved.
    """

    def __init__(
        self,
        names: dict[str, str],
        *,
        threshold: float = 0.62,
        min_margin: float = 0.0,
    ) -> None:
        """`names` maps a stable key to the human-readable display name."""
        if not names:
            raise ValueError("matcher requires at least one candidate name")
        self._threshold = threshold
        self._min_margin = min_margin
        self._display = dict(names)
        self._normalised = {key: normalise(value) for key, value in names.items()}
        # Aliases let short forms ("MALENIA") resolve without penalty.
        self._aliases: dict[str, str] = {}

    def add_alias(self, key: str, alias: str) -> None:
        if key not in self._display:
            raise KeyError(f"unknown boss key: {key}")
        self._aliases[normalise(alias)] = key

    @property
    def threshold(self) -> float:
        return self._threshold

    def match(self, observed: str) -> MatchResult | None:
        """Return the best candidate, or None if nothing clears the bar."""
        target = normalise(observed)
        if not target:
            return None

        if target in self._aliases:
            key = self._aliases[target]
            return MatchResult(key, self._display[key], 1.0, None, 1.0)

        scored: list[tuple[float, str]] = []
        for key, candidate in self._normalised.items():
            score = similarity(target, candidate)
            # A clean substring hit ("MALENIA" inside "MALENIA BLADE OF
            # MIQUELLA") is strong evidence that raw edit distance punishes
            # purely for length. Floor the score in that case.
            if len(target) >= 6 and target in candidate:
                score = max(score, 0.90)
            scored.append((score, key))

        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        best_score, best_key = scored[0]
        runner_up_key = scored[1][1] if len(scored) > 1 else None
        margin = best_score - scored[1][0] if len(scored) > 1 else best_score

        if best_score < self._threshold:
            return None
        if margin < self._min_margin:
            return None

        return MatchResult(
            key=best_key,
            display_name=self._display[best_key],
            confidence=best_score,
            runner_up=runner_up_key,
            margin=margin,
        )

    @classmethod
    def from_entries(
        cls, entries: Iterable, **kwargs
    ) -> "BossNameMatcher":
        """Build from BossEntry objects, wiring up their aliases."""
        entry_list: Sequence = list(entries)
        matcher = cls({e.key: e.name for e in entry_list}, **kwargs)
        for entry in entry_list:
            for alias in entry.aliases:
                matcher.add_alias(entry.key, alias)
        return matcher
