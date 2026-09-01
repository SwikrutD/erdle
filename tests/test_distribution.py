"""Config, self-calibration and autostart.

These matter specifically because the app is being handed to other
people. The developer's machine is calibrated and has a console; theirs
has neither.
"""

import json

import pytest

from erdle.autocal import AutoCalibrator, parse_suggestion, strip_regions
from erdle.config import Config, config_dir, config_path
from erdle.detect import make_test_frame
from erdle.geometry import BOSS_BAR, BOSS_NAME, HUD_STRIP, FractionalRect

REAL_BAR = FractionalRect(0.2427, 0.8028, 0.7573, 0.8120)


# --- config ----------------------------------------------------------------


def test_defaults_are_the_shipped_regions():
    config = Config()
    assert config.boss_bar == BOSS_BAR
    assert config.boss_name == BOSS_NAME
    assert config.hud_strip == HUD_STRIP
    assert not config.calibrated


def test_round_trips_through_a_file(tmp_path):
    path = tmp_path / "config.json"
    config = Config(path=path)
    config.apply_regions(
        FractionalRect(0.1, 0.7, 0.9, 0.72),
        FractionalRect(0.1, 0.66, 0.9, 0.69),
        FractionalRect(0.05, 0.64, 0.95, 0.74),
        resolution="3440x1440",
    )
    config.fps = 20.0
    config.save()

    loaded = Config.load(path)
    assert loaded.calibrated
    assert loaded.calibrated_for == "3440x1440"
    assert loaded.boss_bar.top == pytest.approx(0.7)
    assert loaded.fps == pytest.approx(20.0)


def test_missing_file_gives_defaults(tmp_path):
    config = Config.load(tmp_path / "nope.json")
    assert not config.calibrated
    assert config.boss_bar == BOSS_BAR


def test_a_corrupt_file_does_not_stop_startup(tmp_path):
    """There is no console to report a bad config to."""
    path = tmp_path / "config.json"
    path.write_text("{{{ not json", encoding="utf-8")
    assert Config.load(path).boss_bar == BOSS_BAR


def test_partial_regions_are_rejected_wholesale(tmp_path):
    """A calibrated bar with a default name plate is worse than neither."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"regions": {"boss_bar": {"left": 0.1, "top": 0.7,
                                             "right": 0.9, "bottom": 0.72}}}),
        encoding="utf-8",
    )
    config = Config.load(path)
    assert config.boss_bar == BOSS_BAR


def test_nonsense_region_values_are_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"regions": {
            "boss_bar": {"left": 0.9, "top": 0.7, "right": 0.1, "bottom": 0.72},
            "boss_name": {"left": 0.1, "top": 0.6, "right": 0.9, "bottom": 0.65},
            "hud_strip": {"left": 0.0, "top": 0.5, "right": 1.0, "bottom": 0.8},
        }}),
        encoding="utf-8",
    )
    assert Config.load(path).boss_bar == BOSS_BAR


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "config.json"
    config = Config(path=path)
    for _ in range(10):
        config.save()
        json.loads(path.read_text(encoding="utf-8"))
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_reset_marks_it_uncalibrated():
    config = Config()
    config.apply_regions(REAL_BAR, BOSS_NAME, HUD_STRIP, resolution="x")
    assert config.calibrated
    config.reset_regions()
    assert not config.calibrated
    assert config.boss_bar == BOSS_BAR


def test_config_lives_in_a_writable_user_directory():
    """A frozen build may sit in Program Files, which is not writable."""
    directory = str(config_dir()).lower()
    assert "program files" not in directory
    assert config_path().name == "config.json"


# --- self-calibration ------------------------------------------------------


def test_parses_its_own_suggestion_output():
    from erdle.calibrate import find_bar, suggest_regions

    frame = make_test_frame(
        1920, 1080, bar_fill=1.0, bar_region=REAL_BAR,
        background=(153, 115, 69), health_colour=(150, 40, 40),
    )
    parsed = parse_suggestion(suggest_regions(find_bar(frame)))
    assert parsed is not None
    bar, name, strip = parsed
    assert bar.top == pytest.approx(REAL_BAR.top, abs=0.01)
    assert strip.top <= name.top and strip.bottom >= bar.bottom


def test_rejects_a_suggestion_whose_strip_does_not_contain_the_bar():
    text = (
        "BOSS_BAR = FractionalRect(left=0.2, top=0.8, right=0.8, bottom=0.82)\n"
        "BOSS_NAME = FractionalRect(left=0.2, top=0.76, right=0.8, bottom=0.79)\n"
        "HUD_STRIP = FractionalRect(left=0.3, top=0.9, right=0.7, bottom=0.95)\n"
    )
    assert parse_suggestion(text) is None


def test_rejects_garbage_text():
    assert parse_suggestion("this is not python") is None
    assert parse_suggestion("BOSS_BAR = 5") is None


def test_calibrator_finds_and_saves_regions(tmp_path):
    config = Config(path=tmp_path / "config.json")
    calibrator = AutoCalibrator()
    frame = make_test_frame(
        2560, 1440, bar_fill=1.0, bar_region=REAL_BAR,
        background=(96, 120, 52), health_colour=(150, 40, 40),
    )
    assert calibrator.attempt(frame, config, now=1.0)
    assert config.calibrated
    assert config.calibrated_for == "2560x1440"
    assert config.boss_bar.top == pytest.approx(REAL_BAR.top, abs=0.01)


def test_calibrator_reports_failure_without_a_bar():
    config = Config()
    calibrator = AutoCalibrator()
    # Deliberately off-aspect (21:9). A 16:9 frame now adopts the shipped
    # regions without sweeping, so it could never exercise the sweep.
    frame = make_test_frame(1720, 720, bar_fill=None)
    assert not calibrator.attempt(frame, config, now=1.0)
    assert not config.calibrated


def test_calibrator_does_not_run_when_already_calibrated():
    calibrator = AutoCalibrator()
    assert not calibrator.should_attempt(100.0, already_calibrated=True)


def test_calibrator_is_rate_limited():
    calibrator = AutoCalibrator(interval=20.0)
    assert calibrator.should_attempt(0.0, already_calibrated=False)
    # Deliberately off-aspect (21:9). A 16:9 frame now adopts the shipped
    # regions without sweeping, so it could never exercise the sweep.
    calibrator.attempt(make_test_frame(860, 360, bar_fill=None), Config(), 0.0)
    assert not calibrator.should_attempt(5.0, already_calibrated=False)
    assert calibrator.should_attempt(25.0, already_calibrated=False)


def test_calibrator_gives_up_eventually():
    """Otherwise every idle machine scans forever."""
    calibrator = AutoCalibrator(interval=0.0, max_attempts=3)
    config = Config()
    # Deliberately off-aspect (21:9). A 16:9 frame now adopts the shipped
    # regions without sweeping, so it could never exercise the sweep.
    blank = make_test_frame(860, 360, bar_fill=None)
    for i in range(10):
        if calibrator.should_attempt(float(i), already_calibrated=False):
            calibrator.attempt(blank, config, float(i))
    assert calibrator.attempts == 3
    assert not calibrator.should_attempt(999.0, already_calibrated=False)


def test_calibrator_stops_after_success():
    calibrator = AutoCalibrator(interval=0.0)
    config = Config()
    frame = make_test_frame(
        1920, 1080, bar_fill=1.0, bar_region=REAL_BAR,
        background=(96, 120, 52), health_colour=(150, 40, 40),
    )
    assert calibrator.attempt(frame, config, 0.0)
    assert not calibrator.should_attempt(999.0, already_calibrated=False)


@pytest.mark.parametrize(
    "width,height",
    [(1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160), (1920, 1200)],
)
def test_calibration_works_on_any_display_shape(width, height):
    """The reason this exists: shipped regions only suit one aspect ratio."""
    config = Config()
    frame = make_test_frame(
        width, height, bar_fill=1.0, bar_region=REAL_BAR,
        background=(120, 70, 55), health_colour=(150, 40, 40),
    )
    assert AutoCalibrator().attempt(frame, config, 0.0), f"{width}x{height}"
    assert config.calibrated


def test_strip_regions_are_inside_the_strip():
    config = Config()
    bar, name, band = strip_regions(config)
    for region in (bar, name, band):
        assert 0.0 <= region.left < region.right <= 1.0
        assert 0.0 <= region.top < region.bottom <= 1.0


def test_strip_regions_follow_calibration():
    config = Config()
    before = strip_regions(config)
    config.apply_regions(
        FractionalRect(0.1, 0.60, 0.9, 0.62),
        FractionalRect(0.1, 0.55, 0.9, 0.59),
        FractionalRect(0.05, 0.53, 0.95, 0.64),
    )
    assert strip_regions(config) != before


# --- autostart -------------------------------------------------------------


def test_launch_target_avoids_a_console_window():
    from erdle.autostart import launch_target

    executable, _ = launch_target()
    assert executable, "no launch target"
    # On Windows from source this should prefer pythonw.exe; elsewhere it
    # just needs to be a real path rather than empty.
    assert "python" in executable.lower() or executable.endswith(".exe")


def test_autostart_is_a_noop_off_windows(monkeypatch):
    from erdle import autostart

    monkeypatch.setattr(autostart.os, "name", "posix")
    ok, message = autostart.set_autostart(True)
    assert not ok
    assert "Windows" in message


# --- both entrypoints agree ------------------------------------------------


def test_run_and_tray_read_the_same_config():
    """A calibration done in the tray must apply to run.py as well."""
    import run
    import tray

    source = open(run.__file__, encoding="utf-8").read()
    assert "Config.load()" in source
    assert "strip_regions" in source

    tray_source = open(tray.__file__, encoding="utf-8").read()
    assert "Config.load()" in tray_source
    assert "strip_regions" in tray_source


def test_tray_is_the_windowless_entrypoint():
    import tray

    source = open(tray.__file__, encoding="utf-8").read()
    assert "pystray" in source
    assert "daemon=True" in source, "capture must not block the tray thread"


def test_spec_builds_without_a_console():
    from pathlib import Path

    spec = Path(__file__).resolve().parent.parent / "erdle.spec"
    text = spec.read_text(encoding="utf-8")
    assert "console=False" in text, "a console window defeats the purpose"
    assert "tray.py" in text, "must bundle the tray entrypoint, not run.py"
    assert "data/bosses.json" in text, "boss data must be bundled"


# --- calibration must move the search band too -----------------------------
# Regression: strip_regions returned only (bar, name), so the name band
# kept the module default. On any display needing calibration the band
# pointed at the wrong rows -- the exact users calibration exists for.


def test_the_band_follows_calibration():
    from erdle.autocal import strip_regions
    from erdle.config import Config
    from erdle.geometry import FractionalRect

    config = Config()
    config.apply_regions(
        FractionalRect(0.24, 0.700, 0.76, 0.716),
        FractionalRect(0.24, 0.660, 0.52, 0.696),
        FractionalRect(0.20, 0.650, 0.80, 0.730),
        resolution="2560x1440",
    )
    width, height = 2560, 1440
    strip = config.hud_strip.resolve(width, height)
    _, _, band = strip_regions(config)

    inside = band.resolve(strip.width, strip.height)
    top, bottom = strip.top + inside.top, strip.top + inside.bottom
    plate = config.boss_name.resolve(width, height)
    assert top <= plate.top and bottom >= plate.bottom, (
        f"band {top}..{bottom} misses the calibrated plate "
        f"{plate.top}..{plate.bottom}"
    )


def test_the_band_still_works_uncalibrated():
    from erdle.autocal import strip_regions
    from erdle.config import Config

    config = Config()
    for width, height in [(1920, 1080), (2560, 1440), (3840, 2160)]:
        strip = config.hud_strip.resolve(width, height)
        _, _, band = strip_regions(config)
        inside = band.resolve(strip.width, strip.height)
        plate = config.boss_name.resolve(width, height)
        assert strip.top + inside.top <= plate.top, (width, height)
        assert strip.top + inside.bottom >= plate.bottom, (width, height)


def test_strip_regions_returns_three_regions():
    from erdle.autocal import strip_regions
    from erdle.config import Config

    assert len(strip_regions(Config())) == 3


def test_both_entrypoints_pass_the_band_through():
    """A band left at the default silently reads the wrong rows."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("run.py", "tray.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "strip_regions" in source, name
        assert "name_band=" in source, f"{name} does not pass the band"


def test_the_band_stays_inside_the_captured_strip():
    """Otherwise the crop would fall outside the grabbed pixels."""
    from erdle.autocal import band_for
    from erdle.config import Config

    config = Config()
    band = band_for(config)
    strip = config.hud_strip
    assert strip.left <= band.left and strip.right >= band.right
    assert strip.top <= band.top and strip.bottom >= band.bottom


# --- the bundle -------------------------------------------------------------


def test_the_spec_bundles_whatever_is_in_vendor():
    """Collected by walking, not by naming files.

    Tesseract's DLL dependency list changes between releases, and a
    missing one fails at run time with a dialog that names no cause.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    namespace = {"__file__": str(root / "erdle.spec")}
    source = (root / "erdle.spec").read_text(encoding="utf-8")
    exec(source.split("a = Analysis(")[0], namespace)

    items = namespace["vendored_tesseract"]()
    vendor = root / "vendor" / "tesseract"
    if not vendor.is_dir():
        assert items == []
        return

    on_disk = {p for p in vendor.rglob("*") if p.is_file()}
    assert len(items) == len(on_disk), "not every vendored file is bundled"
    assert all(target.startswith("tesseract") for _, target in items)


def test_the_spec_still_bundles_data_and_icons():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "erdle.spec").read_text(
        encoding="utf-8"
    )
    for required in ("data/bosses.json", "assets/tray-active.png",
                     "assets/tray-error.png", "TESSERACT"):
        assert required in source, required


def test_licences_ship_with_the_source():
    """Bundling third-party binaries requires carrying their notices."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert (root / "LICENSE").exists()

    notices = (root / "THIRD_PARTY.md").read_text(encoding="utf-8")
    for required in ("Tesseract", "Apache License 2.0", "Leptonica"):
        assert required in notices, required


# --- what must not ship -----------------------------------------------------


def test_regulation_bin_is_ignored():
    """It is FromSoftware's file, not ours.

    It sat in the project root for the cross-check and would have gone
    public on the first push. Reading a local copy is fine; redistributing
    one is not.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # Comment lines only, not `.split()` on the whole file: the comment
    # explaining the rule also contains the word, so the loose version
    # passed with the rule itself deleted.
    rules = [line.strip()
             for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert "regulation.bin" in rules


def test_the_spec_ships_no_game_file():
    """Nothing extracted from the game may reach the bundle."""
    from pathlib import Path

    spec = (Path(__file__).resolve().parent.parent
            / "erdle.spec").read_text(encoding="utf-8")
    for banned in ("regulation.bin", "params/", ".param"):
        assert banned not in spec, banned


def test_the_seeded_atlas_is_bundled():
    """A shipped atlas is what makes a new user's first fight resolve.

    The file existed but was missing from the spec's `datas`, so it was
    never bundled: every install started with a zero-character alphabet
    and read every plate through Tesseract until it had taught itself.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = (root / "erdle.spec").read_text(encoding="utf-8")
    assert "data/glyphs.json" in spec
    # The name has to reach `datas`, not merely be defined above it.
    # Checking for the string alone passed with the list entry removed,
    # because the helper that builds it mentions the path in its body.
    assert "] + ATLAS + TESSERACT," in spec


def test_the_shipped_atlas_is_not_empty():
    from erdle.glyphs import GlyphAtlas, default_atlas_path

    atlas = GlyphAtlas.load(default_atlas_path())
    assert len(atlas) > 0, "run tools/seed_atlas.py"


def test_research_inputs_are_not_in_the_bundled_data_directory():
    """`data/` is what the exe ships. Sources belong under tools/.

    A 65 KB spreadsheet in the bundle that nothing reads at run time is
    both dead weight and a question waiting to be asked.
    """
    from pathlib import Path

    data = Path(__file__).resolve().parent.parent / "data"
    stray = [p.name for p in data.iterdir()
             if p.suffix.lower() in (".xlsx", ".xls", ".csv")]
    assert not stray, f"move these to tools/sources: {stray}"


def test_the_licence_notices_are_bundled():
    """Apache-2.0 s4 and LGPL both require notices to travel with the binary.

    A user who downloads only `ERDLE.exe` -- which is the whole
    distribution story -- has to receive the attribution too.
    """
    from pathlib import Path

    spec = (Path(__file__).resolve().parent.parent
            / "erdle.spec").read_text(encoding="utf-8")
    assert "('LICENSE', '.')" in spec
    assert "('THIRD_PARTY.md', '.')" in spec


def test_third_party_names_everything_bundled():
    """The notice has to match what the spec actually ships."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    notice = (root / "THIRD_PARTY.md").read_text(encoding="utf-8").lower()
    for name in ("tesseract", "leptonica", "pystray", "pillow", "mss",
                 "pytesseract", "pyinstaller" if False else "python"):
        assert name in notice, name


def test_seed_atlas_refuses_an_atlas_that_cannot_read():
    """A character count says nothing about correctness.

    An atlas of 46 characters was seeded that read neither reference
    plate -- 0.40 coverage on one, 0.00 on the other -- because a
    filename with a trailing digit made the label one longer than the
    plate and shifted every subsequent glyph onto the wrong letter. The
    count went up the whole time, so nothing looked wrong.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import seed_atlas

    from erdle.glyphs import GlyphAtlas

    assert seed_atlas.can_read_the_reference_plates(GlyphAtlas())


def test_the_reference_plates_are_present():
    """They are the only end-to-end check on atlas quality.

    And they have to be captures of the game itself. A YouTube frame was
    one of them for a while; it rejected genuine screenshots, because
    compression had softened the letterforms until they no longer
    matched what the game draws.
    """
    from pathlib import Path

    plates = Path(__file__).resolve().parent / "plates"
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import seed_atlas

    for filename in seed_atlas.PLATES:
        assert (plates / filename).exists(), filename
