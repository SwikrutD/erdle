import pytest

from erdle.detect import Frame, make_test_frame
from erdle.ocr import (
    NullRecogniser,
    ScriptedRecogniser,
    binarise,
    estimate_text_presence,
)

DARK = (10, 10, 10)
LIGHT = (240, 240, 235)


def two_tone(width=8, height=4, light_columns=3) -> Frame:
    pixels = []
    for _ in range(height):
        for x in range(width):
            pixels.append(LIGHT if x < light_columns else DARK)
    return Frame(width, height, pixels)


# --- binarise polarity -----------------------------------------------------


def test_light_text_marks_bright_pixels_as_ink():
    """Boss names are light glyphs on dark backing; the glyph is the ink."""
    bits = binarise(two_tone(), light_text=True)
    assert bits[0] == [1, 1, 1, 0, 0, 0, 0, 0]


def test_dark_text_inverts_the_polarity():
    bits = binarise(two_tone(), light_text=False)
    assert bits[0] == [0, 0, 0, 1, 1, 1, 1, 1]


def test_binarise_preserves_shape():
    bits = binarise(two_tone(width=12, height=5))
    assert len(bits) == 5
    assert all(len(row) == 12 for row in bits)


def test_threshold_is_respected():
    mid = Frame(2, 1, [(120, 120, 120), (140, 140, 140)])
    assert binarise(mid, threshold=130) == [[0, 1]]
    assert binarise(mid, threshold=100) == [[1, 1]]


def test_binarise_uses_perceptual_luminance():
    """Pure blue is much darker than pure green at the same channel value."""
    frame = Frame(2, 1, [(0, 255, 0), (0, 0, 255)])
    assert binarise(frame, threshold=130) == [[1, 0]]


# --- presence gate ---------------------------------------------------------


def test_presence_is_zero_on_a_dark_region():
    frame = Frame(10, 10, [DARK] * 100)
    assert estimate_text_presence(frame) == 0.0


def test_presence_is_one_on_a_bright_region():
    frame = Frame(10, 10, [LIGHT] * 100)
    assert estimate_text_presence(frame) == 1.0


def test_presence_reports_the_lit_fraction():
    assert estimate_text_presence(two_tone(width=10, light_columns=2)) == pytest.approx(0.2)


def test_presence_on_an_empty_region_is_safe():
    assert estimate_text_presence(Frame(1, 1, [DARK])) == 0.0


def test_real_name_plate_clears_the_default_gate():
    from erdle.geometry import BOSS_NAME

    frame = make_test_frame(1920, 1080, bar_fill=1.0, with_name=True)
    crop = frame.region(BOSS_NAME.resolve(1920, 1080))
    assert estimate_text_presence(crop) > 0.012


# --- recognisers -----------------------------------------------------------


def test_scripted_recogniser_walks_then_holds():
    recogniser = ScriptedRecogniser(["a", "b"])
    frame = two_tone()
    assert recogniser.read(frame) == "a"
    assert recogniser.read(frame) == "b"
    assert recogniser.read(frame) == "b"
    assert recogniser.calls == 3


def test_scripted_recogniser_handles_an_empty_script():
    assert ScriptedRecogniser([]).read(two_tone()) == ""


def test_null_recogniser_returns_nothing():
    assert NullRecogniser().read(two_tone()) == ""


# --- ink cropping ----------------------------------------------------------
# The name plate spans the whole bar width but the name occupies only its
# left third. Handing OCR a mostly-blank strip hurts accuracy.


def _plate_with_text(width=400, height=40, text_w=80, text_h=14, left=10, top=12):
    pixels = [DARK] * (width * height)
    for y in range(top, top + text_h):
        for x in range(left, left + text_w):
            pixels[y * width + x] = LIGHT
    return Frame(width, height, pixels)


def test_ink_bounds_finds_the_text():
    from erdle.ocr import ink_bounds

    left, top, right, bottom = ink_bounds(_plate_with_text(), margin=0)
    assert (left, top) == (10, 12)
    assert (right, bottom) == (90, 26)


def test_ink_bounds_applies_margin():
    from erdle.ocr import ink_bounds

    left, top, _, _ = ink_bounds(_plate_with_text(), margin=5)
    assert (left, top) == (5, 7)


def test_ink_bounds_clamps_margin_at_the_edges():
    from erdle.ocr import ink_bounds

    frame = _plate_with_text(left=0, top=0)
    left, top, _, _ = ink_bounds(frame, margin=20)
    assert left == 0 and top == 0


def test_ink_bounds_returns_none_when_blank():
    from erdle.ocr import ink_bounds

    assert ink_bounds(Frame(50, 20, [DARK] * 1000)) is None


def test_crop_to_ink_shrinks_a_mostly_empty_plate():
    from erdle.ocr import crop_to_ink

    plate = _plate_with_text(width=2000, height=91, text_w=260, text_h=30)
    cropped = crop_to_ink(plate)
    assert cropped.width < plate.width / 5
    assert cropped.height < plate.height


def test_crop_to_ink_keeps_all_the_text():
    from erdle.ocr import crop_to_ink, estimate_text_presence

    plate = _plate_with_text(width=2000, height=91, text_w=260, text_h=30)
    lit_before = sum(
        1 for y in range(plate.height) for x in range(plate.width)
        if plate.pixel(x, y) == LIGHT
    )
    cropped = crop_to_ink(plate)
    lit_after = sum(
        1 for y in range(cropped.height) for x in range(cropped.width)
        if cropped.pixel(x, y) == LIGHT
    )
    assert lit_after == lit_before
    # And the text now dominates the frame instead of being a speck.
    assert estimate_text_presence(cropped) > estimate_text_presence(plate) * 5


def test_crop_to_ink_passes_a_blank_frame_through():
    from erdle.ocr import crop_to_ink

    blank = Frame(50, 20, [DARK] * 1000)
    assert crop_to_ink(blank) is blank


def test_crop_to_ink_on_a_realistic_4k_plate():
    from erdle.geometry import BOSS_NAME
    from erdle.ocr import crop_to_ink
    from erdle.detect import make_test_frame

    frame = make_test_frame(3840, 2160, bar_fill=1.0, with_name=True)
    plate = frame.region(BOSS_NAME.resolve(3840, 2160))
    cropped = crop_to_ink(plate)
    assert cropped.width < plate.width
    assert cropped.width > 20


# --- locating the Tesseract binary -----------------------------------------
# winget routinely installs Tesseract without updating PATH for the current
# session, so "I installed it and it still says missing" is the common case.



@pytest.fixture
def no_bundle(monkeypatch):
    """Neutralise the bundled Tesseract.

    `locate_binary` prefers the copy inside the build, so a machine that
    has actually vendored one would otherwise short-circuit every test of
    the fallback order. That preference is deliberate and is tested on its
    own in `test_tray.py`.
    """
    from erdle.ocr import TesseractRecogniser

    monkeypatch.setattr(
        TesseractRecogniser, "bundled_binary", classmethod(lambda cls: None)
    )


def test_locate_prefers_the_explicit_env_var(tmp_path, monkeypatch, no_bundle):
    from erdle.ocr import TesseractRecogniser

    fake = tmp_path / "tesseract.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("TESSERACT_CMD", str(fake))
    assert TesseractRecogniser.locate_binary() == str(fake)


def test_locate_ignores_a_bad_env_var(monkeypatch, no_bundle):
    from erdle.ocr import TesseractRecogniser

    monkeypatch.setenv("TESSERACT_CMD", "/nope/tesseract.exe")
    monkeypatch.setattr("erdle.ocr.shutil.which", lambda _: None)
    monkeypatch.setattr(TesseractRecogniser, "_WINDOWS_CANDIDATES", ())
    assert TesseractRecogniser.locate_binary() is None


def test_locate_falls_back_to_path(monkeypatch, no_bundle):
    from erdle.ocr import TesseractRecogniser

    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr("erdle.ocr.shutil.which", lambda _: "/usr/bin/tesseract")
    assert TesseractRecogniser.locate_binary() == "/usr/bin/tesseract"


def test_locate_probes_install_directories(tmp_path, monkeypatch, no_bundle):
    from erdle.ocr import TesseractRecogniser

    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr("erdle.ocr.shutil.which", lambda _: None)
    installed = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        TesseractRecogniser, "_WINDOWS_CANDIDATES",
        ("/definitely/not/here.exe", str(installed)),
    )
    assert TesseractRecogniser.locate_binary() == str(installed)


def test_locate_expands_environment_variables(tmp_path, monkeypatch, no_bundle):
    """The shipped candidates use %LOCALAPPDATA% and friends."""
    import os

    from erdle.ocr import TesseractRecogniser

    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr("erdle.ocr.shutil.which", lambda _: None)
    monkeypatch.setenv("MYPREFIX", str(tmp_path))
    target = tmp_path / "tesseract.exe"
    target.write_text("", encoding="utf-8")
    # expandvars understands %VAR% only on Windows and ${VAR} only on POSIX,
    # so use whichever syntax this platform actually supports.
    pattern = (
        r"%MYPREFIX%\tesseract.exe" if os.name == "nt" else "${MYPREFIX}/tesseract.exe"
    )
    monkeypatch.setattr(TesseractRecogniser, "_WINDOWS_CANDIDATES", (pattern,))
    assert TesseractRecogniser.locate_binary() == str(target)


def test_shipped_candidates_use_windows_syntax():
    """Guards the real table, which the platform-agnostic test above skips."""
    from erdle.ocr import TesseractRecogniser

    for candidate in TesseractRecogniser._WINDOWS_CANDIDATES:
        assert candidate.endswith("tesseract.exe")
    joined = " ".join(TesseractRecogniser._WINDOWS_CANDIDATES)
    assert "%LOCALAPPDATA%" in joined
    assert "Program Files" in joined


def test_availability_message_is_actionable(monkeypatch):
    from erdle.ocr import TesseractRecogniser

    monkeypatch.setattr(TesseractRecogniser, "locate_binary", classmethod(lambda cls: None))
    ok, reason = TesseractRecogniser.availability()
    if ok:  # pytesseract not installed in this environment; skip
        return
    assert "winget" in reason or "pip install" in reason


# --- threshold vs bright terrain -------------------------------------------
# Regression: boss names are drawn over whatever is behind the bar. In
# Limgrave that is sunlit grass at luma ~139, and the original cutoff of
# 130 let it through -- so crop_to_ink grabbed the entire plate and
# Tesseract was handed a picture of foliage.

GRASS = (150, 140, 95)      # luma ~139
NAME_TEXT = (238, 232, 218)  # luma ~232


def _plate_over_grass(width=2007, height=91):
    pixels = [GRASS] * (width * height)
    for y in range(30, 60):
        for x in range(20, 280):
            pixels[y * width + x] = NAME_TEXT
    return Frame(width, height, pixels)


def test_grass_is_brighter_than_the_old_threshold():
    """Documents why 130 failed."""
    from erdle.ocr import _luma

    assert _luma(GRASS) > 130
    assert _luma(NAME_TEXT) > _luma(GRASS) + 80


def test_default_threshold_clears_bright_terrain():
    from erdle.ocr import DEFAULT_INK_THRESHOLD, _luma

    assert DEFAULT_INK_THRESHOLD > _luma(GRASS)
    assert DEFAULT_INK_THRESHOLD < _luma(NAME_TEXT)


def test_old_threshold_would_have_grabbed_the_grass():
    from erdle.ocr import crop_to_ink

    plate = _plate_over_grass()
    assert crop_to_ink(plate, threshold=130).width == plate.width


def test_default_threshold_isolates_the_name():
    from erdle.ocr import crop_to_ink

    plate = _plate_over_grass()
    cropped = crop_to_ink(plate)
    assert cropped.width < plate.width * 0.25
    assert cropped.height < plate.height


def test_isolated_crop_still_contains_every_glyph_pixel():
    from erdle.ocr import crop_to_ink

    plate = _plate_over_grass()
    cropped = crop_to_ink(plate)
    lit = sum(
        1 for y in range(cropped.height) for x in range(cropped.width)
        if cropped.pixel(x, y) == NAME_TEXT
    )
    assert lit == 30 * 260


def test_presence_gate_still_fires_over_grass():
    from erdle.ocr import estimate_text_presence

    ink = estimate_text_presence(_plate_over_grass())
    assert 0.012 < ink < 0.5, ink


# --- density cropping vs stray specks --------------------------------------
# Regression from a real 4K capture: crop_to_ink reported 1333x91 out of a
# 2006x91 plate because a naive bounding box is stretched by a single bright
# pixel at the far edge. OCR then returned '' at three of seven thresholds.


def _plate_with_specks(width=2006, height=91):
    """Dense name on the left, isolated bright specks scattered right."""
    pixels = [(40, 38, 34)] * (width * height)
    for y in range(30, 62):
        for x in range(20, 290):
            pixels[y * width + x] = NAME_TEXT
    for x in (700, 1180, 1778, 1990):          # terrain glints
        for y in (12, 45, 80):
            pixels[y * width + x] = NAME_TEXT
    return Frame(width, height, pixels)


def test_naive_bounding_box_is_stretched_by_specks():
    """Documents the bug: this is why the old crop was useless."""
    from erdle.ocr import ink_bounds

    left, _, right, _ = ink_bounds(_plate_with_specks(), margin=0)
    assert left == 20
    assert right >= 1990, "a single speck should stretch the naive box"


def test_density_crop_ignores_the_specks():
    from erdle.ocr import crop_to_ink

    cropped = crop_to_ink(_plate_with_specks())
    assert cropped.width < 320, f"still {cropped.width}px wide"
    assert cropped.height < 50


def test_density_crop_keeps_the_whole_name():
    from erdle.ocr import crop_to_ink

    cropped = crop_to_ink(_plate_with_specks())
    lit = sum(
        1 for y in range(cropped.height) for x in range(cropped.width)
        if cropped.pixel(x, y) == NAME_TEXT
    )
    assert lit == 32 * 270


def test_density_crop_tolerates_letter_spacing():
    """Word gaps must not split the name into two runs."""
    from erdle.ocr import crop_to_ink

    width, height = 2006, 91
    pixels = [(40, 38, 34)] * (width * height)
    for block in ((20, 120), (150, 260), (300, 400)):   # three "words"
        for y in range(30, 62):
            for x in range(*block):
                pixels[y * width + x] = NAME_TEXT
    cropped = crop_to_ink(Frame(width, height, pixels))
    assert 380 < cropped.width < 440, cropped.width


def test_density_crop_picks_the_densest_block_not_the_first():
    from erdle.ocr import crop_to_ink

    width, height = 1200, 91
    pixels = [(40, 38, 34)] * (width * height)
    for y in range(40, 44):                     # thin scratch on the left
        for x in range(10, 90):
            pixels[y * width + x] = NAME_TEXT
    for y in range(30, 62):                     # the actual name
        for x in range(600, 850):
            pixels[y * width + x] = NAME_TEXT
    cropped = crop_to_ink(Frame(width, height, pixels))
    assert 240 < cropped.width < 300, cropped.width


def test_density_crop_returns_frame_when_nothing_is_dense():
    from erdle.ocr import crop_to_ink

    blank = Frame(100, 40, [(20, 20, 20)] * 4000)
    assert crop_to_ink(blank) is blank


def test_column_ink_counts_correctly():
    from erdle.ocr import column_ink

    frame = Frame(4, 3, [NAME_TEXT, DARK, NAME_TEXT, DARK] * 3)
    assert column_ink(frame) == [3, 0, 3, 0]


def test_narrowed_name_region_is_much_smaller():
    from erdle.geometry import BOSS_BAR, BOSS_NAME

    bar_width = BOSS_BAR.right - BOSS_BAR.left
    name_width = BOSS_NAME.right - BOSS_NAME.left
    assert name_width < bar_width * 0.6


def test_narrowed_name_region_still_fits_the_longest_name():
    """At 4K a 26-character name is roughly 520px; the region must cover it."""
    from erdle.geometry import BOSS_NAME

    rect = BOSS_NAME.resolve(3840, 2160)
    assert rect.width > 560


def test_only_an_exe_counts_as_bundled_on_windows(tmp_path, monkeypatch):
    """A stray non-Windows binary in vendor/ must not be picked up.

    It would be found, handed to pytesseract, and fail to execute -- with
    an error that blames Tesseract rather than the leftover file.
    """
    import sys

    from erdle.ocr import TesseractRecogniser

    (tmp_path / "tesseract").mkdir()
    (tmp_path / "tesseract" / "tesseract").write_bytes(b"elf")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    monkeypatch.setattr("erdle.ocr.IS_WINDOWS", True)
    assert TesseractRecogniser.bundled_binary() is None

    monkeypatch.setattr("erdle.ocr.IS_WINDOWS", False)
    assert TesseractRecogniser.bundled_binary() is not None


def test_an_exe_is_accepted_on_windows(tmp_path, monkeypatch):
    import sys

    from erdle.ocr import TesseractRecogniser

    (tmp_path / "tesseract").mkdir()
    exe = tmp_path / "tesseract" / "tesseract.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr("erdle.ocr.IS_WINDOWS", True)
    assert TesseractRecogniser.bundled_binary() == exe


# --- the config must survive pytesseract's own parsing ----------------------


def test_the_tesseract_config_splits_on_both_platforms():
    """Regression: a lone apostrophe in the whitelist crashed the run.

    pytesseract does `shlex.split(config, posix=system() != "Windows")`.
    The old code added `'` to the whitelist on Windows only, on the
    assumption that non-posix splitting takes it literally. It does not --
    non-posix shlex still requires quotes to balance -- so *both* modes
    raise, and the failure landed in the middle of a boss fight rather
    than at startup.
    """
    import shlex

    from erdle.ocr import TesseractRecogniser

    config = TesseractRecogniser()._config
    for posix in (True, False):
        shlex.split(config, posix=posix)  # must not raise


def test_the_whitelist_holds_no_quote_characters():
    from erdle.ocr import TesseractRecogniser

    config = TesseractRecogniser()._config
    assert "'" not in config
    assert '"' not in config


def test_an_unsplittable_config_is_rejected_at_construction():
    """Fail loudly at startup, not fifteen polls into a fight."""
    import pytest

    from erdle.ocr import TesseractRecogniser

    with pytest.raises(ValueError, match="not shell-splittable"):
        TesseractRecogniser(config="--psm 7 -c whitelist=abc'")


def test_names_with_apostrophes_still_match_without_one():
    """Dropping `'` from the alphabet costs nothing.

    `normalise` strips punctuation before comparing, so the three bosses
    with apostrophes resolve from a reading that has none.
    """
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.matching import normalise

    db = BossDatabase.load(default_data_path())
    for name in ("Commander O'Neil", "Fia's Champion", "Night's Cavalry"):
        entry = next(e for e in db if e.name == name)
        assert normalise(name.replace("'", "")) == normalise(entry.name)
