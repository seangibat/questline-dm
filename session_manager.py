"""
session_manager.py — Group and session metadata management.

Manages the multi-group, multi-campaign directory structure:
  data/groups/<hash>/group_meta.json
  data/groups/<hash>/sessions/<session_id>/{game_state,consciousness,narrative/}

Campaign bibles (read-only templates) live in campaigns/<name>/.

This module has no Claude/agent dependencies — it only deals with
filesystem paths, JSON metadata, and directory creation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

log = logging.getLogger("questline.session")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """Metadata for a single campaign session."""
    session_id: str
    campaign: str       # campaign bible directory name
    label: str          # human-readable label
    created: str        # ISO 8601 timestamp


@dataclass
class GroupContext:
    """Runtime representation of a group's metadata."""
    group_id: str
    group_hash: str
    group_dir: str
    active_session_id: str | None
    sessions: dict[str, SessionInfo] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages group metadata, session CRUD, and path derivation."""

    def __init__(self, config: dict):
        self.data_dir: str = config.get("data_dir", "data")
        self.campaigns_dir: str = config.get("campaigns_dir", "campaigns")

    # ------------------------------------------------------------------
    # Path derivation
    # ------------------------------------------------------------------

    @staticmethod
    def group_hash(group_id: str) -> str:
        """Deterministic filesystem-safe hash of a Signal group ID."""
        return hashlib.sha256(group_id.encode()).hexdigest()[:16]

    def group_dir(self, group_id: str) -> str:
        """Absolute path to a group's data directory."""
        return os.path.join(self.data_dir, "groups", self.group_hash(group_id))

    def session_dir(self, group_id: str, session_id: str) -> str:
        """Absolute path to a session's data directory."""
        return os.path.join(self.group_dir(group_id), "sessions", session_id)

    def session_paths(self, group_id: str, session_id: str) -> dict[str, str]:
        """Return all paths an AgentDM needs for a session.

        Returns:
            Dict with keys: state_file, consciousness_file, narrative_dir, campaign_dir.
        """
        sdir = self.session_dir(group_id, session_id)
        ctx = self.load_group_meta(group_id)
        info = ctx.sessions.get(session_id)
        campaign_name = info.campaign if info else ""
        return {
            "state_file": os.path.join(sdir, "game_state.json"),
            "consciousness_file": os.path.join(sdir, "consciousness.json"),
            "narrative_dir": os.path.join(sdir, "narrative"),
            "campaign_dir": os.path.join(self.campaigns_dir, campaign_name),
        }

    # ------------------------------------------------------------------
    # Group metadata
    # ------------------------------------------------------------------

    def _meta_path(self, group_id: str) -> str:
        return os.path.join(self.group_dir(group_id), "group_meta.json")

    def load_group_meta(self, group_id: str) -> GroupContext:
        """Load group_meta.json, creating a fresh context if it doesn't exist."""
        ghash = self.group_hash(group_id)
        gdir = self.group_dir(group_id)
        meta_path = self._meta_path(group_id)

        if os.path.exists(meta_path):
            with open(meta_path) as f:
                data = json.load(f)
            sessions = {}
            for sid, sdata in data.get("sessions", {}).items():
                sessions[sid] = SessionInfo(
                    session_id=sid,
                    campaign=sdata["campaign"],
                    label=sdata.get("label", sid),
                    created=sdata.get("created", ""),
                )
            return GroupContext(
                group_id=group_id,
                group_hash=ghash,
                group_dir=gdir,
                active_session_id=data.get("active_session_id"),
                sessions=sessions,
            )

        return GroupContext(
            group_id=group_id,
            group_hash=ghash,
            group_dir=gdir,
            active_session_id=None,
            sessions={},
        )

    def save_group_meta(self, ctx: GroupContext) -> None:
        """Atomically persist group_meta.json."""
        os.makedirs(ctx.group_dir, exist_ok=True)
        data = {
            "group_id": ctx.group_id,
            "active_session_id": ctx.active_session_id,
            "sessions": {
                sid: {
                    "campaign": info.campaign,
                    "label": info.label,
                    "created": info.created,
                }
                for sid, info in ctx.sessions.items()
            },
        }
        meta_path = self._meta_path(ctx.group_id)
        tmp = meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, meta_path)

    # ------------------------------------------------------------------
    # Campaign listing
    # ------------------------------------------------------------------

    def list_campaigns(self) -> list[dict[str, Any]]:
        """Scan campaigns_dir for available campaign bibles.

        Each campaign is a subdirectory containing at least world.md.
        An optional meta.yaml provides name and description.

        Returns:
            List of dicts: {name, directory, description, has_npcs}.
        """
        campaigns = []
        if not os.path.isdir(self.campaigns_dir):
            return campaigns

        for entry in sorted(os.listdir(self.campaigns_dir)):
            cdir = os.path.join(self.campaigns_dir, entry)
            if not os.path.isdir(cdir):
                continue
            world_path = os.path.join(cdir, "world.md")
            if not os.path.exists(world_path):
                continue

            name = entry
            description = ""
            meta_path = os.path.join(cdir, "meta.yaml")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = yaml.safe_load(f) or {}
                    name = meta.get("name", entry)
                    description = meta.get("description", "")
                except Exception as e:
                    log.warning("Failed to read %s: %s", meta_path, e)

            campaigns.append({
                "name": name,
                "directory": entry,
                "description": description or "(No description)",
                "has_npcs": os.path.isdir(os.path.join(cdir, "npcs")),
            })

        return campaigns

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def create_session(
        self, group_id: str, campaign: str, label: str | None = None
    ) -> SessionInfo:
        """Create a new session from a campaign bible.

        Creates the session directory, initializes an empty game_state.json,
        creates the narrative/ dir, updates group_meta, and sets this as the
        active session.

        Args:
            group_id: Signal group ID.
            campaign: Campaign bible directory name (must exist in campaigns_dir).
            label: Optional human-readable label. Defaults to campaign name.

        Returns:
            The newly created SessionInfo.

        Raises:
            FileNotFoundError: If the campaign bible doesn't exist.
        """
        campaign_path = os.path.join(self.campaigns_dir, campaign)
        if not os.path.isdir(campaign_path):
            raise FileNotFoundError(f"Campaign '{campaign}' not found in {self.campaigns_dir}")
        if not os.path.exists(os.path.join(campaign_path, "world.md")):
            raise FileNotFoundError(f"Campaign '{campaign}' has no world.md")

        ctx = self.load_group_meta(group_id)

        # Generate session ID: campaign-name-N
        counter = 1
        for sid in ctx.sessions:
            if sid.startswith(f"{campaign}-"):
                try:
                    n = int(sid.rsplit("-", 1)[-1])
                    counter = max(counter, n + 1)
                except ValueError:
                    pass
        session_id = f"{campaign}-{counter}"

        info = SessionInfo(
            session_id=session_id,
            campaign=campaign,
            label=label or f"{campaign} #{counter}",
            created=datetime.now().isoformat(timespec="seconds"),
        )

        # Create session directory structure
        sdir = self.session_dir(group_id, session_id)
        os.makedirs(os.path.join(sdir, "narrative"), exist_ok=True)

        # Initialize empty game state
        initial_state = {
            "session_id": session_id,
            "phase": "lobby",
            "current_location": "start",
            "players": {},
            "enemies": [],
            "scene": {},
            "turn": {},
            "flags": {},
            "narrative_file": "",
            "_enemy_counter": 0,
        }
        state_path = os.path.join(sdir, "game_state.json")
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(initial_state, f, indent=2)
        os.replace(tmp, state_path)

        # Update group metadata
        ctx.sessions[session_id] = info
        ctx.active_session_id = session_id
        self.save_group_meta(ctx)

        log.info(
            "Created session '%s' (campaign=%s) for group %s",
            session_id, campaign, self.group_hash(group_id),
        )
        return info

    def switch_session(self, group_id: str, session_id: str) -> SessionInfo:
        """Switch the active session for a group.

        Args:
            group_id: Signal group ID.
            session_id: Session to activate.

        Returns:
            The SessionInfo of the newly active session.

        Raises:
            KeyError: If session_id doesn't exist for this group.
        """
        ctx = self.load_group_meta(group_id)
        if session_id not in ctx.sessions:
            raise KeyError(f"Session '{session_id}' not found for this group")

        ctx.active_session_id = session_id
        self.save_group_meta(ctx)

        log.info(
            "Switched group %s to session '%s'",
            self.group_hash(group_id), session_id,
        )
        return ctx.sessions[session_id]

    def get_active_session(self, group_id: str) -> SessionInfo | None:
        """Return the active session for a group, or None if in lobby mode."""
        ctx = self.load_group_meta(group_id)
        if ctx.active_session_id and ctx.active_session_id in ctx.sessions:
            return ctx.sessions[ctx.active_session_id]
        return None

    def deactivate_session(self, group_id: str) -> None:
        """Set the group to lobby mode (no active session).

        The current session is preserved on disk but no longer active.
        """
        ctx = self.load_group_meta(group_id)
        ctx.active_session_id = None
        self.save_group_meta(ctx)
        log.info("Group %s entered lobby mode", self.group_hash(group_id))
