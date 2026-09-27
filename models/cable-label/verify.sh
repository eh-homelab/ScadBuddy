#!/usr/bin/env bash
# Render models/cable-label with the defaults and each style, and check the
# 3MFs: material count, no uncoloured geometry, bounding box against the
# dimensions the parameters imply, sits on z=0, and the text where the style
# puts it.
#
# The XML checking runs on the host: openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans Mono"

mkdir -p "$OUT"

# Without the default face OpenSCAD silently falls back to another font and
# the text measurements are meaningless, so derive an image that has the
# ScadBuddy image's font packages when the base one lacks it.
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

# case name | -D overrides (one per line in the array below)
WARN=0
render() {
    local name="$1"; shift
    local args=()
    for d in "$@"; do args+=(-D "$d"); done
    echo "==> rendering $name ${*:-(defaults)}"
    local t0 t1
    t0=$(date +%s.%N)
    docker run --rm -v "$PWD":/w -w /w "$IMAGE" \
        openscad --backend=Manifold "${args[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1 \
        || { echo "  FAIL  openscad exited non-zero (see $OUT/$name.log)"; WARN=1; }
    if grep -E 'ERROR|WARNING' "$OUT/$name.log"; then
        echo "  FAIL  OpenSCAD warnings (see $OUT/$name.log)"; WARN=1
    fi
    t1=$(date +%s.%N)
    printf '    %.1f s\n' "$(echo "$t1 - $t0" | bc)"
}

render defaults
render double_sided 'style="double_sided"'
render wrap_band 'style="wrap_band"'
render no_text 'text=""'
render thick_cable 'cable_d=15' 'clip_opening_pct=85' 'flag_h=6' 'flag_len=20' 'clip_len=14'
render band_thick 'style="wrap_band"' 'cable_d=12' 'flag_h=14' 'flag_len=50'
render long_flag 'text="ETHERNET-SWITCH1"'
render long_band 'style="wrap_band"' 'flag_len=20' 'text="ETHERNET-SWITCH1"'
render tall_text 'text="HI"' 'text_size=12' 'flag_h=6' 'flag_len=15'

python3 - "$OUT" <<'PY'
import math, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
TOL = 0.01

DEFAULTS = dict(cable_d=5, clip_opening_pct=70, clip_len=10, flag_len=35,
                flag_h=10, style="flag", text="HDMI")
# Mirrors the [Hidden] section of model.scad.
CLEARANCE, TAG_T, INLAY, FACE_STANDOFF = 0.2, 2.4, 0.6, 1.0

CASES = {
    "defaults": {},
    "double_sided": dict(style="double_sided"),
    "wrap_band": dict(style="wrap_band"),
    "no_text": dict(text=""),
    "thick_cable": dict(cable_d=15, clip_opening_pct=85, flag_h=6, flag_len=20, clip_len=14),
    "band_thick": dict(style="wrap_band", cable_d=12, flag_h=14, flag_len=50),
    # Text too big for the face shrinks to fit instead of being cut off at the
    # margin. `shrunk` bounds the extent (along, across the face) that only a
    # scaled-down word stays under: cut-off letters keep their full height,
    # and cut-off words their full length.
    "long_flag": dict(text="ETHERNET-SWITCH1", shrunk=(None, 0.8 * 6)),
    "long_band": dict(style="wrap_band", flag_len=20, text="ETHERNET-SWITCH1",
                      shrunk=(None, 0.8 * 6)),
    "tall_text": dict(text="HI", text_size=12, flag_h=6, flag_len=15, shrunk=(8, None)),
}
MARGIN = 1

failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def load(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [b.get("name") for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
             for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


def span(pts, axis):
    vals = [p[axis] for p in pts]
    return min(vals), max(vals)


for name, over in CASES.items():
    p = dict(DEFAULTS, **over)
    print("\n%s: %s" % (name, over or "defaults"))
    mats, verts, tris = load("%s/%s.3mf" % (OUT, name))
    counts = Counter(t[3] for t in tris)
    per_mat = defaultdict(set)
    for t in tris:
        per_mat[t[3]].update(t[:3])

    r_in = (p["cable_d"] + CLEARANCE) / 2
    wall = max(1.6, 0.15 * p["cable_d"] + 1.0)
    r_out = r_in + wall
    has_text = len(p["text"]) > 0
    band = p["style"] == "wrap_band"

    named = [i for i, n in enumerate(mats) if n != "Default" and counts.get(i)]
    want = 2 if has_text else 1
    check(len(named) == want, "%d non-empty material(s) besides Default (got %d)"
          % (want, len(named)))
    check(counts.get(0, 0) == 0,
          "Default material carries no geometry (got %d triangles)" % counts.get(0, 0))

    x0, x1 = span(verts, 0)
    y0, y1 = span(verts, 1)
    z0, z1 = span(verts, 2)
    print("  bbox X %.3f..%.3f  Y %.3f..%.3f  Z %.3f..%.3f" % (x0, x1, y0, y1, z0, z1))
    check(abs(z0) <= TOL, "sits on z=0 (min z %.3f)" % z0)

    if band:
        want_x1, want_z = r_out + FACE_STANDOFF, p["flag_len"]
    else:
        want_x1, want_z = r_out + p["flag_len"], max(p["clip_len"], TAG_T)
    want_y = max(p["flag_h"], 2 * r_out)
    check(abs(x1 - want_x1) <= TOL, "max X %.3f == %.3f" % (x1, want_x1))
    # The slot cuts the clip's -X side, so the left edge is the jaw tips:
    # inside the outer radius, beyond the bore.
    check(-r_out - TOL <= x0 <= -r_in, "min X %.3f within the clip wall [%.3f, %.3f]"
          % (x0, -r_out, -r_in))
    check(abs((y1 - y0) - want_y) <= 0.05, "Y %.3f == %.3f" % (y1 - y0, want_y))
    check(abs((z1 - z0) - want_z) <= TOL, "Z %.3f == %.3f" % (z1 - z0, want_z))

    # The opening: no geometry at x<0 between the jaws at mid height.
    w = p["cable_d"] * p["clip_opening_pct"] / 100
    blocked = [v for v in verts if v[0] < -0.5 and abs(v[1]) < w / 2 - 0.05]
    check(not blocked, "clip opening %.2f mm wide is clear (%d vertices inside)"
          % (w, len(blocked)))

    if has_text and len(named) == 2:
        tv = [verts[i] for i in per_mat[named[1]]]
        tx, tz = span(tv, 0), span(tv, 2)
        if band:
            check(abs(tx[1] - (r_out + FACE_STANDOFF)) <= TOL,
                  "text is flush with the band face at X %.3f" % tx[1])
            check(tz[0] > 0 and tz[1] < p["flag_len"], "text inside the band's height")
        else:
            check(abs(tz[1] - TAG_T) <= TOL, "text flush with the tag top (max z %.3f)" % tz[1])
            check(tx[0] >= r_out, "text clear of the clip (min X %.3f >= %.3f)" % (tx[0], r_out))
            bottom = p["style"] == "double_sided"
            got_bottom = abs(tz[0]) <= TOL
            check(got_bottom == bottom, "text on the bottom face: %s (min z %.3f)"
                  % (bottom, tz[0]))
        ty = span(tv, 1)
        along = (tz[1] - tz[0]) if band else (tx[1] - tx[0])
        across = ty[1] - ty[0]
        area_along = p["flag_len"] - (3 if band else 2 * MARGIN)
        check(along <= area_along + TOL and across <= p["flag_h"] - 2 * MARGIN + TOL,
              "text %.2f x %.2f inside the %.1f x %.1f label face"
              % (along, across, area_along, p["flag_h"] - 2 * MARGIN))
        max_along, max_across = p.get("shrunk", (None, None))
        if max_along is not None:
            check(along < max_along, "text shrunk to fit, not cut off (length %.2f < %.2f)"
                  % (along, max_along))
        if max_across is not None:
            check(across < max_across, "text shrunk to fit, not cut off (height %.2f < %.2f)"
                  % (across, max_across))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK")
PY

if [ "$WARN" -ne 0 ]; then
    echo "FAILED: OpenSCAD warnings or errors above"
    exit 1
fi
