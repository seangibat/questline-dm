"""
signal_io.py — Signal I/O for QuestLine Agent DM.
TCP listener for incoming messages, HTTP JSON-RPC for sending.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import requests

log = logging.getLogger("questline.signal")

MAX_MESSAGE_AGE = 60  # reject messages older than this (replay protection)


class SignalIO:
    def __init__(self, config: dict):
        self.bot_number: str = config["bot_number"]
        self.allowed_groups: set[str] = set(config.get("allowed_groups", []))
        self.rpc_url: str = config["signal_rpc_url"]
        self.tcp_host: str = config["signal_tcp_host"]
        self.tcp_port: int = config["signal_tcp_port"]

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_group_message(self, group_id: str, message: str) -> bool:
        """Send message to a specific group.  Handles formatting + splitting."""
        chunks = self._split_message(message)
        for chunk in chunks:
            plain_text, text_styles = self._parse_formatting(chunk)
            payload = {
                "jsonrpc": "2.0",
                "method": "send",
                "id": "questline-dm-send",
                "params": {
                    "account": self.bot_number,
                    "message": plain_text,
                    "groupId": group_id,
                },
            }
            if text_styles:
                payload["params"]["textStyle"] = text_styles
            try:
                resp = await asyncio.to_thread(
                    requests.post, self.rpc_url, json=payload, timeout=10
                )
                resp.raise_for_status()
                log.info("Sent group message (%d chars)", len(plain_text))
            except requests.RequestException as e:
                log.error("Failed to send: %s", e)
                return False
        return True

    async def send_private_message(self, recipient: str, message: str) -> bool:
        """Send DM to a specific player."""
        plain_text, text_styles = self._parse_formatting(message)
        payload = {
            "jsonrpc": "2.0",
            "method": "send",
            "id": "questline-dm-dm",
            "params": {
                "account": self.bot_number,
                "message": plain_text,
                "recipient": recipient,
            },
        }
        if text_styles:
            payload["params"]["textStyle"] = text_styles
        try:
            resp = await asyncio.to_thread(
                requests.post, self.rpc_url, json=payload, timeout=10
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Failed to send DM: %s", e)
            return False

    # ------------------------------------------------------------------
    # Receiving (TCP listener)
    # ------------------------------------------------------------------

    async def start_listener(self, message_callback) -> None:
        """TCP listener with automatic reconnection.

        *message_callback(group_id, sender_id, text)* is awaited for each
        valid incoming group message from an allowed group.
        """
        backoff = 1
        while True:
            writer = None
            try:
                log.info(
                    "Connecting to signal-cli TCP: %s:%d",
                    self.tcp_host,
                    self.tcp_port,
                )
                reader, writer = await asyncio.open_connection(
                    self.tcp_host, self.tcp_port
                )
                log.info("TCP connected")
                backoff = 1

                while True:
                    line = await reader.readline()
                    if not line:
                        log.warning("TCP connection closed")
                        break
                    try:
                        data = json.loads(line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                    if data.get("method") == "receive":
                        envelope = data.get("params", {}).get("envelope")
                        if envelope:
                            asyncio.create_task(
                                self._handle_envelope(envelope, message_callback)
                            )

            except (ConnectionRefusedError, OSError) as e:
                log.warning("TCP failed: %s — reconnecting in %ds", e, backoff)
            except Exception:
                log.exception("TCP error — reconnecting in %ds", backoff)
            finally:
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handle_envelope(self, envelope: dict, callback) -> None:
        """Extract sender and text from envelope, validate, call callback."""
        # Get sender
        sender = envelope.get("source")
        if not sender or not sender.startswith("+"):
            source_uuid = envelope.get("sourceUuid") or envelope.get("source_uuid")
            if source_uuid:
                sender = f"uuid:{source_uuid}"
            else:
                return

        # Ignore own messages
        if sender == self.bot_number:
            return

        # Extract group ID — ignore non-group messages (1:1 DMs to bot)
        data_message = envelope.get("dataMessage") or {}
        group_v2 = data_message.get("groupV2") or {}
        group_id = group_v2.get("id") or data_message.get("groupInfo", {}).get(
            "groupId"
        )
        if not group_id:
            return
        # Whitelist check
        if self.allowed_groups and group_id not in self.allowed_groups:
            log.info("Ignored message from non-allowed group: %s", group_id)
            return

        # Replay protection
        timestamp_ms = envelope.get("timestamp")
        if timestamp_ms:
            age = time.time() - (timestamp_ms / 1000)
            if age > MAX_MESSAGE_AGE:
                log.debug("Rejected stale message (age=%.0fs)", age)
                return

        # Extract text
        text = (data_message.get("message") or "").strip()
        if not text:
            return

        # Filter out OOC (out-of-character) messages — these don't need DM response
        if text.lower().startswith("ooc:") or text.lower().startswith("ooc "):
            log.info("Ignored OOC message from %s: %s", sender, text[:100])
            return

        log.info("Message from %s in group %s: %s", sender, group_id[:16], text[:100])
        await callback(group_id, sender, text)

    # ------------------------------------------------------------------
    # Formatting — markdown → Signal textStyles
    # ------------------------------------------------------------------

    def _parse_formatting(self, text: str) -> tuple[str, list[str]]:
        """Parse markdown formatting into Signal textStyles.

        Supports: ||spoiler||, **bold**, ~~strike~~, *italic*,
        ~strike~, _italic_, `mono`.

        Returns ``(plain_text, list_of_style_strings)`` where each style
        string is ``"start:length:STYLE"``.
        """
        patterns = [
            (r"\|\|(.+?)\|\|", 2, 2, "SPOILER"),
            (r"\*\*(.+?)\*\*", 2, 2, "BOLD"),
            (r"~~(.+?)~~", 2, 2, "STRIKETHROUGH"),
            (r"\*(.+?)\*", 1, 1, "ITALIC"),
            (r"~(.+?)~", 1, 1, "STRIKETHROUGH"),
            # M1 fix: use word boundaries to avoid matching snake_case_words
            (r'(?<!\w)_(.+?)_(?!\w)', 1, 1, 'ITALIC'),
            (r"`(.+?)`", 1, 1, "MONOSPACE"),
        ]

        all_matches: list[tuple] = []
        for pattern_str, prefix_len, suffix_len, style in patterns:
            pattern = re.compile(pattern_str)
            pos = 0
            while pos < len(text):
                match = pattern.search(text, pos)
                if not match:
                    break
                all_matches.append(
                    (
                        match.start(),
                        match.end(),
                        match.group(1),
                        style,
                        prefix_len,
                        suffix_len,
                    )
                )
                pos = match.start() + 1

        # Sort by position, prefer longer matches (greedy overlap resolution)
        all_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

        filtered: list[tuple] = []
        covered: set[int] = set()
        for m in all_matches:
            chars = set(range(m[0], m[1]))
            if not chars & covered:
                filtered.append(m)
                covered.update(chars)
        filtered.sort(key=lambda x: x[0])

        # Build plain text + style list
        plain = ""
        styles: list[str] = []
        last_pos = 0
        offset_adj = 0
        for orig_start, orig_end, content, style, plen, slen in filtered:
            plain += text[last_pos:orig_start]
            new_start = orig_start - offset_adj
            plain += content
            styles.append(f"{new_start}:{len(content)}:{style}")
            offset_adj += plen + slen
            last_pos = orig_end
        plain += text[last_pos:]

        return plain, styles

    def _split_message(self, text: str, max_len: int = 3500) -> list[str]:
        """Split long messages at paragraph boundaries."""
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        current = ""
        for para in text.split("\n\n"):
            if len(current) + len(para) + 2 > max_len:
                if current:
                    chunks.append(current.strip())
                current = para
            else:
                current = (current + "\n\n" + para) if current else para
        if current:
            chunks.append(current.strip())

        # Safety: hard-split any chunk that's still too long
        final: list[str] = []
        for chunk in chunks:
            while len(chunk) > max_len:
                final.append(chunk[:max_len])
                chunk = chunk[max_len:]
            if chunk:
                final.append(chunk)

        return final
