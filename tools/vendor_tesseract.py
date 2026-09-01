#!/usr/bin/env python3
"""Copy a local Tesseract install into vendor/ so it can be bundled.

    python tools/vendor_tesseract.py            # find it automatically
    python tools/vendor_tesseract.py "C:\\Program Files\\Tesseract-OCR"

Why bundle at all: detection is name-driven, so a machine with no OCR
does not get a reduced ERDLE, it gets one that never detects anything.
Asking every user to install a 30 MB dependency before the app works once
is the difference between a tool and a project.

Only English is copied. The UB-Mannheim installer ships a hundred
languages and most of the weight is data for scripts Elden Ring does not
use.

Licence: Tesseract is Apache 2.0 and Leptonica is BSD-2-Clause. Both
permit redistribution; `THIRD_PARTY.md` carries the notices.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erdle.ocr import TesseractRecogniser  # noqa: E402

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "tesseract"

#: The one language the boss names are in.
LANGUAGES = ("eng",)


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
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return source.name, target.stat().st_size


if __name__ == "__main__":
    raise SystemExit(main())
