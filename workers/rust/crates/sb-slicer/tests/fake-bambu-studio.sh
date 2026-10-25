#!/bin/sh
# tests/fake-bambu-studio.sh: speaks the CLI's protocol. FAKE_EXIT (default 0), FAKE_SPLIT=1 writes one
# progress line in two chunks with a pause, FAKE_SILENT=1 writes nothing to the pipe, FAKE_HANG=1 sleeps 300 s.
# FAKE_QUIET=<s> sleeps that long, silent, before it writes anything. FAKE_NO_OUTPUT=1 exits 0 without writing
# the output; FAKE_ENV=1 writes its environment to stderr. FAKE_STRAGGLER=1 writes "pgid <its pid>" to stderr
# and leaves a background sleep in its group. FAKE_HOLDER=1 leaves a process in a session of its own (out of
# the group's reach) holding stderr open, its pid in holder.pid beside the output (the CLI's environment
# is cleared, so no variable can name the file). FAKE_EMPTY_PLATE=1 does what the real CLI does with an empty
# plate (#4638): its reason in an "[error]" log line on stdout, nothing on stderr, exit 206.
pipe=""; out=""
while [ $# -gt 0 ]; do case "$1" in --pipe) pipe="$2"; shift;; --export-3mf) out="$2"; shift;; esac; shift; done
if [ -n "$FAKE_EMPTY_PLATE" ]; then
  echo "[2026-10-24 20:00:29.080527] [0x0000785ffa373540] [trace]   Initializing StaticPrintConfigs"
  echo "[2026-10-24 20:00:29.080527] [0x0000785ffa373540] [error]   plate 2: Nothing to be sliced, Either the print is empty or no object is fully inside the print volume before apply."
  echo
  echo "run found error, return -50, exit..."
  exit 206
fi
[ -n "$FAKE_HANG" ] && sleep 300
if [ -n "$FAKE_STRAGGLER" ]; then echo "pgid $$" >&2; sleep 300 & fi
if [ -n "$FAKE_HOLDER" ]; then
  # shellcheck disable=SC2016 # $$ and $1 are the held shell's own
  setsid sh -c 'echo held >&2; echo $$ > "$1"; exec sleep 30' sh "$(dirname "$out")/holder.pid" &
  # "held" and the pid are written before the CLI exits, so the test always sees one and can stop it.
  # Bounded (2 s, inside the test's own timeout), so a setsid that fails says so (#4263).
  tries=0
  while [ ! -s "$(dirname "$out")/holder.pid" ]; do
    tries=$((tries + 1))
    [ "$tries" -gt 40 ] && { echo "fake: the setsid holder wrote no pid in 2 s" >&2; exit 3; }
    sleep 0.05
  done
fi
[ -n "$FAKE_QUIET" ] && sleep "$FAKE_QUIET"
if [ -z "$FAKE_SILENT" ]; then
  if [ -n "$FAKE_SPLIT" ]; then
    printf '{"plate_index":0,"plate_count":1,"plate_pe' > "$pipe"; sleep 0.2
    printf 'rcent":40,"total_percent":40,"message":"Slicing"}\n' > "$pipe"
  else
    printf '{"plate_index":0,"plate_count":1,"plate_percent":40,"total_percent":40,"message":"Slicing"}\n' > "$pipe"
  fi
  printf '{"warning":"thin wall"}\n{"plate_index":0,"plate_count":1,"plate_percent":100,"total_percent":100,"message":"Done"}\n' > "$pipe"
fi
[ "${FAKE_EXIT:-0}" = 0 ] && [ -z "$FAKE_NO_OUTPUT" ] && printf 'PK fake 3mf' > "$out"
[ -n "$FAKE_ENV" ] && env >&2
echo "fake stderr line" >&2
exit "${FAKE_EXIT:-0}"
