"""
main.py — Entry point for QuestLine Agent DM.
Loads config, initializes state, starts Signal listener + agent loop.

Multi-group routing: incoming messages are routed to the correct group's
agent based on group_id. Each group has its own buffer, debounce timer,
lock, and AgentDM instance.

Two modes per group:
- Lobby mode (no active session): session management tools only
- Campaign mode (active session): game tools + exit_to_lobby

Message batching: incoming messages are buffered and processed together
after a 3-second debounce window per group.

Messaging architecture: the DM sends messages ONLY via the send_group_message
tool. Claude's raw text response is never auto-sent.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from pathlib import Path
import sys
from dataclasses import dataclass, field

from state import load_state, load_config
from agent import AgentDM
from signal_io import SignalIO
from session_manager import SessionManager


def _load_dotenv(path: str = ".env") -> None:
    """Load key=value pairs from a .env file into os.environ (setdefault)."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

log = logging.getLogger("questline")

DEBOUNCE_SECONDS = 3.0  # Wait this long after last message before processing


@dataclass
class GroupRuntime:
    """Per-group runtime state. Created on first message from a group."""
    group_id: str
    message_buffer: list[tuple[str, str]] = field(default_factory=list)
    buffer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    game_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    debounce_task: asyncio.Task | None = None
    agent: AgentDM | None = None      # None = lobby mode
    state: object | None = None       # GameState or None


def setup_logging() -> None:
    """Configure root 'questline' logger with stderr + rotating file output."""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    log.addHandler(stderr)

    os.makedirs("data", exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        "data/questline-dm.log", maxBytes=2_000_000, backupCount=2
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)


def _create_agent(
    config: dict,
    session_mgr: SessionManager,
    group_id: str,
    session_id: str,
) -> tuple[AgentDM, object]:
    """Create an AgentDM for an active session. Returns (agent, state)."""
    paths = session_mgr.session_paths(group_id, session_id)
    state = load_state(paths["state_file"], paths["narrative_dir"])
    agent = AgentDM(
        config=config,
        state=state,
        paths=paths,
        session_manager=session_mgr,
        group_id=group_id,
    )
    return agent, state


def _create_lobby_agent(
    config: dict,
    session_mgr: SessionManager,
    group_id: str,
) -> AgentDM:
    """Create a lobby-mode AgentDM (no game state, session tools only)."""
    return AgentDM(
        config=config,
        state=None,
        paths=None,
        session_manager=session_mgr,
        group_id=group_id,
    )


async def run() -> None:
    _load_dotenv()

    config_path = os.environ.get("QUESTLINE_CONFIG", "config.yaml")
    config = load_config(config_path)

    provider = config.get("provider", "anthropic")
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)
    elif provider == "gemini" and not (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ):
        log.error("GEMINI_API_KEY or GOOGLE_API_KEY not set")
        sys.exit(1)

    session_mgr = SessionManager(config)
    signal = SignalIO(config)

    # Per-group runtime state, keyed by Signal group ID
    groups: dict[str, GroupRuntime] = {}

    def get_or_create_group(group_id: str) -> GroupRuntime:
        """Get existing GroupRuntime or create one, loading active session if any."""
        if group_id in groups:
            return groups[group_id]

        gr = GroupRuntime(group_id=group_id)

        # Try to load active session
        active = session_mgr.get_active_session(group_id)
        if active:
            try:
                agent, state = _create_agent(config, session_mgr, group_id, active.session_id)
                gr.agent = agent
                gr.state = state
                log.info(
                    "Loaded active session '%s' for group %s",
                    active.session_id, session_mgr.group_hash(group_id),
                )
            except Exception:
                log.exception("Failed to load session '%s' — starting in lobby", active.session_id)
        else:
            log.info("Group %s has no active session — lobby mode", session_mgr.group_hash(group_id))

        groups[group_id] = gr
        return gr

    async def process_batch(gr: GroupRuntime) -> None:
        """Process all buffered messages as a single batch."""
        async with gr.buffer_lock:
            if not gr.message_buffer:
                return
            batch = list(gr.message_buffer)
            gr.message_buffer.clear()

        log.info(
            "Processing batch of %d message(s) for group %s: %s",
            len(batch),
            session_mgr.group_hash(gr.group_id)[:8],
            ", ".join(f"{sid[:20]}:{txt[:30]}" for sid, txt in batch),
        )

        async with gr.game_lock:
            # Ensure we have an agent (lobby or campaign)
            agent = gr.agent
            if agent is None:
                agent = _create_lobby_agent(config, session_mgr, gr.group_id)
                gr.agent = agent

            # Build batch with resolved player names
            named_batch = []
            for sender_id, text in batch:
                player_name = agent.get_player_name(sender_id)
                named_batch.append((sender_id, player_name, text))

            # Wire up immediate message delivery during the turn
            async def _deliver_group(msg: str) -> None:
                await signal.send_group_message(gr.group_id, msg)

            agent._message_callback = _deliver_group

            try:
                responses = await agent.handle_batch(named_batch)
            except Exception:
                log.exception("Agent error processing batch")
                responses = [
                    "*The DM's skull cracks ominously.* Something went wrong."
                ]

            # Deliver messages
            for resp in responses:
                if resp.startswith("PRIVATE:"):
                    parts = resp.split(":", 2)
                    if len(parts) == 3:
                        await signal.send_private_message(parts[1], parts[2])
                else:
                    await signal.send_group_message(gr.group_id, resp)

            if not responses:
                log.info("DM chose silence — no messages to deliver")

            # Handle session switch signals AFTER messages are delivered
            if agent.session_switch_request:
                req = agent.session_switch_request
                if req["action"] == "lobby":
                    log.info("Group %s exiting to lobby", session_mgr.group_hash(gr.group_id)[:8])
                    gr.agent = None
                    gr.state = None
                elif req["action"] == "switch":
                    new_session_id = req["session_id"]
                    log.info(
                        "Group %s switching to session '%s'",
                        session_mgr.group_hash(gr.group_id)[:8],
                        new_session_id,
                    )
                    try:
                        new_agent, new_state = _create_agent(
                            config, session_mgr, gr.group_id, new_session_id
                        )
                        gr.agent = new_agent
                        gr.state = new_state
                    except Exception:
                        log.exception("Failed to switch to session '%s'", new_session_id)
                        gr.agent = None
                        gr.state = None

    async def on_message(group_id: str, sender_id: str, text: str) -> None:
        """Buffer an incoming message and reset the debounce timer."""
        gr = get_or_create_group(group_id)

        async with gr.buffer_lock:
            gr.message_buffer.append((sender_id, text))
            log.info(
                "Buffered message from %s in group %s (buffer size: %d)",
                sender_id[:20],
                session_mgr.group_hash(group_id)[:8],
                len(gr.message_buffer),
            )

        # Cancel existing debounce timer and start a new one
        if gr.debounce_task and not gr.debounce_task.done():
            gr.debounce_task.cancel()

        async def debounce_fire():
            await asyncio.sleep(DEBOUNCE_SECONDS)
            # Shield from cancellation — once processing starts, it must
            # complete. New messages arriving mid-batch get buffered and
            # picked up by the next debounce cycle.
            try:
                await asyncio.shield(process_batch(gr))
            except asyncio.CancelledError:
                pass  # Debounce was reset, but processing continues

        gr.debounce_task = asyncio.create_task(debounce_fire())

    log.info("QuestLine Agent DM starting")
    log.info(
        "Bot: %s | Model: %s | Allowed groups: %d",
        config["bot_number"],
        config.get("model"),
        len(config.get("allowed_groups", [])),
    )
    log.info("Debounce: %.1fs", DEBOUNCE_SECONDS)

    campaigns = session_mgr.list_campaigns()
    log.info("Available campaigns: %s", [c["directory"] for c in campaigns])

    await signal.start_listener(on_message)


def main() -> None:
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
