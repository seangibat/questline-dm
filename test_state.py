"""
test_state.py — Tests for the QuestLine Agent DM state management layer.

Run: python3 test_state.py
"""

import json
import os
import shutil
import tempfile
import sys

from state import (
    GameState, PlayerState, EnemyState, SceneState, TurnState,
    Condition, DeathSaves, EnemyAttack,
    load_state, save_state, dataclass_to_dict, dict_to_dataclass,
    add_player, remove_player, update_player,
    deal_damage, heal,
    add_condition, remove_condition,
    give_item, take_item,
    spawn_enemy, remove_enemy,
    set_scene, set_flag, get_flag,
    start_combat, end_combat, next_turn,
    append_narrative, get_recent_narrative,
    state_summary,
    ability_modifier, proficiency_bonus,
    DEFAULT_ABILITIES,
)


class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"  ✓ {name}")

    def fail(self, name: str, msg: str):
        self.failed += 1
        self.errors.append(f"{name}: {msg}")
        print(f"  ✗ {name}: {msg}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for e in self.errors:
                print(f"  - {e}")
        return self.failed == 0


# M3 fix: collect all tmpdirs for cleanup at the end
_all_tmpdirs: list[str] = []


def make_temp_state() -> tuple[GameState, str, str]:
    """Create a GameState with a temp directory for testing."""
    tmpdir = tempfile.mkdtemp(prefix="questline_test_")
    _all_tmpdirs.append(tmpdir)
    state_path = os.path.join(tmpdir, "state.json")
    narrative_dir = os.path.join(tmpdir, "narrative")
    os.makedirs(narrative_dir, exist_ok=True)

    state = GameState(
        _state_path=state_path,
        _narrative_dir=narrative_dir,
        session_id="test-session",
        narrative_file="test_narrative.md",
    )
    return state, tmpdir, state_path


def run_tests():
    results = TestResults()

    # --- Helpers ---
    print("\n[Helpers]")
    try:
        assert ability_modifier(10) == 0
        assert ability_modifier(16) == 3
        assert ability_modifier(8) == -1
        assert ability_modifier(1) == -5
        assert ability_modifier(20) == 5
        results.ok("ability_modifier")
    except AssertionError as e:
        results.fail("ability_modifier", str(e))

    try:
        assert proficiency_bonus(1) == 2
        assert proficiency_bonus(4) == 2
        assert proficiency_bonus(5) == 3
        assert proficiency_bonus(8) == 3
        assert proficiency_bonus(9) == 4
        assert proficiency_bonus(12) == 4
        results.ok("proficiency_bonus")
    except AssertionError as e:
        results.fail("proficiency_bonus", str(e))

    # --- Persistence ---
    print("\n[Persistence]")
    state, tmpdir, state_path = make_temp_state()
    try:
        save_state(state, state_path)
        assert os.path.exists(state_path), "State file should exist"
        loaded = load_state(state_path, narrative_dir=os.path.join(tmpdir, "narrative"))
        assert loaded.session_id == "test-session"
        assert loaded.phase == "lobby"
        results.ok("save_state / load_state basic")
    except Exception as e:
        results.fail("save_state / load_state basic", str(e))

    try:
        fresh = load_state(os.path.join(tmpdir, "nonexistent.json"))
        assert fresh.phase == "lobby"
        assert fresh.session_id == "campaign-001"
        results.ok("load_state creates fresh state for missing file")
    except Exception as e:
        results.fail("load_state creates fresh state", str(e))

    # --- Player Management ---
    print("\n[Player Management]")
    state, tmpdir, state_path = make_temp_state()
    try:
        player = add_player(state, "uuid:abc123", "Kael", "fighter",
                           abilities={"strength": 16, "dexterity": 12, "constitution": 14},
                           ac=16, skills=["athletics", "intimidation"],
                           inventory=["longsword", "shield"])
        assert player.name == "Kael"
        assert player.char_class == "fighter"
        assert player.abilities["strength"] == 16
        assert player.abilities["intelligence"] == 10  # default
        assert player.ac == 16
        assert player.proficiency == 2
        assert "longsword" in player.inventory
        assert player.hp > 0
        assert player.hp == player.max_hp
        # Should be auto-saved
        assert os.path.exists(state_path)
        results.ok("add_player")
    except Exception as e:
        results.fail("add_player", str(e))

    try:
        player2 = add_player(state, "uuid:def456", "Lux", "wizard",
                            abilities={"intelligence": 18, "constitution": 12},
                            level=3, ac=12)
        assert player2.name == "Lux"
        assert player2.char_class == "wizard"
        assert player2.abilities["intelligence"] == 18
        assert len(state.players) == 2
        results.ok("add_player (second player)")
    except Exception as e:
        results.fail("add_player (second)", str(e))

    try:
        updated = update_player(state, "uuid:abc123", hp=20, position="front")
        assert updated.hp == 20
        assert updated.position == "front"
        results.ok("update_player")
    except Exception as e:
        results.fail("update_player", str(e))

    try:
        update_player(state, "uuid:abc123", abilities={"strength": 18})
        assert state.players["uuid:abc123"].abilities["strength"] == 18
        assert state.players["uuid:abc123"].abilities["dexterity"] == 12  # unchanged
        results.ok("update_player abilities merge")
    except Exception as e:
        results.fail("update_player abilities merge", str(e))

    try:
        update_player(state, "uuid:abc123", level=5)
        assert state.players["uuid:abc123"].proficiency == 3  # recalculated
        results.ok("update_player recalculates proficiency")
    except Exception as e:
        results.fail("update_player proficiency", str(e))

    try:
        remove_player(state, "uuid:def456")
        assert "uuid:def456" not in state.players
        assert len(state.players) == 1
        results.ok("remove_player")
    except Exception as e:
        results.fail("remove_player", str(e))

    try:
        remove_player(state, "uuid:nonexistent")
        results.fail("remove_player nonexistent", "Should have raised KeyError")
    except KeyError:
        results.ok("remove_player raises KeyError for missing player")
    except Exception as e:
        results.fail("remove_player nonexistent", str(e))

    # --- S6: remove_player prunes initiative ---
    try:
        state2, _, _ = make_temp_state()
        add_player(state2, "p1", "Kael", "fighter", hp=30, max_hp=30,
                   abilities={"dexterity": 14})
        add_player(state2, "p2", "Lux", "wizard", hp=18, max_hp=18,
                   abilities={"dexterity": 16})
        spawn_enemy(state2, "Goblin", hp=7, ac=15,
                    attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}])
        start_combat(state2, enemies_initiative={"enemy_1": 2})
        assert "Lux" in state2.turn.initiative_order
        remove_player(state2, "p2")
        assert "Lux" not in state2.turn.initiative_order
        results.ok("remove_player prunes initiative order (S6)")
    except Exception as e:
        results.fail("remove_player prunes initiative (S6)", str(e))

    # --- Damage & Healing ---
    print("\n[Damage & Healing]")
    state, tmpdir, state_path = make_temp_state()
    add_player(state, "p1", "Kael", "fighter", abilities={"constitution": 14}, hp=34, max_hp=34, ac=16)

    try:
        result = deal_damage(state, "p1", 10, "slashing")
        assert result["new_hp"] == 24
        assert result["damage_dealt"] == 10
        assert result["unconscious"] is False
        assert result["dead"] is False
        results.ok("deal_damage basic")
    except Exception as e:
        results.fail("deal_damage basic", str(e))

    try:
        result = deal_damage(state, "Kael", 30, "fire")  # by name
        assert result["new_hp"] == 0
        assert result["unconscious"] is True
        # Check unconscious condition was added
        assert any(c.name == "unconscious" for c in state.players["p1"].conditions)
        results.ok("deal_damage to 0 HP → unconscious")
    except Exception as e:
        results.fail("deal_damage unconscious", str(e))

    try:
        result = deal_damage(state, "p1", 100, "necrotic")
        assert result["new_hp"] == 0  # clamped
        assert result["damage_dealt"] == 0  # already at 0
        results.ok("deal_damage clamps to 0")
    except Exception as e:
        results.fail("deal_damage clamp", str(e))

    try:
        result = heal(state, "p1", 15)
        assert result["new_hp"] == 15
        assert result["healed"] == 15
        # Unconscious should be cleared
        assert not any(c.name == "unconscious" for c in state.players["p1"].conditions)
        results.ok("heal clears unconscious")
    except Exception as e:
        results.fail("heal clears unconscious", str(e))

    try:
        result = heal(state, "p1", 1000)
        assert result["new_hp"] == 34  # clamped to max
        results.ok("heal clamps to max_hp")
    except Exception as e:
        results.fail("heal clamp", str(e))

    # Enemy damage
    try:
        eid = spawn_enemy(state, "Goblin", hp=7, ac=15,
                          attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}])
        result = deal_damage(state, eid, 7, "slashing")
        assert result["new_hp"] == 0
        assert result["dead"] is True
        enemy = next(e for e in state.enemies if e.id == eid)
        assert not enemy.is_alive
        results.ok("deal_damage kills enemy")
    except Exception as e:
        results.fail("deal_damage enemy", str(e))

    # --- Conditions ---
    print("\n[Conditions]")
    state, tmpdir, state_path = make_temp_state()
    add_player(state, "p1", "Kael", "fighter", hp=30, max_hp=30)

    try:
        result = add_condition(state, "p1", "poisoned", duration=3, source="poison trap")
        assert result["status"] == "added"
        assert len(state.players["p1"].conditions) == 1
        assert state.players["p1"].conditions[0].name == "poisoned"
        assert state.players["p1"].conditions[0].duration == 3
        assert state.players["p1"].conditions[0].source == "poison trap"
        results.ok("add_condition with source")
    except Exception as e:
        results.fail("add_condition", str(e))

    try:
        result = remove_condition(state, "p1", "poisoned")
        assert result["status"] == "removed"
        assert len(state.players["p1"].conditions) == 0
        results.ok("remove_condition")
    except Exception as e:
        results.fail("remove_condition", str(e))

    try:
        result = remove_condition(state, "p1", "nonexistent")
        assert result["status"] == "not_found"
        results.ok("remove_condition not_found")
    except Exception as e:
        results.fail("remove_condition not_found", str(e))

    # --- Inventory ---
    print("\n[Inventory]")
    try:
        result = give_item(state, "p1", "Healing Potion", properties={"heals": "2d4+2"})
        assert result["status"] == "given"
        assert "Healing Potion" in state.players["p1"].inventory
        assert "item_healing_potion_properties" in state.flags
        results.ok("give_item with properties")
    except Exception as e:
        results.fail("give_item", str(e))

    try:
        result = take_item(state, "p1", "healing potion")  # case-insensitive
        assert result["status"] == "taken"
        assert "Healing Potion" not in state.players["p1"].inventory
        results.ok("take_item case-insensitive")
    except Exception as e:
        results.fail("take_item", str(e))

    try:
        take_item(state, "p1", "nonexistent item")
        results.fail("take_item nonexistent", "Should have raised ValueError")
    except ValueError:
        results.ok("take_item raises ValueError for missing item")
    except Exception as e:
        results.fail("take_item nonexistent", str(e))

    # --- Enemy Management ---
    print("\n[Enemy Management]")
    state, tmpdir, state_path = make_temp_state()

    try:
        eid1 = spawn_enemy(state, "Goblin", hp=7, ac=15,
                           attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}],
                           position="front")
        assert eid1 == "enemy_1"
        assert len(state.enemies) == 1
        assert state.enemies[0].name == "Goblin"
        assert state.enemies[0].attacks[0].bonus == 4
        results.ok("spawn_enemy")
    except Exception as e:
        results.fail("spawn_enemy", str(e))

    try:
        eid2 = spawn_enemy(state, "Goblin Archer", hp=5, ac=13,
                           attacks=[{"name": "Shortbow", "bonus": 3, "damage": "1d6+1"}])
        assert eid2 == "enemy_2"
        assert len(state.enemies) == 2
        results.ok("spawn_enemy auto-increment ID")
    except Exception as e:
        results.fail("spawn_enemy ID", str(e))

    try:
        result = remove_enemy(state, "enemy_1")
        assert result["status"] == "removed"
        assert len(state.enemies) == 1
        results.ok("remove_enemy")
    except Exception as e:
        results.fail("remove_enemy", str(e))

    try:
        remove_enemy(state, "enemy_999")
        results.fail("remove_enemy nonexistent", "Should have raised KeyError")
    except KeyError:
        results.ok("remove_enemy raises KeyError for missing enemy")
    except Exception as e:
        results.fail("remove_enemy nonexistent", str(e))

    # --- Scene ---
    print("\n[Scene Management]")
    state, tmpdir, state_path = make_temp_state()

    try:
        result = set_scene(state, "A damp stone corridor stretching into darkness.",
                          interactables=["rusted lever", "pile of bones"],
                          exits={"north": "crypt_hall", "south": "entrance"},
                          light="dim", environment=["difficult_terrain"])
        assert result["status"] == "scene_set"
        assert state.scene.light == "dim"
        assert "rusted lever" in state.scene.interactables
        assert state.scene.exits["north"] == "crypt_hall"
        results.ok("set_scene")
    except Exception as e:
        results.fail("set_scene", str(e))

    # --- Flags ---
    print("\n[Story Flags]")
    try:
        result = set_flag(state, "merchant_alive", True)
        assert result["status"] == "set"
        assert get_flag(state, "merchant_alive") is True
        assert get_flag(state, "nonexistent") is None
        results.ok("set_flag / get_flag")
    except Exception as e:
        results.fail("flags", str(e))

    # --- Combat ---
    print("\n[Combat Management]")
    state, tmpdir, state_path = make_temp_state()
    add_player(state, "p1", "Kael", "fighter",
               abilities={"dexterity": 14}, hp=30, max_hp=30)
    add_player(state, "p2", "Lux", "wizard",
               abilities={"dexterity": 16}, hp=18, max_hp=18)
    spawn_enemy(state, "Goblin", hp=7, ac=15,
                attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}])

    try:
        result = start_combat(state, enemies_initiative={"enemy_1": 2})
        assert result["status"] == "combat_started"
        assert state.phase == "combat"
        assert len(result["initiative_order"]) == 3  # 2 players + 1 enemy
        assert state.turn.round_number == 1
        assert state.turn.current is not None
        results.ok("start_combat")
    except Exception as e:
        results.fail("start_combat", str(e))

    # I4: test start_combat with include_enemies filter
    try:
        state_i4, _, _ = make_temp_state()
        add_player(state_i4, "p1", "Kael", "fighter",
                   abilities={"dexterity": 14}, hp=30, max_hp=30)
        spawn_enemy(state_i4, "Goblin", hp=7, ac=15,
                    attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}])
        spawn_enemy(state_i4, "Orc", hp=15, ac=13,
                    attacks=[{"name": "Greataxe", "bonus": 5, "damage": "1d12+3"}])
        # Only include Goblin (enemy_1) in combat
        result = start_combat(state_i4, enemies_initiative={"enemy_1": 2},
                             include_enemies=["enemy_1"])
        assert "enemy_2" not in result["initiative_order"], \
            f"enemy_2 (Orc) should not be in initiative: {result['initiative_order']}"
        assert len(result["initiative_order"]) == 2  # 1 player + 1 enemy
        results.ok("start_combat with include_enemies filter (I4)")
    except Exception as e:
        results.fail("start_combat include_enemies (I4)", str(e))

    try:
        first = state.turn.current
        result = next_turn(state)
        assert result["status"] == "turn_advanced"
        assert result["current"] != first or len(state.turn.initiative_order) == 1
        results.ok("next_turn")
    except Exception as e:
        results.fail("next_turn", str(e))

    try:
        result = end_combat(state)
        assert result["status"] == "combat_ended"
        assert state.phase == "exploration"
        assert state.turn.current is None
        results.ok("end_combat")
    except Exception as e:
        results.fail("end_combat", str(e))

    # I1: test round_number doesn't multi-increment within a single next_turn call.
    # Set up: order = [enemy_1, enemy_2, Kael]. Current = Kael (idx 2).
    # Kill both enemies. A single next_turn should skip enemy_1 (idx 0, wraps)
    # and enemy_2 (idx 1, also past wrap), landing on Kael (idx 2).
    # The OLD bug would increment round_number for each skip past the wrap point.
    # The fix ensures it only increments once per wrap.
    print("\n[Round Number (I1)]")
    try:
        state_r, _, _ = make_temp_state()
        add_player(state_r, "p1", "Kael", "fighter",
                   abilities={"dexterity": 14}, hp=30, max_hp=30)
        eid_a = spawn_enemy(state_r, "Goblin A", hp=7, ac=15,
                    attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}])
        eid_b = spawn_enemy(state_r, "Goblin B", hp=7, ac=15,
                    attacks=[{"name": "Scimitar", "bonus": 4, "damage": "1d6+2"}])

        # Force a deterministic initiative order for this test
        state_r.phase = "combat"
        state_r.turn = TurnState(
            current="Kael",
            initiative_order=[eid_a, eid_b, "Kael"],
            round_number=1,
            phase="combat",
        )
        save_state(state_r)

        # Kill both goblins so they get skipped
        deal_damage(state_r, eid_a, 100, "slashing")
        deal_damage(state_r, eid_b, 100, "slashing")

        # A single next_turn should wrap once and land back on Kael = round 2
        result = next_turn(state_r)
        assert result["current"] == "Kael"
        assert state_r.turn.round_number == 2, \
            f"Expected round 2 after single wrap, got {state_r.turn.round_number}"
        results.ok("next_turn round_number no multi-increment (I1)")
    except Exception as e:
        results.fail("next_turn round I1", str(e))

    # --- Narrative ---
    print("\n[Narrative Logging]")
    state, tmpdir, state_path = make_temp_state()

    try:
        result = append_narrative(state, "COMBAT", "Kael attacks Goblin with longsword.")
        assert result["status"] == "appended"
        append_narrative(state, "ROLEPLAY", "Lux taunts the goblin.")
        append_narrative(state, "WORLD", "The goblin flees north.")

        entries = get_recent_narrative(state, n=10)
        assert len(entries) == 3
        assert "COMBAT" in entries[0]
        assert "Kael" in entries[0]
        results.ok("append_narrative / get_recent_narrative")
    except Exception as e:
        results.fail("narrative", str(e))

    try:
        entries = get_recent_narrative(state, n=2)
        assert len(entries) == 2
        assert "ROLEPLAY" in entries[0]
        assert "WORLD" in entries[1]
        results.ok("get_recent_narrative respects n limit")
    except Exception as e:
        results.fail("narrative n limit", str(e))

    # --- Serialization round-trip ---
    print("\n[Serialization Round-trip]")
    state, tmpdir, state_path = make_temp_state()
    add_player(state, "p1", "Kael", "fighter",
               abilities={"strength": 16, "dexterity": 12},
               hp=30, max_hp=30, ac=16,
               skills=["athletics"], inventory=["longsword"])
    add_condition(state, "p1", "blessed", duration=5, source="cleric")
    spawn_enemy(state, "Skeleton", hp=13, ac=13,
                attacks=[{"name": "Shortsword", "bonus": 4, "damage": "1d6+2"}])
    set_scene(state, "A dusty crypt.", interactables=["sarcophagus"],
              exits={"east": "hallway"}, light="dim")
    set_flag(state, "door_locked", True)
    start_combat(state, enemies_initiative={"enemy_1": 1})

    try:
        save_state(state, state_path)
        loaded = load_state(state_path, narrative_dir=os.path.join(tmpdir, "narrative"))

        # Verify everything survived
        assert loaded.session_id == state.session_id
        assert loaded.phase == "combat"
        assert "p1" in loaded.players
        p = loaded.players["p1"]
        assert p.name == "Kael"
        assert p.char_class == "fighter"
        assert p.abilities["strength"] == 16
        assert p.hp == 30
        assert p.ac == 16
        assert "longsword" in p.inventory
        assert len(p.conditions) == 1
        assert p.conditions[0].name == "blessed"
        assert p.conditions[0].duration == 5

        assert len(loaded.enemies) == 1
        assert loaded.enemies[0].name == "Skeleton"
        assert loaded.enemies[0].attacks[0].bonus == 4

        assert loaded.scene.description == "A dusty crypt."
        assert loaded.scene.light == "dim"
        assert loaded.scene.exits["east"] == "hallway"

        assert loaded.flags["door_locked"] is True

        assert loaded.turn.round_number == 1
        assert len(loaded.turn.initiative_order) == 2

        results.ok("full serialization round-trip")
    except Exception as e:
        results.fail("serialization round-trip", str(e))

    # --- State summary ---
    print("\n[State Summary]")
    try:
        summary = state_summary(state)
        assert "Kael" in summary
        assert "combat" in summary.lower()
        assert "Skeleton" in summary
        assert "dusty crypt" in summary.lower() or "A dusty crypt" in summary
        results.ok("state_summary")
    except Exception as e:
        results.fail("state_summary", str(e))

    # --- M3 fix: Cleanup ALL temp dirs ---
    for td in _all_tmpdirs:
        shutil.rmtree(td, ignore_errors=True)

    # Final results
    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    run_tests()
