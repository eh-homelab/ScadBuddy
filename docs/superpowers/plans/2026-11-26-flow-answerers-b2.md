# Who may answer a flow's question, B2: the run grant (#7471)

**Goal:** an approved run grant may also let an assistant session or MCP token answer the
run's questions (`GrantSpec.answers`), as spec §4.3 says.

**Architecture:** B1 (PR #7754) added the setting, the policy, the record and `answers` in
`GrantSpec` (refused). B2 accepts `answers`, lets `admit` pass an answer when the setting or
the run's grant admits it, records the grant's id in `admitted_by`, and checks again in the
decision's transaction that the grant chain is in force. In Python: the owner's choice of
2026-11-26, because the flows area has no Go copy yet. This branch stacks on B1's.

**Spec:** `docs/superpowers/specs/2026-11-25-flow-answerers-design.md` (§4.3, §4.6, §6, §8).

## Global constraints

- An answer is allowed when the setting **or** the grant allows it. The setting is checked
  first, so `admitted_by` is `setting` when both allow it.
- Only a person approves a grant that holds `answers`. An agent may propose it.
- A child run's grant drops `answers`, and a trigger's standing grant refuses it (both done
  in B1).
- `people_only` wins over the grant.
- A revoked or lapsed grant admits nobody, also between the route and the decision.
- Every Python route this changes has no Go copy (`tools/go/core/README.md`).
- B2 ships once every worker runs B1 (spec §6).

## Task 1: the policy and the record (backend)

Files: `flows/grants.py`, `flows/answerers.py`, `api/flows.py`, `api/tasks.py`,
`flows/store.py`, `api/crud_projects.py`, `crud/projects.py`, `flows/operations.py`, tests.

- `validate_spec` accepts `answers`. Its `who` holds `AnswererRule`s, as the setting does.
- `admit(found, settings, flows, run_id, call_id) -> str`: it returns what admitted the
  agent: `BY_SETTING`, or the grant's id. A grant admits only when it is `approved`, in
  force with its chain (`store.grant_in_force`), and `admitted(found, answers.who,
  started_by)` holds.
- `agent_answer(found, admitted_by)`: the request carries that `admitted_by`.
- `FlowStore.record_decision`: for an answer whose `admitted_by` is not `setting`, the same
  transaction locks the grant row (`FOR SHARE`), checks that the grant belongs to the run
  and that `answers` is still set, and runs `_chain_in_force`. If not, it raises
  `GrantLapsedError`. The CRUD route maps it to 409 `grant-lapsed`, and `RemoteFlowStore`
  raises it again. `land` turns it into 403 `flow-answer-not-allowed`.
- Tests:
  - `test_grants.py`: `answers` is valid.
  - Route tests (`test_flow_answerers_routes.py`):
    - an approved grant admits its answerer, with `admitted_by` set to the grant's id;
    - a requested, declined or revoked grant admits nobody;
    - `people_only` wins over the grant;
    - an agent cannot approve a grant that holds `answers`.
  - Store test: a grant revoked after the route admitted the answer refuses the decision.

## Task 2: the agent tool

- `agent/src/tools/flows.ts` `grantSpec`: an optional `answers: {who: [...]}` with the
  setting's values, described as proposed only.
- Test in `agent/test/flows.test.ts`: the schema takes it.

## Task 3: the screens (frontend, load frontend-design)

- Extract the setting's picker into `components/flows/AnswerersPicker.tsx` (`rules`,
  `onChange`), and use it in the settings section and in `GrantEditor`. The editor shows it
  only for a run grant (not `standing`), as "Who else may answer its questions". An empty
  list leaves `answers` out.
- `GrantPanel` says who the grant lets answer.
- `AnsweredBy` says "under the run's grant" when `admitted_by` is not `setting`.
- Tests: the editor writes `answers`; the panel shows it; the label says it.

## Task 4: docs

- `docs/features/flows.md` "Who answers": the grant.
- `docs/architecture/workflows.md`: `admitted_by` and the decision's grant check.
