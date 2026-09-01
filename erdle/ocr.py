"""OCR abstraction.

The real engine is a runtime dependency we do not want to force on tests
(or on users who only want to run the detector). Everything downstream
talks to the `TextRecogniser` protocol, so a scripted fake substitutes
cleanly.

Preprocessing matters more than engine choice here. The boss name plate is
light text on a dark gradient; thresholding to pure black-on-white before
handing it to any engine is worth more accuracy than swapping engines.
"""

from __future__ import annotations

import sys
import os
import shlex
import shutil
from pathlib import Path
from typing import Protocol, Sequence

from .detect import RGB, Frame

# Brightness cutoff separating the boss name from what is behind it.
#
# Measured: white name glyphs sit around luma 230, but sunlit Limgrave
# grass reaches 139. The original 130 let the terrain through, so the
# crop-to-text step grabbed the whole plate and OCR read foliage. 170
# clears the brightest terrain with margin while keeping the glyphs.
DEFAULT_INK_THRESHOLD = 170


class TextRecogniser(Protocol):
    def read(self, frame: Frame, threshold: int | None = None) -> str:
        """Best-effort transcription, optionally at a specific cutoff."""
        ...


class ScriptedRecogniser:
    """Returns queued strings in order. For tests and dry runs."""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls = 0

    def read(self, frame: Frame, threshold: int | None = None) -> str:
        self.calls += 1
        if not self._responses:
            return ""
        if self._index < len(self._responses):
            value = self._responses[self._index]
            self._index += 1
            return value
        return self._responses[-1]  # hold the last value


class NullRecogniser:
    def read(self, frame: Frame, threshold: int | None = None) -> str:
        return ""


def binarise(
    frame: Frame, *, threshold: int = DEFAULT_INK_THRESHOLD, light_text: bool = True
) -> list[list[int]]:
    """Threshold a region to 1-bit, returning 1 for *ink* and 0 for paper.

    Boss names are light glyphs on dark backing, so with `light_text` the
    bright pixels are the ink. Callers render ink as black on white, which
    is the polarity OCR engines expect.
    """
    rows: list[list[int]] = []
    for y in range(frame.height):
        row: list[int] = []
        for x in range(frame.width):
            bright = _luma(frame.pixel(x, y)) >= threshold
            row.append(1 if bright == light_text else 0)
        rows.append(row)
    return rows


def _luma(rgb: RGB) -> float:
    red, green, blue = rgb
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def ink_bounds(
    frame: Frame, *, threshold: int = DEFAULT_INK_THRESHOLD, margin: int = 6
) -> tuple[int, int, int, int] | None:
    """Bounding box of bright pixels, or None if the region is blank.

    The name plate region spans the full width of the bar, but the name
    itself occupies only its left third -- "Tree Sentinel" is about 260px
    of a 2000px region at 4K. Handing OCR a mostly-empty strip wastes time
    and hurts accuracy, so crop to the text before recognising.
    """
    left, top = frame.width, frame.height
    right = bottom = -1
    for y in range(frame.height):
        for x in range(frame.width):
            if _luma(frame.pixel(x, y)) >= threshold:
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
    if right < 0:
        return None
    return (
        max(left - margin, 0),
        max(top - margin, 0),
        min(right + margin + 1, frame.width),
        min(bottom + margin + 1, frame.height),
    )


def column_ink(frame: Frame, *, threshold: int = DEFAULT_INK_THRESHOLD) -> list[int]:
    """Count of bright pixels in each column."""
    counts = [0] * frame.width
    for y in range(frame.height):
        for x in range(frame.width):
            if _luma(frame.pixel(x, y)) >= threshold:
                counts[x] += 1
    return counts


def text_bounds(
    frame: Frame,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
    min_column_density: float = 0.05,
    max_gap_fraction: float = 0.025,
    margin: int = 6,
) -> tuple[int, int, int, int] | None:
    """Locate the densest block of text, ignoring isolated bright specks.

    A plain bounding box is useless here: one stray bright pixel at the far
    end of a 2000px plate stretches the box across the whole thing, and OCR
    gets handed the noise it was supposed to be spared. Measured on a real
    capture, the "cropped" region was still 1333 of 2006 pixels wide.

    Text is *dense* -- many lit pixels per column, over consecutive columns.
    Specks are sparse. So score columns by ink, keep the ones above a floor,
    group them into runs (tolerating letter and word gaps), and take the run
    holding the most ink.
    """
    counts = column_ink(frame, threshold=threshold)
    min_count = max(1, int(frame.height * min_column_density))
    max_gap = max(4, int(frame.width * max_gap_fraction))

    runs: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    gap = 0
    for x, count in enumerate(counts):
        if count >= min_count:
            if start is None:
                start = x
            end = x
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                runs.append((start, end))
                start = None
                gap = 0
    if start is not None:
        runs.append((start, end))

    if not runs:
        return None

    left, right = max(runs, key=lambda run: sum(counts[run[0] : run[1] + 1]))

    # Vertical extent, measured only within the chosen columns.
    top, bottom = None, None
    for y in range(frame.height):
        lit = sum(
            1
            for x in range(left, right + 1)
            if _luma(frame.pixel(x, y)) >= threshold
        )
        if lit > 0:
            if top is None:
                top = y
            bottom = y
    if top is None:
        return None

    return (
        max(left - margin, 0),
        max(top - margin, 0),
        min(right + margin + 1, frame.width),
        min(bottom + margin + 1, frame.height),
    )


def crop_to_ink(frame: Frame, *, threshold: int = DEFAULT_INK_THRESHOLD, margin: int = 6) -> Frame:
    """Crop to the densest block of text; unchanged if nothing is found."""
    bounds = text_bounds(frame, threshold=threshold, margin=margin)
    if bounds is None:
        return frame
    left, top, right, bottom = bounds
    pixels = []
    for y in range(top, bottom):
        pixels.extend(frame.scanline(y, left, right))
    return Frame(right - left, bottom - top, pixels)


def region_ink_fraction(
    frame: Frame,
    rect,
    *,
    threshold: int = DEFAULT_INK_THRESHOLD,
    step: int = 3,
) -> float:
    """Lit fraction inside a rect, without materialising a crop.

    Two economies over cropping and then measuring, and both matter: the
    name band is 351k pixels at 4K, so building the crop costs more than
    everything else in the loop combined, and it is wasted whenever the
    answer is "no text here" -- which is most of the time.

    `step` subsamples. Deciding whether a region contains text does not
    need every pixel; deciding what the text says does.
    """
    lit = total = 0
    for y in range(rect.top, rect.bottom, step):
        for x in range(rect.left, rect.right, step):
            total += 1
            if _luma(frame.pixel(x, y)) >= threshold:
                lit += 1
    return (lit / total) if total else 0.0


def estimate_text_presence(frame: Frame, *, threshold: int = DEFAULT_INK_THRESHOLD) -> float:
    """Fraction of pixels bright enough to be glyph strokes.

    Cheap gate: if the name plate is essentially empty there is no point
    paying for an OCR pass. Real name plates land roughly in 0.03-0.25.
    """
    if frame.width == 0 or frame.height == 0:
        return 0.0
    lit = 0
    for y in range(frame.height):
        for x in range(frame.width):
            if _luma(frame.pixel(x, y)) >= threshold:
                lit += 1
    return lit / (frame.width * frame.height)


#: Checked through a module constant rather than `os.name` at each use.
#: Tests need to exercise both branches, and monkeypatching `os.name`
#: patches the real os module -- which breaks pathlib for the whole
#: process and takes the test runner down with it.
IS_WINDOWS = os.name == "nt"


class TesseractRecogniser:
    """Thin adapter over pytesseract. Imported lazily and optional."""

    def __init__(self, *, threshold: int = DEFAULT_INK_THRESHOLD, config: str | None = None) -> None:
        self._threshold = threshold
        # Constrain the alphabet: boss names never contain digits, and
        # telling the engine so removes a whole class of confusions.
        #
        # No apostrophe, on any platform. pytesseract splits the config
        # string with `shlex.split(config, posix=system() != "Windows")`,
        # and an earlier version of this file assumed non-posix splitting
        # would take a lone `\'` literally. It does not -- non-posix shlex
        # still requires quotes to balance, so both modes raise
        # ValueError("No closing quotation") before Tesseract is ever
        # run. That killed the capture loop mid-fight the moment a
        # vendored Tesseract made this path reachable.
        #
        # Nothing is lost: `matching.normalise` strips punctuation before
        # comparing, so "Commander O'Neil" is matched on "COMMANDER ONEIL"
        # whether or not the engine is allowed to emit the apostrophe.
        whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz,- "
        self._config = config or f"--psm 7 -c tessedit_char_whitelist={whitelist}"
        # A caller-supplied config gets the same guarantee. Failing here
        # is a clear error at startup rather than a crash three hours in.
        try:
            shlex.split(self._config, posix=False)
            shlex.split(self._config, posix=True)
        except ValueError as exc:
            raise ValueError(
                f"tesseract config is not shell-splittable: {exc}"
            ) from exc
        self._pytesseract = None
        self._Image = None

    # winget and the UB-Mannheim installer frequently do not update PATH
    # for the current session, so probe the usual install locations before
    # giving up. Saves a "but I installed it" round trip.
    _WINDOWS_CANDIDATES = (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe",
        r"%USERPROFILE%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
    )

    @classmethod
    def bundled_binary(cls) -> Path | None:
        """Tesseract shipped inside the build, if this is a frozen exe.

        The whole point of bundling is that a new user does not have to
        install anything: detection is name-driven, so without a reader
        the app does nothing at all rather than merely losing a feature.
        """
        base = getattr(sys, "_MEIPASS", None)
        if base is None:
            # Running from source: use the vendored copy if it is there.
            base = Path(__file__).resolve().parent.parent / "vendor"
        else:
            base = Path(base)
        folder = base / "tesseract"
        # `tesseract.exe` on Windows and nothing else. The extensionless
        # name is only accepted off Windows, where it is how the binary is
        # actually named -- accepting it everywhere meant a Linux binary
        # left in vendor/ would be picked up on Windows and fail to
        # execute, with an error blaming Tesseract rather than the file.
        names = ("tesseract.exe",) if IS_WINDOWS else ("tesseract",)
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def locate_binary(cls) -> str | None:
        """Find tesseract: bundled, then $TESSERACT_CMD, PATH, install dirs.

        Bundled first so a build behaves the same on every machine. An
        explicit $TESSERACT_CMD still wins over a system install, for
        anyone deliberately pointing at their own copy.
        """
        bundled = cls.bundled_binary()
        if bundled is not None:
            return str(bundled)

        explicit = os.environ.get("TESSERACT_CMD")
        if explicit and Path(explicit).exists():
            return explicit

        on_path = shutil.which("tesseract")
        if on_path:
            return on_path

        for candidate in cls._WINDOWS_CANDIDATES:
            expanded = Path(os.path.expandvars(candidate))
            if expanded.exists():
                return str(expanded)
        return None

    @staticmethod
    def prepare_environment(binary: str) -> None:
        """Point Tesseract at its own language data.

        A bundled tesseract.exe has no registry entry and no install
        directory to fall back on, so without TESSDATA_PREFIX it starts,
        fails to load `eng`, and reports an error that says nothing about
        the real cause.
        """
        tessdata = Path(binary).parent / "tessdata"
        if tessdata.is_dir():
            os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))

    @classmethod
    def availability(cls) -> tuple[bool, str]:
        """Check for both the Python package and the Tesseract binary.

        Checked up front rather than on first use, so a missing binary
        surfaces at startup instead of several minutes into a fight.
        """
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # noqa: F401
        except ImportError:
            return False, "pip install pytesseract pillow"

        binary = cls.locate_binary()
        if binary is None:
            return False, (
                "Tesseract binary not found. Install it with\n"
                "    winget install UB-Mannheim.TesseractOCR\n"
                "then reopen your terminal. If it is already installed, set\n"
                "    $env:TESSERACT_CMD = 'C:\\path\\to\\tesseract.exe'"
            )

        cls.prepare_environment(binary)
        pytesseract.pytesseract.tesseract_cmd = binary
        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:
            return False, f"found {binary} but could not run it: {exc}"
        return True, f"Tesseract {version} at {binary}"

    def _ensure_loaded(self) -> None:
        if self._pytesseract is not None:
            return
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "TesseractRecogniser needs `pip install pytesseract pillow` "
                "plus the Tesseract binary on PATH."
            ) from exc
        binary = self.locate_binary()
        if binary is not None:
            self.prepare_environment(binary)
            pytesseract.pytesseract.tesseract_cmd = binary
        self._pytesseract = pytesseract
        self._Image = Image

    def read(
        self, frame: Frame, threshold: int | None = None
    ) -> str:  # pragma: no cover - needs the binary
        self._ensure_loaded()
        assert self._Image is not None and self._pytesseract is not None
        cutoff = self._threshold if threshold is None else threshold
        cropped = crop_to_ink(frame, threshold=cutoff)
        bits = binarise(cropped, threshold=cutoff, light_text=True)
        image = self._Image.new("L", (cropped.width, cropped.height))
        # ink -> black, paper -> white
        image.putdata([0 if bit else 255 for row in bits for bit in row])
        # Upscaling helps Tesseract considerably on small HUD text, but a
        # 4K name plate is already large; cap the result to stay fast.
        scale = max(1, min(4, 240 // max(cropped.height, 1)))
        if scale > 1:
            image = image.resize(
                (cropped.width * scale, cropped.height * scale),
                self._Image.LANCZOS,
            )
        return self._pytesseract.image_to_string(image, config=self._config).strip()
