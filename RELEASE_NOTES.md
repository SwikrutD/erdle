# ERDLE v1.0.0

**E**lden **R**ing **D**amage **L**ookup **E**ngine: an on-screen boss
cheat sheet. It notices when a fight starts, reads the boss's name off
the screen, and shows what actually works: what to bring, what not to
bother with, and the poise threshold.

Works on any Windows PC. Download `ERDLE.exe`, double-click, play. No
Python, no install, no configuration. It lives in the system tray.

If you own a SteelSeries keyboard with an OLED (Apex Pro, Apex Pro TKL,
Apex 7, Apex 7 TKL), it drives that too - the same cheat sheet, below
your hands instead of over the game. That needs SteelSeries GG running,
and it's the only hardware supported. Everyone else gets the overlay.

## Will this get me banned?

No. ERDLE never touches the game.

It reads your screen the way OBS does, and reads a local JSON file. It
opens no handle to `eldenring.exe`, injects no DLL, hooks nothing, and
attaches no debugger - that is the complete list of things Easy
Anti-Cheat looks for, and ERDLE does none of them. Single-player and
multiplayer are equally unaffected because from the game's side nothing
is happening at all.

## What's in the box

- **122 bosses** with damage negation, status resistances and poise,
  cross-checked between a community dataset and the game's own
  `NpcParam` - 218 of 228 damage values agreed, and the disagreements
  were resolved in favour of the game.
- **Two displays.** A screen overlay you can drag anywhere, with compact
  and full detail modes, and the 128×40 keyboard panel.
- **Automatic calibration.** It finds the boss health bar on your
  display the first time it sees one; no measuring, no config file.
- **Boss-name recognition** that teaches itself as you play, with
  Tesseract bundled so it works from the first fight.

## First run

Windows will show a SmartScreen warning: the exe is unsigned, because a
certificate costs more per year than this project cost to make.

**More info → Run anyway.**

Verify the download if you'd rather not take that on faith:

```powershell
(Get-FileHash ERDLE.exe -Algorithm SHA256).Hash
```

and compare it against `SHA256.txt`.

Some antivirus tools flag PyInstaller one-file builds on sight, because
self-extracting archives resemble packers. If yours does, the checksum
above is the thing to check.

## Known limitations

- **Ultrawide and 16:10 displays are untested on real hardware.** The
  calibration is written for them and covered by tests, but nobody has
  run it on a 21:9 monitor yet. If the panel sits on "ERDLE" forever,
  that's this - please open an issue with your resolution.
- The exe is unsigned. See above.
- It is ~75 MB, most of that the bundled Tesseract OCR engine and its
  ICU locale data. Down from 127 MB: the upstream Windows build ships
  with 93 MB of debug symbols, which are now stripped at build time.
- English boss names only.

## Files

| File | |
|---|---|
| `ERDLE.exe` | the whole application, ~75 MB |
| `LICENSE` | MIT |
| `THIRD_PARTY.md` | Tesseract (Apache-2.0), pystray (LGPL-3.0), and the rest |
| `SHA256.txt` | checksum for the exe |

Elden Ring is a trademark of FromSoftware, Inc. and Bandai Namco
Entertainment Inc. This project is unaffiliated with either.
