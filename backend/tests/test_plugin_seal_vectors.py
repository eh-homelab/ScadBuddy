"""The plugin secrets' and sign-ins' sealing vectors (`agent_data/plugins.py`,
`api/crud_agent_plugins.py`), which Go core's copy reads too
(`tools/go/core/internal/plugins/vectors_test.go`, #8048): each AAD, each plaintext and each
envelope written here by Python's own functions, so Python and Go open each other's rows.

`UPDATE_PLUGIN_SEAL_VECTORS=1 uv run --frozen pytest tests/test_plugin_seal_vectors.py`
writes the file again; without it the test checks that the file is what these functions
write."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scadbuddy.agent_data.plugins import flow_aad, grant_aad, plugin_aad
from scadbuddy.api.crud_agent_plugins import FlowSecret, GrantSecret, _flow_text, _grant_text
from scadbuddy.core.secrets import Envelope, kek_from_base64, open_secret, seal_secret

FILE = Path(__file__).parent / "fixtures" / "plugin-seal-vectors.json"
KEK_B64 = "WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo="
#: Text a JSON writer could spell two ways: quotes, a backslash, control characters,
#: non-ASCII, an astral character, U+2028, and HTML.
ODD = 'a"b\\c\n\t\x01\x1f é 🔑 \u2028 </script> & <'

PLUGINS = [
    {"name": "docs", "url": "https://mcp.example/mcp/", "auth_header": "X-Key", "secret": "sk-1"},
    {
        "name": "odd-one",
        "url": "https://mcp.example/" + ODD,
        "auth_header": "Authorization",
        "secret": ODD,
    },
]
GRANTS: list[dict[str, Any]] = [
    {
        "plugin": "docs",
        "plugin_url": "https://mcp.example/mcp/",
        "resource": "https://mcp.example/mcp/",
        "issuer": "https://auth.example",
        "token_endpoint": "https://auth.example/token",
        "client_id": "client-1",
        "secret": {"access_token": "at-1"},
    },
    {
        "plugin": "odd-one",
        "plugin_url": "https://x/" + ODD,
        "resource": ODD,
        "issuer": "é",
        "token_endpoint": "https://t/",
        "client_id": ODD,
        "secret": {"access_token": ODD, "refresh_token": "rt-1", "client_secret": "cs-" + ODD},
    },
    {
        "plugin": "docs",
        "plugin_url": "u",
        "resource": "r",
        "issuer": "i",
        "token_endpoint": "t",
        "client_id": "c",
        "secret": {"access_token": "at", "client_secret": "cs"},
    },
]
META = {
    "issuer": "https://auth.example",
    "authorization_endpoint": "https://auth.example/authorize",
    "token_endpoint": "https://auth.example/token",
    "iss_supported": True,
    "resource": "https://mcp.example/mcp/",
    "scope": None,
    "client_id": "client-1",
    "auth_method": "none",
    "registration": "dynamic",
    "redirect_uri": "https://scadbuddy.example/api/v1/ai/plugin-oauth/callback",
}
FLOWS: list[dict[str, Any]] = [
    {
        "state_hash": "ab" * 32,
        "plugin": "docs",
        "plugin_url": "https://mcp.example/mcp/",
        "meta": META,
        "secret": {"state": "st", "verifier": "ve"},
    },
    {
        "state_hash": "cd" * 32,
        "plugin": "odd-one",
        "plugin_url": ODD,
        "meta": {**META, "client_id": ODD, "redirect_uri": "é"},
        "secret": {"state": ODD, "verifier": "v", "client_secret": ODD},
    },
]


def _counter(start: int) -> Callable[[int], bytes]:
    state = {"n": start}

    def random(size: int) -> bytes:
        out = bytes((state["n"] + i) & 0xFF for i in range(size))
        state["n"] += size
        return out

    return random


def _sealed(n: int, plaintext: str, aad: str) -> dict[str, str]:
    e = seal_secret(kek_from_base64(KEK_B64), plaintext, aad, random=_counter(n * 64))
    return {
        "aad": aad,
        "plaintext": plaintext,
        "secret_sealed_b64": base64.b64encode(e.secret_sealed).decode(),
        "dek_sealed_b64": base64.b64encode(e.dek_sealed).decode(),
        "kek_id": e.kek_id,
    }


def _vectors() -> dict[str, Any]:
    n = 0
    plugins = []
    for p in PLUGINS:
        n += 1
        aad = plugin_aad(p["name"], p["url"], p["auth_header"])
        plugins.append({**p, **_sealed(n, p["secret"], aad)})
    grants = []
    for g in GRANTS:
        n += 1
        text = _grant_text(GrantSecret(**g["secret"]))
        grants.append({**g, **_sealed(n, text, grant_aad(g))})
    flows = []
    for f in FLOWS:
        n += 1
        text = _flow_text(FlowSecret(**f["secret"]))
        aad = flow_aad(f["state_hash"], f["plugin"], f["plugin_url"], f["meta"])
        flows.append({**f, **_sealed(n, text, aad)})
    kek = kek_from_base64(KEK_B64)
    return {
        "kek_b64": KEK_B64,
        "kek_id": kek.id,
        "plugins": plugins,
        "grants": grants,
        "flows": flows,
    }


def test_the_vectors_are_what_python_writes() -> None:
    want = json.dumps(_vectors(), indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("UPDATE_PLUGIN_SEAL_VECTORS"):
        FILE.write_text(want, encoding="utf-8")
    assert FILE.read_text(encoding="utf-8") == want


def test_every_vector_opens_with_its_aad() -> None:
    doc = json.loads(FILE.read_text(encoding="utf-8"))
    kek = kek_from_base64(doc["kek_b64"])
    for v in doc["plugins"] + doc["grants"] + doc["flows"]:
        e = Envelope(
            secret_sealed=base64.b64decode(v["secret_sealed_b64"]),
            dek_sealed=base64.b64decode(v["dek_sealed_b64"]),
            kek_id=v["kek_id"],
        )
        assert open_secret(kek, e, v["aad"]) == v["plaintext"]
