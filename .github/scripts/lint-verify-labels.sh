#!/usr/bin/env bash
#
# Fail when a `docker run` in a models/*/verify.sh does not carry the
# per-template label (#302):
#
#   shell:   docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" ...
#   python:  ["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), ...]
#
# verify-models.sh sets SCADBUDDY_VERIFY_LABEL to the slug and, when a template
# times out, force-removes its containers by that label. An unlabelled
# container that mounts nothing (the `fc-list` font probe, say) would outlive
# the timeout and keep burning the runner's cores.
#
# The rule is per physical line: the line that starts the `docker run` (the
# shell form or the Python argv form) must name both `scadbuddy-verify=` and
# `SCADBUDDY_VERIFY_LABEL`, so a hard-coded value that no timeout would ever
# reap by does not pass. Because the check is per line, a `docker` whose
# subcommand is on the NEXT line (a `docker \` continuation, or a Python argv
# list wrapped one element per line) fails too: a per-line match could not
# see that it is a run, let alone whether it is labelled. Comment lines are
# skipped.
#
# It is a text check, not a parser: it only recognises a LITERAL `docker run`
# (or `"docker", "run"`) on one line. A run whose argv is assembled at run time
# (`cmd = ["docker"]; cmd += ["run", ...]`, `docker "$SUB" ...`) slips past it,
# and if that container mounts nothing the reaper's bind-mount fallback misses
# it too. Keep every `docker run` in a verify.sh literal, and review new
# templates for that shape. Its cases are in lint-verify-labels.test.sh; both run in ci.yml's
# `lint` job.
set -euo pipefail

MODELS_DIR="${MODELS_DIR:-models}"

q="[\"']"
run_re="(^|[^[:alnum:]_-])docker[[:space:]]+(container[[:space:]]+)?run([[:space:]]|\$)|${q}docker${q},[[:space:]]*(${q}container${q},[[:space:]]*)?${q}run${q}"
# `docker` (or `docker container`) as the last thing on a line: its
# subcommand follows on another line.
split_re="(^|[^[:alnum:]_-])docker([[:space:]]+container)?[[:space:]]*\\\\\$|${q}docker${q}[[:space:]]*(,[[:space:]]*${q}container${q}[[:space:]]*)?,?[[:space:]]*\$"

bad=0
checked=0
for f in "$MODELS_DIR"/*/verify.sh; do
  [ -f "$f" ] || continue
  checked=$((checked + 1))
  n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ $split_re ]]; then
      echo "$f:$n: docker command split across lines; keep 'docker run' and its --label on one line"
      echo "    $line"
      bad=$((bad + 1))
      continue
    fi
    [[ "$line" =~ $run_re ]] || continue
    if [[ "$line" != *scadbuddy-verify=* || "$line" != *SCADBUDDY_VERIFY_LABEL* ]]; then
      echo "$f:$n: docker run without --label scadbuddy-verify=\$SCADBUDDY_VERIFY_LABEL"
      echo "    $line"
      bad=$((bad + 1))
    fi
  done <"$f"
done

if [ "$bad" -ne 0 ]; then
  echo "$bad unlabelled docker run(s). verify-models.sh cannot reap them after a timeout (#302)."
  exit 1
fi
echo "lint-verify-labels: every docker run in $checked verify.sh script(s) is labelled."
