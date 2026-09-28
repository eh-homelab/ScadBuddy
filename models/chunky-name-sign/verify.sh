#!/usr/bin/env bash
# Render models/chunky-name-sign with the defaults and each option, then check
# concrete facts: the set of colour parts, that no geometry is uncoloured, that
# everything sits on z=0 at the height the parameters imply, and -- from one
# closed render per colour, the way ScadBuddy builds its parts -- that each part
# sits at its expected height, that the letters and the backing are one
# connected piece each, that auto-fit holds the word to max_length, that the
# flat bottom is flat, that magnet pockets remove exactly their volume, and
# that the colour parts do not overlap (their volumes add up to the volume of
# the whole model rendered as one solid).
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# Render in the ScadBuddy fonts image. Build it from the base image with the
# same Debian font packages when it is missing -- a face that is not installed
# falls back silently and every measurement below would be meaningless.
IMAGE="$FONTS_IMAGE"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building $IMAGE from $BASE_IMAGE with the image's font packages"
    docker build -q -t "$IMAGE" - <<DOCKERFILE
FROM $BASE_IMAGE
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core \\
 && fc-cache -f \\
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
fi
FAMILIES="$(docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" "$IMAGE" fc-list : family)"
for family in "DejaVu Sans" "Lobster Two" "Noto Sans"; do
    if ! printf '%s\n' "$FAMILIES" | grep -F "$family" >/dev/null; then
        echo "FAIL: $IMAGE has no '$family'" >&2
        exit 1
    fi
done
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01

# Defaults, mirrored from model.scad.
D = dict(text="Emma", text_size=40, boldness=1, auto_fit=True, max_length=180,
         layout="horizontal", text_shape="straight", border=5,
         backing_thickness=7, letter_height=4, outer_ring=False, ring_width=3,
         text_outline=False, outline_width=2, stand="none", mount="none",
         magnet_d=10, magnet_h=2)
FOOT_H = 2 + 8                       # foot floor + lip
MAGNET_CLEAR_D, MAGNET_CLEAR_H, FN = 0.3, 0.2, 48
BACK, TEXT, RING, OUTLINE = "#FFD23F", "#1565C0", "#E53935", "#FFFFFF"

EXTRAS = dict(outer_ring=True, text_outline=True)
# name, overrides, expected pieces in the letters (None: not checked)
CASES = [
    ("defaults", {}, 1),
    ("fit-long", dict(text="WOLFGANG MOZART"), 2),
    ("nofit-long", dict(text="Charlotte", auto_fit=False), 1),
    ("small-size", dict(text="Charlotte", text_size=20), 1),
] + [
    ("shape-" + sh, dict(text="Charlotte", text_shape=sh), 1)
    for sh in ["arch_up", "arch_down", "circle", "wave", "slant_up",
               "slant_down", "bulge", "pinch", "perspective", "stairs"]
] + [
    ("rotated-extras", dict(layout="vertical_rotated", stand="flat_bottom", **EXTRAS), 1),
    ("rotated-extras-magnets", dict(layout="vertical_rotated", stand="flat_bottom",
                                    mount="magnets", **EXTRAS), 1),
    ("stacked-foot-keyholes", dict(text="LUCAS", layout="vertical_stacked",
                                   stand="foot", mount="keyholes"), 1),
    ("stacked-long-fit", dict(text="ALEXANDRA", layout="vertical_stacked"), None),
    ("stacked-lower-gap", dict(text="Mia", layout="vertical_stacked", row_gap=8), 2),
    ("arch-extras-foot", dict(text_shape="arch_up", stand="foot", **EXTRAS), 1),
    ("arch-extras-foot-magnets", dict(text_shape="arch_up", stand="foot",
                                      mount="magnets", **EXTRAS), 1),
    ("circle-ring-keyholes", dict(text="Charlotte", text_shape="circle",
                                  outer_ring=True, mount="keyholes"), 1),
    ("rotated-circle", dict(text_shape="circle", layout="vertical_rotated",
                            stand="flat_bottom"), 1),
    ("wave-two-words", dict(text="Anna Lena", text_shape="wave", text_outline=True), 2),
    ("generic-font-bulge", dict(text="Oscar", font="Noto Sans:style=Bold",
                                text_shape="bulge", stand="flat_bottom"), None),
    ("lobster-arch-down", dict(text="Rosa", font="Lobster Two:style=Bold",
                               text_shape="arch_down"), None),
    ("thin-square-magnets", dict(backing_thickness=2, bevel=0, mount="magnets"), 1),
    ("empty-text", dict(text=""), 1),
    # Parameters that would run off the 300 x 320 mm plate are capped to it.
    ("plate-long", dict(text="Christopher", max_length=400, text_size=100), None),
    ("plate-nofit", dict(text="ABCDEFGHIJKLMNOPQRST", auto_fit=False, text_size=100), None),
    ("plate-rotated-foot", dict(text="Christopher", layout="vertical_rotated",
                                max_length=400, text_size=100, stand="foot"), None),
    ("plate-circle", dict(text="Charlotte", text_shape="circle", circle_radius=150,
                          outer_ring=True), 1),
    ("plate-stairs", dict(text="ABCDEFGHIJKLMNOPQRST", text_shape="stairs",
                          stair_step=30, stand="foot"), None),
]
PLATE_W, PLATE_D = 300, 320


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


def docker(script):
    r = subprocess.run(["docker", "run", "--rm", "--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local"), "-v", os.getcwd() + ":/w", "-w", "/w",
                        IMAGE, "bash", "-ec", script], capture_output=True, text=True)
    if r.returncode or "WARNING" in r.stderr or "ERROR" in r.stderr:
        print(r.stderr)
        sys.exit("FAIL: OpenSCAD reported errors or warnings")


def read_3mf(path):
    root = ET.fromstring(zipfile.ZipFile(path).read("3D/3dmodel.model"))
    mats = [(b.get("name"), (b.get("displaycolor") or "")[:7].upper())
            for b in root.iter(NS + "base")]
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
             for v in root.iter(NS + "vertex")]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")), int(t.get("p1") or 0))
            for t in root.iter(NS + "triangle")]
    return mats, verts, tris


def read_stl(path):
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:300]:
        vs = [tuple(map(float, m.groups())) for m in re.finditer(
            rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        return [vs[i:i + 3] for i in range(0, len(vs), 3)]
    n = struct.unpack("<I", data[80:84])[0]
    tris = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        tris.append([f[3:6], f[6:9], f[9:12]])
    return tris


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


def pieces(tris):
    """Connected pieces of a closed mesh (triangles sharing a vertex)."""
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for t in tris:
        ks = [tuple(round(c, 4) for c in v) for v in t]
        for k in ks:
            parent.setdefault(k, k)
        for k in ks[1:]:
            ra, rb = find(ks[0]), find(k)
            if ra != rb:
                parent[ra] = rb
    return len({find(k) for k in parent})


# ---- phase 1: combined renders (colours as materials)
docker("\n".join("openscad --backend=Manifold %s -o %s/%s.3mf model.scad"
                 % (defines(ov), OUT, name) for name, ov, _ in CASES))
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _, _ in CASES}

# ---- phase 2: one closed render per colour (ScadBuddy's colour wrapper), and
# one render of everything as a single solid for the overlap check.
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (_sb_t == "*" || c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov, _ in CASES:
    mats, _, tris = combined[name]
    used = Counter(t[3] for t in tris)
    for i, (n, col) in enumerate(mats):
        if n != "Default" and used.get(i):
            jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                        % (defines(ov), col, OUT, name, col[1:], OUT))
    jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"*\"' -o %s/%s_all.stl %s/wrap.scad"
                % (defines(ov), OUT, name, OUT))
docker("\n".join(jobs))

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def p(ov, k):
    return ov.get(k, D[k])


closed = {}
for name, ov, want_pieces in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}

    T, LH = p(ov, "backing_thickness"), p(ov, "letter_height")
    roles = {"backing": BACK, "text": TEXT}
    if p(ov, "outer_ring"):
        roles["ring"] = RING
    if p(ov, "text_outline"):
        roles["outline"] = OUTLINE
    check(named == set(roles.values()),
          "parts are %s (got %s)" % (sorted(roles.values()), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])
    check(hi[0] - lo[0] <= PLATE_W and hi[1] - lo[1] <= PLATE_D,
          "fits the %d x %d mm plate (%.1f x %.1f)"
          % (PLATE_W, PLATE_D, hi[0] - lo[0], hi[1] - lo[1]))
    top = max(T + LH, FOOT_H if p(ov, "stand") == "foot" else 0)
    check(near(hi[2], top), "top at z=%.3f (expected %.3f)" % (hi[2], top))

    parts = {role: read_stl("%s/%s_%s.stl" % (OUT, name, col[1:])) for role, col in roles.items()}
    closed[name] = parts
    box = {role: bbox([v for t in tr for v in t]) for role, tr in parts.items()}

    zexp = {"backing": (0, max(T, FOOT_H) if p(ov, "stand") == "foot" else T),
            "ring": (0, T), "text": (T, T + LH), "outline": (T, T + LH)}
    for role in roles:
        (blo, bhi), (zlo, zhi) = box[role], zexp[role]
        check(near(blo[2], zlo) and near(bhi[2], zhi),
              "%-7s closed part z %.3f .. %.3f (expected %.3f .. %.3f)"
              % (role, blo[2], bhi[2], zlo, zhi))

    # One piece each: the letters (where the word allows it) and the backing
    # (plus the separate foot).
    lp = pieces(parts["text"])
    if want_pieces is not None:
        check(lp == want_pieces, "letters are %d piece(s) (got %d)" % (want_pieces, lp))
    else:
        print("  info  letters are %d piece(s)" % lp)
    bp = pieces(parts["backing"])
    want_b = 2 if p(ov, "stand") == "foot" else 1
    check(bp == want_b, "backing is %d piece(s)%s (got %d)"
          % (want_b, " incl. the foot" if want_b == 2 else "", bp))
    if "ring" in parts:
        check(pieces(parts["ring"]) == 1, "outer ring is one piece")

    # No overlap: the colour parts' volumes add up to the whole.
    vsum = sum(volume(tr) for tr in parts.values())
    vall = volume(read_stl("%s/%s_all.stl" % (OUT, name)))
    check(abs(vsum - vall) <= 0.001 * vall,
          "colour parts do not overlap: sum %.1f == whole %.1f mm^3" % (vsum, vall))

    # Letter extents.
    (tlo, thi) = box["text"]
    tw, th = thi[0] - tlo[0], thi[1] - tlo[1]
    bold = p(ov, "boldness")
    horizontal = p(ov, "layout") != "vertical_stacked"
    length = th if p(ov, "layout") != "horizontal" else tw
    shaped = p(ov, "text_shape") not in ("straight", "slant_up", "slant_down") \
        and p(ov, "layout") != "vertical_stacked"
    if p(ov, "auto_fit") and p(ov, "text_shape") != "circle":
        # Straight text is fitted exactly; shaped text is fitted along its
        # path, and tilted letters may reach a little past it.
        slack = 2 * bold + (0.03 * p(ov, "max_length") if shaped else 0.5)
        check(length <= p(ov, "max_length") + slack,
              "auto-fit: word runs %.1f mm <= max_length %d + %.1f"
              % (length, p(ov, "max_length"), slack))
    if name == "fit-long":
        m = p(ov, "max_length")
        check(m - 0.5 <= tw <= m + 2 * bold + 0.5,
              "a long word is shrunk to max_length: %.1f in %.1f .. %.1f"
              % (tw, m - 0.5, m + 2 * bold + 0.5))
    if name == "nofit-long":
        check(tw > p(ov, "max_length") + 10,
              "without auto-fit the word keeps its size: %.1f > %d" % (tw, p(ov, "max_length")))
    if p(ov, "layout") == "vertical_stacked":
        check(th > 2 * tw, "stacked letters run down the sign: %.1f tall x %.1f wide" % (th, tw))
    if p(ov, "layout") == "vertical_rotated" and p(ov, "text_shape") == "straight":
        check(th > 2 * tw, "rotated word runs up the sign: %.1f tall x %.1f wide" % (th, tw))

    # Backing border: grown from the letters by `border` (straight, no stand).
    (blo, bhi) = box["backing"]
    if p(ov, "text_shape") == "straight" and p(ov, "stand") == "none" \
            and p(ov, "layout") == "horizontal":
        b = p(ov, "border")
        check(all(near(a, e, 0.1) for a, e in [(blo[0], tlo[0] - b), (bhi[0], thi[0] + b),
                                               (blo[1], tlo[1] - b), (bhi[1], thi[1] + b)]),
              "backing is the letters grown by the %.1f mm border" % b)
    if "ring" in parts:
        (rlo, rhi) = box["ring"]
        rw = p(ov, "ring_width")
        check(near(rhi[0], bhi[0] + rw, 0.1) and near(rlo[0], blo[0] - rw, 0.1),
              "ring is %.1f mm outside the backing" % rw)

    # Flat bottom: the lowest edge of the sign is a straight line.
    if p(ov, "stand") in ("flat_bottom", "foot"):
        sign = parts["ring"] if "ring" in parts else parts["backing"]
        pts = [v for t in sign for v in t]
        if p(ov, "stand") == "foot":
            # the sign is everything above the gap to the foot
            pts = [v for v in pts if v[1] > box["text"][0][1] - 40]
        ymin = min(v[1] for v in pts)
        xs = [v[0] for v in pts if near(v[1], ymin, 0.02)]
        span = max(xs) - min(xs)
        check(span >= 15, "flat bottom at y=%.2f is %.1f mm long" % (ymin, span))
        if p(ov, "stand") == "foot":
            fb = [v for t in parts["backing"] for v in t]
            fz = max(v[2] for v in fb if v[1] < ymin - 1)
            check(near(fz, FOOT_H), "foot sits below the sign, %.1f mm tall" % fz)


def pocket_volume(ov):
    r = (p(ov, "magnet_d") + MAGNET_CLEAR_D) / 2
    depth = min(p(ov, "magnet_h") + MAGNET_CLEAR_H, p(ov, "backing_thickness") - 1)
    return 2 * 0.5 * FN * r * r * math.sin(2 * math.pi / FN) * depth, depth


print("\n[magnet pockets]")
for with_m, without in [("rotated-extras-magnets", "rotated-extras"),
                        ("arch-extras-foot-magnets", "arch-extras-foot")]:
    removed = volume(closed[without]["backing"]) - volume(closed[with_m]["backing"])
    exp, depth = pocket_volume(dict(CASES[[c[0] for c in CASES].index(with_m)][1]))
    check(abs(removed - exp) <= 0.01 * exp,
          "%s: two %.1f mm deep pockets remove %.1f mm^3 (expected %.1f)"
          % (with_m, depth, removed, exp))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
