# Shipping ERDLE

ERDLE stands for **E**lden **R**ing **D**amage **L**ookup **E**ngine.

## There is no SteelSeries app store

This is worth explaining up front because it affects how ERDLE has to be distributed.

SteelSeries GG does not have a plugin marketplace or a self-service publishing system. The "Engine Apps" section is mostly first-party software such as PrismSync, ImageSync, and Discord integrations, along with official game partnerships arranged directly with studios. The "+ Your app here" link on their site leads to a business development contact form rather than an app submission portal.

GameSense itself is simply a local HTTP API. It is not an app platform.

Programs that use it are normal Windows applications that send requests to `127.0.0.1`. Community tools such as GGSystemMonitor, gamesense-essentials, and OmniLED work this way, and ERDLE does the same thing.

So ERDLE is distributed as a Windows `.exe` through GitHub Releases.

Users download it, double-click it, and ERDLE runs from the system tray. There is no terminal window and Python does not need to be installed.

## Building the exe

From the project root, open PowerShell and run:

```powershell
.\build.ps1
```

The build script installs the required build dependencies, runs the test suite, stops if any tests fail, generates the application icon, and builds:

```text
dist\ERDLE.exe
```

The result is a single executable with no installer required.

## What the user experiences

1. Download `ERDLE.exe`
2. Double-click it
3. A gold rune appears in the Windows system tray

Right-clicking the tray icon gives access to:

* Current status
* **Recalibrate**
* **Start with Windows**
* **Show log**
* **Quit**

No console window appears during normal use.

`tray.py` is the entry point used by the packaged application. `run.py` is still available for development, where seeing the event stream in the terminal is useful.

## Why it works on different monitors

This was one of the main problems that had to be solved before ERDLE could realistically be distributed.

The original regions in `geometry.py` were measured on a 3840×2160 display. Fractional coordinates handle changes in resolution fairly well when the aspect ratio stays the same, but they are not enough for every display.

A 21:9 ultrawide and a 16:10 laptop can place HUD elements differently. Without calibration, a new user could launch ERDLE and have it sit there waiting forever because it was looking in the wrong part of the screen.

ERDLE therefore calibrates itself.

If there is no saved calibration, the app periodically scans a full screenshot for the boss bar using the same search logic as `erdle.calibrate`.

Once it finds the bar, it saves the detected regions to:

```text
%APPDATA%\erdle\config.json
```

After that, the full-screen search stops.

The tray icon is blue while ERDLE is still searching for the HUD and turns gold once calibration is complete.

The calibration system has been tested at:

* 1920×1080
* 2560×1440
* 3840×2160
* 1920×1200
* 3440×1440 ultrawide

The **Recalibrate** option in the tray menu clears the saved calibration for users who change monitors or display settings.

## The Tesseract problem, and how it mostly disappears

Boss names originally relied on Tesseract.

That worked, but it meant bundling a fairly large third-party OCR package, dealing with another license, and depending on OCR that was not always reliable on Elden Ring's boss-name text.

`erdle/glyphs.py` takes a different approach.

The important realization was that ERDLE does not need templates for every boss name. Across all boss names, there are only around forty distinct characters.

If ERDLE learns those characters, it can reconstruct names it has never seen before.

It also does not need FromSoftware's actual font file.

The glyphs can be learned directly from the game's rendered text. Whenever a boss name is identified with high confidence, ERDLE already knows what the text says. That means the individual characters in the name can be treated as labelled training samples.

Over time, those samples build an atlas.

Once enough characters are represented in the atlas, Tesseract is needed less and less.

The pipeline is roughly:

```text
plate
  -> column projection
  -> glyph boxes
  -> normalise each glyph onto an 8x12 grid
  -> quantise coverage
  -> nearest-neighbour match
  -> reconstructed string
```

Two decisions turned out to matter quite a bit.

### Quantised coverage instead of binary pixels

Turning each grid cell into a simple 0 or 1 throws away useful information.

For example, a stroke might cover 40% of one cell at one resolution and 60% at another. A binary threshold could turn those into completely different values even though the glyph is visually almost identical.

Coverage levels preserve that information much better.

### Size-aware matching

At the same scale, matching works extremely well.

A glyph can match another sample of the same letter with distance 0 while the nearest different letter, such as M versus N, is much farther away.

The problem appears when the scale changes significantly.

Across a large size difference, those distance ranges begin to overlap. For that reason, ERDLE only compares glyphs against samples whose heights are within roughly 1.5 times the current glyph height.

If the scale is too different, the atlas returns `?` instead of guessing.

That failed match can then fall back to another reader and become a new training sample at the correct size.

## Why one atlas can cover several display resolutions

Template matching normally raises an obvious problem: resolution.

A glyph rendered at 4K can be roughly twice the size of the same glyph rendered at 1080p.

The tempting solution is to make the matcher completely scale invariant.

That does not work very well.

If a glyph is normalized entirely to its own bounding box, its original size information disappears. In a real font, characters such as `C` and `c`, `O` and `o`, `S` and `s`, or `W` and `w` can be nearly the same shape at different sizes.

Perfect scale invariance can therefore make uppercase and lowercase characters difficult or impossible to distinguish.

Tests with a synthetic alphabet showed that changing grid size, supersampling, or quantisation did not fully solve that problem.

So ERDLE deliberately keeps size as part of the matching process.

Instead of forcing the matcher to handle every scale, the atlas contains samples at several common display resolutions.

Run:

```text
python tools/learn.py --dir screenshots/
```

Each screenshot is also learned after being downscaled to several common vertical resolutions:

* 1440p
* 1200p
* 1080p
* 900p
* 720p

This works reasonably well because the downscaled screenshots closely approximate how the game renders the same HUD geometry at those resolutions.

Two 4K captures, for example, produced an atlas reporting:

```text
serves: 720p, 900p, 1080p, 1200p, 1440p, 4K
```

You can check the current coverage with:

```text
python tools/atlas.py show
```

Use `--no-ladder` if you want to learn only the screenshot's original resolution.

Users with unusual display sizes can still recover automatically. If the atlas cannot recognize a glyph, the fallback reader can identify it and teach the atlas a sample at that size.

## Three ways to ship

Tesseract is now better thought of as a tutor than as the core recognition system.

There are three practical distribution options.

### 1. Ship a complete atlas

Play until:

```text
python tools/atlas.py show
```

reports enough coverage, then run:

```text
python tools/atlas.py ship
```

The atlas is bundled into the release and users do not need to install anything separately.

This is the intended release path.

### 2. Let users install Tesseract

Users can install Tesseract once and let it act as the tutor while ERDLE builds its own atlas.

This avoids shipping a large prebuilt atlas, although it adds setup work for users.

### 3. Merge user-generated atlases

Atlases produced on different systems can be combined with:

```text
tools/atlas.py merge
```

This is useful for increasing coverage across different resolutions and display configurations.

## Publishing

### What goes in the release

`build.ps1` stages the release files inside:

```text
dist\release\
```

| File             | Purpose                                                                                         |
| ---------------- | ----------------------------------------------------------------------------------------------- |
| `ERDLE.exe`      | The complete application. Boss data, glyph atlas, Tesseract, and tray icons are bundled inside. |
| `LICENSE`        | MIT license for ERDLE                                                                           |
| `THIRD_PARTY.md` | Third-party license notices, including Tesseract and pystray                                    |
| `SHA256.txt`     | SHA256 checksum for verifying the executable                                                    |

`LICENSE` and `THIRD_PARTY.md` are also bundled inside the executable.

That way, someone who downloads only `ERDLE.exe` still receives the required attribution. Loose copies are included as well so nobody has to extract an executable just to read the licenses.

There is no separate `data/` folder, Python installation, or Tesseract installer.

The release is one executable plus three small text files.

## What goes in the repository

Almost everything can live in the repository except for two items.

### `vendor/`

This contains the local Tesseract runtime used during packaging. It is around 70 MB after stripping unnecessary debug data and can be recreated by `build.ps1`, so it does not need to be committed.

### `regulation.bin`

This belongs to FromSoftware.

ERDLE can read a local copy to cross-check information in `bosses.json`, but redistributing the file itself is a different matter.

It is gitignored, and there is a test to make sure it does not accidentally end up in the repository.

The public repository looks roughly like this:

```text
GitHub repo
├── Releases
│   ├── ERDLE.exe
│   ├── LICENSE
│   ├── THIRD_PARTY.md
│   └── SHA256.txt
├── README
└── LICENSE
```

From there, the project can be shared wherever Elden Ring and SteelSeries users are likely to find it.

Likely places include r/Eldenring, r/SteelSeries, and the SteelSeries Discord.

For a project like this, a photo or short clip of ERDLE running during an actual boss fight will probably explain the idea much faster than a long description.

## Two things to deal with before a public release

### Code signing

Unsigned Windows executables can trigger the SmartScreen "Windows protected your PC" warning.

That creates real friction because some users will stop as soon as they see it.

A code-signing certificate typically costs money every year. If ERDLE stays unsigned, the README should clearly explain why the warning appears and provide the SHA256 checksum so users can verify the file they downloaded.

### Antivirus false positives

PyInstaller one-file executables sometimes trigger heuristic antivirus detections.

That happens partly because self-extracting executables resemble the techniques used by packers and some malware.

If a release is incorrectly detected, submitting the binary to antivirus vendors as a false positive can help.

Another option is using a PyInstaller `--onedir` build. Those builds are sometimes less suspicious to heuristic scanners, although the tradeoff is that users download a folder of files instead of one executable.

## Licensing note

`data/bosses.json` currently contains community-sourced boss information.

If those values are replaced with data extracted directly from the game's `NpcParam`, that changes the licensing situation because the values would come directly from FromSoftware's game data.

Many community tools distribute extracted game information, but that should be a deliberate project decision rather than something that happens accidentally.
