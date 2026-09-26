# Templates and duplicates

Status: draft, 2026-09-26

## 1. The problem

Bundled models are *seeded*: `Catalogue.seed` copies each one into `data/models/`
once, if its slug is absent, and from then on it is an ordinary user model. So:

- a user edit to a seeded model *is* an edit to the original — there is no
  pristine copy to start another one from;
- a newer image never reaches an existing install (copy-if-absent), so fixes to
  bundled models are stranded;
- there is no way to say "this model started as that one", nor to take that
  one's later improvements.

## 2. The model

Everything in the library is a **template** — something you customize, print, or
duplicate. A template has one of two origins:

| Origin | Comes from | Editable | Deletable | Duplicable |
|---|---|---|---|---|
| **Built-in** | the image (`/app/models`) | no | no | yes |
| **Mine** | upload, paste, or duplicate | yes | yes | yes |

**Duplicating** any template makes a new *mine* template that records its
**upstream** — the template it was copied from, and the upstream revision it
currently includes. When the upstream changes, the duplicate offers to take the
changes (§7). The upstream may be built-in or mine; a duplicate of a duplicate
tracks its immediate parent.

Customizing *parameters* never needs a duplicate: rendering any template, built-in
included, produces an output stamped with `(template, commit, params)`, as today.
A duplicate is only needed to change a built-in's source, or to branch a variant
of your own.

## 3. Why not a git branch per duplicate

Git is the right store for lineage, but a branch is the wrong unit:

- a repository has one working tree, and the server renders many templates at
  once, so every duplicate would need its own `git worktree` or an export per
  render;
- listing the library would mean walking branches instead of directories;
- the only thing a branch buys here is a known **merge base**, and that is one
  commit id we can record ourselves.

So a duplicate is a directory, like every other template, plus a pointer to the
upstream commit it includes. The three-way merge then works exactly as it would
between branches.

## 4. Storage and identity

One repository, as today (§4.3 of the main spec):

```
data/models/                     the git repository
data/models/<slug>/              mine (uploads, pastes, duplicates) — unchanged
data/models/_builtin/<slug>/     built-ins, mirrored from the image
```

`list_models` only takes directories with a `model.scad` at their top, so
`_builtin/` is never mistaken for a template of mine; slugs are `[a-z0-9-]`, so it
cannot collide either.

Derived per-template storage outside the repository — `cache/schema/`,
`cache/revisions/`, `outputs/` — names a template by ONE path component:
`<slug>` for mine (unchanged), `_builtin-<slug>` for a built-in. Those trees are
two levels deep by construction (the output lookup globs `*/<output-id>`), and a
flat key keeps them so (#155).

A template's id is `<slug>` for mine and `builtin:<slug>` for a built-in. `:` is
not a slug character, so the two namespaces cannot collide, and every existing id
(outputs, `model_version` stamps, "Edit in ScadBuddy" links) keeps meaning what it
means now. Every route that takes a slug accepts either form; write routes refuse
`builtin:` with 403.

A duplicate's `model.json` gains:

```json
"upstream": {
  "id": "builtin:name-keychain",
  "path": "_builtin/name-keychain",
  "base": "<commit>",          // the upstream revision this template currently includes
  "dismissed": "<commit>|null" // an upstream revision the user chose not to take
}
```

`path` is stored, not derived, so a merge base can name a commit where the source
lived somewhere else (the migration in §8 relies on this).

## 5. Built-in sync (replaces `seed`)

On boot, after `ensure_repo`:

1. Mirror every bundled model into `_builtin/<slug>/`, overwriting — the image is
   the source of truth for a built-in. Built-ins no longer in the image are
   removed from the mirror.
2. If anything changed, commit once: `Sync built-in templates from the image`.

Nothing else ever writes `_builtin/`, so its git history is each built-in's
version history and old bases stay resolvable for good.

## 6. Upload and duplicate

**Upload / paste** — unchanged: `POST /models` creates a template of mine with no
upstream.

**Duplicate** — `POST /models/{id}/duplicate` with `{name}` (slug derived as for
create):

- copy the template's directory to `<new-slug>/`, set `upstream` with
  `base = last_commit(<path>)`;
- one commit: `Duplicate <id> as <new-slug>`.

UI: the library shows built-in and mine together, built-ins badged. A built-in's
source editor is read-only with a **Duplicate to edit** action; mine are edited in
place as today. Every template's menu has **Duplicate**.

## 7. Taking upstream updates

A duplicate has an **update available** when its upstream's current revision
differs from both `base` and `dismissed`. The library computes this in the same
single history walk as `last_commits()`, which keys `_builtin/<slug>/…` paths by
the built-in's id, `builtin:<slug>` (#155; keying by the first component would
fold every built-in into one `_builtin` entry).

`GET /models/{slug}/upstream` returns the state and, when an update exists, a
merge preview:

- `ours` — this template's `model.scad`
- `base` — `git show <base>:<path>/model.scad`
- `theirs` — the upstream's current `model.scad`
- result of `git merge-file -p --diff3 ours base theirs`: clean, or with
  conflict markers

`POST /models/{slug}/upstream/merge`:

- **clean** — write the merged source, set `base` to the upstream's revision,
  clear `dismissed`; one commit: `Merge <upstream id> into <slug>`.
- **conflicted** — refuse with 409 and the marked-up source. The UI opens it in
  the existing Monaco editor; saving via `PUT /models/{slug}/source` with
  `?merge_base=<commit>` writes the resolution *and* advances `base` in the same
  commit. The source is parse-checked as on any edit, so a leftover marker cannot
  be saved.

`POST /models/{slug}/upstream/dismiss` sets `dismissed` to the upstream's current
revision; the badge returns only when the upstream moves again.

Only `model.scad` is merged. Other files (included `.scad`, README, thumbnail)
are taken from the upstream when this template has not changed them since
`base`, and otherwise kept with the conflict listed. `model.json` metadata is
always this template's own. The thumbnail regenerates on the next render.

**Upstream gone.** A built-in dropped from the image, or a template of mine that
is deleted, leaves its duplicates working; they report `upstream: "gone"` and
offer **Detach**, which clears `upstream`. Deleting a template of mine that has
duplicates says how many first.

## 8. Migration of existing installs

Existing seeded models are templates of mine whose history begins at a
`Seed … from the image` commit. On the first boot with this feature:

- the built-in sync (§5) runs, creating `_builtin/`;
- every template of mine whose slug matches a built-in and has no `upstream`
  becomes a duplicate of it, with `base` = its own seed commit and
  `path` = `<slug>`. The originally seeded source is the true merge base, so an
  unedited one merges cleanly to the current built-in and an edited one keeps its
  edits;
- one commit: `Link seeded templates to their built-ins`.

Nothing is renamed, so outputs, `model_version` stamps and deep links are
untouched.

## 9. Out of scope

- Built-ins from external git repositories (a later source for the same origin;
  that one *may* be a clone plus branch, behind the same `upstream` field).
- Pushing a duplicate's changes back to its upstream.
