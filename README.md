# QuestLine Agent DM

An AI Dungeon Master that runs D&D campaigns in Signal group chats. Uses Claude or Gemini as the LLM backend, with full tool use (dice rolls, combat, inventory, narrative logging) and persistent game state.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **[signal-cli](https://github.com/AsamK/signal-cli)** — Signal messenger CLI (requires Java 21+)
- A **phone number** registered with Signal for the bot

## Quick Start

```bash
git clone <repo-url> && cd questline-dm
./setup.sh                  # creates venv, installs deps, copies config templates
```

Then edit the two config files:

1. **`.env`** — add your API key
2. **`config.yaml`** — set your bot's phone number and allowed group IDs

Run:

```bash
source .venv/bin/activate
python main.py
```

## Signal Setup

The bot communicates via [signal-cli](https://github.com/AsamK/signal-cli) running as a local daemon.

### 1. Install signal-cli

Download from [releases](https://github.com/AsamK/signal-cli/releases) or install via your package manager. Requires Java 21+.

```bash
# Example: extract to ~/.local/bin
tar xf signal-cli-0.13.24-Linux.tar.gz
mv signal-cli-0.13.24/bin/signal-cli ~/.local/bin/
mv signal-cli-0.13.24/lib ~/.local/lib/signal-cli
```

### 2. Register a phone number

```bash
signal-cli -a +1XXXXXXXXXX register
signal-cli -a +1XXXXXXXXXX verify CODE
```

### 3. Run signal-cli as a daemon

Manually:

```bash
signal-cli -a +1XXXXXXXXXX daemon --http 127.0.0.1:8080 --tcp 127.0.0.1:7583 --no-receive-stdout
```

Or install the systemd user service:

```bash
# Edit services/signal-cli.service — replace __BOT_NUMBER__ with your number
cp services/signal-cli.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now signal-cli
```

### 4. Find your group ID

Add the bot's phone number to a Signal group. Send a message, then check the bot's logs:

```bash
grep "non-allowed group" data/questline-dm.log
```

Copy the group ID into `config.yaml` under `allowed_groups`.

## Running as a Service

Edit `services/questline-dm.service` — replace `__REPO_PATH__` with the absolute path to this repo.

```bash
cp services/questline-dm.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now questline-dm
```

Check logs:

```bash
journalctl --user -u questline-dm -f
# or
tail -f data/questline-dm.log
```

## Configuration

### config.yaml

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | `"anthropic"` | LLM provider: `"anthropic"` or `"gemini"` |
| `model` | `"claude-sonnet-4-6"` | Main model for DM responses |
| `triage_model` | `"claude-haiku-4-5-20251001"` | Cheap model for filtering banter |
| `triage_enabled` | `true` | Skip main model for non-game messages |
| `max_tokens` | `1024` | Max output tokens per response |
| `bot_number` | — | Signal phone number (e.g. `"+15551234567"`) |
| `allowed_groups` | `[]` | Signal group IDs that can interact with the bot |
| `tool_clear_threshold` | `35000` | Input tokens before clearing old tool results |
| `compaction_threshold` | `60000` | Input tokens before compacting context |

### .env

```
ANTHROPIC_API_KEY=sk-ant-...    # for provider: "anthropic"
GEMINI_API_KEY=AIza...          # for provider: "gemini"
```

## Switching Providers

Change `provider`, `model`, and `triage_model` in `config.yaml`:

**Anthropic (Claude):**
```yaml
provider: "anthropic"
model: "claude-sonnet-4-6"
triage_model: "claude-haiku-4-5-20251001"
```

**Google (Gemini):**
```yaml
provider: "gemini"
model: "gemini-2.5-flash"
triage_model: "gemini-2.5-flash"
```

Existing game state and conversation history are preserved when switching — the provider translates on the fly.

## Campaigns

Campaigns live in `campaigns/<name>/`:

```
campaigns/infinite-costco/
  meta.yaml       # name, description
  world.md        # setting, lore, tone
  npcs/           # NPC descriptions
    greeter.md
    manager.md
```

The DM loads `world.md` into its system prompt and uses tools to manage game state (dice, combat, items, quests, narrative).

## Project Structure

```
main.py              Entry point, Signal listener, message batching
agent.py             LLM agent loop, system prompt, tool routing
signal_io.py         Signal TCP listener + HTTP RPC sender
tools.py             Tool definitions + executor
state.py             Game state dataclasses + persistence
session_manager.py   Multi-group session management
providers/           LLM provider abstraction (Anthropic, Gemini)
prompts/             System prompt templates (system.md, rules.md)
campaigns/           Campaign content (world, NPCs, quests)
data/                Runtime data (game state, logs, narrative)
services/            systemd service templates
```
