#!/usr/bin/env python3
"""Merge hand-researched boss stats from tools/sources/worksheet.csv in.

    python tools/import_worksheet.py --check     # validate, change nothing
    python tools/import_worksheet.py             # merge it in

Fill in whatever you can find and leave the rest blank. A blank cell is
left alone rather than overwritten, so the file can be filled in over
several sittings, and a boss with three known values is worth having.

Each cell accepts either a word or a number:

    weak | normal | resistant | immune       both damage and status
    1.2 | 1.0 | 0.8 | 0.65 | 0.2             a damage multiplier
    999 | 542 | 252 | 154                    a status resistance value

The numbers are what the game's own NpcParam holds -- a damage multiplier
above 1.0 means the boss takes *more*, and a status value is the build-up
needed to proc, so lower is easier and 999 is immune. Wiki tables usually
quote one form or the other; both are handled, and the numeric form is
better because it carries the finer ordering the words throw away.

`poise` is a whole number. `source` is free text: a URL, or "wiki", or
whatever will remind you where a suspicious value came from.

Entries filled in here are marked `confidence: "manual"` -- below the
game's own data, above a guess.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erdle.bossdb import IMMUNE, NORMAL, RESISTANT, WEAK  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORKSHEET = ROOT / "tools" / "sources" / "worksheet.csv"
DATABASE = ROOT / "data" / "bosses.json"

DAMAGE = ("standard", "slash", "strike", "pierce",
          "magic", "fire", "lightning", "holy")
STATUS = ("poison", "rot", "bleed", "frost", "sleep", "madness")

WORDS = {
    "weak": WEAK, "w": WEAK,
    "normal": NORMAL, "n": NORMAL, "neutral": NORMAL,
    "resistant": RESISTANT, "r": RESISTANT, "resist": RESISTANT,
    "immune": IMMUNE, "i": IMMUNE,
}


def damage_from(cell: str, where: str) -> tuple[int, int] | None:
    """A damage cell as (bucket, severity), or None when blank."""
    cell = cell.strip().lower()
    if not cell:
        return None
    if cell in WORDS:
        bucket = WORDS[cell]
        # Words carry no finer ordering, so severity takes the middle of
        # the bucket. A multiplier is better precisely because it does.
        return bucket, {WEAK: 4, NORMAL: 3, RESISTANT: 1, IMMUNE: 0}[bucket]
    try:
        rate = float(cell)
    except ValueError:
        raise ValueError(f"{where}: {cell!r} is neither a word nor a number")
    if not 0 <= rate <= 3:
        raise ValueError(f"{where}: {rate} is not a plausible multiplier")
    bucket = WEAK if rate > 1.02 else (NORMAL if rate >= 0.98 else RESISTANT)
    for cut, value in ((1.15, 5), (1.02, 4), (0.98, 3), (0.85, 2), (0.55, 1)):
        if rate >= cut:
            return bucket, value
    return bucket, 0


def status_from(cell: str, where: str) -> int | None:
    cell = cell.strip().lower()
    if not cell:
        return None
    if cell in WORDS:
        return WORDS[cell]
    try:
        value = float(cell)
    except ValueError:
        raise ValueError(f"{where}: {cell!r} is neither a word nor a number")
    if value >= 999:
        return IMMUNE
    if value >= 500:
        return RESISTANT
    if value >= 200:
        return NORMAL
    return WEAK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="validate and report, write nothing")
    parser.add_argument("--worksheet", type=Path, default=WORKSHEET)
    args = parser.parse_args()

    if not args.worksheet.exists():
        print(f"no worksheet at {args.worksheet}", file=sys.stderr)
        return 1

    doc = json.loads(DATABASE.read_text(encoding="utf-8"))
    bosses = doc["bosses"]

    problems: list[str] = []
    touched = filled = 0

    with args.worksheet.open(encoding="utf-8-sig", newline="") as fh:
        for line, row in enumerate(csv.DictReader(fh), start=2):
            key = (row.get("key") or "").strip()
            if not key:
                continue
            entry = bosses.get(key)
            if entry is None:
                problems.append(f"line {line}: no boss called {key!r}")
                continue

            damage = dict(entry.get("damage", {}))
            severity = dict(entry.get("severity", {}))
            statuses = dict(entry.get("statuses", {}))
            cells = 0

            try:
                for field in DAMAGE:
                    got = damage_from(row.get(field, ""), f"line {line} {field}")
                    if got is None:
                        continue
                    bucket, sev = got
                    severity[field] = sev
                    # NORMAL is pruned, matching the importer, so absence
                    # keeps meaning normal rather than unknown.
                    if bucket == NORMAL:
                        damage.pop(field, None)
                    else:
                        damage[field] = bucket
                    cells += 1

                for field in STATUS:
                    got = status_from(row.get(field, ""), f"line {line} {field}")
                    if got is None:
                        continue
                    statuses[field] = got
                    cells += 1

                poise_cell = (row.get("poise") or "").strip()
                poise = None
                if poise_cell:
                    poise = int(float(poise_cell))
                    if poise < 0:
                        raise ValueError(f"line {line}: poise {poise} is negative")
                    cells += 1
            except ValueError as exc:
                problems.append(str(exc))
                continue

            if not cells:
                continue

            touched += 1
            filled += cells
            if args.check:
                continue

            entry["damage"] = damage
            entry["severity"] = severity
            entry["statuses"] = statuses
            if poise is not None:
                entry["poise"] = poise
            # Only claim "manual" for values a person actually researched.
            # The worksheet ships pre-filled with the spreadsheet's own
            # numbers so gaps are visible; re-importing those unchanged
            # would relabel 43 entries as hand-checked when nobody
            # checked them.
            source = (row.get("source") or "").strip()
            if entry.get("confidence") != "regulation" and source:
                entry["confidence"] = "manual"
            if source:
                entry["note"] = source

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problems; nothing written", file=sys.stderr)
        return 1

    print(f"{touched} bosses, {filled} values")
    if args.check:
        print("(--check: nothing written)")
        return 0

    doc["bosses"] = dict(sorted(bosses.items()))
    DATABASE.write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"written to {DATABASE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
