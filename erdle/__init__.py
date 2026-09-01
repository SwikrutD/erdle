"""erdle -- Elden Ring boss cheat sheet for SteelSeries OLED devices.

Reads pixels and files. Never reads game memory, never injects, never
opens a handle to eldenring.exe.
"""

__version__ = "0.1.0"

from .bossdb import BossDatabase, BossEntry
from .canvas import Canvas
from .detect import Frame, analyse_bar
from .matching import BossNameMatcher
from .state import EventKind, FightState, FightTracker

__all__ = [
    "BossDatabase",
    "BossEntry",
    "Canvas",
    "Frame",
    "analyse_bar",
    "BossNameMatcher",
    "EventKind",
    "FightState",
    "FightTracker",
    "__version__",
]
