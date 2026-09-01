"""The spreadsheet importer.

The source columns are *absorption* and *resistance*, both inverted
relative to what the panel shows: high absorption is a reason not to bring
that damage type. Getting the direction wrong would turn every resistance
into a recommended weakness, so it is pinned from both ends.
"""

import pytest

from erdle.bossdb import (
    IMMUNE,
    NORMAL,
    RESISTANT,
    WEAK,
    BossDatabase,
    BossDataError,
    default_data_path,
)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def test_absorption_labels_are_boss_relative():
    from tools.import_xlsx import ABSORPTION

    assert ABSORPTION["WEAK AGAINST"] == WEAK
    assert ABSORPTION["NEUTRAL AGAINST"] == NORMAL
    assert ABSORPTION["STRONG AGAINST"] == RESISTANT


def test_the_sources_typo_is_handled():
    """The sheet contains 'VERY STONG AGAINST' three times."""
    from tools.import_xlsx import ABSORPTION

    assert ABSORPTION["VERY STONG AGAINST"] == ABSORPTION["VERY STRONG AGAINST"]


def test_resistance_labels_are_inverted():
    from tools.import_xlsx import RESISTANCE

    assert RESISTANCE["IMMUNE"] == IMMUNE
    assert RESISTANCE["HIGH"] == RESISTANT
    assert RESISTANCE["LOW"] == WEAK


def test_medium_is_the_default_not_a_mild_resistance():
    """MEDIUM is the modal value in every resistance column."""
    from tools.import_xlsx import RESISTANCE

    assert RESISTANCE["MEDIUM"] == NORMAL


def test_severity_orders_within_a_bucket():
    from tools.import_xlsx import SEVERITY

    assert SEVERITY["STRONG AGAINST"] > SEVERITY["STRONGER AGAINST"]
    assert SEVERITY["STRONGER AGAINST"] > SEVERITY["VERY STRONG AGAINST"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TREE SENTINEL", "Tree Sentinel"),
        ("MARGIT, THE FELL OMEN", "Margit, the Fell Omen"),
        ("COMMANDER O'NEIL", "Commander O'Neil"),
        ("FIA'S CHAMPION", "Fia's Champion"),
        ("DEMI-HUMAN CHIEF", "Demi-Human Chief"),
        ("ASTEL, NATURALBORN OF THE VOID", "Astel, Naturalborn of the Void"),
    ],
)
def test_title_casing_matches_the_games_rendering(raw, expected):
    from tools.import_xlsx import clean_name

    assert clean_name(raw)[0] == expected


def test_sheet_typos_are_corrected():
    """The stored name is matched against OCR output, so a typo costs
    similarity on every read."""
    from tools.import_xlsx import clean_name

    assert clean_name("ADAN, THIEF OF HRE")[0] == "Adan, Thief of Fire"
    assert clean_name("NIGHT'S CALVALRY")[0] == "Night's Cavalry"


def test_slash_rows_become_a_name_and_an_alias():
    from tools.import_xlsx import clean_name

    name, aliases = clean_name("MALENIA, BLADE OF MIQUELLA/GODESS OF ROT")
    assert name == "Malenia, Blade of Miquella"
    assert "Malenia, Goddess of Rot" in aliases


def test_parenthetical_qualifiers_are_stripped():
    from tools.import_xlsx import clean_name

    assert clean_name("CRUCIBLE KNIGHT (CAPITAL)")[0] == "Crucible Knight"
    assert clean_name("CRYSTALIAN (ALL)")[0] == "Crystalian"


def test_duplicate_rows_merge_cautiously():
    """Two rows for one enemy must never combine into a stronger
    recommendation than either supports."""
    from tools.import_xlsx import more_cautious

    assert more_cautious(WEAK, RESISTANT) == RESISTANT
    assert more_cautious(NORMAL, IMMUNE) == IMMUNE


# --- what shipped ----------------------------------------------------------


def test_the_roster_is_complete(database):
    assert len(database) >= 100


def test_no_alias_shadows_a_real_name(database):
    """An exact alias hit scores 1.0, so a shadowing alias wins outright.
    'Beast Clergyman' was an alias on Maliketh until the sheet gave it an
    entry of its own."""
    names = {e.name.strip().lower(): e.key for e in database}
    for entry in database:
        for alias in entry.aliases:
            owner = names.get(alias.strip().lower())
            assert owner in (None, entry.key), f"{entry.key}: {alias!r}"


def test_the_schema_rejects_a_shadowing_alias():
    payload = {
        "bosses": {
            "a": {"name": "Beast Clergyman"},
            "b": {"name": "Maliketh", "aliases": ["Beast Clergyman"]},
        }
    }
    with pytest.raises(BossDataError, match="shadows"):
        BossDatabase.from_dict(payload)


def test_phase_pairs_resolve_separately(database):
    """Beast Clergyman and Maliketh are one fight with two names."""
    from erdle.matching import BossNameMatcher

    matcher = BossNameMatcher.from_entries(database)
    assert matcher.match("Beast Clergyman").key == "beast_clergyman"
    assert matcher.match("Maliketh, the Black Blade").key == "maliketh"


def test_every_name_still_matches_itself(database):
    """With 106 entries the chance of two names colliding is far higher."""
    from erdle.matching import BossNameMatcher

    matcher = BossNameMatcher.from_entries(database, threshold=0.62, min_margin=0.03)
    for entry in database:
        result = matcher.match(entry.name)
        assert result is not None, entry.name
        assert result.key == entry.key, f"{entry.name} -> {result.key}"


def test_hand_written_extras_survived(database):
    """The sheet carries no poise values or notes."""
    assert len([e for e in database if e.poise is not None]) >= 15
    # 60 was a hand-written guess. NpcParam says 80, and the game wins.
    assert database.require("tree_sentinel").poise == 80
    assert database.require("erdtree_burial_watchdog").note


def test_every_entry_fits_the_panel(database):
    from erdle.font import text_width
    from erdle.render import advice_row, display_name, status_row

    for entry in database:
        for row in (display_name(entry), status_row(entry), advice_row(entry)):
            assert text_width(row) <= 128, f"{entry.key}: {row!r}"


# --- atlas coverage target --------------------------------------------------


def test_expected_alphabet_matches_the_shipped_roster():
    """The finish line has to be reachable.

    `tools/atlas.py` measured coverage against `string.ascii_letters`,
    which demanded I, J, X, Y and j -- none of which appear in any boss
    name -- so the count could never reach 100% and there was no way to
    tell when the atlas was done. It also ignored `&`, which "Nox
    Swordstress & Nox Priest" needs.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import atlas as atlas_tool

    from erdle.bossdb import BossDatabase, default_data_path

    database = BossDatabase.load(default_data_path())
    used = set()
    for entry in database:
        used |= set(entry.name)
    used.discard(" ")

    expected = atlas_tool.expected_alphabet()
    assert expected == used, {
        "demanded but unused": "".join(sorted(expected - used)),
        "used but not demanded": "".join(sorted(used - expected)),
    }


def test_the_alphabet_covers_every_punctuation_mark_in_use():
    """Replaces an older test that asserted the roster needs `&`.

    It did, once -- but only because of two combined Nox entries that do
    not correspond to anything the game draws. With those split into one
    boss per health bar, no name contains an ampersand, and the atlas no
    longer has to learn a character that appears nowhere.

    The general form is the useful one: whatever punctuation the names do
    use has to be learnable, and `GlyphAtlas` must not reject it for
    being short.
    """
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.glyphs import GlyphAtlas

    database = BossDatabase.load(default_data_path())
    marks = {c for entry in database for c in entry.name
             if not c.isalnum() and not c.isspace()}
    assert marks, "the roster has no punctuation at all?"
    for mark in marks:
        assert mark in GlyphAtlas.SHORT_BY_NATURE, mark


# --- roster completeness ----------------------------------------------------


def test_the_misspelled_names_are_corrected():
    """Both carried full stats, so the typo cost two working bosses.

    OCR reads what the game renders; scoring that against a string the
    game never renders throws the match away.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    names = {e.name for e in db}
    assert "Dragonkin Soldier of Nokstella" in names
    assert "Demi-Human Queen Gilika" in names
    assert "Dragonkin Soldier of Nokestella" not in names
    assert "Demi-Human Queen Gilka" not in names

    # And the stats survived the rename.
    entry = db.require("dragonkin_soldier_of_nokstella")
    assert entry.damage and entry.severity


def test_name_only_entries_carry_no_invented_stats():
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    for entry in db:
        if entry.confidence != "name-only":
            continue
        assert not entry.damage, entry.key
        assert not entry.statuses, entry.key
        assert not entry.severity, entry.key


def test_a_boss_with_no_data_says_so():
    """"No notable weaknesses" would be a claim, and a false one."""
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.overlay import build_content

    # No shipped boss is bare any more -- the Godskin Duo used to be, and
    # now has the Noble's numbers -- so the empty case is constructed.
    from erdle.bossdb import parse_entry

    content = build_content(parse_entry("x", {"name": "Unknown"}))
    assert "no data" in content.headline
    assert "no notable weaknesses" not in content.headline


def test_the_importer_would_recreate_the_extra_bosses():
    """A re-import must not quietly drop them."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import import_xlsx

    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    names = {e.name for e in db} | {a for e in db for a in e.aliases}
    # Two have since been removed on the strength of what they are:
    # Cleanrot Knight Finlay is a spirit ash with no health bar, and the
    # Apostle/Noble pair is a sequential fight that shows each name in
    # turn rather than a combined plate.
    gone = {"Cleanrot Knight Finlay", "Godskin Apostle and Godskin Noble"}
    for name in import_xlsx.EXTRA_BOSSES:
        if name in gone:
            assert name not in names, f"{name} should have been removed"
            continue
        assert name in names, name


def test_no_added_name_shadows_an_existing_one():
    """An exact alias hit scores 1.0, so a shadow wins over the real boss."""
    from erdle.bossdb import BossDatabase, default_data_path
    from erdle.matching import BossNameMatcher

    db = BossDatabase.load(default_data_path())
    matcher = BossNameMatcher.from_entries(db)
    for entry in db:
        result = matcher.match(entry.name)
        assert result is not None and result.key == entry.key, (
            f"{entry.name!r} resolves to "
            f"{result.key if result else None}, not itself"
        )


# --- regulation.bin ---------------------------------------------------------


def _extract_tool():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import extract_regulation

    return extract_regulation


def test_the_regulation_key_is_the_published_one():
    """A wrong key decrypts to noise that looks like a corrupt file."""
    tool = _extract_tool()
    assert len(tool.REGULATION_KEY) == 32
    assert tool.REGULATION_KEY[:2] == bytes([0x99, 0xBF])


def test_dcx_rejects_something_that_is_not_a_container():
    tool = _extract_tool()
    with pytest.raises(ValueError, match="not a DCX"):
        tool.decompress_dcx(b"NOPE" + b"\x00" * 64)


def test_bnd4_rejects_something_that_is_not_an_archive():
    tool = _extract_tool()
    with pytest.raises(ValueError, match="not a BND4"):
        tool.read_bnd4(b"NOPE" + b"\x00" * 64)


def test_an_already_unpacked_file_skips_the_crypto():
    """So a decrypted copy can be passed straight in."""
    import struct

    tool = _extract_tool()
    # A BND4 with zero files is enough to prove the branch.
    header = b"BND4" + b"\x00" * 8 + struct.pack("<i", 0) + b"\x00" * 48
    assert tool.read_bnd4(header) == []


@pytest.mark.skipif(
    not (__import__("pathlib").Path(__file__).resolve().parent.parent
         / "regulation.bin").exists(),
    reason="regulation.bin not present",
)
def test_the_real_file_unpacks_to_params():
    """End to end against the actual game file, when it is available.

    Skipped rather than failed when absent: the file is the user's copy of
    game data and does not belong in the repository.
    """
    from pathlib import Path

    tool = _extract_tool()
    root = Path(__file__).resolve().parent.parent
    files = tool.load(root / "regulation.bin")

    assert len(files) > 100
    names = {name.split("\\")[-1] for name, _ in files}
    assert "NpcParam.param" in names

    blob = next(b for n, b in files if n.endswith("NpcParam.param"))
    info = tool.describe_param(blob)
    assert info["rows"] > 5000
    assert info["row_size"] and info["row_size"] > 100


# --- data from the game itself ---------------------------------------------


def test_regulation_entries_carry_the_row_they_came_from():
    """So a wrong number can be traced back and re-checked."""
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    # Most of these were superseded by the PvE workbook, which carries
    # damage negation rather than the subtler DamageCutRate.
    fromgame = [e for e in db if e.confidence == "regulation"]
    assert len(fromgame) >= 5
    for entry in fromgame:
        # Either a specific row, or a model whose variants are uniform.
        assert entry.note and "NpcParam" in entry.note, entry.key
    # Rennala is the only entry that legitimately has none: her poise
    # field holds -1, a sentinel meaning she cannot be staggered in phase
    # one rather than a threshold. She is now sourced from the workbook,
    # which agrees by writing the same gap.
    for entry in fromgame:
        assert entry.poise is not None, entry.key


def test_known_facts_survive_the_import():
    """Spot checks against things any player knows.

    These are the reason to trust the field offsets at all: they were
    derived from a paramdef, and a paramdef applied at the wrong offset
    still produces confident, plausible, wrong numbers.
    """
    from erdle.bossdb import IMMUNE, WEAK, BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())

    # The Elden Beast shrugs off holy more than anything else.
    beast = db.require("elden_beast")
    assert beast.worst_damage_types(limit=1) == ["holy"]
    assert beast.status("bleed") == IMMUNE

    # Tree spirits burn.
    spirit = db.require("ulcerated_tree_spirit")
    assert spirit.damage_effectiveness("fire") == WEAK

    # Crystalians are stone: no bleed, no rot, and strike beats blades.
    crystal = db.require("crystalian")
    assert crystal.status("bleed") == IMMUNE
    assert crystal.severity["strike"] > crystal.severity["slash"]


def test_severity_keeps_ordering_below_the_bottom_bucket():
    """The Elden Beast takes 0.6x magic and 0.2x holy.

    Collapsing everything under 0.65 into one bucket lost that, and the
    panel named magic as the worst type when holy is three times worse.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    beast = db.require("elden_beast")
    assert beast.severity["holy"] < beast.severity["magic"]


def test_the_game_data_and_the_spreadsheet_mostly_agree():
    """96% agreement is what makes both sources believable.

    If they disagreed wholesale, the likeliest explanation would be that
    the field offsets are wrong -- so this is really a check on the
    extraction, not on the spreadsheet.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    counts = {}
    for entry in db:
        counts[entry.confidence] = counts.get(entry.confidence, 0) + 1
    assert counts.get("regulation", 0) + counts.get("pve-sheet", 0) >= 90
    # Nothing is left on either: every entry now has real numbers.
    assert counts.get("sheet", 0) + counts.get("name-only", 0) == 0


def test_the_field_offsets_fit_inside_a_row():
    """The 736-byte row size is the only check on the offsets.

    A paramdef applied at the wrong offset produces confident, plausible,
    wrong numbers -- so the arithmetic gets an assertion rather than a
    comment.
    """
    tool = _extract_tool()
    assert tool.ROW_SIZE == 736
    for field, offset in tool.FIELDS.items():
        assert 0 <= offset < tool.ROW_SIZE - 3, f"{field} at {offset}"

    # The eight damage multipliers are consecutive 4-byte floats.
    damage = [tool.FIELDS[f] for f in (
        "neutralDamageCutRate", "slashDamageCutRate", "blowDamageCutRate",
        "thrustDamageCutRate", "magicDamageCutRate", "fireDamageCutRate",
        "thunderDamageCutRate", "darkDamageCutRate")]
    assert damage == list(range(damage[0], damage[0] + 32, 4))


def test_model_inferred_entries_only_used_uniform_models():
    """Named variants share a model with their generic counterpart.

    Where every row for that model carries the same profile, the variant's
    numbers are that profile whichever row it actually is -- a proof, not
    a guess. Where the rows differ, nothing was written.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    inferred = [e for e in db
                if e.note and "every variant of this model" in e.note]
    for entry in inferred:
        assert entry.confidence == "regulation"
        assert entry.damage or entry.severity, entry.key

    # The ambiguous ones must have been left alone.
    ambiguous = {"crucible_knight_ordovis", "crystalian_spear",
                 "cleanrot_knight_finlay", "flying_dragon_agheel"}
    for key in ambiguous:
        entry = db.get(key)
        if entry is None:
            continue
        assert "every variant" not in (entry.note or ""), key


def test_poise_is_absent_rather_than_negative():
    """NpcParam uses -1 as a sentinel for "no poise system here".

    Writing it through would have claimed Rennala staggers on contact;
    the schema guard rejected it, which is how it was noticed.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    for entry in db:
        assert entry.poise is None or entry.poise >= 0, entry.key
    assert db.require("rennala").poise is None


# --- the hand-research worksheet -------------------------------------------


def _worksheet_tool():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import import_worksheet

    return import_worksheet


def test_the_worksheet_covers_exactly_what_is_missing():
    """One row per boss that does not yet have the game's own numbers."""
    import csv
    from pathlib import Path

    from erdle.bossdb import BossDatabase, default_data_path

    sheet = (Path(__file__).resolve().parent.parent
             / "tools" / "sources" / "worksheet.csv")
    if not sheet.exists():
        pytest.skip("worksheet not generated")

    rows = list(csv.DictReader(sheet.open(encoding="utf-8-sig")))
    keys = {r["key"] for r in rows}

    # The worksheet is a snapshot of what was missing when it was
    # generated. Every row in it must still name a real boss; bosses that
    # have since been filled in simply drop out of scope.
    db = BossDatabase.load(default_data_path())
    known = {e.key for e in db}
    for key in keys:
        assert key in known, f"worksheet row {key!r} is not a boss"

    still_missing = {e.key for e in db
                     if e.confidence in ("sheet", "name-only")}
    assert still_missing <= keys, sorted(still_missing - keys)
    assert not still_missing, "the worksheet should be empty now"


def test_cells_accept_words_and_multipliers():
    tool = _worksheet_tool()
    from erdle.bossdb import IMMUNE, NORMAL, RESISTANT, WEAK

    assert tool.damage_from("weak", "x")[0] == WEAK
    assert tool.damage_from("1.2", "x")[0] == WEAK
    assert tool.damage_from("1.0", "x")[0] == NORMAL
    assert tool.damage_from("0.8", "x")[0] == RESISTANT
    assert tool.damage_from("", "x") is None

    assert tool.status_from("immune", "x") == IMMUNE
    assert tool.status_from("999", "x") == IMMUNE
    assert tool.status_from("154", "x") == WEAK


def test_a_multiplier_keeps_ordering_a_word_would_lose():
    """0.6 and 0.2 are both "resistant"; only the number ranks them."""
    tool = _worksheet_tool()
    assert tool.damage_from("0.2", "x")[1] < tool.damage_from("0.6", "x")[1]
    assert tool.damage_from("resistant", "x")[1] == tool.damage_from("r", "x")[1]


def test_nonsense_is_rejected_rather_than_guessed():
    tool = _worksheet_tool()
    for bad in ("maybe", "very weak", "-1", "50"):
        with pytest.raises(ValueError):
            tool.damage_from(bad, "line 2 fire")


def test_blank_cells_are_left_alone_not_zeroed():
    """So the worksheet can be filled in over several sittings."""
    tool = _worksheet_tool()
    assert tool.damage_from("   ", "x") is None
    assert tool.status_from("", "x") is None


# --- the PvE stats workbook -------------------------------------------------


def _pve_tool():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import import_pve_stats

    return import_pve_stats


def test_negation_is_a_percentage_not_a_multiplier():
    """A negative negation means the boss takes *more* damage.

    The four-bucket scale had no way to say that and the old spreadsheet
    never recorded it, so every amplified weakness in the game was missing.
    """
    from erdle.bossdb import IMMUNE, NORMAL, RESISTANT, WEAK

    tool = _pve_tool()
    assert tool.bucket_for(-20) == WEAK
    assert tool.bucket_for(0) == NORMAL
    assert tool.bucket_for(40) == RESISTANT
    assert tool.bucket_for(100) == IMMUNE

    # And the ordering survives inside a bucket.
    assert tool.severity_for(-20) > tool.severity_for(-10)
    assert tool.severity_for(20) > tool.severity_for(50)


def test_a_status_can_be_weak_absolutely_or_relatively():
    """Neither test alone is enough.

    Judging only against the boss's own peers calls Godrick weak to
    everything, since all five of his sit at 318. Judging only absolutely
    reported Malenia as having no notable status at all, when frost at 306
    against her poison at 1481 is the whole strategy.
    """
    from erdle.bossdb import IMMUNE, NORMAL, WEAK

    tool = _pve_tool()

    godrick = [318, 318, 318, 318, 318]
    assert all(tool.status_for(v, godrick) == NORMAL for v in godrick)

    malenia = [1481, 1481, 420, 306, 688]
    assert tool.status_for(306, malenia) == WEAK
    assert tool.status_for(1481, malenia) != WEAK

    assert tool.status_for("Immune", malenia) == IMMUNE
    assert tool.status_for(None, malenia) is None


def test_the_workbook_layout_is_checked_before_reading():
    """Poise sits at columns 40-43 next to "Incoming Mult" and "Regen
    Delay". Grabbing the wrong one gave Godrick a poise of 8 instead of
    105, which looks entirely plausible."""
    tool = _pve_tool()
    assert tool.POISE_EFFECTIVE == 42


@pytest.mark.skipif(
    not (__import__("pathlib").Path(__file__).resolve().parent.parent
         / "tools" / "sources"
         / "ER - PvE Health_Defense_DmgNeg_Resistances.xlsx").exists(),
    reason="workbook not present",
)
def test_known_fights_come_out_right():
    """Spot checks a player would catch instantly."""
    from erdle.bossdb import IMMUNE, WEAK, BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())

    # Crystalians are stone: strike beats blades by a mile.
    crystal = db.require("crystalian")
    assert crystal.severity["strike"] > crystal.severity["slash"]
    # Bleed only. Rot reads 530 here against NpcParam's 999 -- different
    # placements of the same enemy carry different resistances, and the
    # workbook describes the one actually standing in the cave.
    assert crystal.status("bleed") == IMMUNE

    # Tree spirits burn -- negation -20, an amplified weakness.
    assert db.require("ulcerated_tree_spirit").damage_effectiveness("fire") == WEAK

    # The Fire Giant does not.
    giant = db.require("fire_giant")
    assert giant.severity["fire"] < giant.severity["slash"]

    # Rot on Radahn and frost on Rykard are the well-known strategies.
    assert db.require("radahn").status("rot") == WEAK
    assert db.require("rykard").status("frost") == WEAK

    # Mohg can be bled despite the title: 290 against 653 for everything
    # else. "Lord of Blood" is flavour, not a resistance.
    assert db.require("mohg").status("bleed") == WEAK


def test_the_relative_rule_never_acts_alone_on_a_flat_boss():
    """It must not manufacture a weakness where the numbers are level.

    Some bosses genuinely are weak to nearly everything -- Battlemage
    Hugues sits at 181-241 across the board, which is a human mage with
    no status defence. That is the absolute test firing, which is correct.
    What would be wrong is the *relative* test firing on a boss whose
    resistances are all the same, and that is what this pins.
    """
    tool = _pve_tool()
    from erdle.bossdb import NORMAL

    flat = [400, 400, 400, 400, 400]
    assert all(tool.status_for(v, flat) == NORMAL for v in flat)

    hugues = [181, 181, 241, 241, 241]
    assert all(tool.status_for(v, hugues) is not None for v in hugues)


def test_an_all_immune_placement_is_not_the_fightable_one():
    """Godfrey has a variant immune to everything -- a cutscene phase.

    Letting it win made him read as immune to four statuses, which is
    both wrong and the kind of wrong a player would never question.
    """
    tool = _pve_tool()
    fightable = {"resistance": {"poison": 367, "rot": 367, "bleed": 601,
                                "frost": 601, "sleep": 601, "madness": "Immune"}}
    cutscene = {"resistance": {k: "Immune" for k in
                               ("poison","rot","bleed","frost","sleep","madness")}}
    assert tool.pick_status_row([cutscene, fightable]) is fightable
    # With nothing else to choose from, the immune row is the answer.
    assert tool.pick_status_row([cutscene]) is cutscene


def test_placements_that_differ_only_by_scaling_take_the_middle():
    """The Bell Bearing Hunter's four placements run 316 to 383.

    The lowest would over-claim weakness; the middle is least wrong.
    """
    tool = _pve_tool()
    rows = [{"resistance": {"poison": v, "rot": v, "bleed": v,
                            "frost": v, "sleep": v, "madness": "Immune"}}
            for v in (316, 329, 337, 383)]
    chosen = tool.pick_status_row(rows)
    assert chosen["resistance"]["poison"] in (329, 337)


def test_damage_survives_when_only_statuses_disagree():
    """Discarding both halves threw away the one that decides a fight."""
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    hunter = db.require("bell_bearing_hunter")
    assert hunter.confidence == "pve-sheet"
    assert hunter.severity, "damage negation was dropped over a status mismatch"
    assert hunter.poise is not None


def test_the_nox_pairs_are_separate_bosses():
    """They fight one at a time, each with its own health bar.

    Reported from play: there is no combined "Nox Swordstress & Nox Monk"
    plate. The workbook agrees -- it lists "Nox Priest [Boss]" and "Nox
    Swordstress [Boss]" as separate rows with separate NpcParam ids -- so
    the combined entries were wrong on both the name and the stats. The
    priest's entry had been given the swordstress's row.

    Same class of error as the Godskin Duo, and the same fix: one entry
    per health bar, and the tracker's phase switch handles the changeover.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    names = {e.name for e in db}
    for name in ("Nox Swordstress", "Nox Priest", "Nox Monk"):
        assert name in names, name
    assert not [n for n in names if "&" in n]


def test_no_boss_name_holds_an_ampersand():
    """It cost a whole fight's worth of atlas coverage for nothing.

    `&` appeared in exactly two names, both of them combined entries that
    should never have existed. Removing them took the alphabet the atlas
    has to learn from 51 characters to 50 -- and the one it dropped was
    the rarest.
    """
    from erdle.bossdb import BossDatabase, default_data_path

    db = BossDatabase.load(default_data_path())
    alphabet = set("".join(e.name for e in db)) - {" "}
    assert "&" not in alphabet
    assert len(alphabet) == 50
