# Plan: a second models repository, with meshes in Git LFS

Spec: `docs/superpowers/specs/2026-11-24-models-repository-lfs-design.md`. One PR per step,
test first.

1. **LFS support.** Image: `git-lfs`. `GitRepo`/`ModelHistory`: the filter on every call,
   `.gitattributes` on a fresh repository only, `show` and `read_blob` of a mesh with `cat-file --filters`, `git lfs
   push` before `push` with `lfs.url` pinned. Tests: a mesh commit's blob is a pointer;
   export, restore, duplicate and an upstream merge give real bytes; an existing repository
   gets no attributes; a push to a `file://` remote clones complete; a `.lfsconfig` does not
   move the endpoint; the credential is in no argv. Deploy before step 2.
2. **Missing revisions.** An output, order or link whose revision is gone answers a clear
   4xx, and the UI says so. Tests per route.
3. **Builder and check command.** Tests: pinned revisions, a pin of a deleted template, a
   built-in, a duplicate's upstream, videos, a second run, the old repository untouched.
4. **Dry run on prod.** Build `models-next`, check, push to a trial branch, clone, compare.
   Report. Nothing serves it.
5. **Switch command and runbook.** Tests: the rows, one transaction, a published listing's
   hold, roll back. Run it in a window the owner agrees to.
6. **Delete `models-old`.** Only on the owner's yes.
