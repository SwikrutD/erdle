# Third-party software

`ERDLE.exe` bundles the following. Each is redistributed under a licence
that permits it. This file ships inside the executable and alongside it in
the release; Tesseract's own licence text is vendored next to its binaries
when the installer provides one.

## Tesseract OCR

Reads the boss name from the framebuffer.

- Upstream: https://github.com/tesseract-ocr/tesseract
- Licence: Apache License 2.0
- Included: `tesseract.exe`, its runtime libraries, and `eng.traineddata`

Bundled rather than required because detection is name-driven: without a
reader, ERDLE does not lose a feature, it stops working. Only English
language data is included.

## Leptonica

Image-processing library Tesseract links against.

- Upstream: http://www.leptonica.org/
- Licence: BSD 2-Clause

## Python runtime and libraries

Frozen into the executable by PyInstaller:

| Package | Licence |
|---|---|
| Python | PSF |
| mss | MIT |
| Pillow | MIT-CMU |
| pystray | LGPL-3.0 |

pystray is LGPL-3.0 and is bundled rather than dynamically linked. It is
kept as ordinary Python bytecode inside the PyInstaller archive rather
than compiled in, so it can be extracted and replaced -- which is what the
licence asks for. Its source is at https://github.com/moses-palmer/pystray
| pytesseract | Apache-2.0 |

## Data sources

`data/bosses.json` holds numbers, not text or assets: damage negation,
status resistance and poise, transcribed and bucketed into four
categories.

- Community spreadsheet, "ER - PvE Health/Defense/DmgNeg/Resistances",
  which is where most of the values came from
- `regulation.bin` from a local Elden Ring install, read once to
  cross-check the above. Not redistributed, not modified, and not
  included in this repository or the release.

## Not included

Elden Ring itself, and no asset from it. ERDLE reads the screen the way a
capture card does; boss data is transcribed into `data/bosses.json`.
Elden Ring is a trademark of FromSoftware, Inc. and Bandai Namco
Entertainment Inc. This project is unaffiliated.
