# openscad-lsp hardening (#95) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close #95. Its feature shipped in PR #188 (bridge and client), #883/#185
(go-to-definition into includes and libraries). The "Open in VS Code" link is #648. Two
things the language-server WebSocket still lacks: a per-client session limit and network
isolation for the process it starts.

**Architecture:** A per-client counter, keyed like the telemetry relay's rate limits
(`telemetry/admission.py` `relay_client` + `bucket_key`, so `SCADBUDDY_TRUSTED_PROXIES`
decides who a client is), held by a new `editor` component and taken before the global
`state.language_servers` permit. Every openscad-lsp start (the socket bridge and the
diagnostics route) goes through a small stdlib-only launcher, `scadbuddy/editor/nonet.py`.
The launcher sets `PR_SET_NO_NEW_PRIVS`, installs a seccomp filter that refuses every
non-`AF_UNIX` `socket()` and `io_uring_setup`, then `execv`s the server. That needs no
privilege, so it works in a default container, where `unshare -n` is refused.

**Tech Stack:** Python 3.12, FastAPI WebSockets, ctypes `prctl`, classic BPF.

**Spec:** #95 issue body and its scope-change comment; PR #188's description.

## Global Constraints

- Refusals happen before `accept()`: same-origin first (`refuse_foreign_origin`), then caps (1013).
- The child gets `env_for(...)` only (no `SCADBUDDY_*`, no secrets); unchanged.
- Process lifetime equals the socket's; unchanged (`library/lsp.py` `serve`).
- x86_64 and aarch64 only (the image's two arches); any other arch fails closed (the
  launcher exits non-zero and the editor runs without a language server).

## Review Focus

- A client past its own limit is refused with 1013 while other clients still connect.
- A refused or closed session gives its per-client slot back (no leak after disconnect).
- `X-Forwarded-For` from an untrusted peer does not mint a fresh client.
- A filtered server still runs: the real openscad-lsp test passes through the launcher.
- AF_INET/AF_INET6/AF_NETLINK/AF_PACKET sockets fail in the child; AF_UNIX works.

---

### Task 1: no-network launcher

**Files:** Create `backend/scadbuddy/editor/__init__.py`, `backend/scadbuddy/editor/nonet.py`;
test `backend/tests/test_editor_nonet.py`.

**Produces:** `nonet.command(binary: str, *args: str) -> list[str]` (the argv that runs
`binary` under the filter), `nonet.main(argv) -> NoReturn`.

- [ ] Test: run `command(sys.executable, "-I", probe.py)` where the probe opens AF_UNIX
  (works) and AF_INET, AF_INET6, AF_NETLINK and AF_PACKET (each `OSError` EAFNOSUPPORT);
  run `command("/bin/true")` and expect exit 0 (exec works); an unknown arch exits non-zero.
- [ ] Implement the BPF filter and launcher; run; commit.

### Task 2: launch openscad-lsp through it

**Files:** Modify `backend/scadbuddy/library/lsp.py` (`serve`) and
`backend/scadbuddy/library/lsp_diagnostics.py` (`lsp_diagnostics`) to spawn
`*nonet.command(binary, "--stdio")`. Test: in `tests/api/test_lsp.py`, the fake server
reports whether `socket(AF_INET)` succeeds, and the test asserts that it does not.

### Task 3: per-client session limit

**Files:** Create `backend/scadbuddy/editor/component.py` (`LanguageServerClients` with
`take(client) -> bool` / `give(client)` as a context manager `slot(client)`;
`LSP_SESSIONS_PER_CLIENT = 2`; `Key("language_server_clients")`). Modify
`backend/scadbuddy/api/lsp.py` `_serve` to check the client slot after the global check
and before accept. Test in `tests/api/test_lsp.py`: with `lsp_sessions=4`, the third
socket from one client is refused 1013; a second client (trusted proxy plus
`X-Forwarded-For`) still connects; after the first closes, the same client connects again;
an untrusted peer's `X-Forwarded-For` is ignored.
