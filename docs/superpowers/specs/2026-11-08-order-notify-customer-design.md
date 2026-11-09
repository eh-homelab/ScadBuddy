# `order_notify_customer` sends through a messaging plugin

Issue #5660. Follow-up to #4252 (orders spec `2026-10-18-orders-design.md` §7.3 and §12).
Status: design, for the owner's approval. No product code is in this PR.

## 1. Intent

Today `order_notify_customer` only writes a message. A person copies the text and sends it.
This design lets the shop send the message to the customer through a messaging plugin
that the shop installed (email, SMS, chat). A send is an `outward` call: a person approves
it, or a grant covers it. The order's audit records each send. A deployment with no
messaging plugin keeps the compose-only tool as it is.

## 2. What exists today (with sources)

- `order_notify_customer` is in `agent/src/tools/orders.ts` (`defineTool`, `risk: 'read'`).
  It reads `GET /api/v1/orders/{order_id}` and `GET /api/v1/customers/{customer_id}`.
  `composeNotice` builds the text for one of four templates (`NOTIFY_TEMPLATES`:
  `quoted`, `accepted`, `ready`, `shipped`). It addresses the customer's primary contacts,
  else every contact. It always answers `sent: false`, and adds `warning` when the
  customer has no contact. A cart (no customer or no number) is refused.
- The built-in flow `order-fulfilment` (`backend/scadbuddy/orders/builtin_flow.py`) calls
  `tool("order_notify_customer", {"template": "ready"})` when its input `notify` is true
  and the re-read order is `ready`. Its standing grant names only `order_item_fulfil` and
  `order_attach`. The compose call needs no grant because it is `read`.
- Tool tiers: every tool has a `risk` (`read`, `write`, `outward`) in its `ToolSpec`
  (`agent/src/tools/registry.ts`). Settings can move a tool's tier either way
  (`effectiveTool`, #1953, #2171). Only `pinnedOutward` (`agent/src/tools/pinnedOutward.ts`,
  #959) keeps a call outward whatever the override. The owner rejected new fixed or
  non-lowerable tiers in #4208, so this design adds none.
- Outward approval: in a session an outward call parks on the session workflow's gate
  until a person decides (`agent/src/harness/permissions.ts`, `sessions/turnHost.ts`,
  `ai_pending_input`). Over `/mcp` it is an `ai_approvals` row
  (`agent/src/approvals/service.ts`) that `confirm_action` runs. `prepareApproval` on a
  `ToolSpec` can resolve what the person approves, or refuse the call before it parks
  (#4442). In a flow, a call is covered by a grant rule (`backend/scadbuddy/flows/grants.py`
  `GrantRule`: `fn`, `tool`, `orders`, `max_calls`) or parks for a
  `workflow_run_decisions` approval.
- Idempotency: `idempotencyKey(ctx)` in `agent/src/tools/registry.ts` is a digest of the
  call's `callKey`, the same on every host path that runs one model call.
- Plugins (#297): remote MCP plugins live in `ai_plugins` (`agent/src/plugins/registry.ts`,
  routes `agent/src/routes/plugins.ts`). Claude Code reaches them only through the loopback
  forwarder (`agent/src/plugins/forwarder.ts`), which pins the checked address and adds
  the header or the OAuth token (`agent/src/plugins/oauth.ts`). Every plugin tool is
  `outward` unless `tool_tiers` names it (`registry.ts`). Plugin packages
  (`agent/src/plugins/packages/`) can carry http MCP servers, which also go through the
  forwarder. A package that starts a process is refused unless approved with
  `allow_refused` (`agent/src/harness/plugins.ts`).
- Agent code can already call a plugin's tool without Claude Code: a turn tool or a flow's
  `<server>__<tool>` call is the activity `turn:<server>__<tool>`
  (`agent/src/temporal/turnTools.ts` `turnToolActivity`, servers built by
  `turnServers.ts`). A flow retries a call only when its tier and its declared tier are
  both `read` (`turnTools.ts`, `turn_tool_tier`).
- **No messaging plugin exists, and no plugin "capability" for sending exists.** A search
  of `agent/src`, `backend/scadbuddy` and `docs/` finds no messaging, email or SMTP sender
  and no capability role on a plugin. The only mention is orders spec §7.3 ("an email or
  messaging MCP plugin, #297"). `/api/v1/ai/capabilities` (`agent/src/routes/plugins.ts`)
  is the tool-access switchboard (#4828), not a role a plugin declares.
- The order's audit is `order_events` (`backend/scadbuddy/orders/store.py`, written by
  `Lifecycle` in `backend/scadbuddy/orders/lifecycle.py`; `by` from `orders/actor.py`, never
  a body).
- Storefront mail: storefront spec `2026-11-02-storefront-design.md` §5.7 lists the mail
  the storefront sends: the sign-in code, the order link, "a revision is ready", "your
  price is ready", "payment requested", "shipped". It says "Every other customer
  notification stays a plugin's job (orders spec §12)". #5283 owns it and is open; no
  SMTP code exists yet. The comment on #5283 for this design says the storefront sends
  these "for an order with a storefront visitor only" and proposes "the plugin skips
  events 2 to 6 for an order that has a storefront visitor". `orders.source`
  (`back_office` or `storefront`) and `orders.storefront_visitor_id` exist
  (`backend/scadbuddy/orders/models.py`, migration `20261107T2235Z_storefront_drafts.sql`).
  Note: spec §5.7 says the CRUD service sends this mail with SMTP settings sealed under
  its KEK. The brief for this design says the sender lives in the storefront edge. This
  design does not depend on which one is right (§8), but the owner must confirm it (Q2).

## 3. Goals, non-goals, anti-goals

Goals:

1. The agent, a flow or `/mcp` can send an order message to the customer through a
   messaging plugin the shop chose.
2. Each send is `outward`: a person approves the exact recipients and text, or a grant
   rule covers it.
3. Each send has one durable record on the order: who asked, what went, to whom,
   through which plugin and tool, and the outcome.
4. One request never sends twice: a retry, a lost answer or a re-run approval is safe.
5. With no messaging plugin, `order_notify_customer` works as it does today.
6. A storefront order never gets the same notice from the storefront mail and from the
   plugin.
7. It works the same in every agent path: a session turn (in-process and as a ToolCall
   activity), `/mcp`, and a flow run.

Non-goals:

- New message templates, or templates the shop edits. The four templates stay.
- Inbound messages (customer replies).
- A ScadBuddy SMTP sender for back-office orders. That is a plugin's job (orders spec §12).
- Changes to the storefront mail itself.
- Attachments in a message.

Anti-goals (we must not do these):

- Use the storefront's mail sender for this path.
- Give the CRUD service (the API) a plugin client, a plugin secret or plugin code.
- Run plugin code in a pod that holds a database secret. Only remote (http) MCP servers
  can be bound (§4.3).
- Add a database client, table read or grant to the agent. The agent writes the record
  through CRUD routes only (root `CLAUDE.md`, "Database access (#5295)").
- Add a fixed or non-lowerable tier (#4208).
- Send a message the person did not approve: what runs is what was approved.

## 4. The plugin contract

### 4.1 Decision: a binding, not a capability

There is no capability system for plugins, and this design does not add one. A plugin
author cannot be asked to implement a ScadBuddy interface: the shop installs an off-the-
shelf MCP server (an email MCP, an SMS gateway's MCP). So the shop **binds** one tool of
one installed plugin to the role "customer messages". The binding maps ScadBuddy's
message fields to that tool's argument names. This is the minimal contract.

### 4.2 The binding

A stored setting in the CRUD service, `orders_customer_messaging` (live, nullable; null is
"no plugin, compose only"). It holds no secret. The plugin's URL, header and OAuth tokens
stay in the agent (`ai_plugins`, `ai_plugin_oauth`).

```jsonc
{
  "source": "remote:mail",          // remote:<ai_plugins name> or package:<package>/<http server>
  "tool": "send_email",             // the plugin tool's own name
  "contact_kind": "email",          // which customer contacts this channel reaches
  "args": {
    "to": "to",                     // required: the argument that takes the address(es)
    "to_shape": "list",             // "list" of strings, or "string" (comma-joined)
    "subject": "subject",           // optional: null when the channel has none (SMS)
    "text": "body",                 // required: the plain-text body
    "idempotency_key": null         // optional: the argument for a dedupe key, if the tool takes one
  },
  "fixed_args": {"from": "shop@example.com"}  // optional constants, never a secret
}
```

Rules:

- The tool must take plain JSON strings for `to`, `subject` and `text`. ScadBuddy sends
  plain text only.
- `contact_kind` filters the customer's contacts: only contacts of that kind are
  addressed (primary ones of that kind, else every one of that kind).
- `fixed_args` may not name an argument that `args` maps, and its values are strings,
  numbers or booleans. The binding route refuses a key whose name looks like a secret
  (`password`, `token`, `key`, `secret`); secrets go in the plugin's header or OAuth.

### 4.3 Validation (agent route)

`PUT /api/v1/ai/orders/messaging-binding` on the agent (person-only, same origin as the
plugin routes) checks the binding, then writes the setting through the CRUD route
`PUT /api/v1/settings` (no new grant):

1. The source exists and is enabled.
2. The source is a remote MCP server: an `ai_plugins` row, or a package's **http** server.
   A stdio or local server is refused, even one approved with `allow_refused`: its code
   would run in the agent pod, which holds a database secret until #5299.
3. The tool is listed now (`tools/list` through the forwarder, with its egress check),
   and is not disabled in Settings → Tools & plugins.
4. The tool's `inputSchema` has each mapped argument with a `string` type (or an array of
   strings for `to_shape: list`), and every required argument is mapped or in `fixed_args`.
5. A "Send a test" button sends one message to an address the person types, through the
   same path as §6, recorded as a test (no order).

The binding is read again at every send. A plugin removed, disabled or changed since is a
refusal at send time (`messaging_plugin_unavailable`), never a silent fallback.

### 4.4 What the plugin answers

Success is an MCP `tools/call` result with `isError` false. ScadBuddy keeps an id from it
only when the result's `structuredContent` has a string `id` or `message_id`; otherwise
none. The result text is untrusted data: it is not stored and not shown to the model as
instructions (the untrusted-data envelope, `agent/src/safety/untrusted.ts`). `isError` true
is a failed send (§7).

## 5. The tools

### 5.1 Decision: two tools

- `order_notify_customer` stays `read` and compose-only. It gains two fields in its answer:
  `channel` (the bound source and `contact_kind`, or null) and `send_ready` (a binding
  exists and at least one contact of its kind exists). Its `to` uses the bound
  `contact_kind` when there is a binding. Existing callers and the built-in flow's current
  version keep working.
- New `order_notify_customer_send`, `outward`. Input: `order_id`, `template`, `note?`,
  `also_when_storefront_mails?` (default false, §8). It composes with the same
  `composeNotice`, then sends through the binding.

Reason: a tier is per tool. One tool with a `send` flag would need an input-dependent tier
that Settings cannot lower, which is the fixed tier #4208 rejected. Two tools keep each
tier honest and each one configurable. The cost: the issue title names one tool. Q1 asks
the owner to confirm.

### 5.2 Approval binds the exact message

`order_notify_customer_send` has a `prepareApproval` (#4442 shape):

- It reads the order, the customer and the binding, and composes the message.
- It refuses before anything parks when: there is no binding (`no_messaging_plugin`,
  with the composed text, so the person can still send it by hand); no contact of the
  bound kind exists (`no_contact`); the order is a cart; or §8 skips it (`skipped`,
  `storefront_mail`).
- Otherwise the approved input carries `to`, `subject`, `text`, `source` and `tool`, and
  the summary line is "Send '<subject>' to <addresses> through <source>".
- The handler composes again. If the result differs from the approved input (the order,
  the customer or the binding changed), it refuses `message_changed` and sends nothing.
  What runs is what was approved.

`flowBinding` binds the same resolved fields, so a flow's per-call approval
(`workflow_run_decisions`) is for the exact message too.

## 6. The outward approval and grant flow

| Path | How a send is allowed |
|---|---|
| Session (panel) | Parks on the session gate; the person sees the summary and the full text; approve runs it once. |
| `/mcp` | Answers a pending action (`ai_approvals`); `confirm_action` with the same input runs it once. |
| Flow run | A grant rule `{"fn": "tool", "tool": "order_notify_customer_send", "orders": [...] or "any", "max_calls": n}` covers it; else it parks for `workflow_run_decisions`. |
| Settings override | A person may lower the tool's tier (#2171, one-line confirmation). Then it runs without asking. This design adds no extra guard (#4208). |

Each path runs the same `defineTool` handler: in-process for the harness and `/mcp`, as a
ToolCall activity in an AgentSession, and as a `flow-<id>` call
(`agent/src/temporal/toolActivities.ts`). The messaging client (§6.1) is a `ToolServices`
member built in both the sidecar (`main.ts`) and the worker (`temporal/worker.ts`), so no
path lacks it.

### 6.1 The messaging client (agent)

`agent/src/orders/messaging.ts` (new): given the binding, it builds one MCP client for the
source as `turnServers.ts` builds a turn's (the forwarder route, the egress check, the
header or OAuth token), calls the tool once, and closes the client. It never hands the
plugin's URL or secret to the model. It reports one of: `sent` (with optional provider
id), `failed` (`isError`, or a refusal before any request was written: egress refused,
plugin off, tool not listed), or `unknown` (the request was written and no answer came).

### 6.2 The built-in flow

A new built-in version of `order-fulfilment`: with `notify` true and the order `ready`, it
composes, and when `send_ready` is true it calls `order_notify_customer_send` with
`template: "ready"`. Its standing grant gains the rule
`{"fn": "tool", "tool": "order_notify_customer_send", "orders": [<order>], "max_calls": 1}`.
`move_triggers` (`builtin_flow.py`) moves a trigger only when its approved grant admits
the new call, so today's triggers get `NEWER_NOTICE` and keep composing until a person
approves the new grant. No send starts without a person's yes. Q3 asks whether `notify`
should mean "send" or whether a new input `send` is wanted.

## 7. The audit record, failure and retry

### 7.1 The record (CRUD service)

New table `order_messages` (backend migration, a new file
`<UTC stamp>_order_messages.sql`):

| Column | Meaning |
|---|---|
| `id` | uuid |
| `order_id` | the order |
| `template` | `quoted`, `accepted`, `ready`, `shipped`, or `test` (order null) |
| `source`, `tool` | the binding used |
| `recipients` | jsonb `[{kind, value}]` |
| `subject`, `text` | exactly what was sent |
| `status` | `sending`, `sent`, `failed`, `unknown`, `skipped` |
| `reason` | a fixed category (`plugin_error`, `transport`, `no_answer`, `storefront_mail`, …), never the plugin's text (as #5684 does for webhooks) |
| `provider_message_id` | from §4.4, or null |
| `idempotency_key` | unique |
| `episode` | the id of the order's newest status-change audit row at claim time |
| `resend_of` | an earlier message this one sends again, or null |
| `by` | from the author header (`orders/actor.py`), never a body |
| `session_id`, `flow_run_id` | where the call came from, when known |
| `created_at`, `finished_at` | times |

Routes (`backend/scadbuddy/api/order_messages.py`, all under `/api/v1/orders/{id}/messages`):

- `POST` (claim). Header `Idempotency-Key`. Under the order's row lock (`lock_order`):
  1. A row with that key exists: answer it as it is (200). Nothing new is written.
  2. §8 says skip: write a `skipped` row and the audit row `message.skipped`; answer it.
  3. A `sent` row of the same template and `episode` exists and the body has no
     `resend_of`: refuse 409 `already_notified`, naming that row. This stops a flow
     re-run or a second session from telling the customer twice for one status change.
  4. Else write a `sending` row and the audit row `message.sending`; answer 201.
- `POST …/{message_id}/outcome`: `sent`, `failed` or `unknown`, with `reason` and
  `provider_message_id`. Only from `sending`. Writes the audit row `message.sent`,
  `message.failed` or `message.unknown` in the same transaction.
- `POST …/{message_id}/mark-sent`: a person says an `unknown` message did arrive.
  Person-only (`PERSON_ENDPOINTS`). Audit `message.marked_sent`.
- `GET`: the order's messages, newest first. `order_get` also returns the latest one per
  template.

The text is the customer's data; it lives and goes with the order (Q6).

### 7.2 One send per key

The handler's order of work:

1. `POST …/messages` with `Idempotency-Key: idempotencyKey(ctx)`.
2. If the answer is `sent`, `failed`, `unknown` or `skipped`: return it. Do not send.
3. If the answer is `sending` and the row is new: call the plugin, then post the outcome.
4. If the answer is `sending` and the row is old (a retry after a crash between the call
   and the outcome):
   - With `args.idempotency_key` bound: call the plugin again with the same key (the
     plugin drops the copy), then post the outcome.
   - Without it: post `unknown` (`no_answer`) and send nothing. The answer tells the
     person to check the inbox, then "Mark sent" or send again.
5. The plugin's key is the message row's `id`, so each send attempt of one row has one key.

"Send again" is a new call with `resend_of: <message id>`: a new key, a new approval, a
new row. It is the only way past `already_notified` and `unknown`.

Outward calls are not retried by Temporal on a lost answer (`turnTools.ts`: only `read`
calls are), and an approval runs once (`confirm_action`). The key and the claim make a
retry safe even if that changes.

A `sending` row older than 15 minutes reads as `unknown` in every answer, and the
housekeeping sweep (`workflows/housekeeping.py` `SWEEPS`, behind its patch) writes it as
`unknown` with an audit row. No message is ever sent by the sweep.

## 8. The storefront overlap rule

### 8.1 The starting rule, judged

"The plugin path skips an event that storefront mail already sends when the order has a
storefront visitor." It is right in intent. It has three gaps:

1. **Mail not set up.** A storefront order on a shop with no SMTP settings gets no
   storefront mail. Skipping there tells the customer nothing. The rule must ask whether
   the storefront mail *will send* this notice, not only whether a visitor exists.
2. **The event map.** Only two of the four templates overlap: `quoted` is "your price is
   ready" and `shipped` is "shipped". `accepted` and `ready` have no storefront mail, so
   the plugin sends them. The order link, "a revision is ready" and "payment requested"
   have no plugin template, so nothing is skipped for them.
3. **Who knows.** The plugin path runs in the agent, which reads no storefront table. The
   rule must be one function in the CRUD service, owned with the storefront mail, so the
   two senders never disagree.

### 8.2 The rule

In the CRUD service, `orders/notify_overlap.py`:

```python
STOREFRONT_MAILS = {"quoted": "price_ready", "shipped": "shipped"}

def storefront_mail_covers(order, template, settings) -> bool:
    event = STOREFRONT_MAILS.get(template)
    return (
        event is not None
        and order.source == "storefront"
        and order.storefront_visitor_id is not None
        and storefront_mail_enabled(settings, event)   # #5283's: SMTP set and that mail on
    )
```

- The claim route (§7.1 step 2) and `prepareApproval` (§5.2) both call it through the
  route; the agent never decides it.
- Until #5283's mail PR lands, `storefront_mail_enabled` returns false, so nothing is
  skipped. That PR fills it and names its event ids (the #5283 comment promises this).
- `also_when_storefront_mails: true` sends anyway. The approval summary says "the
  storefront mail also sends this". It exists for a second channel (an SMS nudge); a flow
  never sets it.
- A skip is a `skipped` row with `reason: storefront_mail` and an audit row, so the order
  shows why no plugin message went.

## 9. Tests

Backend (`backend/tests/`, Postgres):

- Claim: same key twice gives one row; two keys in one episode give `already_notified`;
  `resend_of` passes; a new status change (new episode) passes.
- Outcome moves only from `sending`; `mark-sent` only from `unknown` and only for a person.
- Each write and its `order_events` row commit together; `by` comes from the header.
- `storefront_mail_covers` matrix: source × visitor × mail on/off × each template.
- The stale-`sending` sweep writes `unknown` and sends nothing.
- The setting's schema: secret-looking `fixed_args` keys refused.

Agent (`agent/test/`, a fake http MCP server through the real forwarder):

- No binding: `order_notify_customer` unchanged plus `send_ready: false`; the send tool
  refuses before it parks, with the text.
- Sent once: approve, the fake gets one call, the record is `sent`.
- Plugin `isError`: `failed`, `plugin_error`, no retry.
- Lost answer, no key arg: the second run posts `unknown` and the fake sees one call.
- Lost answer, key arg bound: the second run calls again with the same key.
- `message_changed`: the order changes between park and approve; nothing is sent.
- Storefront skip and `also_when_storefront_mails`.
- Binding validation: a stdio package server refused; a missing or non-string argument
  refused; a disabled tool refused at send.
- Every path: in-process (`/mcp` + `confirm_action`), ToolCall activity in an
  AgentSession, and a `flow-<id>` call under a grant (Temporal tests); each sends once.
- `coverage.test.ts` covers the new routes.

Flow (`backend/tests/`, Temporal): the new built-in version sends under its grant; an
older trigger gets `NEWER_NOTICE`; a replay of
`tests/fixtures/project_workflow_histories/` still passes.

Frontend (vitest + msw): the Settings binding form and its test send; the order page's
messages list with "Mark sent" and "Send again"; the panel's approval shows the full text.

## 10. PR breakdown

1. **Backend record.** Migration `order_messages`, the routes of §7.1, the audit kinds,
   the setting `orders_customer_messaging`, `notify_overlap.py` with
   `storefront_mail_enabled` returning false, the stale sweep. Tests.
2. **Agent send.** `orders/messaging.ts`, the binding route and validation (§4.3),
   `order_notify_customer_send` with `prepareApproval` and `flowBinding`, the new answer
   fields of `order_notify_customer`, `ToolServices` wiring in sidecar and worker. Tests on
   every path.
3. **Frontend.** Settings → Orders "Customer messages" (bind, test send, unbind), the
   order page's messages, the approval text. msw mocks.
4. **Built-in flow.** The new version and its grant rule; docs (orders spec §7.3 "As built",
   `backend/CLAUDE.md`, `agent/CLAUDE.md`).
5. **Overlap fill-in** (with #5283's mail PR, by the storefront session):
   `storefront_mail_enabled` reads the real mail settings and event switches.

Each PR is `Refs #5660`; the last of 1–4 to merge says `Fixes #5660`.

## 11. Decisions made here (with reason and cost)

- A binding, not a plugin capability (§4.1): works with off-the-shelf MCP servers. Cost: a
  person maps argument names once.
- Remote http servers only (§4.3): no plugin code in a pod with a database secret. Cost:
  a local mail tool cannot be bound until #5299 and #5296 land.
- Two tools (§5.1): each tier configurable. Cost: a second tool name.
- The approval holds the exact text (§5.2): what runs is what was approved. Cost: a change
  between park and approve needs a new approval.
- `unknown` is never resent by itself (§7.2): no double send. Cost: a person checks.
- Error categories only, no plugin text stored (§7.1): untrusted text stays out of the
  record. Cost: less detail for debugging; the agent log has the call's span.

## 12. Open questions for the owner (priority order)

1. **One tool or two?** The issue says `order_notify_customer` sends. This design keeps it
   compose-only and adds `order_notify_customer_send` (§5.1), because one tool with a
   `send` flag needs a tier Settings cannot lower (#4208). Confirm, or choose one tool
   whose tier is always `outward` (then a compose-only call also asks a person).
2. **Where the storefront mail lives, and what it covers.** Spec §5.7 says the CRUD
   service sends it; the brief for this work says the storefront edge does. The overlap
   rule needs a CRUD-side fact either way (`storefront_mail_enabled`). Also: does a
   `back_office` order that got a customer link ("Send customer link", spec §5.2) get
   storefront "shipped" mail? This design says no (it needs `source = storefront` and a
   visitor). And should the plugin still skip when its channel is not email (SMS)? This
   design skips anyway, with `also_when_storefront_mails` as the way to send both.
3. **The built-in flow's `notify`.** Should `notify: true` send once a binding exists
   (this design, after a person approves the new grant), or should "send" be a new input?
4. **Which plugin first.** No messaging plugin is installed or in the repo. Which channel
   should the first dogfood binding and the e2e test use (an SMTP MCP, Gmail's MCP, an SMS
   gateway)?
5. **Shop-edited templates.** Out of scope here. Is a template editor wanted next, or are
   the four fixed texts enough?
6. **Keeping message text.** `order_messages` keeps the full text and recipients for as
   long as the order exists. Is that the retention you want, or should text be dropped
   after a period (keeping a hash)?
