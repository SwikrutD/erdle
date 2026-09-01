"""Start with Windows, via a shortcut in the user's Startup folder.

The Startup folder is chosen over a Run registry key deliberately: users
can see it, and delete it, without regedit. Antivirus heuristics are also
far less interested in it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SHORTCUT_NAME = "ERDLE.lnk"
BATCH_NAME = "ERDLE.cmd"


def startup_dir() -> Path | None:
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (
        Path(appdata)
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def launch_target() -> tuple[str, str]:
    """What to run, and any arguments.

    Frozen by PyInstaller, `sys.executable` is the app itself. Running
    from source it is python.exe, and we want pythonw.exe so no console
    window flashes up at login.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ""

    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.exists():
        executable = windowless
    script = Path(__file__).resolve().parent.parent / "tray.py"
    return str(executable), f'"{script}"'


def is_enabled() -> bool:
    directory = startup_dir()
    if directory is None:
        return False
    return (directory / SHORTCUT_NAME).exists() or (directory / BATCH_NAME).exists()


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Create or remove the startup entry. Returns (ok, message)."""
    directory = startup_dir()
    if directory is None:
        return False, "autostart is only supported on Windows"

    shortcut = directory / SHORTCUT_NAME
    batch = directory / BATCH_NAME

    if not enabled:
        removed = False
        for path in (shortcut, batch):
            try:
                if path.exists():
                    path.unlink()
                    removed = True
            except OSError as exc:
                return False, f"could not remove {path.name}: {exc}"
        return True, "autostart disabled" if removed else "autostart was not set"

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"could not open Startup folder: {exc}"

    target, arguments = launch_target()

    # Prefer a real .lnk, but that needs pywin32. Fall back to a .cmd,
    # which works everywhere and is easier for a user to inspect.
    try:
        import pythoncom  # type: ignore
        from win32com.client import Dispatch  # type: ignore

        shell = Dispatch("WScript.Shell")
        link = shell.CreateShortCut(str(shortcut))
        link.Targetpath = target
        link.Arguments = arguments
        link.WorkingDirectory = str(Path(target).parent)
        link.Description = "ERDLE - Elden Ring boss cheat sheet"
        link.save()
        return True, f"autostart enabled ({shortcut.name})"
    except Exception:
        pass

    try:
        command = f'@echo off\r\nstart "" "{target}" {arguments}\r\n'
        batch.write_text(command, encoding="utf-8")
        return True, f"autostart enabled ({batch.name})"
    except OSError as exc:
        return False, f"could not write startup entry: {exc}"
