"""
state.py — Game state management for QuestLine Agent DM.

Defines all game state dataclasses (PlayerState, EnemyState, SceneState, TurnState,
GameState) and provides mutation functions that auto-persist to disk after every change.

Includes narrative logging (append-only markdown files) and retrieval.

Design principles:
  - All state mutations auto-save to JSON after modification.
  - Enemy IDs are auto-generated ("enemy_1", "enemy_2", ...).
  - HP clamped to [0, max_hp]. At 0 HP: unconscious. Death saves tracked separately.
  - Conditions stored as dicts: {name, duration, source}.
  - Abilities use full names: "strength", "dexterity", etc.
  - Default ability scores: 10 (modifier +0) if not specified.
  - Proficiency: +2 (L1-4), +3 (L5-8), +4 (L9-12).
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("questline.state")


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """Load the YAML configuration file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ABILITY_NAMES = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")
DEFAULT_ABILITIES: dict[str, int] = {a: 10 for a in ABILITY_NAMES}

PHASES = ("lobby", "exploration", "combat", "social", "rest")

HIT_DICE = {
    "fighter": 10,
    "ranger": 10,
    "cleric": 8,
    "rogue": 8,
    "bard": 8,
    "wizard": 6,
}


def ability_modifier(score: int) -> int:
    """Compute the ability modifier for a given score (5e rules)."""
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    """Proficiency bonus by level: +2 (1-4), +3 (5-8), +4 (9-12)."""
    if level <= 4:
        return 2
    elif level <= 8:
        return 3
    else:
        return 4


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    """A condition affecting a creature (e.g., poisoned, frightened, prone)."""
    name: str
    duration: int | None = None   # Rounds remaining; None = indefinite
    source: str | None = None     # What caused this condition


@dataclass
class DeathSaves:
    """Tracks death saving throws."""
    successes: int = 0
    failures: int = 0


@dataclass
class PlayerState:
    """Full character sheet for a player."""
    name: str
    char_class: str                                       # "fighter", "wizard", etc.
    level: int = 1
    hp: int = 10
    max_hp: int = 10
    ac: int = 10
    abilities: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_ABILITIES))
    proficiency: int = 2
    skills: list[str] = field(default_factory=list)
    inventory: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    position: str | None = None                           # Narrative position ("front", "back", etc.)
    spell_slots: dict[str, int] | None = None             # {"1": 2, "2": 1} or None
    death_saves: DeathSaves = field(default_factory=DeathSaves)


@dataclass
class EnemyAttack:
    """A single attack option for an enemy."""
    name: str
    bonus: int
    damage: str  # e.g., "1d6+2"


@dataclass
class EnemyState:
    """A creature controlled by the DM."""
    id: str
    name: str
    hp: int
    max_hp: int
    ac: int
    attacks: list[EnemyAttack] = field(default_factory=list)
    position: str | None = None
    conditions: list[Condition] = field(default_factory=list)
    is_alive: bool = True


@dataclass
class SceneState:
    """The current environment / room."""
    description: str = ""
    interactables: list[str] = field(default_factory=list)
    exits: dict[str, str] = field(default_factory=dict)   # direction → location_id
    light: str = "bright"                                  # "bright", "dim", "dark"
    environment: list[str] = field(default_factory=list)   # e.g., ["difficult_terrain"]


@dataclass
class TurnState:
    """Combat turn tracking."""
    current: str | None = None           # Player name or enemy ID whose turn it is
    initiative_order: list[str] = field(default_factory=list)
    round_number: int = 0
    phase: str = "combat"                # Always "combat" when TurnState is active


@dataclass
class GameState:
    """Top-level game state — the single source of truth."""
    session_id: str = "campaign-001"
    phase: str = "lobby"                 # One of PHASES
    current_location: str = "start"
    players: dict[str, PlayerState] = field(default_factory=dict)   # sender_id → PlayerState
    enemies: list[EnemyState] = field(default_factory=list)
    scene: SceneState = field(default_factory=SceneState)
    turn: TurnState = field(default_factory=TurnState)
    flags: dict[str, Any] = field(default_factory=dict)
    narrative_file: str = ""             # Current session's narrative log filename
    _enemy_counter: int = 0             # Auto-increment for enemy IDs
    _state_path: str = ""               # Path to save file (set on load/init)
    _narrative_dir: str = ""            # Path to narrative directory


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _condition_to_dict(c: Condition) -> dict:
    return {"name": c.name, "duration": c.duration, "source": c.source}


def _condition_from_dict(d: dict) -> Condition:
    return Condition(name=d["name"], duration=d.get("duration"), source=d.get("source"))


def _attack_to_dict(a: EnemyAttack) -> dict:
    return {"name": a.name, "bonus": a.bonus, "damage": a.damage}


def _attack_from_dict(d: dict) -> EnemyAttack:
    return EnemyAttack(name=d["name"], bonus=d["bonus"], damage=d["damage"])


def _death_saves_to_dict(ds: DeathSaves) -> dict:
    return {"successes": ds.successes, "failures": ds.failures}


def _death_saves_from_dict(d: dict) -> DeathSaves:
    return DeathSaves(successes=d.get("successes", 0), failures=d.get("failures", 0))


def _player_to_dict(p: PlayerState) -> dict:
    return {
        "name": p.name,
        "char_class": p.char_class,
        "level": p.level,
        "hp": p.hp,
        "max_hp": p.max_hp,
        "ac": p.ac,
        "abilities": dict(p.abilities),
        "proficiency": p.proficiency,
        "skills": list(p.skills),
        "inventory": list(p.inventory),
        "conditions": [_condition_to_dict(c) for c in p.conditions],
        "position": p.position,
        "spell_slots": dict(p.spell_slots) if p.spell_slots else None,
        "death_saves": _death_saves_to_dict(p.death_saves),
    }


def _player_from_dict(d: dict) -> PlayerState:
    abilities = d.get("abilities", dict(DEFAULT_ABILITIES))
    level = d.get("level", 1)
    return PlayerState(
        name=d["name"],
        char_class=d.get("char_class", "fighter"),
        level=level,
        hp=d.get("hp", 10),
        max_hp=d.get("max_hp", 10),
        ac=d.get("ac", 10),
        abilities=abilities,
        proficiency=d.get("proficiency", proficiency_bonus(level)),
        skills=d.get("skills", []),
        inventory=d.get("inventory", []),
        conditions=[_condition_from_dict(c) for c in d.get("conditions", [])],
        position=d.get("position"),
        spell_slots=d.get("spell_slots"),
        death_saves=_death_saves_from_dict(d.get("death_saves", {})),
    )


def _enemy_to_dict(e: EnemyState) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "hp": e.hp,
        "max_hp": e.max_hp,
        "ac": e.ac,
        "attacks": [_attack_to_dict(a) for a in e.attacks],
        "position": e.position,
        "conditions": [_condition_to_dict(c) for c in e.conditions],
        "is_alive": e.is_alive,
    }


def _enemy_from_dict(d: dict) -> EnemyState:
    return EnemyState(
        id=d["id"],
        name=d["name"],
        hp=d["hp"],
        max_hp=d["max_hp"],
        ac=d["ac"],
        attacks=[_attack_from_dict(a) for a in d.get("attacks", [])],
        position=d.get("position"),
        conditions=[_condition_from_dict(c) for c in d.get("conditions", [])],
        is_alive=d.get("is_alive", True),
    )


def _scene_to_dict(s: SceneState) -> dict:
    return {
        "description": s.description,
        "interactables": list(s.interactables),
        "exits": dict(s.exits),
        "light": s.light,
        "environment": list(s.environment),
    }


def _scene_from_dict(d: dict) -> SceneState:
    return SceneState(
        description=d.get("description", ""),
        interactables=d.get("interactables", []),
        exits=d.get("exits", {}),
        light=d.get("light", "bright"),
        environment=d.get("environment", []),
    )


def _turn_to_dict(t: TurnState) -> dict:
    return {
        "current": t.current,
        "initiative_order": list(t.initiative_order),
        "round_number": t.round_number,
        "phase": t.phase,
    }


def _turn_from_dict(d: dict) -> TurnState:
    return TurnState(
        current=d.get("current"),
        initiative_order=d.get("initiative_order", []),
        round_number=d.get("round_number", 0),
        phase=d.get("phase", "combat"),
    )


def dataclass_to_dict(state: GameState) -> dict[str, Any]:
    """Serialize a GameState to a JSON-compatible dict."""
    return {
        "session_id": state.session_id,
        "phase": state.phase,
        "current_location": state.current_location,
        "players": {sid: _player_to_dict(p) for sid, p in state.players.items()},
        "enemies": [_enemy_to_dict(e) for e in state.enemies],
        "scene": _scene_to_dict(state.scene),
        "turn": _turn_to_dict(state.turn),
        "flags": dict(state.flags),
        "narrative_file": state.narrative_file,
        "_enemy_counter": state._enemy_counter,
    }


def dict_to_dataclass(d: dict[str, Any], state_path: str = "", narrative_dir: str = "") -> GameState:
    """Deserialize a dict into a GameState."""
    state = GameState(
        session_id=d.get("session_id", "campaign-001"),
        phase=d.get("phase", "lobby"),
        current_location=d.get("current_location", "start"),
        players={sid: _player_from_dict(p) for sid, p in d.get("players", {}).items()},
        enemies=[_enemy_from_dict(e) for e in d.get("enemies", [])],
        scene=_scene_from_dict(d.get("scene", {})),
        turn=_turn_from_dict(d.get("turn", {})),
        flags=d.get("flags", {}),
        narrative_file=d.get("narrative_file", ""),
        _enemy_counter=d.get("_enemy_counter", 0),
        _state_path=state_path,
        _narrative_dir=narrative_dir,
    )
    return state


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_state(state: GameState, path: str | None = None) -> None:
    """Persist the game state to a JSON file.

    Args:
        state: The current GameState.
        path: File path. Falls back to state._state_path if not given.
    """
    target = path or state._state_path
    if not target:
        raise ValueError("No state file path configured — pass path or set state._state_path")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    data = dataclass_to_dict(state)
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, target)


def load_state(path: str, narrative_dir: str = "data/narrative") -> GameState:
    """Load game state from a JSON file. Returns fresh state if file doesn't exist.

    Args:
        path: Path to the state JSON file.
        narrative_dir: Directory for narrative log files.

    Returns:
        A GameState instance with _state_path and _narrative_dir set.
    """
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        state = dict_to_dataclass(data, state_path=path, narrative_dir=narrative_dir)
    else:
        state = GameState(_state_path=path, _narrative_dir=narrative_dir)
    # Ensure narrative file is set
    if not state.narrative_file:
        state.narrative_file = _new_narrative_filename(state.session_id)
    return state


def _new_narrative_filename(session_id: str) -> str:
    """Generate a narrative log filename from the session ID and current date."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_id = session_id.replace(" ", "_").replace("/", "_")
    return f"{safe_id}_{date_str}.md"


def _auto_save(state: GameState) -> None:
    """Save state after a mutation. Called by every state-modifying function.

    S3: Wrapped in try/except to avoid crashing on disk errors.
    """
    try:
        save_state(state)
    except OSError as e:
        log.error("Auto-save failed: %s", e)


# ---------------------------------------------------------------------------
# Player management
# ---------------------------------------------------------------------------

def add_player(
    state: GameState,
    sender_id: str,
    name: str,
    char_class: str,
    abilities: dict[str, int] | None = None,
    level: int = 1,
    hp: int | None = None,
    max_hp: int | None = None,
    ac: int = 10,
    skills: list[str] | None = None,
    inventory: list[str] | None = None,
    position: str | None = None,
    spell_slots: dict[str, int] | None = None,
) -> PlayerState:
    """Create and register a new player character.

    Args:
        state: The current GameState.
        sender_id: Unique identifier for the player (Signal UUID, phone, etc.).
        name: Character name.
        char_class: Character class ("fighter", "wizard", etc.).
        abilities: Ability scores dict. Defaults to all 10s.
        level: Starting level (default 1).
        hp: Starting HP. If None, computed from class + CON.
        max_hp: Max HP. If None, same as hp.
        ac: Armor class (default 10).
        skills: Proficient skills list.
        inventory: Starting inventory.
        position: Narrative position.
        spell_slots: Spell slot dict.

    Returns:
        The newly created PlayerState.
    """
    abs_scores = dict(DEFAULT_ABILITIES)
    if abilities:
        abs_scores.update(abilities)

    prof = proficiency_bonus(level)
    hit_die = HIT_DICE.get(char_class.lower(), 8)
    con_mod = ability_modifier(abs_scores["constitution"])

    if hp is None:
        # Level 1: max hit die + CON. Each additional level: avg roll + CON.
        hp = hit_die + con_mod + (level - 1) * (hit_die // 2 + 1 + con_mod)
        hp = max(hp, 1)
    if max_hp is None:
        max_hp = hp

    player = PlayerState(
        name=name,
        char_class=char_class.lower(),
        level=level,
        hp=hp,
        max_hp=max_hp,
        ac=ac,
        abilities=abs_scores,
        proficiency=prof,
        skills=skills or [],
        inventory=inventory or [],
        conditions=[],
        position=position,
        spell_slots=spell_slots,
        death_saves=DeathSaves(),
    )
    state.players[sender_id] = player
    _auto_save(state)
    return player


def remove_player(state: GameState, sender_id: str) -> None:
    """Remove a player from the game.

    S6: Also removes from initiative_order if in combat.

    Args:
        state: The current GameState.
        sender_id: The player's identifier.

    Raises:
        KeyError: If sender_id not found.
    """
    if sender_id not in state.players:
        raise KeyError(f"Player '{sender_id}' not found")

    # S6: Prune from initiative order if present
    player_name = state.players[sender_id].name
    if player_name in state.turn.initiative_order:
        state.turn.initiative_order.remove(player_name)
        # If the removed player was the current turn holder, advance
        if state.turn.current == player_name:
            state.turn.current = (
                state.turn.initiative_order[0]
                if state.turn.initiative_order
                else None
            )

    del state.players[sender_id]
    _auto_save(state)


def update_player(state: GameState, sender_id: str, **changes: Any) -> PlayerState:
    """Update arbitrary fields on a player's state.

    Supports nested updates for 'abilities' and 'death_saves'.
    Recalculates proficiency if level changes.

    Args:
        state: The current GameState.
        sender_id: The player's identifier.
        **changes: Field names and new values.

    Returns:
        The updated PlayerState.

    Raises:
        KeyError: If sender_id not found.
        AttributeError: If a field name is invalid.
    """
    if sender_id not in state.players:
        raise KeyError(f"Player '{sender_id}' not found")
    player = state.players[sender_id]

    for key, value in changes.items():
        if key == "abilities" and isinstance(value, dict):
            player.abilities.update(value)
        elif key == "death_saves" and isinstance(value, dict):
            if "successes" in value:
                player.death_saves.successes = value["successes"]
            if "failures" in value:
                player.death_saves.failures = value["failures"]
        elif key == "conditions":
            # Accept list of dicts or Condition objects
            player.conditions = [
                c if isinstance(c, Condition) else _condition_from_dict(c)
                for c in value
            ]
        elif hasattr(player, key):
            setattr(player, key, value)
        else:
            raise AttributeError(f"PlayerState has no field '{key}'")

    # Recalculate proficiency if level changed
    if "level" in changes:
        player.proficiency = proficiency_bonus(player.level)

    _auto_save(state)
    return player


# ---------------------------------------------------------------------------
# Damage & Healing
# ---------------------------------------------------------------------------

def _find_target(state: GameState, target_id: str) -> tuple[str, PlayerState | EnemyState]:
    """Locate a target by player sender_id, player name, or enemy ID.

    Returns:
        Tuple of (actual_id, target_object).

    Raises:
        KeyError: If target not found.
    """
    # Check player sender IDs
    if target_id in state.players:
        return target_id, state.players[target_id]

    # Check player names (case-insensitive)
    for sid, p in state.players.items():
        if p.name.lower() == target_id.lower():
            return sid, p

    # Check enemy IDs
    for e in state.enemies:
        if e.id == target_id:
            return e.id, e

    # Check enemy names (first alive match)
    for e in state.enemies:
        if e.name.lower() == target_id.lower() and e.is_alive:
            return e.id, e

    raise KeyError(f"Target '{target_id}' not found among players or enemies")


def deal_damage(
    state: GameState, target_id: str, amount: int, damage_type: str = "untyped"
) -> dict[str, Any]:
    """Deal damage to a player or enemy. Clamps HP to 0. Handles unconsciousness.

    Args:
        state: The current GameState.
        target_id: Player sender_id/name or enemy ID/name.
        amount: Damage amount (positive integer).
        damage_type: Type of damage (e.g., "slashing", "fire"). Informational.

    Returns:
        Dict with keys: target, new_hp, max_hp, damage_dealt, damage_type,
        unconscious, dead.
    """
    actual_id, target = _find_target(state, target_id)
    old_hp = target.hp
    target.hp = max(0, target.hp - amount)
    damage_dealt = old_hp - target.hp

    unconscious = False
    dead = False

    if isinstance(target, PlayerState):
        if target.hp == 0:
            unconscious = True
            # Add unconscious condition if not already present
            if not any(c.name == "unconscious" for c in target.conditions):
                target.conditions.append(Condition(name="unconscious"))
    elif isinstance(target, EnemyState):
        if target.hp == 0:
            target.is_alive = False
            dead = True

    _auto_save(state)
    return {
        "target": target.name if isinstance(target, PlayerState) else target.name,
        "target_id": actual_id,
        "new_hp": target.hp,
        "max_hp": target.max_hp,
        "damage_dealt": damage_dealt,
        "damage_type": damage_type,
        "unconscious": unconscious,
        "dead": dead,
    }


def heal(state: GameState, target_id: str, amount: int) -> dict[str, Any]:
    """Heal a player or enemy. Clamps HP to max_hp. Clears unconscious if applicable.

    Args:
        state: The current GameState.
        target_id: Player sender_id/name or enemy ID/name.
        amount: Healing amount (positive integer).

    Returns:
        Dict with keys: target, new_hp, max_hp, healed.
    """
    actual_id, target = _find_target(state, target_id)
    old_hp = target.hp
    target.hp = min(target.max_hp, target.hp + amount)
    healed = target.hp - old_hp

    if isinstance(target, PlayerState) and target.hp > 0:
        # Clear unconscious and reset death saves
        target.conditions = [c for c in target.conditions if c.name != "unconscious"]
        target.death_saves = DeathSaves()

    if isinstance(target, EnemyState) and target.hp > 0:
        target.is_alive = True

    _auto_save(state)
    return {
        "target": target.name,
        "target_id": actual_id,
        "new_hp": target.hp,
        "max_hp": target.max_hp,
        "healed": healed,
    }


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def add_condition(
    state: GameState,
    target_id: str,
    condition: str,
    duration: int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Add a condition to a player or enemy.

    Args:
        state: The current GameState.
        target_id: Player sender_id/name or enemy ID/name.
        condition: Condition name (e.g., "poisoned", "prone").
        duration: Rounds remaining, or None for indefinite.
        source: What caused this condition.

    Returns:
        Confirmation dict.
    """
    actual_id, target = _find_target(state, target_id)
    new_cond = Condition(name=condition, duration=duration, source=source)
    target.conditions.append(new_cond)
    _auto_save(state)
    return {
        "target": target.name if hasattr(target, "name") else actual_id,
        "condition": condition,
        "duration": duration,
        "source": source,
        "status": "added",
    }


def remove_condition(state: GameState, target_id: str, condition: str) -> dict[str, Any]:
    """Remove a condition from a player or enemy.

    Removes the first matching condition by name.

    Args:
        state: The current GameState.
        target_id: Player sender_id/name or enemy ID/name.
        condition: Condition name to remove.

    Returns:
        Confirmation dict.
    """
    actual_id, target = _find_target(state, target_id)
    original_count = len(target.conditions)
    # Remove first match
    for i, c in enumerate(target.conditions):
        if c.name == condition:
            target.conditions.pop(i)
            break
    removed = len(target.conditions) < original_count
    _auto_save(state)
    return {
        "target": target.name if hasattr(target, "name") else actual_id,
        "condition": condition,
        "status": "removed" if removed else "not_found",
    }


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def give_item(
    state: GameState, player_id: str, item: str, properties: dict | None = None
) -> dict[str, Any]:
    """Give an item to a player.

    Args:
        state: The current GameState.
        player_id: Player sender_id or name.
        item: Item name/description.
        properties: Optional properties dict (stored in flags as item metadata).

    Returns:
        Confirmation dict.
    """
    actual_id, player = _find_target(state, player_id)
    if not isinstance(player, PlayerState):
        raise TypeError(f"'{player_id}' is not a player")

    # Store item (with properties as suffix if simple, or in flags)
    player.inventory.append(item)

    if properties:
        flag_key = f"item_{item.lower().replace(' ', '_')}_properties"
        state.flags[flag_key] = properties

    _auto_save(state)
    return {
        "player": player.name,
        "item": item,
        "properties": properties,
        "inventory_count": len(player.inventory),
        "status": "given",
    }


def take_item(state: GameState, player_id: str, item: str) -> dict[str, Any]:
    """Remove an item from a player's inventory.

    Args:
        state: The current GameState.
        player_id: Player sender_id or name.
        item: Item name to remove.

    Returns:
        Confirmation dict.

    Raises:
        ValueError: If the item is not in the player's inventory.
    """
    actual_id, player = _find_target(state, player_id)
    if not isinstance(player, PlayerState):
        raise TypeError(f"'{player_id}' is not a player")

    # Case-insensitive search
    for i, inv_item in enumerate(player.inventory):
        if inv_item.lower() == item.lower():
            player.inventory.pop(i)
            _auto_save(state)
            return {
                "player": player.name,
                "item": item,
                "inventory_count": len(player.inventory),
                "status": "taken",
            }

    raise ValueError(f"'{item}' not found in {player.name}'s inventory")


# ---------------------------------------------------------------------------
# Enemy management
# ---------------------------------------------------------------------------

def spawn_enemy(
    state: GameState,
    name: str,
    hp: int,
    ac: int,
    attacks: list[dict[str, Any]],
    position: str | None = None,
) -> str:
    """Spawn a new enemy in the current scene.

    Args:
        state: The current GameState.
        name: Enemy name (e.g., "Goblin").
        hp: Hit points.
        ac: Armor class.
        attacks: List of attack dicts, each with "name", "bonus", "damage".
        position: Narrative position.

    Returns:
        The auto-generated enemy ID (e.g., "enemy_1").
    """
    state._enemy_counter += 1
    enemy_id = f"enemy_{state._enemy_counter}"

    parsed_attacks = [
        EnemyAttack(name=a["name"], bonus=a["bonus"], damage=a["damage"])
        for a in attacks
    ]

    enemy = EnemyState(
        id=enemy_id,
        name=name,
        hp=hp,
        max_hp=hp,
        ac=ac,
        attacks=parsed_attacks,
        position=position,
        conditions=[],
        is_alive=True,
    )
    state.enemies.append(enemy)
    _auto_save(state)
    return enemy_id


def remove_enemy(state: GameState, enemy_id: str) -> dict[str, Any]:
    """Remove an enemy from the game (killed, fled, etc.).

    Args:
        state: The current GameState.
        enemy_id: The enemy's ID.

    Returns:
        Confirmation dict.

    Raises:
        KeyError: If enemy_id not found.
    """
    for i, e in enumerate(state.enemies):
        if e.id == enemy_id:
            removed = state.enemies.pop(i)
            _auto_save(state)
            return {"enemy_id": enemy_id, "name": removed.name, "status": "removed"}
    raise KeyError(f"Enemy '{enemy_id}' not found")


# ---------------------------------------------------------------------------
# Scene management
# ---------------------------------------------------------------------------

def set_scene(
    state: GameState,
    description: str,
    interactables: list[str] | None = None,
    exits: dict[str, str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Set or update the current scene.

    Args:
        state: The current GameState.
        description: Scene description text.
        interactables: List of interactable objects.
        exits: Dict of direction → location_id.
        **kwargs: Additional SceneState fields (light, environment).

    Returns:
        Confirmation dict with the new scene summary.
    """
    state.scene.description = description
    if interactables is not None:
        state.scene.interactables = interactables
    if exits is not None:
        state.scene.exits = exits
    if "light" in kwargs:
        state.scene.light = kwargs["light"]
    if "environment" in kwargs:
        state.scene.environment = kwargs["environment"]

    _auto_save(state)
    return {
        "description": state.scene.description,
        "interactables": state.scene.interactables,
        "exits": state.scene.exits,
        "light": state.scene.light,
        "environment": state.scene.environment,
        "status": "scene_set",
    }


# ---------------------------------------------------------------------------
# Story flags
# ---------------------------------------------------------------------------

def set_flag(state: GameState, key: str, value: Any) -> dict[str, Any]:
    """Set a story flag.

    Args:
        state: The current GameState.
        key: Flag name.
        value: Flag value (any JSON-serializable type).

    Returns:
        Confirmation dict.
    """
    state.flags[key] = value
    _auto_save(state)
    return {"flag": key, "value": value, "status": "set"}


def get_flag(state: GameState, key: str) -> Any:
    """Get a story flag value.

    Args:
        state: The current GameState.
        key: Flag name.

    Returns:
        The flag value, or None if not set.
    """
    return state.flags.get(key)


# ---------------------------------------------------------------------------
# Combat management
# ---------------------------------------------------------------------------

def start_combat(
    state: GameState,
    enemies_initiative: dict[str, int] | None = None,
    include_enemies: list[str] | None = None,
) -> dict[str, Any]:
    """Start combat. Rolls initiative (d20 + DEX mod) for all players and enemies.

    I4 fix: Added include_enemies parameter to filter which enemies participate.

    Args:
        state: The current GameState.
        enemies_initiative: Optional pre-set initiative bonuses for enemies
                            (enemy_id → bonus). If None, enemies roll d20+0.
        include_enemies: Optional list of enemy IDs to include. If None, all
                         alive enemies participate.

    Returns:
        Dict with initiative_order and round info.
    """
    if enemies_initiative is None:
        enemies_initiative = {}

    rolls: list[tuple[str, int]] = []

    # Roll for players
    for sender_id, player in state.players.items():
        dex_mod = ability_modifier(player.abilities.get("dexterity", 10))
        roll = random.randint(1, 20) + dex_mod
        rolls.append((player.name, roll))

    # Roll for enemies (I4: filter by include_enemies if provided)
    for enemy in state.enemies:
        if enemy.is_alive and (include_enemies is None or enemy.id in include_enemies):
            bonus = enemies_initiative.get(enemy.id, 0)
            roll = random.randint(1, 20) + bonus
            rolls.append((enemy.id, roll))

    # Sort descending by roll (higher goes first); ties broken randomly
    rolls.sort(key=lambda x: (x[1], random.random()), reverse=True)
    order = [name for name, _ in rolls]

    state.phase = "combat"
    state.turn = TurnState(
        current=order[0] if order else None,
        initiative_order=order,
        round_number=1,
        phase="combat",
    )
    _auto_save(state)
    return {
        "initiative_order": order,
        "current": state.turn.current,
        "round": 1,
        "rolls": {name: roll for name, roll in rolls},
        "status": "combat_started",
    }


def end_combat(state: GameState) -> dict[str, Any]:
    """End combat and return to exploration.

    Returns:
        Summary dict with surviving players, defeated enemies, etc.
    """
    surviving_players = {
        sid: p.name for sid, p in state.players.items() if p.hp > 0
    }
    defeated_enemies = [e.name for e in state.enemies if not e.is_alive]
    surviving_enemies = [e.name for e in state.enemies if e.is_alive]

    # Clean up dead enemies from the list
    state.enemies = [e for e in state.enemies if e.is_alive]

    # Reset combat state
    state.phase = "exploration"
    state.turn = TurnState()

    # Clear combat-specific conditions from players (like "unconscious" stays, "dodge" clears)
    # For now, leave conditions — the DM can clear them manually.

    _auto_save(state)
    return {
        "surviving_players": surviving_players,
        "defeated_enemies": defeated_enemies,
        "surviving_enemies": surviving_enemies,
        "phase": "exploration",
        "status": "combat_ended",
    }


def next_turn(state: GameState) -> dict[str, Any]:
    """Advance to the next creature in initiative order.

    I1 fix: Uses a boolean flag to prevent multi-incrementing round_number.

    Skips dead enemies and dead players (3 failed death saves).
    Unconscious players still get turns (for death saves).
    Advances round number when wrapping past the end of the order.

    Returns:
        Dict with current turn holder, initiative order, round number.

    Raises:
        RuntimeError: If not in combat or no initiative order.
    """
    if state.phase != "combat" or not state.turn.initiative_order:
        raise RuntimeError("Not in combat or no initiative order set")

    order = state.turn.initiative_order
    current = state.turn.current

    # Find current index
    try:
        current_idx = order.index(current) if current else -1
    except ValueError:
        current_idx = -1

    # I1 fix: Walk forward through the order, using a flag to avoid multi-increment
    wrapped = False
    for step in range(1, len(order) + 1):
        candidate_idx = (current_idx + step) % len(order)

        # Detect round wrap: we've gone past the end of the order
        if not wrapped and candidate_idx <= current_idx:
            wrapped = True
            state.turn.round_number += 1

        candidate = order[candidate_idx]

        # Check if candidate can act
        if _is_participant_active(state, candidate):
            state.turn.current = candidate
            _auto_save(state)
            return {
                "current": candidate,
                "initiative_order": order,
                "round": state.turn.round_number,
                "status": "turn_advanced",
            }

    # Everyone is dead/incapacitated
    state.turn.current = None
    _auto_save(state)
    return {
        "current": None,
        "initiative_order": order,
        "round": state.turn.round_number,
        "status": "no_active_participants",
    }


def _is_participant_active(state: GameState, name: str) -> bool:
    """Check if a combat participant can still take turns.

    Dead enemies (is_alive=False) are skipped.
    Truly dead players (3 failed death saves) are skipped.
    Unconscious players (0 HP, < 3 failures) still get turns for death saves.
    """
    # Check enemies
    for e in state.enemies:
        if e.id == name:
            return e.is_alive

    # Check players (by name, since initiative uses character names)
    for sid, p in state.players.items():
        if p.name == name:
            # Dead (3 failures) = skip. Unconscious = still gets turns.
            return p.death_saves.failures < 3

    # Unknown participant — assume active (safety fallback)
    return True


# ---------------------------------------------------------------------------
# Narrative logging
# ---------------------------------------------------------------------------

def _ensure_narrative_dir(state: GameState) -> str:
    """Ensure the narrative directory exists and return full path to current file."""
    narrative_dir = state._narrative_dir or "data/narrative"
    os.makedirs(narrative_dir, exist_ok=True)
    if not state.narrative_file:
        state.narrative_file = _new_narrative_filename(state.session_id)
    return os.path.join(narrative_dir, state.narrative_file)


def append_narrative(state: GameState, event_type: str, text: str) -> dict[str, Any]:
    """Append a narrative entry to the session's log file.

    Each entry is a timestamped line in markdown format:
        [2026-03-01 14:23] COMBAT | Kael attacks Goblin 1 with longsword.

    Args:
        state: The current GameState.
        event_type: Category (e.g., "COMBAT", "ROLEPLAY", "WORLD", "SYSTEM").
        text: The narrative text.

    Returns:
        Confirmation dict with the entry written.
    """
    filepath = _ensure_narrative_dir(state)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {event_type.upper()} | {text}\n"

    with open(filepath, "a") as f:
        f.write(entry)

    return {
        "file": state.narrative_file,
        "event_type": event_type.upper(),
        "text": text,
        "timestamp": timestamp,
        "status": "appended",
    }


def get_recent_narrative(state: GameState, n: int = 15) -> list[str]:
    """Retrieve the last N narrative entries from the current session's log.

    Args:
        state: The current GameState.
        n: Number of recent entries to return (default 15).

    Returns:
        List of narrative entry strings (most recent last).
    """
    filepath = _ensure_narrative_dir(state)
    if not os.path.exists(filepath):
        return []

    with open(filepath, "r") as f:
        lines = f.readlines()

    # Filter to non-empty lines that match the entry format
    entries = [line.rstrip("\n") for line in lines if line.strip()]
    return entries[-n:]


# ---------------------------------------------------------------------------
# Convenience: full state summary (for system prompt building)
# ---------------------------------------------------------------------------

def state_summary(state: GameState) -> str:
    """Generate a human-readable state summary for injection into the system prompt.

    Returns:
        A formatted string summarizing phase, location, players, enemies, scene.
    """
    lines: list[str] = []
    lines.append(f"**Session**: {state.session_id}")
    lines.append(f"**Phase**: {state.phase}")
    lines.append(f"**Location**: {state.current_location}")

    if state.players:
        lines.append("\n**Players**:")
        for sid, p in state.players.items():
            cond_str = ", ".join(c.name for c in p.conditions) if p.conditions else "none"
            lines.append(
                f"  - {p.name} ({p.char_class.title()} L{p.level}, "
                f"{p.hp}/{p.max_hp} HP, AC {p.ac}, pos: {p.position or '?'}, "
                f"conditions: {cond_str})"
            )

    if state.enemies:
        alive = [e for e in state.enemies if e.is_alive]
        if alive:
            lines.append("\n**Enemies**:")
            for e in alive:
                cond_str = ", ".join(c.name for c in e.conditions) if e.conditions else "none"
                lines.append(
                    f"  - {e.name} [{e.id}] ({e.hp}/{e.max_hp} HP, AC {e.ac}, "
                    f"pos: {e.position or '?'}, conditions: {cond_str})"
                )

    if state.scene.description:
        lines.append(f"\n**Scene**: {state.scene.description}")
        if state.scene.exits:
            exits_str = ", ".join(f"{d} → {loc}" for d, loc in state.scene.exits.items())
            lines.append(f"  Exits: {exits_str}")
        if state.scene.interactables:
            lines.append(f"  Interactables: {', '.join(state.scene.interactables)}")
        lines.append(f"  Light: {state.scene.light}")

    if state.phase == "combat" and state.turn.initiative_order:
        lines.append(
            f"\n**Combat**: Round {state.turn.round_number}, "
            f"Current turn: {state.turn.current}"
        )
        lines.append(f"  Initiative: {' → '.join(state.turn.initiative_order)}")

    if state.flags:
        lines.append("\n**Flags**:")
        for k, v in state.flags.items():
            if not k.startswith("item_"):  # Skip item metadata flags
                lines.append(f"  - {k}: {v}")

    return "\n".join(lines)
