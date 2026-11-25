# A second models repository, with meshes in Git LFS — design

Date: 2026-11-24. Status: approved by the owner (2026-11-24). Replaces #7319.
Issues: #7313, #7314.

## 1. Intent

The off-box push of the models history (#4974) has never worked. GitHub refuses a file
over 100 MB ([docs](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github#file-size-limits)).
The history holds one (136.6 MB, commit `bb466fe`). So no off-box git copy of the models
exists. The owner's decision: build a second repository correctly, move the templates to
it, then delete the old one.

Measured on prod, 2026-11-24:

- `git rev-list --count HEAD` in `/data/models` is 1372. Settings shows the same number
  as behind. The remote has no `models` branch.
- 159 templates, 23 of them with meshes. All mesh blobs in history: 105, 565 MB.
- The ids are in 25 database columns, 857 of 915 outputs' `meta.json`, and 29 `model.json`
  files (every text and JSON column searched on the read replica for the 1372 ids).
  That is why the history is not rewritten in place.

## 2. Goals, non-goals, anti-goals

Goals

- The off-box copy is complete: a clone plus `git lfs pull` gives every byte.
- Every template keeps its slug and its files, byte for byte.
- Every version of every template, deleted ones included, has a new id. (Changed on
  2026-11-25: the first design kept the pinned and the current versions only. On prod 638 of
  916 outputs had another version, and all versions add one mesh to the copy.)
- Every revision an order item, an order revision or a listing pins still resolves.

Non-goals

- Moving the catalogue to Postgres and the blob store.

Anti-goals

- The old repository deleted without the owner's yes at that step.
- A mesh-less tree rendered or cached (the hazard found in #7319).
- The remote's credential sent to another host.

## 3. Approaches

1. **Second repository with Git LFS, then delete the old one.** Chosen by the owner.
2. Pointers of our own plus the blob store (#7319). The off-box copy holds no mesh bytes,
   and old replicas can cache a revision without its meshes.
3. A filtered copy for GitHub. Rejected by the owner: other commit ids, no mesh bytes.
4. Rewrite in place. Too many references (§1).
5. A remote with no file size limit. No such host exists today.

## 4. Why LFS fits

Measured 2026-11-24 with git 2.43.0 and git-lfs 3.4.1, with ScadBuddy's isolated
configuration (`GIT_CONFIG_GLOBAL=/dev/null`, hooks off) and the filter passed with `-c`:

- A 136.6 MB mesh is a 134 byte blob. `git archive` and `git cat-file --filters` give the
  real bytes. `git checkout <rev> -- <path>` does too (3 MB file).
- `git show` and `git cat-file blob` give the pointer. So `GitRepo.show` (which
  `upstream.plan_merge` reads) and `ModelHistory.read_blob` must read a mesh with
  `git cat-file --filters <rev>:<path>`.
- `git push --no-verify` sends no LFS object. `git lfs push <url> <commit>` must run first.
  After both, a clone gives the real bytes (2 MB file, `file://` remote).
- A `.lfsconfig` in the repository redirects the endpoint. `-c lfs.url=` overrides it.
- With `filter.lfs.required=true` and a filter that cannot run, `git archive` fails with an
  error. So every git call passes both: git ignores an attribute whose filter is not
  configured at all.

The app image is Debian 13; `apt-cache policy git-lfs` there gives 3.6.1. The API pods reach
`lfs.github.com` and `github-cloud.s3.amazonaws.com`. GitHub takes
[2 GB per LFS file on Free](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage)
and includes [10 GiB of storage](https://docs.github.com/en/billing/concepts/product-billing/git-lfs).

Not measured: GitHub as the LFS remote, and LFS over the NFS share. The plan's step 4
measures both before the switch.

## 5. Design

**LFS support.** `git-lfs` in the app image. `ModelHistory` passes the LFS filter on every
git call. It writes `.gitattributes` (`*/meshes/*.3mf` and `_builtin/*/meshes/*.3mf`) only
when it makes a repository, and never in one that has a commit. So the old repository stays
as it is. A push of a repository that uses LFS runs `git lfs push` first, with `lfs.url`
derived from the remote's own URL.

**Builder.** A command in the API's image. It makes `data/models-next`. It is idempotent
and reads the old repository only. It commits every version of each template and built-in,
in the order of the old history, with the original author and date. An upstream is older
than the `model.json` that names it, so its new id is known when that file is written. A
delete is a commit too. A revision that the old HEAD does not reach is kept where an order,
a listing, an output or a `model.json`'s upstream names it. Each file keeps its time, so a
template's "Updated" stays. Each
commit has an `Imported-From: <old id>` trailer, so git itself holds the id
map. It also copies ignored files that are data (videos in `media/`), and not `.renders/`
or wrappers. It points each `model.json` upstream at the new id. An id that the new history
does not have stays as it was.

**Check.** Every file's sha256, in every version, old against new. Every mesh model
renders. Push to a branch of the remote, clone, pull, compare again. (As built: the
command's `push` sends it to a `models-trial` branch, with the app's own push.)

**Switch.** (Changed on 2026-11-25, from a trace of main `4911d1da0`; read from code, not
run.) Only the `scadbuddy` Deployment mounts `data/models`. Argo CD has `selfHeal` on, so
its automated sync is off first. All API replicas are at 0, and nothing is running: render
jobs, operations, slice prices, template pipelines, listing preparations, backfills. Then:

1. Run the builder and the check again. Dump the rows, and copy each `meta.json`, that the
   next steps change.
2. Rename `models` to `models-old` and `models-next` to `models`. Check that the new HEAD is
   the one that was built and that every slug is there. No API may start while `data/models`
   is missing or empty: its sweep would delete the outputs and presets (#7649).
3. Set `model_version` in the `meta.json` of each output whose id is in the map.
4. One transaction: `commit` and `render_key` in `order_items` and `order_item_revisions`
   (`render_key` is a hash of the commit: by the rule of `render_key_of`); `commit` in
   `storefront_listings`, and not `updated_at`, which drops its prices; `model_version` in
   `outputs.meta`; `scope_key` and `body` of the `template_version` rows of
   `analyzer_decisions`; `models_repo_head`; no row in `templates` and `template_copy_runs`,
   which the start copy makes again; and no `models` row in `git_remote_pushes`.
5. Start one replica and check it, then the rest, then Argo CD.

A published listing keeps its snapshot blob and its hold. The SQL runs only in the API's
image with the API's credential (root `CLAUDE.md`, "Database access"). Each step can run
again. A crash before step 4 leaves the rows as they were: go on, or rename back and put
the `meta.json` files back. A crash in step 4 leaves no change in the rows. After it, a roll
back is: stop, rename back, put the rows and the files back. What was saved since the switch
names a new id, so a roll back is for the first minutes only.

**After.** An output of a revision that is gone must open with a clear message, and not
a 500. `models-old` stays until the owner says to delete it.

## 6. Left to the builder

The command's name, the order of the checks, and the module for the LFS code.
