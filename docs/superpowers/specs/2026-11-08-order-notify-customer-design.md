# `order_notify_customer` sends through a messaging plugin

Issue #5660. Follow-up to #4252 (orders spec `2026-10-18-orders-design.md` §7.3 and §12).
Status: **approved by the owner on 2026-11-09**, with the calls in §12. The storefront
facts in §2 and §8 are scadify's answers (#5283) of the same day.

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
  and the re-read order is `ready`. It runs on an `order.changed` trigger that the shop
  makes, with a standing grant that the shop writes and approves; the module docstring
  says it names `queue_print`, `order_item_fulfil` and `order_attach`, and no code writes
  or approves it (`builtin_flow.py` lines 12-15). The two-rule grant in the script
  (`order_item_fulfil`, `order_attach`) is only the grant of its `agent(...)` call, which
  narrows the run's. The compose call needs no grant because it is `read`.
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
  SMTP code exists yet on main. The event list is
  [#5283's comment](https://github.com/eh-homelab/ScadBuddy/issues/5283#issuecomment-6729346808).
  scadify's answers for this design (2026-11-09), which replace that comment's
  "storefront visitor only" rule:
  - The **CRUD service** sends storefront mail, after the transaction that causes the
    event commits. The storefront edge holds no database secret and no KEK under #5295;
    scadify's mail PR corrects storefront spec §10 to say so.
  - Storefront mail goes to **every order with a live (unrevoked) order link**,
    `back_office` orders included ("Send customer link", spec §5.2). A revoked link stops
    it. It does not depend on `orders.source` or `orders.storefront_visitor_id`.
  - The seam is `storefront_mail_covers(conn, order_id, event, *, ready) -> bool`
    (`orders/storefront/mail.py`): true when the order has a live order link and a mail
    host and a sender are set. Its events are
    `order_link`, `revision_ready`, `price_ready`, `payment_requested`, `shipped`. It lands
    in scadify's branch `feat/storefront-follow` (PR 1 of #5283).

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
6. An order that gets storefront mail never gets the same notice from the storefront
   mail and from the plugin.
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
  numbers or booleans. The setting refuses a key whose name looks like a secret: one of
  its words, split at `_` and at a lower-to-upper case change, is `pass`, `password`,
  `token`, `secret`, `key`, `apikey`, `auth`, `credential`, a near form, or a lowercase
  compound such as `apitoken`, `smtppass` or `privatekey` (`message_models.py`
  `SECRET_WORDS`). So `smtp_password` and `apiKey` are refused, and `author` and `keyword`
  pass. Secrets go in the plugin's header or OAuth.

### 4.3 Validation (agent route)

`POST /api/v1/ai/orders/messaging-binding/check` on the agent (same origin as the plugin
routes) checks a binding and writes nothing. The Settings page then saves it as the
person, with the browser's own `PUT /api/v1/settings`, as every other setting is saved.
The agent never writes the setting, so no service credential gets a settings write. A
binding saved without the check is still safe: the send checks rules 1 to 4 again (below).
The checks:

1. The source exists and is enabled.
2. The source is a remote MCP server: an `ai_plugins` row, or a package's **http** server.
   A stdio or local server is refused, even one approved with `allow_refused`: its code
   would run in the agent pod, which holds a database secret until #5299.
3. The tool is listed now (`tools/list` through the forwarder, with its egress check),
   and is not disabled in Settings → Tools & plugins.
4. The tool's `inputSchema` has each mapped argument with a `string` type (or an array of
   strings for `to_shape: list`), and every required argument is mapped or in `fixed_args`.
5. A "Send a test" button (`POST /api/v1/ai/orders/messaging-binding/test`) sends one
   message to an address the person types, through the messaging client of §6.1. It sends
   a real message, so it needs a person's signed-in session (401 without one): no tool, no
   flow and no `/mcp` client reaches it. It belongs to no order, so no `order_messages` row
   records it; the agent's audit log does, like any other send (kind `plugin`, action
   `messaging.test_send`, the person, the address, the source and tool, and the outcome).
   With auth mode `off` no person is signed in, so the test answers 401, and the page says
   that the test needs a signed-in person.

Every send runs rules 1 to 4 again (`orders/messaging.ts` `openChecked`) before it claims a
row, so a tool changed or removed since the binding was saved is refused
`messaging_plugin_unavailable` and nothing is claimed or sent.

The binding is read again at every send. A plugin removed, disabled or changed since is a
refusal at send time (`messaging_plugin_unavailable`), never a silent fallback.

### 4.4 What the plugin answers

Success is an MCP `tools/call` result with `isError` false. ScadBuddy keeps an id from it
only when the result's `structuredContent` has a string `id` or `message_id`; otherwise
none. The result text is untrusted data: it is not stored and not shown to the model as
instructions (the untrusted-data envelope, `agent/src/safety/untrusted.ts`). `isError` true
is a failed send (§7). The result is not checked against the tool's `outputSchema`: the
client sends a plain `tools/call` request, not the SDK's `callTool`, which would throw for a
message the plugin already sent. Only `isError`, or a JSON-RPC error from the server, is a
refusal.

A message goes to at most 20 recipients (the record's bound). A customer with more contacts
of the bound kind to address is refused `too_many_recipients` before anything parks; the
shop marks the ones to use as primary.

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
tier honest and each one configurable. The cost: the issue title names one tool. The
owner approved two tools (§12).

### 5.2 Approval binds the exact message

`order_notify_customer_send` has a `prepareApproval` (#4442 shape):

- It reads the order, the customer and the binding, and composes the message.
- It refuses before anything parks when: there is no binding (`no_messaging_plugin`,
  with the composed text, so the person can still send it by hand); no contact of the
  bound kind exists (`no_contact`); the order is a cart; the template was already sent
  for the order's current status (`already_notified`); or §8 skips it (`skipped`,
  `storefront_mail`). The last two come from the read-only route
  `GET /api/v1/orders/{id}/messages/check?template=` (§7.1), which writes nothing.
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

A new built-in version of `order-fulfilment` takes a new opt-in input `send` (boolean,
default false; the owner's call, §12). `notify` keeps its meaning: compose the `ready`
message. Only with `notify` and `send` both true, the order `ready` and `send_ready` true
does it call `order_notify_customer_send` with `template: "ready"`, from the script
itself (not inside `agent(...)`). So the trigger's **standing grant** must admit it: the
shop adds the rule
`{"fn": "tool", "tool": "order_notify_customer_send", "orders": "any", "max_calls": 1}`
and approves the grant again. The run grant seeded for one order's event narrows it to
that order (`narrowed_to_order`). No code writes or approves that rule.
`move_triggers` (`builtin_flow.py`) moves a trigger only when its approved standing grant
admits every call the new script makes that its version did not, so a trigger whose
grant lacks the rule gets `NEWER_NOTICE` and keeps its version. A moved trigger still
sends nothing until a person sets `send` true. Without the rule, a run with `send` true
parks the send for a `workflow_run_decisions` approval. No send starts without a
person's yes.

As built (PR 4): the run's result carries `send`, which says what became of the message.
The run page shows it with the rest of the result:

- `{"status": "sent", "message_id": …}`: the plugin took it.
- `{"status": "skipped", "reason": …}`: `notify` is off, the order is not ready, `send` is
  off (the message was composed for a person to send), or no plugin is bound or the
  customer has no contact of its kind.
- `{"status": "refused", "reason": …}`: the agent refused it (`already_notified`, a denied
  approval, `messaging_plugin_unavailable`, `message_changed`, a backend 4xx, a call that was
  not run). The run goes on to its end.
- `{"status": "unknown", "reason": "the outcome is unknown; check the order's messages"}`: the
  call failed in a way that may have sent it (a timeout, a lost answer, a 5xx). The script sees
  a failed call only as `Exception("<type>: <text>")` (the harness's `_script_outcome`), so
  `send_refusal` tells a refusal by the agent's own texts; nothing is sent again.
- `{"status": "not_sent", "reason": …, "message_id": …}`: it was claimed, but the plugin
  refused it or did not answer (`failed`, `unknown`, in flight).

A run never sends one message twice. A retried call keeps its request id, which is its
claim key, so the backend answers the first claim. Another run on the same ready order
finds the template told since the order's last status change (`episode`), so it is
refused `already_notified`.

## 7. The audit record, failure and retry

### 7.1 The record (CRUD service)

New table `order_messages` (backend migration, a new file
`<UTC stamp>_order_messages.sql`):

| Column | Meaning |
|---|---|
| `id` | uuid |
| `order_id` | the order; never null (a test send is in the agent's audit log, §4.3) |
| `template` | `quoted`, `accepted`, `ready`, `shipped` |
| `source`, `tool` | the binding used |
| `recipients` | jsonb `[{kind, value}]` |
| `subject`, `text` | exactly what was sent |
| `status` | `sending`, `sent`, `failed`, `unknown`, `skipped` |
| `reason` | a fixed category (`plugin_error`, `transport`, `no_answer`, `storefront_mail`, …), never the plugin's text (as #5684 does for webhooks) |
| `provider_message_id` | from §4.4, or null |
| `idempotency_key` | unique |
| `episode` | the id of the order's newest status-change audit row at claim time |
| `resend_of` | an earlier message this one sends again, or null |
| `by` | from the author header (`orders/actor.py`), never a body; its `session` and `run_id` say which session or flow run asked |
| `created_at`, `finished_at` | times |

Routes (`backend/scadbuddy/api/order_messages.py`, all under `/api/v1/orders/{id}/messages`):

- `GET …/check?template=` (read-only, writes nothing): whether a send of the template
  would be refused now: `already_notified` (with the message id) and `storefront_mail`
  (a boolean, §8). `prepareApproval` reads it, so a refused approval writes no row.
- `POST` (claim). Header `Idempotency-Key`. Under the order's row lock (`lock_order`). A
  cart is refused `order_not_submitted`.
  1. A row with that key exists: answer it as it is (200). Nothing new is written. A key
     of another order is 409 `idempotency_key_reused`, also when that order's claim
     commits the key between this claim's read and its insert (the insert's unique
     violation is caught and the row read again).
  2. §8 says skip and the body has no `also_when_storefront_mails`: write a `skipped` row
     (reason `storefront_mail`, `finished_at` set) and the audit row `message.skipped`;
     answer it (201). As built (PR 5) this is asked after steps 3 and 4, so a claim they
     refuse is refused, never recorded as skipped.
  3. A row of the same template and `episode` that is `sent`, `sending` or `unknown`
     exists and the body has no `resend_of`: refuse 409 `already_notified`, naming that
     row. This stops a flow re-run or a second session from telling the customer twice
     for one status change. A `failed` or `skipped` row does not block.
  4. With `resend_of`: it must name a row of this order and of the same template (else
     422), and not one still `sending` and fresh (409 `message_in_flight`). A stale
     `sending` row reads `unknown`, so it may be sent again by name.
  5. Else write a `sending` row and the audit row `message.sending`; answer 201.
- `POST …/{message_id}/outcome`: `sent`, `failed` or `unknown`, with `reason` and
  `provider_message_id`. Taken while the stored status is `sending` (stale or not) or
  `unknown`, so a slow plugin's late `sent` is recorded. `sent`, `failed` and `skipped`
  are final: 409 `message_final`.
  Writes the audit row `message.sent`, `message.failed` or `message.unknown` in the same
  transaction.
- `POST …/{message_id}/mark-sent`: a person says an `unknown` message (or a stale
  `sending` one) did arrive. Person-only (`PERSON_ENDPOINTS`). Audit `message.marked_sent`.
- `GET`: the order's messages, newest first.

The text is the customer's data. It is kept as long as the order exists and goes with
the order (the owner's call, §12).

### 7.2 One send per key

The handler's order of work:

1. `POST …/messages` with `Idempotency-Key: idempotencyKey(ctx)`.
2. If the row is new (201): call the plugin, then post the outcome.
3. If the row is old (200) and is `sent`, `failed` or `skipped`: return it. Do not send.
4. If the row is old (200) and `sending` or `unknown`: a retry after a crash, or a lost
   answer, between the call and the outcome.
   - With `args.idempotency_key` bound: call the plugin again with the same key (the
     plugin drops the copy), then post the outcome.
   - Without it, a row that reads `unknown` (stale, or recorded so): post `unknown`
     (`no_answer`) and send nothing. The answer tells the person to check the inbox, then
     "Mark sent" or send again.
   - Without it, a row still `sending` and fresh: another call is sending it now. Send
     nothing and post no outcome; the answer says it is in flight. Once stale, it reads
     `unknown` (above).
5. The plugin's key is the message row's `id`, so each send attempt of one row has one key.

"Send again" is a new call with `resend_of: <message id>`: a new key, a new approval, a
new row. It is the only way past `already_notified` and `unknown`. The ids are the read tool
`order_messages_list`'s (`GET …/messages`), as the order page lists them.

On the order page (PR 3), "Send again" goes to the agent's person-only routes
`POST /api/v1/ai/orders/{id}/messages/preview` and `…/send` (`routes/orderMessages.ts`, the
UI's origin and a signed-in person, as the test send). The preview composes the message and
answers it with its digest; nothing is sent or recorded. The send names the template,
`resend_of` and that digest, never the text: the agent composes the message again, refuses
409 `message_changed` when its digest differs, and else claims and sends it as the tool does,
under the open preview's `Idempotency-Key`. The browser makes one key per preview and keeps it
for every click on its Send until a send is answered, so a retry after a lost answer is served
from the first claim. The agent looks the key's claim up first (the check under that key) and
compares the digest only when there is none, so such a retry is served even when the order
changed since (#5978). The claimed row is the record: an ended one is answered with no plugin,
binding or contact, and one with no outcome is sent again only as step 4 allows, with the row's
own recipients and text, never a message composed since; it needs the binding
(`no_messaging_plugin` without one) but no contact. A backend 404, 409 or 422 keeps its status and slug: a claim that lost a
race after the preview is 409 `already_notified`. A template the storefront already emails
(§8) is refused 409 `storefront_mail` by the preview and the send; the page asks "The
storefront already emails this update to the customer. Send it here too?" and on yes previews
it again with `also_when_storefront_mails`, which that preview's send carries under its own
key. A row skipped for that reason offers "Send here too" (the owner's decision on #5660: a
person who presses it means it). Any other backend status is logged and answered
502 `upstream_error`: a 401 or 403 there is the agent's service credential, not the person. The
person's click on the shown message is the approval.

Outward calls are not retried by Temporal on a lost answer (`turnTools.ts`: only `read`
calls are), and an approval runs once (`confirm_action`). The key and the claim make a
retry safe even if that changes.

The stale rule: a `sending` row older than 15 minutes reads as `unknown` in every answer,
and it is never resent by itself. There is no sweep: the row stays `sending` in storage
until a write touches it (its outcome, or "Mark sent"), with its audit row. Only a
person's "Send again" (a new row with `resend_of`) sends it once more. A test covers it.

## 8. The storefront overlap rule

### 8.1 The starting rule, judged

"The plugin path skips an event that storefront mail already sends when the order has a
storefront visitor." It is right in intent. It had four gaps, which scadify's answers
(§2) close:

1. **Mail not set up.** An order on a shop with no storefront mail gets none. Skipping
   there tells the customer nothing. So the rule asks whether storefront mail *will send*
   this notice: the seam is true only when a mail host and a sender are set.
2. **Who gets storefront mail.** Not "a storefront visitor": every order with a live
   order link, `back_office` orders included. A revoked link stops it. A rule keyed on the
   visitor would send twice to a `back_office` order with a link, and skip wrongly for a
   storefront order whose link was revoked.
3. **The event map.** Only two of the four templates overlap: `quoted` is `price_ready`
   and `shipped` is `shipped`. `accepted` and `ready` have no storefront mail, so the
   plugin sends them. `order_link`, `revision_ready` and `payment_requested` have no plugin
   template, so nothing is skipped for them.
4. **Who knows.** The plugin path runs in the agent, which reads no storefront table. The
   rule is one function in the CRUD service, owned with the storefront mail, so the two
   senders never disagree.

### 8.2 The rule

scadify's seam (#5283, `orders/storefront/mail.py`, on main since #5923) is
`storefront_mail_covers(conn, order_id, event, *, ready) -> bool`: true when the order has
a live order link and ``ready`` (a mail host and a sender are set). It does not read
`source` or the visitor. This design only consumes it (PR 5), in the CRUD service, passing
whether the settings snapshot's mail config is ready:

```python
STOREFRONT_EVENT = {"quoted": "price_ready", "shipped": "shipped"}

def skipped_for_storefront(conn, order_id: str, template: str, *, ready: bool) -> bool:
    event = STOREFRONT_EVENT.get(template)
    return event is not None and storefront_mail_covers(conn, order_id, event, ready=ready)
```

- The claim route (§7.1 step 2) calls it under the order's row lock. `prepareApproval`
  (§5.2) reads the same answer through the read-only `GET …/messages/check`; the agent
  never decides it.
- As built (PR 5): `orders/messages.py` `STOREFRONT_EVENT` and `_skipped_for_storefront`,
  with the lifecycle's `mail_ready` (the running mail config has both a host and a
  sender; otherwise it covers nothing, #5969). The seam ignores the event today; the real event is always passed. The
  agent's `refuseIfTold` refuses `storefront_mail` before anything parks, and a claim that
  comes back `skipped` (the storefront began to mail between the check and the claim)
  sends nothing and says so in its answer's `warning`.
- `also_when_storefront_mails: true` sends anyway. The approval summary says "the
  storefront mail also sends this". It exists for a second channel (an SMS nudge), and for
  a person on the order page who answers yes to "Send it here too?" or presses "Send here
  too" on a skipped row (§7.2); a flow never sets it.
- A skip is a `skipped` row with `reason: storefront_mail` and an audit row, so the order
  shows why no plugin message went.

## 9. Tests

Backend (`backend/tests/`, Postgres):

- Claim: same key twice gives one row; two keys in one episode give `already_notified`;
  `resend_of` passes; a new status change (new episode) passes.
- Outcome is taken from `sending` or `unknown` and refused once final; `mark-sent` only
  from `unknown` or a stale `sending`, and only for a person.
- `GET …/check` answers `already_notified` and writes nothing.
- Each write and its `order_events` row commit together; `by` comes from the header.
- (PR 5) The skip: each template × `storefront_mail_covers` true or false, with a fake
  seam; only `quoted` and `shipped` can skip.
- The stale rule: a `sending` row older than 15 minutes reads as `unknown`, a new claim of
  its key returns it without a new row, a plain claim of its template is
  `already_notified`, and "Mark sent" takes it.
- The setting's schema: secret-looking `fixed_args` keys refused.

Agent (`agent/test/`, a fake http MCP server through the real forwarder):

- No binding: `order_notify_customer` unchanged plus `send_ready: false`; the send tool
  refuses before it parks, with the text.
- Sent once: approve, the fake gets one call, the record is `sent`.
- Plugin `isError`: `failed`, `plugin_error`, no retry.
- Lost answer, no key arg: the second run posts `unknown` and the fake sees one call.
- Lost answer, key arg bound: the second run calls again with the same key.
- `message_changed`: the order changes between park and approve; nothing is sent.
- (PR 5) Storefront skip and `also_when_storefront_mails`.
- The e2e test binds a fake SMTP-sending MCP server (the owner's choice for the first real
  binding) and checks one mail reaches a fake SMTP server per approved send.
- Binding validation: a stdio package server refused; a missing or non-string argument
  refused; a disabled tool refused at send.
- Every path: in-process (`/mcp` + `confirm_action`), ToolCall activity in an
  AgentSession, and a `flow-<id>` call under a grant (Temporal tests); each sends once.
- `coverage.test.ts` covers the new routes.

Flow (`backend/tests/`, Temporal): the new built-in version sends under its grant only
with `send` true, and only composes with `send` false; an older trigger gets
`NEWER_NOTICE`; a replay of
`tests/fixtures/project_workflow_histories/` still passes.

Frontend (vitest + msw): the Settings binding form and its test send; the order page's
messages list with "Mark sent" and "Send again"; the panel's approval shows the full text.

## 10. PR breakdown

1. **Backend record.** Migration `order_messages`, the routes of §7.1, the audit kinds,
   the setting `orders_customer_messaging`. No sweep and no storefront skip yet. Tests.
2. **Agent send.** `orders/messaging.ts`, the binding check and test routes (§4.3),
   `order_notify_customer_send` with `prepareApproval` and `flowBinding`, the new answer
   fields of `order_notify_customer`, `ToolServices` wiring in sidecar and worker. Tests on
   every path.
3. **Frontend.** Settings → Orders "Customer messages" (bind, test send, unbind), the
   order page's messages, the approval text. msw mocks.
4. **Built-in flow.** The new version, its opt-in input `send` and its grant rule; docs
   (orders spec §7.3 "As built", `backend/CLAUDE.md`, `agent/CLAUDE.md`).
5. **Overlap.** Waits for `storefront_mail_covers` on main (scadify's
   `feat/storefront-follow`, PR 1 of #5283). Then: the claim route's skip step,
   `skipped_for_storefront`, `also_when_storefront_mails`, and the answer in
   `prepareApproval`.

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

## 12. The owner's decisions (2026-11-09)

The owner approved this design on 2026-11-09 with these calls:

1. **Two tools.** `order_notify_customer` stays compose-only (`read`);
   `order_notify_customer_send` is the `outward` send (§5.1).
2. **Storefront mail** is the CRUD service's, sent after the causing transaction commits;
   it goes to every order with a live order link, `back_office` orders included; the seam
   is scadify's `storefront_mail_covers(conn, order_id, event, *, ready)` (§2, §8). The plugin skips
   `quoted` and `shipped` when the seam says the storefront mail sends them, whatever the
   plugin's channel; `also_when_storefront_mails` sends both on purpose.
3. **The built-in flow** sends only with a new opt-in input `send` (default false). `notify`
   alone still only composes (§6.2).
4. **The first real binding and the e2e test** use an SMTP-sending MCP server (§9).
5. **Templates:** the four fixed templates for now. A template editor is not planned.
6. **Retention:** message text and recipients are kept as long as the order exists (§7.1).
