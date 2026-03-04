# 5e Lite — Rules Reference

## Core Mechanic
Roll d20 + modifier vs target number. ≥ target = success. Natural 20 always hits, natural 1 always misses.

## Abilities (6)
STR (melee, athletics), DEX (ranged, stealth, initiative), CON (HP, concentration), INT (arcana, investigation), WIS (perception, insight), CHA (persuasion, deception)

**Modifier** = (score - 10) / 2, round down. Proficiency bonus = +2 (levels 1-4), +3 (5-8), +4 (9-12).

## Combat
- **Initiative**: d20 + DEX mod. Highest goes first.
- **Attack**: d20 + ability mod + proficiency (if proficient) vs target AC.
- **Damage**: Weapon die + ability mod. Crits (nat 20) = double dice.
- **Actions per turn**: 1 action + 1 bonus action + movement (30 ft default).
- **Action options**: Attack, Cast Spell, Dash (double move), Dodge (attacks have disadvantage), Help, Hide, Disengage, Use Item.

## HP & Death
- HP = class hit die + CON mod per level.
- 0 HP = unconscious. Death saves: d20 each turn, 10+ = success, <10 = failure. 3 successes = stable, 3 failures = dead. Nat 20 = regain 1 HP. Nat 1 = 2 failures.
- Healing: Potions (2d4+2), spells, short rest (spend hit dice), long rest (full restore).

## Advantage / Disadvantage
Roll 2d20, take higher (advantage) or lower (disadvantage). They cancel out.

## Ability Checks
d20 + ability mod + proficiency (if applicable) vs DC. Easy=10, Medium=13, Hard=16, Very Hard=19, Nearly Impossible=22.

## Classes

### Fighter (d10 HP)
All armor and weapons. Frontline tank/damage.
- **Action Surge** (1/rest): Take an extra action this turn.
- **Second Wind** (1/rest): Heal d10 + level as bonus action.

### Rogue (d8 HP)
Light armor, finesse and ranged weapons. Sneaky damage dealer.
- **Sneak Attack**: +d6 damage per 2 levels when you have advantage or an ally is adjacent to target.
- **Cunning Action** (bonus): Dash, Disengage, or Hide.

### Wizard (d6 HP)
No armor. Arcane caster with powerful but fragile spells.
- **Arcane Recovery** (1/rest): Recover spent spell slots (up to half your level, rounded up).
- Primary ability: INT.

### Cleric (d8 HP)
Medium armor, shields. Healer and divine caster.
- **Turn Undead**: Undead within 30ft make WIS save or flee for 1 minute.
- Primary ability: WIS.

### Ranger (d10 HP)
Medium armor. Tracker, survivalist, ranged specialist.
- **Favored Enemy**: +2 damage vs chosen enemy type.
- **Hunter's Mark** (bonus, concentration): +d6 damage to marked target.
- Primary ability: DEX.

### Bard (d8 HP)
Light armor. Support, inspiration, and debuffs.
- **Bardic Inspiration** (bonus): Give an ally a d6 to add to their next roll.
- Primary ability: CHA.

## Spellcasting

### Basics
- Cantrips: At will, no cost.
- Leveled spells: Cost a spell slot of that level or higher. Slots refresh on long rest.
- Concentration: Only one concentration spell at a time. CON save (DC 10 or half damage taken) when hit.
- Spell attack: d20 + spellcasting mod + proficiency vs AC.
- Spell save DC: 8 + spellcasting mod + proficiency.

### Core Spell List

**Cantrips (at will):**
| Spell | Effect | Classes |
|-------|--------|---------|
| Fire Bolt | Ranged spell attack, d10 fire damage | Wizard |
| Sacred Flame | DEX save or d8 radiant damage (ignores cover) | Cleric |
| Vicious Mockery | WIS save or d4 psychic + disadvantage on next attack | Bard |
| Mage Hand | Telekinetic hand, 30ft range, 10 lbs | Wizard |
| Minor Illusion | Create a small sound or image | Wizard, Bard |
| Guidance | Touch, +d4 to one ability check | Cleric |

**Level 1 (slot cost: 1):**
| Spell | Effect | Classes |
|-------|--------|---------|
| Magic Missile | Auto-hit, 3 darts of d4+1 force damage | Wizard |
| Shield | Reaction, +5 AC until next turn | Wizard |
| Sleep | 5d8 HP of creatures fall asleep (lowest HP first) | Wizard, Bard |
| Healing Word | Bonus action, d4 + mod HP, 60ft range | Cleric, Bard |
| Cure Wounds | Action, d8 + mod HP, touch | Cleric |
| Bless | Concentration, up to 3 allies get +d4 to attacks and saves | Cleric |
| Faerie Fire | Concentration, DEX save or outlined (attacks have advantage) | Bard |
| Entangle | Concentration, STR save or restrained in 20ft area | Ranger |

**Level 2 (slot cost: 2):**
| Spell | Effect | Classes |
|-------|--------|---------|
| Misty Step | Bonus action, teleport 30ft | Wizard |
| Spiritual Weapon | Bonus action, summon weapon (d8 + mod force), lasts 1 min | Cleric |
| Heat Metal | Concentration, target holding metal takes 2d8 fire + drops it (CON save) | Bard |
| Hold Person | Concentration, WIS save or paralyzed (save each turn) | Wizard, Cleric, Bard |
| Invisibility | Concentration, target is invisible until they attack or cast | Wizard, Bard |
| Spike Growth | Concentration, 20ft area, 2d4 piercing per 5ft moved | Ranger |

**Level 3 (slot cost: 3):**
| Spell | Effect | Classes |
|-------|--------|---------|
| Fireball | 20ft radius, DEX save, 8d6 fire (half on save) | Wizard |
| Counterspell | Reaction, negate a spell being cast (auto if same level or lower) | Wizard |
| Revivify | Touch, raise dead creature (died within 1 min) to 1 HP | Cleric |
| Spirit Guardians | Concentration, 15ft aura, WIS save, 3d8 radiant | Cleric |
| Conjure Animals | Concentration, summon beasts to fight for you | Ranger |

### Improv Spell Guidelines
Players may attempt spells not on this list. The DM should adjudicate using these guidelines:
- **Damage spells**: d6 per spell level (single target) or d4 per spell level (area). Save for half.
- **Buffs**: +2 bonus or advantage, last 1 minute or until concentration ends.
- **Debuffs**: Save to resist, effect lasts 1 round (no save = weaker effect).
- **Healing**: d6 per spell level + spellcasting mod.
- **Utility**: If it's clever and fun, let it work. Set a DC and let them roll.
- **Rule of cool**: If a player describes something awesome, lean toward "yes, and..." with appropriate costs.
- **Consistency**: Once you establish what an improvised spell does, it should work the same way next time.

## Conditions

Use `add_condition` / `remove_condition` to track these mechanically.

| Condition | Effect |
|-----------|--------|
| Prone | Disadvantage on attacks. Melee attacks against have advantage, ranged have disadvantage. Costs half movement to stand. |
| Restrained | Speed 0. Attacks against have advantage. Target's attacks and DEX saves at disadvantage. |
| Stunned | Incapacitated, can't move, auto-fail STR/DEX saves. Attacks against have advantage. |
| Poisoned | Disadvantage on attacks and ability checks. |
| Frightened | Disadvantage on ability checks and attacks while source is visible. Can't willingly move closer to source. |
| Blinded | Auto-fail sight checks. Attacks at disadvantage. Attacks against have advantage. |
| Charmed | Can't attack charmer. Charmer has advantage on social checks against target. |
| Paralyzed | As stunned, plus melee hits within 5 ft are auto-crits. |
| Invisible | Attacks against at disadvantage. Target's attacks have advantage. |
| Grappled | Speed 0. Ends if grappler is incapacitated or target is moved out of reach. |

Conditions end when their source ends, or as specified (save each turn, after 1 minute, etc.).

## Items
- Healing Potion: 2d4+2 HP
- Leather: AC 11+DEX | Chain: AC 16 | Plate: AC 18 (stealth disadvantage)
- Dagger d4 | Shortsword d6 | Longsword d8 | Greataxe d12 | Longbow d8 | Crossbow d10
