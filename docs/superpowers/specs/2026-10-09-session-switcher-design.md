# Assistant sessions in the panel: fork from a message, Fork, and the switcher

Epic #792 (part of #249): #793 (fork route with `up_to`), #794 (Fork and "Fork from
here" in the panel), #795 (the session switcher). Written 2026-10-09.

## Where it stands

On main already:

- `SessionManager.fork` (`agent/src/sessions/manager.ts`) and `POST
  /api/v1/ai/sessions/:id/fork {title?}` (`routes/sessions.ts`). The fork copies the
  whole transcript. Budgets follow #823: a fork shares its parent's budget, except the
  user's "Continue in a new chat" (#790), which gets a fresh one.
- `sessions_fork`, the MCP tool.
- The panel's Sessions list (`AssistantChat.tsx`): a flat list showing title, origin,
  owner and status, with a model filter (#931, #1309). It is fed by the chat socket's
  `sessions.snapshot`.
- The panel calls fork only from the budget-exhausted card.

Missing: `up_to`, an audit row for a fork, rename, mark done, nesting forks under their
parent, spend and last activity in the list, a Fork button, "Fork from here", and the
"Forked from" link.

## Decisions

1. **`up_to` names an assistant message: the panel's `messageId`
   (`<API message id>:<block index>`).** The fork keeps the conversation through that
   reply.
   - *Why:* it is the one panel id that maps exactly onto the SDK transcript. Claude
     Code writes each content block as an entry carrying the API `message.id`, and
     `forkSession({upToMessageId})` slices by that entry's `uuid`.
   - *Turn ids won't work:* a turn id (`user.turn`) is ScadBuddy's own and appears
     nowhere in the transcript.
   - *Timestamps won't work:* a fork's copied history shares one insert time, so matching
     by time breaks for a fork of a fork.
   - *How it resolves:* the manager loads the parent's main transcript and takes the entry
     for that block, falling back to the message's last text entry. The panel's events are
     copied up to and including that message's `assistant.text.done`.
   - *Errors:* an id that is in neither is a 400 `invalid` error.
2. **"Fork from here" on a user message forks up to the assistant reply before it, and
   puts that message's text in the composer.** That is "try this question another way".
   - *Why:* a cut after the question but before its answer would leave a dangling user
     turn. A cut after its answer keeps the very turn the user wants to redo.
   - *First message:* it has no reply before it, so it gets no "Fork from here". New chat
     covers that case.
3. **No `fork` message on the chat socket.** The panel already calls the HTTP route, and
   `routes/chat.ts` is where durable sessions (#1056, phase 5c) are landing. The route's
   `{session}` answer is all the panel needs, and its socket then attaches to the child.
4. **A fork is audited.**
   - *Success:* the manager writes a `resource` row (action `session_fork`) for the child,
     whose detail names the parent and the `up_to`. This mirrors `raiseBudget`.
   - *Refused or failed forks over HTTP:* app.ts's failures-only `auditWrites` mount
     records them, as it already does for budget raises.
   - *Over MCP:* a fork by the tool is also a `tool_call` row, as every tool call is.
5. **"No transcript yet" is 409, not 400.**
   - *Why:* it is a state of the session, not a bad request (#793), so it gets its own
     error code, `no_transcript`.
   - *Busy parent:* forking while the parent runs a turn is allowed. It reads what is
     stored.
6. **Rename and mark done are `PATCH /api/v1/ai/sessions/:id {title?, done?: true}`.**
   They live in their own module (`sessions/edits.ts`), so `manager.ts` changes stay
   small while #1056 lands.
   - **Who:** owner-only, like a budget raise and a send. Another principal's session is
     taken over first, as the panel already offers. Over HTTP the actor is the browser
     user.
   - **Done** is the existing terminal status. A send to a done session is refused
     (`closed`) and fork stays allowed, so "continue" means fork. There is no reopen: an
     archive (#1885) is the way to put a chat away and bring it back.
   - **Running turn:** marking a session done while it runs a turn is refused (409): Stop
     it first.
   - **Events:** a done session gets a `session.status` event, so every open panel shows
     it.
   - **Rename:** it shows through the snapshot, which the socket re-reads. No new event
     type is needed.
   - **Durable sessions:** `done` is refused on a durable one (`ai_sessions.mode`), because
     ending its workflow belongs to #1056. Rename is allowed on any session.
7. **The snapshot carries `parentId`, `updatedAt`, `costUsd` and `budgetUsd`.** That is
   four fields in `snapshot()`, optional in the panel's schema, so an older agent still
   parses. The switcher nests forks under their parent and shows spend and last activity
   from them, with no second request.
8. **Nesting is one level deep, shown by indentation.**
   - A fork's row sits under its parent when the parent is listed. Otherwise it stands on
     its own with a "fork" label.
   - A fork of a fork nests under its own parent, and is indented the same.
   - *Why:* the panel is 380 px wide, so deeper indents cost the title.
9. **The Fork button is in the session header.**
   - *What it does:* forks the whole chat and opens the fork.
   - *When it is disabled:* while the session has no reply yet, with the reason as a
     tooltip. Forking a session the user does not own is allowed, and the fork is the
     user's.
   - *Budget:* every panel fork, the header's and "Fork from here" included, goes
     through the route, so it gets a budget of its own, as "Continue in a new chat" does.
     *Why:* only the user may grant budget (#823), and the route is the user's, refused
     to the headless browser's marked requests. An agent's `sessions_fork` still shares
     the parent's budget.
10. **The "Forked from" link.** A fork's header shows "Forked from <parent title>",
    opening the parent through the same `select` the switcher uses.

## Left alone

- #1284 (handoff offers in the UI) belongs with #300's hand-off.
- #1885 (archive) is its own change: a column, a filter and `sessions_list`.
- Fork of a durable session is left to #1056: the fork reads `ai_session_entries`.
