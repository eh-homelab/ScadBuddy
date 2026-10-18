#!/bin/sh
# tests/fake-bambu-studio.sh: speaks the CLI's protocol. FAKE_EXIT (default 0), FAKE_SPLIT=1 writes one
# progress line in two chunks with a pause, FAKE_SILENT=1 writes nothing to the pipe, FAKE_HANG=1 sleeps 300 s.
# FAKE_QUIET=<s> sleeps that long, silent, before it writes anything. FAKE_NO_OUTPUT=1 exits 0 without writing
# the output; FAKE_ENV=1 writes its environment to stderr. FAKE_STRAGGLER=1 writes "pgid <its pid>" to stderr
# and leaves a background sleep in its group.
pipe=""; out=""
while [ $# -gt 0 ]; do case "$1" in --pipe) pipe="$2"; shift;; --export-3mf) out="$2"; shift;; esac; shift; done
[ -n "$FAKE_HANG" ] && sleep 300
if [ -n "$FAKE_STRAGGLER" ]; then echo "pgid $$" >&2; sleep 300 & fi
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
