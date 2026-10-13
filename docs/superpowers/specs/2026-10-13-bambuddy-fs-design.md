# bambuddy-fs: the Bambuddy library as a FUSE filesystem

Status: design, approved in conversation 2026-10-13; implementation plan to follow.
Issue: #2686.

## 1. Goal

`bambuddy-fs mount <mountpoint>` shows Bambuddy's library, as ScadBuddy sees it, as
a directory tree: folders are directories and library files are files. People
browse it, open 3MFs in Bambu Studio or OrcaSlicer, drag files in, rename and move
them, and delete them. Scripts and servers use the same mount to read and write
library files as plain paths.

The tool talks only to ScadBuddy's `/api/v1`, never to Bambuddy. Every effect is a
ScadBuddy route, with that route's semantics, its operations record, its idempotency
and its tracing. Deletes do exactly what `POST /print/library/delete` does (§5).

Out of scope: agent and flow access through a mount. A FUSE mount in the agent pod
needs `/dev/fuse` and `CAP_SYS_ADMIN`, against the agent's hardening (read-only root,
uid 10001, two `emptyDir`s). It would also bypass the approval gate on outward
actions. The agent reaches the library through its tools, and the new routes in §4
get tools of their own. Authentication is also out of scope: ScadBuddy has none and
is LAN-only (README, "LAN only").

## 2. Facts this rests on

- **Bambuddy file names are not unique.** Files are keyed by ID, and two files in
  one folder can share a filename (`LibraryFile.duplicate_of`,
  `backend/scadbuddy/bambuddy/models.py`; the listing carries size and `created_at`
  to tell them apart, #935).
- **Bambuddy 1.2.5.5 has the write surface.** From
  `backend/tests/bambuddy/recordings/openapi/routes.txt`:
  - `PUT /library/files/{id}` takes `FileUpdate {filename, folder_id, notes,
    project_id}`. Each field is applied only when not null, and `0` clears
    `folder_id` (`client.py` `annotate_library_file`, read off Bambuddy's source).
  - `POST /library/folders/` creates a folder; `PUT` and `DELETE
    /library/folders/{id}` rename and delete one.
  - `POST /library/files` uploads (multipart).
- **Deletes differ by file kind.** A managed file goes to Bambuddy's trash. An
  `is_external` file loses its record for good (#2167). A file that a print waiting
  in Bambuddy's queue names is refused with a 409 before anything is deleted
  (`api/library_print.py` `post_library_delete`).
- **ScadBuddy already serves the read side.**
  - `GET /print/library?folder_id=&all=true`: the folder tree and one folder's files,
    each with `id`, `filename`, `file_size`, `created_at` and `is_external`.
  - `GET /print/library/{id}/file`: the bytes, streamed and always
    `application/octet-stream`.
  - `POST /print/library/delete` and `/restore`.

## 3. Shape

```
bambuddy-fs mount --api http://scadbuddy.lan/api/v1 [--ro] [--all] [--allow-other] [--attr-ttl 5s] <mountpoint>
```

- Without `--all`, the mount lists what the library dialog lists by default
  (unsliced 3MFs). With it, every file.
- `--ro` makes every write `EROFS`.

The repository layout follows forge's (`WonderForgeLabs/forge`, `go.work`,
`tools/go/*`):

```
go.work                          # go + toolchain pinned; use ./tools/go/*
vendor/                          # `go work vendor`, committed
tools/go/scadbuddy-client/       # Kiota-generated, committed (§6); own go.mod and vendor/
tools/go/bambuddy-fs/            # the CLI and FUSE layer (hanwen/go-fuse/v2, fs node API)
scripts/generate-go-client.sh
.github/workflows/go-module-ci.yml
```

Inode model:
- A folder's inode is its Bambuddy folder ID; a file's inode is its file ID, in a
  separate namespace.
- Within a folder, the oldest file of a name (`created_at`, then `id`) shows as the
  plain name. Later ones show as `<stem> (id <N>)<ext>`, so a path is stable for as
  long as its file exists.
- `created_at` is used as both mtime and ctime. Size is `file_size`.

## 4. New ScadBuddy routes (backend + agent)

Each route is a new operation kind on the `library` queue, beside `library_file_delete` (`library/library_file_operations.py`), run through
`run_operation` (the `Idempotency-Key` header, 202 plus `GET /operations/{id}` past
the deadline). Each one gets an agent tool or a `coverage.ts` entry, and works in
both session modes.

| Route | Effect |
|---|---|
| `POST /print/library/files` (multipart: `folder_id`, file) | Upload into a folder. The bytes go by claim check (`ClaimStore.hold_file`, streamed), like a mesh upload (`api/mesh_models.py`), under a `RouteLimit` of `MAX_DOWNLOAD_BYTES` plus 64 KiB, since multipart is otherwise capped at 32 MiB. |
| `PATCH /print/library/{id}` `{filename?, folder_id?}` | Rename and/or move a file (`PUT /library/files/{id}`). |
| `POST /print/library/folders` `{name, parent_id?}` | mkdir. |
| `PATCH /print/library/folders/{id}` `{name?, parent_id?}` | Rename or move a folder. |
| `POST /print/library/folders/{id}/delete` | Delete a folder **with everything in it** (§5). |

The agent tools are outward (approval-gated) for the deletes, as `delete_library_files`
is today. The others take the tiers their library-write peers have.

## 5. Deletes

- `unlink` → `POST /print/library/delete {file_ids: [id]}`. Managed files go to
  Bambuddy's trash, and external files are gone for good (the route's `trashed:
  false`). A 409 maps to `EBUSY` and a 404 to `ENOENT`.
- `rmdir` → `POST /print/library/folders/{id}/delete`, **whether or not the folder is
  empty**. Bambuddy's folder delete cascades: in 1.2.5.5, `DELETE /library/folders/{id}`
  removes from disk every file row whose `folder_id` is in the folder, **including rows
  already in the trash**, because trashing only sets `deleted_at`. So the route must
  never let Bambuddy's folder delete reach a file. It:
  1. walks the folder and its subfolders;
  2. runs the same queued-print check across every live file (any hit is a 409 before
     anything is deleted);
  3. refuses with 409, before anything is deleted, if Bambuddy's trash
     (`GET /library/trash`) holds files whose `folder_id` is in the subtree ("restore or
     empty them first"). A trashed row cannot be moved out: in v1.2.5.5,
     `PUT /library/files/{id}` loads only rows not in the trash and answers 404 for a
     trashed one (`routes/library.py` L5092);
  4. moves each managed file to the top level (`folder_id: 0`), then deletes it as
     `/print/library/delete` does, to the trash. Undo then restores it at the top
     level, since its folder is gone. External files are deleted for good, as
     always, and their result says `trashed: false`;
  5. deletes the folders, deepest first. Before deleting each one it re-reads that
     folder's files **and child folders**. A folder that still holds any file or
     a child folder the check did not see is kept, with its ancestors, and what it
     holds is reported in `skipped`.

  Its result lists `deleted` (each with `trashed`), `skipped` and `folders` (the folders
  deleted), so the UI's Undo can restore the trashed files.

  Measured from Bambuddy's source at the v1.2.5.5 tag (`routes/library.py`
  `delete_folder` and L5092; `routes/library_trash.py` L88-150; `LibraryFile.folder_id`
  `ondelete="CASCADE"`) by the PR #2798 final review and its fix, 2026-10-13. `skipped`
  may name a subfolder made after the check, as well as files.

## 6. Generated client (Kiota)

- **Generation.** `scripts/generate-go-client.sh [--check]` is adapted from forge's
  `scripts/generate-kiota-clients.sh`:
  1. Export the spec with uv (`python -m scadbuddy.tools.export_openapi`), as
     `gen:api` does.
  2. Normalise it with forge's jq pass, which drops `$ref`-sibling
     `description`/`summary`. Without it, Kiota's doc comments depend on hash order
     and the check flip-flops (forge #1043).
  3. **`rm -rf tools/go/scadbuddy-client/client`**, then run Kiota from its official
     image (§6.1) with `generate -l Go -n <module>/client --include-path
     '/api/v1/print/library**' --include-path '/api/v1/operations/**'
     --clean-output`. Deleting first is what makes a file the generator no longer
     emits show up as a deletion. The include paths keep the client to what the mount
     calls.
  4. Drop `descriptionHash` from `client/kiota-lock.json`. That hash covers the whole
     spec, so without dropping it every API change anywhere would churn the committed
     client. What matters is the generated code.
  5. gofmt, `go mod tidy`, `GOWORK=off go mod vendor` in the client module, and
     `go work vendor` at the root.
- **Drift.** `--check` fails on `git status --porcelain` of the client, both vendor
  trees, `go.work*` and the go.mod/go.sum files. Untracked and deleted files count.

### 6.1 Kiota from its image

Kiota runs from Microsoft's image, pinned by tag and index digest as the Dockerfile
pins OpenSCAD:
`mcr.microsoft.com/openapi/kiota:1.36.1@sha256:077073656898de0f8d3339bad496f154d603c30c0cdcbcbc5feacad2ad988d9d`.
Forge runs Kiota as a `dotnet tool`, and ScadBuddy has no .NET.

Measured 2026-10-13 on this machine (Docker on WSL2), against this repo's exported
spec: 225 Go files in about 8 s. Three facts about the image follow from that run:

- **The working directory must stay `/app`.** The entrypoint is `dotnet kiota.dll`,
  resolved relative to the working directory, and the image's working directory is
  `/app`. With `-w /work` the container fails with ".NET SDK not found". So the repo
  is mounted at `/work`, and every path passed to Kiota is absolute (`-d
  /work/...`, `-o /work/...`).
- **It runs as uid 1654**, so the script passes `-u "$(id -u):$(id -g)" -e
  HOME=/tmp`. With that, the files it writes are owned by the caller.
- **The image has no shell.** `sh` is not on `PATH`, so every step besides `kiota`
  (jq, gofmt, go) runs on the host.

The `kiota-lock.json` it writes records `descriptionLocation` relative to the output
directory. Mounting the repo root at `/work` in both CI and locally keeps that
identical.

- **Exception to #492.** `backend/openapi.json` and the two `schema.d.ts` stay
  gitignored. The Go client is the one committed generated client, because a
  vendored Go module must build offline. CLAUDE.md gains that line.

## 7. CI

**The `freshness` job ("API spec determinism and diff").** It already exports
`head.json` twice and diffs it against main's export. It gains:

1. **Determinism.** Generate the client twice from `head.json` into scratch
   directories and require byte-identical trees.
2. **Freshness.** When the API diff against main is non-empty, or the PR touches
   `tools/go/scadbuddy-client/**` or the script, regenerate and check drift. If the
   tree drifts:
   - Run the script in a worktree of the PR's **head ref** (the job checks out the
     merge ref).
   - `git add -A` the generated paths and commit `chore: regenerate Kiota client`.
   - Push with the `RUNNER_APP` token, and fail so the next run verifies.
   - Fork and Dependabot PRs get no token: they fail with the command to run.
   - The job's permissions become `contents: write`.
3. **The diff comment.** The edited-in-place `<!-- scadbuddy-api-diff -->` comment
   gains a **Go client** section: "up to date", or "regenerated in `<sha>`" with a
   `git diff --stat`.

With no API change and no client change, the steps are skipped.

**The `go` job.** It calls `go-module-ci.yml`, copied from forge's: for each
`tools/go/*` module, gofmt, vet, build, `test -race`, and the module-mode vendor
build (`GOWORK=off go build -mod=vendor`), then `go work vendor` drift. Changes from
forge's version:
- `ubuntu-latest` only. The `runner-label` input is dropped, since the repo is public
  and self-hosted pools are off limits.
- `GOTOOLCHAIN` is pinned to `go.work`'s toolchain.
- `apt install fuse3` for the mount tests.

**Gating.** `ci.yml` never gates a job by path: CI Summary counts a skipped job as
passed, which is the vacuous-green trap its comments describe. The `go` job therefore
always runs and decides inside itself, as the `models` job does (`ci.yml`, "Select the
templates to verify"):
- On a pull request it diffs `HEAD^1..HEAD`; in a merge group it diffs against the
  queue's base.
- It does the Go work only when that diff touches `go.work*`, `vendor/**`,
  `tools/go/**`, `scripts/generate-go-client.sh`,
  `.github/workflows/go-module-ci.yml`.
- On push to main, on the schedule, or when the diff cannot be computed, it does the
  Go work every time.
- Otherwise it writes "No Go changes" to the job summary and succeeds.

The Go work runs in the reusable workflow's steps, so the job's selection step sets
an output that each later step checks.

A spec change reaches the `go` job through the regen commit, which touches
`tools/go/**`. `CI Summary` asserts `go` as required on every event, and
`dispatch-review` adds it to its `needs`.

**Dependabot.** A `gomod` entry for both modules, grouping
`github.com/microsoft/kiota-*` (forge's group) and `github.com/hanwen/go-fuse`.

### 7.1 Container image (forge's pattern)

`bambuddy-fs` is published as `ghcr.io/eh-homelab/scadbuddy-bambuddy-fs`. This copies
forge's `build-go-image.yml` and per-module `tools/go/<m>/Dockerfile` (WonderForgeLabs/forge,
`tools/go/envctl/Dockerfile`).

- **`.github/workflows/build-go-image.yml`**
  - **Triggers:** push to main when `tools/go/**`, `go.work*`, `vendor/**` or the
    workflow changes; `v*` tags; `workflow_dispatch` with a `commit_sha`.
  - **Discovery.** A `discover` job finds every `tools/go/*/main.go`. It fails if
    one has no `Dockerfile`: every Go CLI here publishes an image, as in forge.
  - **Build.** A matrix job builds and pushes each image, named `scadbuddy-<dir>`.
    Unlike forge's plain `docker build`, it uses buildx for
    `linux/amd64,linux/arm64`, with the `type=gha` cache at
    `scope=go-<dir>,mode=min`.
  - **Tags:** `sha-<sha>` and `latest`, plus the version on a `v*` tag.
  - **Runners:** `ubuntu-latest`. The package must be public, like the others.
- **`tools/go/bambuddy-fs/Dockerfile`**
  - **Context:** built with the repo root as context (`-f tools/go/bambuddy-fs/Dockerfile .`),
    not the module directory as forge does. The mount depends on the workspace's
    `scadbuddy-client` and on the root `vendor/`.
  - **Build stage:** `golang:<go.work toolchain>-bookworm` on `--platform=$BUILDPLATFORM`.
    It cross-compiles with `TARGETOS`/`TARGETARCH`, `CGO_ENABLED=0` and
    `-mod=vendor`, so nothing is downloaded.
  - **Runtime:** `debian:bookworm-slim` with `fuse3` and `ca-certificates`, unlike
    forge's distroless. FUSE needs `fusermount3`, and the process runs as root,
    since mounting needs `CAP_SYS_ADMIN` and `/dev/fuse` in a container anyway.
  - **Entrypoint:** `ENTRYPOINT ["bambuddy-fs"]`.
- **hadolint.** The lint job's hadolint step also covers `tools/go/*/Dockerfile`.
- **CI.** The `go` job (§7) builds each module's image and runs `--help` whenever it
  does the Go work. Its selector already matches `tools/go/**`.
- **`.dockerignore`.** `tools/`, `go.work*` and `vendor/` must reach the build
  context; nothing ignores them.

**Running it**
```
docker run --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined \
  -v /mnt/bambuddy:/mnt/bambuddy:rshared ghcr.io/eh-homelab/scadbuddy-bambuddy-fs \
  mount --api http://scadbuddy/api/v1 --allow-other /mnt/bambuddy
```
`--allow-other` (FUSE `allow_other`) lets processes other than the mounting one,
such as the host and other containers, see the files.

### 7.2 Go lint and vendoring (gooey's pattern)

- **Lint is gofmt and `go vet`.** That is what forge and gooey run; neither uses
  golangci-lint. The gofmt step is gooey's (WonderForgeLabs/gooey `ci.yml`, "gofmt"):
  - it walks the tree, pruning dot-directories and `vendor`;
  - it emits an `::error file=<f>::` annotation per unformatted file;
  - it fails if the walk finds suspiciously few files, the sign of a broken prune.
- **Vendor autofix for Dependabot.** A gomod bump changes a `require`, and with one
  root `vendor/`, `go` then refuses with "inconsistent vendoring".
  `.github/workflows/vendor-autofix.yml` is copied from gooey:
  - it runs on `pull_request_target` from `dependabot[bot]` on this repo;
  - it runs `go work vendor` (and `GOWORK=off go mod vendor` in the client module);
  - it commits `build(deps): go work vendor` and pushes.

  It pushes with the `RUNNER_APP` token, not `GITHUB_TOKEN`, so CI re-runs on the new
  head; gooey's note says a `GITHUB_TOKEN` push does not. It runs on `ubuntu-latest`.
- **Vendor freshness** stays in the `go` job (forge's `go work vendor` drift check),
  not gooey's separate `vendor-freshness.yml`. One gated job covers it.

## 8. Caching and consistency

- **Listings** are cached for `--attr-ttl` (default 5 s), and go-fuse's kernel
  entry/attr timeouts use the same value. Every write invalidates its folder's
  listing and kernel entries.
- **Contents.** A file's bytes are immutable per ID, so content is cached on disk at
  `$XDG_CACHE_HOME/bambuddy-fs/<id>`, checked against `file_size`. The first `read`
  of an open file streams the whole file into the cache, so readers get seekable
  random access.
- **Writes.**
  - `create`/`write` go to a temp file under the cache directory. The upload runs
    on `flush` (each `close()` of a descriptor) when writes are unsent, and returns its
    error to `close()`. A flush with nothing new to send uploads nothing, so a file
    written once uploads once however often it is flushed. `release` uploads only
    what is still unsent.

    Measured 2026-10-13 on WSL2's 6.6 kernel by the Task 8 implementer: `close()`
    returns before FUSE's `release` runs, so an upload made only in `release` could
    never report an error. That is why the upload runs on `flush`.
  - Writing over an existing name uploads the new file, then deletes the old one, so
    the path keeps its name but its ID changes. Bambuddy has no in-place replace.
  - `truncate` to 0 on a file being written is allowed.

## 9. Errors

| ScadBuddy answer | errno |
|---|---|
| 404 | `ENOENT` |
| 409 | `EBUSY` |
| 413, or a size cap | `EFBIG` |
| 401/403 (a Bambuddy scope the key lacks) | `EACCES` |
| 5xx, timeout, unreachable | `EIO` |
| any write on `--ro` | `EROFS` |
| chmod, chown, symlink, link, xattrs, truncate of a stored file | `ENOTSUP` |

Each error is logged with the operation and the response's problem `detail`. A 202
is followed through `GET /operations/{id}` until it finishes or the FUSE request's
own timeout passes, in which case the result is `EIO` and a log line names the
operation ID.

## 10. Testing

- **Go unit tests** run against an `httptest` fake of the routes above, behind the
  generated client. They cover:
  - the tree;
  - duplicate-name suffixes and their stability;
  - `--all` and `--ro`;
  - errno mapping;
  - 202 polling;
  - upload on `release` and replace-by-name;
  - `rmdir` of a non-empty folder.
- **Mount tests** use go-fuse's test helpers on a real mount, and skip without
  `/dev/fuse` (CI installs `fuse3`).
- **Backend tests**, `requires_postgres` like their peers, cover each new kind and
  route against the recorded Bambuddy fakes:
  - the folder delete's walk, its 409-before-anything, and external files reported as
    not trashed;
  - multipart upload by claim;
  - rename/move sending only the fields given.
- **Agent tests:** the new tools, coverage, and both session modes.

## 11. Phases

1. **Backend and agent.** The §4 routes and kinds, their tools, and their tests.
2. **The Go tool.** Workspace, vendor tree, Kiota client and script, the CLI and FUSE
   layer, `go-module-ci.yml`, the `go` job and its gating, the `freshness`
   additions, Dependabot, and the CLAUDE.md lines. It is read-write from the start,
   since phase 1 has landed.
