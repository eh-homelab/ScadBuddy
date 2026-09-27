#!/usr/bin/env bash
# Render models/flexi-fabric for every pattern and every colour mode (both
# lists are read from the dropdown annotations in model.scad, so a newly added
# mode is tested automatically), plus overlay, two-tone, shape, tight-size and
# largest-size cases, and check each 3MF:
#
#   - no uncoloured geometry, the expected number of colour parts
#   - on z=0, as tall as the bar levels imply, inside width x height
#   - print in place: one connected body per link (count == links placed),
#     every link reachable from every other through overlapping neighbours
#   - sampled around the sheet centre: no two links closer than `clearance`
#     (vertex-to-face and edge-to-edge), and every overlapping pair is
#     interlocked: each passes over the other somewhere (neither can be lifted
#     off the other)
#   - colour parts do not overlap: every colour rendered closed on its own
#     (ScadBuddy's per-colour wrapper) sums to the volume of the whole render
#
# The XML checking runs on the host with python3 and the standard library.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"
mkdir -p "$OUT"

# No text in this model, so any OpenSCAD image will do; prefer the ScadBuddy
# fonts image when it exists so every template verifies in the same image.
IMAGE="$BASE_IMAGE"
if docker image inspect "$FONTS_IMAGE" >/dev/null 2>&1; then
    IMAGE="$FONTS_IMAGE"
fi
echo "==> rendering with $IMAGE"

scad() { docker run --rm -v "$PWD":/w -w /w "$IMAGE" openscad --backend=Manifold "$@"; }

# Values of a dropdown annotation: `name = "x"; // [a:Label, b, ...]` -> a b ...
options() {
    sed -n "s/^$1 = .*\/\/ \[\(.*\)\]\$/\1/p" model.scad | tr ',' '\n' | sed 's/:.*//; s/[" ]//g'
}
PATTERNS=$(options pattern)
MODES=$(options colour_mode)
[ -n "$PATTERNS" ] && [ -n "$MODES" ] || { echo "FAIL: could not read the dropdowns"; exit 1; }

# name | -D overrides separated by ";"
CASES=()
for p in $PATTERNS; do
    CASES+=("pattern-$p|pattern=\"$p\";width=70;height=70;colour_mode=\"checker\"")
done
for m in $MODES; do
    CASES+=("colour-$m|colour_mode=\"$m\"")
done
CASES+=(
    "two-tone|colour_mode=\"checker\";two_tone=true;top_layers=2"
    "overlay-svg-links|colour_mode=\"overlay_only\";palette_1=\"#1E88E5\";overlay_file=\"sample-overlay.svg\""
    "overlay-svg-inlay|colour_mode=\"checker\";overlay_file=\"sample-overlay.svg\";overlay_detail=\"inlay\";top_layers=2"
    "overlay-png-links|overlay_file=\"sample-overlay.png\";overlay_type=\"image_threshold\";image_threshold=50;overlay_scale=90"
    "overlay-invert-two-tone|colour_mode=\"checker\";two_tone=true;top_color=\"#FFEB3B\";overlay_file=\"sample-overlay.svg\";overlay_invert=true;overlay_scale=60;overlay_rotation=30;overlay_x=10"
    "overlay-hex|pattern=\"hex_scales\";colour_mode=\"rows\";overlay_file=\"sample-overlay.svg\""
    "overlay-triflex-inlay|pattern=\"triflex_triangles\";overlay_file=\"sample-overlay.png\";overlay_type=\"image_threshold\";overlay_detail=\"inlay\""
    "overlay-missing|overlay_file=\"no-such-file.svg\""
    "shape-heart|shape=\"heart\";pattern=\"chainmail_rings\";colour_mode=\"gradient_bands\""
    "shape-star|shape=\"star\";width=150;height=150;colour_mode=\"random_seeded\""
    "shape-star-triflex|shape=\"star\";pattern=\"triflex_triangles\";colour_mode=\"rainbow\""
    "small-sheet-big-links|width=40;height=40;link_size=15;shape=\"heart\";pattern=\"hex_scales\";colour_mode=\"single\""
    "shape-circle|shape=\"circle\";width=100;height=70;pattern=\"diamond\""
    "shape-hexagon|shape=\"hexagon\";pattern=\"hex_scales\";colour_mode=\"stripes\""
    "tight-square|link_size=5;clearance=0.6;width=40;height=40;colour_mode=\"single\""
    "tight-rings|pattern=\"chainmail_rings\";link_size=5;clearance=0.6;width=40;height=40;colour_mode=\"single\""
    "big-links-thin-layers|link_size=15;layer_height=0.12;bar_layers=6;bar_width=2;clearance=0.2;width=80;height=60"
    "largest|width=300;height=300;link_size=5;colour_mode=\"checker\""
)

# Colours of the palette / top / overlay parameters, for the per-colour renders.
default_of() { sed -n "s/^$1 = \"\(#[0-9A-Fa-f]*\)\".*/\1/p" model.scad; }

: > "$OUT/cases.txt"
for c in "${CASES[@]}"; do
    name="${c%%|*}"
    # ONLY=<regex> runs a subset while iterating.
    if [ -n "${ONLY:-}" ] && ! grep -Eq "$ONLY" <<< "$name"; then continue; fi
    defs=()
    IFS=";" read -ra kvs <<< "${c#*|}"
    for d in "${kvs[@]}"; do [ -n "$d" ] && defs+=(-D "$d"); done
    rm -f "$OUT/$name"@*.stl
    start=$(date +%s%N)
    if ! scad "${defs[@]}" -o "$OUT/$name.3mf" model.scad >"$OUT/$name.log" 2>&1; then
        tail -20 "$OUT/$name.log"; echo "FAIL: $name did not render"; exit 1
    fi
    ms=$(( ($(date +%s%N) - start) / 1000000 ))
    printf '%s %s %s\n' "$name" "$ms" "${c#*|}" >> "$OUT/cases.txt"
    printf '==> %-26s %6d ms\n' "$name" "$ms"

    # Every colour on its own, closed, through a wrapper like ScadBuddy's
    # (§6.3): a color() that only passes its target through. Skipped for the
    # largest case, which only measures time and structure.
    [ "$name" = "largest" ] && continue
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
from collections import Counter, defaultdict

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
OUT = sys.argv[1]

# Defaults straight from the model source, so the checks follow the file.
DEFAULTS = {}
for line in open("model.scad"):
    m = re.match(r'^(\w+) = ("[^"]*"|[-\d.]+|true|false);', line)
    if m:
        v = m.group(2)
        DEFAULTS[m.group(1)] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))

failures = []
summary = []


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


def stl_volume(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    vol = 0.0
    for k in range(n):
        f = struct.unpack("<12f", data[84 + 50 * k: 84 + 50 * k + 48])
        a, b, c = f[3:6], f[6:9], f[9:12]
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return vol


def volume(V, T):
    vol = 0.0
    for t in T:
        a, b, c = V[t[0]], V[t[1]], V[t[2]]
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return vol


# ---- geometry for the clearance sample ------------------------------------
def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def closest_on_tri(p, a, b, c):
    # Ericson, Real-Time Collision Detection 5.1.5.
    ab, ac, ap = sub(b, a), sub(c, a), sub(p, a)
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = sub(p, b)
    d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        return (a[0] + v * ab[0], a[1] + v * ab[1], a[2] + v * ab[2])
    cp = sub(p, c)
    d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        return (a[0] + w * ac[0], a[1] + w * ac[1], a[2] + w * ac[2])
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return (b[0] + w * (c[0] - b[0]), b[1] + w * (c[1] - b[1]), b[2] + w * (c[2] - b[2]))
    den = 1.0 / (va + vb + vc)
    v, w = vb * den, vc * den
    return (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w, a[2] + ab[2] * v + ac[2] * w)


def seg_seg(p1, q1, p2, q2):
    # Ericson 5.1.9: distance between segments p1q1 and p2q2.
    d1, d2, r = sub(q1, p1), sub(q2, p2), sub(p1, p2)
    a, e, f = dot(d1, d1), dot(d2, d2), dot(d2, r)
    if a <= 1e-12 and e <= 1e-12:
        return math.dist(p1, p2)
    if a <= 1e-12:
        s, t = 0.0, min(1, max(0, f / e))
    else:
        c = dot(d1, r)
        if e <= 1e-12:
            t, s = 0.0, min(1, max(0, -c / a))
        else:
            b = dot(d1, d2)
            den = a * e - b * b
            s = min(1, max(0, (b * f - c * e) / den)) if den > 1e-12 else 0.0
            t = (b * s + f) / e
            if t < 0:
                t, s = 0.0, min(1, max(0, -c / a))
            elif t > 1:
                t, s = 1.0, min(1, max(0, (b - c) / a))
    c1 = (p1[0] + d1[0] * s, p1[1] + d1[1] * s, p1[2] + d1[2] * s)
    c2 = (p2[0] + d2[0] * t, p2[1] + d2[1] * t, p2[2] + d2[2] * t)
    return math.dist(c1, c2)


def body_gap(V, ta, tb, reach):
    """Smallest distance between two bodies' surfaces, searched out to reach."""
    cell = max(reach, 0.5)
    grid = defaultdict(list)
    for t in tb:
        pts = [V[i] for i in t]
        lo = [min(p[k] for p in pts) - reach for k in range(3)]
        hi = [max(p[k] for p in pts) + reach for k in range(3)]
        for i in range(math.floor(lo[0] / cell), math.floor(hi[0] / cell) + 1):
            for j in range(math.floor(lo[1] / cell), math.floor(hi[1] / cell) + 1):
                for k in range(math.floor(lo[2] / cell), math.floor(hi[2] / cell) + 1):
                    grid[(i, j, k)].append(t)
    best = reach
    seen = set()
    for t in ta:
        for vi in t:
            p = V[vi]
            key = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
            for u in grid.get(key, ()):
                d = math.dist(p, closest_on_tri(p, V[u[0]], V[u[1]], V[u[2]]))
                best = min(best, d)
        for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            p = V[e[0]]
            key = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
            for u in grid.get(key, ()):
                for f in ((u[0], u[1]), (u[1], u[2]), (u[2], u[0])):
                    k = (min(e), max(e), min(f), max(f))
                    if k in seen:
                        continue
                    seen.add(k)
                    best = min(best, seg_seg(V[e[0]], V[e[1]], V[f[0]], V[f[1]]))
    return best


def tri2d_overlap(a, b):
    # Separating axis test for two 2D triangles, positive-area overlap only.
    for poly in (a, b):
        for i in range(3):
            p, q = poly[i], poly[(i + 1) % 3]
            n = (q[1] - p[1], p[0] - q[0])
            pa = [n[0] * x[0] + n[1] * x[1] for x in a]
            pb = [n[0] * x[0] + n[1] * x[1] for x in b]
            if max(pa) <= min(pb) + 1e-9 or max(pb) <= min(pa) + 1e-9:
                return False
    return True


def horizontal(V, tris, up):
    out = []
    for t in tris:
        a, b, c = V[t[0]], V[t[1]], V[t[2]]
        if abs(a[2] - b[2]) > 1e-6 or abs(a[2] - c[2]) > 1e-6:
            continue
        nz = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(nz) < 1e-9 or (nz > 0) != up:
            continue
        out.append((a[2], [(a[0], a[1]), (b[0], b[1]), (c[0], c[1])]))
    return out


def passes_over(V, top_tris, bottom_tris):
    """Some down-facing face of `top` lies above an up-facing face of `bottom`."""
    downs = horizontal(V, top_tris, False)
    ups = horizontal(V, bottom_tris, True)
    for za, ta in downs:
        for zb, tb in ups:
            if zb < za - 1e-6 and tri2d_overlap(ta, tb):
                return True
    return False


# ---- per case -------------------------------------------------------------
for line in open(os.path.join(OUT, "cases.txt")):
    parts = line.rstrip("\n").split(" ", 2)
    name, ms = parts[0], int(parts[1])
    p = dict(DEFAULTS)
    for kv in (parts[2].split(";") if len(parts) > 2 and parts[2] else []):
        k, v = kv.split("=", 1)
        p[k] = v.strip('"') if v.startswith('"') else (v == "true" if v in ("true", "false") else float(v))
    log = open(os.path.join(OUT, name + ".log")).read()
    m = re.search(r'ECHO: FLEXI = \["(\w+)", (\d+), ([\d.]+), ([\d.]+), ([-\d.e]+), (\d+), ([\d.]+), ([\d.]+), ([\d.]+), ([\d.]+)', log)
    print("\n%s (%d ms): %s" % (name, ms, parts[2] if len(parts) > 2 else ""))
    if not m:
        check(name, False, "model echoed its FLEXI design line")
        continue
    pat, nlinks, link, barw, gap, levels, total_h, hb, vg, pitch = m.groups()
    nlinks, levels = int(nlinks), int(levels)
    link, barw, total_h, pitch = float(link), float(barw), float(total_h), float(pitch)
    c = p["clearance"]
    print("  pattern %s: %d links, link %.1f mm, bar %.1f mm, %d levels, %.2f mm tall, pitch %.3f"
          % (pat, nlinks, link, barw, levels, total_h, pitch))

    mats, V, T = load_3mf(os.path.join(OUT, name + ".3mf"))
    counts = Counter(t[3] for t in T)
    named = [i for i, (n, _) in enumerate(mats) if n != "Default" and counts.get(i)]
    print("  parts: " + " ".join("%s(%d)" % (mats[i][1], counts[i]) for i in named))
    xs, ys, zs = zip(*V)
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    print("  bbox %.2f x %.2f x %.3f (sheet %g x %g)" % (dx, dy, max(zs) - min(zs), p["width"], p["height"]))

    # Expected parts: the palette colours the mode uses, top, overlay.
    ncol = int(p["colour_count"])
    mode = p["colour_mode"]
    classes = {"hex_scales": 3}.get(pat, 2)
    expect_pal = {"single": 1, "overlay_only": 1, "checker": classes}.get(mode)
    overlay = p["overlay_file"] != "" and os.path.exists(p["overlay_file"])
    extra = (1 if p["two_tone"] else 0) + (1 if overlay else 0)

    check(name, counts.get(0, 0) == 0, "Default material carries no geometry (%d triangles)" % counts.get(0, 0))
    if expect_pal is not None:
        check(name, len(named) == expect_pal + extra,
              "%d colour part(s) (%d palette + %d top/overlay; got %d)" % (expect_pal + extra, expect_pal, extra, len(named)))
    else:
        check(name, 2 <= len(named) - extra <= ncol,
              "2..%d palette parts for mode %s (got %d)" % (ncol, mode, len(named) - extra))
    if overlay:
        oc = p["overlay_color"].upper()
        check(name, any(mats[i][1] == oc for i in named), "overlay colour %s is present" % oc)
    if p["overlay_file"] and not overlay:
        check(name, "no-such-file" in log, "missing overlay file is reported in the log and the render still completes")
        check(name, not any(mats[i][1] == p["overlay_color"].upper() for i in named),
              "missing overlay file adds no overlay part")
    check(name, abs(min(zs)) <= 1e-4, "sits on z=0 (min z %.4f)" % min(zs))
    exp_h = (levels - 1) * (float(hb) + float(vg)) + float(hb)
    check(name, abs(max(zs) - total_h) <= 1e-3 and abs(total_h - exp_h) <= 1e-6,
          "height %.3f = %d levels of %.2f bars + %.2f gaps" % (max(zs), levels, float(hb), float(vg)))
    check(name, float(vg) >= c - 1e-9, "vertical gap between levels %.2f >= clearance %.2f" % (float(vg), c))
    check(name, dx <= p["width"] + 1e-3 and dy <= p["height"] + 1e-3,
          "inside the %g x %g sheet" % (p["width"], p["height"]))
    if p["shape"] == "rectangle":
        check(name, dx >= p["width"] - (link + 2 * pitch) and dy >= p["height"] - (link + 2 * pitch),
              "fills the sheet to within one link and two pitches")
    check(name, nlinks > 1, "more than one link placed")

    # Bodies: union-find over coincident vertices, across all materials (the
    # top layers / overlay of a link share its outline vertices).
    key = {}
    parent = list(range(len(V)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, v in enumerate(V):
        k = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
        if k in key:
            union(i, key[k])
        else:
            key[k] = i
    for t in T:
        union(t[0], t[1]); union(t[1], t[2])
    btris = defaultdict(list)
    for t in T:
        btris[find(t[0])].append(t[:3])
    # A link split between colours (overlay inlay, top layers) can meet its
    # other part along a face without sharing a vertex: merge components one
    # of whose vertices lies on the other's surface.
    comps = list(btris.values())

    def cbox(tris):
        vs = {i for t in tris for i in t}
        return [min(V[i][k] for i in vs) for k in range(3)] + [max(V[i][k] for i in vs) for k in range(3)]

    if len(comps) > nlinks:
        boxes = [cbox(cc) for cc in comps]
        cp = list(range(len(comps)))

        def cfind(a):
            while cp[a] != a:
                cp[a] = cp[cp[a]]
                a = cp[a]
            return a
        order = sorted(range(len(comps)), key=lambda k: boxes[k][0])
        for ai, a in enumerate(order):
            for b in order[ai + 1:]:
                if boxes[b][0] > boxes[a][3] + 1e-4:
                    break
                if all(boxes[a][k] <= boxes[b][k + 3] + 1e-4 and boxes[b][k] <= boxes[a][k + 3] + 1e-4
                       for k in (1, 2)) and cfind(a) != cfind(b):
                    small, big = (a, b) if len(comps[a]) < len(comps[b]) else (b, a)
                    vs = {i for t in comps[small] for i in t}
                    if any(math.dist(V[i], closest_on_tri(V[i], V[u[0]], V[u[1]], V[u[2]])) < 1e-4
                           for i in vs for u in comps[big]):
                        cp[cfind(a)] = cfind(b)
        merged = defaultdict(list)
        for k, cc in enumerate(comps):
            merged[cfind(k)].extend(cc)
        comps = list(merged.values())
    bodies = comps
    check(name, len(bodies) == nlinks, "one body per link: %d bodies, %d links" % (len(bodies), nlinks))

    bbox = []
    ups = []
    for b in bodies:
        vs = {i for t in b for i in t}
        bx = [V[i][0] for i in vs]; by = [V[i][1] for i in vs]
        bbox.append((min(bx), max(bx), min(by), max(by)))
        ups.append(horizontal(V, b, True))

    def plan_overlap(a, b):
        """Do the two links overlap in plan (their top faces' shadows)?"""
        x0, x1 = max(bbox[a][0], bbox[b][0]), min(bbox[a][1], bbox[b][1])
        y0, y1 = max(bbox[a][2], bbox[b][2]), min(bbox[a][3], bbox[b][3])

        def clip(faces):
            return [t for _, t in faces
                    if max(p[0] for p in t) > x0 and min(p[0] for p in t) < x1
                    and max(p[1] for p in t) > y0 and min(p[1] for p in t) < y1]
        fa, fb = clip(ups[a]), clip(ups[b])
        return any(tri2d_overlap(ta, tb) for ta in fa for tb in fb)

    # Links overlap in plan exactly where they are linked; the sheet must be
    # one connected net through those overlaps. (The largest case uses the
    # cheaper bounding-box overlap.)
    n = len(bodies)
    order = sorted(range(n), key=lambda k: bbox[k][0])
    adj = defaultdict(set)
    for a_i, a in enumerate(order):
        for b in order[a_i + 1:]:
            if bbox[b][0] >= bbox[a][1] - 1e-3:
                break
            if bbox[a][2] < bbox[b][3] - 1e-3 and bbox[b][2] < bbox[a][3] - 1e-3 \
                    and (name == "largest" or plan_overlap(a, b)):
                adj[a].add(b); adj[b].add(a)
    seen, stack = {0}, [0]
    while stack:
        for nb in adj[stack.pop()]:
            if nb not in seen:
                seen.add(nb); stack.append(nb)
    check(name, len(seen) == n, "every link is connected to the sheet (%d of %d reached)" % (len(seen), n))

    if name == "largest":
        summary.append("%s: %d links, %.1f s" % (name, nlinks, ms / 1000))
        check(name, ms < 120000, "renders inside ScadBuddy's 120 s job timeout (%.1f s)" % (ms / 1000))
        continue

    # Sample: the bodies nearest the sheet centre.
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    near = sorted(range(n), key=lambda k: math.hypot((bbox[k][0] + bbox[k][1]) / 2 - cx,
                                                      (bbox[k][2] + bbox[k][3]) / 2 - cy))[:9]
    pairs = sorted({(min(a, b), max(a, b)) for a in near for b in adj[a]})
    worst = 9.0
    for a, b in pairs:
        worst = min(worst, body_gap(V, bodies[a], bodies[b], 2 * c), body_gap(V, bodies[b], bodies[a], 2 * c))
    # Faceted arcs sit a hair inside the true curve; allow 2%.
    check(name, worst >= 0.98 * c,
          "sampled %d linked pairs: no two links closer than %.2f (min gap %.3f)" % (len(pairs), c, worst))
    locked = [passes_over(V, bodies[a], bodies[b]) and passes_over(V, bodies[b], bodies[a]) for a, b in pairs]
    check(name, all(locked) and len(pairs) > 0,
          "every sampled pair is interlocked, each passing over the other (%d/%d)" % (sum(locked), len(pairs)))
    deg = [len(adj[k]) for k in near]
    if nlinks >= 30:
        check(name, min(deg) >= 2, "centre links interlock with >= 2 neighbours (%s)" % deg)

    # Colour parts: rendered one at a time, closed, they add up to the whole.
    stls = [f for f in os.listdir(OUT) if f.startswith(name + "@") and f.endswith(".stl")]
    if len(named) > 1:
        whole = volume(V, T)
        vols = {f: stl_volume(os.path.join(OUT, f)) for f in stls}
        total = sum(vols.values())
        check(name, len(stls) == len(named), "each of the %d colours rendered on its own" % len(named))
        check(name, abs(total - whole) <= 1e-3 * whole,
              "colour parts do not overlap: parts sum to %.2f mm3, whole is %.2f mm3" % (total, whole))
    summary.append("%s: %d links, %d parts, %.1f s" % (name, nlinks, len(named), ms / 1000))

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
