"""Composes the 128x40 OLED screen.

Layout budget, top to bottom:

    y=0..6    boss short name
    y=9..15   status effectiveness row  (BLD+ FRS+ ROT- SLP-)
    y=18..24  damage advice + poise
    y=28..35  health bar with percentage

Everything is driven by what a player can act on. Precise negation
percentages are omitted because nobody changes their build mid-swing over
four points of slash negation, whereas "bleed does nothing here" changes
the next ten minutes.
"""

from __future__ import annotations

from .bossdb import (
    IMMUNE,
    NORMAL,
    RESISTANT,
    STATUS_ABBREV,
    WEAK,
    BossEntry,
)
from .canvas import Canvas, progress_bar
from .font import ADVANCE, fit_text, text_width

NAME_Y = 0
STATUS_Y = 9
ADVICE_Y = 18
BAR_Y = 28
BAR_HEIGHT = 8

# Effectiveness glyphs. '+' reads as "bring this", '-' as "don't bother",
# 'x' as "does literally nothing".
EFFECT_MARK = {
    IMMUNE: "x",
    RESISTANT: "-",
    NORMAL: "=",
    WEAK: "+",
}

_SPLIT_TOKENS = (",", " of ", " the ")

# Four characters each, so the advice row's width is predictable.
DAMAGE_ABBREV = {
    "standard": "PHYS",
    "slash": "SLSH",
    "strike": "STRK",
    "pierce": "PRC",
    "magic": "MAG",
    "fire": "FIRE",
    "lightning": "LTNG",
    "holy": "HOLY",
}


def display_name(entry: BossEntry, max_width: int = 128) -> str:
    """The entry's own short name if it has one, else derive one."""
    if entry.short:
        return fit_text(entry.short.upper(), max_width)
    return short_name(entry.name, max_width)


def short_name(name: str, max_width: int = 128) -> str:
    """Reduce a full boss title to something that fits on 128px.

    Elden Ring names are epithet-heavy ("Malenia, Blade of Miquella"), and
    the distinguishing part is almost always first.
    """
    candidate = name.strip()
    if text_width(candidate.upper()) <= max_width:
        return candidate.upper()

    for token in _SPLIT_TOKENS:
        if token in candidate:
            head = candidate.split(token)[0].strip()
            if head and text_width(head.upper()) <= max_width:
                return head.upper()

    return fit_text(candidate.upper(), max_width)


def status_row(entry: BossEntry, limit: int = 4) -> str:
    """Build the status line, e.g. 'BLD+ FRS+ ROT- SLP-'."""
    parts = []
    for name, effectiveness in entry.status_summary(limit=limit):
        parts.append(f"{STATUS_ABBREV[name]}{EFFECT_MARK[effectiveness]}")
    return " ".join(parts)


def advice_row(entry: BossEntry) -> str:
    """Damage-type advice plus poise, e.g. '+LTNG -FIRE P100'.

    Fixed-width abbreviations keep this line's length predictable, which
    matters because there is no room to wrap.
    """
    fragments: list[str] = []
    for name in entry.best_damage_types(limit=2):
        fragments.append("+" + DAMAGE_ABBREV[name])
    for name in entry.worst_damage_types(limit=1):
        fragments.append("-" + DAMAGE_ABBREV[name])
    if entry.poise is not None:
        fragments.append(f"P{entry.poise}")

    # Drop whole fields rather than cutting a token in half. Truncating
    # "P100" to "P10" reports a poise value that is simply wrong, which is
    # worse than omitting it -- and it happened on the Fire Giant.
    while fragments and text_width(" ".join(fragments).upper()) > 128:
        fragments.pop()
    return " ".join(fragments).upper()


def render_boss_screen(
    entry: BossEntry,
    *,
    fill_ratio: float | None = None,
    canvas: Canvas | None = None,
) -> Canvas:
    canvas = canvas or Canvas()
    canvas.clear()

    canvas.draw_text_centered(display_name(entry), NAME_Y)

    statuses = status_row(entry)
    if statuses:
        canvas.draw_text_centered(statuses, STATUS_Y)

    advice = advice_row(entry)
    if advice:
        canvas.draw_text_centered(advice, ADVICE_Y)

    if fill_ratio is not None:
        _draw_health(canvas, fill_ratio)

    return canvas


def _draw_health(canvas: Canvas, fill_ratio: float) -> None:
    percent = int(round(max(0.0, min(1.0, fill_ratio)) * 100))
    label = f"{percent}%"
    label_width = text_width(label)
    bar_width = canvas.width - label_width - 3
    progress_bar(canvas, 0, BAR_Y, bar_width, BAR_HEIGHT, fill_ratio)
    canvas.draw_text(label, bar_width + 3, BAR_Y + 1)


# Outcome screens. Two centred lines, vertically balanced on the panel.
OUTCOME_LINE_1_Y = 12
OUTCOME_LINE_2_Y = 22

VICTORY_LINES = ("GOOD JOB", "TARNISHED")
DEFEAT_LINES = ("GIT GUD", "TARNISHED")


def render_message_screen(
    first: str, second: str = "", *, canvas: Canvas | None = None
) -> Canvas:
    """Two centred lines. Used for the win and lose messages."""
    canvas = canvas or Canvas()
    canvas.clear()
    canvas.draw_text_centered(fit_text(first.upper(), canvas.width), OUTCOME_LINE_1_Y)
    if second:
        canvas.draw_text_centered(
            fit_text(second.upper(), canvas.width), OUTCOME_LINE_2_Y
        )
    return canvas


def render_victory_screen(*, canvas: Canvas | None = None) -> Canvas:
    """Shown after ENEMY / GREAT ENEMY / DEMIGOD FELLED."""
    return render_message_screen(*VICTORY_LINES, canvas=canvas)


def render_defeat_screen(*, canvas: Canvas | None = None) -> Canvas:
    """Shown after YOU DIED."""
    return render_message_screen(*DEFEAT_LINES, canvas=canvas)


def render_idle_screen(message: str = "ERDLE", *, canvas: Canvas | None = None) -> Canvas:
    canvas = canvas or Canvas()
    canvas.clear()
    canvas.draw_text_centered(fit_text(message.upper(), 128), 16)
    return canvas


def render_unknown_boss(
    *, fill_ratio: float | None = None, canvas: Canvas | None = None
) -> Canvas:
    """Bar is up but the name did not resolve.

    Still worth showing the health bar -- the mirror is useful even when
    the cheat sheet is not.
    """
    canvas = canvas or Canvas()
    canvas.clear()
    canvas.draw_text_centered("UNKNOWN BOSS", NAME_Y)
    canvas.draw_text_centered("NO DATA", STATUS_Y)
    if fill_ratio is not None:
        _draw_health(canvas, fill_ratio)
    return canvas
