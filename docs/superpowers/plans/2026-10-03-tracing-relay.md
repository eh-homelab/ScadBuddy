# Tracing, browser relay (PR #2 of #988) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The backend takes the browser's spans at `POST /telemetry/v1/traces`, refuses
anything not from ScadBuddy's own pages or over its limits, rewrites and scrubs what it
accepts, and forwards it to the collector in the background without ever making the
browser wait or losing a batch uncounted; `SCADBUDDY_TRUSTED_PROXIES` says whose
`X-Forwarded-For` names the client a rate limit counts.

**Architecture:** A new feature package, `scadbuddy/telemetry/`, holds three focused
modules and one component: `payload.py` (pure functions: parse, rebuild, cap and scrub
an OTLP/JSON export), `admission.py` (the same-origin checks, the content type and the
two rate limits), `forwarder.py` (`TraceForwarder`: the bounded queue, the one
forwarding task, the shutdown drain, the outcome counter) and `component.py`
(`TraceRelay`, the three wired together as a `Component` whose `run` is the forwarder).
The route is `scadbuddy/api/telemetry.py`, a root route module beside `health` and
`metrics`; `main.py` mounts it before `_api_router()` and gives the body gate its
256 KiB `RouteLimit`. Who the client is comes from `scadbuddy/core/proxies.py`, a port
of the agent's `forwardedClient` rules, fed by the new bootstrap setting
`trusted_proxies`.

**Tech Stack:** Python 3.12, FastAPI/Starlette, httpx 0.28 (`MockTransport` in tests),
prometheus_client, pytest with `asyncio_mode = "auto"`. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` (§3, §4,
§5.2, §6, §8 backend relay part, §9 row 2). Row 1's plan
(`docs/superpowers/plans/2026-10-02-tracing-backend-core.md`) built what this one
stands on: `core/tracing.py` (`tracing_disabled`), `core/trace_scrub.py`, and
`main.EXCLUDED_URLS`, which already lists `/telemetry/v1/traces`.

**Branch:** from `feat/988-tracing-backend` (row 1), e.g. `feat/988-tracing-relay`.
Every path below is relative to the repository root; every `uv run` runs in `backend/`.

## Global Constraints

- The relay is `POST /telemetry/v1/traces`, outside `/api/v1`, mounted beside `/healthz` and `/metrics` before `_api_router()`, excluded from the OpenAPI schema; no agent tool and no `coverage.ts` entry (§5.2).
- The path never serves `index.html` (§5.2).
- Checks, in order, before the body is read: no `Origin` is 403; `origin_allowed` (`api/realtime.py`) must accept the `Origin`; a present `Sec-Fetch-Site` must be `same-origin`; anything else is 403 (§5.2).
- No CORS headers on any response; no preflight answered (§5.2).
- OTLP/JSON only: any other `Content-Type` is 415 (§5.2).
- Body ≤ 256 KiB through `BodySizeGate` with a `RouteLimit` for `POST /telemetry/v1/traces` in `main.py`; more than 512 spans after parsing is also 413 (§5.2).
- Rate limits in memory with `RateLimit` (`api/realtime.py`): a per-process bucket and a per-client bucket under it; over either is 429 with `Retry-After`, the seconds until the bucket that refused it refills one batch (§5.2).
- The client is the peer's address unless the peer is in `SCADBUDDY_TRUSTED_PROXIES` (CIDRs, default empty); then the LAST `X-Forwarded-For` value, trimmed; missing or empty falls back to the peer. Same rules and cases as the agent's `forwardedClient` (§5.2).
- `trusted_proxies` is a `Settings` field in `BOOTSTRAP_FIELDS` with a reason, never `ENV_SEEDED` (§5.2; `tests/test_settings_coverage.py`).
- Resource rewritten: `service.name` forced to `scadbuddy-web`; every other resource attribute dropped except `service.version` and `user_agent.original` (§5.2).
- Per-span caps: 64 attributes; strings 1024 characters; arrays 32 items; 16 events with 16 attributes each; 8 links; span name 128 characters; the excess dropped (truncated, for strings) and counted in OTel's own dropped-count field (§5.2).
- The same scrub as `ScrubbingSpanExporter` before forwarding: no `exception.message`, stack trace frame lines only, status description replaced by the exception type or `error`, no query string, no user agent attribute on a span (§6).
- Forwarding in the background: queue of 64 batches / 16 MiB; one task started and stopped in the lifespan; httpx with a 5 s timeout; no retries; a full queue drops the new batch and still answers 204 (§5.2).
- Shutdown: stop accepting (503), drain for at most 5 s in total, each post's timeout what remains of the budget, the first failed post ends the drain, the rest counted `shutdown` (§5.2).
- `scadbuddy_trace_relay_batches_total{outcome}` with `forwarded`, `failed`, `queue_full`, `shutdown`, in `core/metrics.py`, pre-created at zero; failures log at most one warning a minute naming the status or error class (§5.2).
- Tracing off (no `OTEL_EXPORTER_OTLP_ENDPOINT`, or `OTEL_SDK_DISABLED=true`): `204` with `X-ScadBuddy-Tracing: off`, nothing forwarded (§3, §5.2).
- Every refusal is an RFC 9457 problem (`application/problem+json`): 403, 415, 429 (and here 400, 405, 503) raised as `ApiError`; 413 on size is `BodySizeGate`'s own; a `detail` names the rule, never the request's values (§5.2).
- The forwarder's httpx client is never instrumented, and the route is not traced (`EXCLUDED_URLS`, already in row 1) (§4, §6).
- A new backend service is a `Component` in `scadbuddy/<feature>/component.py`, never an `AppState` field; routes read it through `component_dep` (CLAUDE.md, #508).
- `uv run --frozen ruff check .`, `ruff format --check .`, `mypy` (strict, `scadbuddy` and `tests`) and `pytest` must pass (CLAUDE.md "Commands"). `tests/api` needs `SCADBUDDY_TEST_DATABASE_URL` and a Temporal (CLAUDE.md).

## Review Focus

1. **A hostile body** (JSON nested 100 000 deep, bytes that are not UTF-8, a lone surrogate escape `"\ud800"`): a 400 or a clean forward, never a 500 from `RecursionError` or `UnicodeEncodeError`. Test in Task 2 (`test_a_body_that_is_not_an_export_is_refused` `deep`, `not-utf8`; `test_a_lone_surrogate_is_forwarded_escaped`).
2. **A `GET` (or `HEAD`, `OPTIONS`) on the relay path, or `/telemetry/v1/traces/`, with the SPA mounted**: verified on this tree that a `POST`-only route lets `GET` fall through to the mount and serve `index.html` (200). The plan answers every other method with a 405 problem and every other `/telemetry` path with a 404 problem. Test in Task 5 (`test_another_method_is_405_never_the_page`, `test_no_other_telemetry_path_serves_the_page`, and the real-app `test_the_page_is_never_served_for_it`).
3. **A proxy that sends `X-Forwarded-For` as two header lines**: Starlette's `headers.get` returns only the first, Node joins them; the relay joins every line before taking the last value, so the backend and agent agree. Test in Task 4 (`test_a_trusted_proxy_names_the_client_by_its_last_value`) and Task 5 (`test_a_trusted_proxy_names_the_client_the_bucket_counts`).
4. **A collector that accepts the connection and never answers, at shutdown**: the drain ends inside its budget whatever the transport does (`asyncio.timeout` around each post, not only httpx's timeout). Test in Task 3 (`test_a_hung_collector_ends_the_drain_within_the_budget`, `test_a_post_cut_off_by_shutdown_counts_as_shutdown`).
5. **Many distinct client addresses** (a scan, or a busy NAT with the proxies list set): the per-client buckets stay bounded (4096, least recently seen forgotten) instead of growing without limit. Test in Task 4 (`test_the_client_buckets_are_bounded`).

## Where this plan fills in or departs from the spec

Each is resolved here; a reviewer who disagrees should change the spec first.

1. **"Registered before the SPA's static fallback, so the path never serves `index.html`"** is not enough on its own (Review Focus 2). Resolution: the route module also answers other methods (405, `Allow: POST`) and `/telemetry` and `/telemetry/{rest:path}` (404).
2. **The rates.** §5.2 speaks of "the configured rate" but row 2 adds only `SCADBUDDY_TRUSTED_PROXIES`, and a `Settings` field would have to be env-seeded (patch, view, `APPLIES`) or bootstrap. Resolution: module constants in `telemetry/admission.py` (per process: burst 100, 20/s; per client: burst 20, 2/s), with the per-pod note "documented beside" them as §5.2 asks. A change of replica count changes the constants.
3. **What "counts it in `otel.dropped_attributes_count`" means per cap.** Resolution: a whole attribute, event or link dropped is counted in OTLP's own field for its kind (`droppedAttributesCount` on the span, event or link; `droppedEventsCount`; `droppedLinksCount`), and the browser's own counts are added to. A string or array cut to its cap is not counted, as the SDK's own limits do not count one.
4. **Link attributes** have no cap in §5.2. Resolution: 16, the event cap. Row 4 must set the browser provider's `spanLimits` to match (`attributePerLinkCountLimit: 16`, with the rest of §5.2's numbers).
5. **A body that is not an OTLP/JSON export** is not in §5.2's list. Resolution: 400 problem, "the body is not an OTLP/JSON trace export".
6. **"On SIGTERM the lifespan stops accepting."** The forwarder is a `Component` (CLAUDE.md requires it), so its `run` exits with the other components: after the previews, the print watcher and, for `SCADBUDDY_TEMPORAL_WORKER_INPROCESS`, the in-process worker's drain (`main.lifespan`'s `finally`). In production the worker is separate, so the drain starts within moments of SIGTERM; in the one-process dev mode it can start up to `activity_timeout` later. Accepted.
7. **`user_agent.original`.** Row 1's scrub drops it from span attributes; §5.2 keeps it on the browser resource. Resolution: kept on the resource (spec), dropped from span attributes (the shared scrub).
8. **The agent's `parseCidrList` reads `10.0.0.0/` as `/0`** (`Number('')` is 0), trusting every peer. The backend refuses it at start. Recorded in `core/proxies.py`'s docstring; worth a follow-up issue against the agent, not fixed here.
9. **`public_url`** is read from the running `Settings` (`state.settings`, kept live by `api/runtime.py` on every `settings.changed`) rather than with a database read per request as `api/realtime.py` does, since the relay's job is to be cheap.
10. **The agent's headless browser** marks its requests with `X-ScadBuddy-Agent-Session`, and `AgentActorGate` refuses every unlisted write, so its page's relay posts get a 403 before the relay sees them. Harmless (those spans are dropped); row 4's exporter should treat 403 like 413/429 (drop, no retry).
11. **The endpoint.** The relay posts to `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`, the same variable and rule as `core/tracing.py` `build_provider`; `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` are not read (§3 lists neither).

## File structure

| File | Task | Responsibility |
|---|---|---|
| `backend/scadbuddy/core/proxies.py` (new) | 1 | CIDR list parsing and the forwarded-client rules, ported from the agent |
| `backend/scadbuddy/core/settings.py` | 1 | `trusted_proxies` field, validator, `trusted_proxy_networks`, `BOOTSTRAP_FIELDS` entry |
| `backend/tests/test_proxies.py` (new) | 1 | the agent's cases |
| `backend/scadbuddy/core/trace_scrub.py` | 2 | `_DROPPED`/`_CUT_AT_QUERY` become public `DROPPED_ATTRIBUTES`/`CUT_AT_QUERY` |
| `backend/scadbuddy/telemetry/__init__.py` (new, empty) | 2 | the feature package |
| `backend/scadbuddy/telemetry/payload.py` (new) | 2 | parse, rebuild, cap and scrub an export |
| `backend/tests/support/otlp.py` (new) | 2 | OTLP/JSON builders shared by the relay's tests |
| `backend/tests/test_trace_relay_payload.py` (new) | 2 | caps, rewrite, scrub, malformed bodies |
| `backend/scadbuddy/core/metrics.py` | 3 | `TraceRelayOutcome`, `trace_relay_batches` |
| `backend/scadbuddy/telemetry/forwarder.py` (new) | 3 | `TraceForwarder`, `relay_endpoint` |
| `backend/tests/test_trace_relay_forwarding.py` (new) | 3 | queue, posts, drain, counter, warning |
| `backend/scadbuddy/api/realtime.py` | 4 | `RateLimit.retry_after` |
| `backend/scadbuddy/telemetry/admission.py` (new) | 4 | `check_origin`, `check_content_type`, `relay_client`, `RelayLimits` |
| `backend/tests/test_trace_relay_admission.py` (new) | 4 | origin, content type, limits, client |
| `backend/scadbuddy/telemetry/component.py` (new) | 5 | `TraceRelay`, `TRACE_RELAY`, `COMPONENT`, `TraceRelayDep` |
| `backend/scadbuddy/api/telemetry.py` (new) | 5 | the route, its siblings, `RELAY_ROUTE_LIMIT` |
| `backend/scadbuddy/main.py` | 5 | root mount, `ROOT_ROUTE_MODULES`, the route limit |
| `backend/tests/test_trace_relay_route.py` (new) | 5 | the HTTP surface, no database |
| `backend/tests/api/test_trace_relay_app.py` (new) | 5 | the real app's wiring |
| `README.md`, `CLAUDE.md` | 5 | the relay and `SCADBUDDY_TRUSTED_PROXIES` |

---

### Task 1: `SCADBUDDY_TRUSTED_PROXIES` and the forwarded-client rules

**Files:**
- Create: `backend/scadbuddy/core/proxies.py`
- Modify: `backend/scadbuddy/core/settings.py` (import after the `scadbuddy.core.config` import block; field after `allowed_origins`; validator before `_web_urls_are_http`; property after `allowed_origin_list`; `BOOTSTRAP_FIELDS` entry after `"allowed_origins"`)
- Test: `backend/tests/test_proxies.py`

**Interfaces:**
- Produces (Tasks 4 and 5 use them):
  - `type Network = ipaddress.IPv4Network | ipaddress.IPv6Network`
  - `class ProxyConfigError(ValueError)`
  - `def parse_cidr_list(raw: str | None, name: str = "SCADBUDDY_TRUSTED_PROXIES") -> tuple[Network, ...]`
  - `def in_networks(networks: Sequence[Network], address: str | None) -> bool`
  - `def last_value(value: str | None) -> str | None`
  - `def forwarded_client(peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]) -> str | None`
  - `def client_address(peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]) -> str | None`
  - `Settings.trusted_proxies: str = ""` and `Settings.trusted_proxy_networks -> tuple[Network, ...]` (property)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_proxies.py
"""core/proxies.py: the agent's forwarded-client rules, ported (spec 2026-10-01 §5.2).

The cases mirror ``agent/test/origins.test.ts`` ("trusted proxies") and
``agent/test/mcpAuthMode.test.ts`` (the client a proxy names), so the backend and the
agent agree about who a client is."""

from __future__ import annotations

import ipaddress

import pytest
from pydantic import ValidationError

from scadbuddy.core.proxies import (
    ProxyConfigError,
    client_address,
    forwarded_client,
    in_networks,
    last_value,
    parse_cidr_list,
)
from scadbuddy.core.settings import BOOTSTRAP_FIELDS, ENV_SEEDED, Settings
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

INGRESS = "10.42.0.0/16"


def test_addresses_and_ranges_of_both_families_parse() -> None:
    networks = parse_cidr_list("10.42.0.0/16, 192.168.1.10 ,fd00::/8")
    assert in_networks(networks, "10.42.9.9")
    assert not in_networks(networks, "10.43.0.1")
    assert in_networks(networks, "192.168.1.10")
    assert not in_networks(networks, "192.168.1.11")
    assert in_networks(networks, "fd12::1")


def test_host_bits_under_the_prefix_are_ignored() -> None:
    assert parse_cidr_list("10.42.9.9/16") == (ipaddress.ip_network("10.42.0.0/16"),)


def test_empty_trusts_no_one() -> None:
    assert parse_cidr_list("") == ()
    assert parse_cidr_list(None) == ()
    assert parse_cidr_list(" , ") == ()


@pytest.mark.parametrize(
    "entry",
    ["10.0.0.0/33", "fd00::/129", "not-an-ip", "10.0.0.0/8/1", "10.0.0.0/abc", "10.0.0.0/", "/8"],
)
def test_a_malformed_entry_is_refused_by_name(entry: str) -> None:
    with pytest.raises(ProxyConfigError, match=f'SCADBUDDY_TRUSTED_PROXIES: "{entry}"'):
        parse_cidr_list(f"10.42.0.0/16, {entry}")


def test_an_ipv4_mapped_peer_is_matched_as_ipv4() -> None:
    assert in_networks(parse_cidr_list(INGRESS), "::ffff:10.42.0.5")
    assert in_networks(parse_cidr_list(INGRESS), "::FFFF:10.42.0.5")


def test_a_peer_that_is_not_an_address_is_never_trusted() -> None:
    networks = parse_cidr_list(INGRESS)
    assert not in_networks(networks, None)
    assert not in_networks(networks, "testclient")
    assert not in_networks(networks, "")


def test_the_last_value_is_taken_trimmed_and_empty_is_none() -> None:
    assert last_value("198.51.100.1, 203.0.113.9") == "203.0.113.9"
    assert last_value(" 203.0.113.9 ") == "203.0.113.9"
    assert last_value("203.0.113.9, ") is None
    assert last_value("") is None
    assert last_value(None) is None


def test_a_trusted_peer_names_the_client_with_its_last_value() -> None:
    trusted = parse_cidr_list(INGRESS)
    assert forwarded_client("10.42.0.5", "198.51.100.1, 203.0.113.9", trusted) == "203.0.113.9"
    assert forwarded_client("::ffff:10.42.0.5", "203.0.113.9", trusted) == "203.0.113.9"


def test_an_untrusted_peer_is_not_believed() -> None:
    trusted = parse_cidr_list(INGRESS)
    assert forwarded_client("10.43.0.5", "203.0.113.9", trusted) is None
    # From loopback no proxy is involved unless it is listed.
    assert forwarded_client("127.0.0.1", "203.0.113.9", trusted) is None
    assert forwarded_client("10.42.0.5", "203.0.113.9", ()) is None


def test_the_client_is_the_peer_unless_a_trusted_proxy_names_one() -> None:
    trusted = parse_cidr_list(INGRESS)
    assert client_address("10.42.0.5", "198.51.100.1, 203.0.113.9", trusted) == "203.0.113.9"
    assert client_address("10.43.0.5", "203.0.113.9", trusted) == "10.43.0.5"
    assert client_address("10.42.0.5", "203.0.113.9, ", trusted) == "10.42.0.5"
    assert client_address("10.42.0.5", None, trusted) == "10.42.0.5"
    assert client_address(None, None, trusted) is None


def _settings(trusted_proxies: str = "") -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        trusted_proxies=trusted_proxies,
    )


def test_the_setting_is_a_bootstrap_field_parsed_once_valid() -> None:
    assert "trusted_proxies" in BOOTSTRAP_FIELDS
    assert "trusted_proxies" not in ENV_SEEDED
    assert _settings().trusted_proxy_networks == ()
    assert _settings(trusted_proxies="10.42.0.0/16").trusted_proxy_networks == (
        ipaddress.ip_network("10.42.0.0/16"),
    )


def test_a_malformed_setting_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_TRUSTED_PROXIES", "10.0.0.0/33")
    with pytest.raises(ValidationError, match="SCADBUDDY_TRUSTED_PROXIES"):
        Settings(database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_proxies.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'scadbuddy.core.proxies'`.

- [ ] **Step 3: Write `core/proxies.py`**

```python
# backend/scadbuddy/core/proxies.py
"""Who a request is from, behind a proxy the deployment trusts (spec 2026-10-01 §5.2).

A port of the agent's rules in ``agent/src/http/origins.ts`` (``parseCidrList``,
``plainAddress``, ``inBlockList``, ``lastValue``, ``forwardedClient``), with the same
cases in ``tests/test_proxies.py``, so the two services cannot disagree about who a
client is. ``X-Forwarded-For`` is believed only from a peer in
``SCADBUDDY_TRUSTED_PROXIES``, and only its LAST value is taken: the one the nearest
proxy, the trusted peer, appended. Earlier values may have come from the client. One
trusted hop is all the cluster has (Envoy Gateway in front of the pod), so there is no
right-to-left walk over several.

One deliberate difference: the agent reads ``10.0.0.0/`` as ``/0`` (``Number('')`` is
0), which trusts every peer. Here it is refused at start like any other malformed entry.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from typing import Final

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

TRUSTED_PROXIES_VAR: Final = "SCADBUDDY_TRUSTED_PROXIES"
_MAPPED: Final = re.compile(r"^::ffff:(\d+\.\d+\.\d+\.\d+)$", re.IGNORECASE)
_PREFIX: Final = re.compile(r"^[0-9]+$")


class ProxyConfigError(ValueError):
    pass


def _network(entry: str) -> Network | None:
    address, *prefixes = entry.split("/")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    prefix = prefixes[0] if prefixes else str(parsed.max_prefixlen)
    if len(prefixes) > 1 or not _PREFIX.match(prefix) or int(prefix) > parsed.max_prefixlen:
        return None
    return ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)


def parse_cidr_list(raw: str | None, name: str = TRUSTED_PROXIES_VAR) -> tuple[Network, ...]:
    """``10.0.0.0/8, fd00::/8, 192.168.1.10``: a bare address is a /32 or /128, and host
    bits under the prefix are ignored, as Node's ``BlockList.addSubnet`` ignores them."""
    networks: list[Network] = []
    for entry in (part.strip() for part in (raw or "").split(",")):
        if not entry:
            continue
        network = _network(entry)
        if network is None:
            raise ProxyConfigError(f'{name}: "{entry}" is not an IP address or CIDR range')
        networks.append(network)
    return tuple(networks)


def plain_address(address: str) -> str:
    """Unwraps an IPv4-mapped IPv6 peer (``::ffff:10.0.0.1``), which a dual-stack
    socket reports."""
    mapped = _MAPPED.match(address)
    return mapped.group(1) if mapped else address


def in_networks(networks: Sequence[Network], address: str | None) -> bool:
    if address is None:
        return False
    try:
        parsed = ipaddress.ip_address(plain_address(address))
    except ValueError:
        return False
    # An address of the other family is in no network: `in` answers False, not an error.
    return any(parsed in network for network in networks)


def last_value(value: str | None) -> str | None:
    """The last comma-separated value of a header, trimmed: the one the nearest proxy
    added. Empty is none."""
    if value is None:
        return None
    return value.split(",")[-1].strip() or None


def forwarded_client(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]
) -> str | None:
    """The client a trusted proxy names; None from any other peer, whose header is not
    believed."""
    return last_value(forwarded_for) if in_networks(trusted, peer) else None


def client_address(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]
) -> str | None:
    """Who the request is from: the client a trusted proxy names, else the peer itself.
    A missing or empty last value from a trusted proxy falls back to the peer."""
    return forwarded_client(peer, forwarded_for, trusted) or peer


__all__ = [
    "TRUSTED_PROXIES_VAR",
    "Network",
    "ProxyConfigError",
    "client_address",
    "forwarded_client",
    "in_networks",
    "last_value",
    "parse_cidr_list",
    "plain_address",
]
```

- [ ] **Step 4: Add the setting**

In `backend/scadbuddy/core/settings.py`:

After the closing `)` of the `from scadbuddy.core.config import (...)` block, add:

```python
from scadbuddy.core.proxies import Network, parse_cidr_list
```

Directly after `    allowed_origins: str = ""` (inside `class Settings`), add:

```python
    # SCADBUDDY_TRUSTED_PROXIES: comma-separated CIDRs (a bare address is one host) whose
    # `X-Forwarded-For` is believed, and then only its last value (`core/proxies.py`, the
    # agent's SCADBUDDY_AGENT_TRUSTED_PROXIES rules). The browser trace relay's per-client
    # rate limit keys on it (spec 2026-10-01 §5.2). Empty, no forwarding header is
    # believed and every browser behind the gateway shares one client bucket. Not a
    # stored setting: it decides who the server believes about who it is talking to.
    trusted_proxies: str = ""

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_are_cidrs(cls, value: str) -> str:
        parse_cidr_list(value)
        return value
```

(The existing `@field_validator("bambuddy_web_urls")` follows it unchanged.)

Directly after the `allowed_origin_list` property, add:

```python
    @property
    def trusted_proxy_networks(self) -> tuple[Network, ...]:
        """`SCADBUDDY_TRUSTED_PROXIES` parsed; the validator has already refused a bad one."""
        return parse_cidr_list(self.trusted_proxies)
```

In `BOOTSTRAP_FIELDS`, directly after the `"allowed_origins": (...)` entry, add:

```python
        "trusted_proxies": (
            "Which peers are believed about the client they forward for. Like the allowed"
            " origins, it decides who the server believes it is talking to, so it belongs"
            " to the deployment."
        ),
```

- [ ] **Step 5: Run the tests, and the settings checks that every field is classified**

Run: `cd backend && uv run --frozen pytest tests/test_proxies.py tests/test_settings_coverage.py tests/test_settings_reference.py -v`
Expected: PASS (37 tests). `test_every_setting_is_env_seeded_or_bootstrap` passes only with the `BOOTSTRAP_FIELDS` entry.

- [ ] **Step 6: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/core/proxies.py backend/scadbuddy/core/settings.py backend/tests/test_proxies.py
git commit -m "feat(tracing): SCADBUDDY_TRUSTED_PROXIES, the agent's forwarded-client rules in Python (#988)"
```

---

### Task 2: Rebuild, cap and scrub a page's spans

**Files:**
- Modify: `backend/scadbuddy/core/trace_scrub.py:29-32,85-88,128` (rename two constants public)
- Create: `backend/scadbuddy/telemetry/__init__.py` (empty), `backend/scadbuddy/telemetry/payload.py`
- Create: `backend/tests/support/otlp.py`
- Test: `backend/tests/test_trace_relay_payload.py`

**Interfaces:**
- Consumes: `scadbuddy.core.trace_scrub.DROPPED_ATTRIBUTES`, `CUT_AT_QUERY` (this task makes them public).
- Produces (Task 5 uses them):
  - `MAX_SPANS: Final = 512`, and the other caps (`MAX_NAME_CHARS`, `MAX_ATTRIBUTES`, `MAX_STRING_CHARS`, `MAX_ARRAY_ITEMS`, `MAX_EVENTS`, `MAX_EVENT_ATTRIBUTES`, `MAX_LINKS`, `MAX_LINK_ATTRIBUTES`)
  - `class PayloadError(ValueError)`, `class TooManySpansError(ValueError)`
  - `def prepare(body: bytes) -> bytes` (raises either error)
  - `tests/support/otlp.py`: `SENTINEL: str`, `TRACE_ID`, `SPAN_ID`, `type Json = dict[str, Any]`, `def string(key: str, value: str) -> Json`, `def span(**fields: Any) -> Json`, `def export(*spans: Json, resource: list[Json] | None = None) -> bytes`

- [ ] **Step 1: Make the scrub's attribute sets public**

In `backend/scadbuddy/core/trace_scrub.py`, replace

```python
#: Attributes the HTTP instrumentation fills from the request's own text: a query
#: string can carry anything a user typed, a user agent is a header value.
_DROPPED: Final = frozenset({"url.query", "http.user_agent", "user_agent.original"})
_CUT_AT_QUERY: Final = frozenset({"http.url", "url.full", "http.target"})
```

with

```python
#: Attributes the HTTP instrumentation fills from the request's own text: a query
#: string can carry anything a user typed, a user agent is a header value. The browser
#: relay applies the same two sets to the page's spans (`telemetry/payload.py`).
DROPPED_ATTRIBUTES: Final = frozenset({"url.query", "http.user_agent", "user_agent.original"})
CUT_AT_QUERY: Final = frozenset({"http.url", "url.full", "http.target"})
```

In `_scrub_attributes`, change `if key in _DROPPED:` to `if key in DROPPED_ATTRIBUTES:` and
`if key in _CUT_AT_QUERY and isinstance(value, str):` to
`if key in CUT_AT_QUERY and isinstance(value, str):`. Replace the last line with:

```python
__all__ = [
    "CUT_AT_QUERY",
    "DROPPED_ATTRIBUTES",
    "ScrubbingSpanExporter",
    "frames_only",
    "scrub",
]
```

Run: `cd backend && uv run --frozen pytest tests/test_trace_scrub.py -q`
Expected: PASS (nothing else names the old constants; `grep -rn "_DROPPED\b\|_CUT_AT_QUERY" backend/` finds nothing).

- [ ] **Step 2: Write the shared builders and the failing tests**

```python
# backend/tests/support/otlp.py
"""OTLP/JSON trace exports as a browser's exporter writes them, for the relay's tests."""

from __future__ import annotations

import json
from typing import Any

type Json = dict[str, Any]

#: Put where a value must never reach the collector; asserted absent from what is sent.
SENTINEL = "SENTINEL-7f3a9c"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"


def string(key: str, value: str) -> Json:
    return {"key": key, "value": {"stringValue": value}}


def span(**fields: Any) -> Json:
    return {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "name": "Generate",
        "kind": 1,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000100000000",
        "attributes": [],
        "events": [],
        "links": [],
        "status": {"code": 0},
        **fields,
    }


def export(*spans: Json, resource: list[Json] | None = None) -> bytes:
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource or []},
                    "scopeSpans": [{"scope": {"name": "scadbuddy-web"}, "spans": list(spans)}],
                }
            ]
        }
    ).encode()
```

```python
# backend/tests/test_trace_relay_payload.py
"""telemetry/payload.py: what the relay forwards of a page's spans (spec 2026-10-01 §5.2, §6)."""

from __future__ import annotations

import json

import pytest

from scadbuddy.telemetry import payload
from scadbuddy.telemetry.payload import PayloadError, TooManySpansError, prepare
from tests.support.otlp import SENTINEL, SPAN_ID, TRACE_ID, Json, export, span, string


def forwarded(body: bytes) -> Json:
    result: Json = json.loads(prepare(body))
    return result


def only_span(body: bytes) -> Json:
    (scope,) = forwarded(body)["resourceSpans"][0]["scopeSpans"]
    (result,) = scope["spans"]
    return dict(result)


def test_the_resource_is_rebuilt_as_the_web_service() -> None:
    body = export(
        span(),
        resource=[
            string("service.name", "scadbuddy-api"),
            string("service.version", "1.2.3"),
            string("user_agent.original", "Mozilla/5.0"),
            string("host.name", "attacker"),
            string("telemetry.sdk.name", "opentelemetry"),
        ],
    )
    resource = forwarded(body)["resourceSpans"][0]["resource"]
    assert resource == {
        "attributes": [
            string("service.name", "scadbuddy-web"),
            string("service.version", "1.2.3"),
            string("user_agent.original", "Mozilla/5.0"),
        ]
    }


def test_a_span_keeps_its_ids_times_and_kind_and_nothing_unknown() -> None:
    result = only_span(export(span(parentSpanId="00f067aa0ba902b7", extra=SENTINEL)))
    assert result["traceId"] == TRACE_ID
    assert result["spanId"] == SPAN_ID
    assert result["parentSpanId"] == "00f067aa0ba902b7"
    assert result["kind"] == 1
    assert result["startTimeUnixNano"] == "1700000000000000000"
    assert "extra" not in result


@pytest.mark.parametrize(("count", "kept", "dropped"), [(64, 64, 0), (65, 64, 1)])
def test_attributes_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    attributes = [string(f"a{i}", "v") for i in range(count)]
    result = only_span(export(span(attributes=attributes, droppedAttributesCount=2)))
    assert len(result["attributes"]) == kept
    assert result["droppedAttributesCount"] == 2 + dropped


@pytest.mark.parametrize(("length", "kept"), [(1024, 1024), (1025, 1024)])
def test_a_string_value_is_truncated_uncounted(length: int, kept: int) -> None:
    result = only_span(export(span(attributes=[string("a", "x" * length)])))
    assert len(result["attributes"][0]["value"]["stringValue"]) == kept
    assert result["droppedAttributesCount"] == 0


@pytest.mark.parametrize(("length", "kept"), [(32, 32), (33, 32)])
def test_an_array_value_is_cut_to_its_cap(length: int, kept: int) -> None:
    values = [{"intValue": str(i)} for i in range(length)]
    attribute = {"key": "a", "value": {"arrayValue": {"values": values}}}
    result = only_span(export(span(attributes=[attribute])))
    assert len(result["attributes"][0]["value"]["arrayValue"]["values"]) == kept


@pytest.mark.parametrize(("count", "kept", "dropped"), [(16, 16, 0), (17, 16, 1)])
def test_events_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    events = [{"name": f"e{i}", "timeUnixNano": "1", "attributes": []} for i in range(count)]
    result = only_span(export(span(events=events)))
    assert len(result["events"]) == kept
    assert result["droppedEventsCount"] == dropped


@pytest.mark.parametrize(("count", "kept", "dropped"), [(16, 16, 0), (17, 16, 1)])
def test_event_attributes_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    event = {"name": "e", "attributes": [string(f"a{i}", "v") for i in range(count)]}
    (result,) = only_span(export(span(events=[event])))["events"]
    assert len(result["attributes"]) == kept
    assert result["droppedAttributesCount"] == dropped


@pytest.mark.parametrize(("count", "kept", "dropped"), [(8, 8, 0), (9, 8, 1)])
def test_links_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    links = [{"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": []} for _ in range(count)]
    result = only_span(export(span(links=links)))
    assert len(result["links"]) == kept
    assert result["droppedLinksCount"] == dropped


@pytest.mark.parametrize(("count", "kept", "dropped"), [(16, 16, 0), (17, 16, 1)])
def test_link_attributes_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    link = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "attributes": [string(f"a{i}", "v") for i in range(count)],
    }
    (result,) = only_span(export(span(links=[link])))["links"]
    assert len(result["attributes"]) == kept
    assert result["droppedAttributesCount"] == dropped


@pytest.mark.parametrize(("length", "kept"), [(128, 128), (129, 128)])
def test_the_name_is_truncated(length: int, kept: int) -> None:
    assert len(only_span(export(span(name="n" * length)))["name"]) == kept


def test_a_value_no_page_sends_is_dropped_and_counted() -> None:
    attributes = [
        {"key": "map", "value": {"kvlistValue": {"values": []}}},
        {"key": "bytes", "value": {"bytesValue": "AAAA"}},
        {"key": "nested", "value": {"arrayValue": {"values": [{"arrayValue": {}}]}}},
        {"key": "big", "value": {"intValue": "1" * 30}},
        {"value": {"stringValue": "no key"}},
        "not an attribute",
        string("kept", "v"),
    ]
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("kept", "v")]
    assert result["droppedAttributesCount"] == 6


def test_query_strings_and_user_agents_are_scrubbed_as_the_backend_does() -> None:
    attributes = [
        string("http.url", f"https://scadbuddy.example/api/v1/models?q={SENTINEL}"),
        string("url.query", f"q={SENTINEL}"),
        string("user_agent.original", SENTINEL),
        string("http.user_agent", SENTINEL),
    ]
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("http.url", "https://scadbuddy.example/api/v1/models")]
    assert result["droppedAttributesCount"] == 0


def test_an_exception_keeps_its_type_and_frames_only() -> None:
    stack = (
        f"TypeError: {SENTINEL}\n"
        f"second line of the message {SENTINEL}\n"
        "    at render (https://scadbuddy.example/assets/index-abc.js:10:5)\n"
        "    at https://scadbuddy.example/assets/index-abc.js:20:7\n"
        "onClick@https://scadbuddy.example/assets/index-abc.js:30:9"
    )
    event = {
        "name": "exception",
        "timeUnixNano": "1",
        "attributes": [
            string("exception.type", "TypeError"),
            string("exception.message", SENTINEL),
            string("exception.stacktrace", stack),
        ],
    }
    status = {"code": 2, "message": SENTINEL}
    body = export(span(events=[event], status=status))
    assert SENTINEL not in prepare(body).decode()
    result = only_span(body)
    assert result["status"] == {"code": 2, "message": "TypeError"}
    (scrubbed,) = result["events"]
    assert scrubbed["attributes"] == [
        string("exception.type", "TypeError"),
        string(
            "exception.stacktrace",
            "at render (https://scadbuddy.example/assets/index-abc.js:10:5)\n"
            "at https://scadbuddy.example/assets/index-abc.js:20:7\n"
            "onClick@https://scadbuddy.example/assets/index-abc.js:30:9",
        ),
    ]


def test_a_status_description_without_an_exception_reads_error() -> None:
    result = only_span(export(span(status={"code": 2, "message": SENTINEL})))
    assert result["status"] == {"code": 2, "message": "error"}


def test_512_spans_pass_and_513_do_not() -> None:
    body = export(*[span() for _ in range(payload.MAX_SPANS)])
    assert len(forwarded(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 512
    with pytest.raises(TooManySpansError):
        prepare(export(*[span() for _ in range(payload.MAX_SPANS + 1)]))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b"{}",
        b'{"resourceSpans": {}}',
        b'{"resourceSpans": [1]}',
        b'{"resourceSpans": [{"scopeSpans": [{"spans": [1]}]}]}',
        b"[" * 100_000,
    ],
    ids=["text", "not-utf8", "array", "empty", "object", "number", "span-number", "deep"],
)
def test_a_body_that_is_not_an_export_is_refused(body: bytes) -> None:
    with pytest.raises(PayloadError):
        prepare(body)


def test_a_lone_surrogate_is_forwarded_escaped() -> None:
    body = b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"name":"\\ud800"}]}]}]}'
    assert b"\\ud800" in prepare(body)
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_payload.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'scadbuddy.telemetry'`.

- [ ] **Step 4: Write the package and `payload.py`**

Create `backend/scadbuddy/telemetry/__init__.py` empty (a feature package: `core/components.py` `discover_components` looks for its `component.py` in Task 5).

```python
# backend/scadbuddy/telemetry/payload.py
"""Browser spans made safe to forward (spec 2026-10-01 §5.2, §6).

The relay's input is untrusted: any script on the page writes it. So nothing passes
through by default. The resource is rebuilt with ``service.name`` forced to
``scadbuddy-web``; each span is rebuilt from the OTLP/JSON fields it may carry; every
list and string is capped at the limits the browser SDK's provider is configured with
(the frontend ``RelayExporter``'s ``spanLimits``), so a well-behaved page never meets
them; and the backend's own scrub (`core/trace_scrub.py`) is applied: no exception
message, no query string, no user agent on a span.

What is dropped for a cap is counted in OTLP's own field for its kind, as the SDK's
limits count it: ``droppedAttributesCount`` on the span, an event or a link,
``droppedEventsCount``, ``droppedLinksCount``. A string or an array cut to its cap is
not counted, as the SDK does not count one.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from scadbuddy.core.trace_scrub import CUT_AT_QUERY, DROPPED_ATTRIBUTES

MAX_SPANS: Final = 512
MAX_NAME_CHARS: Final = 128
MAX_ATTRIBUTES: Final = 64
MAX_STRING_CHARS: Final = 1024
MAX_ARRAY_ITEMS: Final = 32
MAX_EVENTS: Final = 16
MAX_EVENT_ATTRIBUTES: Final = 16
MAX_LINKS: Final = 8
#: The spec names no cap for a link's attributes; the event cap, which the frontend's
#: ``spanLimits`` sets as ``attributePerLinkCountLimit`` too.
MAX_LINK_ATTRIBUTES: Final = 16

WEB_SERVICE_NAME: Final = "scadbuddy-web"
#: Every other resource attribute the page sends is dropped.
KEPT_RESOURCE_ATTRIBUTES: Final = ("service.version", "user_agent.original")

#: Span and link fields forwarded as they came: ids, times, kind and flags. A wrong
#: value makes the collector refuse the batch, which the relay counts as ``failed``.
_SPAN_FIELDS: Final = (
    "traceId",
    "spanId",
    "parentSpanId",
    "traceState",
    "flags",
    "kind",
    "startTimeUnixNano",
    "endTimeUnixNano",
)
_LINK_FIELDS: Final = ("traceId", "spanId", "traceState", "flags")
#: OTLP/JSON writes a 64-bit integer as a decimal string.
_INT_STRING: Final = re.compile(r"^-?[0-9]{1,19}$")
#: A browser stack frame: V8's ``    at f (https://…/x.js:1:2)``, or Firefox and
#: Safari's ``f@https://…/x.js:1:2``. Every other line of a stack is the message.
_BROWSER_FRAME: Final = re.compile(r"^(?:\s+at \S.*:\d+:\d+\)?|[^\s@]*@\S+:\d+:\d+)$")

type Json = dict[str, Any]


class PayloadError(ValueError):
    """The body is not an OTLP/JSON trace export."""


class TooManySpansError(ValueError):
    """The batch holds more than :data:`MAX_SPANS` spans."""


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise PayloadError
    return value


def parse(body: bytes) -> Json:
    """The export's JSON, checked for the shape `rewrite` walks, and its span count."""
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        # ValueError covers bad JSON and bytes that are not UTF-8; RecursionError, a
        # body nested deeper than the decoder recurses.
        raise PayloadError from error
    if not isinstance(payload, dict):
        raise PayloadError
    count = 0
    for resource_spans in _list(payload.get("resourceSpans")):
        if not isinstance(resource_spans, dict):
            raise PayloadError
        for scope_spans in _list(resource_spans.get("scopeSpans", [])):
            if not isinstance(scope_spans, dict):
                raise PayloadError
            spans = _list(scope_spans.get("spans", []))
            if not all(isinstance(span, dict) for span in spans):
                raise PayloadError
            count += len(spans)
    if count > MAX_SPANS:
        raise TooManySpansError
    return payload


def browser_frames_only(stack: str) -> str:
    """The frame lines of a browser stack, and nothing else: the message is the rest."""
    return "\n".join(line.strip() for line in stack.splitlines() if _BROWSER_FRAME.match(line))


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _scalar(value: object) -> Json | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((kind, inner),) = value.items()
    if kind == "stringValue" and isinstance(inner, str):
        return {kind: inner[:MAX_STRING_CHARS]}
    if kind == "boolValue" and isinstance(inner, bool):
        return {kind: inner}
    if kind == "intValue" and (
        (isinstance(inner, int) and not isinstance(inner, bool))
        or (isinstance(inner, str) and _INT_STRING.match(inner))
    ):
        return {kind: inner}
    if kind == "doubleValue" and isinstance(inner, int | float) and not isinstance(inner, bool):
        return {kind: inner}
    return None


def _value(value: object) -> Json | None:
    """A scalar, or an array of at most :data:`MAX_ARRAY_ITEMS` scalars; anything else
    (a key-value list, bytes, a nested array) is not a value a page sends."""
    scalar = _scalar(value)
    if scalar is not None:
        return scalar
    if not isinstance(value, dict) or set(value) != {"arrayValue"}:
        return None
    array = value["arrayValue"]
    items = array.get("values", []) if isinstance(array, dict) else None
    if not isinstance(items, list):
        return None
    kept: list[Json] = []
    for item in items[:MAX_ARRAY_ITEMS]:
        inner = _scalar(item)
        if inner is None:
            return None
        kept.append(inner)
    return {"arrayValue": {"values": kept}}


def _attributes(raw: object, limit: int) -> tuple[list[Json], int]:
    """At most ``limit`` attributes, scrubbed, and how many were dropped for the cap or
    for a value no page sends. A scrubbed key is removed without being counted."""
    kept: list[Json] = []
    dropped = 0
    for item in raw if isinstance(raw, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str):
            dropped += 1
            continue
        if key in DROPPED_ATTRIBUTES:
            continue
        value = _value(item.get("value"))
        if value is None or len(kept) >= limit:
            dropped += 1
            continue
        if key in CUT_AT_QUERY and "stringValue" in value:
            value = {"stringValue": value["stringValue"].split("?", 1)[0]}
        kept.append({"key": key[:MAX_STRING_CHARS], "value": value})
    return kept, dropped


def _scrub_exception(raw: object) -> list[Any]:
    """An ``exception`` event's attributes without the message, and with only the frame
    lines of the stack (spec §6)."""
    scrubbed: list[Any] = []
    for item in raw if isinstance(raw, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if key == "exception.message":
            continue
        if key == "exception.stacktrace":
            value = item.get("value")
            stack = value.get("stringValue") if isinstance(value, dict) else None
            if not isinstance(stack, str):
                continue
            item = {"key": key, "value": {"stringValue": browser_frames_only(stack)}}
        scrubbed.append(item)
    return scrubbed


def _name(value: object) -> str:
    return value[:MAX_NAME_CHARS] if isinstance(value, str) else ""


def _event(raw: Json) -> Json:
    name = _name(raw.get("name"))
    attributes = raw.get("attributes")
    if name == "exception":
        attributes = _scrub_exception(attributes)
    kept, dropped = _attributes(attributes, MAX_EVENT_ATTRIBUTES)
    event: Json = {"name": name, "attributes": kept}
    if "timeUnixNano" in raw:
        event["timeUnixNano"] = raw["timeUnixNano"]
    event["droppedAttributesCount"] = _count(raw.get("droppedAttributesCount")) + dropped
    return event


def _link(raw: Json) -> Json:
    kept, dropped = _attributes(raw.get("attributes"), MAX_LINK_ATTRIBUTES)
    link: Json = {field: raw[field] for field in _LINK_FIELDS if field in raw}
    link["attributes"] = kept
    link["droppedAttributesCount"] = _count(raw.get("droppedAttributesCount")) + dropped
    return link


def _capped(raw: object, limit: int) -> tuple[list[Json], int]:
    """The first ``limit`` objects of a list, and how many entries were left out."""
    items = raw if isinstance(raw, list) else []
    kept = [item for item in items if isinstance(item, dict)][:limit]
    return kept, len(items) - len(kept)


def _exception_type(events: list[Json]) -> str | None:
    for event in events:
        if event["name"] != "exception":
            continue
        for attribute in event["attributes"]:
            if attribute["key"] == "exception.type" and "stringValue" in attribute["value"]:
                return str(attribute["value"]["stringValue"])
    return None


def _status(raw: object, events: list[Json]) -> Json:
    """The status code as sent; a description, which carries the message, becomes the
    exception's type or ``error``, as `core/trace_scrub.py` does."""
    if not isinstance(raw, dict):
        return {}
    status: Json = {}
    code = raw.get("code")
    if isinstance(code, int | str) and not isinstance(code, bool):
        status["code"] = code
    message = raw.get("message")
    if isinstance(message, str) and message:
        status["message"] = _exception_type(events) or "error"
    return status


def _span(raw: Json) -> Json:
    span: Json = {field: raw[field] for field in _SPAN_FIELDS if field in raw}
    span["name"] = _name(raw.get("name"))
    attributes, dropped = _attributes(raw.get("attributes"), MAX_ATTRIBUTES)
    span["attributes"] = attributes
    span["droppedAttributesCount"] = _count(raw.get("droppedAttributesCount")) + dropped
    events, dropped_events = _capped(raw.get("events"), MAX_EVENTS)
    span["events"] = [_event(event) for event in events]
    span["droppedEventsCount"] = _count(raw.get("droppedEventsCount")) + dropped_events
    links, dropped_links = _capped(raw.get("links"), MAX_LINKS)
    span["links"] = [_link(link) for link in links]
    span["droppedLinksCount"] = _count(raw.get("droppedLinksCount")) + dropped_links
    span["status"] = _status(raw.get("status"), span["events"])
    return span


def _resource(raw: object) -> Json:
    kept: list[Json] = [{"key": "service.name", "value": {"stringValue": WEB_SERVICE_NAME}}]
    seen: set[str] = set()
    attributes = raw.get("attributes") if isinstance(raw, dict) else None
    for item in attributes if isinstance(attributes, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if key not in KEPT_RESOURCE_ATTRIBUTES or key in seen:
            continue
        value = _scalar(item.get("value"))
        if value is not None and "stringValue" in value:
            seen.add(key)
            kept.append({"key": key, "value": value})
    return {"attributes": kept}


def _scope(raw: object) -> Json:
    if not isinstance(raw, dict):
        return {}
    return {
        field: raw[field][:MAX_NAME_CHARS]
        for field in ("name", "version")
        if isinstance(raw.get(field), str)
    }


def rewrite(payload: Json) -> Json:
    """The export rebuilt from what `parse` accepted: only the fields listed here survive."""
    return {
        "resourceSpans": [
            {
                "resource": _resource(resource_spans.get("resource")),
                "scopeSpans": [
                    {
                        "scope": _scope(scope_spans.get("scope")),
                        "spans": [_span(span) for span in scope_spans.get("spans", [])],
                    }
                    for scope_spans in resource_spans.get("scopeSpans", [])
                ],
            }
            for resource_spans in payload["resourceSpans"]
        ]
    }


def prepare(body: bytes) -> bytes:
    """The body as the relay forwards it. ASCII JSON, so a lone surrogate the page
    escaped stays an escape rather than failing to encode."""
    return json.dumps(rewrite(parse(body)), separators=(",", ":")).encode("ascii")


__all__ = [
    "KEPT_RESOURCE_ATTRIBUTES",
    "MAX_ARRAY_ITEMS",
    "MAX_ATTRIBUTES",
    "MAX_EVENTS",
    "MAX_EVENT_ATTRIBUTES",
    "MAX_LINKS",
    "MAX_LINK_ATTRIBUTES",
    "MAX_NAME_CHARS",
    "MAX_SPANS",
    "MAX_STRING_CHARS",
    "WEB_SERVICE_NAME",
    "PayloadError",
    "TooManySpansError",
    "browser_frames_only",
    "parse",
    "prepare",
    "rewrite",
]
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_payload.py tests/test_trace_scrub.py -v`
Expected: PASS.

- [ ] **Step 6: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/core/trace_scrub.py backend/scadbuddy/telemetry/__init__.py \
  backend/scadbuddy/telemetry/payload.py backend/tests/support/otlp.py \
  backend/tests/test_trace_relay_payload.py
git commit -m "feat(tracing): the relay rebuilds, caps and scrubs a page's spans (#988)"
```

---

### Task 3: Forward in the background, drain on shutdown, count every outcome

**Files:**
- Modify: `backend/scadbuddy/core/metrics.py` (`TraceRelayOutcome` after `EventDropReason`; the counter before `self.http_requests`; its pre-creation after the `EventDropReason` loop; `__all__`)
- Create: `backend/scadbuddy/telemetry/forwarder.py`
- Test: `backend/tests/test_trace_relay_forwarding.py`

**Interfaces:**
- Consumes: `scadbuddy.core.tracing.tracing_disabled() -> bool` (row 1).
- Produces (Task 5 uses them):
  - `core/metrics.py`: `TraceRelayOutcome = Literal["forwarded", "failed", "queue_full", "shutdown"]`; `Metrics.trace_relay_batches: Counter` named `scadbuddy_trace_relay_batches_total`, label `outcome`
  - `def relay_endpoint() -> str | None`
  - `class TraceForwarder(*, metrics: Metrics, endpoint: str | None, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.monotonic, forward_timeout: float = 5.0, drain_seconds: float = 5.0)` with `off: bool` (property), `closing: bool` (attribute), `metrics: Metrics`, `def offer(self, batch: bytes) -> None`, `def running(self) -> AbstractAsyncContextManager[None]`
  - `MAX_QUEUED_BATCHES: Final = 64`, `MAX_QUEUED_BYTES: Final = 16 * 1024 * 1024` (module globals, read at call time; a Task 5 test monkeypatches the first)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_trace_relay_forwarding.py
"""telemetry/forwarder.py: the relay's queue and its posts (spec 2026-10-01 §5.2)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine

import httpx
import pytest

from scadbuddy.core.metrics import Metrics
from scadbuddy.telemetry import forwarder as forwarder_module
from scadbuddy.telemetry.forwarder import (
    MAX_QUEUED_BATCHES,
    MAX_QUEUED_BYTES,
    TraceForwarder,
    relay_endpoint,
)

ENDPOINT = "http://collector.test:4318"
BATCH = b'{"resourceSpans":[]}'

type Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def outcome(metrics: Metrics, name: str) -> float:
    value = metrics.registry.get_sample_value(
        "scadbuddy_trace_relay_batches_total", {"outcome": name}
    )
    assert value is not None
    return value


def make(
    handler: Handler,
    *,
    endpoint: str | None = ENDPOINT,
    forward_timeout: float = 5.0,
    drain_seconds: float = 5.0,
) -> tuple[TraceForwarder, Metrics]:
    metrics = Metrics()
    forwarder = TraceForwarder(
        metrics=metrics,
        endpoint=endpoint,
        transport=httpx.MockTransport(handler),
        forward_timeout=forward_timeout,
        drain_seconds=drain_seconds,
    )
    return forwarder, metrics


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


async def test_an_accepted_batch_is_posted_to_the_traces_path() -> None:
    seen: list[httpx.Request] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    forwarder, metrics = make(collector, endpoint=ENDPOINT + "/")
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "forwarded") == 1)
    (request,) = seen
    assert str(request.url) == "http://collector.test:4318/v1/traces"
    assert request.headers["content-type"] == "application/json"
    assert request.content == BATCH
    # Never instrumented: the relay adds no trace context of its own.
    assert "traceparent" not in request.headers


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_a_refused_post_drops_its_batch_and_counts_it(status: int) -> None:
    calls = 0

    async def collector(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    forwarder, metrics = make(collector)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 1)
    assert calls == 1  # no retry
    assert outcome(metrics, "forwarded") == 0


async def test_an_unreachable_collector_counts_failed() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    forwarder, metrics = make(collector)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 1)


async def test_a_collector_that_never_answers_times_out() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    forwarder, metrics = make(collector, forward_timeout=0.05)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 1)


async def test_failures_warn_once_a_minute(caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]

    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    metrics = Metrics()
    forwarder = TraceForwarder(
        metrics=metrics,
        endpoint=ENDPOINT,
        transport=httpx.MockTransport(collector),
        clock=lambda: now[0],
    )
    caplog.set_level(logging.WARNING, logger=forwarder_module.__name__)
    async with forwarder.running():
        forwarder.offer(BATCH)
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 2)
        now[0] += 61
        forwarder.offer(BATCH)
        await until(lambda: outcome(metrics, "failed") == 3)
    warnings = [r for r in caplog.records if r.name == forwarder_module.__name__]
    assert len(warnings) == 2
    assert all(getattr(r, "reason", None) == "http-502" for r in warnings)


async def test_a_full_queue_drops_the_new_batch() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    # Not running: nothing is posted, so the queue only fills.
    for _ in range(MAX_QUEUED_BATCHES):
        forwarder.offer(BATCH)
    forwarder.offer(BATCH)
    assert outcome(metrics, "queue_full") == 1


async def test_the_queue_is_bounded_in_bytes_too() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    forwarder.offer(b"x" * MAX_QUEUED_BYTES)
    forwarder.offer(b"x")
    assert outcome(metrics, "queue_full") == 1


async def test_shutdown_drains_the_queue_and_refuses_new_batches() -> None:
    seen: list[bytes] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200)

    forwarder, metrics = make(collector)
    for index in range(3):
        forwarder.offer(b"%d" % index)
    # The block awaits nothing, so the forwarding task never ran: the exit drains all three.
    async with forwarder.running():
        assert not forwarder.closing
    assert forwarder.closing
    assert seen == [b"0", b"1", b"2"]
    assert outcome(metrics, "forwarded") == 3
    assert outcome(metrics, "shutdown") == 0


async def test_a_dead_collector_ends_the_drain_at_its_first_failure() -> None:
    calls = 0

    async def collector(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    forwarder, metrics = make(collector)
    for _ in range(5):
        forwarder.offer(BATCH)
    started = time.monotonic()
    async with forwarder.running():
        pass
    assert time.monotonic() - started < 1
    assert calls == 1
    assert outcome(metrics, "failed") == 1
    assert outcome(metrics, "shutdown") == 4


async def test_a_hung_collector_ends_the_drain_within_the_budget() -> None:
    async def collector(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    forwarder, metrics = make(collector, drain_seconds=0.2)
    for _ in range(3):
        forwarder.offer(BATCH)
    started = time.monotonic()
    async with forwarder.running():
        pass
    assert time.monotonic() - started < 1
    assert outcome(metrics, "failed") == 1
    assert outcome(metrics, "shutdown") == 2


async def test_a_post_cut_off_by_shutdown_counts_as_shutdown() -> None:
    entered = asyncio.Event()

    async def collector(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(60)
        return httpx.Response(200)

    forwarder, metrics = make(collector, drain_seconds=0.1)
    async with forwarder.running():
        forwarder.offer(BATCH)
        await entered.wait()
    assert outcome(metrics, "shutdown") == 1
    assert outcome(metrics, "failed") == 0


async def test_off_starts_nothing_and_posts_nothing() -> None:
    calls = 0

    async def collector(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    forwarder, _ = make(collector, endpoint=None)
    assert forwarder.off
    async with forwarder.running():
        pass
    assert calls == 0


def test_the_endpoint_follows_the_backend_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert relay_endpoint() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert relay_endpoint() == ENDPOINT
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert relay_endpoint() is None
```

The drain tests rely on one property of `running()`: the forwarding task is created
but not started before the `async with` body runs, so a body that awaits nothing exits
with every pre-queued batch still queued, and the drain alone handles them.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_forwarding.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'scadbuddy.telemetry.forwarder'`.

- [ ] **Step 3: Add the counter**

In `backend/scadbuddy/core/metrics.py`, after the `EventDropReason = Literal[...]` line, add:

```python
#: What became of a browser trace batch the relay accepted (spec 2026-10-01 §5.2): posted
#: to the collector, refused or unreachable there, dropped because the queue was full,
#: or still queued when the process stopped.
TraceRelayOutcome = Literal["forwarded", "failed", "queue_full", "shutdown"]
```

In `Metrics.__init__`, directly before `        self.http_requests = Counter(`, add:

```python
        # The browser trace relay (spec 2026-10-01 §5.2): it watches the tracing path
        # itself, and is the one metric the tracing design adds.
        self.trace_relay_batches = Counter(
            "scadbuddy_trace_relay_batches_total",
            "Browser trace batches the relay accepted, by what became of them.",
            ["outcome"],
            registry=r,
        )

```

After the existing

```python
        for reason in get_args(EventDropReason):
            self.events_dropped.labels(reason)
```

add:

```python
        for outcome in get_args(TraceRelayOutcome):
            self.trace_relay_batches.labels(outcome)
```

Replace `__all__` with:

```python
__all__ = [
    "CONTENT_TYPE_LATEST",
    "HttpMetrics",
    "Metrics",
    "RenderOutcome",
    "RenderStage",
    "TraceRelayOutcome",
]
```

- [ ] **Step 4: Write `forwarder.py`**

```python
# backend/scadbuddy/telemetry/forwarder.py
"""The browser trace relay's forwarding to the collector (spec 2026-10-01 §5.2).

An accepted batch goes on a bounded in-memory queue and the browser is answered at
once; one task posts the queue to the collector, so the browser never waits on it.

- **No retries.** A failed post (unreachable, a timeout, any non-2xx) drops its batch.
  Retrying and buffering are alloy's job, and the browser has already gone.
- **A full queue** drops the new batch at once; the browser still gets its 204.
- **Shutdown** (`running` exiting, in the app's lifespan): new batches are refused
  (`closing`, a 503), then the queue is drained for at most :data:`DRAIN_SECONDS` in
  all. Each post's timeout is whatever remains of that budget, the first post that
  fails ends the drain (an unreachable collector would fail every later post the same
  way), and the rest is dropped as ``shutdown``.
- **Visibility:** every outcome is counted in ``scadbuddy_trace_relay_batches_total``,
  and failures log one warning a minute at most, naming the status or the error class.

The httpx client here is never passed to ``HTTPXClientInstrumentor.instrument_client``
(spec §4, §6): traced, the relay would trace its own exports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Final

import httpx

from scadbuddy.core.metrics import Metrics, TraceRelayOutcome
from scadbuddy.core.tracing import tracing_disabled

logger = logging.getLogger(__name__)

#: 64 batches of at most 256 KiB each (the route's body limit).
MAX_QUEUED_BATCHES: Final = 64
MAX_QUEUED_BYTES: Final = 16 * 1024 * 1024
FORWARD_TIMEOUT: Final = 5.0
DRAIN_SECONDS: Final = 5.0
WARNING_INTERVAL: Final = 60.0
#: The OTLP/HTTP signal path under ``OTEL_EXPORTER_OTLP_ENDPOINT``, as the SDK's exporter
#: appends it.
TRACES_PATH: Final = "/v1/traces"


def relay_endpoint() -> str | None:
    """The collector the relay forwards to, or None when tracing is off: no
    ``OTEL_EXPORTER_OTLP_ENDPOINT``, or ``OTEL_SDK_DISABLED=true`` (spec §3). The same
    rule `core/tracing.py` ``build_provider`` applies to the backend's own spans."""
    if tracing_disabled():
        return None
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None


class TraceForwarder:
    def __init__(
        self,
        *,
        metrics: Metrics,
        endpoint: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        forward_timeout: float = FORWARD_TIMEOUT,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self.metrics = metrics
        self.endpoint = endpoint
        self._transport = transport
        self._clock = clock
        self._forward_timeout = forward_timeout
        self._drain_seconds = drain_seconds
        #: Set once shutdown has begun: the route answers 503 from then on.
        self.closing = False
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        #: Set when a batch is queued; made per run, so it is bound to that run's loop.
        self._wake: asyncio.Event | None = None
        self._client: httpx.AsyncClient | None = None
        self._warned_at: float | None = None

    @property
    def off(self) -> bool:
        return self.endpoint is None

    @property
    def url(self) -> str:
        if self.endpoint is None:
            raise RuntimeError("tracing is off: there is no collector to forward to")
        return self.endpoint.rstrip("/") + TRACES_PATH

    def offer(self, batch: bytes) -> None:
        """Queue ``batch`` for the collector. When the queue is full it is dropped and
        counted; the caller answers the browser 204 either way."""
        if (
            len(self._pending) >= MAX_QUEUED_BATCHES
            or self._pending_bytes + len(batch) > MAX_QUEUED_BYTES
        ):
            self._count("queue_full")
            return
        self._pending.append(batch)
        self._pending_bytes += len(batch)
        if self._wake is not None:
            self._wake.set()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """The forwarding task, for as long as the app runs (`Component.run`)."""
        if self.off:
            yield
            return
        self.closing = False
        self._wake = asyncio.Event()
        self._client = httpx.AsyncClient(transport=self._transport)
        task = asyncio.create_task(self._forward(self._wake))
        try:
            yield
        finally:
            self.closing = True
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            try:
                await self._drain()
            finally:
                await self._client.aclose()
                self._client = None
                self._wake = None

    def _pop(self) -> bytes:
        batch = self._pending.popleft()
        self._pending_bytes -= len(batch)
        return batch

    async def _forward(self, wake: asyncio.Event) -> None:
        while True:
            # What was queued before the run started goes first.
            while self._pending:
                await self._post(self._pop(), self._forward_timeout)
            wake.clear()
            await wake.wait()

    async def _drain(self) -> None:
        deadline = self._clock() + self._drain_seconds
        while self._pending:
            remaining = deadline - self._clock()
            if remaining <= 0 or not await self._post(self._pop(), remaining):
                break
        while self._pending:
            self._pop()
            self._count("shutdown")

    async def _post(self, batch: bytes, timeout: float) -> bool:
        """One post, bounded by ``timeout`` whatever the transport does; True when the
        collector took it. A post cancelled by shutdown counts as ``shutdown``."""
        client = self._client
        if client is None:
            raise RuntimeError("the forwarder is not running")
        try:
            async with asyncio.timeout(timeout):
                response = await client.post(
                    self.url, content=batch, headers={"Content-Type": "application/json"}
                )
        except asyncio.CancelledError:
            self._count("shutdown")
            raise
        except (TimeoutError, httpx.HTTPError, httpx.InvalidURL) as error:
            self._failed(type(error).__name__)
            return False
        if not response.is_success:
            self._failed(f"http-{response.status_code}")
            return False
        self._count("forwarded")
        return True

    def _count(self, outcome: TraceRelayOutcome) -> None:
        self.metrics.trace_relay_batches.labels(outcome).inc()

    def _failed(self, reason: str) -> None:
        self._count("failed")
        now = self._clock()
        if self._warned_at is None or now - self._warned_at >= WARNING_INTERVAL:
            self._warned_at = now
            logger.warning(
                "could not forward browser spans to the collector; dropped the batch",
                extra={"reason": reason},
            )


__all__ = [
    "DRAIN_SECONDS",
    "FORWARD_TIMEOUT",
    "MAX_QUEUED_BATCHES",
    "MAX_QUEUED_BYTES",
    "TraceForwarder",
    "relay_endpoint",
]
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_forwarding.py -v`
Expected: PASS (15 tests, about a second in all).

- [ ] **Step 6: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/core/metrics.py backend/scadbuddy/telemetry/forwarder.py \
  backend/tests/test_trace_relay_forwarding.py
git commit -m "feat(tracing): the relay forwards in the background and counts every batch (#988)"
```

---

### Task 4: Same origin only, JSON only, and the two rate limits

**Files:**
- Modify: `backend/scadbuddy/api/realtime.py:218-225` (`RateLimit`: `_refill`, `retry_after`)
- Create: `backend/scadbuddy/telemetry/admission.py`
- Test: `backend/tests/test_trace_relay_admission.py`

**Interfaces:**
- Consumes: `scadbuddy.api.realtime.origin_allowed(origin, public_url, allowed_origins) -> bool`, `RateLimit(burst, per_second, clock)`; `scadbuddy.core.proxies.client_address` (Task 1); `Settings.public_url`, `Settings.allowed_origin_list`, `Settings.trusted_proxy_networks` (Task 1); `scadbuddy.core.problems.ApiError(status, detail, *, title=None, type_="about:blank", headers=None)`.
- Produces (Task 5 uses them):
  - `RateLimit.retry_after(self) -> float`
  - `def check_origin(headers: Headers, settings: Settings) -> None` (raises `ApiError` 403)
  - `def check_content_type(headers: Headers) -> None` (raises `ApiError` 415)
  - `def relay_client(headers: Headers, peer: str | None, settings: Settings) -> str`
  - `class RelayLimits(*, clock=time.monotonic, process_burst=100, process_per_second=20.0, client_burst=20, client_per_second=2.0, max_clients=4096)` with `def take(self, client: str) -> None` (raises `ApiError` 429 with `Retry-After`) and `tracked_clients: int` (property)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_trace_relay_admission.py
"""telemetry/admission.py: who may post to the relay, and how often (spec 2026-10-01 §5.2)."""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from scadbuddy.api.realtime import RateLimit
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.telemetry.admission import (
    RelayLimits,
    check_content_type,
    check_origin,
    relay_client,
)
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

PUBLIC = "https://scadbuddy.example"


def settings(trusted_proxies: str = "") -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        public_url=PUBLIC,
        allowed_origins="https://scadbuddy.lan",
        trusted_proxies=trusted_proxies,
    )


def headers(*pairs: tuple[str, str]) -> Headers:
    return Headers(raw=[(name.encode(), value.encode()) for name, value in pairs])


def refusal(error: pytest.ExceptionInfo[ApiError]) -> tuple[int, str]:
    return error.value.status, error.value.detail


@pytest.mark.parametrize(
    "origin",
    [PUBLIC, "https://SCADBUDDY.example:443", "https://scadbuddy.lan", "http://localhost:5173"],
)
def test_the_uis_own_origins_are_accepted(origin: str) -> None:
    check_origin(headers(("origin", origin)), settings())
    check_origin(headers(("origin", origin), ("sec-fetch-site", "same-origin")), settings())


def test_no_origin_is_refused() -> None:
    with pytest.raises(ApiError) as error:
        check_origin(headers(), settings())
    assert refusal(error) == (
        403,
        "the relay accepts requests from ScadBuddy's own pages, which send Origin",
    )


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "file://", "https://scadbuddy.example.evil"]
)
def test_a_foreign_origin_is_refused_by_rule(origin: str) -> None:
    with pytest.raises(ApiError) as error:
        check_origin(headers(("origin", origin)), settings())
    assert refusal(error) == (403, "Origin not allowed")
    assert origin not in error.value.detail


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_a_sec_fetch_site_other_than_same_origin_is_refused(site: str) -> None:
    with pytest.raises(ApiError) as error:
        check_origin(headers(("origin", PUBLIC), ("sec-fetch-site", site)), settings())
    assert refusal(error) == (403, "Sec-Fetch-Site must be same-origin")


@pytest.mark.parametrize(
    "kind", ["application/json", "application/json; charset=utf-8", "Application/JSON"]
)
def test_json_is_accepted(kind: str) -> None:
    check_content_type(headers(("content-type", kind)))


@pytest.mark.parametrize("kind", ["text/plain", "application/x-protobuf", ""])
def test_anything_else_is_415(kind: str) -> None:
    with pytest.raises(ApiError) as error:
        check_content_type(headers(("content-type", kind)))
    assert refusal(error) == (415, "the relay accepts application/json only")


def test_retry_after_is_the_wait_for_one_more_token() -> None:
    now = [0.0]
    limit = RateLimit(1, 0.5, lambda: now[0])
    assert limit.retry_after() == 0
    assert limit.take()
    assert limit.retry_after() == pytest.approx(2.0)
    now[0] = 1.5
    assert limit.retry_after() == pytest.approx(0.5)
    now[0] = 2.0
    assert limit.take()


def test_the_per_client_bucket_refuses_with_retry_after() -> None:
    limits = RelayLimits(clock=lambda: 0.0, client_burst=1, client_per_second=0.25)
    limits.take("203.0.113.9")
    with pytest.raises(ApiError) as error:
        limits.take("203.0.113.9")
    assert refusal(error) == (429, "this client is over the relay's rate limit")
    assert error.value.headers["Retry-After"] == "4"
    # Another client has a bucket of its own.
    limits.take("203.0.113.10")


def test_the_per_process_bucket_caps_every_client_together() -> None:
    limits = RelayLimits(clock=lambda: 0.0, process_burst=2, process_per_second=0.5)
    limits.take("a")
    limits.take("b")
    with pytest.raises(ApiError) as error:
        limits.take("c")
    assert refusal(error) == (429, "the relay is over its overall rate limit")
    assert error.value.headers["Retry-After"] == "2"


def test_the_client_buckets_are_bounded() -> None:
    limits = RelayLimits(clock=lambda: 0.0, max_clients=3, process_burst=1000)
    for index in range(10):
        limits.take(f"10.0.0.{index}")
    assert limits.tracked_clients == 3


def test_an_untrusted_peer_is_its_own_client() -> None:
    forwarded = headers(("x-forwarded-for", "203.0.113.9"))
    assert relay_client(forwarded, "10.43.0.5", settings("10.42.0.0/16")) == "10.43.0.5"
    assert relay_client(forwarded, "10.42.0.5", settings()) == "10.42.0.5"


def test_a_trusted_proxy_names_the_client_by_its_last_value() -> None:
    trusted = settings("10.42.0.0/16")
    forwarded = headers(("x-forwarded-for", "198.51.100.1, 203.0.113.9"))
    assert relay_client(forwarded, "10.42.0.5", trusted) == "203.0.113.9"
    # Two header lines read as one list, as Node joins them for the agent.
    split = headers(("x-forwarded-for", "198.51.100.1"), ("x-forwarded-for", "203.0.113.9"))
    assert relay_client(split, "10.42.0.5", trusted) == "203.0.113.9"


def test_an_empty_last_value_falls_back_to_the_peer() -> None:
    forwarded = headers(("x-forwarded-for", "203.0.113.9, "))
    assert relay_client(forwarded, "10.42.0.5", settings("10.42.0.0/16")) == "10.42.0.5"
    assert relay_client(headers(), None, settings()) == "unknown"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_admission.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'scadbuddy.telemetry.admission'`.

- [ ] **Step 3: Give `RateLimit` a `retry_after`**

In `backend/scadbuddy/api/realtime.py`, replace `RateLimit.take` with:

```python
    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.per_second)
        self._at = now

    def take(self) -> bool:
        self._refill()
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def retry_after(self) -> float:
        """Seconds until one more frame is allowed; 0 when one is now (a ``Retry-After``)."""
        self._refill()
        return max(0.0, (1 - self._tokens) / self.per_second)
```

`take` behaves exactly as before; the socket's own limiter (`_read`) is unchanged.

- [ ] **Step 4: Write `admission.py`**

```python
# backend/scadbuddy/telemetry/admission.py
"""Who may post browser spans to the relay, and how often (spec 2026-10-01 §5.2).

**Same origin only.** A page on another origin must not drive the relay through a LAN
user's browser, to spend its budget or inject spans. In order, before the body is read:

1. no ``Origin`` is refused: a browser always sends one on a ``fetch`` POST, so its
   absence means the caller is not this page (`origin_allowed` answers True for None
   on purpose, for the realtime socket, so this is a check of its own);
2. `origin_allowed` (`api/realtime.py`: the public URL, ``SCADBUDDY_ALLOWED_ORIGINS``
   and loopback) must accept the ``Origin`` that is present;
3. a ``Sec-Fetch-Site`` that is present must be ``same-origin``.

The ``Origin`` check is the one that matters: a cross-origin page can skip the
preflight (``mode: 'no-cors'`` with a ``text/plain`` body), and that request still
carries its foreign ``Origin``. The route sends no CORS headers and answers no
preflight, but that is not relied on to stop a request. These are the checks #962
calls for every write; when it lands, the relay uses its shared guard instead.

**Rate limits**, in memory: a per-process bucket caps what one pod sends the collector
whatever the client, and a per-client bucket sits under it. The client is the peer,
unless the peer is in ``SCADBUDDY_TRUSTED_PROXIES`` (`core/proxies.py`).
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from starlette.datastructures import Headers

from scadbuddy.api.realtime import RateLimit, origin_allowed
from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address
from scadbuddy.core.settings import Settings

#: Per process, so per pod rather than cluster-wide: with N API replicas the ceiling is
#: N times this. The API runs one replica (2026-10-01, ``replicas: 1`` in
#: eh-homelab/clusters ``applications/scadbuddy/scadbuddy.yaml``); a change there has to
#: adjust these. A bucket shared in Postgres would cost a write per batch on a path whose
#: only job is to be cheap.
PROCESS_BURST: Final = 100
PROCESS_PER_SECOND: Final = 20.0
#: A page's exporter sends about one batch every 5 s (frontend `RelayExporter`).
CLIENT_BURST: Final = 20
CLIENT_PER_SECOND: Final = 2.0
#: Clients with a bucket of their own; the least recently seen is forgotten past it.
MAX_TRACKED_CLIENTS: Final = 4096
#: The bucket of a request whose peer is unknown.
UNKNOWN_CLIENT: Final = "unknown"


def _forbidden(detail: str) -> ApiError:
    return ApiError(403, detail, title="Forbidden")


def check_origin(headers: Headers, settings: Settings) -> None:
    """Refuse (403) a request that is not from one of ScadBuddy's own pages."""
    origin = headers.get("origin")
    if origin is None:
        raise _forbidden("the relay accepts requests from ScadBuddy's own pages, which send Origin")
    if not origin_allowed(origin, settings.public_url, settings.allowed_origin_list):
        raise _forbidden("Origin not allowed")
    site = headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        raise _forbidden("Sec-Fetch-Site must be same-origin")


def check_content_type(headers: Headers) -> None:
    """Refuse (415) anything but OTLP/JSON, the browser exporter's format."""
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise ApiError(415, "the relay accepts application/json only")


def relay_client(headers: Headers, peer: str | None, settings: Settings) -> str:
    """The client a rate limit counts against. Every ``X-Forwarded-For`` line, joined as
    Node joins them for the agent, so a proxy that sends two gives the same answer."""
    forwarded_for = ", ".join(headers.getlist("x-forwarded-for")) or None
    return client_address(peer, forwarded_for, settings.trusted_proxy_networks) or UNKNOWN_CLIENT


def _too_many(detail: str, limit: RateLimit) -> ApiError:
    seconds = max(1, math.ceil(limit.retry_after()))
    return ApiError(429, detail, title="Too Many Requests", headers={"Retry-After": str(seconds)})


class RelayLimits:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        process_burst: int = PROCESS_BURST,
        process_per_second: float = PROCESS_PER_SECOND,
        client_burst: int = CLIENT_BURST,
        client_per_second: float = CLIENT_PER_SECOND,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self._clock = clock
        self._process = RateLimit(process_burst, process_per_second, clock)
        self._client_burst = client_burst
        self._client_per_second = client_per_second
        self._max_clients = max_clients
        self._clients: OrderedDict[str, RateLimit] = OrderedDict()

    @property
    def tracked_clients(self) -> int:
        return len(self._clients)

    def _client(self, client: str) -> RateLimit:
        limit = self._clients.get(client)
        if limit is not None:
            self._clients.move_to_end(client)
            return limit
        limit = RateLimit(self._client_burst, self._client_per_second, self._clock)
        self._clients[client] = limit
        if len(self._clients) > self._max_clients:
            self._clients.popitem(last=False)
        return limit

    def take(self, client: str) -> None:
        """One batch from ``client``, or a 429 whose ``Retry-After`` is the seconds until
        the bucket that refused it holds one again."""
        limit = self._client(client)
        if not limit.take():
            raise _too_many("this client is over the relay's rate limit", limit)
        if not self._process.take():
            raise _too_many("the relay is over its overall rate limit", self._process)


__all__ = [
    "CLIENT_BURST",
    "CLIENT_PER_SECOND",
    "MAX_TRACKED_CLIENTS",
    "PROCESS_BURST",
    "PROCESS_PER_SECOND",
    "RelayLimits",
    "check_content_type",
    "check_origin",
    "relay_client",
]
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_admission.py -v`
Expected: PASS.

- [ ] **Step 6: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/api/realtime.py backend/scadbuddy/telemetry/admission.py \
  backend/tests/test_trace_relay_admission.py
git commit -m "feat(tracing): the relay admits same-origin JSON within its rate limits (#988)"
```

---

### Task 5: `POST /telemetry/v1/traces`, the component, and the app's wiring

**Files:**
- Create: `backend/scadbuddy/telemetry/component.py`
- Create: `backend/scadbuddy/api/telemetry.py`
- Modify: `backend/scadbuddy/main.py` (the `scadbuddy.api` import; `ROOT_ROUTE_MODULES`; the `BodySizeGate` routes; `app.include_router(telemetry.router)` after the metrics router)
- Modify: `README.md` ("### Tracing (#988)"), `CLAUDE.md` (the `core/tracing.py` bullet under "Layout")
- Test: `backend/tests/test_trace_relay_route.py`, `backend/tests/api/test_trace_relay_app.py`

**Interfaces:**
- Consumes: everything above: `prepare`, `PayloadError`, `TooManySpansError`, `MAX_SPANS` (Task 2); `TraceForwarder`, `relay_endpoint` (Task 3); `check_origin`, `check_content_type`, `relay_client`, `RelayLimits` (Task 4); `scadbuddy.api.limits.RouteLimit(method, path, limit, what, source)`, `BodySizeGate`, `BODY_LIMITS`; `scadbuddy.api.components.component_dep`, `getter_for`; `scadbuddy.core.components.Component`, `Components`, `Core`, `Key`.
- Produces:
  - `scadbuddy/telemetry/component.py`: `@dataclass(frozen=True) class TraceRelay(forwarder: TraceForwarder, limits: RelayLimits, settings: Callable[[], Settings])`, `TRACE_RELAY: Key[TraceRelay] = Key("trace_relay")`, `COMPONENT`, `TraceRelayDep`
  - `scadbuddy/api/telemetry.py`: `router`, `RELAY_PATH = "/telemetry/v1/traces"`, `TRACING_HEADER = "X-ScadBuddy-Tracing"`, `MAX_BODY_BYTES = 256 * 1024`, `RELAY_ROUTE_LIMIT: RouteLimit`
  - Row 4 relies on: `204` (accepted, or `X-ScadBuddy-Tracing: off`), `400`, `403`, `405`, `413`, `415`, `429` with `Retry-After`, `503`, all problem documents.

- [ ] **Step 1: Write the failing route tests (no database needed)**

```python
# backend/tests/test_trace_relay_route.py
"""``POST /telemetry/v1/traces`` over HTTP (spec 2026-10-01 §5.2, §8).

The route in a small app composed as ``main.py`` composes it (the problem handlers, the
body gate with the relay's limit, the SPA's mount last), with the relay's component
overridden: no database or Temporal needed. ``tests/api/test_trace_relay_app.py``
checks the same wiring in the real app."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import telemetry
from scadbuddy.api.components import getter_for
from scadbuddy.api.limits import BODY_LIMITS, BodySizeGate
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.telemetry import forwarder as forwarder_module
from scadbuddy.telemetry.admission import RelayLimits
from scadbuddy.telemetry.component import TRACE_RELAY, TraceRelay
from scadbuddy.telemetry.forwarder import TraceForwarder
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.support.otlp import SENTINEL, export, span, string

PATH = "/telemetry/v1/traces"
ORIGIN = "https://scadbuddy.example"
UI = {"Origin": ORIGIN, "Content-Type": "application/json"}

type Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


class Collector:
    def __init__(self, status: int = 200, *, hang: bool = False) -> None:
        self.status = status
        self.hang = hang
        self.bodies: list[bytes] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        if self.hang:
            await asyncio.sleep(60)
        return httpx.Response(self.status)


def relay_settings(trusted_proxies: str = "") -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        public_url=ORIGIN,
        trusted_proxies=trusted_proxies,
    )


def make_relay(
    collector: Handler | None = None,
    *,
    endpoint: str | None = "http://collector.test:4318",
    limits: RelayLimits | None = None,
    trusted_proxies: str = "",
    drain_seconds: float = 5.0,
) -> TraceRelay:
    settings = relay_settings(trusted_proxies)
    return TraceRelay(
        forwarder=TraceForwarder(
            metrics=Metrics(),
            endpoint=endpoint,
            transport=httpx.MockTransport(collector or Collector()),
            drain_seconds=drain_seconds,
        ),
        limits=limits or RelayLimits(),
        settings=lambda: settings,
    )


def relay_app(relay: TraceRelay, frontend: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with relay.forwarder.running():
            yield

    app = FastAPI(lifespan=lifespan)
    install_problem_handlers(app)
    app.add_middleware(BodySizeGate, limits=BODY_LIMITS, routes=[telemetry.RELAY_ROUTE_LIMIT])
    app.include_router(telemetry.router)
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    app.dependency_overrides[getter_for(TRACE_RELAY)] = lambda: relay
    return app


@pytest.fixture
def frontend(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    return dist


def outcome(relay: TraceRelay, name: str) -> float:
    value = relay.forwarder.metrics.registry.get_sample_value(
        "scadbuddy_trace_relay_batches_total", {"outcome": name}
    )
    assert value is not None
    return value


def until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def assert_problem(response: httpx.Response, status: int, detail: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["status"] == status
    assert response.json()["detail"] == detail
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]


@pytest.fixture
def collector() -> Collector:
    return Collector()


@pytest.fixture
def relay(collector: Collector) -> TraceRelay:
    return make_relay(collector)


@pytest.fixture
def client(relay: TraceRelay) -> Iterator[TestClient]:
    with TestClient(relay_app(relay)) as test_client:
        yield test_client


def test_an_accepted_batch_is_answered_204_and_forwarded_rewritten(
    client: TestClient, relay: TraceRelay, collector: Collector
) -> None:
    body = export(span(), resource=[string("service.name", "scadbuddy-api")])
    response = client.post(PATH, content=body, headers=UI)
    assert response.status_code == 204
    assert "x-scadbuddy-tracing" not in response.headers
    until(lambda: outcome(relay, "forwarded") == 1)
    (sent,) = collector.bodies
    resource = json.loads(sent)["resourceSpans"][0]["resource"]
    assert resource["attributes"][0] == string("service.name", "scadbuddy-web")


def test_a_browser_exception_reaches_the_collector_without_its_message(
    client: TestClient, relay: TraceRelay, collector: Collector
) -> None:
    event = {
        "name": "exception",
        "attributes": [
            string("exception.type", "Error"),
            string("exception.message", SENTINEL),
            string("exception.stacktrace", f"Error: {SENTINEL}\n    at f (https://x/a.js:1:2)"),
        ],
    }
    body = export(span(events=[event], status={"code": 2, "message": SENTINEL}))
    assert client.post(PATH, content=body, headers=UI).status_code == 204
    until(lambda: outcome(relay, "forwarded") == 1)
    assert SENTINEL not in collector.bodies[0].decode()


def test_tracing_off_answers_off_and_forwards_nothing(collector: Collector) -> None:
    relay = make_relay(collector, endpoint=None)
    with TestClient(relay_app(relay)) as client:
        response = client.post(PATH, content=export(span()), headers=UI)
    assert response.status_code == 204
    assert response.headers["x-scadbuddy-tracing"] == "off"
    assert collector.bodies == []


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        (
            {"Content-Type": "application/json"},
            "the relay accepts requests from ScadBuddy's own pages, which send Origin",
        ),
        ({**UI, "Origin": "https://evil.example"}, "Origin not allowed"),
        ({**UI, "Sec-Fetch-Site": "cross-site"}, "Sec-Fetch-Site must be same-origin"),
    ],
    ids=["no-origin", "foreign-origin", "cross-site"],
)
def test_a_request_not_from_the_page_is_403_before_its_body_is_read(
    client: TestClient, collector: Collector, headers: dict[str, str], detail: str
) -> None:
    # Not JSON at all: a 403 rather than a 400 shows the body was never parsed.
    response = client.post(PATH, content=b"not json", headers=headers)
    assert_problem(response, 403, detail)
    assert collector.bodies == []


def test_a_preflight_is_not_answered(client: TestClient) -> None:
    response = client.options(
        PATH,
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert_problem(response, 405, "the relay accepts POST only")


def test_another_content_type_is_415(client: TestClient) -> None:
    response = client.post(
        PATH, content=export(span()), headers={**UI, "Content-Type": "text/plain"}
    )
    assert_problem(response, 415, "the relay accepts application/json only")


def test_a_body_over_256_kib_is_413_on_its_headers(client: TestClient) -> None:
    response = client.post(PATH, content=b" " * (256 * 1024 + 1), headers=UI)
    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["detail"].startswith("a trace batch is at most 0.25 MB")


def test_a_chunked_body_over_256_kib_is_413_as_it_streams(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b" " * (64 * 1024)

    response = client.post(PATH, content=chunks(), headers=UI)
    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE


def test_more_than_512_spans_is_413(client: TestClient) -> None:
    body = json.dumps(
        {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "s"}] * 513}]}]}
    ).encode()
    assert len(body) < 256 * 1024
    response = client.post(PATH, content=body, headers=UI)
    assert_problem(response, 413, "a trace batch holds at most 512 spans")


def test_a_body_that_is_not_an_export_is_400(client: TestClient) -> None:
    response = client.post(PATH, content=b"{", headers=UI)
    assert_problem(response, 400, "the body is not an OTLP/JSON trace export")


def test_the_per_client_limit_is_429_with_retry_after() -> None:
    relay = make_relay(limits=RelayLimits(client_burst=1, client_per_second=0.5))
    with TestClient(relay_app(relay)) as client:
        assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
        response = client.post(PATH, content=export(span()), headers=UI)
    assert_problem(response, 429, "this client is over the relay's rate limit")
    assert response.headers["retry-after"] in {"1", "2"}


def test_the_per_process_limit_is_429_for_every_client() -> None:
    relay = make_relay(limits=RelayLimits(process_burst=1, process_per_second=0.5))
    app = relay_app(relay)
    with TestClient(app, client=("192.0.2.1", 1000)) as first:
        assert first.post(PATH, content=export(span()), headers=UI).status_code == 204
    with TestClient(app, client=("192.0.2.2", 1000)) as second:
        response = second.post(PATH, content=export(span()), headers=UI)
    assert_problem(response, 429, "the relay is over its overall rate limit")
    assert "retry-after" in response.headers


def test_a_trusted_proxy_names_the_client_the_bucket_counts() -> None:
    relay = make_relay(
        limits=RelayLimits(client_burst=1, client_per_second=0.01),
        trusted_proxies="10.42.0.0/16",
    )
    with TestClient(relay_app(relay), client=("10.42.0.5", 1000)) as gateway:

        def post(*forwarded_for: str) -> int:
            headers = [*UI.items(), *(("X-Forwarded-For", v) for v in forwarded_for)]
            response: httpx.Response = gateway.post(PATH, content=export(span()), headers=headers)
            return response.status_code

        assert post("198.51.100.1, 203.0.113.9") == 204
        assert post("203.0.113.10") == 204
        # Two header lines are one list: its last value is the client already counted.
        assert post("198.51.100.7", "203.0.113.9") == 429
        # An empty last value is the gateway itself, a client of its own.
        assert post("203.0.113.9, ") == 204


def test_an_untrusted_peer_cannot_name_another_client() -> None:
    relay = make_relay(limits=RelayLimits(client_burst=1, client_per_second=0.01))
    with TestClient(relay_app(relay), client=("10.42.0.5", 1000)) as client:
        first = [*UI.items(), ("X-Forwarded-For", "203.0.113.9")]
        second = [*UI.items(), ("X-Forwarded-For", "203.0.113.10")]
        assert client.post(PATH, content=export(span()), headers=first).status_code == 204
        assert client.post(PATH, content=export(span()), headers=second).status_code == 429


def test_a_failing_collector_never_fails_the_browser() -> None:
    relay = make_relay(Collector(500))
    with TestClient(relay_app(relay)) as client:
        assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
        until(lambda: outcome(relay, "failed") == 1)


def test_a_full_queue_never_fails_the_browser(
    client: TestClient, relay: TraceRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forwarder_module, "MAX_QUEUED_BATCHES", 0)
    assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
    assert outcome(relay, "queue_full") == 1


def test_batches_still_queued_at_shutdown_are_counted_not_refused() -> None:
    relay = make_relay(Collector(hang=True), drain_seconds=0.1)
    with TestClient(relay_app(relay)) as client:
        for _ in range(3):
            assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
    assert outcome(relay, "shutdown") >= 1
    assert outcome(relay, "failed") + outcome(relay, "shutdown") == 3


def test_a_closing_relay_answers_503(client: TestClient, relay: TraceRelay) -> None:
    relay.forwarder.closing = True
    response = client.post(PATH, content=export(span()), headers=UI)
    assert_problem(response, 503, "the relay is shutting down")


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
def test_another_method_is_405_never_the_page(
    relay: TraceRelay, frontend: Path, method: str
) -> None:
    with TestClient(relay_app(relay, frontend)) as client:
        response = client.request(method, PATH, headers=UI)
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert "text/html" not in response.headers["content-type"]


@pytest.mark.parametrize(
    "path", ["/telemetry", "/telemetry/", "/telemetry/v1/traces/", "/telemetry/v1/metrics"]
)
def test_no_other_telemetry_path_serves_the_page(
    relay: TraceRelay, frontend: Path, path: str
) -> None:
    with TestClient(relay_app(relay, frontend)) as client:
        response = client.get(path)
    assert_problem(response, 404, "the only telemetry route is POST /telemetry/v1/traces")


def test_the_page_is_still_served_beside_it(relay: TraceRelay, frontend: Path) -> None:
    with TestClient(relay_app(relay, frontend)) as client:
        assert "text/html" in client.get("/models/demo").headers["content-type"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_route.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'telemetry' from 'scadbuddy.api'`.

- [ ] **Step 3: Write the component**

```python
# backend/scadbuddy/telemetry/component.py
"""The browser trace relay (spec 2026-10-01 §5.2) as a component (`core/components.py`).

Its forwarding task runs for as long as the app does (`Component.run`); on the way down
it stops accepting and drains, within the lifespan, inside the pod's grace period.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.settings import Settings
from scadbuddy.telemetry.admission import RelayLimits
from scadbuddy.telemetry.forwarder import TraceForwarder, relay_endpoint


@dataclass(frozen=True)
class TraceRelay:
    forwarder: TraceForwarder
    limits: RelayLimits
    #: The settings in effect: ``public_url`` is live (#322), applied to the app's
    #: ``settings`` on every change, so it is read per request rather than held.
    settings: Callable[[], Settings]


TRACE_RELAY: Key[TraceRelay] = Key("trace_relay")


def _build(core: Core, components: Components) -> TraceRelay:
    return TraceRelay(
        forwarder=TraceForwarder(metrics=core.metrics, endpoint=relay_endpoint()),
        limits=RelayLimits(),
        settings=lambda: core.settings,
    )


def _run(relay: TraceRelay) -> AbstractAsyncContextManager[None]:
    return relay.forwarder.running()


COMPONENT = Component(TRACE_RELAY, build=_build, run=_run)

TraceRelayDep = Annotated[TraceRelay, component_dep(TRACE_RELAY)]
```

- [ ] **Step 4: Write the route module**

```python
# backend/scadbuddy/api/telemetry.py
"""``POST /telemetry/v1/traces``: the browser's spans, relayed to the collector
(spec 2026-10-01 §5.2).

A transport, not a versioned operation: mounted at the root beside ``/healthz`` and
``/metrics`` (``main.ROOT_ROUTE_MODULES``) and left out of the OpenAPI schema, so
nothing ``_api_router()`` attaches applies to it, and it needs no agent tool and no
``coverage.ts`` entry. Same origin as the page, so it works inside Bambuddy's iframe.

Every refusal is an RFC 9457 problem (`core/problems.py`), and its ``detail`` names the
rule, never the request's own values. Every other method on the path, and every other
path under ``/telemetry``, is answered here too: registered before the SPA's static
mount is not enough, because a ``GET`` the ``POST`` route does not take would fall
through to the mount and be served ``index.html``.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, Request, Response

from scadbuddy.api.limits import RouteLimit
from scadbuddy.core.problems import ApiError
from scadbuddy.telemetry.admission import check_content_type, check_origin, relay_client
from scadbuddy.telemetry.component import TraceRelayDep
from scadbuddy.telemetry.payload import MAX_SPANS, PayloadError, TooManySpansError, prepare

RELAY_PATH: Final = "/telemetry/v1/traces"
#: On the 204 when tracing is off (no endpoint, or ``OTEL_SDK_DISABLED``): the page's
#: exporter stops for the rest of its life (spec §5.3).
TRACING_HEADER: Final = "X-ScadBuddy-Tracing"
#: The browser never comes near it (it sends at most 48 KiB a request, spec §5.3); it
#: bounds other callers. Without it the ``application/json`` default, 8 MiB, would apply.
MAX_BODY_BYTES: Final = 256 * 1024

#: For ``main.py``'s `BodySizeGate`: refused on the headers, or as the body streams.
RELAY_ROUTE_LIMIT: Final = RouteLimit(
    "POST",
    re.compile(rf"^{re.escape(RELAY_PATH)}$"),
    MAX_BODY_BYTES,
    "a trace batch",
    "fixed, not a setting",
)

_NOT_POST: Final = ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"]

router = APIRouter(tags=["telemetry"])


@router.post(RELAY_PATH, include_in_schema=False, status_code=204)
async def relay_traces(request: Request, relay: TraceRelayDep) -> Response:
    settings = relay.settings()
    check_origin(request.headers, settings)
    if relay.forwarder.off:
        return Response(status_code=204, headers={TRACING_HEADER: "off"})
    if relay.forwarder.closing:
        raise ApiError(503, "the relay is shutting down")
    check_content_type(request.headers)
    peer = request.client.host if request.client is not None else None
    relay.limits.take(relay_client(request.headers, peer, settings))
    try:
        batch = prepare(await request.body())
    except TooManySpansError as error:
        raise ApiError(413, f"a trace batch holds at most {MAX_SPANS} spans") from error
    except PayloadError as error:
        raise ApiError(400, "the body is not an OTLP/JSON trace export") from error
    relay.forwarder.offer(batch)
    return Response(status_code=204)


@router.api_route(RELAY_PATH, methods=_NOT_POST, include_in_schema=False)
async def relay_other_methods() -> Response:
    raise ApiError(405, "the relay accepts POST only", headers={"Allow": "POST"})


@router.api_route("/telemetry", methods=[*_NOT_POST, "POST"], include_in_schema=False)
@router.api_route("/telemetry/{rest:path}", methods=[*_NOT_POST, "POST"], include_in_schema=False)
async def no_such_telemetry_route() -> Response:
    raise ApiError(404, "the only telemetry route is POST /telemetry/v1/traces")
```

The module has a `router`, so `main._api_router()` would also mount it under
`/api/v1` unless it is in `ROOT_ROUTE_MODULES` (next step); `tests/api/test_routes.py`
and `tests/api/test_openapi.py` read the same set.

- [ ] **Step 5: Wire it into `main.py`**

In `backend/scadbuddy/main.py`:

Replace

```python
from scadbuddy.api import assets, health, libraries, media, metrics, models
```

with

```python
from scadbuddy.api import assets, health, libraries, media, metrics, models, telemetry
```

Replace

```python
ROOT_ROUTE_MODULES = frozenset({"health", "metrics"})
```

with

```python
ROOT_ROUTE_MODULES = frozenset({"health", "metrics", "telemetry"})
```

In `create_app`, in the `BodySizeGate` middleware's `routes=[...]`, replace

```python
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            )
        ],
```

with

```python
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            # The browser trace relay's 256 KiB (spec 2026-10-01 §5.2).
            telemetry.RELAY_ROUTE_LIMIT,
        ],
```

Replace

```python
    app.include_router(metrics.router)
```

with

```python
    app.include_router(metrics.router)
    # The browser trace relay: at the root like the two above, before the SPA's mount.
    app.include_router(telemetry.router)
```

`EXCLUDED_URLS` already lists `/telemetry/v1/traces` (row 1); do not change it. The
component needs no edit anywhere: `api/deps.py` `build_state` discovers
`scadbuddy/telemetry/component.py`, and `main.lifespan` enters its `run` with the others
(`state.components.running()`).

- [ ] **Step 6: Run the route tests**

Run: `cd backend && uv run --frozen pytest tests/test_trace_relay_route.py tests/test_components.py -v`
Expected: PASS (`test_every_discovered_key_is_unique` now sees `trace_relay` too).

- [ ] **Step 7: Write the real-app tests**

```python
# backend/tests/api/test_trace_relay_app.py
"""The browser trace relay as ``create_app`` mounts it (spec 2026-10-01 §5.2, §6).

The route's own behaviour is in ``tests/test_trace_relay_route.py``; this checks the
wiring only the real app has: the root mount, the body gate's route limit, the SPA
mount after it, the excluded server span, the component and its counter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.support.otlp import export, span

PATH = "/telemetry/v1/traces"
UI = {"Origin": "http://localhost:5173", "Content-Type": "application/json"}
#: Nothing listens on the discard port: a forwarded batch fails, which no test waits on.
UNREACHABLE_COLLECTOR = "http://127.0.0.1:9"


@contextmanager
def app_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> Iterator[TestClient]:
    """The real app, with a bundle so the SPA's fallback is mounted. The endpoint is read
    when the app is built; the tests' own span provider is kept either way."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    if endpoint is None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    else:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    with TestClient(create_app(settings.model_copy(update={"frontend_dir": dist}))) as client:
        yield client


@pytest.fixture
def relay_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with app_client(settings, tmp_path, monkeypatch, UNREACHABLE_COLLECTOR) as client:
        yield client


def test_without_a_collector_the_relay_answers_off(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_client(settings, tmp_path, monkeypatch, None) as client:
        response = client.post(PATH, content=export(span()), headers=UI)
    assert response.status_code == 204
    assert response.headers["x-scadbuddy-tracing"] == "off"


def test_with_a_collector_a_batch_is_accepted(relay_client: TestClient) -> None:
    response = relay_client.post(PATH, content=export(span()), headers=UI)
    assert response.status_code == 204
    assert "x-scadbuddy-tracing" not in response.headers


def test_the_body_gate_holds_it_to_256_kib(relay_client: TestClient) -> None:
    declared = relay_client.post(PATH, content=b" " * (256 * 1024 + 1), headers=UI)
    assert declared.status_code == 413
    assert declared.json()["detail"].startswith("a trace batch is at most 0.25 MB")

    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b" " * (64 * 1024)

    streamed = relay_client.post(PATH, content=chunks(), headers=UI)
    assert streamed.status_code == 413
    assert streamed.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("path", [PATH, "/telemetry/v1/traces/", "/telemetry"])
def test_the_page_is_never_served_for_it(relay_client: TestClient, path: str) -> None:
    response = relay_client.get(path)
    assert response.status_code in (404, 405)
    assert response.headers["content-type"] == "application/problem+json"


def test_it_is_not_traced(relay_client: TestClient, spans: InMemorySpanExporter) -> None:
    assert relay_client.post(PATH, content=export(span()), headers=UI).status_code == 204
    servers = [s for s in spans.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert servers == []


def test_its_counter_is_scraped_from_zero(relay_client: TestClient) -> None:
    text = relay_client.get("/metrics").text
    for outcome in ("forwarded", "failed", "queue_full", "shutdown"):
        assert f'scadbuddy_trace_relay_batches_total{{outcome="{outcome}"}} 0.0' in text


def test_it_is_not_in_the_openapi_schema(relay_client: TestClient) -> None:
    paths = relay_client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/telemetry")]
```

With tracing off the route answers before it reads the body, so a streamed oversized
body is only counted by the gate when the relay is on: that is why `relay_client`
points the relay at an unreachable collector.

- [ ] **Step 8: Run them with the app's API tests that read the same wiring**

Needs Postgres and Temporal (CLAUDE.md "Commands"), e.g.
`docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:17`
and a `temporal` CLI on `PATH`.

Run:

```bash
cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test \
  uv run --frozen pytest tests/api/test_trace_relay_app.py tests/api/test_routes.py \
  tests/api/test_openapi.py tests/api/test_tracing_http.py tests/api/test_metrics.py \
  tests/api/test_settings.py -v
```

Expected: PASS. Without `SCADBUDDY_TEST_DATABASE_URL` or a Temporal they skip; do not
report them as passing then.

- [ ] **Step 9: Document it**

In `README.md`, at the end of the "### Tracing (#988)" section (after the paragraph
that ends `Design: \`docs/superpowers/specs/2026-10-01-distributed-tracing-design.md\`.`),
add a blank line and:

```markdown
**Browser spans** reach the collector through the backend: the page posts OTLP/JSON to
`POST /telemetry/v1/traces` on ScadBuddy's own origin, and the relay
(`backend/scadbuddy/telemetry/`) forwards it in the background to
`$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces`. It accepts only the UI's own origins (the
public URL, `SCADBUDDY_ALLOWED_ORIGINS` and loopback, as the realtime socket does), at
most 256 KiB and 512 spans a batch, and rewrites every batch's resource to
`service.name=scadbuddy-web`. Without an endpoint, or with `OTEL_SDK_DISABLED=true`, it
answers `204` with `X-ScadBuddy-Tracing: off` and the page stops exporting. Its rate
limits are per pod (100 batches at once and 20 a second overall; 20 and 2 a second per
client), so with more than one API replica the overall ceiling multiplies.
**`SCADBUDDY_TRUSTED_PROXIES`** (comma-separated CIDRs, default empty) names the peers
whose `X-Forwarded-For` is believed, and then only its last value, as the agent's
`SCADBUDDY_AGENT_TRUSTED_PROXIES` does; set it to the gateway's range so each browser
gets a bucket of its own. Empty, every browser behind the gateway shares one.
`scadbuddy_trace_relay_batches_total{outcome}` counts `forwarded`, `failed`,
`queue_full` and `shutdown`; any rise in the last three means browser spans were lost.
```

In `CLAUDE.md`, in the `backend/scadbuddy/core/tracing.py` bullet under "Layout",
after `fixture \`spans\`); the Bambuddy client injects no trace headers.` add:

```markdown
  The browser relay (`POST /telemetry/v1/traces`, route `api/telemetry.py`, feature
  `scadbuddy/telemetry/`): `admission.py` (same-origin checks and the rate limits; the
  client by `core/proxies.py` and `SCADBUDDY_TRUSTED_PROXIES`, the agent's rules),
  `payload.py` (rebuilds, caps and scrubs the page's spans), `forwarder.py` (the queue
  and the one uninstrumented httpx client; never retries).
```

- [ ] **Step 10: Lint, type-check and the whole backend suite**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`
Expected: no errors; with `SCADBUDDY_TEST_DATABASE_URL` and a Temporal, nothing skipped
under `tests/api` for want of them.

- [ ] **Step 11: Commit**

```bash
git add backend/scadbuddy/telemetry/component.py backend/scadbuddy/api/telemetry.py \
  backend/scadbuddy/main.py backend/tests/test_trace_relay_route.py \
  backend/tests/api/test_trace_relay_app.py README.md CLAUDE.md
git commit -m "feat(tracing): POST /telemetry/v1/traces relays the browser's spans (#988)"
```

---

## After the last task

Run the full CI set from `backend/` (with `SCADBUDDY_TEST_DATABASE_URL` and a Temporal):

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest
```

The API diff comment the `freshness` job posts should show no change: the relay is out
of the schema. Then open the PR `feat(tracing): browser relay and SCADBUDDY_TRUSTED_PROXIES (#988)`
with `Refs #988` (the epic stays open for rows 3–5), noting in its body the follow-ups
from "Where this plan fills in or departs from the spec": item 4 (row 4's
`spanLimits`), item 8 (the agent's `/` prefix) and item 10 (row 4 drops on 403).
