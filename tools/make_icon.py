#!/usr/bin/env python3
"""Generate assets/erdle.ico -- the icon baked into ERDLE.exe.

    python tools/make_icon.py                # the built-in drawn mark
    python tools/make_icon.py my_art.png     # your own image

Keeping one source for the mark means the taskbar icon and the tray icon
cannot drift apart.

To change only the *tray* icon, no rebuild is needed: drop a PNG at
%APPDATA%\\erdle\\icon.png and restart ERDLE. Rebuilding is only required
for the icon Windows shows on the exe itself.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("needs `pip install pillow`", file=sys.stderr)
        return 1

    out = Path(__file__).resolve().parent.parent / "assets" / "erdle.ico"
    out.parent.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
        if not source.exists():
            print(f"no such file: {source}", file=sys.stderr)
            return 1
        base = Image.open(source).convert("RGBA")
        # Square it off first. A non-square source stretches badly at
        # 16x16, which is the size that actually gets looked at.
        side = max(base.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(
            base, ((side - base.width) // 2, (side - base.height) // 2)
        )
        base = canvas.resize((256, 256), Image.LANCZOS)
        print(f"using {source}")
    else:
        # The exe icon is the active mark: it is what Windows shows in
        # Explorer and the taskbar, where "is it running" is not the
        # question being asked.
        from tray import ACTIVE, Status, bundled_icon_path, make_icon_image

        art = bundled_icon_path(ACTIVE)
        if art is not None:
            # Opened at native size rather than through `make_icon_image`,
            # which would resize to 256 first and leave every entry in the
            # .ico resampled twice. Icons are looked at at 16px; softness
            # shows.
            base = Image.open(art).convert("RGBA")
            print(f"using bundled {art.name}")
        else:
            base = make_icon_image(Status.RUNNING, size=256)

    # Pillow silently drops any requested size larger than the source, and
    # the artwork is 144px. That left the .ico with no 256x256 entry, so
    # Explorer's large-icon views fell back to scaling the 128 -- which is
    # exactly the "the icon didn't change" symptom. Upscale once, here.
    if min(base.size) < 256:
        base = base.resize((256, 256), Image.LANCZOS)

    base.save(out, sizes=SIZES)
    print(f"wrote {out}")
    _report_sizes(out)
    return 0


def _report_sizes(path) -> None:
    """List what actually landed in the file. Trust nothing about icons."""
    import struct

    data = path.read_bytes()
    _, _, count = struct.unpack_from("<HHH", data, 0)
    sizes = []
    for index in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + index * 16)
        sizes.append(f"{width or 256}x{height or 256}")
    print("  contains: " + ", ".join(sizes))


if __name__ == "__main__":
    raise SystemExit(main())
