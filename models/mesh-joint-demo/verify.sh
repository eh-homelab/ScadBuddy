#!/usr/bin/env bash
# Render models/mesh-joint-demo with the defaults and with the leaves moved, and check
# each 3MF has the two colours, one orange piece (the body) and two green ones (the neck
# stub with its peg, and the leaves). A peg that came loose is an extra piece. Then check
# leaves_x moved the leaves: one green piece 20 mm further along X, the other where it was
# (#2633), so an exposure that compiles but moves nothing fails.
# With SCADBUDDY_PIPELINE_IMAGE set, also check model.scad is what scene.json compiles to.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

CASES=(
    'defaults|'
    'leaves-far|leaves_x=90'
)

status=0
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    defs="${c#*|}"
    args=()
    [ -n "$defs" ] && args+=(-D "$defs")
    echo "==> $name ${defs:-(defaults)}"
    docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$BASE_IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad \
        >"$OUT/$name.log" 2>&1 || { echo "  FAIL  openscad exited non-zero (see $OUT/$name.log)"; status=1; continue; }
    if grep -E 'WARNING|ERROR' "$OUT/$name.log"; then
        echo "  FAIL  OpenSCAD warnings (see $OUT/$name.log)"; status=1
    fi
    python3 - "$OUT/$name.3mf" "$OUT/$name.green.json" <<'PY' || status=1
import json, sys, zipfile, xml.etree.ElementTree as ET
from collections import defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
ORANGE, GREEN = "#F08A24", "#3A7D2C"

root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
        for t in root.iter(NS + "triangle")]
xs = [float(v.get("x")) for v in root.iter(NS + "vertex")]

fails = []
def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        fails.append(msg)

def components(material):
    """Each piece of one material (its triangles joined by shared vertices), as its
    vertices."""
    parent = {}
    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for v1, v2, v3, p in tris:
        if p == material:
            parent[find(v2)] = find(v1)
            parent[find(v3)] = find(v1)
    groups = defaultdict(list)
    for v in list(parent):
        groups[find(v)].append(v)
    return list(groups.values())

def pieces(material):
    return len(components(material))

by_colour = defaultdict(list)
for i, (n, colour) in enumerate(mats):
    if n != "Default" and any(t[3] == i for t in tris):
        by_colour[colour].append(i)
check(sorted(by_colour) == sorted([ORANGE, GREEN]),
      "colours %s (got %s)" % (", ".join(sorted([ORANGE, GREEN])), ", ".join(sorted(by_colour))))
dflt = sum(1 for t in tris if mats[t[3]][0] == "Default")
check(dflt == 0, "Default material carries no geometry (got %d triangles)" % dflt)
for colour, want in ((ORANGE, 1), (GREEN, 2)):
    got = sum(pieces(i) for i in by_colour.get(colour, []))
    check(got == want, "%s has %d piece(s) (got %d)" % (colour, want, got))

# Each green piece's X extent, for the comparison of the cases below.
extents = sorted(
    [min(xs[v] for v in piece), max(xs[v] for v in piece)]
    for i in by_colour.get(GREEN, []) for piece in components(i)
)
with open(sys.argv[2], "w") as f:
    json.dump(extents, f)

sys.exit(1 if fails else 0)
PY
done

echo "==> leaves_x moves the leaves"
python3 - "$OUT/defaults.green.json" "$OUT/leaves-far.green.json" <<'PY' || status=1
import json, sys

MOVED = 90 - 70  # leaves-far's leaves_x less model.scad's default
try:
    before, after = (json.load(open(p)) for p in sys.argv[1:3])
except (OSError, ValueError) as e:
    print("  FAIL  no green extents to compare (%s)" % e)
    sys.exit(1)
near = lambda a, b: all(abs(x - y) < 0.01 for x, y in zip(a, b))
kept = [p for p in before if any(near(p, q) for q in after)]
moved = [p for p in before if any(near([p[0] + MOVED, p[1] + MOVED], q) for q in after)]
ok = len(before) == len(after) == 2 and len(kept) == 1 and len(moved) == 1
print(("  PASS  " if ok else "  FAIL  ")
      + "one green piece %d mm further along X, the other kept (before %s, after %s)" % (MOVED, before, after))
sys.exit(0 if ok else 1)
PY

if [ -n "${SCADBUDDY_PIPELINE_IMAGE:-}" ]; then
    echo "==> pipeline check"
    docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD/../..":/repo -w /repo "$SCADBUDDY_PIPELINE_IMAGE" \
        python -m scadbuddy.meshmodel.check models/mesh-joint-demo || { echo "  FAIL  pipeline check"; status=1; }
else
    echo "pipeline check: skipped"
fi

if [ "$status" -ne 0 ]; then
    echo; echo "FAILED"; exit 1
fi
echo; echo "OK"
