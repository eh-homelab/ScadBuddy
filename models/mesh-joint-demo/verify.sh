#!/usr/bin/env bash
# Render models/mesh-joint-demo with the defaults and with the leaves moved, and check
# each 3MF has the two colours, one orange piece (the body) and two green ones (the neck
# stub with its peg, and the leaves). A peg that came loose is an extra piece.
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
    python3 - "$OUT/$name.3mf" <<'PY' || status=1
import sys, zipfile, xml.etree.ElementTree as ET
from collections import defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
ORANGE, GREEN = "#F08A24", "#3A7D2C"

root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
        for t in root.iter(NS + "triangle")]

fails = []
def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        fails.append(msg)

def pieces(material):
    """Triangles of one material joined by shared vertices."""
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
    return len({find(v) for v in list(parent)})

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

sys.exit(1 if fails else 0)
PY
done

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
