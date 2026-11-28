# Upstream cache: one engine for git, HTTP and blob copies

Status: design, 2026-11-28. Issue: #8517 (epic). Hotfix that led here: #8509, PR #8518.

## 1. Problem

ScadBuddy fetches the same upstream bytes many times, and each consumer fetches them in its
own way.

| Consumer | Upstream | When | Today's copy |
|---|---|---|---|
| OpenSCAD libraries (BOSL2 and others) | git, GitHub | each pin on the API; each library on each render pod start | one clone per pod, `/data/libraries` on the API's NFS share, each render pod's emptyDir |
| Agent plugin packages, commit pins | git, GitHub | install, re-pin, mirror job | a zip blob per commit, `plugin-mirror-<sha256>` (#7907) |
| Agent plugin packages, archive and npm pins | HTTP | install, re-pin | a blob per sha256 (#6935) |
| Fonts | HTTP, `raw.githubusercontent.com/google/fonts` | each install | blobs; `store/fonts.py` `FontMirror` copies them to the workers |
| URL import, `fetch_asset` | the URL a person gives | each request | one asset per fetch |

There are three git runners with their own hardening: `backend/scadbuddy/library/libraries.py`
`_git`, `backend/scadbuddy/core/gitrepo.py`, and `agent/src/plugins/packages/git.ts`. #8509 was one of
them without `safe.directory` on the NFS share, which writes every file as uid 1024 while the API
runs as uid 10001 (measured on prod 2026-11-28, pod `scadbuddy-75c5db88f9-2scw8`).

## 2. Decisions

- **D1. One engine.** A cache is the same Temporal pattern for every source and every
  storage. Only the source and the storage are different. The owner asked for this
  (2026-11-28).
- **D2. Go.** The engine, its sources and its storages are a Go module, `tools/go/upstreamcache`.
  New backend work is in Go (#7667).
- **D3. Keys are immutable.** A cache entry is keyed by what its bytes are: a git commit, or
  a sha256. An entry never changes after it is written. A name that can move (a git ref, a
  URL) is not a key. It is resolved to a key first (§4.3).
- **D4. One writer per key and location.** For each pair (location, key) there is one path
  that writes: one workflow id (§4.2). Two callers that want the same entry at the same place
  join one run. Reads take no lock, because an entry is published whole by an atomic rename
  and never changed after. So locks are needed only for writes, and the workflow id is the
  lock.
- **D5. Tiers chain.** A storage tier fills a miss from the tier above it, never from
  upstream past it. The usual chain is: local disk, then blob store, then (for git) the NFS
  mirror, then upstream. Only the top tier talks to upstream.
- **D6. Routing by task queue.** A pod's local disk is reached through its own task queue,
  `cache-local.<hostname>`, as `agent-sessions.<hostname>` and `agent-tools.<hostname>` do. No
  long-lived workflow per pod.
- **D7. No upstream on a hot path.** A render, a turn or a load reads only a cache tier. Only
  a fill of the top tier calls upstream, once per key (owner, 2026-11-26).
- **D8. Untrusted pods get no shared storage.** The render worker runs template Python. It
  never mounts NFS and never calls upstream. It reads the blob store through the API, as
  today (#7958).

## 3. Model

```
Key      { source: "git" | "http" | "blob", id: commit | sha256, origin: url }
Source   resolve(name) -> Key            # git ref -> commit, URL -> sha256 (top tier only)
         fetch(key, into dir) -> files   # fills the top tier
Store    has(key) -> bool
         open(key) -> reader             # lock-free
         write(key, from tier) -> done   # only inside the key's fill workflow
         sweep(policy)                   # LRU or holds, never a key a hold names
```

The storages:

| Store | Where | Holds | Writer |
|---|---|---|---|
| `nfs-git` | `/data/upstream-cache/git/<host>/<owner>/<repo>.git`, a bare mirror on the API's NFS share | every fetched commit of a repository | the mirror's worker in the API pod |
| `blob` | the content store (S3 on prod) | one archive per key: `cache-git-<commit>`, `cache-http-<sha256>` | the tier's fill activity, through the blob API (#7958) |
| `local` | a pod's own disk, `<root>/<source>/<id>/`; `<root>` is configurable, by default `<state>/upstream-cache/` on the pod's emptyDir (§8) | the unpacked files of one key | that pod's `cache-local.<hostname>` worker |

The sources:

| Source | Resolve | Fetch |
|---|---|---|
| `git` | `git ls-remote` or a fetch of the ref into the mirror | `git fetch` of the commit into `nfs-git` |
| `http` | none: an HTTP key is the sha256 the pin records; a first fetch hashes what arrives | a GET with the URL import's guards (https, public addresses, pinned address, byte cap, deadline) |
| `blob` | none | a read of the blob, for `local` only |

The git library code (hermetic environment, `safe.directory`, allowed protocols, no
redirects, pinned addresses, size caps, timeouts) lives once, in `upstreamcache/git`. It
replaces the three runners in §1, as each caller moves to Go.

## 4. Workflows

### 4.1 `CacheFill`

One workflow type for every tier. Its input is `{store, key, from}`. It:

1. asks `store.has(key)`, and returns at once on a hit;
2. ensures `from` holds the key: a child `CacheFill` on the tier above, or `source.fetch` on
   the top tier;
3. writes the key into `store` from `from`, to a temporary name, then renames it into place;
4. returns where the entry is.

Each step is an activity on the store's own task queue: `cache-nfs` for `nfs-git` (polled
only in the API pod), `cache-blob` for `blob`, and `cache-local.<hostname>` for one pod's
`local`.

### 4.2 Ids and joining

The workflow id is `cache-<store>-<location>-<source>-<id>`, with `<location>` the hostname for
`local` and `shared` for the others. A caller starts it with `USE_EXISTING` and
`ALLOW_DUPLICATE` and waits for its result. Ten renders on one pod that need one commit
start one run; the nine others join it. A run that ended is started again only when the
entry has gone since (a sweep, a new pod). This is the pattern `PackageMirror` uses today
(`agent/src/temporal/packageMirror.ts`).

### 4.3 `CacheResolve`

A name that can move is resolved by the top tier of its source: `git` asks the mirror,
which fetches the ref from upstream into `nfs-git`. One run per repository at a time
(`cache-resolve-git-<repo>`), so fetches into one bare repository never overlap: D4 for the
one mutable thing, the mirror's refs. A resolve is a person's action (a new pin, "update to
latest") or an optional Schedule that keeps refs warm for update checks. A pin stores the
key it resolved to, never the name alone.

### 4.4 Subscribers

A caller needs one answer: the entry is ready, and where. The fill workflow's result gives
it (an Update-with-start, or the result of the started run). No stream is needed for that.

Temporal's Workflow Streams are in Public Preview since Replay 2026, built on Signals and
Updates, aimed at live output to a UI ([announcement](https://temporal.io/blog/replay-2026-product-announcements)).
Its language coverage is unclear from the sources found (Python and TypeScript are named; Go is
not confirmed), and third-party notes say it does not replay missed events on a reconnect
([Ably](https://ably.com/blog/temporal-workflow-streams-durable-session-layer)). So the engine
does not depend on it. Progress for a person (bytes fetched, a slow clone) goes on the
existing event bus and realtime socket as a `cache.progress` event, if the UI needs it. A
stream can replace that later without a change to the engine.

## 5. Consumers

| Consumer | Calls | Reads from |
|---|---|---|
| API: pin, openscad, preview, `openscad-lsp` | `CacheResolve` at pin; `CacheFill{local}` on its own queue | its pod's `local` |
| Render worker | `CacheFill{local}` on its own queue, run by a Go sidecar `upstreamcache-local` in its pod that shares its emptyDir | its pod's `local` |
| Agent plugin commit pins | `CacheFill{blob}`; the zip of #7907 becomes the `blob` tier's archive format | `blob`, then its own cache dir |
| Agent plugin archive and npm pins, fonts | `CacheFill{blob}` with the `http` source | `blob` |
| URL import, `fetch_asset` | unchanged: one-off fetches of a person's URL, already stored as assets | — |
| Model and flow history (`gitrepo.py`) | unchanged: local repositories with no upstream; they take only `upstreamcache/git` when they move to Go | — |

## 6. Security

- Only the API pod's worker writes `nfs-git` and calls upstream for git. The render pod's
  sidecar holds the render worker's CRUD credential only, reads blobs through the API, and
  has no route to upstream (D8).
- Every fetch from upstream keeps today's guards: the allowed protocols, the vetted and
  pinned addresses, no redirects, the size cap (`library_max_bytes`, the package caps).
- An archive is unpacked with the package unpacker's rules (no links, no devices, no paths
  out; `agent/src/plugins/packages/unpack.ts`), ported to Go once.
- A key is checked when it is read into a tier: a git archive by the commit's tree, an HTTP
  entry by its sha256.

## 7. Work

1. `tools/go/upstreamcache`: the engine (`CacheFill`, `CacheResolve`), the `Store` and `Source`
   interfaces, `upstreamcache/git`, and tests on a local upstream.
2. The `nfs-git` and `blob` stores, run by core's worker in the API pod.
3. The `local` store and the `upstreamcache-local` sidecar for the render pod (an image and
   its Deployment change in eh-homelab/clusters).
4. OpenSCAD libraries move to it: `LibraryStore` and `CheckoutFetcher` call `CacheFill`, and
   `/data/libraries` goes once nothing reads it.
5. Agent plugin commit pins move their fetch to `CacheFill{blob}`.
6. Agent plugin archive and npm pins, and Google Fonts, move to the `http` source.

## 8. Answered questions (owner, 2026-11-28)

- **The `nfs-git` mirror is never pruned.** It keeps every commit it fetched, so an old pin
  never goes back to upstream. Git's own `gc` may still pack it; nothing deletes a commit.
- **The `local` store is each pod's own disk, at a configurable path.** The default is
  `<state>/upstream-cache/`, on the pod's emptyDir, and a setting of the store's worker can move
  it (to the NFS share, for one). D4 holds on any path: the workflow id includes the location,
  so one writer per path.
