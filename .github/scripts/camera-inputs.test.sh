#!/usr/bin/env bash
#
# Cases for camera-inputs.sh (#8191): the hash of what a camera image runs moves
# when, and only when, one of its inputs changes. Each case edits a scratch
# copy of the files the script reads. Runs in ci.yml's `lint` job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
script="$here/camera-inputs.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

fresh() {
  rm -rf "$work/repo" && mkdir -p "$work/repo/backend" "$work/repo/deploy"
  cp -r "$repo/backend/scadbuddy" "$work/repo/backend/"
  cp "$repo/backend/pyproject.toml" "$repo/backend/uv.lock" "$work/repo/backend/"
  cp -r "$repo/deploy/camera" "$work/repo/deploy/"
  cp "$repo/Dockerfile" "$work/repo/"
}
hash() { (cd "$work/repo" && bash "$script" "$1"); }

fresh
for part in relay recorder edge; do
  h=$(hash "$part")
  [[ "$h" =~ ^[0-9a-f]{64}$ ]] || fail "$part: not a sha256: $h"
  [ "$h" = "$(hash "$part")" ] || fail "$part: two runs differ"
done
recorder=$(hash recorder) edge=$(hash edge)
[ "$recorder" != "$edge" ] || fail "recorder and edge hash alike"

files=$(cd "$work/repo" && bash "$script" recorder --files)
for want in backend/scadbuddy/camera/recorder.py backend/scadbuddy/camera/edge.py \
  backend/scadbuddy/crud/client.py backend/uv.lock deploy/camera/mediamtx-recorder.yml \
  'Dockerfile#camera-recorder' 'Dockerfile#camera-deps'; do
  grep -qxF "$want" <<<"$files" || fail "--files does not list $want"
done
for unwanted in backend/scadbuddy/api/camera.py backend/scadbuddy/render/__init__.py \
  deploy/camera/mediamtx-edge.yml 'Dockerfile#camera-edge'; do
  grep -qxF "$unwanted" <<<"$files" && fail "--files lists $unwanted"
done

# A backend module the camera does not import: no part moves.
echo "# unrelated" >> "$work/repo/backend/scadbuddy/render/__init__.py"
[ "$(hash recorder)" = "$recorder" ] || fail "an unrelated backend change moved the recorder"
[ "$(hash edge)" = "$edge" ] || fail "an unrelated backend change moved the edge"

# An import inside a module-level `match` case is followed.
fresh
printf '%s\n' 'match 1:' '    case 1:' '        import scadbuddy.render' >> "$work/repo/backend/scadbuddy/camera/recorder.py"
(cd "$work/repo" && bash "$script" recorder --files) | grep -qxF backend/scadbuddy/render/__init__.py \
  || fail "an import in a match case is not followed"

# A camera module, and a shared module it imports: every part moves.
for path in camera/recorder.py crud/client.py; do
  fresh
  echo "# changed" >> "$work/repo/backend/scadbuddy/$path"
  [ "$(hash recorder)" != "$recorder" ] || fail "$path did not move the recorder"
  [ "$(hash edge)" != "$edge" ] || fail "$path did not move the edge"
done

# One part's MediaMTX configuration moves that part only.
fresh
echo "# changed" >> "$work/repo/deploy/camera/mediamtx-edge.yml"
[ "$(hash recorder)" = "$recorder" ] || fail "the edge's configuration moved the recorder"
[ "$(hash edge)" != "$edge" ] || fail "the edge's configuration did not move the edge"

# A dependency lock change moves every part.
fresh
echo "# changed" >> "$work/repo/backend/uv.lock"
[ "$(hash recorder)" != "$recorder" ] || fail "uv.lock did not move the recorder"

# The Dockerfile: another stage changes nothing; a camera stage moves its part.
fresh
sed -i -E 's/^(FROM [^ ]+ AS slicer-build)$/\1\n# changed/' "$work/repo/Dockerfile"
grep -q '^# changed$' "$work/repo/Dockerfile" || fail "test setup: no slicer-build stage to edit"
[ "$(hash recorder)" = "$recorder" ] || fail "a non-camera Dockerfile stage moved the recorder"
fresh
sed -i -E 's/^(FROM camera AS camera-recorder)$/\1\n# changed/' "$work/repo/Dockerfile"
[ "$(hash recorder)" != "$recorder" ] || fail "the camera-recorder stage did not move the recorder"
[ "$(hash edge)" = "$edge" ] || fail "the camera-recorder stage moved the edge"

# A missing stage is an error, never a hash of less.
fresh
sed -i -E 's/^(FROM camera) AS camera-edge$/\1 AS camera-gone/' "$work/repo/Dockerfile"
if (cd "$work/repo" && bash "$script" edge >/dev/null 2>&1); then
  fail "a missing camera-edge stage still hashed"
fi

if (cd "$work/repo" && bash "$script" nope >/dev/null 2>&1); then fail "an unknown part passed"; fi

[ "$failures" -eq 0 ] || { echo "$failures failure(s)" >&2; exit 1; }
echo "camera-inputs: ok"
