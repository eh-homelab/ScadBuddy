"""Infrastructure variables only (CLAUDE.md: no AI settings in env)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ENV_VARS = (
    "SCADBUDDY_DATABASE_URL",
    "SCADBUDDY_SECRET_KEY_FILE",
    "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE",
    "SCADBUDDY_TEMPORAL_ADDRESS",
    "SCADBUDDY_TEMPORAL_NAMESPACE",
    "SCADBUDDY_AGENT_DURABLE_HEALTH_PORT",
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str | None
    secret_key_file: str | None
    secret_key_previous_file: str | None
    temporal_address: str | None
    temporal_namespace: str
    health_port: int


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def load_config(env: Mapping[str, str]) -> Config:
    address = _opt(env, "SCADBUDDY_TEMPORAL_ADDRESS")
    if address is not None and re.search(r"\s", address):
        raise ConfigError("SCADBUDDY_TEMPORAL_ADDRESS must be host:port with no whitespace")
    port_text = _opt(env, "SCADBUDDY_AGENT_DURABLE_HEALTH_PORT") or "8082"
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ConfigError("SCADBUDDY_AGENT_DURABLE_HEALTH_PORT must be a port number")
    return Config(
        database_url=_opt(env, "SCADBUDDY_DATABASE_URL"),
        secret_key_file=_opt(env, "SCADBUDDY_SECRET_KEY_FILE"),
        secret_key_previous_file=_opt(env, "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE"),
        temporal_address=address,
        temporal_namespace=_opt(env, "SCADBUDDY_TEMPORAL_NAMESPACE") or "scadbuddy",
        health_port=int(port_text),
    )
