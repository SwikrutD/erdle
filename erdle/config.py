"""User settings, including auto-discovered screen regions.

Shipping this to other people creates a problem the developer never has:
the HUD regions were measured on one machine. Fractional coordinates carry
across resolutions of the same aspect ratio, but ultrawide, 16:10 and
21:9 all put the boss bar somewhere else.

So the app calibrates itself. The first time it sees a bar it does not
recognise, it searches for one, saves what it finds, and uses that from
then on. Nobody has to run a terminal command.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .geometry import BOSS_BAR, BOSS_NAME, HUD_STRIP, FractionalRect

SCHEMA_VERSION = 1


def config_dir() -> Path:
    """Per-user settings location, not next to the executable.

    A frozen build may live in Program Files, which is not writable.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "erdle"
    return Path.home() / ".config" / "erdle"


def config_path() -> Path:
    return config_dir() / "config.json"


def _rect_to_dict(rect: FractionalRect) -> dict[str, float]:
    return {
        "left": rect.left, "top": rect.top,
        "right": rect.right, "bottom": rect.bottom,
    }


def _rect_from_dict(raw: Any) -> FractionalRect | None:
    if not isinstance(raw, dict):
        return None
    try:
        return FractionalRect(
            float(raw["left"]), float(raw["top"]),
            float(raw["right"]), float(raw["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _optional_fraction(raw: Any) -> float | None:
    """A 0.0-1.0 position, or None when absent or nonsense."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return max(0.0, min(1.0, value))


def _clamp(raw: Any, low: float, high: float, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if value != value:  # NaN compares unequal to itself and survives min/max
        return fallback
    return max(low, min(high, value))


@dataclass
class Config:
    boss_bar: FractionalRect = BOSS_BAR
    boss_name: FractionalRect = BOSS_NAME
    hud_strip: FractionalRect = HUD_STRIP
    calibrated: bool = False
    calibrated_for: str = ""          # "3840x2160", for reporting
    monitor: int = 1
    fps: float = 15.0
    autostart: bool = False

    # --- overlay ----------------------------------------------------------
    overlay_enabled: bool = True
    #: Position as fractions, not pixels, so it survives a resolution
    #: change the way the HUD regions do. `fx` is a fraction of the free
    #: horizontal space (screen width minus panel width), so 1.0 is flush
    #: right on any display and the panel can never hang off the edge.
    #: `fy` is a plain fraction of screen height. None means "the default
    #: corner", resolved against the live screen at display time.
    overlay_fx: float | None = None
    overlay_fy: float | None = None
    #: Pixel position written by builds before the switch to fractions.
    #: Kept only so upgrading does not silently move someone's window; it
    #: is converted on first use and then never written again.
    overlay_x: int | None = None
    overlay_y: int | None = None
    overlay_scale: float = 1.0
    overlay_opacity: float = 0.88
    #: "compact" (only the rows that change a decision) or "full".
    overlay_detail: str = "compact"

    path: Path | None = None

    # --- calibration ------------------------------------------------------

    def apply_regions(
        self,
        bar: FractionalRect,
        name: FractionalRect,
        strip: FractionalRect,
        *,
        resolution: str = "",
    ) -> None:
        self.boss_bar = bar
        self.boss_name = name
        self.hud_strip = strip
        self.calibrated = True
        self.calibrated_for = resolution

    def reset_regions(self) -> None:
        """Back to the shipped defaults, and mark as needing calibration."""
        self.boss_bar = BOSS_BAR
        self.boss_name = BOSS_NAME
        self.hud_strip = HUD_STRIP
        self.calibrated = False
        self.calibrated_for = ""

    # --- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "regions": {
                "boss_bar": _rect_to_dict(self.boss_bar),
                "boss_name": _rect_to_dict(self.boss_name),
                "hud_strip": _rect_to_dict(self.hud_strip),
            },
            "calibrated": self.calibrated,
            "calibrated_for": self.calibrated_for,
            "monitor": self.monitor,
            "fps": self.fps,
            "autostart": self.autostart,
            "overlay": {
                "enabled": self.overlay_enabled,
                "fx": self.overlay_fx,
                "fy": self.overlay_fy,
                "x": self.overlay_x,
                "y": self.overlay_y,
                "scale": self.overlay_scale,
                "opacity": self.overlay_opacity,
                "detail": self.overlay_detail,
            },
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Config":
        config = cls()
        if not isinstance(payload, dict):
            return config

        regions = payload.get("regions", {})
        if isinstance(regions, dict):
            bar = _rect_from_dict(regions.get("boss_bar"))
            name = _rect_from_dict(regions.get("boss_name"))
            strip = _rect_from_dict(regions.get("hud_strip"))
            # All three or none. A partial set would mix a calibrated bar
            # with a default name plate, which is worse than either.
            if bar and name and strip:
                config.boss_bar, config.boss_name, config.hud_strip = bar, name, strip

        config.calibrated = bool(payload.get("calibrated", False))
        config.calibrated_for = str(payload.get("calibrated_for", ""))
        try:
            config.monitor = int(payload.get("monitor", 1))
            config.fps = float(payload.get("fps", 15.0))
        except (TypeError, ValueError):
            pass
        config.autostart = bool(payload.get("autostart", False))
        config._load_overlay(payload.get("overlay"))
        return config

    def _load_overlay(self, raw: Any) -> None:
        """Read the overlay block, clamping anything out of range.

        A hand-edited scale of 40 or an opacity of 0 produces a window the
        user cannot see and cannot drag back, with no console to explain
        why. Clamping is friendlier than validating and refusing to start.
        """
        if not isinstance(raw, dict):
            return
        self.overlay_enabled = bool(raw.get("enabled", True))
        self.overlay_fx = _optional_fraction(raw.get("fx"))
        self.overlay_fy = _optional_fraction(raw.get("fy"))
        self.overlay_x = _optional_int(raw.get("x"))
        self.overlay_y = _optional_int(raw.get("y"))
        self.overlay_scale = _clamp(raw.get("scale"), 0.6, 3.0, 1.0)
        self.overlay_opacity = _clamp(raw.get("opacity"), 0.25, 1.0, 0.88)
        detail = raw.get("detail")
        self.overlay_detail = detail if detail in ("compact", "full") else "compact"

    def move_overlay(self, fx: float, fy: float) -> None:
        """Remember where the user dragged the window, as fractions.

        Clears the legacy pixel fields: once the user has placed the
        window on this display, the old absolute position is not just
        redundant but actively misleading.
        """
        self.overlay_fx = max(0.0, min(1.0, float(fx)))
        self.overlay_fy = max(0.0, min(1.0, float(fy)))
        self.overlay_x = None
        self.overlay_y = None

    def reset_overlay_position(self) -> None:
        self.overlay_fx = self.overlay_fy = None
        self.overlay_x = self.overlay_y = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Load, tolerating a missing or damaged file.

        Bad settings must never stop the app starting -- it has no console
        to report the problem to.
        """
        target = Path(path) if path is not None else config_path()
        if not target.exists():
            config = cls()
            config.path = target
            return config
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            config = cls()
            config.path = target
            return config
        config = cls.from_dict(payload)
        config.path = target
        return config

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(target.parent),
            prefix=target.name, suffix=".tmp", delete=False,
        )
        try:
            with handle as stream:
                json.dump(self.to_dict(), stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, target)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        self.path = target
        return target
