#!/usr/bin/env bash
# Render models/filament-swatch with the defaults and a spread of variations
# (both text modes, both orientations, 3 to 8 steps, no hole, empty text) and
# check each result: number of materials (deboss must be exactly one),
# nothing uncoloured, the card's bounding box, sitting on z=0, the stepped
# window floors at exactly 0.2, 0.4, ... mm, and the card volume against the
# volume the steps imply. Each colour is also re-rendered on its own the way
# ScadBuddy builds closed parts, and the per-colour volumes must add up to
# the volume of the whole model — i.e. the lettering and the card do not
# overlap.
#
# The XML/STL checking runs on the host with python3 and the standard library:
# openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="DejaVu Sans"

mkdir -p "$OUT"

IMAGE="$BASE_IMAGE"
if ! docker run --rm "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    if ! docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
        echo "==> $BASE_IMAGE has no '$FONT_FAMILY'; building $FONTS_IMAGE with the image's font packages"
        docker build -q -t "$FONTS_IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
    fi
    IMAGE="$FONTS_IMAGE"
fi
echo "==> rendering with $IMAGE"

COLOURS=("#0086D6" "#FFFFFF")   # swatch, text (extruder order)

# name | -D overrides (space-separated, no spaces inside values)
CASES=(
    'defaults|'
    'inlay|text_mode="inlay"'
    'portrait|orientation="portrait"'
    'portrait-inlay-8|orientation="portrait" text_mode="inlay" step_count=8 thickness=2 width=100 height=60'
    'small-3-nohole|step_count=3 hole=false width=60 height=30 thickness=4'
    'inlay-8-small|text_mode="inlay" step_count=8 width=60 height=30'
    'empty-text-inlay|text_mode="inlay" brand="" material="" color_name="" temp=""'
)

cat > "$OUT/one_colour.scad" <<'SCAD'
// ScadBuddy-style closed-part render: keep only the geometry of one colour.
_sb_target = "";
module color(c, alpha = 1) { if (c == _sb_target) children(); }
include <../model.scad>
SCAD

run_openscad() {
    docker run --rm -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@" 2>&1
}

for c in "${CASES[@]}"; do
    name="${c%%|*}"
    read -r -a defs <<<"${c#*|}"
    dargs=()
    for d in "${defs[@]}"; do dargs+=(-D "$d"); done
    mkdir -p "$OUT/$name"
    log=$(run_openscad "${dargs[@]}" -o "$OUT/$name/all.3mf" model.scad) \
        || { echo "$log"; echo "FAIL: $name did not render"; exit 1; }
    if grep -qE '^(WARNING|ERROR)' <<<"$log"; then
        grep -E '^(WARNING|ERROR)' <<<"$log"; echo "FAIL: $name rendered with warnings"; exit 1
    fi
    for i in "${!COLOURS[@]}"; do
        rm -f "$OUT/$name/part$i.stl"
        run_openscad "${dargs[@]}" -D "_sb_target=\"${COLOURS[$i]}\"" \
            -o "$OUT/$name/part$i.stl" "$OUT/one_colour.scad" >/dev/null || true
    done
    printf '%s\n' "${defs[@]}" > "$OUT/$name/params"
done

python3 - "$OUT" "${CASES[@]}" <<'PY'
import math, os, re, struct, sys, zipfile, xml.etree.ElementTree as ET

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT, CASES = sys.argv[1], sys.argv[2:]
TOL = 0.01
MARGIN, CORNER_R, HOLE_D, FN, TEXT_DEPTH, STEP = 3, 3, 5, 48, 0.6, 0.2

DEFAULTS = dict(width=80, height=40, thickness=3.2, step_count=5, hole=True,
                text_mode="deboss", orientation="landscape")

failures = []


def check(case, ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append("%s: %s" % (case, msg))


def parse_params(lines):
    p = dict(DEFAULTS)
    for l in lines:
        k, v = l.split("=", 1)
        if v.startswith('"'):
            p[k] = v.strip('"')
        elif v in ("true", "false"):
            p[k] = v == "true"
        else:
            p[k] = float(v)
    return p


def mesh_volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6.0


def read_stl(path):
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:400]:
        nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        pts = [tuple(float(x) for x in n) for n in nums]
        return [tuple(pts[i:i + 3]) for i in range(0, len(pts), 3)]
    n = struct.unpack("<I", data[80:84])[0]
    return [tuple(tuple(struct.unpack("<12f", data[84 + 50 * i: 132 + 50 * i])[j:j + 3])
                  for j in (3, 6, 9)) for i in range(n)]


def distinct(values, tol=0.005):
    out = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


for case in CASES:
    name = case.split("|", 1)[0]
    d = os.path.join(OUT, name)
    p = parse_params([l for l in open(os.path.join(d, "params")).read().split("\n") if l])
    portrait = p["orientation"] == "portrait"
    cw, ch = (p["height"], p["width"]) if portrait else (p["width"], p["height"])
    T, n, inlay = p["thickness"], int(p["step_count"]), p["text_mode"] == "inlay"
    print("\n== %s  (%s)" % (name, case.split("|", 1)[1] or "defaults"))

    root = ET.fromstring(zipfile.ZipFile(os.path.join(d, "all.3mf")).read("3D/3dmodel.model"))
    mats = [b.get("name") for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    counts = {}
    for tr in tris:
        counts[tr[3]] = counts.get(tr[3], 0) + 1
    xs, ys, zs = zip(*verts)

    want = 2 if inlay else 1
    named = [i for i, m in enumerate(mats) if m != "Default" and counts.get(i)]
    default_tris = sum(counts.get(i, 0) for i, m in enumerate(mats) if m == "Default")
    check(name, len(named) == want, "%d non-empty material(s) besides Default (got %d)" % (want, len(named)))
    check(name, default_tris == 0, "Default material carries no geometry (got %d triangles)" % default_tris)
    check(name, abs(max(xs) - min(xs) - cw) <= TOL and abs(max(ys) - min(ys) - ch) <= TOL,
          "card %.1f x %.1f (got %.3f x %.3f)" % (cw, ch, max(xs) - min(xs), max(ys) - min(ys)))
    check(name, abs(min(zs)) <= TOL and abs(max(zs) - T) <= TOL,
          "sits on z=0, top at z=%.1f (got %.3f .. %.3f)" % (T, min(zs), max(zs)))

    # Every level carrying a horizontal face of the union: the bed, the top, each window floor
    # and (deboss only) the floor of the lettering.
    floors = [round(STEP * k, 3) for k in range(1, n + 1)]
    levels = set([0.0, T] + floors + ([] if inlay else [round(T - TEXT_DEPTH, 3)]))
    flat = [verts[a][2] for a, b, c, _ in tris
            if abs(verts[a][2] - verts[b][2]) < 1e-4 and abs(verts[a][2] - verts[c][2]) < 1e-4]
    got = distinct(flat)
    check(name, len(got) == len(distinct(levels))
          and all(abs(a - b) <= TOL for a, b in zip(got, distinct(levels))),
          "z levels %s (got %s)" % (distinct(levels), [round(z, 3) for z in got]))

    # Card volume the steps imply: rounded rectangle less the ring hole, less
    # each window cut down to its floor. Exact for the inlay union (lettering
    # fills its pockets); deboss removes the lettering on top of that.
    area = cw * ch - (4 - math.pi) * CORNER_R ** 2
    if p["hole"]:
        area -= 0.5 * FN * (HOLE_D / 2) ** 2 * math.sin(2 * math.pi / FN)
    cell_area = ((cw - 2 * MARGIN - 9) * 0.45 * ch / n) if portrait \
        else ((cw - 2 * MARGIN) / n * 0.34 * ch)
    ideal = area * T - sum(cell_area * (T - f) for f in floors)
    whole = mesh_volume([(verts[a], verts[b], verts[c]) for a, b, c, _ in tris])
    # Corner arcs are polygons; allow for that and nothing else.
    arc_err = 4 * CORNER_R ** 2 * (math.pi / 4) * (1 - math.sin(2 * math.pi / FN) / (2 * math.pi / FN)) * T * 1.5
    if inlay:
        check(name, abs(whole - ideal) <= arc_err + 0.5,
              "volume %.1f mm3 matches the stepped card %.1f mm3" % (whole, ideal))
    else:
        check(name, 0 < ideal - whole,
              "volume %.1f mm3 = stepped card %.1f mm3 less the debossed lettering (%.1f)"
              % (whole, ideal, ideal - whole))

    parts = {}
    for i, role in enumerate(("card", "text")):
        f = os.path.join(d, "part%d.stl" % i)
        if os.path.exists(f) and os.path.getsize(f) > 0:
            parts[role] = read_stl(f)
    check(name, set(parts) == ({"card", "text"} if inlay else {"card"}),
          "closed per-colour renders: %s" % ", ".join(sorted(parts)))
    total = 0.0
    for role, pt in parts.items():
        vol = mesh_volume(pt)
        total += vol
        pz = [v[2] for tri in pt for v in tri]
        print("        %-5s volume %8.1f mm3   z %.2f .. %.2f" % (role, vol, min(pz), max(pz)))
        if role == "text":
            check(name, abs(min(pz) - (T - TEXT_DEPTH)) <= TOL and abs(max(pz) - T) <= TOL,
                  "lettering spans z %.1f .. %.1f" % (T - TEXT_DEPTH, T))
    check(name, abs(total - whole) <= max(0.2, 0.002 * whole),
          "colours do not overlap: parts %.1f mm3 vs whole %.1f mm3" % (total, whole))

print()
if failures:
    for f in failures:
        print("FAILED  " + f)
    print("FAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("OK: all %d cases passed" % len(CASES))
PY
