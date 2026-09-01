"""The recogniser the app actually uses: atlas first, Tesseract as tutor.

Behaviour, in order:

1. Try the glyph atlas. If it reads a high enough fraction of the plate,
   return that. No external process, no 30 MB dependency, and it is the
   same answer every time -- template matching has no temperature.
2. Otherwise fall back to Tesseract, if present.
3. Whenever the fallback produces a name that the boss matcher accepts,
   segment the plate and file its glyphs under the letters they must be.

So the atlas fills itself in during ordinary play and the fallback stops
being consulted. A user without Tesseract gets whatever atlas ships with
the build; a user with it contributes to their own.

The learning step is deliberately conservative. It only fires on a name
that matched a real boss with high confidence, and only when the glyph
count agrees with the string length, because one mislabelled sample stays
in the atlas forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .detect import Frame
from .glyphs import GlyphAtlas, learn_from_any_line, read_text
from .ocr import DEFAULT_INK_THRESHOLD, TextRecogniser


@dataclass
class AtlasRecogniser:
    """Reads with the atlas; optionally backed by another recogniser."""

    atlas: GlyphAtlas
    fallback: TextRecogniser | None = None
    # Fraction of glyphs the atlas must recognise before its answer is
    # trusted without consulting the fallback.
    min_recognised: float = 0.8
    max_distance: int = 10
    autosave: bool = True
    # Below this many learned characters the atlas cannot spell a boss
    # name, so consulting it is pure cost. It was costing 180ms per poll
    # at 4K -- twelve times the rest of the loop -- segmenting a band for
    # an atlas that could never return anything.
    min_alphabet: int = 20

    atlas_reads: int = field(default=0, init=False)
    fallback_reads: int = field(default=0, init=False)
    glyphs_learned: int = field(default=0, init=False)
    #: Set when the tutor was retired mid-run, so `summary` can say why
    #: the fallback count stopped moving.
    fallback_error: str | None = field(default=None, init=False)
    _dirty: bool = field(default=False, init=False)

    def read(self, frame: Frame, threshold: int | None = None) -> str:
        cutoff = DEFAULT_INK_THRESHOLD if threshold is None else threshold

        if len(self.atlas) < self.min_alphabet:
            # Too sparse to spell anything. Skip straight to the fallback
            # rather than paying for segmentation that cannot succeed.
            if self.fallback is None:
                return ""
            return self._ask_fallback(frame, cutoff, "")

        # Deliberately NOT cropped first. Narrowing to the densest ink
        # block before segmenting looked like an easy win -- the band is
        # 1651x138 at 4K -- but crop_to_ink keeps only the densest column
        # run, and a slightly-too-wide letter gap splits a name and drops
        # the tail. It turned "MALENIA" into "MALEN". A truncated name is
        # a wrong answer; the alphabet gate above already removes the cost
        # in the case that actually mattered.
        text, coverage = read_text(
            frame, self.atlas, threshold=cutoff, max_distance=self.max_distance
        )
        if text and coverage >= self.min_recognised:
            self.atlas_reads += 1
            return text

        if self.fallback is None:
            # Even a partial read beats nothing: the boss matcher tolerates
            # substitutions, and '?' preserves the string length.
            return text

        return self._ask_fallback(frame, cutoff, text)

    def _ask_fallback(self, frame: Frame, cutoff: int, partial: str) -> str:
        """Consult the tutor. Both call sites go through here.

        There used to be two copies of this, and only one of them was
        guarded -- which is the usual way a fix misses the path that
        actually fires.
        """
        self.fallback_reads += 1
        try:
            return self.fallback.read(frame, cutoff)
        except TypeError:
            # Older fallbacks take no threshold. Narrow on purpose: this
            # is a signature mismatch, not a failure to read.
            try:
                return self.fallback.read(frame)
            except Exception as exc:
                return self._fallback_failed(exc, partial)
        except Exception as exc:
            return self._fallback_failed(exc, partial)

    def _fallback_failed(self, exc: Exception, partial: str) -> str:
        """Retire the tutor rather than take the whole run down with it.

        The tutor is an optional convenience -- it labels plates so the
        atlas can learn them -- and the atlas keeps working without it.
        Letting an exception out of here ended a session with a traceback
        in the middle of a boss fight, which is the worst possible moment
        and the least recoverable one.

        Retiring rather than retrying is deliberate: every observed
        failure mode (a malformed config, a missing binary, a deleted
        traineddata file) is constant for the process, so retrying
        fifteen times a second would only bury the message.
        """
        self.fallback = None
        self.fallback_error = f"{type(exc).__name__}: {exc}"
        return partial

    # --- learning ---------------------------------------------------------

    def teach(
        self, frame: Frame, confirmed_text: str, *, threshold: int | None = None
    ) -> int:
        """File glyphs from a plate whose text is now known to be correct."""
        cutoff = DEFAULT_INK_THRESHOLD if threshold is None else threshold
        # Per-line, not the whole band: a duo fight stacks two names, and
        # segmenting both at once yields a count that can never match
        # either, so the sample is refused and nothing is ever learned
        # from those fights.
        learned = learn_from_any_line(
            frame, confirmed_text, self.atlas, threshold=cutoff
        )
        if learned:
            self.glyphs_learned += learned
            self._dirty = True
            if self.autosave:
                self.flush()
        return learned

    def flush(self) -> bool:
        if not self._dirty or self.atlas.path is None:
            return False
        try:
            self.atlas.save()
            self._dirty = False
            return True
        except (OSError, ValueError):
            return False

    # --- reporting --------------------------------------------------------

    def summary(self) -> str:
        return (
            f"atlas: {len(self.atlas)} characters, "
            f"{self.atlas.total_samples} samples; "
            f"{self.atlas_reads} reads, {self.fallback_reads} fallbacks"
            + (f"; tutor retired -- {self.fallback_error}"
               if self.fallback_error else "")
        )

    #: Below this many characters the atlas cannot carry a name on its
    #: own. The roster uses 51; a plate reads as mostly '?' long before
    #: that, and the matcher stops resolving well short of it.
    MIN_USABLE_ALPHABET = 45

    @property
    def atlas_is_usable(self) -> bool:
        """Whether names can be read with no fallback at all.

        Used to decide if the app should say so loudly at startup.
        Detection is name-driven, so "no reader" is not a lost feature --
        it is an app that runs forever and never sees a boss.
        """
        return len(self.atlas) >= self.MIN_USABLE_ALPHABET

    @property
    def is_self_sufficient(self) -> bool:
        """True once the atlas has been carrying the reads on its own."""
        return self.atlas_reads > 0 and self.fallback_reads == 0


def build_recogniser(
    atlas_path: Path | None = None,
    fallback: TextRecogniser | None = None,
    *,
    writable_path: Path | None = None,
) -> AtlasRecogniser:
    """Load the shipped atlas, but learn into a writable copy.

    A frozen build's data directory is read-only, so learned glyphs go to
    the user's config directory instead.
    """
    from .config import config_dir
    from .glyphs import default_atlas_path

    shipped = GlyphAtlas.load(atlas_path or default_atlas_path())
    target = writable_path or (config_dir() / "glyphs.json")

    learned = GlyphAtlas.load(target)
    for char, signatures in learned.samples.items():
        for signature, height in signatures:
            shipped.learn(char, signature, height)
    shipped.path = target

    return AtlasRecogniser(atlas=shipped, fallback=fallback)
