# bambuddy-fs

Mounts Bambuddy's print library, through ScadBuddy's API, as a FUSE filesystem, so
folders and files can be browsed, copied in and out, renamed and deleted with ordinary
tools (#2686). Design: `docs/superpowers/specs/2026-10-13-bambuddy-fs-design.md`.

It needs `fuse3` (`fusermount3`) and `/dev/fuse`. By default it sends traces and
metrics, with the host's name, to eh-homelab's collector; `--no-telemetry` turns that
off (see "Telemetry").

## Build and run

The module's `go.mod` has a `replace` for the generated client (`../scadbuddy-client`),
so build it from the workspace at the repo root:

```bash
go install ./tools/go/bambuddy-fs     # into $(go env GOPATH)/bin; again after a pull
bambuddy-fs mount --all --background  # the deployed ScadBuddy, at ~/bambuddy
bambuddy-fs status
bambuddy-fs umount
```

`mount` defaults `--api` to `$BAMBUDDY_FS_API`, else the deployed ScadBuddy, and the
mountpoint to `$BAMBUDDY_FS_DIR`, else `~/bambuddy` (made if missing). Without
`--background` it stays in the foreground and unmounts cleanly on SIGINT or SIGTERM;
if the mount is busy it logs that and exits non-zero.

`--background` starts it again detached, its output appended to
`<state-dir>/mount.log` after a `===` banner (so the lines naming unsent writes outlive
the next mount), and returns once the mount is up, or prints what the log gained and
fails. Every mount records its pid in `<state-dir>/mount.pid`.

`umount` runs `fusermount3 -u`, then waits for the process, then sends it SIGTERM,
then SIGKILL: on WSL, Windows reaches the mount through `\\wsl$` from another mount
namespace, whose copy keeps the FUSE connection open after the unmount, so the process
may not exit on its own. Its unsent writes are on disk either way. `status` exits 0
only when the mountpoint is mounted and its process is running. Both take the
mountpoint and `--state-dir` of the mount they act on.

### WSL and Windows

Windows Explorer reaches a WSL mount only when other users may see it. Uncomment
`user_allow_other` in `/etc/fuse.conf` (the mount then passes FUSE `allow_other` by
default), then open `\\wsl$\<distro>\home\<you>\bambuddy`.

`allow_other` has a cost: the mount does not check permissions (no
`default_permissions`), so with it every local user, and every container that shares
the mount, can rename, overwrite and delete library files through it, with the
mounting user's access to ScadBuddy. Use it only on a machine whose users you trust.

## Container image

Published as `ghcr.io/eh-homelab/scadbuddy-bambuddy-fs` (`sha-<7-char commit>`, `latest`
for main's head, and the version on a `v*` tag; amd64 and arm64) by
`.github/workflows/build-go-image.yml` on every push to main that touches Go. Build it from the repo root, since it needs the
workspace and the root `vendor/`:

```bash
docker build -f tools/go/bambuddy-fs/Dockerfile -t scadbuddy-bambuddy-fs:dev .
```

It runs as the unprivileged user `bambuddy` (uid 10001) and mounts through the setuid
`fusermount3`. The container needs:

- `/dev/fuse`: `--device /dev/fuse`;
- `CAP_SYS_ADMIN` in its bounding set: `--cap-add SYS_ADMIN`;
- privilege escalation allowed, for the setuid helper: not `--security-opt
  no-new-privileges` (in Kubernetes, `allowPrivilegeEscalation: true`);
- under Docker's default AppArmor profile, which denies `mount`:
  `--security-opt apparmor:unconfined`;
- a mountpoint uid 10001 can write. Otherwise run it with `--user 0`;
- a volume for the state directory, `/home/bambuddy/.local/state/bambuddy-fs`. It holds
  the unsent and refused writes (see "Unsent writes"), the only copy of a save
  ScadBuddy has not taken yet, so it must outlive the container: an image update, a
  `docker run --rm` or a pod restart would otherwise delete them. The image declares it
  a `VOLUME`, but an anonymous one goes with `--rm`; use a named volume (Docker gives an
  empty one the directory's owner, uid 10001) or a host directory uid 10001 owns. In
  Kubernetes, a PersistentVolumeClaim. One mount at a time may use it.
- optionally, a volume for the cache directory, `/home/bambuddy/.cache/bambuddy-fs`, so
  downloads stay cached across recreates.

```bash
sudo install -d -o 10001 -g 10001 /mnt/bambuddy
docker run --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined \
  -v bambuddy-fs-state:/home/bambuddy/.local/state/bambuddy-fs \
  -v bambuddy-fs-cache:/home/bambuddy/.cache/bambuddy-fs \
  -v /mnt/bambuddy:/mnt/bambuddy:rshared ghcr.io/eh-homelab/scadbuddy-bambuddy-fs \
  mount --api http://scadbuddy/api/v1 --allow-other /mnt/bambuddy
```

`--allow-other` lets processes other than the mounting one, such as the host and other
containers, see and change the files (see "WSL and Windows" for what that allows); the image's `/etc/fuse.conf` sets `user_allow_other` so the
unprivileged user may pass it. `--cache-dir` and `--state-dir` default to the two volume
paths above. With no arguments the image prints `mount --help`.

## Flags

```
bambuddy-fs mount [--api <url>] [--background] [--ro] [--all] [--allow-other | --no-allow-other] [--attr-ttl 5s] [--cache-dir <dir>] [--cache-max 2G] [--state-dir <dir>] [--otel-endpoint <url>] [--no-telemetry] [<mountpoint>]
bambuddy-fs umount [--state-dir <dir>] [<mountpoint>]
bambuddy-fs status [--state-dir <dir>] [<mountpoint>]
bambuddy-fs unsent [--state-dir <dir>]
```

| Flag | Meaning |
|---|---|
| `--api` | ScadBuddy's API root, like `http://host/api/v1`. Default `$BAMBUDDY_FS_API`, else the deployed ScadBuddy. |
| `--background` | Run detached and return once mounted (see "Build and run"). |
| `--ro` | Every write fails with `EROFS`. |
| `--all` | List every file. Without it the mount lists what the library dialog lists by default (unsliced 3MFs). |
| `--allow-other` | Fail unless FUSE `allow_other` is allowed (other users and processes see the mount). It is on by default whenever `/etc/fuse.conf` has `user_allow_other`, or for root. |
| `--no-allow-other` | Only the mounting user sees the mount. |
| `--attr-ttl` | How long listings are cached, and the kernel's entry and attribute timeout. Default `5s`. |
| `--cache-dir` | Downloaded contents and files being written, under `<cache-dir>/<API host>[_<port>]/`, so two ScadBuddy instances never share contents (file IDs are each one's own). Default `$XDG_CACHE_HOME/bambuddy-fs`, or `~/.cache/bambuddy-fs`. One mount at a time uses an API's cache (it holds a `lock` there). |
| `--cache-max` | At most this much downloaded contents is kept in `--cache-dir`, the least recently read removed first: a number of bytes, or with `K`, `M`, `G` or `T` (binary; `2G`, `2GiB` and `2GB` are the same). `0` is no bound. Default `2G`. |
| `--state-dir` | Writes whose upload failed, under `unsent/` (see "Unsent writes"). Default `$XDG_STATE_HOME/bambuddy-fs`, or `~/.local/state/bambuddy-fs`. |
| `--otel-endpoint` | OTLP/HTTP collector base URL for traces and metrics (see "Telemetry"). |
| `--no-telemetry` | Send no traces or metrics. `--no-tracing` is the same. |

`bambuddy-fs --help` and `bambuddy-fs mount --help` print the usage and exit 0; an
unknown command exits 2.

## Names

A folder's contents show under their names. When several files in a folder share a
name, the oldest shows as the plain name and the later ones as `<stem> (id <N>)<ext>`,
so a path is stable for as long as its file exists. Times are Bambuddy's `created_at`.
Everything is owned by the user who mounted it, files `0644` and folders `0755` (no
write bits with `--ro`).

## Deletes

- `unlink` of a managed file moves it to Bambuddy's trash. An external file is gone for
  good.
- `rmdir` takes the folder's contents with it, whether or not the folder is empty:
  managed files go to the trash (Undo restores them at the top level, since their
  folder is gone), external files are deleted for good, and then the folders.
- A queued print that uses a file in the folder, or files in the folder's trash, answer
  409, which is `EBUSY`; nothing is deleted. Restore or empty the trash first.
- A folder that still holds a file or subfolder the check did not see is kept, with its
  ancestors.

## Caching and uploads

A file's bytes are immutable per ID, so contents are cached on disk under `--cache-dir`.
Opening a file downloads nothing; its first read fetches all of it, so a file manager
that opens every file to sniff its type, or only `fstat`s it, costs no download. The
downloaded contents are kept to `--cache-max` (default `2G`): past it, the least
recently read are removed. Files being written are under the cache directory too but
never count or go, and writes not sent yet are under `--state-dir`, not here. Listings
are cached for `--attr-ttl`; every write invalidates its folder.

`statfs` (`df`, a copy's free-space check) reports the cache directory's filesystem,
where every write is staged before its upload, and names of up to 255 bytes.

Writes go to a temporary file, and the upload runs when the descriptor is closed
(`flush`). A close with nothing new to send uploads nothing. `fsync()` makes the
temporary file durable, but nothing reads it back after a crash: a save that was
fsync'd and not yet closed is lost if the machine crashes. Writing over an existing name uploads the new file and then
deletes the old one, so the path keeps its name but its file ID changes: Bambuddy has
no in-place replace. Truncating to 0 a file being written is allowed. If the upload
fails, the write is queued (below) and `close()` succeeds.

Without `--all`, a file this mount uploaded that the listing does not show (an app's
`x.3mf.tmp`, say) stays visible until it is renamed, removed or the mount ends, so the
app can still rename or remove it. Changes made outside the mount to such a file show
only after a listing shows it, or after a remount.

## Renames

`rename()` over an existing file replaces it, as on any filesystem, though the library
cannot do it in one step:

- A stored file renamed over another takes the name, then the other is deleted. If that
  delete fails (a queued print uses it), both remain, the answer is `EIO`, and the log
  names both IDs.
- A file not sent yet (still being written, or queued) takes the name at once and
  shadows the stored file there, which it replaces when it is sent (see the conflict
  rule below). A slicer's save, write `x.3mf.tmp`, close, rename over `x.3mf`, works
  whether ScadBuddy is up or not, in any folder the mount has listed since it started
  (see "While ScadBuddy is down").
- A queued file renamed over another queued file supersedes it; a file still being
  written supersedes it once its own bytes are durable (sent or queued).

`RENAME_NOREPLACE` over anything, a folder over a folder, and anything over a file
still being written are `EEXIST`; a file over a folder is `EISDIR`, a folder over a file
`ENOTDIR`.

## While ScadBuddy is down

When ScadBuddy does not answer (unreachable, a timeout, or a 5xx such as a deploy's
503), each folder's last listing is served, however old, so `ls`, lookups, reads of
cached contents and queued writes, creates and renames go on, and a save is queued
(below) and sent once ScadBuddy is back. The log says so once when it starts, and once
when a listing succeeds again. What changed in the library meanwhile shows only then.

Only folders listed since the mount started have a listing to serve. Mounted while
ScadBuddy is down, the top level, and any folder not yet opened, answer `EIO` until
it is back. Reading a file whose contents are not cached, `mkdir`, `rmdir`, `unlink`
of a stored file and renaming a stored file need ScadBuddy, and answer `EIO`.

## Unsent writes

A write whose upload fails for any reason but a refusal of the request itself (see
"Refused writes") is not dropped: ScadBuddy down or unreachable, a 5xx, a timeout, a 404
from an older ScadBuddy without the upload route, and also a 401 or 403 (the API key
lacks a Bambuddy scope) or a 409, which a later attempt may get past once the cause is
fixed. Its bytes move to
`<state-dir>/unsent/<n>/contents`, beside a `record.json` (folder ID, name, size, when
it was saved, attempts, last error, and the stored files it replaces), both fsync'd, and
the log names that directory once.

The queue is in the state directory, not the cache directory, because it is the only
copy of what was written: cache cleaners may empty `~/.cache`. One mount at a time uses
a state directory (it holds `<state-dir>/lock`); a second fails to start. Records do not
say which ScadBuddy they are for: a mount of another `--api` over the same state
directory would send them there, so give each API its own `--state-dir`.

`close()`, `fsync()` and `release` then succeed: the bytes are as safe on local disk as
they would be in the library, and an app told its save failed would report it, or
delete what it wrote, though nothing is lost. Only a failure to queue the bytes (a disk
error) is `EIO`.

While queued, the file stays in its folder: `stat` and reads come from the local copy,
and it can be renamed, moved, overwritten or removed like any file. Its inode numbers
have bit 62 set, apart from folders (`id << 1`) and files (`id << 1 | 1`). A rename
updates the record, so it is sent under its final name and folder. Writing a queued
file never changes the queued copy: the writer works on a copy, which supersedes the
queued one once it is itself sent or queued. Two saves of one name never land out of
order: the later always supersedes the earlier.

A background retrier sends each queued write when no handle has it open: first at
mount start, then after 15 s doubling to 10 min, with 20% jitter. Read-only mounts
retry too. Records survive an unmount, a crash or `kill -9`, and are reloaded at the
next mount; a record that cannot be read is logged and left in place. If the queued
write's folder is gone, it is sent to the top level instead.

Before each upload the folder is listed afresh, so a save that waited never overwrites
a newer one. The record keeps the stored files the write replaces: the one that had the
name when the write was saved, and those of whatever it was renamed over, each with its
ID, `created_at` and name.

- If no file has the name, or only files the write replaces, the write is sent under
  its name, and each file it replaces that is still the same file under the same name
  is deleted. One renamed or replaced meanwhile is left alone.
- If any other file has the name (someone saved after this write, or created the name
  while it waited), it is left alone and the write is sent as a conflict copy,
  `<stem> (conflict <saved at, local time, YYYY-MM-DD HHMMSS>)<ext>`, for example
  `bracket (conflict 2026-10-13 141502).3mf`, and a warning names both. Nothing is
  deleted.

Every attempt to send a write carries the same `Idempotency-Key`, kept in `record.json`
(a record from an older build gets one when it is loaded). So when an upload ran but
its answer was lost (a deploy cut the connection), the next attempt, finding a file of
the write's name that it does not replace and having last been sent under that name,
sends the same request again, and ScadBuddy answers it with that file instead of
uploading a second: the write then replaces its bases as usual. If ScadBuddy answers
with a new file instead, the name is someone else's, and the new file is renamed to the
conflict copy.

Once sent, the local copy is deleted and the folder is listed afresh. While a library
file it does not replace has the name, a queued write of that name is not shown, so a
name never shows twice.

### Refused writes

An upload ScadBuddy refuses outright (400, 413, 415, 422) would be refused again, so it
is not retried, but the bytes are kept: on the first close the refusal is that
`close()`'s error (`EFBIG` for 413) and the last release tries once more; then the bytes
move to `<state-dir>/refused/<n>/` with the same `record.json`, and the log names the
path. A queued write refused on a retry moves there too. Refused writes leave their
folder; fix the cause (a proxy's body limit, a name) and copy the file back in.

`bambuddy-fs unsent [--state-dir <dir>]` lists the queue (path, folder ID, name, size,
attempts, last error), then the refused writes under a "refused" heading, and exits. It
works while a mount runs (it takes no lock), so an entry being written may show briefly
as an unreadable record.

## Errors

| ScadBuddy answer | errno |
|---|---|
| 404 | `ENOENT` |
| 409 | `EBUSY` |
| 413, or a size cap | `EFBIG` |
| 401/403 (a Bambuddy scope the key lacks) | `EACCES` |
| 5xx, timeout, unreachable | `EIO` |

Those are the answers of every operation but an upload. An upload is queued instead,
and its `close()` succeeds, on everything but 400, 413, 415 and 422, which are refusals
(`close()` answers `EFBIG` for 413, else `EIO`; see "Refused writes"). So a 401, 403,
404 or 409 on an upload is queued, not `EACCES`, `ENOENT` or `EBUSY`. While ScadBuddy
does not answer, listings are served stale instead (see "While ScadBuddy is down").

| Local cause | errno |
|---|---|
| any write on `--ro` | `EROFS` |
| chmod, chown, symlink, link, setting or removing an xattr, truncate of a stored file | `ENOTSUP` |

Reading xattrs answers `ENODATA` and an empty list, so `ls -l` and `cp` stay quiet, and
a change of times only is accepted and ignored. Each error is logged with the operation
and the problem `detail`. A write that answers 202 is followed through
`GET /operations/{id}`, polled from 250 ms backing off to 2 s, through network errors
and 5xx answers, until the operation ends. FUSE requests have no timeout, so that wait
lasts as long as the operation, and ends early only when the caller is interrupted
(`EIO`, and the log names the operation id). An upload, including its operation, is
cut off after 30 minutes and queued.

## Telemetry

Telemetry is on by default: unless told otherwise, the mount sends OpenTelemetry
traces and metrics, including this machine's host name as `service.instance.id`, to
eh-homelab's collector. Pass `--no-telemetry` (or set `OTEL_SDK_DISABLED=true`) to send
nothing, which anyone running the published image outside eh-homelab should do.

They go over OTLP/HTTP to `https://otlp.internal.nullreference.io` (`/v1/traces`,
`/v1/metrics`), with TLS checked against the system roots (eh-homelab/clusters#2221).
That endpoint takes no credentials, and its address is also the gateway's public HTTPS
target, so it is reachable from outside the LAN as well, not only from it.

The standard `OTEL_*` variables apply as the OpenTelemetry
SDK reads them (`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_CERTIFICATE`,
`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER` and `_ARG`,
`OTEL_METRIC_EXPORT_INTERVAL`, default 60 s). Where each signal goes, first match wins:

1. `--otel-endpoint <base>`: `<base>/v1/traces` and `<base>/v1/metrics`;
2. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, as is;
3. `OTEL_EXPORTER_OTLP_ENDPOINT` plus the signal's path;
4. the default above.

`--no-telemetry` (or `--no-tracing`) or `OTEL_SDK_DISABLED=true` turns both off;
`OTEL_TRACES_EXPORTER=none` or `OTEL_METRICS_EXPORTER=none` turns off one. Off, no
exporter is built and nothing touches the network. Metrics are always cumulative,
whatever `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` says, since Prometheus
refuses delta.

Telemetry never holds up the mount: setup makes no network call, export runs in the
background, an unreachable collector (a name that does not resolve, a refused connection) is
logged once with the first export error and never again, and the final flush on unmount
is bounded at 5 s (1 s when the mount itself failed).

The resource is `service.name=bambuddy-fs`, `service.version` when the binary carries
one, and `service.instance.id` set to the host's name (a random UUID if it has none):
the collector replaces `host.name` with its own, so the instance id is what keeps two
workstations' series apart. In a container the host name is the container's ID, so
each new container starts new series; set `OTEL_RESOURCE_ATTRIBUTES=service.instance.id=<name>`
to keep one. `service.namespace` is not set. In Prometheus the series
carry `job="bambuddy-fs"`, so `{job="bambuddy-fs"}` selects them (eh-homelab/clusters#2224).

Spans:

- `fuse.<op>` (INTERNAL) for each FUSE operation that reaches ScadBuddy or does work:
  a lookup or readdir that lists its folder, a read or write that downloads,
  a flush or release that uploads, create, mkdir, rmdir, unlink, rename and a truncate.
  A lookup or getattr answered from the cache has none, and a lookup of a name that is
  not there (`ENOENT`) is not an error.
- `scadbuddy.<operation>` (CLIENT) for each ScadBuddy API request (`list_library`,
  `download`, `upload`, `update_file`, `delete_files`, `create_folder`, `update_folder`,
  `delete_folder`, `poll_operation`), with the method, status code and route template
  (`url.template`, never the URL); each request carries `traceparent`, so the backend's
  spans join the trace. A download's span lasts until its bytes are read.
- `unsent.retry` (INTERNAL) for each attempt to send a queued write, with the attempt
  number and the result (`ok`, `failed`, `refused`).

Metrics: `bambuddy_fs.fuse.operations` and `bambuddy_fs.fuse.operation.duration` (by
`op` and `result`, `ok` or the errno's name), `http.client.request.duration` (by
`scadbuddy.operation`, method, status code, and `error.type`: the status code of a
4xx/5xx, or `timeout`, `canceled` or `transport` with no answer),
`bambuddy_fs.transfer.bytes` (file contents only, by `direction`, `upload` or
`download`), the gauges `bambuddy_fs.cache.bytes`,
`bambuddy_fs.unsent.entries` and `bambuddy_fs.unsent.bytes`, and
`bambuddy_fs.unsent.uploads` (by `result`).

What is recorded is ids (file, folder, operation), byte counts, operation names, errno
names, status codes and durations. Never a file or folder name, a path, file contents,
an xattr, a response body or any text ScadBuddy or Bambuddy returns; an error's status
is its errno name or status class, never its message. Metric attributes never carry
ids. The OpenTelemetry global providers stay the no-op, so the Kiota client's own spans,
which carry URLs, are not sent.
