# Templates and forks

Status: draft, 2026-09-26

## 1. The problem

Bundled models are *seeded*: `Catalogue.seed` copies each one into `data/models/`
once, if its slug is absent, and from then on it is an ordinary user model. So:

- a user edit to a seeded model *is* an edit to "the template" — there is no
  pristine original to start another copy from;
- a newer image never reaches an existing install (copy-if-absent), so template
  fixes are stranded;
- there is no way to say "this model of mine started as that one".

## 2. The model

Two tiers:

- **Templates** — read-only. The image's bundled models (`/app/models`), mirrored
  into the data repository on every boot. Later, other sources (a git URL) can
  feed the same tier.
- **Models** — the user's own, as today. A model may be a **fork** of a template:
  a copy that records where it came from and can take the template's later
  changes.

Customizing a template's *parameters* is not a fork. Rendering a template
directly already produces an output stamped with `(slug, commit, params)`; a fork
is only needed to change the `.scad` source.

## 3. Why not a git branch per fork

Git is the right store for fork lineage, but a branch is the wrong unit:

- a repository has one working tree, and the server renders many models at once,
  so every fork would need its own `git worktree` or an export per render;
- listing the catalogue would mean walking branches instead of directories;
- the only thing a branch buys here is a known **merge base**, and that is one
  commit id we can record ourselves.

So a fork is a directory, like every other model, plus a pointer to the template
commit it was taken from. The three-way merge then works exactly as it would
between branches.

## 4. Storage

One repository, as today (§4.3 of the main spec). Templates live in a subtree the
catalogue cannot mistake for a model — slugs are `[a-z0-9-]`, so `_templates`
cannot collide, and `list_models` only takes directories with a `model.scad` at
their top:

```
data/models/                          the git repository
data/models/<slug>/                   user models, forks included (unchanged)
data/models/_templates/<slug>/        templates, mirrored from the image
```

A fork's `model.json` gains:

```json
"forked_from": {
  "template": "name-keychain",
  "base": "<commit>",          // the template revision this fork currently includes
  "path": "_templates/name-keychain",
  "dismissed": "<commit>|null" // a template revision the user chose not to take
}
```

`path` is stored rather than derived so migrated models (§8) can point their
base at a commit where the source lived somewhere else.

## 5. Template sync (replaces `seed`)

On boot, after `ensure_repo`:

1. Mirror every bundled model into `_templates/<slug>/`, overwriting — the image
   is the source of truth for a template. Templates no longer in the image are
   removed from the mirror.
2. If anything changed, commit once: `Sync templates from the image`.

Templates are never written any other way; the API has no write route for them.
Git history of `_templates/` is therefore the template's version history, and
old bases stay resolvable forever.

A template removed from the image leaves its forks working; they just have no
upstream (`GET` reports `upstream: "gone"`).

## 6. Forking

`POST /templates/{slug}/fork` with `{name}` (slug derived as for create):

- copy `_templates/<slug>/` to `<new-slug>/`, set `forked_from` with
  `base = last_commit("_templates/<slug>")`;
- one commit: `Fork <new-slug> from template <slug>`.

UI: the library shows templates and my models as two sections. Customizing a
template renders it directly. Opening its source editor offers **Make my copy**
instead of an editable buffer — that is the fork.

## 7. Taking template updates

A fork has an **update available** when the template's current revision differs
from both `base` and `dismissed`. Listing computes this in the same single
history walk as `last_commits()`, extended to key `_templates/<slug>/…` paths by
their second component (today it keys by the first, which would fold every
template into one `_templates` entry).

`GET /models/{slug}/upstream` returns the state and, when an update exists, a
merge preview:

- `ours` — the fork's `model.scad`
- `base` — `git show <base>:<path>/model.scad`
- `theirs` — the template's current `model.scad`
- result of `git merge-file -p --diff3 ours base theirs`: clean, or with
  conflict markers

`POST /models/{slug}/upstream/merge`:

- **clean** — write the merged source, set `base` to the template's revision,
  clear `dismissed`; one commit: `Merge template <t> into <slug>`.
- **conflicted** — refuse with 409 and the marked-up source. The UI opens it in
  the existing Monaco editor; saving via `PUT /models/{slug}/source` with
  `?merge_base=<commit>` writes the resolution *and* advances `base` in the same
  commit. The source is parse-checked as on any edit, so a leftover marker cannot
  be saved.

`POST /models/{slug}/upstream/dismiss` sets `dismissed` to the template's current
revision; the badge returns only when the template moves again.

Only `model.scad` is merged. Other files in the template (included `.scad`,
README, thumbnail) are taken from the template when the fork has not changed
them since `base`, and otherwise kept as the fork's with the conflict listed.
`model.json` metadata is always the fork's. The thumbnail regenerates on the next
render as today.

## 8. Migration of existing installs

Existing seeded models are user models whose history begins at a
`Seed … from the image` commit. On the first boot with this feature:

- the template sync (§5) runs, creating `_templates/`;
- every model whose slug matches a template and has no `forked_from` becomes a
  fork of it, with `base` = its own seed commit and `path` = `<slug>`. Its
  original seeded source is the true merge base, so an unedited model merges
  cleanly to the new template and an edited one keeps its edits;
- one commit: `Link seeded models to their templates`.

Slugs, outputs, `model_version` stamps and "Edit in ScadBuddy" links are
unchanged: the user's model keeps its slug, and templates are addressed under
`/templates/{slug}`, a separate namespace.

## 9. Out of scope

- Templates from external git repositories (a later source for the same tier;
  that one *may* be a clone plus branch, behind the same `forked_from` field).
- Publishing a user model as a template.
- Forking a fork (a fork's upstream is always a template).
