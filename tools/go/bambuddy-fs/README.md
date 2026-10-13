# bambuddy-fs

Mounts Bambuddy's print library, through ScadBuddy's API, as a FUSE filesystem, so
folders and files can be browsed, copied in and out, renamed and deleted with ordinary
tools (#2686). Design: `docs/superpowers/specs/2026-10-13-bambuddy-fs-design.md`.

It needs `fuse3` (`fusermount3`) and `/dev/fuse`.

## Build and run

The module's `go.mod` has a `replace` for the generated client (`../scadbuddy-client`),
so build it from the workspace at the repo root:

```bash
go build -o bambuddy-fs ./tools/go/bambuddy-fs
mkdir -p ~/bambuddy
./bambuddy-fs mount --api http://scadbuddy.lan/api/v1 --all ~/bambuddy
```

The process stays in the foreground and unmounts cleanly on SIGINT or SIGTERM. If the
mount is busy it logs that and exits non-zero; close what uses it and run
`fusermount3 -u <mountpoint>`.

A helper that builds from the checkout it lives in and mounts at `~/bambuddy` is
`scripts/bambuddy-mount.sh`. Run it directly, or put it on PATH with
`ln -s "$PWD/scripts/bambuddy-mount.sh" ~/bin/bambuddy-mount`:

```bash
scripts/bambuddy-mount.sh [mount] [--ro] [--api URL] [--dir DIR] [-- extra flags]
scripts/bambuddy-mount.sh umount
scripts/bambuddy-mount.sh status
```

`SCADBUDDY_REPO`, `BAMBUDDY_FS_API` and `BAMBUDDY_FS_DIR` override its defaults. It adds
`--allow-other` when `/etc/fuse.conf` allows it.

### WSL and Windows

Windows Explorer reaches a WSL mount only when other users may see it. Uncomment
`user_allow_other` in `/etc/fuse.conf`, mount with `--allow-other`, then open
`\\wsl$\<distro>\home\<you>\bambuddy`.

## Container image

Published as `ghcr.io/eh-homelab/scadbuddy-bambuddy-fs` (`sha-<commit>`, `latest`, and
the version on a `v*` tag; amd64 and arm64) by `.github/workflows/build-go-image.yml` on
every push to main that touches Go. Build it from the repo root, since it needs the
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
- a mountpoint uid 10001 can write. Otherwise run it with `--user 0`.

```bash
sudo install -d -o 10001 -g 10001 /mnt/bambuddy
docker run --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined \
  -v /mnt/bambuddy:/mnt/bambuddy:rshared ghcr.io/eh-homelab/scadbuddy-bambuddy-fs \
  mount --api http://scadbuddy/api/v1 --allow-other /mnt/bambuddy
```

`--allow-other` lets processes other than the mounting one, such as the host and other
containers, see the files; the image's `/etc/fuse.conf` sets `user_allow_other` so the
unprivileged user may pass it. `--cache-dir` defaults to `/home/bambuddy/.cache/bambuddy-fs`
inside the container. With no arguments the image prints `mount --help`.

## Flags

```
bambuddy-fs mount --api <url> [--ro] [--all] [--allow-other] [--attr-ttl 5s] [--cache-dir <dir>] [--state-dir <dir>] <mountpoint>
bambuddy-fs unsent [--state-dir <dir>]
```

| Flag | Meaning |
|---|---|
| `--api` | ScadBuddy's API root, like `http://host/api/v1`. Required. |
| `--ro` | Every write fails with `EROFS`. |
| `--all` | List every file. Without it the mount lists what the library dialog lists by default (unsliced 3MFs). |
| `--allow-other` | FUSE `allow_other`: other users and processes see the mount. |
| `--attr-ttl` | How long listings are cached, and the kernel's entry and attribute timeout. Default `5s`. |
| `--cache-dir` | Downloaded contents and files being written. Default `$XDG_CACHE_HOME/bambuddy-fs`, or `~/.cache/bambuddy-fs`. |
| `--state-dir` | Writes whose upload failed, under `unsent/` (see "Unsent writes"). Default `$XDG_STATE_HOME/bambuddy-fs`, or `~/.local/state/bambuddy-fs`. |

`bambuddy-fs --help` and `bambuddy-fs mount --help` print the usage and exit 0; an
unknown command exits 2.

## Names

A folder's contents show under their names. When several files in a folder share a
name, the oldest shows as the plain name and the later ones as `<stem> (id <N>)<ext>`,
so a path is stable for as long as its file exists. Times are Bambuddy's `created_at`.

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

A file's bytes are immutable per ID, so contents are cached on disk under `--cache-dir`
and the first read of an open file streams all of it. Listings are cached for
`--attr-ttl`; every write invalidates its folder.

Writes go to a temporary file, and the upload runs when the descriptor is closed
(`flush`). A close with nothing new to send uploads nothing. `fsync()` makes the
temporary file durable. Writing over an existing name uploads the new file and then
deletes the old one, so the path keeps its name but its file ID changes: Bambuddy has
no in-place replace. Truncating to 0 a file being written is allowed. If the upload
fails, the write is queued (below) and `close()` succeeds.

Without `--all`, a file this mount uploaded that the listing does not show (an app's
`x.3mf.tmp`, say) stays visible until it is renamed, removed or the mount ends, so the
app can still rename or remove it.

## Renames

`rename()` over an existing file replaces it, as on any filesystem, though the library
cannot do it in one step:

- A stored file renamed over another takes the name, then the other is deleted. If that
  delete fails (a queued print uses it), both remain, the answer is `EIO`, and the log
  names both IDs.
- A file not sent yet (still being written, or queued) takes the name at once and
  shadows the stored file there, which it replaces when it is sent (see the conflict
  rule below). A slicer's save, write `x.3mf.tmp`, close, rename over `x.3mf`, works
  whether ScadBuddy is up or not.
- A queued file renamed over another queued file supersedes it; a file still being
  written supersedes it once its own bytes are durable (sent or queued).

`RENAME_NOREPLACE` over anything, a folder over a folder, and anything over a file
still being written are `EEXIST`; a file over a folder is `EISDIR`, a folder over a file
`ENOTDIR`.

## Unsent writes

A write whose upload fails (ScadBuddy down or unreachable, a 5xx, a 404 from an older
ScadBuddy without the upload route, a timeout) is not dropped. Its bytes move to
`<state-dir>/unsent/<n>/contents`, beside a `record.json` (folder ID, name, size, when
it was saved, attempts, last error, and the stored files it replaces), both fsync'd, and
the log names that directory once.

The queue is in the state directory, not the cache directory, because it is the only
copy of what was written: cache cleaners may empty `~/.cache`. One mount at a time uses
a state directory (it holds `<state-dir>/lock`); a second fails to start.

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
| 5xx, timeout, unreachable | `EIO` (an upload that fails so is queued instead, see "Unsent writes") |
| any write on `--ro` | `EROFS` |
| chmod, chown, symlink, link, setting or removing an xattr, truncate of a stored file | `ENOTSUP` |

Reading xattrs answers `ENODATA` and an empty list, so `ls -l` and `cp` stay quiet, and
a change of times only is accepted and ignored. Each error is logged with the operation
and the problem `detail`. A write that answers 202 is followed through
`GET /operations/{id}`; if it is still running when the request's timeout passes, the
result is `EIO` and the log names the operation id.
