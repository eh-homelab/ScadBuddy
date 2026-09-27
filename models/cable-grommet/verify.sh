#!/usr/bin/env bash
# Render models/cable-grommet with the defaults and each cap style and shape,
# and check the 3MFs: material count, no uncoloured geometry, plate layout and
# heights against the parameters, sits on z=0, and the two fits (sleeve in the
# hole, cap lip in the sleeve) at the sizes `fit` implies.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"

mkdir -p "$OUT"

# Without the default face OpenSCAD silently falls back to another font, so
# derive an image that has the ScadBuddy image's font packages when the base
# one lacks it.
IMAGE="$BASE_IMAGE"
if ! docker run --rm "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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

render() {
    local name="$1"; shift
    local args=()
    for d in "$@"; do args+=(-D "$d"); done
    echo "==> rendering $name ${*:-(defaults)}"
    local t0 t1
    t0=$(date +%s.%N)
    docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad 2>&1 \
        | grep -E 'ERROR|WARNING' || true
    t1=$(date +%s.%N)
    printf '    %.1f s\n' "$(echo "$t1 - $t0" | bc)"
}

# A distinct cap colour in the variations so each piece is its own material and
# can be measured on its own. The text keeps its #FFFFFF default, which is the
# colour the checks below look it up by.
C='cap_color="#FF8800"'

render defaults
render slot_text "$C" 'cap_text="CABLES"'
render brush "$C" 'cap_style="brush_segments"'
render solid_square "$C" 'cap_style="solid"' 'shape="square"' 'cap_text="USB"'
render open_ring "$C" 'cap_style="open_ring"'
render small_tight "$C" 'hole_d=20' 'slot_w=30' 'fit=0' 'flange_w=3' 'desk_thickness=10'
render big_loose "$C" 'hole_d=100' 'fit=1' 'flange_w=15' 'desk_thickness=60' 'cap_style="brush_segments"'

python3 - "$OUT" <<'PY'
import math, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
TOL = 0.01

DEFAULTS = dict(hole_d=60, desk_thickness=25, flange_w=6, fit=0.3, shape="round",
                cap_style="slot", cap_text="", distinct=False)
# Mirrors the [Hidden] section of model.scad.
SLEEVE_WALL, FLANGE_T, CAP_T, LIP_DEPTH, LIP_WALL, INLAY, GAP = 2.4, 3, 3, 6, 1.6, 0.6, 6

CASES = {
    "defaults": {},
    "slot_text": dict(distinct=True, cap_text="CABLES"),
    "brush": dict(distinct=True, cap_style="brush_segments"),
    "solid_square": dict(distinct=True, cap_style="solid", shape="square", cap_text="USB"),
    "open_ring": dict(distinct=True, cap_style="open_ring"),
    "small_tight": dict(distinct=True, hole_d=20, fit=0, flange_w=3, desk_thickness=10),
    "big_loose": dict(distinct=True, hole_d=100, fit=1, flange_w=15, desk_thickness=60,
                      cap_style="brush_segments"),
}

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def load(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper())
            for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
             for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


for name, over in CASES.items():
    p = dict(DEFAULTS, **over)
    print("\n%s: %s" % (name, {k: v for k, v in over.items() if k != "distinct"} or "defaults"))
    mats, verts, tris = load("%s/%s.3mf" % (OUT, name))
    counts = Counter(t[3] for t in tris)
    per_mat = defaultdict(set)
    for t in tris:
        per_mat[t[3]].update(t[:3])

    F = p["hole_d"] + 2 * p["flange_w"]
    sleeve_size = p["hole_d"] - p["fit"]
    bore = sleeve_size - 2 * SLEEVE_WALL
    lip = bore - p["fit"]
    lip_bore = lip - 2 * LIP_WALL
    has_text = bool(p["cap_text"]) and p["cap_style"] in ("slot", "solid")
    sleeve_h = FLANGE_T + p["desk_thickness"]
    cap_x = F + GAP

    named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
    want = (2 if p["distinct"] else 1) + (1 if has_text else 0)
    check(len(named) == want, "%d non-empty material(s) besides Default (got %d)"
          % (want, len(named)))
    check(counts.get(0, 0) == 0,
          "Default material carries no geometry (got %d triangles)" % counts.get(0, 0))

    xs, ys, zs = zip(*verts)
    print("  bbox X %.3f..%.3f  Y %.3f..%.3f  Z %.3f..%.3f"
          % (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    check(abs(min(zs)) <= TOL, "sits on z=0 (min z %.3f)" % min(zs))
    check(abs((max(xs) - min(xs)) - (2 * F + GAP)) <= TOL,
          "X %.3f == two flanges of %.1f plus a %d mm gap" % (max(xs) - min(xs), F, GAP))
    check(abs((max(ys) - min(ys)) - F) <= TOL, "Y %.3f == flange %.1f" % (max(ys) - min(ys), F))
    check(abs(max(zs) - sleeve_h) <= TOL,
          "Z %.3f == flange + desk %.1f" % (max(zs), sleeve_h))

    if not p["distinct"]:
        continue
    by_col = {mats[i][1]: [verts[v] for v in per_mat[i]] for i in named}
    sleeve = by_col.get("#1E1E1E", [])
    cap = by_col.get("#FF8800", [])
    check(bool(sleeve) and bool(cap), "sleeve and cap are separate materials")
    if not (sleeve and cap):
        continue

    sz = [v[2] for v in sleeve]
    cz = [v[2] for v in cap]
    check(abs(min(sz)) <= TOL and abs(max(sz) - sleeve_h) <= TOL,
          "sleeve 0 .. %.1f (got %.3f .. %.3f)" % (sleeve_h, min(sz), max(sz)))
    check(abs(min(cz)) <= TOL and abs(max(cz) - CAP_T - LIP_DEPTH) <= TOL,
          "cap printed top-down, 0 .. %.1f (got %.3f .. %.3f)"
          % (CAP_T + LIP_DEPTH, min(cz), max(cz)))
    check(max(v[0] for v in sleeve) < min(v[0] for v in cap),
          "sleeve and cap do not overlap on the plate")

    if p["shape"] == "round":
        # Radii measured about each piece's own centre.
        tube = [math.hypot(x, y) for x, y, z in sleeve if z > FLANGE_T + TOL]
        check(abs(max(tube) - sleeve_size / 2) <= 0.02,
              "sleeve OD %.3f == hole %.1f - fit %.1f" % (2 * max(tube), p["hole_d"], p["fit"]))
        check(abs(min(tube) - bore / 2) <= 0.02, "sleeve bore %.3f == %.3f" % (2 * min(tube), bore))
        lipr = [math.hypot(x - cap_x, y) for x, y, z in cap if z > CAP_T + TOL]
        check(abs(max(lipr) - lip / 2) <= 0.02,
              "cap lip OD %.3f == bore %.3f - fit %.1f" % (2 * max(lipr), bore, p["fit"]))
        inner = [math.hypot(x - cap_x, y) for x, y, z in cap if z <= CAP_T + TOL]
        if p["cap_style"] == "open_ring":
            check(min(inner) >= lip_bore / 2 - 0.02,
                  "open ring: nothing inside the lip bore (min r %.3f)" % min(inner))
        if p["cap_style"] == "brush_segments":
            fingers = [r for r in inner if r < lip_bore / 2 - 1]
            check(len(fingers) > 0, "brush fingers span the lip bore (%d vertices)" % len(fingers))

    if has_text:
        tv = by_col.get("#FFFFFF", [])
        tz = [v[2] for v in tv]
        check(bool(tz) and max(tz) <= INLAY + TOL,
              "cap text inlaid into the face on the bed (z %.3f .. %.3f)"
              % (min(tz), max(tz)) if tz else "cap text present")

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK")
PY
