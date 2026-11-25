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
- Every revision an order item, an order revision or a listing pins still resolves.

Non-goals

- Old revisions that nothing pins. The owner accepts their loss.
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
and reads the old repository only. It commits each pinned (slug, commit) and the current
version of every template and built-in, in the order of the old history, with the original
author and date. (As built: one order for all of them, not the pins first. An upstream is
older than the `model.json` that names it, so its new id is known when that file is
written.) Each commit has an `Imported-From: <old id>` trailer, so git itself holds the id
map. It also copies ignored files that are data (videos in `media/`), and not `.renders/`
or wrappers. It points each `model.json` upstream at the new id. An id that the new history
does not have stays as it was.

**Check.** Every file's sha256, current and pinned, old against new. Every mesh model
renders. Push to a branch of the remote, clone, pull, compare again.

**Switch.** All API replicas at 0. Run the builder again for the last changes. The
transaction: set `models_repo_head`, delete the `models` row of `git_remote_pushes`, and
set `commit` in `order_items`, `order_item_revisions` and `storefront_listings`. A published
listing keeps its stored snapshot blob and its hold. Rename `models` to `models-old` and
`models-next` to `models`. Start. The SQL runs only in the API's image with the API's
credential (root `CLAUDE.md`, "Database access"). The order is: dump the rows, then the
renames, then the transaction, then start. A crash after the renames leaves the rows as
they were: run the transaction again, or rename back. A crash in the transaction leaves
no change in the rows. So the transaction is the point of no return, and after it a roll
back is: stop, rename back, restore the rows from the dump.

**After.** An output of a revision that is gone must open with a clear message, and not
a 500. `models-old` stays until the owner says to delete it.

## 6. Left to the builder

The command's name, the order of the checks, and the module for the LFS code.
