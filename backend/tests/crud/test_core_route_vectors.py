"""The shared vectors that hold Go core's ports of the agent's last small CRUD routes to
Python's own functions (#8046, epic #7667): written here from Python's code with
``UPDATE_CORE_ROUTE_VECTORS=1``, and read by the Go tests in ``tools/go/core/internal``.

- ``python-json-vectors.json``: ``json.dumps(json.loads(text), ensure_ascii=False,
  separators=(",", ":"))``, how a fork's copied events are written again
  (``agent_data/events.py`` ``copy_fork_events``) and how an operation's JSON reaches its
  activity row (``jsonv.Loads``).
- ``activity-title-vectors.json``: an agent operation's activity title
  (``activity/index.py`` ``ingest_agent_operation_in``; the expression is inline there, so it is
  repeated here as written).
- ``fetch-grant-vectors.json``: the host ``parse_fetch_url`` and ``ascii_host`` read from a URL
  (``None`` for a refusal), and grants ``fetch_grants.mint`` makes for a fixed time and nonce.
- ``assistant-cost-vectors.json``: ``cost_usd`` as ``AssistantCost`` reads it, the entry's
  amount (``to_minor`` at ``UsdRate``) and its note (``api/crud_agent.py`` ``assistant_cost``).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.api.crud_agent import AssistantCost
from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.library import fetch_grants
from scadbuddy.library.asset_fetch import ascii_host, parse_fetch_url
from scadbuddy.library.url_import import ImportRefusedError
from scadbuddy.orders.lifecycle import UsdRate
from scadbuddy.orders.pricing import to_minor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
UPDATE = os.environ.get("UPDATE_CORE_ROUTE_VECTORS") == "1"

JSON_INPUTS = [
    '{"type":"user.turn","n":1,"f":1.5,"e":1e5,"big":12345678901234567890123}',
    '{"a":1.0,"b":100000.0,"c":1e16,"d":1e15,"g":0.0001,"h":0.00001,"i":-0.0,"j":-0}',
    '{"x":1E2,"y":1.50,"z":123456789.123456789,"w":2.5e-7,"v":1e400,"u":-1e400}',
    '{"s":"caf\\u00e9 \\ud83d\\ude00 \\u0001 \\"q\\" \\\\ /\\n\\t","dup":1,"dup":2}',
    '[1,2.0,[3e0],{"k":null,"t":true,"f":false}]',
    '{"sessionId":"old","type":"tool.result","images":[{"name":"a.png"}]}',
    '{"emoji":"\U0001f600","del":"\x7f","nbsp":"\u00a0"}',
    '{"n":1.7976931348623157e308,"m":5e-324,"o":123456789012345678,"p":0.1}',
]

TITLES = [
    ("fetch_asset", "https://example.com/a.stl"),
    ("import_makerworld_model", ""),
    ("RENDER_Model", "model:box"),
    ("_lead", " padded "),
    ("ßeta_ΣΑΣ", "x"),
    ("éclair", "ünïcode"),
    ("", ""),
    ("x", "\u2003tail\u2003"),
]

URLS = [
    "https://example.com/a.stl",
    "  https://Example.COM./a  ",
    "https://example.com:443/a",
    "https://example.com:8443/a",
    "https://example.com:/a",
    "https://example.com:abc/a",
    "https://example.com: 8_0 /a",
    "https://user@example.com/a",
    "https://@example.com/a",
    "https://a@b@example.com/a",
    "http://example.com/a",
    "HTTPS://example.com",
    "https:example.com",
    "https:///a",
    "https://[::1]:443/a",
    "https://[::1]443/a",
    "https://[::1]x/a",
    "https://[fe80::1%25eth0]/a",
    "https://[::ffff:1.2.3.4]/a",
    "https://[nope]/a",
    "https://1.2.3.4/a",
    "https://01.2.3.4/a",
    "https://256.2.3.4/a",
    "https://1.2.3/a",
    "https://a b.com/a",
    "https://a%20b.com/a",
    "https://[abc/a",
    "https://a]b.com/a",
    "https://ex^ample.com/a",
    "https://exa_mple.com/a",
    "https://bücher.example/a",
    "https://BÜCHER.example/a",
    "https://xn--bcher-kva.example/a",
    "https://xn--zz.example/a",
    "https://xn--bcher-kva.exa_mple/a",
    "https://ü_x.example/a",
    "https://example.com\\@evil.com/a",
    "https://exam\tple.com/a",
    "https://example.com?x=1",
    "https://example.com#frag",
    "ftp://example.com",
    "//example.com/a",
    "example.com",
    "",
    "https://.",
    "https://a..b/x",
]

GRANTS: list[tuple[str, str, int, str]] = [
    ("example.com", "https://example.com/a.stl", 1_800_000_000, "0" * 32),
    ("xn--bcher-kva.example", "https://bücher.example/x", 1_800_000_600, "0123456789abcdef" * 2),
    ("::1", "https://[::1]/a?b=c#d", 1, "f" * 32),
]
KEK = base64.b64encode(bytes(range(32))).decode()

COSTS = [
    "0.5",
    "1e-3",
    "0.00000001",
    "0.000000001",
    "0.10",
    "1.000000000",
    '"0.5"',
    "1E2",
    "1000000",
    "1000000.0000000001",
    "-0",
    "-0.0",
    "0",
    '"1_0"',
    '" 2 "',
    "1e6",
    "1.5e-7",
    "true",
    "null",
    '"nan"',
    '"inf"',
    '"abc"',
    "123456.123456789",
    "2.5",
    "0.123456780",
    "12345678901234567890.5",
    "0.1000000000",
    "1e-8",
    "5e-9",
    '"1e-8"',
    "1.0e0",
    "100",
    "0.005",
    "0.015",
    "0.0049",
    '"0.10000000000000000000000000001"',
    "-1",
    "1e400",
    "999999.99999999",
    "0.30000000000000004",
]
RATES = [
    ("1", "USD", "USD"),
    ("1.35", "EUR", "EUR"),
    ("1.35", "EUR", "USD"),
    ("1.35", "EUR", "GBP"),
    ("150.123456789", "JPY", "JPY"),
    (" 0.377 ", "KWD", "KWD"),
    ("1.123456789012345678901234567", "EUR", "EUR"),
    ("999999999.999999999", "CLF", "CLF"),
]


def _write_or_check(name: str, value: Any) -> None:
    path = FIXTURES / name
    text = json.dumps(value, ensure_ascii=False, indent=1) + "\n"
    if UPDATE:
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, f"{name} is stale: UPDATE_CORE_ROUTE_VECTORS=1"


def test_python_json_vectors() -> None:
    cases = [
        {
            "input": text,
            "output": json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")),
        }
        for text in JSON_INPUTS
    ]
    _write_or_check("python-json-vectors.json", cases)


def test_activity_title_vectors() -> None:
    cases = [
        {
            "kind": kind,
            "subject": subject,
            "title": f"{kind.replace('_', ' ').capitalize()} {subject or ''}".strip(),
        }
        for kind, subject in TITLES
    ]
    _write_or_check("activity-title-vectors.json", cases)


def _host(url: str) -> str | None:
    try:
        return ascii_host(parse_fetch_url(url))
    except (ImportRefusedError, ValueError):
        return None


def test_fetch_grant_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    key = fetch_grants.fetch_grant_key(kek_from_base64(KEK))
    grants = []
    for host, url, expires, nonce in GRANTS:
        monkeypatch.setattr(fetch_grants.secrets, "token_hex", lambda _n, nonce=nonce: nonce)
        now = datetime.fromtimestamp(expires - fetch_grants.FETCH_GRANT_TTL_SECONDS, UTC)
        grant = fetch_grants.mint(key, host, url, now=now)
        assert fetch_grants.verify(key, grant, host=host, url=url, now=now) == nonce
        grants.append(
            {"host": host, "url": url, "expires": expires, "nonce": nonce, "grant": grant}
        )
    _write_or_check(
        "fetch-grant-vectors.json",
        {"kek": KEK, "hosts": [{"url": u, "host": _host(u)} for u in URLS], "grants": grants},
    )


def _cost(raw: str) -> Decimal | None:
    body = '{"session_id":"00000000-0000-4000-8000-000000000000","cost_usd":' + raw + "}"
    try:
        return AssistantCost.model_validate_json(body).cost_usd
    except ValidationError:
        return None


def test_assistant_cost_vectors() -> None:
    session = "00000000-0000-4000-8000-000000000000"
    cases = []
    for raw in COSTS:
        cost = _cost(raw)
        for rate, shop, currency in RATES:
            case: dict[str, Any] = {
                "cost_usd": raw,
                "rate": rate,
                "shop_currency": shop,
                "order_currency": currency,
                "valid": cost is not None,
            }
            if cost is not None:
                factor = UsdRate(Decimal(rate.strip()), shop).factor(currency)
                case["convertible"] = factor is not None
                if factor is not None:
                    case["amount_minor"] = to_minor(cost * factor, currency)
                    case["note"] = f"assistant session {session}: {cost} USD"
            cases.append(case)
    _write_or_check("assistant-cost-vectors.json", cases)
