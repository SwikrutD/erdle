# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ERDLE.

    pyinstaller erdle.spec

Produces dist/ERDLE.exe -- one file, no console, no Python needed on the
target machine.

Tesseract IS bundled, from vendor/. It used to be left out on the grounds
that the app degraded honestly without it -- true under the old
bar-driven detector, and false since detection became name-driven. With
no reader there is no name, and with no name there is no fight: the app
does nothing at all. A dependency that decides whether the program works
is not optional, so it ships inside.

Adds ~30 MB. Apache 2.0 and BSD-2-Clause; see THIRD_PARTY.md.
"""

import os
from pathlib import Path

block_cipher = None


def vendored_tesseract():
    """Everything under vendor/tesseract, flattened into the bundle.

    Collected by walking rather than by naming files: Tesseract's DLL
    dependency list changes between releases, and a missing one fails at
    run time with a dialog that names no cause.

    Absent is not fatal -- the build still works and falls back to a
    system install -- but `build.ps1` vendors first, so absence means
    someone bypassed it.
    """
    root = Path("vendor/tesseract")
    if not root.is_dir():
        print("WARNING: vendor/tesseract is missing; the exe will need a "
              "system Tesseract. Run tools/vendor_tesseract.py.")
        return []
    items = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            target = str(Path("tesseract") / path.relative_to(root).parent)
            items.append((str(path), target))
    return items


def seeded_atlas():
    """`data/glyphs.json`, if it has been seeded.

    Optional rather than required: the app runs without it, falling back
    to the bundled Tesseract. But a fresh install with no atlas reads
    every plate through OCR, so `build.ps1` seeds one from the developer's
    own learning before getting here. Shipping the file is what makes a
    new user's first fight resolve quickly.
    """
    path = Path("data/glyphs.json")
    if not path.is_file():
        print("WARNING: data/glyphs.json is missing; new users will start "
              "with a zero-character alphabet. Run tools/seed_atlas.py.")
        return []
    return [(str(path), "data")]


TESSERACT = vendored_tesseract()
ATLAS = seeded_atlas()

a = Analysis(
    ['tray.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('data/bosses.json', 'data'),
        # Licence notices travel inside the binary as well as beside it.
        # A user who downloads only ERDLE.exe still has the attribution
        # Apache-2.0 and LGPL require, and "Show licences" in the tray
        # menu has something to open.
        ('LICENSE', '.'),
        ('THIRD_PARTY.md', '.'),
        # Every tray mark. Without these the app silently falls back to a
        # drawn rune, which looks like a rendering bug rather than a
        # missing file. Listed one by one rather than globbed so a
        # missing file fails the build instead of shipping quietly.
        ('assets/tray-active.png', 'assets'),
        ('assets/tray-idle.png', 'assets'),
        ('assets/tray-calibrating.png', 'assets'),
        ('assets/tray-amber.png', 'assets'),
        ('assets/tray-error.png', 'assets'),
    ] + ATLAS + TESSERACT,
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',
        # The overlay imports tkinter lazily, inside a thread target, so
        # the analyser does not always follow it.
        'tkinter',
    ],
    hookspath=[],
    runtime_hooks=[],
    # Keep the binary small: none of these are used at runtime.
    # tkinter is NOT excluded -- it draws the on-screen overlay. Excluding
    # it costs about 8 MB and silently downgrades every user to the
    # keyboard panel, which is a strange failure to debug from a log line
    # that only says "no Tk on this machine".
    excludes=[
        'unittest', 'pytest', 'numpy', 'matplotlib',
        'pandas', 'scipy', 'setuptools', 'pip',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ERDLE',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window -- the whole point
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/erdle.ico' if __import__('os').path.exists('assets/erdle.ico') else None,
)
