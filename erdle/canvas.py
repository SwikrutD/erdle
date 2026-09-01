"""A 1-bit drawing surface sized for the Apex Pro's OLED.

GameSense expects `image-data-128x40` as 640 bytes: one bit per pixel,
packed MSB-first, left-to-right then top-to-bottom. 128 * 40 / 8 == 640.
"""

from __future__ import annotations

from .font import ADVANCE, GLYPH_HEIGHT, GLYPH_WIDTH, fit_text, glyph_for, text_width

WIDTH = 128
HEIGHT = 40
PACKED_SIZE = WIDTH * HEIGHT // 8


class Canvas:
    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("canvas dimensions must be positive")
        if (width * height) % 8 != 0:
            raise ValueError("canvas pixel count must be byte-aligned")
        self.width = width
        self.height = height
        self._pixels = bytearray(width * height)

    # --- primitives --------------------------------------------------------

    def clear(self, value: int = 0) -> None:
        self._pixels[:] = bytes([1 if value else 0]) * (self.width * self.height)

    def get(self, x: int, y: int) -> int:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return 0
        return self._pixels[y * self.width + x]

    def set(self, x: int, y: int, value: int = 1) -> None:
        """Set a pixel. Out-of-bounds writes are silently clipped."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self._pixels[y * self.width + x] = 1 if value else 0

    def fill_rect(self, x: int, y: int, w: int, h: int, value: int = 1) -> None:
        for dy in range(h):
            row = y + dy
            if not (0 <= row < self.height):
                continue
            start = max(x, 0)
            end = min(x + w, self.width)
            if end <= start:
                continue
            base = row * self.width
            fill = 1 if value else 0
            for col in range(start, end):
                self._pixels[base + col] = fill

    def draw_rect(self, x: int, y: int, w: int, h: int, value: int = 1) -> None:
        """Outline only."""
        if w <= 0 or h <= 0:
            return
        self.fill_rect(x, y, w, 1, value)
        self.fill_rect(x, y + h - 1, w, 1, value)
        self.fill_rect(x, y, 1, h, value)
        self.fill_rect(x + w - 1, y, 1, h, value)

    def hline(self, x: int, y: int, w: int, value: int = 1) -> None:
        self.fill_rect(x, y, w, 1, value)

    # --- text --------------------------------------------------------------

    def draw_char(self, char: str, x: int, y: int, value: int = 1) -> int:
        glyph = glyph_for(char)
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit:
                    self.set(x + col_index, y + row_index, value)
        return ADVANCE

    def draw_text(self, text: str, x: int, y: int, value: int = 1) -> int:
        """Draw a string; returns the x cursor after the final glyph."""
        cursor = x
        for char in text:
            self.draw_char(char, cursor, y, value)
            cursor += ADVANCE
        return cursor - 1 if text else x

    def draw_text_centered(self, text: str, y: int, value: int = 1) -> None:
        clipped = fit_text(text, self.width)
        x = max((self.width - text_width(clipped)) // 2, 0)
        self.draw_text(clipped, x, y, value)

    # --- output ------------------------------------------------------------

    def to_rows(self) -> list[str]:
        """Debug view: one string per row, '#' for lit pixels."""
        rows = []
        for y in range(self.height):
            base = y * self.width
            rows.append(
                "".join("#" if self._pixels[base + x] else "." for x in range(self.width))
            )
        return rows

    def pack(self) -> list[int]:
        """Pack to GameSense's MSB-first bitmap byte list."""
        packed: list[int] = []
        accumulator = 0
        bit_count = 0
        for value in self._pixels:
            accumulator = (accumulator << 1) | (1 if value else 0)
            bit_count += 1
            if bit_count == 8:
                packed.append(accumulator)
                accumulator = 0
                bit_count = 0
        if bit_count:  # unreachable while dimensions stay byte-aligned
            packed.append(accumulator << (8 - bit_count))
        return packed

    @classmethod
    def from_packed(cls, data: list[int], width: int = WIDTH, height: int = HEIGHT) -> "Canvas":
        """Inverse of `pack`, for round-trip testing."""
        expected = width * height // 8
        if len(data) != expected:
            raise ValueError(f"expected {expected} bytes, got {len(data)}")
        canvas = cls(width, height)
        index = 0
        for byte in data:
            for bit in range(7, -1, -1):
                canvas._pixels[index] = (byte >> bit) & 1
                index += 1
        return canvas


def progress_bar(
    canvas: Canvas, x: int, y: int, w: int, h: int, ratio: float, *, border: bool = True
) -> None:
    """Draw a bordered horizontal bar filled to `ratio` (clamped to 0..1)."""
    ratio = max(0.0, min(1.0, ratio))
    if border:
        canvas.draw_rect(x, y, w, h)
        inner_x, inner_y = x + 1, y + 1
        inner_w, inner_h = max(w - 2, 0), max(h - 2, 0)
    else:
        inner_x, inner_y, inner_w, inner_h = x, y, w, h
    filled = int(round(inner_w * ratio))
    # Any non-zero ratio should show at least one column, so the bar never
    # reads as empty while the boss is still alive.
    if ratio > 0 and filled == 0:
        filled = 1
    canvas.fill_rect(inner_x, inner_y, filled, inner_h)
