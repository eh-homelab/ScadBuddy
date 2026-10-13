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

A helper that builds from a worktree and mounts at `~/bambuddy` is `~/bin/bambuddy-mount`
(`bambuddy-mount`, `bambuddy-mount umount`, `bambuddy-mount status`; `SCADBUDDY_REPO`,
`BAMBUDDY_FS_API` and `BAMBUDDY_FS_DIR` override its defaults). It adds `--allow-other`
when `/etc/fuse.conf` allows it.

### WSL and Windows

Windows Explorer reaches a WSL mount only when other users may see it. Uncomment
`user_allow_other` in `/etc/fuse.conf`, mount with `--allow-other`, then open
`\\wsl$\<distro>\home\<you>\bambuddy`.

## Container image

Published as `ghcr.io/eh-homelab/scadbuddy-bambuddy-fs`. Mounting in a container needs
`/dev/fuse` and `CAP_SYS_ADMIN`:

```bash
docker run --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined \
  -v /mnt/bambuddy:/mnt/bambuddy:rshared ghcr.io/eh-homelab/scadbuddy-bambuddy-fs \
  mount --api http://scadbuddy/api/v1 --allow-other /mnt/bambuddy
```

`--allow-other` lets processes other than the mounting one, such as the host and other
containers, see the files.

## Flags

```
bambuddy-fs mount --api <url> [--ro] [--all] [--allow-other] [--attr-ttl 5s] [--cache-dir <dir>] <mountpoint>
```

| Flag | Meaning |
|---|---|
| `--api` | ScadBuddy's API root, like `http://host/api/v1`. Required. |
| `--ro` | Every write fails with `EROFS`. |
| `--all` | List every file. Without it the mount lists what the library dialog lists by default (unsliced 3MFs). |
| `--allow-other` | FUSE `allow_other`: other users and processes see the mount. |
| `--attr-ttl` | How long listings are cached, and the kernel's entry and attribute timeout. Default `5s`. |
| `--cache-dir` | Downloaded contents and files being written. Default `$XDG_CACHE_HOME/bambuddy-fs`, or `~/.cache/bambuddy-fs`. |

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
(`flush`), so `close()` returns the upload's error. A close with nothing new to send
uploads nothing. Writing over an existing name uploads the new file and then deletes the
old one, so the path keeps its name but its file ID changes: Bambuddy has no in-place
replace. Truncating to 0 a file being written is allowed.

## Errors

| ScadBuddy answer | errno |
|---|---|
| 404 | `ENOENT` |
| 409 | `EBUSY` |
| 413, or a size cap | `EFBIG` |
| 401/403 (a Bambuddy scope the key lacks) | `EACCES` |
| 5xx, timeout, unreachable | `EIO` |
| any write on `--ro` | `EROFS` |
| chmod, chown, symlink, link, setting or removing an xattr, truncate of a stored file | `ENOTSUP` |

Reading xattrs answers `ENODATA` and an empty list, so `ls -l` and `cp` stay quiet, and
a change of times only is accepted and ignored. Each error is logged with the operation
and the problem `detail`.
