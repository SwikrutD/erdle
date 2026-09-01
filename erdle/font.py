"""A self-contained 5x7 bitmap font.

Deliberately dependency-free. Pillow would render nicer glyphs, but a
128x40 1-bit panel gains nothing from antialiasing and everything from
predictable pixel alignment -- and shipping our own table means the OLED
output is byte-identical on every machine, which makes it testable.

Glyphs are authored as ASCII art so they can be eyeballed in review. They
are parsed once at import.
"""

from __future__ import annotations

GLYPH_WIDTH = 5
GLYPH_HEIGHT = 7
ADVANCE = GLYPH_WIDTH + 1  # one column of letter spacing

_GLYPH_SOURCE: dict[str, str] = {
    " ": "     /     /     /     /     /     /     ",
    "A": " ### /#   #/#   #/#####/#   #/#   #/#   #",
    "B": "#### /#   #/#   #/#### /#   #/#   #/#### ",
    "C": " ### /#   #/#    /#    /#    /#   #/ ### ",
    "D": "#### /#   #/#   #/#   #/#   #/#   #/#### ",
    "E": "#####/#    /#    /#### /#    /#    /#####",
    "F": "#####/#    /#    /#### /#    /#    /#    ",
    "G": " ### /#   #/#    /#  ##/#   #/#   #/ ### ",
    "H": "#   #/#   #/#   #/#####/#   #/#   #/#   #",
    "I": " ### /  #  /  #  /  #  /  #  /  #  / ### ",
    "J": "    #/    #/    #/    #/#   #/#   #/ ### ",
    "K": "#   #/#  # /# #  /##   /# #  /#  # /#   #",
    "L": "#    /#    /#    /#    /#    /#    /#####",
    "M": "#   #/## ##/# # #/#   #/#   #/#   #/#   #",
    "N": "#   #/##  #/# # #/#  ##/#   #/#   #/#   #",
    "O": " ### /#   #/#   #/#   #/#   #/#   #/ ### ",
    "P": "#### /#   #/#   #/#### /#    /#    /#    ",
    "Q": " ### /#   #/#   #/#   #/# # #/#  # / ## #",
    "R": "#### /#   #/#   #/#### /# #  /#  # /#   #",
    "S": " ####/#    /#    / ### /    #/    #/#### ",
    "T": "#####/  #  /  #  /  #  /  #  /  #  /  #  ",
    "U": "#   #/#   #/#   #/#   #/#   #/#   #/ ### ",
    "V": "#   #/#   #/#   #/#   #/#   #/ # # /  #  ",
    "W": "#   #/#   #/#   #/#   #/# # #/## ##/#   #",
    "X": "#   #/#   #/ # # /  #  / # # /#   #/#   #",
    "Y": "#   #/#   #/ # # /  #  /  #  /  #  /  #  ",
    "Z": "#####/    #/   # /  #  / #   /#    /#####",
    "0": " ### /#   #/#  ##/# # #/##  #/#   #/ ### ",
    "1": "  #  / ##  /  #  /  #  /  #  /  #  / ### ",
    "2": " ### /#   #/    #/   # /  #  / #   /#####",
    "3": "#####/   # /  ## /    #/    #/#   #/ ### ",
    "4": "   # /  ## / # # /#  # /#####/   # /   # ",
    "5": "#####/#    /#### /    #/    #/#   #/ ### ",
    "6": "  ## / #   /#    /#### /#   #/#   #/ ### ",
    "7": "#####/    #/   # /  #  / #   / #   / #   ",
    "8": " ### /#   #/#   #/ ### /#   #/#   #/ ### ",
    "9": " ### /#   #/#   #/ ####/    #/   # / ##  ",
    ".": "     /     /     /     /     /  ## /  ## ",
    ",": "     /     /     /     /  ## /  ## /  #  ",
    "'": "  #  /  #  /     /     /     /     /     ",
    "-": "     /     /     /#####/     /     /     ",
    "+": "     /  #  /  #  /#####/  #  /  #  /     ",
    ":": "     /  ## /  ## /     /  ## /  ## /     ",
    "/": "    #/    #/   # /  #  / #   /#    /#    ",
    "!": "  #  /  #  /  #  /  #  /  #  /     /  #  ",
    "?": " ### /#   #/    #/   # /  #  /     /  #  ",
    "%": "#   #/#  # /   # /  #  / #   / #  #/#   #",
    "(": "   # /  #  / #   / #   / #   /  #  /   # ",
    ")": " #   /  #  /   # /   # /   # /  #  / #   ",
    "*": "     / # # / ### /#####/ ### / # # /     ",
    "=": "     /     /#####/     /#####/     /     ",
    "<": "   # /  #  / #   /#    / #   /  #  /   # ",
    ">": " #   /  #  /   # /    #/   # /  #  / #   ",
    "·": "     /     /     /  #  /     /     /     ",
}

FALLBACK_CHAR = "?"


def _parse(source: str) -> list[list[int]]:
    rows = source.split("/")
    if len(rows) != GLYPH_HEIGHT:
        raise ValueError(f"glyph must have {GLYPH_HEIGHT} rows, got {len(rows)}")
    parsed = []
    for row in rows:
        if len(row) != GLYPH_WIDTH:
            raise ValueError(f"glyph row must be {GLYPH_WIDTH} wide: {row!r}")
        parsed.append([1 if c == "#" else 0 for c in row])
    return parsed


GLYPHS: dict[str, list[list[int]]] = {
    char: _parse(source) for char, source in _GLYPH_SOURCE.items()
}


def glyph_for(char: str) -> list[list[int]]:
    """Look up a glyph, folding case and falling back for unknowns."""
    if char in GLYPHS:
        return GLYPHS[char]
    upper = char.upper()
    if upper in GLYPHS:
        return GLYPHS[upper]
    return GLYPHS[FALLBACK_CHAR]


def text_width(text: str) -> int:
    """Rendered width in pixels, excluding the trailing letter-space."""
    if not text:
        return 0
    return len(text) * ADVANCE - 1


def fit_text(text: str, max_width: int) -> str:
    """Truncate to fit, without an ellipsis (every pixel counts)."""
    if text_width(text) <= max_width:
        return text
    max_chars = (max_width + 1) // ADVANCE
    return text[: max(max_chars, 0)]
