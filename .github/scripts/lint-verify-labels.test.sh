#!/usr/bin/env bash
# Tests for lint-verify-labels.sh (#302). Each case writes one verify.sh into a
# temp models tree and asserts the verdict and the line the lint names. The
# labelled forms must pass, or every failure below could be the lint failing
# on everything.
# shellcheck disable=SC2016 # the single-quoted bodies are verify.sh source, not expanded here.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/lint-verify-labels.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf 'ok   %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$name" "$expected" "$actual"
    fail=$((fail + 1))
  fi
}

# run <verify.sh body>: "<exit>:<file:line of each finding, '|'-joined>".
run() {
  local out status
  rm -rf "$tmp/models"
  mkdir -p "$tmp/models/demo"
  printf '%s\n' "$1" >"$tmp/models/demo/verify.sh"
  set +e
  out="$(MODELS_DIR="$tmp/models" "$script" 2>&1)"
  status=$?
  set -e
  printf '%s:%s' "$status" \
    "$(printf '%s\n' "$out" | grep -oE '^[^ ]+/verify\.sh:[0-9]+' | sed "s|^$tmp/||" | paste -sd'|' -)"
}

shell_ok='docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$IMAGE" fc-list'
py_ok='r = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), IMAGE])'

check 'a labelled shell docker run passes' '0:' "$(run "$shell_ok")"
check 'a labelled Python docker run passes' '0:' "$(run "$py_ok")"
check 'docker build and image inspect are not runs' '0:' \
  "$(run $'docker build -q -t "$IMAGE" -\ndocker image inspect "$IMAGE"')"
check 'a comment that mentions docker run is skipped' '0:' "$(run '  # docker run --rm "$IMAGE" fc-list')"

check 'an unlabelled shell docker run fails' '1:models/demo/verify.sh:2' \
  "$(run $'set -e\ndocker run --rm "$IMAGE" fc-list : family')"
check 'an unlabelled Python docker run fails' '1:models/demo/verify.sh:1' \
  "$(run 'r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", IMAGE])')"
check 'docker container run is a run too' '1:models/demo/verify.sh:1' \
  "$(run 'docker container run --rm "$IMAGE" true')"
check 'a label that is not the per-template one fails' '1:models/demo/verify.sh:1' \
  "$(run 'docker run --rm --label scadbuddy-verify=demo "$IMAGE" true')"
check 'a run inside $( ) is still checked' '1:models/demo/verify.sh:1' \
  "$(run 'log=$(docker run --rm "$IMAGE" openscad --version)')"
check 'a Python argv wrapped one element per line fails' '1:models/demo/verify.sh:2' \
  "$(run $'r = subprocess.run([\n    "docker",\n    "run",\n    "--rm", IMAGE])')"
check 'single-quoted Python argv is a run too' '1:models/demo/verify.sh:1' \
  "$(run "r = subprocess.run(['docker', 'run', '--rm', IMAGE])")"
check 'a docker \ continuation before the subcommand fails' '1:models/demo/verify.sh:1' \
  "$(run $'docker \\\n  run --rm "$IMAGE" true')"
check 'a labelled run whose later arguments wrap passes' '0:' \
  "$(run "$shell_ok"$' \\\n  -v "$PWD":/w')"
check 'every finding is reported, not only the first' \
  '1:models/demo/verify.sh:1|models/demo/verify.sh:3' \
  "$(run $'docker run --rm "$IMAGE" a\n'"$shell_ok"$'\ndocker run --rm "$IMAGE" b')"

check 'the repository templates pass' '0:' \
  "$(cd "$here/../.." && "$script" >/dev/null 2>&1; printf '%s:' "$?")"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
