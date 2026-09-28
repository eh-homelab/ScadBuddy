# AI tests and evals: plan

> **This is a plan, not an implementation.** Nothing on this page exists yet except
> what "Already in place" lists. It follows issue
> [#259](https://github.com/eh-homelab/ScadBuddy/issues/259) and spec §13 of
> [`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).

## Rule: CI never calls Anthropic

Spec §4.4 and §13, and issue #259: the Agent SDK is pointed at a **local fake
Anthropic-format endpoint** through `ANTHROPIC_BASE_URL`. This is the seam the
[LLM gateway docs](https://code.claude.com/docs/en/llm-gateway-connect) describe. Live
model runs are opt-in, and never part of required CI.

## Already in place

These are on `main` today.

- **The fake endpoint.** [`agent/test/support/fakeAnthropic.ts`](../../agent/test/support/fakeAnthropic.ts)
  serves scripted `/v1/messages` replies: text, one `tool_use` block, an API error, or a
  hang. Streaming requests get the Messages SSE sequence. Its header cites the
  [gateway protocol page](https://code.claude.com/docs/en/llm-gateway-protocol).
- **Harness tests against it.**
  - [`agent/test/run.test.ts`](../../agent/test/run.test.ts) runs the real SDK and the
    bundled Claude Code (PR #354 body).
  - [`agent/test/sessions.e2e.test.ts`](../../agent/test/sessions.e2e.test.ts) covers
    resume across replicas, fork, interrupt, tool events and replay (PR #377 body).
- **Postgres tests.** They run when `SCADBUDDY_TEST_DATABASE_URL` is set
  ([`agent/test/support/postgres.ts`](../../agent/test/support/postgres.ts)); PR #379
  ran them against `postgres:17`.
- **Permission seam unit tests.** [`agent/test/permissions.test.ts`](../../agent/test/permissions.test.ts).
- **Plugin vetting tests.** [`agent/test/plugins.test.ts`](../../agent/test/plugins.test.ts),
  with the fixtures in `agent/test/fixtures/plugins/`.
- **Mocked UI e2e.** The assistant panel and the bridge are covered against the
  scripted mock agent: [`frontend/e2e/assistant.spec.ts`](../../frontend/e2e/assistant.spec.ts)
  and [`frontend/e2e/agent-bridge.spec.ts`](../../frontend/e2e/agent-bridge.spec.ts).

## Planned: agent service tests (vitest in `agent/`)

From issue #259 and spec §13. Items that depend on unmerged work name the PR.

| Area | Test | Depends on |
|---|---|---|
| Credentials | API key vs gateway token reach Claude Code as the right variables (`credentialEnv()`) | merged |
| Least privilege | `tools: []` enforced: a scripted `tool_use` for `Bash` never runs | merged |
| Tiers | `canUseTool`/`PreToolUse` gating per tier, and unknown tools treated as `outward` | merged; approvals in #471 |
| Limits | `maxTurns` and budget stop a looping script; session budget exhaustion | merged |
| Plugins | Loader reads the init message's `plugins` and `plugin_errors` ([Agent SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins), spec §3.1) | #464 |
| Sessions | `SessionStore` against Postgres | merged (#377), extend |
| Registry | Projection equality (harness vs `/mcp`) and the `openapi.json` coverage check (spec §5.1) | #368 |
| MCP transport | Streaming, resume, `Origin` check, plain-HTTP refusal (spec §8.4) | #368 |
| Approvals | Outward tool pauses, then is approved or denied | #471 |
| Headless browser | Playwright plugin loads from its pinned path; tiers; disallowed tools absent (spec §5.3) | #349 |

## Planned: scripted e2e (Playwright, agent against the fake endpoint)

These are the scenarios issue #259 lists:

1. "Change the text and colour" on `name-keychain`.
2. "Create a new cable label model" from a template, through to a successful render.
3. "Add BOSL2 and use a rounded cube".
4. A send that is denied at confirmation.
5. An external MCP client starts a session, and the browser takes it over (#300).

Each needs the WebSocket transport to the panel (#266) and, apart from (1), tools from
#368. For the headless browser, spec §13 adds a container e2e: clicking *Print* without
an approval is refused by the backend; an off-allowlist navigation is blocked; two
sessions don't share storage.

## Planned: opt-in live evals

From issue #259 and spec §13, "Live evals":

- **Where it runs.** A small eval set against real Claude models. It is never in
  required CI. It runs manually or on a schedule, with a credential supplied to that job
  only.
- **What it measures.** Task success on customizing, authoring and analyzers. The
  rendered result is checked with the existing `models/<name>/verify.sh`-style checks
  (see [`models/`](../../models/)).
- **Citation quality.** Every suggested change carries a source, and the cited source
  actually supports the claim (spec D10). The scoring method is **not designed yet**.
  Candidates are a rubric grader, or checking that a cited repository path and section
  exist.
- **Output.** A per-task pass/fail and a citation score, recorded with the model id and
  the SDK/CLI versions (the build pins `CLAUDE_CODE_VERSION`; see the
  [`Dockerfile`](../../Dockerfile)).

## Open questions

- How a live-eval job gets its credential without it reaching fork PRs. `CLAUDE.md` says
  never to run fork code on the self-hosted pools, and a secret on hosted runners must
  be limited to non-fork events.
- Whether the eval set lives in `agent/` or in a separate `evals/` directory.
- A pass threshold, if any. Issue #259 keeps live evals out of required CI, so they cannot gate a merge.
