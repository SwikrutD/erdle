"""Frame sources.

`mss` grabs from the OS compositor, never from the game. Run Elden Ring in
borderless windowed -- exclusive fullscreen can defeat desktop capture on
some driver stacks.
"""

from __future__ import annotations

from typing import Iterator, Protocol, Sequence

from .detect import RGB, Frame
from .geometry import CENTRE_BANNER, HUD_STRIP, FractionalRect


class FrameSource(Protocol):
    def grab(self) -> Frame:
        ...


class ReplaySource:
    """Plays back a fixed list of frames, holding on the last one."""

    def __init__(self, frames: Sequence[Frame], *, loop: bool = False) -> None:
        if not frames:
            raise ValueError("ReplaySource needs at least one frame")
        self._frames = list(frames)
        self._index = 0
        self._loop = loop
        self.grabs = 0

    def grab(self) -> Frame:
        self.grabs += 1
        frame = self._frames[self._index]
        if self._index < len(self._frames) - 1:
            self._index += 1
        elif self._loop:
            self._index = 0
        return frame

    def __iter__(self) -> Iterator[Frame]:
        return iter(self._frames)


class MSSSource:  # pragma: no cover - requires a display server
    """Desktop capture via `mss`.

    Grabs only the two HUD strips we care about rather than the whole
    screen; at 1440p that is roughly 3% of the pixels, which is the
    difference between a 2ms and a 40ms loop.
    """

    def __init__(self, monitor_index: int = 1) -> None:
        try:
            import mss  # type: ignore
        except ImportError as exc:
            raise RuntimeError("MSSSource needs `pip install mss`") from exc
        self._mss = mss
        self._sct = mss.mss()
        self._monitor = self._sct.monitors[monitor_index]

    @property
    def width(self) -> int:
        return self._monitor["width"]

    @property
    def height(self) -> int:
        return self._monitor["height"]

    def grab(self) -> Frame:
        """Full screen. Correct, but slow -- prefer `grab_hud_strip`.

        Converting a whole 1440p framebuffer into Python tuples is several
        million allocations per frame and will not sustain a capture loop.
        Kept for calibration and debugging.
        """
        return self._to_frame(self._sct.grab(self._monitor))

    def grab_region(
        self, left: int, top: int, width: int, height: int, step: int = 1
    ) -> Frame:
        box = {
            "left": self._monitor["left"] + left,
            "top": self._monitor["top"] + top,
            "width": width,
            "height": height,
        }
        return self._to_frame(self._sct.grab(box), step=step)

    def grab_hud_strip(self, strip: FractionalRect = HUD_STRIP) -> Frame:
        """Grab only the band containing the boss bar and name plate.

        Use with `AppConfig.for_hud_strip()`, which remaps the detector's
        regions into this crop's coordinate space.
        """
        rect = strip.resolve(self.width, self.height)
        return self.grab_region(rect.left, rect.top, rect.width, rect.height)

    def grab_banner(
        self, region: FractionalRect = CENTRE_BANNER, step: int = 4
    ) -> Frame:
        """Grab the centre banner, subsampled.

        The banner region is ~660k pixels at 4K, and converting that to
        Python tuples every frame would cost more than the rest of the loop
        combined. Death and victory text is enormous, so a quarter-scale
        sample is plenty to notice it -- full resolution is only needed
        once something is actually there.
        """
        rect = region.resolve(self.width, self.height)
        return self.grab_region(
            rect.left, rect.top, rect.width, rect.height, step=step
        )

    @staticmethod
    def _to_frame(raw, step: int = 1) -> Frame:
        data = raw.raw  # BGRA

        if step <= 1:
            # Hand the buffer over untouched. Converting every pixel to a
            # tuple costs ~100ms for a 4K HUD strip, and the detector reads
            # three scanlines out of it. Frame builds tuples on demand.
            return Frame.from_bgra(data, raw.width, raw.height)

        # Subsampling has to materialise, since the result is not a
        # contiguous view of the original buffer. Only used for the banner
        # region, where the whole frame really is scanned.
        width = (raw.width + step - 1) // step
        height = (raw.height + step - 1) // step
        stride = raw.width * 4
        pixels: list[RGB] = []
        for y in range(0, raw.height, step):
            row = y * stride
            for x in range(0, raw.width, step):
                i = row + x * 4
                pixels.append((data[i + 2], data[i + 1], data[i]))
        return Frame(width, height, pixels)

    def close(self) -> None:
        self._sct.close()
