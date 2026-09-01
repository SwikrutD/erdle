"""Puts the project root on sys.path so `erdle` imports without install."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Threads the app starts that must never outlive a test.
GUI_THREADS = ("erdle-overlay",)


@pytest.fixture(autouse=True)
def no_stray_gui_threads():
    """Fail loudly rather than crashing the interpreter.

    A real `Tk()` root created inside the suite lives on a daemon thread,
    and on Windows the interpreter dies tearing it down at exit -- a fatal
    C-level crash with a stack trace and no failing test name, which tells
    you nothing about which test did it.

    A test that needs the overlay should use a stand-in for the window.
    This catches the ones that forget.
    """
    yield
    alive = [
        thread.name
        for thread in threading.enumerate()
        if thread.name in GUI_THREADS and thread.is_alive()
    ]
    assert not alive, (
        f"test left a live GUI thread behind: {alive}. "
        "Use a fake window instead of building a real Tk root."
    )
