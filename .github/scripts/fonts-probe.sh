#!/usr/bin/env bash
#
# Decide whether the install-on-demand font test can run, and say why when it
# cannot. Writes `offline=` or `offline=1` to stdout and, when it is set, to
# $GITHUB_OUTPUT; ci.yml passes that to the Playwright step as E2E_OFFLINE.
#
# WHY THIS EXISTS. `frontend/e2e/real-backend.spec.ts`'s second test is the only
# thing in this repository that depends on a service outside it, and it sits
# behind `CI Summary`, the one required check. Unguarded, a Google-side outage
# reds somebody else's unrelated PR; guarded carelessly, every real regression
# in the font pipeline turns into a quiet skip, which is worse. Four rules keep
# both away:
#
#   1. Only a DEPENDENCY being unreachable skips the test. Anything this
#      application does wrong lets the test run and red.
#   2. Reachability is measured against the upstreams THEMSELVES, never
#      inferred from our own route's status. `api/fonts.py` maps every
#      `GoogleFontsError` to 503, and that class covers a schema drift in
#      Google's undocumented payload and an empty catalogue as well as a dead
#      socket — so "our route said 503" cannot tell an outage from a bug.
#   3. Nothing about the upstreams is written down here. The URLs and the
#      timeouts are read out of the running container, so a change in the
#      Python cannot leave a second, stale copy of itself in CI.
#   4. Every skip is a ::warning:: that names what was not proved. A skip that
#      nobody reads is the failure this guard exists to prevent.
#
# Its branches are covered by fonts-probe.test.sh, which runs in the `lint` job.
set -euo pipefail

CONTAINER="${CONTAINER:-scadbuddy-ci}"

# Every call into the container goes through here so the tests can substitute a
# stub for the whole container without docker.
exec_in() {
  if [ -n "${PROBE_EXEC:-}" ]; then
    "$PROBE_EXEC" "$@"
  else
    docker exec "$CONTAINER" "$@"
  fi
}

# `000` is what curl's write-out prints when it never got a response at all, so
# it covers DNS, TLS and connect failures as well as our own --max-time.
unreachable() {
  case "$1" in
    ''|*[!0-9]*) return 0 ;;
    000) return 0 ;;
    *) [ "$1" -ge 500 ] ;;
  esac
}

status_of() {
  exec_in curl --silent --show-error --max-time "$budget" -o /dev/null -w '%{http_code}' "$1" || true
}

# One read, three answers: the budget is the sum of BOTH timeouts the catalogue
# route can spend — the httpx request and the `fc-list` subprocess it runs
# through asyncio.to_thread — plus headroom, so a merely slow stack is not
# mistaken for a dead one.
info="$(exec_in python -c 'from scadbuddy.library.fonts import FC_TIMEOUT
from scadbuddy.library.googlefonts import DEFAULT_TIMEOUT, METADATA_URL, REPO_BASE_URL
print(int(DEFAULT_TIMEOUT) + int(FC_TIMEOUT) + 10)
print(METADATA_URL)
print(REPO_BASE_URL)' 2>/dev/null || true)"

budget="$(printf '%s\n' "$info" | sed -n 1p)"
metadata_url="$(printf '%s\n' "$info" | sed -n 2p)"
repo_url="$(printf '%s\n' "$info" | sed -n 3p)"

# One test for all three, because they come from one read: if any is missing or
# the budget is not a number, the read did not work and none of them is trusted.
usable=1
case "$budget" in
  ''|*[!0-9]*) usable= ;;
esac
if [ -z "$metadata_url" ] || [ -z "$repo_url" ]; then
  usable=
fi
if [ -z "$usable" ]; then
  budget=70
  metadata_url="https://fonts.google.com/metadata/fonts"
  repo_url="https://raw.githubusercontent.com/google/fonts/main"
  echo "::warning::Could not read the font URLs and timeouts out of the container, so the probe is using its own copies (${budget}s, ${metadata_url}, ${repo_url}). Those can be stale — check them against backend/scadbuddy/library/googlefonts.py if this warning persists."
fi
echo "probe budget: ${budget}s (googlefonts.DEFAULT_TIMEOUT + fonts.FC_TIMEOUT + 10)"

offline=
catalogue_status="$(status_of "$metadata_url")"
if unreachable "$catalogue_status"; then
  offline=1
  echo "::warning::${metadata_url} did not answer from the container (status ${catalogue_status}), so the install-on-demand test (#82's acceptance) is being SKIPPED, not run. An air-gapped stack is supported, so this is not a failure — but nothing proved that path today."
else
  repo_status="$(status_of "$repo_url")"
  if unreachable "$repo_status"; then
    offline=1
    echo "::warning::${repo_url}, where the font files are downloaded from, did not answer from the container (status ${repo_status}), so the install-on-demand test (#82's acceptance) is being SKIPPED, not run. The catalogue upstream answered, so this is the download host alone."
  else
    # Both dependencies answer, so from here on nothing skips: whatever is
    # wrong is ours, and the test is the right place for it to surface.
    answer="$(exec_in curl --silent --show-error --max-time "$budget" -w '%{http_code}' \
                "http://127.0.0.1:8080/api/v1/fonts/catalogue?q=Pacifico&limit=1" || true)"
    route_status="${answer: -3}"
    body="${answer%???}"
    case "$route_status" in
      ''|*[!0-9]*) route_status=000 ;;
    esac

    if [ "$route_status" != 200 ]; then
      echo "::warning::Both font upstreams answer, but this app's own /api/v1/fonts/catalogue returned ${route_status}. That is ScadBuddy failing, not an outage — note that its 503 also covers a schema drift or an empty catalogue, which is why reachability is measured upstream and not here. NOT a skip: the install-on-demand test will RUN and is expected to fail."
    elif ! printf '%s' "$body" | grep -q '"family":"Pacifico"'; then
      echo "::warning::The catalogue answered but does not name Pacifico. This is NOT an outage, so the install-on-demand test will RUN and is expected to fail on its missing row — an upstream removal or a regression in this repo's catalogue search is a red, not a skip."
    fi
  fi
fi

echo "offline=${offline}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "offline=${offline}" >> "$GITHUB_OUTPUT"
fi
