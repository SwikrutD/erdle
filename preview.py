#!/usr/bin/env python3
"""Render boss screens to the terminal exactly as they will appear on the OLED.

    python preview.py              # every boss in the database
    python preview.py malenia      # one boss
    python preview.py --png out/   # also write 4x PNGs (needs Pillow)

Unit tests confirm the layout fits and does not clip. This confirms it is
actually legible, which is a different question.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erdle.bossdb import BossDatabase, default_data_path  # noqa: E402
from erdle.canvas import Canvas  # noqa: E402
from erdle.render import (  # noqa: E402
    advice_row,
    display_name,
    render_boss_screen,
    render_idle_screen,
    render_unknown_boss,
    status_row,
)


def show(canvas: Canvas, title: str = "") -> None:
    if title:
        print(f"\n{title}")
    print("  +" + "-" * canvas.width + "+")
    for row in canvas.to_rows():
        print("  |" + row.replace("#", "█").replace(".", " ") + "|")
    print("  +" + "-" * canvas.width + "+")


def write_png(canvas: Canvas, path: Path, scale: int = 4) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    image = Image.new("L", (canvas.width, canvas.height))
    image.putdata(
        [255 if canvas.get(x, y) else 0
         for y in range(canvas.height) for x in range(canvas.width)]
    )
    image = image.resize(
        (canvas.width * scale, canvas.height * scale), Image.NEAREST
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return True


def main() -> int:
    args = [a for a in sys.argv[1:]]
    png_dir = None
    if "--png" in args:
        index = args.index("--png")
        png_dir = Path(args[index + 1])
        del args[index : index + 2]

    database = BossDatabase.load(default_data_path())
    keys = args or [entry.key for entry in database]

    print(f"loaded {len(database)} bosses from {default_data_path().name}")
    print(f"source: {database.meta.get('source')}\n")

    show(render_idle_screen(), "[idle]")
    show(render_unknown_boss(fill_ratio=0.62), "[bar detected, name unresolved]")

    for key in keys:
        entry = database.get(key)
        if entry is None:
            print(f"!! no boss named {key!r}", file=sys.stderr)
            continue
        canvas = render_boss_screen(entry, fill_ratio=0.62)
        show(canvas, f"[{entry.key}] {entry.name}  (confidence: {entry.confidence})")
        print(f"    name  -> {display_name(entry)!r}")
        print(f"    stat  -> {status_row(entry)!r}")
        print(f"    dmg   -> {advice_row(entry)!r}")
        if entry.note:
            print(f"    note  -> {entry.note}   (too long for the panel)")
        if png_dir is not None:
            ok = write_png(canvas, png_dir / f"{entry.key}.png")
            if not ok:
                print("    (install Pillow to export PNGs)")
                png_dir = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
