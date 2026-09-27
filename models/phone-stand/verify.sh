#!/usr/bin/env bash
# Render models/phone-stand with the defaults and each major variation (every
# style at both ends of the angle range, the smallest and largest settings,
# text, no cable slot) and check each 3MF:
#
#   - no uncoloured geometry, the expected number of colour parts, and the
#     colour parts do not overlap (each colour rendered closed on its own
#     through a wrapper like ScadBuddy's adds up to the whole)
#   - on z=0, bounding box equal to what the parameters imply, fits the plate
#   - stable: the stand's own centre of mass and the device's centre of mass
#     both lie inside the footprint, so any mix of the two does too (the
#     combined centre of mass is a weighted average of the two)
#   - prints without supports: no downward-facing surface more than 45
#     degrees from vertical except flat bridges
#   - text inside the front face and flush with it
#   - no OpenSCAD warnings
#
# The XML checking runs on the host with python3 and the standard library.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"
mkdir -p "$OUT"

# The text face comes from the Debian font packages the ScadBuddy image
# installs. If the family is missing, derive a throwaway image that has it --
# otherwise OpenSCAD silently falls back and text widths change.
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

scad() { docker run --rm -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@"; }

# Values of a dropdown annotation: `name = "x"; // [a:Label, b, ...]` -> a b ...
options() {
    sed -n "s/^$1 = .*\/\/ \[\(.*\)\]\$/\1/p" model.scad | tr ',' '\n' | sed 's/:.*//; s/[" ]//g'
}
STYLES=$(options style)
if [ -z "$STYLES" ]; then
    echo "FAIL: could not read the style dropdown"; exit 1
fi

# name | -D overrides separated by ";"
CASES=('defaults|')
for s in $STYLES; do
    CASES+=("$s-steep|style=\"$s\";angle=50;device_height=220")
    CASES+=("$s-upright|style=\"$s\";angle=80;text=\"Desk\"")
    CASES+=("$s-min|style=\"$s\";angle=50;width=40;backrest_length=30;lip_height=3;floor_height=4;thickness=3;device_thickness=4;device_height=60;cable_width=24")
    CASES+=("$s-tablet|style=\"$s\";angle=55;width=250;backrest_length=250;device_thickness=30;device_height=350;lip_height=40;floor_height=40;thickness=10;cable_width=24;cable_channel_height=20;text=\"KITCHEN\";text_size=40")
done
CASES+=(
    'text-long-shrinks|text="Property of the office kitchen";text_size=40'
    'no-cable|cable_slot=false;text="Mia"'
    'one-colour|front_color="#546E7A";text="Solo"'
    'text-lobster|text="Leo";font="Lobster Two:style=Bold";style="cutout"'
)

: > "$OUT/cases.txt"
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    # ONLY=<regex> runs a subset while iterating.
    if [ -n "${ONLY:-}" ] && ! grep -Eq "$ONLY" <<< "$name"; then continue; fi
    defs=()
    IFS=";" read -ra kvs <<< "${c#*|}"
    for d in "${kvs[@]}"; do [ -n "$d" ] && defs+=(-D "$d"); done
    start=$(date +%s%N)
    if ! scad "${defs[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1; then
        tail -20 "$OUT/$name.log"; echo "FAIL: $name did not render"; exit 1
    fi
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    printf '%s %s %s\n' "$name" "$ms" "${c#*|}" >> "$OUT/cases.txt"
    printf '==> %-22s %6d ms\n' "$name" "$ms"

    # Every colour on its own, closed (ScadBuddy's per-colour wrapper, §6.3).
    colours=$(python3 - "$OUT/$name.3mf" <<'PY'
import sys, zipfile, xml.etree.ElementTree as ET
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
root = ET.fromstring(zipfile.ZipFile(sys.argv[1]).read("3D/3dmodel.model"))
used = {int(t.get("p1") or 0) for t in root.iter(NS + "triangle")}
mats = [b.get("displaycolor")[:7] for b in root.iter(NS + "base")]
print(" ".join(mats[i] for i in sorted(used) if i != 0))
PY
)
    [ "$(wc -w <<< "$colours")" -gt 1 ] || continue
    for col in $colours; do
        printf '_t = "%s";\nmodule color(c, alpha = 1) { if (is_string(c) && (c == _t || c == "%s")) children(); }\ninclude <../model.scad>\n' \
            "$col" "$(tr 'A-F' 'a-f' <<< "$col")" > "$OUT/wrap.scad"
        scad "${defs[@]}" --export-format binstl -o "$OUT/$name@${col#\#}.stl" "$OUT/wrap.scad" \
            >"$OUT/$name@${col#\#}.log" 2>&1 || { echo "FAIL: $name colour $col did not render"; exit 1; }
    done
done

python3 - "$OUT" <<'PY'
import math, os, re, struct, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]
BED_X, BED_Y = 300, 320   # H2C plate using both nozzles

# Defaults straight from the model source, so the checks follow the file.
DEFAULTS = {}
for line in open("model.scad"):
    m = re.match(r'^(\w+) = ("[^"]*"|[-\d.]+|true|false);', line)
    if m:
        v = m.group(2)
        DEFAULTS[m.group(1)] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))

failures = []


def check(case, ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append("%s: %s" % (case, msg))


def load(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
    V = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    T = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
         for t in root.iter(NS + "triangle")]
    return mats, V, T


def tet(a, b, c):
    """Signed volume of the tetrahedron (origin, a, b, c) and its centroid."""
    v = (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
         + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return v, [(a[k] + b[k] + c[k]) / 4 for k in range(3)]


def stl_volume(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    vol = 0.0
    for k in range(n):
        f = struct.unpack("<12f", data[84 + 50 * k: 84 + 50 * k + 48])
        vol += tet(f[3:6], f[6:9], f[9:12])[0]
    return vol


def hull(points):
    pts = sorted(set(points))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for q in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], q) <= 0:
            hi.pop()
        hi.append(q)
    return lo[:-1] + hi[:-1]


def inset(poly, q):
    """How far q lies inside the convex CCW polygon (negative = outside)."""
    d = float("inf")
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        ex, ey = b[0] - a[0], b[1] - a[1]
        d = min(d, (ex * (q[1] - a[1]) - ey * (q[0] - a[0])) / math.hypot(ex, ey))
    return d


for line in open(os.path.join(OUT, "cases.txt")):
    parts = line.rstrip("\n").split(" ", 2)
    name, ms = parts[0], int(parts[1])
    p = dict(DEFAULTS)
    for kv in (parts[2].split(";") if len(parts) > 2 and parts[2] else []):
        k, v = kv.split("=", 1)
        p[k] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))
    print("\n%s (%d ms): %s" % (name, ms, parts[2] if len(parts) > 2 else "(defaults)"))

    # Expected geometry from the parameters (mirrors model.scad).
    a = math.radians(p["angle"])
    t = p["thickness"]
    fz = max(p["floor_height"], t + 1)
    xb0 = t + p["device_thickness"] / math.sin(a) + p["clearance"]
    L = p["backrest_length"]
    T1 = (xb0 + L * math.cos(a), fz + L * math.sin(a))
    T2 = (T1[0] + t * math.sin(a), T1[1] - t * math.cos(a))
    lip_top = fz + p["lip_height"]
    if p["style"] == "folded":
        lt = (xb0 + 0.65 * L * math.cos(a) + t / 2 * math.sin(a),
              fz + 0.65 * L * math.sin(a) - t / 2 * math.cos(a))
        x_struct = lt[0] + (lt[1] - t / 2) / math.tan(math.radians(65)) + t / 2 + 10
    else:
        x_struct = T2[0]
    dev_x = xb0 + p["device_height"] / 2 * math.cos(a) - p["device_thickness"] / 2 * math.sin(a)
    x_rear = max(x_struct, dev_x + 15)
    top = max(T1[1], lip_top)

    mats, V, T = load(os.path.join(OUT, name + ".3mf"))
    counts = Counter(tt[3] for tt in T)
    named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
    xs, ys, zs = zip(*V)
    dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    print("  bbox %.1f x %.1f x %.1f" % (dx, dy, dz))

    cols = {p["stand_color"].upper(), p["front_color"].upper()}
    if p["text"]:
        cols.add(p["text_color"].upper())
    check(name, counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
    check(name, len(named) == len(cols),
          "%d colour part(s) (got %d: %s)" % (len(cols), len(named), " ".join(mats[i][1] for i in named)))
    check(name, abs(min(zs)) <= 1e-4, "sits on z=0 (min z %.4f)" % min(zs))
    check(name, abs(dx - p["width"]) <= 0.02, "X (width) %.2f == %.2f" % (dx, p["width"]))
    check(name, abs(dy - x_rear) <= 0.02, "Y (depth) %.2f == %.2f" % (dy, x_rear))
    check(name, abs(dz - top) <= 0.02, "Z (height) %.2f == %.2f" % (dz, top))
    check(name, dx <= BED_X and dy <= BED_Y, "fits the %dx%d plate" % (BED_X, BED_Y))

    # Stability. Plate frame: the front face is at y = min(ys), depth runs +y.
    y0 = min(ys)
    foot = hull([(v[0], v[1]) for v in V if abs(v[2]) < 1e-4])
    vol, mom = 0.0, [0.0, 0.0, 0.0]
    for tt in T:
        dv, cen = tet(V[tt[0]], V[tt[1]], V[tt[2]])
        vol += dv
        for k in range(3):
            mom[k] += dv * cen[k]
    com = [mom[k] / vol for k in range(3)]
    d_stand = inset(foot, (com[0], com[1]))
    d_dev = inset(foot, ((max(xs) + min(xs)) / 2, y0 + dev_x))
    check(name, d_stand >= 5, "stand's centre of mass %.1f mm inside the footprint" % d_stand)
    check(name, d_dev >= 10, "device's centre of mass %.1f mm inside the footprint (%.0f mm tall at %g deg)"
          % (d_dev, p["device_height"], p["angle"]))

    # Overhangs: a downward face above the bed must be flat (a bridge) or
    # within 45 degrees of vertical.
    worst = 0.0
    for tt in T:
        A, B, C = V[tt[0]], V[tt[1]], V[tt[2]]
        if max(A[2], B[2], C[2]) < 1e-4:
            continue
        u = [B[k] - A[k] for k in range(3)]
        w = [C[k] - A[k] for k in range(3)]
        nrm = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        nn = math.sqrt(sum(c * c for c in nrm))
        if nn < 1e-9:
            continue
        down = -nrm[2] / nn
        if down > 0.999:
            continue
        worst = max(worst, down)
    check(name, worst <= math.sin(math.radians(45)) + 1e-3,
          "no supports: steepest non-bridge overhang %.1f deg from vertical"
          % math.degrees(math.asin(min(1.0, worst))))

    if p["text"]:
        ti = [i for i in named if mats[i][1] == p["text_color"].upper()]
        if ti:
            tv = [V[k] for tt in T if tt[3] == ti[0] for k in tt[:3]]
            tx = [v[0] for v in tv]
            tz = [v[2] for v in tv]
            check(name, min(tx) >= min(xs) + 1.4 and max(tx) <= max(xs) - 1.4
                  and min(tz) >= 1.4 and max(tz) <= lip_top - 1.4,
                  "text inside the front face (x %.1f..%.1f, z %.1f..%.1f of 0..%.1f)"
                  % (min(tx), max(tx), min(tz), max(tz), lip_top))
            check(name, all(abs(v[1] - y0) < 1e-3 for v in tv), "text flush with the front face")
        else:
            check(name, False, "text part present")

    if len(named) > 1:
        stls = ["%s@%s.stl" % (name, mats[i][1][1:]) for i in named]
        have = [f for f in stls if os.path.exists(os.path.join(OUT, f))]
        total = sum(stl_volume(os.path.join(OUT, f)) for f in have)
        check(name, len(have) == len(named), "each of the %d colours rendered on its own" % len(named))
        check(name, abs(total - vol) <= 1e-3 * vol,
              "colour parts do not overlap: %.1f mm3 summed, %.1f mm3 whole" % (total, vol))
    check(name, "WARNING" not in open(os.path.join(OUT, name + ".log")).read(), "no OpenSCAD warnings")

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nOK: all cases passed")
PY
