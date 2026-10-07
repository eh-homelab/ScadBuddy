#!/usr/bin/env bash
# Render models/tube-squeezer with the defaults and its variations, and check
# the 3MFs: material count, no uncoloured geometry, sits on z=0, the bar's size
# against the parameters, the slot's gap and length (open through one end for
# open_end), the finger grooves, and the label inlaid in the top of one jaw.
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

WARN=0
render() {
    local name="$1"; shift
    local args=()
    for d in "$@"; do args+=(-D "$d"); done
    echo "==> rendering $name ${*:-(defaults)}"
    local t0 t1
    t0=$(date +%s.%N)
    docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1 \
        || { echo "  FAIL  openscad exited non-zero (see $OUT/$name.log)"; WARN=1; }
    if grep -E 'ERROR|WARNING' "$OUT/$name.log"; then
        echo "  FAIL  OpenSCAD warnings (see $OUT/$name.log)"; WARN=1
    fi
    t1=$(date +%s.%N)
    printf '    %.1f s\n' "$(echo "$t1 - $t0" | bc)"
}

render defaults
render label 'label="TOOTHPASTE"'
render open_end 'style="open_end"' 'label="PAINT"'
render small_tight 'tube_width=15' 'slot_gap=0.6' 'clearance=0' 'jaw=3' 'height=5' 'label="WWWWWWWWWWWWWWWW"'
render big_no_wings 'tube_width=120' 'slot_gap=5' 'clearance=5' 'jaw=15' 'height=25' 'wing=0'
render no_grooves 'grooves=false' 'wing=40'

python3 - "$OUT" <<'PY'
import sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
TOL = 0.01

DEFAULTS = dict(tube_width=50, slot_gap=1.6, clearance=1, style="closed", height=10,
                jaw=6, wing=15, grooves=True, label="")
# Mirrors the [Hidden] section of model.scad.
END_WALL, GROOVE_DEPTH, INLAY = 4, 0.8, 0.6

CASES = {
    "defaults": {},
    "label": dict(label="TOOTHPASTE"),
    "open_end": dict(style="open_end", label="PAINT"),
    "small_tight": dict(tube_width=15, slot_gap=0.6, clearance=0, jaw=3, height=5,
                        label="WWWWWWWWWWWWWWWW"),
    "big_no_wings": dict(tube_width=120, slot_gap=5, clearance=5, jaw=15, height=25, wing=0),
    "no_grooves": dict(grooves=False, wing=40),
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
    print("\n%s: %s" % (name, over or "defaults"))
    mats, verts, tris = load("%s/%s.3mf" % (OUT, name))
    counts = Counter(t[3] for t in tris)
    per_mat = defaultdict(set)
    for t in tris:
        per_mat[t[3]].update(t[:3])

    slot_len = p["tube_width"] + 2 * p["clearance"]
    bar_w = p["slot_gap"] + 2 * p["jaw"]
    bar_l = slot_len + 2 * END_WALL + 2 * p["wing"]
    has_label = bool(p["label"])

    named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
    want = 2 if has_label else 1
    check(len(named) == want, "%d non-empty material(s) besides Default (got %d)"
          % (want, len(named)))
    check(counts.get(0, 0) == 0,
          "Default material carries no geometry (got %d triangles)" % counts.get(0, 0))

    xs, ys, zs = zip(*verts)
    print("  bbox X %.3f..%.3f  Y %.3f..%.3f  Z %.3f..%.3f"
          % (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    check(abs(min(zs)) <= TOL, "sits on z=0 (min z %.3f)" % min(zs))
    check(abs(max(zs) - p["height"]) <= TOL, "Z %.3f == height %s" % (max(zs), p["height"]))
    check(abs((max(xs) - min(xs)) - bar_l) <= TOL,
          "X %.3f == slot %.1f + end walls + wings = %.1f" % (max(xs) - min(xs), slot_len, bar_l))
    check(abs((max(ys) - min(ys)) - bar_w) <= TOL,
          "Y %.3f == gap + two jaws = %.1f" % (max(ys) - min(ys), bar_w))

    half = p["slot_gap"] / 2
    by_col = {mats[i][1]: [verts[v] for v in per_mat[i]] for i in named}
    body = by_col.get("#3A7BD5", [])
    check(bool(body), "body is its own material")
    if not body:
        continue

    # Along the straight part of the slot (out to where the rounded ends
    # start), the nearest body faces to y=0 are the slot walls, at +-gap/2.
    mid = [abs(y) for x, y, z in body if abs(x) <= slot_len / 2 - half + TOL]
    check(abs(min(mid) - half) <= TOL,
          "slot gap %.3f == %s" % (2 * min(mid), p["slot_gap"]))
    # The slot's rounded ends reach y=0 at +-slot_len/2; open_end has only the
    # -X end and nothing of the body inside the gap on the +X side.
    tips = [x for x, y, z in body if abs(y) < TOL]
    check(any(abs(x + slot_len / 2) <= TOL for x in tips),
          "slot ends at -%.1f (half the tube width plus clearance)" % (slot_len / 2))
    if p["style"] == "closed":
        check(any(abs(x - slot_len / 2) <= TOL for x in tips),
              "closed slot ends at +%.1f" % (slot_len / 2))
    else:
        inside = [x for x, y, z in body if x > 0 and abs(y) < half - TOL]
        check(not inside, "open_end: slot open through +X (%d body vertices in the gap)"
              % len(inside))

    # Finger grooves dip below the top face on the wings only.
    dips = [x for x, y, z in body
            if p["height"] - GROOVE_DEPTH - TOL <= z < p["height"] - TOL
            and abs(x) > slot_len / 2 + END_WALL and abs(y) > half + TOL]
    want_grooves = p["grooves"] and p["wing"] >= 7
    if not has_label:
        check(bool(dips) == want_grooves, "finger grooves %s (%d vertices)"
              % ("present" if want_grooves else "absent", len(dips)))

    if has_label:
        tv = by_col.get("#FFFFFF", [])
        check(bool(tv), "label is its own material")
        if tv:
            tx, ty, tz = zip(*tv)
            check(min(tz) >= p["height"] - INLAY - TOL and max(tz) <= p["height"] + TOL,
                  "label inlaid flush in the top face (z %.3f..%.3f)" % (min(tz), max(tz)))
            check(min(ty) > half and max(ty) < bar_w / 2,
                  "label inside the +Y jaw (y %.3f..%.3f)" % (min(ty), max(ty)))
            check(max(tx) - min(tx) <= slot_len - 2 + TOL,
                  "label fits along the slot, shrunk not cut (%.2f <= %.2f)"
                  % (max(tx) - min(tx), slot_len - 2))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK")
PY

if [ "$WARN" -ne 0 ]; then
    echo "FAILED: OpenSCAD warnings or errors above"
    exit 1
fi
