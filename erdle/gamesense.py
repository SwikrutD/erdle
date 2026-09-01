"""SteelSeries GameSense client.

Talks to the local GG HTTP server only. No game process is touched, no
DLL is injected, nothing is hooked -- which is the whole point of this
project's architecture.

Transport is injected so the payload shapes can be asserted in tests
without a SteelSeries device or a running GG installation.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

GAME_NAME = "ERDLE"
GAME_DISPLAY_NAME = "Elden Ring OLED Cheat Sheet"
SCREEN_EVENT = "BOSSINFO"
DEVICE_TYPE = "screened-128x40"

# GG deregisters a game that stops sending heartbeats. Ship well inside it.
HEARTBEAT_INTERVAL = 10.0


class Transport(Protocol):
    def post(self, url: str, payload: dict[str, Any]) -> Any:
        ...


class UrllibTransport:
    """Standard-library HTTP POST. No third-party dependency."""

    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = timeout

    def post(self, url: str, payload: dict[str, Any]) -> Any:  # pragma: no cover
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
            return json.loads(body) if body else None
        except urllib.error.URLError as exc:
            raise GameSenseError(f"POST {url} failed: {exc}") from exc


class RecordingTransport:
    """Captures calls instead of sending them. For tests and --dry-run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, payload: dict[str, Any]) -> Any:
        self.calls.append((url, payload))
        return None

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    def last_payload(self) -> dict[str, Any] | None:
        return self.calls[-1][1] if self.calls else None


class GameSenseError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoreProps:
    address: str

    @property
    def base_url(self) -> str:
        return f"http://{self.address}"


def core_props_path() -> Path:
    """Standard install locations for coreProps.json."""
    if os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "SteelSeries" / "SteelSeries Engine 3" / "coreProps.json"
    # macOS; included so the module imports and tests run anywhere.
    return Path("/Library/Application Support/SteelSeries Engine 3/coreProps.json")


def read_core_props(path: Path | str | None = None) -> CoreProps:
    target = Path(path) if path is not None else core_props_path()
    if not target.exists():
        raise GameSenseError(
            f"coreProps.json not found at {target}. Is SteelSeries GG running?"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GameSenseError(f"{target}: invalid JSON -- {exc}") from exc

    address = payload.get("address")
    if not address:
        raise GameSenseError(f"{target}: no 'address' key")
    return CoreProps(address=address)


class GameSenseClient:
    def __init__(
        self,
        core_props: CoreProps,
        *,
        transport: Transport | None = None,
        game: str = GAME_NAME,
        clock=time.monotonic,
    ) -> None:
        self.core = core_props
        self.transport = transport or UrllibTransport()
        self.game = game
        self._clock = clock
        self._last_heartbeat: float | None = None
        self._registered = False

    # --- lifecycle ---------------------------------------------------------

    def register(self, display_name: str = GAME_DISPLAY_NAME, developer: str = "erdle") -> None:
        self._post(
            "/game_metadata",
            {
                "game": self.game,
                "game_display_name": display_name,
                "developer": developer,
                # Keeps the OLED from being yanked back by GG's own apps
                # the instant we stop sending frames.
                "deinitialize_timer_length_ms": 15000,
            },
        )
        self.bind_screen_event()
        self._registered = True

    def bind_screen_event(self, event: str = SCREEN_EVENT) -> None:
        """Bind a screen handler that renders whatever bitmap we send.

        `has-text: false` plus an `image-data-128x40` frame key is the
        documented route for dynamic bitmaps: the handler declares the
        shape, each event supplies the pixels.
        """
        self._post(
            "/bind_game_event",
            {
                "game": self.game,
                "event": event,
                "min_value": 0,
                "max_value": 100,
                "icon_id": 1,
                "value_optional": True,
                "handlers": [
                    {
                        "device-type": DEVICE_TYPE,
                        "zone": "one",
                        "mode": "screen",
                        "datas": [
                            {
                                "has-text": False,
                                "image-data": [0] * 640,
                            }
                        ],
                    }
                ],
            },
        )

    def send_bitmap(self, packed: list[int], event: str = SCREEN_EVENT) -> None:
        if len(packed) != 640:
            raise ValueError(f"expected 640 packed bytes, got {len(packed)}")
        self._post(
            "/game_event",
            {
                "game": self.game,
                "event": event,
                "data": {
                    "value": 0,
                    "frame": {"image-data-128x40": packed},
                },
            },
        )

    def heartbeat(self, *, force: bool = False) -> bool:
        """Send a heartbeat if one is due. Returns True if sent."""
        now = self._clock()
        if not force and self._last_heartbeat is not None:
            if (now - self._last_heartbeat) < HEARTBEAT_INTERVAL:
                return False
        self._last_heartbeat = now
        self._post("/game_heartbeat", {"game": self.game})
        return True

    def remove_game(self) -> None:
        self._post("/remove_game", {"game": self.game})
        self._registered = False

    # --- internals ---------------------------------------------------------

    def _post(self, endpoint: str, payload: dict[str, Any]) -> Any:
        return self.transport.post(f"{self.core.base_url}{endpoint}", payload)

    @classmethod
    def discover(cls, *, transport: Transport | None = None, **kwargs) -> "GameSenseClient":
        return cls(read_core_props(), transport=transport, **kwargs)
