"""
tools.py — DM tool definitions and implementations for QuestLine Agent DM.

Provides:
  - TOOL_DEFINITIONS: list[dict] — Claude tool-use schemas for the Anthropic API.
  - ToolExecutor: class that routes tool calls to implementations operating on GameState.

All tools cover dice/mechanics, character management, world management,
narrative/memory, story flags, combat management, and communication.
"""

from __future__ import annotations

import random
import re
from dataclasses import asdict
from typing import Any

from state import (
    GameState,
    PlayerState,
    EnemyState,
    SceneState,
    TurnState,
    ability_modifier,
    proficiency_bonus,
    load_state,
    save_state,
    add_player,
    update_player as state_update_player,
    deal_damage as state_deal_damage,
    heal as state_heal,
    add_condition as state_add_condition,
    remove_condition as state_remove_condition,
    give_item as state_give_item,
    take_item as state_take_item,
    spawn_enemy as state_spawn_enemy,
    remove_enemy as state_remove_enemy,
    set_scene as state_set_scene,
    set_flag as state_set_flag,
    get_flag as state_get_flag,
    start_combat as state_start_combat,
    end_combat as state_end_combat,
    next_turn as state_next_turn,
    append_narrative as state_append_narrative,
    get_recent_narrative as state_get_recent_narrative,
    state_summary,
    _find_target,
    _player_to_dict,
    _enemy_to_dict,
    _scene_to_dict,
    _turn_to_dict,
)


# ═══════════════════════════════════════════════════════════════════════════
# Enums — reused across tool schemas
# ═══════════════════════════════════════════════════════════════════════════

ABILITY_ENUM = [
    "strength", "dexterity", "constitution",
    "intelligence", "wisdom", "charisma",
]

DAMAGE_TYPE_ENUM = [
    "slashing", "piercing", "bludgeoning",
    "fire", "cold", "lightning", "poison",
    "psychic", "radiant", "necrotic",
    "force", "thunder", "acid",
]

CONDITION_ENUM = [
    "poisoned", "stunned", "prone", "blinded",
    "frightened", "restrained", "charmed", "paralyzed",
    "incapacitated", "invisible", "deafened", "grappled",
    "petrified", "unconscious",
]

LIGHT_ENUM = ["bright", "dim", "dark"]

EVENT_TYPE_ENUM = [
    "COMBAT", "ROLEPLAY", "WORLD",
    "DISCOVERY", "DEATH", "REST", "QUEST",
]


# ═══════════════════════════════════════════════════════════════════════════
# Tool Definitions — Anthropic API format
# ═══════════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: list[dict] = [
    # ── 1. Dice & Mechanics ──────────────────────────────────────────────
    {
        "name": "roll_dice",
        "description": (
            "Roll dice using standard notation. Supports: 'd20', '2d6+3', "
            "'4d6kh3' (keep highest 3), '4d6kl1' (keep lowest 1), "
            "'d20adv' (advantage), 'd20dis' (disadvantage), and compound "
            "expressions like '2d8+1d6+5'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notation": {
                    "type": "string",
                    "description": (
                        "Dice notation string, e.g. 'd20', '2d6+3', '4d6kh3', "
                        "'d20adv', '2d8+1d6+5'."
                    ),
                },
            },
            "required": ["notation"],
        },
    },
    {
        "name": "ability_check",
        "description": (
            "Make an ability check for a player: d20 + ability modifier vs DC. "
            "Reports success/failure, natural 20, and natural 1."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string", "description": "Character name."},
                "ability": {
                    "type": "string",
                    "enum": ABILITY_ENUM,
                    "description": "The ability score to check.",
                },
                "dc": {
                    "type": "integer",
                    "description": "Difficulty class to meet or exceed.",
                },
            },
            "required": ["player", "ability", "dc"],
        },
    },
    {
        "name": "saving_throw",
        "description": (
            "Make a saving throw for a player: d20 + ability modifier vs DC. "
            "Reports success/failure, natural 20, and natural 1."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string", "description": "Character name."},
                "ability": {
                    "type": "string",
                    "enum": ABILITY_ENUM,
                    "description": "The ability score for the save.",
                },
                "dc": {
                    "type": "integer",
                    "description": "Difficulty class to meet or exceed.",
                },
            },
            "required": ["player", "ability", "dc"],
        },
    },
    {
        "name": "attack_roll",
        "description": (
            "Make an attack roll. Looks up attacker's weapon/attack bonus and "
            "target's AC from game state. Rolls to hit; on hit, rolls damage. "
            "Handles critical hits (double dice) and fumbles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker": {
                    "type": "string",
                    "description": "Name of the attacking creature or player.",
                },
                "target": {
                    "type": "string",
                    "description": "Name of the target creature or player.",
                },
                "advantage": {
                    "type": "boolean",
                    "description": "Roll with advantage (2d20 keep highest).",
                },
                "disadvantage": {
                    "type": "boolean",
                    "description": "Roll with disadvantage (2d20 keep lowest).",
                },
            },
            "required": ["attacker", "target"],
        },
    },

    # ── 2. Character Management ──────────────────────────────────────────
    {
        "name": "register_player",
        "description": "Register a new player into the game. Use during lobby phase when a player wants to join.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sender_id": {"type": "string", "description": "The player's Signal ID (phone number or UUID)."},
                "name": {"type": "string", "description": "Character name chosen by the player."},
                "char_class": {
                    "type": "string",
                    "enum": ["fighter", "ranger", "cleric", "rogue", "bard", "wizard"],
                    "description": "Character class.",
                },
            },
            "required": ["sender_id", "name", "char_class"],
        },
    },
    {
        "name": "get_player",
        "description": "Retrieve the full character sheet for a player by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Character name (case-insensitive).",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_player",
        "description": (
            "Update fields on a player's character sheet. Can update any field: "
            "level, hp, max_hp, ac, abilities (partial dict merge), skills, "
            "inventory, position, spell_slots, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Character name (case-insensitive).",
                },
                "changes": {
                    "type": "object",
                    "description": (
                        "Dict of field names to new values. For 'abilities', "
                        "pass a partial dict to merge."
                    ),
                },
            },
            "required": ["name", "changes"],
        },
    },
    {
        "name": "deal_damage",
        "description": (
            "Deal damage to a target (player or enemy). Reduces HP (clamped to 0). "
            "Players at 0 HP become unconscious; enemies at 0 HP die."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the target."},
                "amount": {"type": "integer", "description": "Damage amount."},
                "damage_type": {
                    "type": "string",
                    "enum": DAMAGE_TYPE_ENUM,
                    "description": "Type of damage.",
                },
            },
            "required": ["target", "amount", "damage_type"],
        },
    },
    {
        "name": "heal",
        "description": (
            "Heal a target. Restores HP (clamped to max). Clears unconscious "
            "condition and resets death saves if HP goes above 0."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the target."},
                "amount": {"type": "integer", "description": "HP to restore."},
            },
            "required": ["target", "amount"],
        },
    },
    {
        "name": "add_condition",
        "description": (
            "Apply a status condition to a target. Optionally set duration in "
            "rounds (null for indefinite) and a source description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the target."},
                "condition": {
                    "type": "string",
                    "enum": CONDITION_ENUM,
                    "description": "The condition to apply.",
                },
                "duration": {
                    "type": ["integer", "null"],
                    "description": "Duration in rounds, or null for indefinite.",
                },
                "source": {
                    "type": ["string", "null"],
                    "description": "What caused this condition (e.g. 'poison trap', 'Hold Person spell').",
                },
            },
            "required": ["target", "condition"],
        },
    },
    {
        "name": "remove_condition",
        "description": "Remove a status condition from a target.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the target."},
                "condition": {
                    "type": "string",
                    "enum": CONDITION_ENUM,
                    "description": "The condition to remove.",
                },
            },
            "required": ["target", "condition"],
        },
    },
    {
        "name": "give_item",
        "description": "Give an item to a player's inventory. Optionally attach properties.",
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string", "description": "Character name."},
                "item": {"type": "string", "description": "Item name."},
                "properties": {
                    "type": ["object", "null"],
                    "description": (
                        "Optional item properties dict (damage, weight, magical, "
                        "description, etc.)."
                    ),
                },
            },
            "required": ["player", "item"],
        },
    },
    {
        "name": "take_item",
        "description": "Remove an item from a player's inventory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {"type": "string", "description": "Character name."},
                "item": {"type": "string", "description": "Item name to remove."},
            },
            "required": ["player", "item"],
        },
    },

    # ── 3. World Management ──────────────────────────────────────────────
    {
        "name": "get_scene",
        "description": (
            "Get the current scene: description, interactables, enemies, exits, "
            "lighting, and environment tags."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "set_scene",
        "description": (
            "Set the current scene with description, interactable objects, exits, "
            "lighting level, and environment tags."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Narrative description of the scene.",
                },
                "interactables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Interactable objects/features in the scene.",
                },
                "exits": {
                    "type": "object",
                    "description": (
                        'Direction → destination mapping, e.g. '
                        '{"north": "Dark Forest", "east": "River Crossing"}.'
                    ),
                },
                "location": {
                    "type": "string",
                    "description": (
                        "Name of the location the scene is set in. "
                        "Updates current_location in game state."
                    ),
                },
                "light": {
                    "type": "string",
                    "enum": LIGHT_ENUM,
                    "description": "Lighting level.",
                },
                "environment": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'Environment tags, e.g. ["underground", "damp"].',
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "spawn_enemy",
        "description": (
            "Spawn a new enemy into the current scene with stats and attacks. "
            "Returns an auto-generated enemy ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Enemy name (e.g. 'Goblin Archer').",
                },
                "hp": {"type": "integer", "description": "Hit points."},
                "ac": {"type": "integer", "description": "Armor class."},
                "attacks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Attack name."},
                            "bonus": {"type": "integer", "description": "Attack roll bonus."},
                            "damage": {"type": "string", "description": "Damage notation, e.g. '1d8+3'."},
                        },
                        "required": ["name", "bonus", "damage"],
                    },
                    "description": "List of attacks the enemy can make.",
                },
                "position": {
                    "type": ["string", "null"],
                    "description": "Optional position description in the scene.",
                },
            },
            "required": ["name", "hp", "ac", "attacks"],
        },
    },
    {
        "name": "remove_enemy",
        "description": "Remove an enemy from the scene (killed, fled, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "enemy_id": {
                    "type": "string",
                    "description": "The enemy's unique ID (e.g. 'enemy_1').",
                },
            },
            "required": ["enemy_id"],
        },
    },
    {
        "name": "get_enemies",
        "description": "List all current enemies in the scene with stats, HP, and conditions.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── 4. Narrative & Memory ────────────────────────────────────────────
    {
        "name": "get_narrative",
        "description": "Retrieve recent narrative log entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "last_n": {
                    "type": "integer",
                    "description": "Number of recent entries to return (default 10).",
                },
            },
        },
    },
    {
        "name": "append_narrative",
        "description": "Add a new entry to the narrative log with an event type and text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": EVENT_TYPE_ENUM,
                    "description": "Category of the narrative event.",
                },
                "text": {
                    "type": "string",
                    "description": "The narrative text to record.",
                },
            },
            "required": ["event_type", "text"],
        },
    },

    # ── 5. Story State (Flags) ───────────────────────────────────────────
    {
        "name": "set_flag",
        "description": (
            "Set a story/quest flag to track world state, quest progress, "
            "NPC attitudes, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Flag name (e.g. 'dragon_defeated', 'tavern_visited').",
                },
                "value": {
                    "description": "Value to store (string, number, boolean, list, or object).",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "get_flag",
        "description": "Get the value of a story/quest flag. Returns null if not set.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Flag name to look up.",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "list_flags",
        "description": "List all currently set story/quest flags and their values.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── 6. Combat Management ─────────────────────────────────────────────
    {
        "name": "start_combat",
        "description": (
            "Start a combat encounter. Rolls initiative for all players and the "
            "specified enemies. Returns the initiative order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "enemies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of enemy IDs (already spawned) to include in combat.",
                },
            },
            "required": ["enemies"],
        },
    },
    {
        "name": "end_combat",
        "description": "End the current combat encounter and return a summary.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "next_turn",
        "description": "Advance to the next turn in combat initiative order.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_initiative",
        "description": (
            "Get the current combat initiative state: turn order, current turn "
            "holder, round number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── 7. Communication ─────────────────────────────────────────────────
    {
        "name": "send_private",
        "description": "Send a private message to a specific player (DM whisper).",
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {
                    "type": "string",
                    "description": "Character name of the player to message.",
                },
                "message": {
                    "type": "string",
                    "description": "The private message content.",
                },
            },
            "required": ["player", "message"],
        },
    },
    {
        "name": "send_group_message",
        "description": (
            "Send a message to the game group chat. This is the ONLY way to communicate "
            "with the players. If you don't call this tool, no message is sent — that's "
            "how you stay silent. Use for narration, NPC dialogue, roll results, "
            "scene descriptions, or any response to player actions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to send to the group chat.",
                },
            },
            "required": ["message"],
        },
    },

    # ── 8. Phase Management ──────────────────────────────────────────────
    {
        "name": "set_phase",
        "description": (
            "Transition the game to a different phase. Valid phases: "
            "lobby, exploration, combat, rest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["lobby", "exploration", "combat", "rest"],
                    "description": "The phase to transition to.",
                },
            },
            "required": ["phase"],
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# Session Management Tool Definitions — used in lobby mode
# ═══════════════════════════════════════════════════════════════════════════

LOBBY_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_campaigns",
        "description": (
            "List all available campaign templates (bibles) that can be started "
            "as new sessions. Returns campaign names and descriptions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_sessions",
        "description": (
            "List all sessions (active and paused) for this group. "
            "Shows session IDs, campaign names, labels, and which is active."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "start_session",
        "description": (
            "Start a new campaign session for this group from a campaign template. "
            "Creates a fresh game state. Use list_campaigns first to see available options."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign": {
                    "type": "string",
                    "description": (
                        "Campaign template directory name (from list_campaigns 'directory' field)."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Optional human-readable label for this session.",
                },
            },
            "required": ["campaign"],
        },
    },
    {
        "name": "switch_session",
        "description": (
            "Switch to an existing paused session. "
            "Use list_sessions first to see available session IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to switch to (from list_sessions).",
                },
            },
            "required": ["session_id"],
        },
    },
    # Communication tools — same as campaign mode, available in lobby too
    {
        "name": "send_group_message",
        "description": (
            "Send a message to the group chat. "
            "This is the DM's only way to communicate with players."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to send.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "send_private",
        "description": "Send a private message to a specific player.",
        "input_schema": {
            "type": "object",
            "properties": {
                "player": {
                    "type": "string",
                    "description": "Player name or ID.",
                },
                "message": {
                    "type": "string",
                    "description": "The private message to send.",
                },
            },
            "required": ["player", "message"],
        },
    },
]


# Single meta-tool added to TOOL_DEFINITIONS during campaign mode
EXIT_TO_LOBBY_DEFINITION: dict = {
    "name": "exit_to_lobby",
    "description": (
        "Pause the current campaign session and return to the lobby. "
        "Players can then list campaigns, start a new session, or switch to "
        "a paused one. Call send_group_message first to announce the exit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Dice Parser
# ═══════════════════════════════════════════════════════════════════════════

# Matches a single dice group: [N]dS[khK|klK][adv|dis]
_DICE_GROUP_RE = re.compile(
    r"(\d*)d(\d+)"           # [count]d<sides>
    r"(?:(kh|kl)(\d+))?"    # optional keep highest/lowest N
    r"(adv|dis)?"            # optional advantage/disadvantage shorthand
)


def parse_and_roll(notation: str) -> dict[str, Any]:
    """Parse dice notation and roll.

    Handles:
      - "d20"           → roll 1d20
      - "2d6+3"         → roll 2d6, add 3
      - "4d6kh3"        → roll 4d6, keep highest 3
      - "4d6kl1"        → roll 4d6, keep lowest 1
      - "d20adv"        → roll 2d20, keep highest (advantage)
      - "d20dis"        → roll 2d20, keep lowest (disadvantage)
      - "2d8+1d6+5"     → multiple dice groups with modifier
      - "2d10-2"        → modifier can be negative

    Returns:
        {rolls: list[int], kept: list[int], total: int, notation: str}
    """
    clean = notation.strip().lower().replace(" ", "")

    # Plain number
    if re.fullmatch(r"[+-]?\d+", clean):
        val = int(clean)
        return {"rolls": [], "kept": [], "total": val, "notation": notation}

    all_rolls: list[int] = []
    all_kept: list[int] = []
    total = 0
    pos = 0
    sign = 1

    while pos < len(clean):
        ch = clean[pos]

        # Operator
        if ch == "+":
            sign = 1
            pos += 1
            continue
        if ch == "-":
            sign = -1
            pos += 1
            continue

        # Try matching a dice group at current position
        m = _DICE_GROUP_RE.match(clean, pos)
        if m:
            count_str, sides_str, keep_mode, keep_n_str, adv_dis = m.groups()
            count = int(count_str) if count_str else 1
            sides = int(sides_str)

            # Advantage / disadvantage shorthand
            if adv_dis == "adv":
                count = 2
                keep_mode = "kh"
                keep_n_str = "1"
            elif adv_dis == "dis":
                count = 2
                keep_mode = "kl"
                keep_n_str = "1"

            rolls = [random.randint(1, sides) for _ in range(count)]

            if keep_mode and keep_n_str:
                keep_n = min(int(keep_n_str), len(rolls))
                if keep_mode == "kh":
                    kept = sorted(rolls, reverse=True)[:keep_n]
                else:  # kl
                    kept = sorted(rolls)[:keep_n]
            else:
                kept = list(rolls)

            all_rolls.extend(rolls)
            all_kept.extend(kept)
            total += sign * sum(kept)
            sign = 1
            pos = m.end()
            continue

        # Try matching a plain integer modifier
        num_m = re.match(r"(\d+)", clean[pos:])
        if num_m:
            val = int(num_m.group(1))
            total += sign * val
            sign = 1
            pos += num_m.end()
            continue

        # Unknown character — skip
        pos += 1

    return {
        "rolls": all_rolls,
        "kept": all_kept,
        "total": total,
        "notation": notation,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tool Executor
# ═══════════════════════════════════════════════════════════════════════════

class ToolExecutor:
    """Routes tool calls to implementations and manages game state persistence."""

    def __init__(
        self,
        state: GameState | None,
        state_path: str,
        session_manager=None,
        group_id: str | None = None,
    ):
        self.state = state
        self.state_path = state_path
        self.session_manager = session_manager
        self.group_id = group_id
        # Outbox for private messages — the agent loop reads and delivers these
        self.private_messages: list[dict[str, str]] = []
        # Outbox for group messages — agent loop delivers these
        self.group_messages: list[str] = []
        # Signal to main.py that a session switch is needed after this turn
        self.session_switch_request: dict | None = None

    def _save(self) -> None:
        """Persist state to disk."""
        save_state(self.state, self.state_path)

    # ── Lookup helpers ───────────────────────────────────────────────────

    def _find_player_by_name(self, name: str) -> tuple[str, PlayerState]:
        """Find a player by character name (case-insensitive).

        Returns (sender_id, PlayerState).
        Raises KeyError if not found.
        """
        name_lower = name.lower()
        for sid, p in self.state.players.items():
            if p.name.lower() == name_lower:
                return sid, p
        raise KeyError(f"Player '{name}' not found")

    def _find_enemy_by_id_or_name(self, identifier: str) -> EnemyState:
        """Find an enemy by ID or name (case-insensitive).

        Returns EnemyState. Raises KeyError if not found.
        """
        # By ID
        for e in self.state.enemies:
            if e.id == identifier:
                return e
        # By name (first alive match)
        id_lower = identifier.lower()
        for e in self.state.enemies:
            if e.name.lower() == id_lower and e.is_alive:
                return e
        # By name (any match)
        for e in self.state.enemies:
            if e.name.lower() == id_lower:
                return e
        raise KeyError(f"Enemy '{identifier}' not found")

    # ── Dispatch ─────────────────────────────────────────────────────────

    # Tools that work without game state (lobby + session management)
    _STATELESS_TOOLS = frozenset({
        "list_campaigns", "list_sessions", "start_session", "switch_session",
        "send_group_message", "send_private", "exit_to_lobby",
    })

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to its implementation.

        Returns a JSON-serializable dict (tool result that Claude sees).
        """
        # Game-state tools require an active session
        if tool_name not in self._STATELESS_TOOLS and self.state is None:
            return {"error": f"No active session. Use start_session or switch_session first."}

        method = getattr(self, tool_name, None)
        if method is None:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return method(**tool_input)
        except KeyError as e:
            return {"error": str(e)}
        except (TypeError, ValueError) as e:
            return {"error": f"{tool_name}: {e}"}
        except Exception as e:
            return {"error": f"{tool_name} failed: {type(e).__name__}: {e}"}

    # ── 1. Dice & Mechanics ──────────────────────────────────────────────

    def roll_dice(self, notation: str) -> dict[str, Any]:
        """Roll dice using standard notation."""
        return parse_and_roll(notation)

    def ability_check(self, player: str, ability: str, dc: int) -> dict[str, Any]:
        """d20 + ability modifier vs DC."""
        _, p = self._find_player_by_name(player)
        score = p.abilities.get(ability.lower(), 10)
        mod = ability_modifier(score)

        roll = random.randint(1, 20)
        total = roll + mod

        return {
            "player": p.name,
            "ability": ability.lower(),
            "roll": roll,
            "modifier": mod,
            "total": total,
            "dc": dc,
            "success": total >= dc,
            "nat20": roll == 20,
            "nat1": roll == 1,
        }

    def saving_throw(self, player: str, ability: str, dc: int) -> dict[str, Any]:
        """d20 + ability modifier vs DC (saving throw)."""
        _, p = self._find_player_by_name(player)
        score = p.abilities.get(ability.lower(), 10)
        mod = ability_modifier(score)

        roll = random.randint(1, 20)
        total = roll + mod

        return {
            "player": p.name,
            "ability": ability.lower(),
            "roll": roll,
            "modifier": mod,
            "total": total,
            "dc": dc,
            "success": total >= dc,
            "nat20": roll == 20,
            "nat1": roll == 1,
        }

    def attack_roll(
        self,
        attacker: str,
        target: str,
        advantage: bool = False,
        disadvantage: bool = False,
    ) -> dict[str, Any]:
        """Full attack sequence: to-hit roll, damage on hit, crit handling.

        Looks up attacker stats and target AC from game state.
        """
        # Determine attacker's attack bonus, damage dice, damage type
        atk_bonus, damage_dice, damage_type, damage_mod = self._resolve_attacker(attacker)

        # Determine target AC
        target_ac, target_found = self._resolve_target_ac(target)
        if not target_found:
            return {"error": f"Target '{target}' not found in players or enemies. Use spawn_enemy first."}

        # Roll to hit
        if advantage and not disadvantage:
            r1, r2 = random.randint(1, 20), random.randint(1, 20)
            roll = max(r1, r2)
            hit_rolls = [r1, r2]
        elif disadvantage and not advantage:
            r1, r2 = random.randint(1, 20), random.randint(1, 20)
            roll = min(r1, r2)
            hit_rolls = [r1, r2]
        else:
            roll = random.randint(1, 20)
            hit_rolls = [roll]

        total = roll + atk_bonus
        crit = roll == 20
        fumble = roll == 1
        hit = (crit or (total >= target_ac)) and not fumble

        result: dict[str, Any] = {
            "attacker": attacker,
            "target": target,
            "hit_rolls": hit_rolls,
            "roll": roll,
            "modifier": atk_bonus,
            "total": total,
            "target_ac": target_ac,
            "hit": hit,
            "crit": crit,
            "fumble": fumble,
            "damage": 0,
            "damage_type": damage_type,
        }

        if hit:
            dmg = parse_and_roll(damage_dice)
            damage = dmg["total"] + damage_mod
            if crit:
                crit_extra = parse_and_roll(damage_dice)
                damage += crit_extra["total"]
            damage = max(0, damage)
            result["damage"] = damage
            result["damage_rolls"] = dmg["rolls"]

        return result

    def _resolve_attacker(self, name: str) -> tuple[int, str, str, int]:
        """Look up attack bonus, damage dice, damage type, and damage modifier.

        Returns (atk_bonus, damage_dice, damage_type, damage_mod).
        """
        # Try player
        name_lower = name.lower()
        for sid, p in self.state.players.items():
            if p.name.lower() == name_lower:
                str_mod = ability_modifier(p.abilities.get("strength", 10))
                dex_mod = ability_modifier(p.abilities.get("dexterity", 10))
                # Default to better of STR/DEX (simple weapon rules)
                best_mod = max(str_mod, dex_mod)
                atk_bonus = best_mod + p.proficiency

                # Check inventory for weapon properties in flags
                damage_dice = "1d4"
                damage_type = "bludgeoning"
                damage_mod = best_mod

                for item_name in p.inventory:
                    flag_key = f"item_{item_name.lower().replace(' ', '_')}_properties"
                    props = self.state.flags.get(flag_key)
                    if props and isinstance(props, dict) and props.get("damage"):
                        damage_dice = props["damage"]
                        damage_type = props.get("damage_type", "slashing")
                        # If damage string already contains a modifier
                        # (e.g. "1d8+3"), don't add ability mod on top
                        baked_in = "+" in damage_dice or "-" in damage_dice
                        if baked_in:
                            damage_mod = 0
                        elif props.get("finesse"):
                            damage_mod = max(str_mod, dex_mod)
                        elif props.get("ranged"):
                            damage_mod = dex_mod
                        else:
                            damage_mod = str_mod

                        # Ranged weapons always use DEX for attack bonus
                        if props.get("ranged"):
                            atk_bonus = dex_mod + p.proficiency
                        elif props.get("finesse"):
                            atk_bonus = best_mod + p.proficiency
                        break

                return atk_bonus, damage_dice, damage_type, damage_mod

        # Try enemy
        for e in self.state.enemies:
            if e.id.lower() == name_lower or e.name.lower() == name_lower:
                if e.attacks:
                    atk = e.attacks[0]
                    return atk.bonus, atk.damage, "slashing", 0
                return 0, "1d4", "bludgeoning", 0

        # Fallback
        return 0, "1d4", "bludgeoning", 0

    def _resolve_target_ac(self, name: str) -> tuple[int, bool]:
        """Look up a target's AC from state. Returns (ac, found)."""
        name_lower = name.lower()
        for sid, p in self.state.players.items():
            if p.name.lower() == name_lower:
                return p.ac, True
        for e in self.state.enemies:
            if e.id.lower() == name_lower or e.name.lower() == name_lower:
                return e.ac, True
        return 10, False

    # ── 2. Character Management ──────────────────────────────────────────

    def register_player(self, sender_id: str, name: str, char_class: str) -> dict[str, Any]:
        """Register a new player into the game."""
        player = add_player(self.state, sender_id, name=name, char_class=char_class)
        return {"player": _player_to_dict(player), "status": "registered"}

    def get_player(self, name: str) -> dict[str, Any]:
        """Full character sheet for a player."""
        _, p = self._find_player_by_name(name)
        return {"player": _player_to_dict(p)}

    def update_player(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Update fields on a player's character sheet."""
        sid, _ = self._find_player_by_name(name)
        updated = state_update_player(self.state, sid, **changes)
        # state_update_player auto-saves
        return {"player": _player_to_dict(updated)}

    def deal_damage(self, target: str, amount: int, damage_type: str) -> dict[str, Any]:
        """Deal damage to a target. State auto-saves."""
        return state_deal_damage(self.state, target, amount, damage_type)

    def heal(self, target: str, amount: int) -> dict[str, Any]:
        """Heal a target. State auto-saves."""
        return state_heal(self.state, target, amount)

    def add_condition(
        self, target: str, condition: str, duration: int | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Add a condition to a target. State auto-saves."""
        return state_add_condition(self.state, target, condition, duration, source=source)

    def remove_condition(self, target: str, condition: str) -> dict[str, Any]:
        """Remove a condition from a target. State auto-saves."""
        return state_remove_condition(self.state, target, condition)

    def give_item(
        self, player: str, item: str, properties: dict | None = None
    ) -> dict[str, Any]:
        """Give an item to a player. State auto-saves."""
        return state_give_item(self.state, player, item, properties)

    def take_item(self, player: str, item: str) -> dict[str, Any]:
        """Remove an item from a player. State auto-saves."""
        return state_take_item(self.state, player, item)

    # ── 3. World Management ──────────────────────────────────────────────

    def get_scene(self) -> dict[str, Any]:
        """Current scene with enemies included."""
        scene_dict = _scene_to_dict(self.state.scene)
        # Include enemy list
        scene_dict["enemies"] = [
            _enemy_to_dict(e) for e in self.state.enemies if e.is_alive
        ]
        return scene_dict

    def set_scene(
        self,
        description: str,
        interactables: list[str] | None = None,
        exits: dict[str, str] | None = None,
        light: str | None = None,
        environment: list[str] | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Set the current scene. State auto-saves."""
        if location is not None:
            self.state.current_location = location
        kwargs: dict[str, Any] = {}
        if light is not None:
            kwargs["light"] = light
        if environment is not None:
            kwargs["environment"] = environment
        result = state_set_scene(
            self.state,
            description=description,
            interactables=interactables,
            exits=exits,
            **kwargs,
        )
        if location is not None:
            result["location"] = location
        return result

    def spawn_enemy(
        self,
        name: str,
        hp: int,
        ac: int,
        attacks: list[dict[str, Any]],
        position: str | None = None,
    ) -> dict[str, Any]:
        """Spawn an enemy. State auto-saves."""
        enemy_id = state_spawn_enemy(self.state, name, hp, ac, attacks, position)
        return {"enemy_id": enemy_id, "name": name}

    def remove_enemy(self, enemy_id: str) -> dict[str, Any]:
        """Remove an enemy. State auto-saves."""
        return state_remove_enemy(self.state, enemy_id)

    def get_enemies(self) -> dict[str, Any]:
        """List all enemies (alive and dead)."""
        return {
            "enemies": [_enemy_to_dict(e) for e in self.state.enemies],
        }

    # ── 4. Narrative & Memory ────────────────────────────────────────────

    def get_narrative(self, last_n: int = 10) -> dict[str, Any]:
        """Recent narrative log entries."""
        entries = state_get_recent_narrative(self.state, n=last_n)
        return {"entries": entries, "count": len(entries)}

    def append_narrative(self, event_type: str, text: str) -> dict[str, Any]:
        """Append a narrative entry. State auto-saves."""
        return state_append_narrative(self.state, event_type, text)

    # ── 5. Story Flags ───────────────────────────────────────────────────

    def set_flag(self, key: str, value: Any) -> dict[str, Any]:
        """Set a story flag. State auto-saves."""
        return state_set_flag(self.state, key, value)

    def get_flag(self, key: str) -> dict[str, Any]:
        """Get a story flag value."""
        return {"key": key, "value": state_get_flag(self.state, key)}

    def list_flags(self) -> dict[str, Any]:
        """All story flags and their values."""
        return {"flags": dict(self.state.flags)}

    # ── 6. Combat Management ─────────────────────────────────────────────

    def start_combat(self, enemies: list[str]) -> dict[str, Any]:
        """Start combat with specified enemy IDs.

        Builds an enemies_initiative dict from enemy DEX (approximated from
        first attack bonus) and delegates to state_start_combat.
        """
        # Build initiative bonuses for the specified enemies
        enemy_init: dict[str, int] = {}
        for eid in enemies:
            for e in self.state.enemies:
                if e.id == eid:
                    # Use first attack bonus as rough DEX-proxy, or 0
                    bonus = e.attacks[0].bonus if e.attacks else 0
                    enemy_init[eid] = bonus
                    break

        # I4 fix: pass include_enemies to filter which enemies participate
        return state_start_combat(
            self.state,
            enemies_initiative=enemy_init,
            include_enemies=enemies,
        )

    def end_combat(self) -> dict[str, Any]:
        """End combat. State auto-saves."""
        return state_end_combat(self.state)

    def next_turn(self) -> dict[str, Any]:
        """Advance to the next turn. State auto-saves."""
        return state_next_turn(self.state)

    def get_initiative(self) -> dict[str, Any]:
        """Current initiative state."""
        if self.state.phase != "combat":
            return {"error": "Not in combat."}
        return _turn_to_dict(self.state.turn)

    # ── 7. Communication ─────────────────────────────────────────────────

    def send_private(self, player: str, message: str) -> dict[str, Any]:
        """Queue a private message for a player. Agent loop delivers it.

        C1 fix: resolve character name to sender_id before queuing.
        In lobby mode (no state), player name lookup is unavailable.
        """
        if self.state is None:
            return {"error": "Cannot send private messages in lobby mode (no player roster)"}
        sender_id = next(
            (sid for sid, p in self.state.players.items() if p.name.lower() == player.lower()),
            None,
        )
        if sender_id is None:
            return {"error": f"No player named '{player}' found"}
        self.private_messages.append({"player_id": sender_id, "message": message})
        return {"sent": True, "player": player, "player_id": sender_id, "message": message}

    def send_group_message(self, message: str) -> dict[str, Any]:
        """Queue a message to be sent to the game group chat.
        
        This is the DM's only way to communicate with players.
        Not calling this tool means choosing to stay silent.
        """
        self.group_messages.append(message)
        return {"sent": True, "message_length": len(message)}

    # ── 8. Phase Management ──────────────────────────────────────────────

    def set_phase(self, phase: str) -> dict[str, Any]:
        """Transition the game to a different phase."""
        valid_phases = ("lobby", "exploration", "combat", "rest")
        if phase not in valid_phases:
            return {"error": f"Invalid phase '{phase}'. Must be one of: {valid_phases}"}
        old_phase = self.state.phase
        self.state.phase = phase
        save_state(self.state, self.state_path)
        return {"old_phase": old_phase, "new_phase": phase, "status": "phase_changed"}

    # ── 9. Session Management ─────────────────────────────────────────────

    def list_campaigns(self) -> dict[str, Any]:
        """List available campaign bibles."""
        if not self.session_manager:
            return {"error": "Session management not available"}
        campaigns = self.session_manager.list_campaigns()
        return {"campaigns": campaigns}

    def list_sessions(self) -> dict[str, Any]:
        """List all sessions for this group."""
        if not self.session_manager or not self.group_id:
            return {"error": "Session management not available"}
        ctx = self.session_manager.load_group_meta(self.group_id)
        sessions = [
            {
                "session_id": sid,
                "campaign": s.campaign,
                "label": s.label,
                "created": s.created,
                "active": (sid == ctx.active_session_id),
            }
            for sid, s in ctx.sessions.items()
        ]
        return {"sessions": sessions, "active_session_id": ctx.active_session_id}

    def start_session(self, campaign: str, label: str | None = None) -> dict[str, Any]:
        """Create a new session from a campaign template."""
        if not self.session_manager or not self.group_id:
            return {"error": "Session management not available"}
        try:
            info = self.session_manager.create_session(self.group_id, campaign, label)
            self.session_switch_request = {
                "action": "switch",
                "session_id": info.session_id,
            }
            return {
                "status": "session_created",
                "session_id": info.session_id,
                "campaign": info.campaign,
                "label": info.label,
                "note": "Session activates after this turn ends.",
            }
        except (ValueError, FileNotFoundError) as e:
            return {"error": str(e)}

    def switch_session(self, session_id: str) -> dict[str, Any]:
        """Switch to an existing paused session."""
        if not self.session_manager or not self.group_id:
            return {"error": "Session management not available"}
        try:
            info = self.session_manager.switch_session(self.group_id, session_id)
            self.session_switch_request = {
                "action": "switch",
                "session_id": session_id,
            }
            return {
                "status": "session_switched",
                "session_id": session_id,
                "campaign": info.campaign,
                "label": info.label,
                "note": "Session activates after this turn ends.",
            }
        except KeyError as e:
            return {"error": str(e)}

    def exit_to_lobby(self) -> dict[str, Any]:
        """Pause the current session and return to lobby mode."""
        if not self.session_manager or not self.group_id:
            return {"error": "Session management not available"}
        self.session_manager.deactivate_session(self.group_id)
        self.session_switch_request = {"action": "lobby"}
        return {"status": "exited_to_lobby", "note": "Lobby mode activates after this turn ends."}
