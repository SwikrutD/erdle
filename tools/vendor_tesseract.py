#!/usr/bin/env python3
"""Copy a local Tesseract install into vendor/ so it can be bundled.

    python tools/vendor_tesseract.py            # find it automatically
    python tools/vendor_tesseract.py "C:\\Program Files\\Tesseract-OCR"

Why bundle at all: detection is name-driven, so a machine with no OCR
does not get a reduced ERDLE, it gets one that never detects anything.
Asking every user to install an OCR engine before the app works once
is the difference between a tool and a project.

Only English is copied. The UB-Mannheim installer ships a hundred
languages and most of the weight is data for scripts Elden Ring does not
use.

Licence: Tesseract is Apache 2.0 and Leptonica is BSD-2-Clause. Both
permit redistribution; `THIRD_PARTY.md` carries the notices.
"""

from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erdle.ocr import TesseractRecogniser  # noqa: E402

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "tesseract"

#: The one language the boss names are in.
LANGUAGES = ("eng",)



def strip_debug(data: bytes) -> bytes:
    """Remove DWARF debug sections from a PE binary.

    The UB-Mannheim Tesseract build ships unstripped. `libtesseract-5.dll`
    is 96.8 MB of which `.text` -- the actual code -- is 2.3 MB; the other
    93 MB is debug symbols nobody downloading a tray utility will ever
    load. Bundled whole, they made `ERDLE.exe` 127 MB; stripped, it is
    about 75 MB.

    Done here in Python rather than by shelling out to `strip`, because
    binutils is not on a normal Windows box and this is thirty lines.
    Verified against `objcopy --strip-debug`: same sections kept, byte
    for byte, and slightly smaller because objcopy pads.
    """
    if data[:2] != b"MZ":
        return data
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return data

    section_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    table = pe + 24 + optional_size

    keep, end = [], 0
    for index in range(section_count):
        entry = data[table + index * 40: table + (index + 1) * 40]
        name = entry[:8].rstrip(b"\0").decode("latin-1")
        raw_size, raw_pointer = struct.unpack_from("<II", entry, 16)
        # GNU stores DWARF under names too long for the 8-byte field, so
        # they show up as "/19", "/97" -- an offset into the string
        # table. Match those as well as the literal `.debug` names.
        if name.startswith("/") or name.startswith(".debug"):
            continue
        keep.append(entry)
        if raw_pointer:
            end = max(end, raw_pointer + raw_size)

    if len(keep) == section_count:
        return data

    out = bytearray(data)
    struct.pack_into("<H", out, pe + 6, len(keep))
    # The COFF symbol table lives past the sections and is debug data too.
    struct.pack_into("<II", out, pe + 12, 0, 0)
    characteristics = struct.unpack_from("<H", out, pe + 22)[0]
    struct.pack_into("<H", out, pe + 22, characteristics | 0x0200)

    for index, entry in enumerate(keep):
        out[table + index * 40: table + (index + 1) * 40] = entry
    for index in range(len(keep), section_count):
        out[table + index * 40: table + (index + 1) * 40] = b"\0" * 40

    return bytes(out[:end]) if end else bytes(out)


def find_install(explicit: str | None) -> Path | None:
    if explicit:
        root = Path(explicit)
        return root if (root / "tesseract.exe").exists() or root.is_dir() else None

    # Deliberately ignores the bundled copy, or re-running this would
    # vendor vendor/ into itself.
    for candidate in (
        TesseractRecogniser._WINDOWS_CANDIDATES
    ):
        import os

        expanded = Path(os.path.expandvars(candidate))
        if expanded.exists():
            return expanded.parent

    found = shutil.which("tesseract")
    return Path(found).parent if found else None


def main() -> int:
    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    root = find_install(explicit)
    if root is None:
        print(
            "Could not find a Tesseract install.\n"
            "  winget install UB-Mannheim.TesseractOCR\n"
            "then run this again, or pass the directory as an argument.",
            file=sys.stderr,
        )
        return 1

    binary = root / "tesseract.exe"
    if not binary.exists():
        binary = root / "tesseract"          # non-Windows, for testing
    if not binary.exists():
        print(f"no tesseract executable in {root}", file=sys.stderr)
        return 1

    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    (VENDOR / "tessdata").mkdir(parents=True)

    copied = [copy(binary, VENDOR / binary.name)]

    # Every DLL beside the executable. Picking them individually means
    # guessing at a dependency tree that changes between releases, and a
    # missing one fails at run time with an unhelpful dialog.
    for library in sorted(root.glob("*.dll")):
        copied.append(copy(library, VENDOR / library.name))

    tessdata = find_tessdata(root)
    if tessdata is None:
        print(f"no tessdata directory near {root}", file=sys.stderr)
        return 1

    for language in LANGUAGES:
        source = tessdata / f"{language}.traineddata"
        if not source.exists():
            print(f"missing {source}", file=sys.stderr)
            return 1
        copied.append(copy(source, VENDOR / "tessdata" / source.name))

    # Apache-2.0 section 4 requires the licence to travel with the
    # binary. THIRD_PARTY.md claimed the text shipped in vendor/ and it
    # did not -- only the DLLs were copied -- so the release was one file
    # short of the terms it redistributes Tesseract under.
    for name in ("LICENSE", "LICENSE.txt", "COPYING", "NOTICE"):
        source = root / name
        if source.exists():
            copied.append(copy(source, VENDOR / f"tesseract-{name}"))
            break
    else:
        # Not fatal: the UB-Mannheim installer does not always place one,
        # and `THIRD_PARTY.md` carries the attribution either way. But it
        # should be a decision, not a silence.
        print("warning: no LICENSE beside tesseract.exe; the release must "
              "carry THIRD_PARTY.md alongside the binary",
              file=sys.stderr)

    total = sum(size for _, size in copied)
    print(f"vendored {len(copied)} files from {root}")
    for name, size in copied:
        if size > 1_000_000:
            print(f"  {name:<32} {size / 1_048_576:6.1f} MB")
    print(f"  total {total / 1_048_576:.1f} MB -> {VENDOR}")
    return 0


def find_tessdata(root: Path) -> Path | None:
    for candidate in (root / "tessdata", root.parent / "tessdata",
                      root.parent / "share" / "tessdata",
                      Path("/usr/share/tesseract-ocr/4.00/tessdata"),
                      Path("/usr/share/tessdata")):
        if candidate.is_dir():
            return candidate
    return None


def copy(source: Path, target: Path) -> tuple[str, int]:
    """Copy one file in, stripping debug symbols from binaries."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in (".dll", ".exe"):
        data = source.read_bytes()
        stripped = strip_debug(data)
        target.write_bytes(stripped)
    else:
        shutil.copy2(source, target)
    return source.name, target.stat().st_size


if __name__ == "__main__":
    raise SystemExit(main())
