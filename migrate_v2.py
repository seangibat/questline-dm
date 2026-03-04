#!/usr/bin/env python3
"""
migrate_v2.py — One-time migration to multi-group, multi-session layout.

Moves existing flat data files into the new directory structure:
  data/game_state.json     → data/groups/<hash>/sessions/<session_id>/game_state.json
  data/consciousness.json  → data/groups/<hash>/sessions/<session_id>/consciousness.json
  data/narrative/           → data/groups/<hash>/sessions/<session_id>/narrative/

Creates group_meta.json with the existing group ID and session.

Usage:
    python migrate_v2.py [--config config.yaml] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime


def group_hash(group_id: str) -> str:
    return hashlib.sha256(group_id.encode()).hexdigest()[:16]


def migrate(config_path: str, dry_run: bool = False) -> None:
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f)

    data_dir = config.get("data_dir", "data")
    campaigns_dir = config.get("campaigns_dir", "campaigns")
    allowed_groups = config.get("allowed_groups", [])

    if not allowed_groups:
        print("ERROR: No allowed_groups in config. Cannot determine group ID.")
        sys.exit(1)

    # Use the first (only) allowed group for migration
    gid = allowed_groups[0]
    ghash = group_hash(gid)
    print(f"Group ID: {gid[:20]}...")
    print(f"Group hash: {ghash}")

    # Determine campaign name from campaigns_dir
    campaign_name = None
    if os.path.isdir(campaigns_dir):
        for entry in os.listdir(campaigns_dir):
            if os.path.isdir(os.path.join(campaigns_dir, entry)):
                campaign_name = entry
                break
    if not campaign_name:
        print("ERROR: No campaign found in campaigns_dir")
        sys.exit(1)
    print(f"Campaign: {campaign_name}")

    session_id = f"{campaign_name}-1"
    session_dir = os.path.join(data_dir, "groups", ghash, "sessions", session_id)
    print(f"Session dir: {session_dir}")

    # Source files
    old_state = os.path.join(data_dir, "game_state.json")
    old_consciousness = os.path.join(data_dir, "consciousness.json")
    old_narrative = os.path.join(data_dir, "narrative")

    if not os.path.exists(old_state) and not os.path.exists(old_narrative):
        print("Nothing to migrate (no game_state.json or narrative/ in data/)")
        return

    if dry_run:
        print("\n--- DRY RUN ---")
        print(f"Would create: {session_dir}/")
        if os.path.exists(old_state):
            print(f"  Move: {old_state} → {session_dir}/game_state.json")
        if os.path.exists(old_consciousness):
            print(f"  Move: {old_consciousness} → {session_dir}/consciousness.json")
        if os.path.isdir(old_narrative):
            for f in os.listdir(old_narrative):
                print(f"  Move: {old_narrative}/{f} → {session_dir}/narrative/{f}")
        print(f"Would create: group_meta.json")
        return

    # Create session directory
    os.makedirs(os.path.join(session_dir, "narrative"), exist_ok=True)

    # Move files
    if os.path.exists(old_state):
        shutil.move(old_state, os.path.join(session_dir, "game_state.json"))
        print(f"  Moved game_state.json")

    if os.path.exists(old_consciousness):
        shutil.move(old_consciousness, os.path.join(session_dir, "consciousness.json"))
        print(f"  Moved consciousness.json")

    if os.path.isdir(old_narrative):
        for fname in os.listdir(old_narrative):
            src = os.path.join(old_narrative, fname)
            dst = os.path.join(session_dir, "narrative", fname)
            shutil.move(src, dst)
            print(f"  Moved narrative/{fname}")
        # Remove the now-empty directory
        try:
            os.rmdir(old_narrative)
            print(f"  Removed empty {old_narrative}/")
        except OSError:
            print(f"  Warning: {old_narrative}/ not empty, left in place")

    # Create group_meta.json
    meta = {
        "group_id": gid,
        "active_session_id": session_id,
        "sessions": {
            session_id: {
                "campaign": campaign_name,
                "label": f"{campaign_name} #1",
                "created": datetime.now().isoformat(timespec="seconds"),
            }
        },
    }
    meta_path = os.path.join(data_dir, "groups", ghash, "group_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  Created {meta_path}")

    print("\nMigration complete!")
    print(f"  Session: {session_id}")
    print(f"  Data dir: {session_dir}")


def main():
    parser = argparse.ArgumentParser(description="Migrate to multi-group layout")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    args = parser.parse_args()
    migrate(args.config, args.dry_run)


if __name__ == "__main__":
    main()
