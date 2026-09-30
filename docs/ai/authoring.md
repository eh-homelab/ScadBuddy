# Agent model authoring

How an agent creates, edits and iterates on a model's OpenSCAD source: the tools, the
conflict rule, who a commit is authored by, and how to back out of a dead end. Issue
[#252](https://github.com/eh-homelab/ScadBuddy/issues/252), part of epic
[#249](https://github.com/eh-homelab/ScadBuddy/issues/249); spec §5.1 and §8.1 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
The conventions a template follows (customizer comments, colours, fonts) are the
plugin's [`authoring` skill](../../plugins/scadbuddy/skills/authoring/SKILL.md).

## 1. The loop

| Step | Tool | Tier | Route |
|---|---|---|---|
| Start | `create_model`, `duplicate_model` | write | `POST /api/v1/models`, `POST /api/v1/models/{slug}/duplicate` |
| Read | `get_model` (its `version`), `get_source` | read | `GET /api/v1/models/{slug}`, `GET /api/v1/models/{slug}/source` |
| Mark | `checkpoint` | read | `GET /api/v1/models/{slug}` |
| Check | `check_source` | read | `POST /api/v1/models/check` |
| Edit | `apply_patch`, or `update_source` for a rewrite | write | `POST /api/v1/models/{slug}/source/patch`, `PUT /api/v1/models/{slug}/source` |
| Render | `render_model`, `get_render_diagnostics` | write, read | `POST /api/v1/models/{slug}/render`, `GET /api/v1/models/{slug}/diagnostics` |
| Look | `get_render_view` (7 views), `analyze_geometry` | read | `GET /api/v1/jobs/{job_id}/views/{view}.png`, `GET /api/v1/outputs/{output_id}/geometry` |
| Back out | `restore_version` | write | `POST /api/v1/models/{slug}/versions/{commit}/restore` |

Every edit, restore and create is one commit in the model's history
(`backend/scadbuddy/library/history.py`), which is why they are `write` and not
`outward`: `write` is "reversible through history" (spec §8.1). The tools are in
[`agent/src/tools/catalogue.ts`](../../agent/src/tools/catalogue.ts),
[`history.ts`](../../agent/src/tools/history.ts) and
[`authoring.ts`](../../agent/src/tools/authoring.ts).

## 2. `apply_patch` and the conflict rule

`apply_patch` sends a change instead of the whole file: a unified diff (`patch`, as
`diff -u` or `git diff` writes it, [GNU diffutils, "Detailed Unified"](https://www.gnu.org/software/diffutils/manual/html_node/Detailed-Unified.html))
or search/replace `edits`. The backend applies it to the source as it stands
(`backend/scadbuddy/library/patch.py`):

- File headers are skipped; a diff naming a second file is refused. After the first
  hunk a second file starts only at a `diff` line (`git diff`, `diff -ru`): a `---` then
  `+++` pair there is a removed and an added line, even right before the next `@@`. A
  second file pasted on with no `diff` line fails as a hunk that does not apply, and the
  error says it may be a second file.
- A hunk is tried at the line its header names, then at the one other place below the
  previous hunk where its old lines occur. Two such places is ambiguous and refused.
  There is no fuzz: a context line that differs is a conflict. At most 100 hunks
  (`MAX_HUNKS`), as at most 100 edits, and the patch is applied off the event loop.
- `\ No newline at end of file` applies to the line before it, on that line's side
  ([GNU diffutils, "Incomplete Lines"](https://www.gnu.org/software/diffutils/manual/html_node/Incomplete-Lines.html)),
  so a diff can add or drop the file's trailing newline. The hunk carrying it must reach
  the end of the file, and an old-side marker against a source that ends in a newline
  is a conflict. A diff with no marker keeps the source's own ending.
- Each search must occur exactly once in the source the earlier edits left.
- Either the whole patch applies or nothing is written, and a refusal (422) names the
  hunk or edit.

It takes `base`, the revision the agent read the source at (`get_model`'s `version`,
or `checkpoint`'s). When the model has moved on since, the backend answers **409** with
the problem-details extension members `base` and `current`
([RFC 9457 §3.2](https://www.rfc-editor.org/rfc/rfc9457#section-3.2)), and writes
nothing. The check is made twice: once before the parse check, cheaply, and again under
the history's write lock (`Catalogue.write_source(expected_version=...)`), so no other
write can land between the check and the commit. The tool turns the 409 into an error
result `{"status": "conflict", "base", "current", "next"}`: read the source again,
rebuild the patch, and pass `current` as `base`.

`update_source` takes the same optional `base` (`PUT /api/v1/models/{slug}/source`
`base`), with the same 409.

## 3. Commits are authored as the agent

Every backend call a tool makes carries `X-ScadBuddy-Agent-Author` (the principal the
tool runs as: `token:<id>`, `oidc:<issuer>#<sub>`, `anonymous:<mcp session>`, or
`browser`) and, in a harness session, `X-ScadBuddy-Agent-Author-Session` (the session
id). `runToolWithOutcome` adds them to the tool's backend client
([`agent/src/tools/authorship.ts`](../../agent/src/tools/authorship.ts),
[`registry.ts`](../../agent/src/tools/registry.ts)); the harness projection passes the
session id (`createHarnessServer`, `harnessTools`).

The backend's `AgentAuthorship` middleware
([`backend/scadbuddy/core/authorship.py`](../../backend/scadbuddy/core/authorship.py))
reads them into a context variable, and `ModelHistory._commit_locked` authors any commit
made while it is set as **ScadBuddy agent** `<agent@scadbuddy.localhost>` (committed by
ScadBuddy as always), with the principal and session as git trailers
([git-interpret-trailers](https://git-scm.com/docs/git-interpret-trailers)):

```
Edit keychain source

ScadBuddy-Agent-Principal: browser
ScadBuddy-Agent-Session: 6f1c…
```

`list_versions` (`GET /api/v1/models/{slug}/versions`) reports them as
`agent: {principal, session}` beside `author`. A request from the headless browser
(#349) carries the agent-actor marker, whose value is the session id, so its commits
are the agent's too. The commit's subject is the tool's `message`; the `authoring`
skill asks the agent to put the user's instruction there (#252: "carrying the user's
instruction as the message").

The headers are labels, not authentication: the backend trusts its callers (spec
§4.3), and a forged header can only mislabel a revision. Each value must be printable
ASCII with no spaces (the principal at most 300 characters, the session
`[A-Za-z0-9_-]{1,64}`), so a trailer is one line; anything else is a 400. The tools'
header is deliberately not the agent-actor marker, which would make the backend refuse
the outward routes a human has already approved.

## 4. `checkpoint`

`checkpoint({slug, label?})` answers the model's current revision and the
`restore_version` call that returns to it. Nothing is stored: every later edit is a
revision of its own, so restoring the checkpoint abandons all of them at once, and the
history still holds them (a restore is a new commit, never a rewrite,
`ModelHistory.restore`). It is `read`, since it changes nothing.

## 5. Not built yet

Tracked on #252:

- multi-file models (reading and writing files beside `model.scad`);
- creating from a bundled example or a blank template in one step (today:
  `duplicate_model` of a `builtin:` template, or `create_model` with the source);
- LSP diagnostics as data, a per-colour breakdown image, and the
  `scadbuddy://docs/authoring` resource;
- a rate limit on renders the agent triggers, beyond the backend's render timeout and
  queue.
