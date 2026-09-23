#!/usr/bin/env bash
# Render models/name-keychain with the default parameters and check the result
# against the reference keychain that printed on 2026-09-21.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="Lobster Two"

mkdir -p "$OUT"

# The model's default face comes from the Debian trixie font packages the
# ScadBuddy image installs. openscad/openscad:dev ships DejaVu only, so if the
# family is missing, derive a throwaway image that has it -- otherwise OpenSCAD
# silently falls back to DejaVu Sans and every measurement below is meaningless.
IMAGE="$BASE_IMAGE"
if ! docker run --rm "$BASE_IMAGE" fc-list : family | grep -qF "$FONT_FAMILY"; then
    echo "==> $BASE_IMAGE has no '$FONT_FAMILY'; building $FONTS_IMAGE with the image's font packages"
    docker build -q -t "$FONTS_IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
    IMAGE="$FONTS_IMAGE"
fi

echo "==> rendering with $IMAGE"
docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
    openscad --backend=Manifold -D 'name="Reagan"' -o "$OUT/out.3mf" model.scad

echo "==> preview"
if docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
    openscad --backend=Manifold -D 'name="Reagan"' \
    --imgsize=1400,520 --viewall --autocenter --projection=o \
    --colorscheme=Tomorrow -o "$OUT/preview.png" model.scad >/dev/null 2>&1; then
    echo "    $OUT/preview.png"
else
    echo "    no GL context in the image -- preview skipped"
fi

python3 - "$OUT/out.3mf" <<'PY'
import sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"

# Reference: MakerWorld "Name Keychain (Font Basic)" as printed 2026-09-21,
# measured from 3D/Objects/object_1.model of its 3MF, for the name "Raegan".
REF_X, REF_Y, REF_Z = 95.7, 34.6, 6.8
TOL_XY, TOL_Z = 1.5, 0.001
BASE_THICKNESS, LETTER_HEIGHT = 4.0, 2.8

root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
mats = [(b.get("name"), (b.get("displaycolor") or "")[:7]) for b in root.iter(NS + "base")]
verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
         for v in root.iter(NS + "vertex")]
tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
        for t in root.iter(NS + "triangle")]

xs, ys, zs = zip(*verts)
dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
counts = Counter(t[3] for t in tris)

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


print("materials:")
for i, (n, col) in enumerate(mats):
    print("  [%d] %-9s %-8s triangles=%d" % (i, n, col, counts.get(i, 0)))

print("bounding box: X %.3f  Y %.3f  Z %.3f mm   (reference %.1f x %.1f x %.1f)"
      % (dx, dy, dz, REF_X, REF_Y, REF_Z))
print("z range: %.3f .. %.3f" % (min(zs), max(zs)))

per_mat = defaultdict(set)
for t in tris:
    if counts.get(t[3]):
        per_mat[t[3]].update(t[:3])
for i in sorted(per_mat):
    mz = [verts[v][2] for v in per_mat[i]]
    print("  material %d z %.3f .. %.3f" % (i, min(mz), max(mz)))

print("checks:")
named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
check(len(named) == 2,
      "exactly two non-empty materials besides Default (got %d)" % len(named))
check(counts.get(0, 0) == 0,
      "Default material carries no geometry (got %d triangles)" % counts.get(0, 0))
check(abs(dx - REF_X) <= TOL_XY,
      "X %.3f within +/-%.1f of %.1f (delta %+.3f)" % (dx, TOL_XY, REF_X, dx - REF_X))
check(abs(dy - REF_Y) <= TOL_XY,
      "Y %.3f within +/-%.1f of %.1f (delta %+.3f)" % (dy, TOL_XY, REF_Y, dy - REF_Y))
check(abs(dz - REF_Z) <= TOL_Z, "Z %.3f == %.1f exactly" % (dz, REF_Z))
check(abs(min(zs)) <= TOL_Z, "model sits on z=0 (min z %.3f)" % min(zs))

if len(named) == 2:
    base_i, text_i = named
    bz = [verts[v][2] for v in per_mat[base_i]]
    tz = [verts[v][2] for v in per_mat[text_i]]
    check(abs(min(bz)) <= TOL_Z and abs(max(bz) - BASE_THICKNESS) <= TOL_Z,
          "base is %.1f mm thick, 0 .. %.1f (got %.3f .. %.3f)"
          % (BASE_THICKNESS, BASE_THICKNESS, min(bz), max(bz)))
    check(abs(min(tz) - BASE_THICKNESS) <= TOL_Z
          and abs(max(tz) - BASE_THICKNESS - LETTER_HEIGHT) <= TOL_Z,
          "letters are %.1f mm proud, %.1f .. %.1f (got %.3f .. %.3f)"
          % (LETTER_HEIGHT, BASE_THICKNESS, REF_Z, min(tz), max(tz)))

    # Letters must be one connected piece: union-find over shared vertex
    # indices of the letter material's triangles.
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for t in tris:
        if t[3] != text_i:
            continue
        for v in t[:3]:
            parent.setdefault(v, v)
        union(t[0], t[1])
        union(t[1], t[2])
    comps = len({find(v) for v in parent})
    check(comps == 1, "letters are one connected piece (%d component(s))" % comps)

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK")
PY
