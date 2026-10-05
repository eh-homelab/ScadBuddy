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
if ! docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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
render() {  # render <out.3mf> [-D ...]: fails on OpenSCAD warnings
    local out=$1 log
    shift
    log=$(docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "$@" -o "$out" model.scad 2>&1) \
        || { echo "$log"; echo "FAIL: $out did not render"; exit 1; }
    if grep -qE '^(WARNING|ERROR)' <<<"$log"; then
        grep -E '^(WARNING|ERROR)' <<<"$log"; echo "FAIL: $out rendered with warnings"; exit 1
    fi
}
render "$OUT/out.3mf" -D 'name="Reagan"'

# Edge cases: name | -D overrides separated by ";". Each must render cleanly,
# fit the bed, and keep the base / letter heights.
EDGE=(
    "long-script|name=\"Maximiliana Wolfgang\";text_size=40"
    "long-wide|name=\"WWWWWWWWWWWWWWWWWWWW\";text_size=40;font=\"DejaVu Sans:style=Bold\""
    "no-hole-small|name=\"Jo\";hole=false;text_size=8;outline=1"
    "big-ring|name=\"Sam\";hole_diameter=8;ring_wall=4;base_thickness=8;letter_height=5"
    "empty|name=\"\""
    "two-words|name=\"Ann Lee\""
    "wide-gap|name=\"Ann    Lee\""
    "missing-glyph|name=\"Zoë 🦄 ß\""
    "leading-gap|name=\"   Ann\""
    "leading-missing-glyph|name=\"🦄 Zoë\""
)
: > "$OUT/edge.txt"
for c in "${EDGE[@]}"; do
    name="${c%%|*}"
    defs=()
    IFS=";" read -ra kvs <<< "${c#*|}"
    for d in "${kvs[@]}"; do defs+=(-D "$d"); done
    render "$OUT/edge-$name.3mf" "${defs[@]}"
    printf '%s %s\n' "$name" "${c#*|}" >> "$OUT/edge.txt"
done

echo "==> preview"
if docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" \
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
PY

python3 - "$OUT" <<'PY'
import sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
BED_X, BED_Y, TOL = 300, 320, 0.001
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


for line in open("%s/edge.txt" % OUT):
    name, _, rest = line.strip().partition(" ")
    p = dict(kv.split("=", 1) for kv in rest.split(";"))
    base = float(p.get("base_thickness", 4))
    top = base + float(p.get("letter_height", 2.8))
    empty = p.get("name") == '""'
    root = ET.fromstring(zipfile.ZipFile("%s/edge-%s.3mf" % (OUT, name)).read("3D/3dmodel.model"))
    mats = [b.get("name") for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
             for v in root.iter(NS + "vertex")]
    counts = Counter(int(t.get("p1") or 0) for t in root.iter(NS + "triangle"))
    xs, ys, zs = zip(*verts)
    print("\n%s: %s" % (name, rest))
    named = [i for i, n in enumerate(mats) if n != "Default" and counts.get(i)]
    want = 1 if empty else 2
    check(len(named) == want, "%d non-empty material(s) besides Default (got %d)" % (want, len(named)))
    check(counts.get(0, 0) == 0, "Default material carries no geometry")
    check(max(xs) - min(xs) <= BED_X and max(ys) - min(ys) <= BED_Y,
          "fits the H2C bed, %d x %d with both nozzles (%.1f x %.1f)"
          % (BED_X, BED_Y, max(xs) - min(xs), max(ys) - min(ys)))
    check(abs(min(zs)) <= TOL, "sits on z=0 (min z %.3f)" % min(zs))
    want_top = base if empty else top
    check(abs(max(zs) - want_top) <= TOL, "top at z=%.1f (got %.3f)" % (want_top, max(zs)))

    # One piece (#920): a space or a glyph the font lacks must not leave part
    # of the word on a base island that falls off the keyring. Union-find over
    # the shared vertex indices of every triangle, both materials: the base
    # inside a letter's counter touches only letter triangles, so the base
    # material alone would count each counter as an island.
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in root.iter(NS + "triangle"):
        a, b, c = (int(t.get(k)) for k in ("v1", "v2", "v3"))
        for v in (a, b, c):
            parent.setdefault(v, v)
        for u, w in ((a, b), (b, c)):
            ru, rw = find(u), find(w)
            if ru != rw:
                parent[ru] = rw
    comps = len({find(v) for v in parent})
    check(comps == 1, "prints as one connected piece (%d component(s))" % comps)

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK")
PY
