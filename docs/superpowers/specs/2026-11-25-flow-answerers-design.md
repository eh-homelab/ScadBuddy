# Who may answer a flow's question (#7471)

The owner, 2026-11-24: `wait_for_human` is for people only by default, and the assistant can
be given a grant. "permissions like that should always be configurable wrt who can call what".

The scope is answers: `wait_for_human` and `prompt`
([flows](../../features/flows.md)). Approvals of a call and of a grant come later under the
same policy (#7647). The owner did not answer the scope question; the session chose it and
recorded that on #7471.

The facts below were read in the code at `4911d1da0` (2026-11-25). Nothing was run for them.

## 1. Goals

- G1. A person may always answer. Nobody else may, until a person allows it.
- G2. A person sets who else may answer, for every run, in Settings.
- G3. A person allows it for one run (or a trigger's runs) in the run grant.
- G4. A script may keep a question for people. No setting or grant overrides that.
- G5. The record says who answered.

## 2. Non-goals

- Approvals of a call or a grant (#7647).
- A notice to a session that a question waits. A session reads it with `get_flow_run`, which
  shows the question today (`agent/src/tools/flows.ts`).
- A service as an answerer. No service has a reason to answer now.

## 3. Facts that shape the design

1. **Tool access is not this policy.** The default of a ScadBuddy tool is its declared tier
   (`agent/src/access/resolve.ts`) and only the agent knows it. An `outward` approval may
   come from an agent token that holds `approval_grant` (`agent/src/approvals/service.ts`).
   So it is not "a person approves each answer", and `ai_access` has no caller.
2. **A run grant goes the other way.** Its rules admit calls that the run makes
   (`flows/grants.py` `match_grant`). An answer is a call into the run from outside.
3. **The browser form cannot open to the assistant.** A click by the paired agent sends the
   person's cookie and no author header, so the API records the person. The forms stay
   `USER_ONLY`.
4. **Author headers are labels** (`core/authorship.py`). The `agent` service credential is
   not: `AuthGate` resolves it in every auth mode, and a route can ask `caller_of`. Prod
   runs `observe` (measured 2026-11-25, `kubectl -n bambuddy get deploy scadbuddy`).
5. **A request with no author header passes as a person outside `enforce`**
   (`by_a_person`). That was so before this design, and stays so. The policy governs
   ScadBuddy's agents that name themselves; it is not a boundary against a client on the
   LAN that does not.
6. **A run can call tools.** A flow's `tool()` reaches the API as the run's starter with the
   flow-run header and no session. Without a rule, a run could answer a question itself.

## 4. Design

### 4.1 The answerers

| Answerer | How the API knows it | Recorded as |
|---|---|---|
| A person | `by_a_person()`, as today | `browser` (the task also records the principal) |
| An assistant session | the `agent` credential, and an author with a session that §4.1.1 admits | `session:<id>` |
| An MCP token | the `agent` credential, and an author `token:<id>` with no session; the token is not revoked or expired and its tier has `write` | `token:<id>` |

Any other caller is refused, as today: a projects credential, a person's access token, the
agent's headless browser, and an author without the `agent` credential.

#### 4.1.1 A session that may answer

One store method reads the session's row. All of these must hold:

- it is not archived, and its owner is the author's principal now;
- its owner holds `write` now;
- it is not a flow's: `background_tools` is true and `turn_flow` is false
  (`ai_sessions`). Those columns mark the sessions that a flow started, their forks and the
  sessions started from them, and a flow's turn in another session. `nfc_write` uses the same
  rule (`agent/src/tools/nfc.ts`).

A step's `session_id` is not that mark. `ask_session` sets it for a session that the run did
not start, and a Reset drops it.

A call from a flow run (the flow-run header) is refused before anything else. A run that
could answer its own questions would ask nobody.

### 4.2 The setting

`flow_answerers`, a list, default empty: people only. Its values:

| Value | Who |
|---|---|
| `starter` | the session that started the run: the run's `started_by` is an agent's, with a session and principal equal to the author's. A run from the browser, a trigger, a token or another run has no such session, so nobody matches |
| `sessions` | any session that a browser user owns |
| `token:<id>` | that MCP token, and the sessions that it owns |

A session that a token owns needs `token:<id>` or, for its own runs, `starter`.

Only a person changes the setting, a reset of it included (403 `person_only`, as the mail
connection's settings). So no agent changes its own access. The routes read it on each
request.

### 4.3 The run grant

`GrantSpec.answers` is optional. It has `who`, with the values of §4.2. It is a field of the
spec and not a `GrantRule`: rules admit the run's calls, and an `agent(grant=)` narrowing
holds rules.

A person approves it with the grant, as today. An agent may propose it and cannot approve it.
A child run does not inherit it, and a trigger's standing grant may not hold it: each would
open runs that the person did not look at.

An answer is allowed when the setting or the grant allows it. For the grant, the transaction
that records the decision also checks that the grant and its chain are in force.

### 4.4 The script

`wait_for_human(question, timeout_s=None, people_only=False)`, and the same keyword on
`prompt`. It is `FlowWaiting.people_only: bool | None = None`. Only an explicit `False` lets an
agent answer:

- A question that parked before the upgrade has `None`, so it is for people.
- An old API that writes the entry again drops the key, so it is for people.

A question asked later by the same run, on a new worker, has `False`.

The route refuses an agent's answer to a question for people. The operation's check asks
again, and asks §4.1.1 again over a CRUD route. The setting itself is read at the route only:
an answer sent just before a person empties the list still counts.

### 4.5 The tool

`answer_flow_question(run_id, call_id, answer)` in `builtin:scadbuddy`, tier `outward`. The
`answer` is a text for `wait_for_human` and an object for `prompt`. It calls the same two
routes, so the schema check, the decision row and the first-answer-wins rule are theirs. A
flow run cannot call it (`NotForFlows`).

Two gates apply, and both must pass: the policy of §4.1 to §4.4 at the API, and the tool's
access in the agent. With the tool at `outward`, each answer also waits for an approval. The
user sets it to `write` so that the assistant answers alone, or `off`.

The two routes leave `PERSON_ENDPOINTS` (`auth/routes.py`) and are `api` routes, because
`enforce` refuses a service on a person route. They ask the policy in every mode, before the
operation starts. A repeat of a request gets its first answer, also after the question is
gone.

### 4.6 The record

- `workflow_run_decisions.responder` is the answerer (§4.1). `grant:` keeps its meaning (an
  approval that a grant let through) and is never an answer's.
- An agent's answer is an operation of a kind of its own. A worker from before this change
  does not know the kind, so it cannot record the answer as a person's. The operation waits
  for a new worker.
- A person's Update id and operation key do not change. An agent's hold the responder too. So
  a person's answer that is in flight during the rollout keeps its identity.
- The comparisons of an answer's row include the responder. A person who gives the answer that
  an agent gave is told that it was answered already.
- Take-over of an answer that never landed (#7597): a person may take over any answer, an
  agent only its own. The row then has the new responder, and putting the old answer back
  restores the old one.
- A Reset undoes answers, as today. An agent that may reset a run and may answer can replace
  an answer that a person gave before the Reset point. The decisions of the replaced execution
  stay on record. `people_only` is the way to prevent it.
- The decision says which of the two admitted an agent (`setting`, or the grant's id).
- A task's `answered_by` is the same text for an agent. For a person it stays the principal.
- The run's reads give each answered question its `answered_by`, from the decisions of kind
  `answer` by call id. The run page and the task card name the assistant, link its session,
  and say when the grant admitted it.

## 5. What changes for whom

| | Before | After, with the defaults |
|---|---|---|
| A person | answers | answers, and is never refused by the policy |
| An assistant | 403 | 403, with a problem type that names the setting |
| A run | 403 | 403, whatever the setting |

## 6. Delivery

1. **B1, the setting.** Policy, setting and its UI, the routes, the records, the tool,
   `people_only`. B1 also teaches `GrantSpec` the `answers` field: it is left out of every
   write when it is empty, and any value is refused. Grant models refuse unknown fields, so
   without this a worker from B1 could not read a grant from B2.
2. **B2, the grant.** `GrantSpec.answers` is accepted, with its editor. It goes out when every
   worker has B1.

A script that passes `people_only` to a worker from before B1 fails with a `TypeError`. The
worker rollout is short, and a script cannot use the keyword before its author knows of it.

## 7. Left out

- A limit on the number of answers and on the places of a grant, "any MCP token", and answers
  in a trigger's standing grant. Each is an addition to one list or model when a use needs it.

## 8. Tests that must exist

- With the defaults, every non-person caller gets a 403 on both routes, in `off`, `observe`
  and `enforce`.
- Each value of the setting admits its answerer and no other. A forged author without the
  `agent` credential is refused with every value set.
- With every value set, these are refused: a run's own call, a session a flow started, its
  fork, a session started from it, a flow's turn in another session, an archived session, a
  session handed to another owner, a revoked or expired token.
- `starter` matches nobody for a run from the browser, a trigger or a token.
- `people_only` wins over the setting and the grant. A question from before the upgrade is
  for people.
- An agent cannot change or reset the setting, and cannot approve a grant that holds
  `answers`. A child run's grant has none.
- The answer of an agent is recorded as its own, before and after a take-over. An agent never
  takes over another answerer's answer.
- A person's Update id and operation key are the same bytes as before.
- A revoked grant admits none, also between the route and the decision.
- The stored histories replay.
