#!/usr/bin/env bash
# Render models/bookmark with its defaults and every shape and mask mode (SVG
# and PNG), overlay + cutout together, missing and refused file names, and
# check each 3MF:
#
#   - no uncoloured geometry; exactly the expected colour parts
#   - on z=0, as tall as the layers imply, inside the extent the model echoes
#     (and filling it, for the exact outlines)
#   - ONE piece: the whole bookmark rendered as a single solid is one
#     connected body, so no cutout island falls out (a negative control shows
#     the check catches loose discs)
#   - min feature: nothing in the top face is thinner than min_feature (the
#     face minus its opening by min_feature / 2 leaves only facet crumbs)
#   - colour parts do not overlap: every colour rendered closed on its own
#     (ScadBuddy's per-colour wrapper) sums to the volume of the whole
#   - page corner: an empty gap of page_gap (>= a 0.1 mm page + 0.2 mm
#     clearance) between the plates, from the corner out to the open edge
#   - missing files render the default shape (OpenSCAD logs ERROR, exits 0);
#     refused names (folders, leading dot) are never opened
#
# The checking runs on the host with python3 and the standard library.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
mkdir -p "$OUT"

# ScadBuddy's file widget (#204) stores an upload beside model.scad under a
# generated bare name; stand two in for the upload-names case.
UPLOADS=(_scadbuddy_solid_asset_0123456789abcdef.svg _scadbuddy_solid_asset_fedcba9876543210.png)
cp sample-lattice.svg "${UPLOADS[0]}"
cp sample-leaf.png "${UPLOADS[1]}"
# ".." inside a bare name cannot traverse, so it must be read like any other.
DOTDOT=cat..v2.svg
cp sample-cat.svg "$DOTDOT"
trap 'rm -f "${UPLOADS[@]}" "$DOTDOT"' EXIT

# The text needs the fonts image; build it from ScadBuddy's backend
# Dockerfile when it is missing (as the name-keychain script does).
if ! docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
    echo "==> building $FONTS_IMAGE"
    printf 'FROM %s\nRUN apt-get update && apt-get install -y --no-install-recommends fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \&\& fc-cache -f \&\& rm -rf /var/lib/apt/lists/*\n' \
        "$BASE_IMAGE" | docker build -q -t "$FONTS_IMAGE" - >/dev/null
fi
IMAGE="$FONTS_IMAGE"
echo "==> rendering with $IMAGE"

# Run a shell script in the image, in this directory.
in_image() { docker run --rm -v "$PWD":/w -w /w "$IMAGE" sh -c "$1"; }

# name | expected colour parameters (comma list) | -D overrides separated by ";"
CASES=(
    "defaults|base_color,border_color,text_color,overlay_color,bead_color,bead_color_2|"
    "classic-plain|base_color,border_color|shape=\"classic\";label=\"\";overlay_file=\"\";mask_file=\"\""
    "tassel-text-across|base_color,text_color,bead_color|shape=\"tassel\";border=false;text_direction=\"horizontal\";label=\"Hi!\";mask_file=\"\";overlay_file=\"\";bead_count=1"
    "pointed-stripes|base_color,stripe_color_2,stripe_color_3,stripe_color_4,stripe_color_5,border_color,overlay_color|shape=\"pointed\";base_style=\"stripes\";stripe_count=5;label=\"\""
    "ribbon-png-inlay|base_color,border_color,mask_color|shape=\"ribbon\";mask_file=\"sample-leaf.png\";mask_type=\"image_threshold\";mask_mode=\"inlay\";label=\"\";overlay_file=\"\""
    "corner-cutout-fill|base_color,border_color|shape=\"corner\";mask_fit=\"fill\";overlay_file=\"\";label=\"\""
    "corner-png-inlay|base_color,border_color,text_color,mask_color,overlay_color|shape=\"corner\";mask_file=\"sample-leaf.png\";mask_type=\"image_threshold\";mask_mode=\"inlay\";mask_rotation=45;overlay_x=-12;overlay_y=-12;overlay_scale=30;label=\"Page 1\""
    "rounded_tab-svg-emboss|base_color,border_color,mask_color|shape=\"rounded_tab\";mask_mode=\"emboss\";label=\"\";overlay_file=\"\""
    "scalloped-svg-deboss-two-tone|base_color,border_color,top_color,text_color|shape=\"scalloped\";mask_mode=\"deboss\";two_tone=true;overlay_file=\"\""
    "star_top-png-emboss-repeat|base_color,topper_color,border_color,text_color,mask_color|shape=\"star_top\";mask_file=\"sample-leaf.png\";mask_mode=\"emboss\";mask_repeat=2;text_style=\"raised\";overlay_file=\"\""
    "heart_top-svg-cutout-invert|base_color,topper_color,border_color|shape=\"heart_top\";mask_file=\"sample-cat.svg\";mask_invert=true;label=\"\";overlay_file=\"\""
    "custom_mask-svg-outline|base_color,border_color|shape=\"custom_mask\";mask_file=\"sample-cat.svg\";label=\"\";overlay_file=\"\""
    "png-outline-no-border|base_color,border_color|shape=\"classic\";mask_mode=\"outline\";mask_file=\"sample-leaf.png\";outline_border=0;label=\"\";overlay_file=\"\""
    "png-cutout-centre-ties|base_color,border_color|shape=\"classic\";mask_file=\"sample-leaf.png\";tie_bars=\"centre\";label=\"\";overlay_file=\"\""
    "svg-inlay-fill|base_color,border_color,mask_color|shape=\"classic\";mask_mode=\"inlay\";mask_fit=\"fill\";label=\"\";overlay_file=\"\""
    "svg-inlay-stretch-no-border|base_color,mask_color|shape=\"classic\";mask_file=\"sample-cat.svg\";mask_mode=\"inlay\";mask_fit=\"stretch\";border=false;label=\"\";overlay_file=\"\""
    "png-deboss|base_color,border_color|shape=\"classic\";mask_file=\"sample-leaf.png\";mask_mode=\"deboss\";label=\"\";overlay_file=\"\""
    "svg-cutout-invert-grid|base_color,border_color|shape=\"classic\";mask_invert=true;tie_bars=\"grid\";tie_pitch=6;label=\"\";overlay_file=\"\""
    "cutout-plus-png-overlay-raised|base_color,border_color,overlay_color|shape=\"classic\";overlay_file=\"sample-leaf.png\";overlay_style=\"raised\";overlay_scale=45;overlay_y=0;label=\"\""
    "cutout-repeat-rotated|base_color,border_color,text_color|shape=\"classic\";mask_file=\"sample-cat.svg\";mask_repeat=3;mask_rotation=20;mask_scale=80;overlay_file=\"\""
    "island-control|base_color,border_color|shape=\"classic\";mask_file=\"sample-rings.svg\";tie_bars=\"none\";label=\"\";overlay_file=\"\""
    "bridge-control|base_color,border_color|shape=\"classic\";mask_invert=true;tie_bars=\"grid\";tie_pitch=6;min_feature=0.8;label=\"\";overlay_file=\"\""
    "island-fixed|base_color,border_color|shape=\"classic\";mask_file=\"sample-rings.svg\";tie_bars=\"centre\";label=\"\";overlay_file=\"\""
    "missing-mask|base_color,border_color,text_color,overlay_color,bead_color,bead_color_2|mask_file=\"no-such-file.svg\""
    "missing-outline|base_color,border_color|shape=\"custom_mask\";mask_file=\"no-such-file.svg\";label=\"\";overlay_file=\"\""
    "missing-overlay|base_color,border_color,text_color,bead_color,bead_color_2|overlay_file=\"no-such-file.png\";overlay_type=\"image_threshold\""
    "refused-parent|base_color,border_color,text_color,bead_color,bead_color_2|mask_file=\"../model.scad\";overlay_file=\"/etc/hostname\""
    "note-corner-outline|base_color,border_color|shape=\"corner\";mask_mode=\"outline\";mask_file=\"sample-cat.svg\";overlay_file=\"\";label=\"\""
    "note-custom-mask-inlay|base_color,border_color|shape=\"custom_mask\";mask_mode=\"inlay\";mask_file=\"sample-cat.svg\";overlay_file=\"\";label=\"\""
    "dotdot-name-read|base_color,border_color|shape=\"custom_mask\";mask_file=\"cat..v2.svg\";label=\"\";overlay_file=\"\""
    "refused-subdir|base_color,border_color|shape=\"custom_mask\";mask_file=\"sub/x.svg\";overlay_file=\".hidden.svg\";label=\"\""
    "upload-names|base_color,border_color,overlay_color|shape=\"classic\";mask_file=\"_scadbuddy_solid_asset_0123456789abcdef.svg\";overlay_file=\"_scadbuddy_solid_asset_fedcba9876543210.png\";overlay_style=\"raised\";overlay_y=40;overlay_scale=40;label=\"\""
    "largest|base_color,border_color,text_color,overlay_color,bead_color,bead_color_2|length=250;width=80;mask_repeat=4;bead_count=6;overlay_file=\"sample-leaf.png\";overlay_type=\"image_threshold\";overlay_scale=40;overlay_y=90;label=\"Largest bookmark\""
)

# Every dropdown value of shape and mask_mode must appear in some case.
options() {
    sed -n "s/^$1 = .*\/\/ \[\(.*\)\]\$/\1/p" model.scad | tr ',' '\n' | sed 's/:.*//; s/[" ]//g'
}
for s in $(options shape); do
    printf '%s\n' "${CASES[@]}" | grep -q "shape=\"$s\"" || [ "$s" = "tassel" ] \
        || { echo "FAIL: no case covers shape $s"; exit 1; }
done
for m in $(options mask_mode); do
    printf '%s\n' "${CASES[@]}" | grep -q "mask_mode=\"$m\"" || [ "$m" = "cutout" ] \
        || { echo "FAIL: no case covers mask_mode $m"; exit 1; }
done

: > "$OUT/cases.txt"
for c in "${CASES[@]}"; do
    name="${c%%|*}"; rest="${c#*|}"; expect="${rest%%|*}"; defs_s="${rest#*|}"
    # ONLY=<regex> runs a subset while iterating.
    if [ -n "${ONLY:-}" ] && ! grep -Eq "$ONLY" <<< "$name"; then continue; fi
    defs=""
    IFS=";" read -ra kvs <<< "$defs_s"
    for d in "${kvs[@]}"; do [ -n "$d" ] && defs="$defs -D '$d'"; done
    rm -f "$OUT/$name"@*.stl
    start=$(date +%s%N)
    if ! in_image "openscad --backend=Manifold $defs -o $OUT/$name.3mf model.scad" >"$OUT/$name.log" 2>&1; then
        tail -20 "$OUT/$name.log"; echo "FAIL: $name did not render"; exit 1
    fi
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    # The diagnostics: the whole bookmark as one solid, and its thin parts.
    in_image "openscad --backend=Manifold $defs -D 'debug=\"solid\"' --export-format binstl -o $OUT/$name.solid.stl model.scad \
              && openscad --backend=Manifold $defs -D 'debug=\"thin\"' --export-format binstl -o $OUT/$name.thin.stl model.scad \
              && openscad --backend=Manifold $defs -D 'debug=\"face\"' --export-format binstl -o $OUT/$name.face.stl model.scad" \
        >"$OUT/$name.diag.log" 2>&1 || { tail -20 "$OUT/$name.diag.log"; echo "FAIL: $name diagnostics did not render"; exit 1; }
    printf '%s|%s|%s|%s\n' "$name" "$ms" "$expect" "$defs_s" >> "$OUT/cases.txt"
    printf '==> %-32s %6d ms\n' "$name" "$ms"

    # Every colour on its own, closed, through a wrapper like ScadBuddy's
    # (§6.3): a color() that only passes its target through.
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
    script=""
    for col in $colours; do
        printf '_t = "%s";\nmodule color(c, alpha = 1) { if (is_string(c) && (c == _t || c == "%s")) children(); }\ninclude <../model.scad>\n' \
            "$col" "$(tr 'A-F' 'a-f' <<< "$col")" > "$OUT/wrap-${col#\#}.scad"
        script="$script openscad --backend=Manifold $defs --export-format binstl -o $OUT/$name@${col#\#}.stl $OUT/wrap-${col#\#}.scad || exit 1;"
    done
    in_image "$script" >"$OUT/$name.parts.log" 2>&1 || { tail -20 "$OUT/$name.parts.log"; echo "FAIL: $name colour parts did not render"; exit 1; }
done

python3 - "$OUT" <<'PY'
import math, os, re, struct, sys, zipfile, xml.etree.ElementTree as ET
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]

DEFAULTS = {}
for line in open("model.scad"):
    m = re.match(r'^(\w+) = ("[^"]*"|[-\d.]+|true|false);', line)
    if m:
        v = m.group(2)
        DEFAULTS[m.group(1)] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))

failures, summary = [], []


def check(case, ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append("%s: %s" % (case, msg))


def load_3mf(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper()) for b in root.iter(NS + "base")]
    V = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(NS + "vertex")]
    T = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
         for t in root.iter(NS + "triangle")]
    return mats, V, T


def load_stl(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    return [struct.unpack("<12f", data[84 + 50 * k: 84 + 50 * k + 48])[3:] for k in range(n)]


def tri_volume(a, b, c):
    return (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6


def stl_volume(tris):
    return sum(tri_volume(t[0:3], t[3:6], t[6:9]) for t in tris)


def components(tris):
    """Connected bodies of a triangle soup: triangles sharing a vertex."""
    parent = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    keys = []
    for t in tris:
        k = [(round(t[i], 3), round(t[i + 1], 3), round(t[i + 2], 3)) for i in (0, 3, 6)]
        keys.append(k)
        ra = find(k[0])
        for x in k[1:]:
            rb = find(x)
            if ra != rb:
                parent[rb] = ra
    comp = defaultdict(list)
    for t, k in zip(tris, keys):
        comp[find(k[0])].append(t)
    return list(comp.values())


def seg_dist(p1, q1, p2, q2):
    """Distance between two 2D segments."""
    def pt_seg(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
        return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    if (cross(p1, q1, p2) * cross(p1, q1, q2) < 0) and (cross(p2, q2, p1) * cross(p2, q2, q1) < 0):
        return 0.0
    return min(pt_seg(p1, p2, q2), pt_seg(q1, p2, q2), pt_seg(p2, p1, q1), pt_seg(q2, p1, q1))


def bridge_width(tris, reach):
    """Narrowest solid between two different boundary loops of a face
    extruded 1 mm: the smallest distance between edges of different loops of
    its top face, searched out to `reach`."""
    top = [t for t in tris if abs(t[2] - 1) < 1e-4 and abs(t[5] - 1) < 1e-4 and abs(t[8] - 1) < 1e-4]
    count = Counter()
    for t in top:
        k = [(round(t[i], 4), round(t[i + 1], 4)) for i in (0, 3, 6)]
        for a, b in ((k[0], k[1]), (k[1], k[2]), (k[2], k[0])):
            count[(min(a, b), max(a, b))] += 1
    edges = [e for e, n in count.items() if n == 1]
    parent = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    loop = [find(a) for a, _ in edges]
    grid = defaultdict(list)
    for i, (a, b) in enumerate(edges):
        for gx in range(math.floor(min(a[0], b[0]) / reach), math.floor(max(a[0], b[0]) / reach) + 1):
            for gy in range(math.floor(min(a[1], b[1]) / reach), math.floor(max(a[1], b[1]) / reach) + 1):
                grid[(gx, gy)].append(i)
    best = reach
    for cell, idx in grid.items():
        near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((cell[0] + dx, cell[1] + dy), ())]
        for i in idx:
            for j in near:
                if j > i and loop[i] != loop[j]:
                    best = min(best, seg_dist(edges[i][0], edges[i][1], edges[j][0], edges[j][1]))
    return best


def ray_z(tris, x, y):
    """z of every crossing of the vertical line through (x, y)."""
    zs = []
    for t in tris:
        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = t[0:3], t[3:6], t[6:9]
        d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if abs(d) < 1e-12:
            continue
        a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
        b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
        c = 1 - a - b
        if min(a, b, c) >= -1e-9:
            zs.append(round(a * z1 + b * z2 + c * z3, 4))
    return sorted(set(zs))


for line in open(os.path.join(OUT, "cases.txt")):
    name, ms, expect, defs = line.rstrip("\n").split("|", 3)
    ms = int(ms)
    p = dict(DEFAULTS)
    for kv in (defs.split(";") if defs else []):
        k, v = kv.split("=", 1)
        p[k] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))
    log = open(os.path.join(OUT, name + ".log")).read()
    print("\n%s (%d ms): %s" % (name, ms, defs))
    m = re.search(r'ECHO: BOOKMARK = \["(\w+)", ([-\d.e]+), ([-\d.e]+), ([-\d.e]+), ([-\d.e]+), ([\d.e]+), ([\d.e]+), (\d+), '
                  r'([\d.e]+), (true|false), (true|false), ([\d.e]+), ([\d.e]+), ([\d.e]+)\]', log)
    if not m:
        check(name, False, "model echoed its BOOKMARK design line")
        continue
    shape = m.group(1)
    x0, x1, y0, y1, z1, ztop = (float(m.group(i)) for i in range(2, 8))
    beads = int(m.group(8))
    from_file, mask_effect = m.group(10) == "true", m.group(11) == "true"
    P, G, A = float(m.group(12)), float(m.group(13)), float(m.group(14))

    mats, V, T = load_3mf(os.path.join(OUT, name + ".3mf"))
    counts = Counter(t[3] for t in T)
    named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
    got = {mats[i][1] for i in named}
    want = {p[k].upper() for k in expect.split(",")}
    print("  parts: " + " ".join("%s(%d)" % (mats[i][1], counts[i]) for i in named))
    check(name, counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
    check(name, got == want, "colour parts are exactly %s (%d)" % (expect, len(want))
          + ("" if got == want else "; got %s" % sorted(got)))

    # Extent, without the beads.
    bead_cols = {p["bead_color"].upper(), p["bead_color_2"].upper()} if beads else set()
    bm = {i for i in named if mats[i][1] not in bead_cols}
    vs = {i for t in T if t[3] in bm for i in t[:3]}
    xs = [V[i][0] for i in vs]; ys = [V[i][1] for i in vs]; zs = [V[i][2] for i in vs]
    print("  bookmark %.2f..%.2f x %.2f..%.2f x %.3f..%.3f; echo %g..%g x %g..%g, top %g"
          % (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs), x0, x1, y0, y1, ztop))
    tol = 0.02
    check(name, abs(min(zs)) <= 1e-4 and abs(max(zs) - ztop) <= 1e-3,
          "on z=0 and %.2f mm tall" % ztop)
    check(name, min(xs) >= x0 - tol and max(xs) <= x1 + tol and min(ys) >= y0 - tol and max(ys) <= y1 + tol,
          "inside its %g x %g mm extent" % (x1 - x0, y1 - y0))
    if from_file:
        fill = max((max(xs) - min(xs)) / (x1 - x0), (max(ys) - min(ys)) / (y1 - y0))
        check(name, fill >= 0.97, "custom outline fills the width or the length (%.3f)" % fill)
    else:
        # Rounded / pointed ends lose a little at the tip; everything else is exact.
        slack = {"pointed": 6, "ribbon": 6, "star_top": 6}.get(shape, tol)
        check(name, max(xs) - min(xs) >= x1 - x0 - slack and max(ys) - min(ys) >= y1 - y0 - slack,
              "fills its extent (%.2f x %.2f)" % (max(xs) - min(xs), max(ys) - min(ys)))
    if shape not in ("corner",) and not from_file:
        check(name, abs(p["width"] - (max(xs) - min(xs))) < tol or shape in ("star_top", "heart_top"),
              "strip is %g mm wide" % p["width"])
    if beads:
        bv = {i for t in T if t[3] not in bm and t[3] != 0 for i in t[:3]}
        bz = max(V[i][2] for i in bv)
        check(name, abs(bz - 0.8 * p["bead_size"]) < 1e-3 and min(V[i][0] for i in bv) > max(xs),
              "%d bead(s), %.1f mm tall, beside the bookmark" % (beads, bz))

    # One piece.
    solid = load_stl(os.path.join(OUT, name + ".solid.stl"))
    bodies = components(solid)
    if name == "island-control":
        check(name, len(bodies) > 1, "negative control: the ring cutouts leave loose discs and the check sees them (%d bodies)" % len(bodies))
    else:
        check(name, len(bodies) == 1, "the bookmark is one connected body (%d)" % len(bodies))

    # Min feature: what the opening removes is only facet crumbs.
    thin = load_stl(os.path.join(OUT, name + ".thin.stl"))
    crumbs = [abs(stl_volume(c)) for c in components(thin)] if thin else []
    worst = max(crumbs) if crumbs else 0.0
    check(name, worst <= 0.3, "no thin slivers: opening the face by min_feature %.1f mm removes only facet crumbs "
          "(largest %.3f mm2, %d crumbs)" % (p["min_feature"], worst, len(crumbs)))
    # Bridges: the narrowest solid between two different edges of the face
    # (outer edge, cord hole, each cutout).
    gap = bridge_width(load_stl(os.path.join(OUT, name + ".face.stl")), 2 * p["min_feature"])
    if name == "bridge-control":
        check(name, gap < 1.2, "negative control: bridges built for 0.8 mm measure %.3f mm, under the default 1.2" % gap)
    else:
        check(name, gap >= 0.95 * p["min_feature"], "every bridge is at least min_feature %.1f mm wide (narrowest %.3f mm)"
              % (p["min_feature"], gap))

    # Colour parts, closed, sum to the whole.
    whole = sum(tri_volume(V[t[0]], V[t[1]], V[t[2]]) for t in T)
    stls = [f for f in os.listdir(OUT) if f.startswith(name + "@") and f.endswith(".stl")]
    if len(named) > 1:
        vols = {f: stl_volume(load_stl(os.path.join(OUT, f))) for f in stls}
        total = sum(vols.values())
        check(name, len(stls) == len(named), "each of the %d colours rendered on its own" % len(named))
        check(name, abs(total - whole) <= 1e-3 * whole,
              "colour parts do not overlap: parts sum to %.2f mm3, whole is %.2f mm3" % (total, whole))

    # Files.
    if name.startswith("missing"):
        check(name, ("Can't open file" in log or "couldn't be opened" in log) and "no-such-file" in log,
              "the missing file is reported (import: ERROR, surface: WARNING) and the render still completes")
    if name == "missing-outline":
        check(name, abs(max(xs) - min(xs) - p["width"]) < tol and abs(max(ys) - min(ys) - p["length"]) < tol,
              "a missing outline file falls back to the classic %g x %g outline" % (p["width"], p["length"]))
    if name.startswith("refused"):
        opened = [l for l in log.splitlines() if "Can't open" in l or "import" in l.lower()]
        check(name, not opened, "refused names are never opened (%s)" % (opened[:1] or "no import/open line"))
        for key in ("mask_file", "overlay_file"):
            if p[key]:
                check(name, ('NOTE: %s "%s" ignored' % (key, p[key])) in log, "%s %r is refused with a NOTE" % (key, p[key]))

    NOTES = {
        "note-corner-outline": "NOTE: the page-corner shape keeps its triangle; mask_mode outline is ignored",
        "note-custom-mask-inlay": "NOTE: shape custom_mask uses the mask file as the outline; mask_mode is ignored",
    }
    if name in NOTES:
        check(name, NOTES[name] in log, "the ignored mask_mode is reported: %s" % NOTES[name])

    if name == "dotdot-name-read":
        check(name, ('NOTE: mask_file "%s" ignored' % p["mask_file"]) not in log and "Can't open" not in log,
              "a bare name containing '..' (%r) is accepted and read" % p["mask_file"])

    if name == "upload-names":
        opened = "Can't open" in log or "couldn't be opened" in log
        face_area = abs(stl_volume(load_stl(os.path.join(OUT, name + ".face.stl"))))
        check(name, not opened and face_area < 0.9 * p["width"] * p["length"],
              "uploaded names (_scadbuddy_solid_asset_<hex>.svg/.png) are read: the cutout removed %.0f%% of the face"
              % (100 - 100 * face_area / (p["width"] * p["length"])))

    # Page corner: the pocket.
    if shape == "corner":
        check(name, G >= 0.1 + 0.2 - 1e-9, "page gap %.2f mm takes a 0.1 mm page with >= 0.2 mm clearance" % G)
        w = p["wall_width"]
        empty_gap, plates = True, True
        probes = []
        for t in [w + 1.5, A * 0.2, A * 0.3, A * 0.4, A / 2 - 1.5]:
            for off in (-0.25 * A, 0, 0.25 * A):
                x, y = -t + off / 2, -t - off / 2
                if x < -w - 1 and y < -w - 1 and x + y > -A:
                    probes.append((x, y))
        for x, y in probes:
            zs_ = ray_z(solid, x, y)
            inside = [z for z in zs_ if P + 1e-3 < z < P + G - 1e-3]
            if inside:
                empty_gap = False
            if not any(abs(z) < 1e-3 for z in zs_) or not any(abs(z - P) < 1e-3 for z in zs_):
                plates = False
        check(name, empty_gap, "no material in the %.2f mm gap at %d points from the corner to the open edge" % (G, len(probes)))
        check(name, plates, "the back plate is whole under the pocket")
        # The long edge is open: just outside it there is nothing.
        # The tips' rounding is compensated by moving the long edge out.
        h = A + math.sqrt(2) * min(p["corner_radius"], A / 6)
        outside = [(-h * f - 0.4, -h * (1 - f) - 0.4) for f in (0.3, 0.5, 0.7)]
        check(name, all(not ray_z(solid, x, y) for x, y in outside), "the long edge is open for the page to slide in")
        # And the corner itself is closed by the two walls.
        check(name, all(len(ray_z(solid, x, y)) == 2 for x, y in [(-w / 2, -A / 2), (-A / 2, -w / 2)]),
              "the two short edges are solid walls, full height")

    if name == "largest":
        check(name, ms < 120000, "renders inside ScadBuddy's 120 s job timeout (%.1f s)" % (ms / 1000))
    summary.append("%s: %d parts, %.1f s" % (name, len(named), ms / 1000))

print()
for s in summary:
    print("  " + s)
if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nOK: all cases passed")
PY
