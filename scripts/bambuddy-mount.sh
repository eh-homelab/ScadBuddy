#!/usr/bin/env bash
# Mount Bambuddy's library (through ScadBuddy's API) as a FUSE filesystem (#2686).
#
#   bambuddy-mount [mount] [--ro] [--api URL] [--dir DIR] [-- extra bambuddy-fs flags]
#   bambuddy-mount umount [--dir DIR]
#   bambuddy-mount status [--dir DIR]
#   bambuddy-mount build
#
# Lives in the repo as scripts/bambuddy-mount.sh; copy or symlink it onto PATH, e.g.
#   ln -s "$PWD/scripts/bambuddy-mount.sh" ~/bin/bambuddy-mount
#
# Env: BAMBUDDY_FS_API (default the deployed ScadBuddy), BAMBUDDY_FS_DIR (default
# ~/bambuddy), SCADBUDDY_REPO (a checkout holding tools/go/bambuddy-fs; default: the
# checkout this script lives in, else the first worktree of ~/repos/eh-homelab/ScadBuddy
# that has it).
set -euo pipefail

API="${BAMBUDDY_FS_API:-https://scadbuddy.internal.nullreference.io/api/v1}"
DIR="${BAMBUDDY_FS_DIR:-$HOME/bambuddy}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/bambuddy-fs"
BIN="$STATE/bambuddy-fs"
LOG="$STATE/mount.log"
PIDFILE="$STATE/mount.pid"

die() { echo "bambuddy-mount: $*" >&2; exit 1; }

find_repo() {
  if [[ -n "${SCADBUDDY_REPO:-}" ]]; then echo "$SCADBUDDY_REPO"; return; fi
  local wt here
  here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
  if [[ -d "$here/tools/go/bambuddy-fs" ]]; then echo "$here"; return; fi
  while read -r wt; do
    if [[ -d "$wt/tools/go/bambuddy-fs" ]]; then echo "$wt"; return; fi
  done < <(git -C "$HOME/repos/eh-homelab/ScadBuddy" worktree list --porcelain | sed -n 's/^worktree //p')
  die "no ScadBuddy checkout has tools/go/bambuddy-fs yet (set SCADBUDDY_REPO)"
}

build() {
  local repo; repo=$(find_repo)
  mkdir -p "$STATE"
  echo "building bambuddy-fs from $repo" >&2
  (cd "$repo" && go build -o "$BIN" ./tools/go/bambuddy-fs)
}

mounted() { mountpoint -q "$DIR"; }

# The running mount's pid, if its process is still alive.
running() {
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
  kill -0 "$pid" 2>/dev/null && echo "$pid"
}

# fusermount3 -u alone can leave the process serving: on WSL, Windows reaches the
# mount through \\wsl$ from another mount namespace, whose copy of the mount keeps the
# FUSE connection open. Wait for the process, then SIGTERM it (it stops its retrier and
# exits; unsent writes are on disk), then SIGKILL as the last resort.
stop() {
  local pid
  pid=$(running) || { rm -f "$PIDFILE"; return 0; }
  for sig in "" TERM KILL; do
    [[ -n "$sig" ]] && kill -s "$sig" "$pid" 2>/dev/null
    for _ in $(seq 100); do
      kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; return 0; }
      sleep 0.1
    done
    echo "bambuddy-mount: pid $pid still running${sig:+ after SIG$sig}" >&2
  done
  die "pid $pid would not exit"
}

cmd=mount
case "${1:-}" in mount|umount|unmount|status|build) cmd=$1; shift ;; esac

ro=() extra=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ro) ro=(--ro); shift ;;
    --api) API="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --) shift; extra=("$@"); break ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$cmd" in
  build) build ;;
  status)
    if mounted; then echo "mounted at $DIR"; else echo "not mounted ($DIR)"; exit 1; fi ;;
  umount|unmount)
    if mounted; then fusermount3 -u "$DIR"; fi
    stop
    echo "unmounted $DIR" ;;
  mount)
    mounted && die "already mounted at $DIR (bambuddy-mount umount first)"
    if pid=$(running); then die "pid $pid from an earlier mount is still running (bambuddy-mount umount stops it)"; fi
    [[ -e /dev/fuse ]] || die "/dev/fuse is missing"
    command -v fusermount3 >/dev/null || die "install fuse3 (sudo apt install fuse3)"
    build
    mkdir -p "$DIR"
    allow=()
    # Windows Explorer (\\wsl$) and other users only see the mount with allow_other,
    # which FUSE permits a non-root user only when /etc/fuse.conf says so.
    if grep -qx 'user_allow_other' /etc/fuse.conf 2>/dev/null; then
      allow=(--allow-other)
    else
      echo "note: uncomment user_allow_other in /etc/fuse.conf to see the mount from Windows" >&2
    fi
    nohup "$BIN" mount --api "$API" --all "${ro[@]}" "${allow[@]}" "${extra[@]}" "$DIR" >"$LOG" 2>&1 &
    pid=$!
    echo "$pid" >"$PIDFILE"
    for _ in $(seq 50); do
      mounted && { echo "mounted $API at $DIR (pid $pid, log $LOG)"; exit 0; }
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    cat "$LOG" >&2
    die "mount did not come up"
    ;;
esac
