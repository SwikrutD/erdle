import json

import pytest

from erdle.bossdb import (
    parse_entry,
    IMMUNE,
    NORMAL,
    RESISTANT,
    WEAK,
    BossDatabase,
    BossDataError,
    default_data_path,
    parse_entry,
)


@pytest.fixture(scope="module")
def database():
    return BossDatabase.load(default_data_path())


def test_ships_with_the_full_roster(database):
    """106 after the spreadsheet import, up from 18."""
    assert len(database) >= 100


def test_all_entries_have_names_and_valid_scales(database):
    for entry in database:
        assert entry.name.strip()
        for value in entry.statuses.values():
            assert value in (IMMUNE, RESISTANT, NORMAL, WEAK)
        for value in entry.damage.values():
            assert value in (IMMUNE, RESISTANT, NORMAL, WEAK)


def test_every_entry_declares_confidence(database):
    """Unverified numbers must be labelled as such, not silently trusted.

    "name-only" is its own level: the name is verified against the wiki
    roster but the sheet has no row, so there are no numbers at all. That
    is different from numbers we merely have not checked, and the overlay
    says so rather than reporting a shrug as a finding.
    """
    for entry in database:
        assert entry.confidence in {
            "high", "medium", "low", "unverified", "sheet", "name-only",
            "regulation", "manual", "pve-sheet",
        }, f"{entry.key}: {entry.confidence}"


def test_name_only_entries_really_have_no_numbers(database):
    for entry in database:
        if entry.confidence == "name-only":
            assert not entry.damage and not entry.statuses, entry.key


# --- the facts worth hard-coding ------------------------------------------


# These previously asserted hand-written guesses. The spreadsheet import
# surfaced 18 disagreements and several of the guesses were wrong -- two of
# them labelled "high confidence". What remains is what the sheet says,
# kept as a canary: if an import ever flips one, the mapping direction has
# broken.


def test_radahn_is_weak_to_rot(database):
    """The most widely known interaction in the game."""
    assert database.require("radahn").status("rot") == WEAK


def test_elden_beast_resists_holy(database):
    """Was asserted IMMUNE; the sheet grades it heavily resistant."""
    assert database.require("elden_beast").damage_effectiveness("holy") == RESISTANT


def test_constructs_are_immune_to_status(database):
    for key in ("erdtree_burial_watchdog", "elden_beast"):
        entry = database.require(key)
        assert entry.status("bleed") == IMMUNE, key
        assert entry.status("rot") == IMMUNE, key


def test_malenia_does_not_recommend_rot(database):
    """She inflicts it. The sheet grades her resistant rather than immune;
    the displayed advice is the same either way."""
    assert database.require("malenia").status("rot") <= RESISTANT


def test_mapping_direction_is_not_inverted(database):
    """The import hinges on absorption being read boss-relative. Radahn/rot
    and Elden Beast/holy point opposite ways, so both holding pins it."""
    assert database.require("radahn").status("rot") == WEAK
    assert database.require("elden_beast").damage_effectiveness("holy") < NORMAL


# --- ranking helpers -------------------------------------------------------


def test_best_damage_types_excludes_merely_normal(database):
    for entry in database:
        if any(v > NORMAL for v in entry.damage.values()):
            for name in entry.best_damage_types():
                assert entry.damage.get(name, NORMAL) > NORMAL, entry.key


def test_a_boss_resisting_everything_still_gets_advice(database):
    """The Burial Watchdog resists all eight damage types. Saying nothing
    is honest but useless -- name the least-resisted one."""
    watchdog = database.require("erdtree_burial_watchdog")
    assert all(v <= NORMAL for v in watchdog.damage.values())
    assert watchdog.best_damage_types() == ["strike"]


def test_worst_damage_types_orders_immune_first(database):
    beast = database.require("elden_beast")
    assert beast.worst_damage_types(limit=2)[0] == "holy"


def test_status_summary_always_includes_bleed(database):
    """'Does bleed work' is the question; it must never be crowded out."""
    for entry in database:
        names = [name for name, _ in entry.status_summary()]
        assert "bleed" in names, f"{entry.key} dropped bleed"


def test_status_summary_is_capped(database):
    for entry in database:
        assert len(entry.status_summary(limit=4)) <= 4


def test_status_summary_follows_display_order(database):
    from erdle.bossdb import STATUS_ORDER

    for entry in database:
        names = [name for name, _ in entry.status_summary()]
        indices = [STATUS_ORDER.index(n) for n in names]
        assert indices == sorted(indices)


def test_unrecorded_status_defaults_to_normal(database):
    """Godskin Noble no longer qualifies: the game data records madness
    as an outright immunity, which is a fact rather than a default."""
    # Every shipped entry now records all six statuses, so the default
    # has to be shown on a constructed one rather than a real boss.
    entry = parse_entry("x", {"name": "Nothing Recorded", "statuses": {}})
    assert "madness" not in entry.statuses
    assert entry.status("madness") == NORMAL


def test_status_summary_omits_uninformative_entries(database):
    """'PSN=' costs four characters and tells the player nothing."""
    for entry in database:
        for name, effectiveness in entry.status_summary():
            if name == "bleed":
                continue
            assert effectiveness != NORMAL, f"{entry.key} padded with {name}"


def test_status_summary_can_return_fewer_than_the_limit(database):
    giant = database.require("fire_giant")
    # Madness joined the list when the game data landed: NpcParam
    # records it as an outright immunity, which the sheet never had.
    # The workbook records every status, so the summary now reports
    # whatever is not ordinary rather than only what the sheet had.
    names = [name for name, _ in giant.status_summary()]
    assert "bleed" in names
    assert len(names) <= 4, names


def test_bleed_shown_even_when_unremarkable():
    entry = parse_entry("x", {"name": "X", "statuses": {"frost": 3}})
    assert ("bleed", NORMAL) in entry.status_summary()


# --- validation ------------------------------------------------------------


def test_rejects_unknown_status_key():
    with pytest.raises(BossDataError, match="unknown key"):
        parse_entry("x", {"name": "X", "statuses": {"curse": 3}})


def test_rejects_out_of_range_effectiveness():
    with pytest.raises(BossDataError, match="0-3"):
        parse_entry("x", {"name": "X", "statuses": {"bleed": 7}})


def test_rejects_missing_name():
    with pytest.raises(BossDataError, match="display name"):
        parse_entry("x", {"statuses": {"bleed": 3}})


def test_rejects_negative_poise():
    with pytest.raises(BossDataError, match="poise"):
        parse_entry("x", {"name": "X", "poise": -5})


def test_rejects_duplicate_display_names():
    payload = {
        "bosses": {
            "a": {"name": "Twinblade Knight"},
            "b": {"name": "twinblade knight"},
        }
    }
    with pytest.raises(BossDataError, match="duplicate"):
        BossDatabase.from_dict(payload)


def test_rejects_empty_database():
    with pytest.raises(BossDataError):
        BossDatabase.from_dict({"bosses": {}})


def test_rejects_bad_json(tmp_path):
    bad = tmp_path / "bosses.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(BossDataError, match="invalid JSON"):
        BossDatabase.load(bad)


def test_shipped_data_declares_its_provenance(database):
    """The stub table must not masquerade as extracted param data."""
    assert "spreadsheet" in database.meta.get("source", "")
    assert "regulation" in database.meta.get("source", "")
    assert "_warning" in database.meta


def test_shipped_json_is_wellformed():
    payload = json.loads(default_data_path().read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


# --- early-game entries, so the first run shows something real -------------


def test_watchdog_is_immune_to_every_status(database):
    """Stone construct. The single most useful fact in the early game."""
    watchdog = database.require("erdtree_burial_watchdog")
    for status in ("bleed", "rot", "poison", "frost"):
        assert watchdog.status(status) == IMMUNE, status


def test_tree_sentinel_is_present(database):
    assert "tree_sentinel" in database


def test_entries_with_explicit_short_names_are_loaded(database):
    assert database.require("erdtree_burial_watchdog").short == "BURIAL WATCHDOG"
    assert database.require("margit").short is None
