#!/usr/bin/env bash
# Render models/name-ornament with the defaults and a spread of variations
# (every shape, both text styles, both hangers, border on and off, an empty
# name) and check each result: number of materials, nothing uncoloured, the
# bounding box the parameters imply, sitting on z=0. Each colour is also
# re-rendered on its own the way ScadBuddy builds closed parts (a color()
# override that keeps one colour), and the per-colour volumes must add up to
# the volume of the whole model — i.e. no two colours overlap.
#
# The XML/STL checking runs on the host with python3 and the standard library:
# openscad/openscad:dev has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
FONT_FAMILY="Lobster Two"

mkdir -p "$OUT"

# openscad/openscad:dev ships DejaVu only; without Lobster Two OpenSCAD
# silently falls back to DejaVu Sans. Derive an image with the ScadBuddy
# image's font packages when the family is missing.
IMAGE="$BASE_IMAGE"
if ! docker run --rm "$BASE_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
    if ! docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1 \
        || ! docker run --rm "$FONTS_IMAGE" fc-list : family | grep -F "$FONT_FAMILY" >/dev/null; then
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

COLOURS=("#B22222" "#FFFFFF" "#D4AF37")   # base, text, accent (extruder order)

# name | -D overrides (space-separated, no spaces inside values)
CASES=(
    'defaults|'
    'star-year|shape="star" year="2026"'
    'tree-inlay-hole|shape="tree" text_style="inlay" hanger="hole" year="2026"'
    'heart-hole-noborder|shape="heart" hanger="hole" border=false'
    'heart-small-loop|shape="heart" size=40 thickness=2'
    'disc-inlay-noborder|shape="snowflake_disc" text_style="inlay" border=false year="2026"'
    'bauble-big-longname|size=120 thickness=6 text_size=30 name="Maximilianus"'
    'star-empty|shape="star" name="" hanger="hole" border=false'
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
TOL = 0.05
RELIEF, HOLE_D, RING_RO = 1.2, 5.0, 4.9

DEFAULTS = dict(name="Ava", year="", text_size=14, text_style="raised", shape="bauble",
                size=70, thickness=3, hanger="loop", border=True)

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


def expected(p):
    S, t = p["size"], p["thickness"]
    shape = p["shape"]
    a = S / (0.75 * math.sqrt(2) + 0.5)
    if shape == "bauble":
        width = (S - max(0.15 * S, HOLE_D + 4)) / 0.96
    elif shape == "star":
        rt = 0.04 * S
        rc = (S - 2 * rt) / (1 + math.sin(math.radians(54)))
        width = 2 * (rc * math.cos(math.radians(18)) + rt)
    elif shape == "tree":
        width = 0.9 * S
    elif shape == "heart":
        width = a * (1 + 1 / math.sqrt(2))
    else:
        width = S
    attach = math.sqrt(2) * a + 2 if shape == "heart" else S
    height = max(S, attach + HOLE_D / 2 - 1 + RING_RO) if p["hanger"] == "loop" else S
    has_text = bool(p["name"]) or bool(p["year"])
    has_trim = p["border"] or shape == "snowflake_disc"
    raised = p["text_style"] == "raised"
    top = t + RELIEF if raised and (has_text or has_trim) else t
    has_accent = has_trim or shape in ("bauble", "tree") or p["hanger"] == "loop"
    return width, height, top, has_text, has_accent


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
    tris = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        tris.append((f[3:6], f[6:9], f[9:12]))
    return tris


for case in CASES:
    name = case.split("|", 1)[0]
    d = os.path.join(OUT, name)
    p = parse_params([l for l in open(os.path.join(d, "params")).read().split("\n") if l])
    width, height, top, has_text, has_accent = expected(p)
    t = p["thickness"]
    print("\n== %s  (%s)" % (name, case.split("|", 1)[1] or "defaults"))

    root = ET.fromstring(zipfile.ZipFile(os.path.join(d, "all.3mf")).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    tris = [(int(t_.get("v1")), int(t_.get("v2")), int(t_.get("v3")), int(t_.get("p1") or 0))
            for t_ in root.iter(NS + "triangle")]
    counts = {}
    for tr in tris:
        counts[tr[3]] = counts.get(tr[3], 0) + 1
    xs, ys, zs = zip(*verts)
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)

    want_mats = 1 + has_text + has_accent
    named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
    default_tris = sum(counts.get(i, 0) for i, (n, _) in enumerate(mats) if n == "Default")
    check(name, len(named) == want_mats,
          "%d non-empty materials besides Default (got %d)" % (want_mats, len(named)))
    check(name, default_tris == 0, "Default material carries no geometry (got %d triangles)" % default_tris)
    check(name, abs(dx - width) <= TOL, "width %.3f == %.3f" % (dx, width))
    check(name, abs(dy - height) <= TOL, "height %.3f == %.3f" % (dy, height))
    check(name, abs(min(zs)) <= TOL, "sits on z=0 (min z %.3f)" % min(zs))
    check(name, abs(max(zs) - top) <= TOL, "top at z=%.2f (got %.3f)" % (top, max(zs)))

    whole = mesh_volume([(verts[a], verts[b], verts[c]) for a, b, c, _ in tris])
    parts = {}
    for i, role in enumerate(("base", "text", "accent")):
        f = os.path.join(d, "part%d.stl" % i)
        if os.path.exists(f) and os.path.getsize(f) > 0:
            parts[role] = read_stl(f)
    check(name, set(parts) == ({"base"} | ({"text"} if has_text else set())
                               | ({"accent"} if has_accent else set())),
          "closed per-colour renders: %s" % ", ".join(sorted(parts)))
    total = 0.0
    for role, pt in parts.items():
        vol = mesh_volume(pt)
        total += vol
        pz = [v[2] for tri in pt for v in tri]
        print("        %-6s volume %9.1f mm3   z %.2f .. %.2f" % (role, vol, min(pz), max(pz)))
        if role == "base":
            check(name, abs(min(pz)) <= TOL and abs(max(pz) - t) <= TOL,
                  "base spans z 0 .. %.1f" % t)
        if role == "text":
            lo, hi = (t, t + RELIEF) if p["text_style"] == "raised" else (t - min(1.0, t / 2), t)
            check(name, abs(min(pz) - lo) <= TOL and abs(max(pz) - hi) <= TOL,
                  "text spans z %.2f .. %.2f (%s)" % (lo, hi, p["text_style"]))
    check(name, abs(total - whole) <= max(0.5, 0.002 * whole),
          "colours do not overlap: parts %.1f mm3 vs whole %.1f mm3" % (total, whole))

print()
if failures:
    for f in failures:
        print("FAILED  " + f)
    print("FAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("OK: all %d cases passed" % len(CASES))
PY
