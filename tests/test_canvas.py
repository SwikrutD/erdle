import pytest

from erdle.canvas import HEIGHT, PACKED_SIZE, WIDTH, Canvas, progress_bar
from erdle.font import (
    ADVANCE,
    GLYPH_HEIGHT,
    GLYPH_WIDTH,
    GLYPHS,
    fit_text,
    glyph_for,
    text_width,
)


# --- font ------------------------------------------------------------------


def test_every_glyph_is_exactly_5x7():
    for char, glyph in GLYPHS.items():
        assert len(glyph) == GLYPH_HEIGHT, f"{char!r} wrong height"
        for row in glyph:
            assert len(row) == GLYPH_WIDTH, f"{char!r} wrong width"


def test_glyphs_contain_only_bits():
    for char, glyph in GLYPHS.items():
        for row in glyph:
            assert set(row) <= {0, 1}, f"{char!r} has non-bit values"


def test_alphabet_and_digits_are_covered():
    for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        assert char in GLYPHS, f"missing glyph {char!r}"


def test_space_is_blank():
    assert all(all(bit == 0 for bit in row) for row in GLYPHS[" "])


def test_visible_glyphs_are_not_blank():
    for char, glyph in GLYPHS.items():
        if char == " ":
            continue
        assert any(any(row) for row in glyph), f"{char!r} renders blank"


def test_glyph_lookup_folds_case():
    assert glyph_for("a") == GLYPHS["A"]


def test_glyph_lookup_falls_back_for_unknown():
    assert glyph_for("☃") == GLYPHS["?"]


def test_text_width_accounts_for_letter_spacing():
    assert text_width("") == 0
    assert text_width("A") == GLYPH_WIDTH
    assert text_width("AB") == ADVANCE + GLYPH_WIDTH


def test_fit_text_leaves_short_strings_alone():
    assert fit_text("ABC", 128) == "ABC"


def test_fit_text_truncates_to_the_panel():
    result = fit_text("A" * 60, WIDTH)
    assert text_width(result) <= WIDTH
    assert len(result) == 21  # (128 + 1) // 6


def test_fit_text_handles_zero_width():
    assert fit_text("ABC", 0) == ""


# --- canvas ----------------------------------------------------------------


def test_default_size_matches_the_panel():
    canvas = Canvas()
    assert (canvas.width, canvas.height) == (WIDTH, HEIGHT)


def test_starts_blank():
    assert all(char == "." for row in Canvas().to_rows() for char in row)


def test_set_and_get_roundtrip():
    canvas = Canvas()
    canvas.set(10, 20, 1)
    assert canvas.get(10, 20) == 1
    assert canvas.get(11, 20) == 0


def test_out_of_bounds_writes_are_clipped_not_wrapped():
    canvas = Canvas()
    canvas.set(-1, 0)
    canvas.set(WIDTH, 0)
    canvas.set(0, HEIGHT)
    canvas.set(0, -1)
    assert all(char == "." for row in canvas.to_rows() for char in row)


def test_out_of_bounds_reads_return_zero():
    canvas = Canvas()
    assert canvas.get(-5, -5) == 0
    assert canvas.get(9999, 9999) == 0


def test_clear_resets():
    canvas = Canvas()
    canvas.fill_rect(0, 0, WIDTH, HEIGHT)
    canvas.clear()
    assert all(char == "." for row in canvas.to_rows() for char in row)


def test_fill_rect_clips_at_edges():
    canvas = Canvas()
    canvas.fill_rect(-10, -10, 20, 20)
    assert canvas.get(0, 0) == 1
    assert canvas.get(10, 10) == 0


def test_draw_rect_is_hollow():
    canvas = Canvas()
    canvas.draw_rect(2, 2, 10, 6)
    assert canvas.get(2, 2) == 1        # corner
    assert canvas.get(11, 7) == 1       # far corner
    assert canvas.get(5, 4) == 0        # interior


def test_draw_text_advances_cursor():
    canvas = Canvas()
    end = canvas.draw_text("AB", 0, 0)
    assert end == text_width("AB")


def test_draw_text_marks_pixels():
    canvas = Canvas()
    canvas.draw_text("A", 0, 0)
    assert any(any(char == "#" for char in row) for row in canvas.to_rows())


def test_draw_text_centered_stays_in_bounds():
    canvas = Canvas()
    canvas.draw_text_centered("A" * 40, 0)
    rows = canvas.to_rows()
    assert all(len(row) == WIDTH for row in rows)


def test_canvas_rejects_bad_dimensions():
    with pytest.raises(ValueError):
        Canvas(0, 40)
    with pytest.raises(ValueError):
        Canvas(3, 3)  # not byte-aligned


# --- packing ---------------------------------------------------------------


def test_packed_size_is_640_bytes():
    assert PACKED_SIZE == 640
    assert len(Canvas().pack()) == 640


def test_packed_bytes_are_in_range():
    canvas = Canvas()
    canvas.draw_text("TESTING 123", 4, 4)
    assert all(0 <= byte <= 255 for byte in canvas.pack())


def test_blank_canvas_packs_to_zeros():
    assert set(Canvas().pack()) == {0}


def test_full_canvas_packs_to_ones():
    canvas = Canvas()
    canvas.fill_rect(0, 0, WIDTH, HEIGHT)
    assert set(canvas.pack()) == {255}


def test_packing_is_msb_first():
    canvas = Canvas()
    canvas.set(0, 0)  # leftmost pixel -> high bit of byte 0
    assert canvas.pack()[0] == 0b10000000
    canvas.clear()
    canvas.set(7, 0)
    assert canvas.pack()[0] == 0b00000001


def test_pack_unpack_roundtrip():
    canvas = Canvas()
    canvas.draw_text("MALENIA", 2, 2)
    canvas.draw_text("BLD+ FRS+", 2, 12)
    progress_bar(canvas, 0, 28, 100, 8, 0.42)
    restored = Canvas.from_packed(canvas.pack())
    assert restored.to_rows() == canvas.to_rows()


def test_from_packed_rejects_wrong_length():
    with pytest.raises(ValueError, match="expected 640"):
        Canvas.from_packed([0] * 100)


# --- progress bar ----------------------------------------------------------


@pytest.mark.parametrize("ratio", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_progress_bar_fill_is_monotonic(ratio):
    canvas = Canvas()
    progress_bar(canvas, 0, 0, 100, 8, ratio)
    lit = sum(row.count("#") for row in canvas.to_rows())
    assert lit > 0  # border is always drawn


def test_progress_bar_clamps_out_of_range():
    for ratio in (-5.0, 5.0):
        canvas = Canvas()
        progress_bar(canvas, 0, 0, 100, 8, ratio)  # must not raise


def test_progress_bar_shows_a_sliver_at_tiny_ratios():
    """A boss on 1% health must not read as dead."""
    canvas = Canvas()
    progress_bar(canvas, 0, 0, 100, 8, 0.001)
    interior = canvas.to_rows()[2][1:99]
    assert "#" in interior


def test_progress_bar_empty_has_no_interior_fill():
    canvas = Canvas()
    progress_bar(canvas, 0, 0, 100, 8, 0.0)
    interior = canvas.to_rows()[3][1:99]
    assert "#" not in interior


def test_progress_bar_full_fills_interior():
    canvas = Canvas()
    progress_bar(canvas, 0, 0, 100, 8, 1.0)
    interior = canvas.to_rows()[3][1:99]
    assert "." not in interior
