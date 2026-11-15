"""The agent's migrations that make the `ai_*` key tables the API reads and writes
(`ai_payload_keys`, `ai_forgotten_subjects`, `ai_flow_blobs`), for a test that makes them
by hand: its app skips the agent's migrations unless it is marked `agent_migrations`."""

from __future__ import annotations

from scadbuddy.render.agent_migrations import AGENT_MIGRATIONS_DIR

AGENT_KEY_MIGRATIONS = (
    AGENT_MIGRATIONS_DIR / "20261009T0421Z_payload_keys.sql",
    AGENT_MIGRATIONS_DIR / "20261012T0221Z_flow_blobs.sql",
)
