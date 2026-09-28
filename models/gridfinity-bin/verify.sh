#!/usr/bin/env bash
# Render models/gridfinity-bin with the defaults and the main variations and
# check the results against the Gridfinity dimensions the parameters imply.
#
# The XML checking runs on the host: the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# Render in the image with the ScadBuddy font packages; build it from the base
# image if it is not there yet (same recipe as models/name-keychain).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> $IMAGE missing; building it from $BASE_IMAGE with the image's font packages"
    docker build -q -t "$IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
fi

scad() { docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@"; }

# name | -D overrides separated by ";"
CASES=(
    "defaults|"
    "label|label_text=\"SCREWS M3\""
    "divided|divisions_x=3;divisions_y=2;label_text=\"A\""
    "left-tab|label_tab=\"left\";label_text=\"M4\""
    "no-tab|label_tab=\"none\";label_text=\"ignored\""
    "magnets|magnet_holes=true;screw_holes=true"
    "ultralight|floor_style=\"ultralight\";magnet_holes=true;screw_holes=true"
    "no-lip|units_x=1;units_y=1;height_units=2;stacking_lip=false"
    "max|units_x=7;units_y=7;height_units=12;divisions_x=8;divisions_y=8;floor_style=\"ultralight\";magnet_holes=true;label_text=\"BIG\""
    # 24 wide letters on a 1-unit left tab: shrunk to fit, not clipped.
    "long-label|units_x=1;label_tab=\"left\";label_text=\"WWWWWWWWWWWWWWWWWWWWWWWW\""
    "long-label-divided|units_x=3;divisions_x=3;wall=2.4;label_text=\"Resistors 10k 1/4W\""
)

: > "$OUT/cases.txt"
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    defs=()
    IFS=";" read -ra kvs <<< "${c#*|}"
    for d in "${kvs[@]}"; do defs+=(-D "$d"); done
    start=$(date +%s.%N)
    scad "${defs[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1 \
        || { cat "$OUT/$name.log"; echo "FAIL: $name did not render"; exit 1; }
    if grep -qE '^(WARNING|ERROR)' "$OUT/$name.log"; then
        grep -E '^(WARNING|ERROR)' "$OUT/$name.log"; echo "FAIL: $name rendered with warnings"; exit 1
    fi
    secs=$(echo "$(date +%s.%N) - $start" | bc)
    printf '%s %s %s\n' "$name" "$secs" "${c#*|}" >> "$OUT/cases.txt"
    printf '==> %-10s %5.1fs\n' "$name" "$secs"
done

# Stacking: a second default bin dropped onto the first must seat where the
# foot's 45-degree faces meet the lip's, H - 0.35 = 20.65 mm up. 0.02 mm above
# that the two must not intersect; 0.1 mm below it they must.
echo "==> stacking"
scad -o "$OUT/stack.stl" model.scad >/dev/null 2>&1
for dz in 20.67 20.55; do
    echo "intersection() { import(\"stack.stl\"); translate([0, 0, $dz]) import(\"stack.stl\"); }" > "$OUT/stack.scad"
    log=$(docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD/$OUT":/w -w /w "$IMAGE" openscad --backend=Manifold \
        -o "stack-$dz.stl" stack.scad 2>&1 || true)
    if grep -q "top level object is empty" <<< "$log"; then
        echo "stack $dz empty" >> "$OUT/cases.txt"
    else
        echo "stack $dz solid" >> "$OUT/cases.txt"
    fi
done

python3 - "$OUT" <<'PY'
import math, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
TOL = 0.005

# Gridfinity constants (README cites the sources).
PITCH, GAP, HU = 42.0, 0.5, 7.0
BASE_INSET = 0.8 + 2.15            # foot bottom is 2.95 in from the 41.5 cell
LIP_TOP = 4.4 - 0.6 - 0.6 * math.sqrt(2) + 0.6   # 3.551: filleted lip above H
LIP_SUPPORT = 1.2

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def load(name):
    root = ET.fromstring(zipfile.ZipFile("%s/%s.3mf" % (OUT, name)).read("3D/3dmodel.model"))
    mats = [b.get("name") for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
             for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


cases = {}
stack = {}
for line in open("%s/cases.txt" % OUT):
    parts = line.split(None, 2)
    if parts[0] == "stack":
        stack[parts[1]] = parts[2].strip()
        continue
    params = {}
    for kv in (parts[2].strip().split(";") if len(parts) > 2 and parts[2].strip() else []):
        k, v = kv.split("=", 1)
        params[k] = v.strip('"')
    cases[parts[0]] = (float(parts[1]), params)

for name, (secs, p) in cases.items():
    ux, uy = int(p.get("units_x", 2)), int(p.get("units_y", 1))
    hu = int(p.get("height_units", 3))
    lip = p.get("stacking_lip", "true") == "true"
    tab = p.get("label_tab", "full")
    labelled = p.get("label_text", "") != "" and tab != "none"
    H = hu * HU
    top_z = H - LIP_SUPPORT if lip else H
    wx, wy = ux * PITCH - GAP, uy * PITCH - GAP
    wz = H + (LIP_TOP if lip else 0)

    mats, verts, tris = load(name)
    counts = Counter(t[3] for t in tris)
    xs, ys, zs = zip(*verts)
    print("\n%s (%.1fs): %s" % (name, secs, " ".join("%s=%s" % kv for kv in p.items()) or "defaults"))
    print("  bbox %.3f x %.3f x %.3f   expected %.3f x %.3f x %.3f"
          % (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), wx, wy, wz))

    named = [i for i, n in enumerate(mats) if n != "Default" and counts.get(i)]
    want = 2 if labelled else 1
    check(len(named) == want, "%d non-empty material(s) besides Default (got %d)" % (want, len(named)))
    check(counts.get(0, 0) == 0, "Default material carries no geometry (got %d triangles)" % counts.get(0, 0))
    check(abs(max(xs) - min(xs) - wx) <= TOL, "X = %d x 42 - 0.5 = %.1f" % (ux, wx))
    check(abs(max(ys) - min(ys) - wy) <= TOL, "Y = %d x 42 - 0.5 = %.1f" % (uy, wy))
    check(abs(max(zs) - min(zs) - wz) <= TOL,
          "Z = %d x 7%s = %.3f" % (hu, " + 3.551 lip" if lip else "", wz))
    check(abs(min(zs)) <= TOL, "sits on z=0 (min z %.4f)" % min(zs))
    check(wx <= 300 and wy <= 320, "fits the H2C bed, 300 x 320 with both nozzles (%.1f x %.1f)" % (wx, wy))

    # Foot bottoms: the z=0 footprint is (n-1) pitches plus one 35.6 mm foot.
    z0 = [v for v in verts if abs(v[2]) <= TOL]
    fx = max(v[0] for v in z0) - min(v[0] for v in z0)
    fy = max(v[1] for v in z0) - min(v[1] for v in z0)
    ex, ey = (ux - 1) * PITCH + PITCH - GAP - 2 * BASE_INSET, (uy - 1) * PITCH + PITCH - GAP - 2 * BASE_INSET
    check(abs(fx - ex) <= TOL and abs(fy - ey) <= TOL,
          "foot bottoms span %.2f x %.2f (35.6 mm per foot; got %.2f x %.2f)" % (ex, ey, fx, fy))

    if labelled:
        li = named[1]
        lz = {round(verts[v][2], 3) for t in tris if t[3] == li for v in t[:3]}
        check(lz == {round(top_z, 3)},
              "label's exposed face is the tab top at z=%.2f (got %s)" % (top_z, sorted(lz)))
        # The label stays on the back-left tab's flat top.
        wall = float(p.get("wall", 1.2))
        nx, ny = int(p.get("divisions_x", 1)), int(p.get("divisions_y", 1))
        ix, iy = wx - 2 * wall, wy - 2 * wall
        cw, cd = (ix - (nx - 1) * 1.2) / nx, (iy - (ny - 1) * 1.2) / ny
        tab_w = min(42, cw) if tab == "left" else cw
        tx0, tx1 = -ix / 2 + 1, -ix / 2 + tab_w - 1
        ty0 = iy / 2 - min(15.85, cd / 2) + 1
        ty1 = iy / 2 - ((2.6 - wall + 0.5) if lip else 1)
        lv = [verts[v] for t in tris if t[3] == li for v in t[:3]]
        lx0, lx1 = min(v[0] for v in lv), max(v[0] for v in lv)
        ly0, ly1 = min(v[1] for v in lv), max(v[1] for v in lv)
        check(lx0 >= tx0 - TOL and lx1 <= tx1 + TOL and ly0 >= ty0 - TOL and ly1 <= ty1 + TOL,
              "label x %.2f..%.2f y %.2f..%.2f within the tab x %.2f..%.2f y %.2f..%.2f"
              % (lx0, lx1, ly0, ly1, tx0, tx1, ty0, ty1))
        # Every letter is there: a clipped label loses its outer letters.
        text = p["label_text"]
        if len(set(text)) == 1 and text[0] in "WMVX":
            parent = {}
            def find(a):
                while parent.setdefault(a, a) != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a
            for t in tris:
                if t[3] == li:
                    for v in t[1:3]:
                        parent[find(t[0])] = find(v)
            n = len({find(v) for t in tris if t[3] == li for v in t[:3]})
            check(n == len(text), "all %d letters present, none clipped (got %d)" % (len(text), n))

    # Holes: a ring of vertices at the hole depth, centred +/-13 mm from a cell centre.
    cx, cy = -(ux - 1) / 2 * PITCH + 13, -(uy - 1) / 2 * PITCH + 13
    def ring(z, r):
        return [v for v in verts if abs(v[2] - z) <= TOL
                and abs(math.hypot(v[0] - cx, v[1] - cy) - r) <= 0.01]
    if p.get("magnet_holes") == "true":
        check(len(ring(2.4, 3.25)) >= 8, "6.5 mm magnet hole 2.4 deep at (+13, +13) from the cell centre")
    if p.get("screw_holes") == "true":
        check(len(ring(6.0, 1.5)) >= 8, "3 mm screw hole 6 deep at (+13, +13) from the cell centre")
    if name == "max":
        check(secs < 60, "7x7x12 renders in under 60 s (%.1fs)" % secs)

print("\nstacking (second default bin on top):")
check(stack.get("20.67") == "empty", "0.02 mm above the seat (z=20.67) the bins do not intersect")
check(stack.get("20.55") == "solid", "0.1 mm below the seat (z=20.55) they do, so the seat is at H - 0.35")

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK")
PY
