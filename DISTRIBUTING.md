# Shipping ERDLE

ERDLE -- **E**lden **R**ing **D**amage **L**ookup **E**ngine.

## There is no SteelSeries app store

Worth stating plainly, because it shapes everything below. SteelSeries GG
has no plugin marketplace and no self-service publishing. The "Engine
Apps" list is first-party software (PrismSync, ImageSync, Discord) plus
official game partnerships negotiated with studios. The "+ Your app here"
link on their site goes to a business-development contact form.

GameSense is an open local HTTP API, not an app platform. Anything that
talks to it is an ordinary Windows program that happens to POST to
`127.0.0.1`. That is what every community OLED tool does — GGSystemMonitor,
gamesense-essentials, OmniLED — and it is what ERDLE does.

So the distribution story is: **a signed-or-not `.exe` on GitHub Releases.**
Users download it, double-click it, and it lives in the system tray. They
never see a terminal, and they don't need Python.

## Building the exe

From the project root, in PowerShell:

```powershell
.\build.ps1
```

That installs build dependencies, runs the test suite, refuses to build if
anything fails, generates the icon, and produces `dist\ERDLE.exe`.

Roughly 25–35 MB, one file, no installer required.

## What the user experiences

1. Download `ERDLE.exe`
2. Double-click
3. A gold rune appears in the system tray

Right-click the tray icon for: current status, **Recalibrate**, **Start with
Windows**, **Show log**, **Quit**.

No console window ever appears. `tray.py` is the shipped entrypoint;
`run.py` still exists for development, where watching the event stream is
the point.

## Why it works on other people's monitors

This is the part that would otherwise break immediately. The regions in
`geometry.py` were measured on one 3840×2160 display. Fractional
coordinates survive a resolution change at the same aspect ratio, but a
21:9 ultrawide or a 16:10 laptop puts the boss bar somewhere else, and a
new user's first experience would be a panel stuck on ERDLE forever.

So the app calibrates itself. When it has no saved calibration it
periodically sweeps a full screenshot for a boss bar — the same search
`erdle.calibrate` uses — and the first time it finds one it saves those
regions to `%APPDATA%\erdle\config.json` and stops sweeping.

The tray icon shows blue while it is still looking, gold once it is
running. Tested against 1080p, 1440p, 4K, 1920×1200 and 3440×1440
ultrawide.

**Recalibrate** in the tray menu clears it, for people who change monitor.

## The Tesseract problem, and how it goes away

Boss *names* originally needed Tesseract — ~30 MB of third-party binary
with its own licence, and unreliable on this input besides.

`erdle/glyphs.py` replaces it. The insight is that we never needed to
pre-render 165 *names*: across every boss in the game there are only
about forty distinct *characters*. Learn those and any name becomes
readable, including bosses never encountered.

Nor do we need FromSoftware's font file. Glyphs are learned from the
game's own output: whenever a name resolves to a real boss with high
confidence, that plate's characters are already labelled, so each one is
filed under the letter it must be. After a handful of fights the atlas
covers the alphabet and Tesseract stops being consulted.

    plate -> column projection -> glyph boxes -> normalise each onto an
    8x12 grid of quantised coverage -> nearest neighbour -> string

Two measured design decisions:

* **Quantised coverage, not bits.** Thresholding each cell to 0/1 throws
  away exactly the information that survives a scale change. A stroke
  covering 40% of a cell at one resolution and 60% at another flips a bit
  but barely moves a coverage level.
* **Size-aware matching.** At a fixed scale the same letter matches at
  distance 0 while the closest different pair (M/N) sits at 14 —
  separation is total. Across a 4x scale change the ranges overlap, so
  samples are only compared when their heights are within 1.5x. A large
  scale change makes the atlas decline and return `?` rather than guess,
  which costs one fallback read and teaches it the new size.

### Why one atlas covers every display

The obvious worry with template matching is resolution: a 4K player's
glyphs are twice the size of a 1080p player's.

The tempting fix -- make the matcher scale-invariant -- does not work, and
the reason is worth stating because it is not obvious. Normalising a glyph
to its own bounding box necessarily throws away how big it was, and in
every real font `C` and `c`, `O` and `o`, `S` and `s`, `W` and `w` are the
*same shape at different sizes*. Perfect scale invariance would make those
pairs indistinguishable. Measured on a synthetic alphabet, no grid size,
supersampling rate or quantisation level separated same-letter-across-
scales from different-letter-same-scale.

So the matcher deliberately keeps size, comparing a glyph only against
samples within 1.5x of its height -- and coverage is solved by supplying
samples at every size instead:

```
python tools/learn.py --dir screenshots/
```

Each screenshot is also learned at 1440p, 1200p, 1080p, 900p and 720p by
downscaling. That is a close approximation of what the game actually
renders at those resolutions: same geometry, same anti-aliasing, fewer
pixels. Two 4K captures produced an atlas reporting

    serves: 720p, 900p, 1080p, 1200p, 1440p, 4K

`python tools/atlas.py show` prints that line, so you can confirm coverage
before shipping. `--no-ladder` disables it if you want single-size samples.

Anyone on an unusual display still self-heals: unmatched glyphs fall back
to Tesseract if present, and are learned at their own size.

### Three ways to ship

Tesseract is now a **tutor, not a dependency**:

1. **Bake in a complete atlas.** Play until `python tools/atlas.py show`
   reports full coverage, then `python tools/atlas.py ship`. Users need
   nothing installed. This is the intended path.
2. **Ask users to install Tesseract** as a one-off tutor, if you would
   rather not ship an atlas.
3. **Collect atlases from users** and `tools/atlas.py merge` them, which
   broadens resolution coverage.

## Publishing

### What goes in the release

`build.ps1` stages these in `dist\release\`:

| File | Why |
|---|---|
| `ERDLE.exe` | the whole application -- boss data, glyph atlas, Tesseract and tray icons are all inside it |
| `LICENSE` | MIT, this project |
| `THIRD_PARTY.md` | Apache-2.0 (Tesseract) and LGPL-3.0 (pystray) both require the notice to travel with the binary |
| `SHA256.txt` | the checksum, because the exe is unsigned and SmartScreen will say so |

`LICENSE` and `THIRD_PARTY.md` are *also* bundled inside the exe, so a
user who downloads only the binary still receives the attribution. They
ship loose as well so nobody has to unpack a binary to read a licence.

Nothing else. No `data/` folder, no Python, no Tesseract install --
one file plus three text files.

### What goes in the repository

Everything except the two things that must not be there:

* `vendor/` -- ~30 MB of Tesseract, recreated by `build.ps1`
* `regulation.bin` -- FromSoftware's file. Reading a local copy to
  cross-check `bosses.json` is fine; redistributing one is not.

Both are gitignored, and a test enforces the second.

```
GitHub repo
├── Releases → ERDLE.exe + LICENSE + THIRD_PARTY.md + SHA256.txt
├── README with a screenshot of the OLED
└── LICENSE (MIT)
```

Then post it. Based on what actually spreads in this community: r/Eldenring
for reach, r/SteelSeries for the people who own the hardware, and the
SteelSeries Discord. A photograph of the keyboard mid-fight will do more
than any amount of description.

## Two things to sort before a public release

**Code signing.** Unsigned executables get SmartScreen's "Windows protected
your PC" wall, and a good fraction of users stop there. A certificate is
~$100–200/year. Without one, put a plain explanation and the SHA256 in the
README, and expect the friction.

**Antivirus false positives.** PyInstaller one-file builds are heuristically
flagged fairly often, because self-extracting archives resemble packers.
Submitting the binary to the major vendors as a false positive helps.
A one-folder build (`--onedir`) trips fewer detectors, at the cost of
shipping a folder rather than a single file.

## Licensing note

`data/bosses.json` currently holds community-sourced approximations. If you
replace it with values extracted from the game's `NpcParam`, that is
FromSoftware's data — most community tools ship it anyway, but it is worth
a deliberate decision rather than an accident.
