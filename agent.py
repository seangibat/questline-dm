"""
agent.py — LLM agent loop for QuestLine Agent DM.
Builds system prompt, sends player messages to LLM with tools,
executes tool calls in a loop, returns final narration.

Features:
- Split prompt caching: static rules/lore cached once per session,
  dynamic game state passed as a separate uncached block each turn.
- Haiku triage: cheap pre-filter skips Sonnet for banter/non-game messages.
- Two-stage server-side context management: tool-result clearing
  (clear_tool_uses_20250919) prunes bulk history at 35k tokens, then
  compaction (compact-2026-01-12) summarises the remainder at 60k.
- Full tool call/result history preserved in conversation cache so
  Claude remembers its own actions in subsequent turns.
- System prompt built once per turn, never per tool iteration.
- Message batching: handle_batch processes multiple player messages
  in one turn.
- Explicit messaging: DM sends messages via send_group_message tool only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from providers import create_provider, ContentBlock

from state import (
    GameState,
    load_state,
    save_state,
    state_summary,
    get_recent_narrative,
    load_config,
)
from tools import (
    TOOL_DEFINITIONS,
    LOBBY_TOOL_DEFINITIONS,
    EXIT_TO_LOBBY_DEFINITION,
    ToolExecutor,
)

log = logging.getLogger("questline.agent")


def _serialize_block(block) -> dict:
    """Convert an SDK content block (Pydantic model, dataclass, or dict) to a plain dict."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):   # Pydantic v2 (anthropic SDK ≥0.20)
        return block.model_dump()
    if hasattr(block, "dict"):         # Pydantic v1 fallback
        return block.dict()
    # Dataclass or other object — filter out None values for clean API payloads
    return {k: v for k, v in vars(block).items() if v is not None}


def _serialize_message(msg: dict) -> dict:
    """Return a JSON-serializable copy of a conversation message."""
    content = msg["content"]
    if isinstance(content, list):
        content = [_serialize_block(b) for b in content]
    return {"role": msg["role"], "content": content}


# Custom compaction instructions for a D&D DM agent.
# Replaces the default compaction prompt entirely.
_COMPACTION_INSTRUCTIONS = """\
Summarize this D&D campaign session so the Dungeon Master can continue seamlessly.

Preserve:
- Current scene, location, and game phase (exploration / combat / social / rest)
- Active quests, unresolved story threads, and key NPC interactions
- Items the players received or lost, and their narrative context
- Story flags and plot decisions that have been made
- Combat state if ongoing: initiative order, round number, notable HP changes
- Recent player actions and the DM's narrative responses
- Any foreshadowing, hooks, or promises the DM introduced
- Players' character names and notable in-game traits

Omit: raw tool call parameters, routine mechanical bookkeeping with no
narrative weight.

Wrap your summary in <summary></summary> tags.\
"""


class AgentDM:
    def __init__(
        self,
        config: dict,
        state: GameState | None,
        paths: dict | None = None,
        session_manager=None,
        group_id: str | None = None,
    ):
        self.config = config
        self.state = state
        self.provider = create_provider(config)
        self.model = config.get("model", "claude-sonnet-4-6")
        self.max_tool_iterations = 100
        self.prompts_dir = Path(__file__).parent / "prompts"

        # Per-session paths (from session_manager) or None for lobby mode
        self.paths = paths
        self.session_manager = session_manager
        self.group_id = group_id
        self.lobby_mode = (state is None)
        # Optional async callback for immediate message delivery during turns.
        # Set by main.py's process_batch to flush messages between API calls.
        self._message_callback = None

        # Campaign dir comes from per-session paths, not global config
        if paths and paths.get("campaign_dir"):
            self.campaign_dir = Path(paths["campaign_dir"])
        else:
            self.campaign_dir = None

        # Compaction triggers when input tokens exceed this threshold.
        self.compaction_threshold = config.get("compaction_threshold", 60_000)
        # Tool results are cleared when input tokens exceed this (fires before compaction).
        self.tool_clear_threshold = config.get("tool_clear_threshold", 35_000)

        # Consciousness file is per-session
        if paths and paths.get("consciousness_file"):
            self.consciousness_file = paths["consciousness_file"]
        else:
            self.consciousness_file = None

        # Full conversation history including tool call/result pairs.
        # Persisted to disk so context survives process restarts.
        # Server-side compaction manages pruning; this is the source of truth.
        self.conversation_cache: list[dict] = []
        self.max_cache_messages = 200  # client-side safety cap

        # In-memory cache for the static system prompt.
        # Invalidated by file mtime changes so edits take effect without restart.
        self._static_system_cache: str | None = None
        self._static_system_mtimes: dict[str, float] = {}

        # Signal to main.py that a session switch is needed after this turn
        self.session_switch_request: dict | None = None

        self._load_consciousness()

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_static_system(self) -> str:
        """Build the stable portion of the system prompt (DM rules + lore).

        This content changes rarely (only when prompt files or world.md change)
        and is cached with cache_control so the KV cache hits on every turn.
        It does NOT include any per-turn game state.

        The result is memoized in memory and only rebuilt when a source file's
        mtime changes, avoiding redundant disk reads on every turn.
        """
        paths = [
            self.prompts_dir / "system.md",
            self.prompts_dir / "rules.md",
        ]
        if self.campaign_dir:
            paths.append(self.campaign_dir / "world.md")
        current_mtimes = {}
        for p in paths:
            try:
                current_mtimes[str(p)] = p.stat().st_mtime
            except FileNotFoundError:
                current_mtimes[str(p)] = 0.0

        if (
            self._static_system_cache is not None
            and current_mtimes == self._static_system_mtimes
        ):
            return self._static_system_cache

        system_template = (self.prompts_dir / "system.md").read_text()
        rules = (self.prompts_dir / "rules.md").read_text()
        lore = self._get_relevant_lore()
        result = system_template.format(rules=rules, relevant_lore=lore)

        self._static_system_cache = result
        self._static_system_mtimes = current_mtimes
        log.debug("Static system prompt rebuilt (%d chars)", len(result))
        return result

    def _build_dynamic_system(self) -> str:
        """Build the volatile portion of the system prompt (current game state).

        Changes every turn as HP, flags, narrative, etc. evolve. Passed as a
        separate uncached block so the static block above stays cached.

        In lobby mode, returns instructions for session management.
        """
        if self.state is None:
            return (
                "# Lobby Mode\n\n"
                "No campaign session is active. Help the players choose or "
                "start a campaign.\n\n"
                "Use `list_campaigns` to see available campaign templates.\n"
                "Use `list_sessions` to see this group's existing sessions.\n"
                "Use `start_session` to begin a new campaign.\n"
                "Use `switch_session` to resume a paused session."
            )

        summary = state_summary(self.state)
        narrative = get_recent_narrative(
            self.state, self.config.get("max_narrative_in_prompt", 15)
        )
        narrative_text = "\n".join(narrative) if narrative else "(No events yet)"
        flags = (
            "\n".join(f"- {k}: {v}" for k, v in self.state.flags.items())
            if self.state.flags
            else "(None)"
        )
        return (
            "# Current State\n\n"
            f"{summary}\n\n"
            "---\n\n"
            "# Recent Events\n\n"
            f"{narrative_text}\n\n"
            "---\n\n"
            "# Active Quests & Story Flags\n\n"
            f"{flags}"
        )

    def _get_relevant_lore(self) -> str:
        """Load campaign lore relevant to current location.

        In lobby mode (no campaign_dir), returns a listing of available
        campaigns so the DM can help players choose.
        """
        if self.campaign_dir is None:
            if self.session_manager:
                campaigns = self.session_manager.list_campaigns()
                if campaigns:
                    lines = [f"- **{c['name']}** (`{c['directory']}`): {c['description']}"
                             for c in campaigns]
                    return (
                        "# Available Campaigns\n\n"
                        + "\n".join(lines)
                        + "\n\nUse `list_campaigns` and `start_session` tools to begin."
                    )
            return "(No campaigns available)"

        world_path = self.campaign_dir / "world.md"
        if world_path.exists():
            return world_path.read_text()
        return "(No campaign lore loaded)"

    # ------------------------------------------------------------------
    # Caching helpers
    # ------------------------------------------------------------------

    def _prepare_system_blocks(self, static: str, dynamic: str) -> list[dict]:
        """Return a two-block system list.

        Block 1 (cached): DM identity, rules, lore, tool instructions.
          cache_control ensures this is written once and read on every
          subsequent turn — pays the write cost once per ~5 min TTL.

        Block 2 (not cached): Current game state, recent narrative, flags.
          Changes every turn, so caching it would be a perpetual cache-write
          with no reads. Pass it uncached.
        """
        return [
            {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic},
        ]

    def _prepare_tools_cached(self, tool_defs: list[dict] | None = None) -> list[dict]:
        """Copy tool definitions and add cache_control to the last one."""
        tools = list(tool_defs or TOOL_DEFINITIONS)
        if tools:
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
        return tools

    def _tag_last_user_message(self, messages: list[dict]) -> None:
        """Add cache_control to the last user message in the list.

        Strips any existing cache_control from all user messages first,
        then tags only the last one.
        """
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)

        for msg in reversed(messages):
            if msg.get("role") == "user":
                if isinstance(msg["content"], str):
                    msg["content"] = [
                        {
                            "type": "text",
                            "text": msg["content"],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                elif isinstance(msg["content"], list) and msg["content"]:
                    last = msg["content"][-1]
                    if isinstance(last, dict):
                        last["cache_control"] = {"type": "ephemeral"}
                break

    # ------------------------------------------------------------------
    # Compaction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_compaction_block(content) -> bool:
        """Return True if the content list contains a compaction block."""
        if not isinstance(content, list):
            return False
        return any(
            (isinstance(b, dict) and b.get("type") == "compaction")
            or (hasattr(b, "type") and b.type == "compaction")
            for b in content
        )

    def _save_consciousness(self) -> None:
        """Atomically persist conversation_cache to disk.

        Uses write-to-temp + os.replace for crash safety, matching the
        same pattern used by state.py for game_state.json.
        """
        if self.consciousness_file is None:
            return
        try:
            os.makedirs(os.path.dirname(self.consciousness_file) or ".", exist_ok=True)
            data = [_serialize_message(m) for m in self.conversation_cache]
            tmp = self.consciousness_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.consciousness_file)
        except OSError as e:
            log.error("Failed to save consciousness: %s", e)

    def _load_consciousness(self) -> None:
        """Load conversation_cache from disk on startup.

        All content blocks come back as plain dicts, which the API accepts
        in place of SDK Pydantic objects.
        """
        if self.consciousness_file is None or not os.path.exists(self.consciousness_file):
            return
        try:
            with open(self.consciousness_file) as f:
                data = json.load(f)
            if isinstance(data, list):
                self.conversation_cache = data
                log.info(
                    "Loaded consciousness: %d messages from %s",
                    len(data),
                    self.consciousness_file,
                )
                self._sanitize_cache()
        except (OSError, json.JSONDecodeError) as e:
            log.error("Failed to load consciousness (starting fresh): %s", e)

    @staticmethod
    def _is_tool_result_message(msg: dict) -> bool:
        """Check if a message contains tool_result blocks (not plain user text)."""
        content = msg.get("content")
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
        return False

    def _sanitize_cache(self) -> None:
        """Ensure conversation_cache is valid for the API.

        Strips leading orphaned tool_result/assistant messages and trailing
        orphaned tool_use blocks (no matching tool_result follows).
        Mirrors the proven _ensure_valid_history pattern from cleo.
        """
        if not self.conversation_cache:
            return

        history = self.conversation_cache
        original_len = len(history)

        # Clean leading orphans (but keep compaction summaries)
        while history:
            if history[0]["role"] == "user" and not self._is_tool_result_message(history[0]):
                break
            if history[0]["role"] == "assistant":
                content = history[0].get("content", [])
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "compaction"
                    for b in content
                ):
                    break  # valid start — compaction summary
            history.pop(0)

        # Clean trailing orphaned tool_use (no matching tool_result follows)
        while history and history[-1]["role"] == "assistant":
            content = history[-1].get("content", [])
            blocks = content if isinstance(content, list) else []
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks
            )
            has_compaction = any(
                isinstance(b, dict) and b.get("type") == "compaction" for b in blocks
            )
            if has_tool_use and not has_compaction:
                history.pop()
            elif has_tool_use and has_compaction:
                # Strip tool_use blocks but preserve compaction summary
                history[-1]["content"] = [
                    b for b in blocks
                    if not (isinstance(b, dict) and b.get("type") == "tool_use")
                ]
                break
            else:
                break

        dropped = original_len - len(history)
        if dropped:
            log.warning("Sanitized cache: dropped %d orphaned message(s)", dropped)
            self._save_consciousness()

    def _prune_before_compaction(self) -> None:
        """Drop client-side messages that predate the most recent compaction block.

        The API ignores those messages anyway once it sees the compaction block,
        so pruning them keeps client-side memory bounded.
        """
        for i in range(len(self.conversation_cache) - 1, -1, -1):
            if self._has_compaction_block(self.conversation_cache[i].get("content", [])):
                if i > 0:
                    del self.conversation_cache[:i]
                return

    def _tag_compaction_block(self) -> None:
        """Add cache_control to the compaction block in conversation_cache.

        After compaction fires, the summary block sits in an assistant message.
        Tagging it with cache_control: ephemeral means subsequent API calls get
        a cache hit on the summary rather than reprocessing it as plain input.
        """
        for msg in self.conversation_cache:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            if self._has_compaction_block(content):
                # Serialize to dicts (blocks may still be Pydantic objects here)
                # then stamp cache_control onto the compaction block.
                serialized = [_serialize_block(b) for b in content]
                for block in serialized:
                    if isinstance(block, dict) and block.get("type") == "compaction":
                        block["cache_control"] = {"type": "ephemeral"}
                msg["content"] = serialized
                log.debug("Tagged compaction block with cache_control")
                return

    # ------------------------------------------------------------------
    # Core agent loop
    # ------------------------------------------------------------------

    async def handle_batch(
        self, batch: list[tuple[str, str, str]]
    ) -> list[str]:
        """
        Process a batch of player messages through Claude in a single turn.

        Args:
            batch: List of (sender_id, sender_name, text) tuples.

        Returns:
            List of response strings. Group messages are plain text;
            DMs are prefixed with ``PRIVATE:<player_id>:<message>``.
        """
        if not batch:
            return []

        if len(batch) == 1:
            sender_id, sender_name, text = batch[0]
            user_content = f"[{sender_id}|{sender_name}]: {text}"
        else:
            lines = [f"[{sid}|{sname}]: {txt}" for sid, sname, txt in batch]
            user_content = "\n".join(lines)

        log.info("Batch user content (%d msg): %s", len(batch), user_content[:200])
        return await self._process_turn(user_content)

    async def handle_message(
        self, sender_id: str, sender_name: str, text: str
    ) -> list[str]:
        """Process a single player message."""
        return await self.handle_batch([(sender_id, sender_name, text)])

    async def _triage_should_respond(self, user_content: str) -> bool:
        """Ask Haiku whether the DM should respond to this message.

        Returns True (RESPOND) or False (SILENT). Falls back to True on error.
        Skips triage (always True) during combat and lobby mode.
        """
        # Always respond in combat (time-sensitive) and lobby (session mgmt)
        if self.lobby_mode:
            log.info("Triage skipped: lobby mode")
            return True
        if self.state and self.state.phase == "combat":
            log.info("Triage skipped: combat phase")
            return True
        if not self.config.get("triage_enabled", True):
            log.info("Triage skipped: disabled in config")
            return True

        triage_model = self.config.get("triage_model", "claude-haiku-4-5-20251001")
        phase = self.state.phase if self.state else "unknown"

        triage_prompt = (
            "You are a filter for a D&D Dungeon Master AI.\n\n"
            f"Game phase: {phase}\n"
            f"Message: {user_content}\n\n"
            "Reply RESPOND or SILENT.\n"
            "SILENT ONLY if the message is 100% clearly players chatting "
            "to each other about real life, memes, or logistics with zero "
            "game content.\n"
            "RESPOND for everything else. When in doubt, RESPOND.\n"
            "Anything that COULD be in-character or game-related = RESPOND."
        )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.provider.create_triage_message,
                    model=triage_model,
                    max_tokens=8,
                    messages=[{"role": "user", "content": triage_prompt}],
                ),
                timeout=10,
            )
            answer = response.content[0].text.strip().upper()
            should_respond = "RESPOND" in answer

            triage_tokens_in = response.usage.input_tokens
            triage_tokens_out = response.usage.output_tokens
            log.info(
                "Triage: %s (model=%s, in=%d, out=%d, message=%s)",
                "RESPOND" if should_respond else "SILENT",
                triage_model,
                triage_tokens_in,
                triage_tokens_out,
                user_content[:100],
            )
            return should_respond

        except Exception as e:
            log.warning("Triage error (falling back to RESPOND): %s", e)
            return True

    async def _process_turn(self, user_content: str) -> list[str]:
        """
        Core agent loop. Takes a formatted user message, sends to Claude
        with conversation history + tools + prompt caching, executes tool
        calls in a loop, and returns response strings.

        Context management strategy:
        - System prompt is built ONCE per turn (not per tool iteration), so
          the static cache breakpoint stays valid throughout.
        - Tool call/result pairs are stored in conversation_cache so Claude
          has explicit memory of its own actions across turns.
        - Server-side compaction replaces the silent rolling-window drop with
          a summarised context handoff when input tokens approach the threshold.
        - Fallback model uses the standard (non-beta) endpoint since it may not
          support the compaction beta.
        """
        self.conversation_cache.append({"role": "user", "content": user_content})
        self._trim_cache()

        # Haiku triage: skip expensive Sonnet call for non-game messages.
        if not await self._triage_should_respond(user_content):
            self.conversation_cache.append(
                {"role": "assistant", "content": "*[silent]*"}
            )
            self._save_consciousness()
            return []

        # Build system prompt ONCE for this entire turn.
        # Rebuilding per tool-iteration would invalidate the static cache breakpoint
        # on every iteration — the static content never changes within a turn.
        static_system = self._build_static_system()
        dynamic_system = self._build_dynamic_system()
        system_blocks = self._prepare_system_blocks(static_system, dynamic_system)
        # Select tool set: lobby tools only, or game tools + exit_to_lobby
        if self.lobby_mode:
            tools_cached = self._prepare_tools_cached(LOBBY_TOOL_DEFINITIONS)
        else:
            tools_cached = self._prepare_tools_cached(
                TOOL_DEFINITIONS + [EXIT_TO_LOBBY_DEFINITION]
            )

        state_file = self.paths["state_file"] if self.paths else ""
        executor = ToolExecutor(
            self.state,
            state_file,
            session_manager=self.session_manager,
            group_id=self.group_id,
        )

        # Local messages list starts as a copy of conversation_cache and grows
        # with tool call/result pairs during this turn.
        messages = list(self.conversation_cache)
        # Track messages already delivered mid-turn via callback.
        delivered: list[str] = []

        for iteration in range(self.max_tool_iterations):
            self._tag_last_user_message(messages)

            try:
                # Build context management config only for providers that support it
                cm_config = None
                if self.provider.supports_compaction():
                    cm_config = {
                        "edits": [
                            {
                                "type": "clear_tool_uses_20250919",
                                "trigger": {
                                    "type": "input_tokens",
                                    "value": self.tool_clear_threshold,
                                },
                                "keep": {"type": "tool_uses", "value": 5},
                                "clear_at_least": {
                                    "type": "input_tokens",
                                    "value": 10_000,
                                },
                            },
                            {
                                "type": "compact_20260112",
                                "trigger": {
                                    "type": "input_tokens",
                                    "value": self.compaction_threshold,
                                },
                                "instructions": _COMPACTION_INSTRUCTIONS,
                            },
                        ]
                    }

                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.provider.create_message,
                        model=self.model,
                        max_tokens=self.config.get("max_tokens", 1024),
                        system=system_blocks,
                        tools=tools_cached,
                        messages=messages,
                        context_management=cm_config,
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                log.error("LLM API timeout (120s) at iteration %d", iteration)
                return [
                    "*The DM's skull flickers.* Something went wrong. Try again."
                ]
            except Exception as e:
                log.error("LLM API error: %s", e)
                return [
                    "*The DM's skull flickers.* Something went wrong. Try again."
                ]

            # Log token usage including cache metrics for cost analysis.
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            uncached_in = response.usage.input_tokens - cache_read - cache_create
            log.info(
                "API: stop=%s, in=%d (uncached=%d, cache_read=%d, cache_create=%d), "
                "out=%d, iter=%d, cache_msgs=%d",
                response.stop_reason,
                response.usage.input_tokens,
                uncached_in,
                cache_read,
                cache_create,
                response.usage.output_tokens,
                iteration,
                len(self.conversation_cache),
            )

            # Transparent compaction: the API may include a compaction block in
            # the response (without pause) when the threshold is exceeded.
            # Sync both messages and conversation_cache to reflect the new state.
            compacted_this_iter = self._has_compaction_block(response.content)
            if compacted_this_iter:
                log.info(
                    "Compaction fired at %d input tokens (threshold=%d, cache_msgs=%d)",
                    response.usage.input_tokens,
                    self.compaction_threshold,
                    len(self.conversation_cache),
                )
                serialized_content = [_serialize_block(b) for b in response.content]
                messages.append({"role": "assistant", "content": serialized_content})
                self.conversation_cache = list(messages)
                self._prune_before_compaction()
                self._tag_compaction_block()
                messages = list(self.conversation_cache)
                self._save_consciousness()

            # ---- Compaction pause (only when pause_after_compaction=True) ----
            if response.stop_reason == "compaction":
                log.info("Compaction pause — continuing to next iteration")
                continue

            # ---- Final response (no more tool use) ----
            elif response.stop_reason in (
                "end_turn", "stop_sequence", "max_tokens", "stop",
                "pause_turn", "refusal",
            ):
                text_blocks = [b.text for b in response.content if b.type == "text"]
                final_text = "\n".join(text_blocks) if text_blocks else ""

                if final_text:
                    log.info(
                        "Claude text (not auto-sent, %d chars): %s",
                        len(final_text),
                        final_text[:200],
                    )

                # Store the final response. If compaction already synced this
                # response into conversation_cache, don't add it a second time.
                if not compacted_this_iter:
                    cache_content = final_text or "*[silent]*"
                    self.conversation_cache.append(
                        {"role": "assistant", "content": cache_content}
                    )
                    self._trim_cache()

                # Collect messages not yet delivered via callback.
                results: list[str] = []

                for msg in executor.group_messages:
                    results.append(msg)

                for pm in executor.private_messages:
                    results.append(f"PRIVATE:{pm['player_id']}:{pm['message']}")

                # Never auto-send raw text. The DM must use send_group_message.
                # Text output is internal thinking only.

                if response.stop_reason == "max_tokens" and not results:
                    results.append(
                        "*The DM trails off, overwhelmed by the enormity of it all.*"
                    )
                elif response.stop_reason == "refusal" and not results:
                    results.append(
                        "*The DM clacks its jaw thoughtfully and declines to answer that.*"
                    )

                total_sent = len(delivered) + len(results)
                if total_sent:
                    log.info(
                        "Messages: %d delivered mid-turn, %d returning now",
                        len(delivered), len(results),
                    )
                else:
                    log.info("DM chose silence (no send_group_message called)")

                log.info(
                    "Turn complete: iterations=%d, tool_calls=%d, "
                    "messages_sent=%d, cache_msgs=%d",
                    iteration + 1,
                    sum(
                        1 for m in messages
                        if m.get("role") == "user"
                        and isinstance(m.get("content"), list)
                        and any(
                            (isinstance(b, dict) and b.get("type") == "tool_result")
                            for b in m["content"]
                        )
                    ),
                    total_sent,
                    len(self.conversation_cache),
                )

                # Propagate session switch signal for main.py to handle
                if executor.session_switch_request:
                    self.session_switch_request = executor.session_switch_request

                return results

            # ---- Tool use — execute and loop back ----
            elif response.stop_reason == "tool_use":
                # Log DM reasoning text that accompanies tool calls
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        log.info("DM thinking: %s", block.text[:500])

                serialized_content = [_serialize_block(b) for b in response.content]
                assistant_msg = {"role": "assistant", "content": serialized_content}

                # If compaction already synced the response, don't re-append it.
                if not compacted_this_iter:
                    messages.append(assistant_msg)
                    # Store tool_use turns in conversation_cache for continuity —
                    # this lets Claude remember what tools it called in previous turns.
                    self.conversation_cache.append(assistant_msg)

                tool_results: list[dict] = []
                for block in response.content:
                    if block.type == "tool_use":
                        log.info(
                            "Tool call: %s(%s)",
                            block.name,
                            json.dumps(block.input)[:200],
                        )
                        try:
                            result = executor.execute(block.name, block.input)
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps(result),
                                }
                            )
                            log.info("Tool result: %s", json.dumps(result)[:200])
                        except Exception as e:
                            log.error("Tool execution error: %s", e)
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps({"error": str(e)}),
                                    "is_error": True,
                                }
                            )

                tool_results_msg = {"role": "user", "content": tool_results}
                messages.append(tool_results_msg)
                # Store tool results in conversation history — gives Claude memory
                # of tool outcomes across turns without re-running them.
                self.conversation_cache.append(tool_results_msg)
                # Save after each tool cycle: state.json already reflects the tool's
                # side effects, so consciousness must stay in sync with it.
                self._save_consciousness()

                # Flush any queued messages immediately so players don't wait
                # for the full tool loop to finish.
                if self._message_callback and executor.group_messages:
                    for msg in executor.group_messages:
                        delivered.append(msg)
                        await self._message_callback(msg)
                    executor.group_messages.clear()

                # NOTE: System prompt is NOT rebuilt here. It was built once at
                # turn start. Tool results carry state updates inline (e.g.,
                # deal_damage returns new_hp). The next turn's dynamic block will
                # reflect all accumulated changes.

            else:
                log.warning("Unexpected stop_reason: %s", response.stop_reason)
                if not compacted_this_iter:
                    self.conversation_cache.append(
                        {"role": "assistant", "content": "*[No response]*"}
                    )
                    self._trim_cache()
                break

        log.warning("Hit max tool iterations (%d)", self.max_tool_iterations)
        return [
            "*The DM's eye sockets glow with an inner fire, lost in thought...*"
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _trim_cache(self) -> None:
        """Client-side safety cap on conversation cache size, then persist to disk.

        Server-side compaction handles real context pruning. This just
        prevents unbounded memory growth on the client side between
        compaction events.
        """
        if len(self.conversation_cache) > self.max_cache_messages:
            # First try to prune old content before the most recent compaction block.
            self._prune_before_compaction()
            # Hard cap as a last resort (shouldn't normally trigger with compaction on).
            if len(self.conversation_cache) > self.max_cache_messages:
                self.conversation_cache = self.conversation_cache[-self.max_cache_messages:]
                # The slice may orphan leading tool_results or assistant messages.
                self._sanitize_cache()
        self._save_consciousness()

    def get_player_name(self, sender_id: str) -> str:
        """Look up a player's character name from their sender ID."""
        if self.state is None:
            return sender_id
        player = self.state.players.get(sender_id)
        if player:
            return player.name
        return sender_id
