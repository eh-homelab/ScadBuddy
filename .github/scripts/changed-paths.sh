#!/usr/bin/env bash
# Prints the paths this change touches, one per line, for the selectors
# (select-go.sh, select-lanes.sh). A pull_request reads its merge commit against
# the base (checkout with fetch-depth: 2); a merge_group reads its commit against
# QUEUE_BASE. Exits non-zero when there is no diff to read: a push, the schedule,
# a dispatch, or a diff that cannot be computed. The caller then checks everything.
set -euo pipefail
case "${EVENT_NAME:-}" in
  pull_request)
    git rev-parse -q --verify 'HEAD^2' >/dev/null
    git diff --name-only 'HEAD^1' HEAD
    ;;
  merge_group)
    test -n "${QUEUE_BASE:-}"
    git fetch -q --depth=1 origin "$QUEUE_BASE"
    git diff --name-only "$QUEUE_BASE" HEAD
    ;;
  *)
    exit 1
    ;;
esac
