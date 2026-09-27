#!/usr/bin/env bash
# Render models/name-puzzle with the defaults and each major variation, then
# check concrete facts: the number of colour parts, that no geometry is
# uncoloured, the bounding box the parameters imply (from the glyph table in
# model.scad), that everything sits on z=0, the z range of every closed
# per-colour part (rendered the way ScadBuddy builds its parts), and — from
# volumes — that the letters drop into their pockets without touching the tray
# and that no two pieces overlap on the plate.
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
# falls back silently and the glyph table in model.scad would not match.
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
FAMILIES="$(docker run --rm "$IMAGE" fc-list : family)"
for fam in "DejaVu Sans" "Lobster Two"; do
    if ! grep -qF "$fam" <<< "$FAMILIES"; then
        echo "FAIL: $IMAGE has no '$fam'" >&2
        exit 1
    fi
done
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import os, re, shlex, struct, subprocess, sys, time, zipfile
import xml.etree.ElementTree as ET
from collections import Counter

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.02

SRC = open("model.scad").read()


def table(name):
    body = re.search(name + r" = \[(.*?)\n\];", SRC, re.S).group(1)
    return {m.group(1): tuple(map(float, m.group(2).split(",")))
            for m in re.finditer(r'\["(.)", ([^\]]*)\]', body)}


TABLES = {"DejaVu Sans:style=Bold": table("DEJ_T"), "Lobster Two:style=Bold": table("LOB_T")}
GENERIC = (-0.45, 0.45, -0.55, 0.55, 0, 0, 0.1)

D = dict(name="MIA", font="DejaVu Sans:style=Bold", letter_size=40, tray_thickness=4,
         pocket_depth=4, letter_thickness=7, clearance=0.4, knobs=False,
         tray_shape="rounded_rect", tray_margin=8)
TRAY = "#FFE08A"
LCOL = ["#E53935", "#1E88E5", "#43A047", "#8E24AA"]
WALL, SPACE_K, KNOB_H, PART_GAP, COUPLER = 4, 0.35, 6, 8, 8

# name, overrides
CASES = [
    ("defaults", {}),
    ("cloud-lobster", dict(tray_shape="cloud", name="Oscar", font="Lobster Two:style=Bold")),
    ("train-knobs", dict(tray_shape="train", knobs=True)),
    ("two-words", dict(name="ANNA LEE", letter_size=30)),
    ("big-clamped", dict(name="W", letter_size=70, tray_thickness=8, pocket_depth=8,
                         letter_thickness=4, clearance=0.8, tray_margin=20)),
    ("small-generic", dict(name="ZOË", letter_size=25, tray_margin=4, clearance=0.2,
                           tray_shape="cloud")),
    ("other-font", dict(name="MAX", font="Noto Serif:style=Bold", knobs=True)),
    ("train-ten", dict(name="BARTHOLOME", letter_size=25, tray_shape="train", tray_margin=4)),
    ("rect-shrunk", dict(name="ALEXANDRA", letter_size=30)),
    ("cloud-shrunk", dict(name="MAXIMILIAN", letter_size=70, tray_shape="cloud")),
    ("empty", dict(name="")),
    ("spaces", dict(name="   ", tray_shape="train")),
]
FIT_CASES = ["defaults", "cloud-lobster", "train-knobs", "big-clamped", "small-generic",
             "other-font", "train-ten", "rect-shrunk", "cloud-shrunk"]


def scad(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return repr(v)


def defines(ov):
    return " ".join("-D %s" % shlex.quote("%s=%s" % (k, scad(v))) for k, v in ov.items())


def docker(script):
    r = subprocess.run(["docker", "run", "--rm", "-v", os.getcwd() + ":/w", "-w", "/w",
                        IMAGE, "bash", "-ec", script], capture_output=True, text=True)
    if r.returncode or "WARNING" in r.stderr or "ERROR" in r.stderr:
        print(r.stderr)
        sys.exit("FAIL: OpenSCAD reported errors or warnings")
    return r.stderr


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
    out = []
    for i in range(n):
        f = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
        out.append([f[3:6], f[6:9], f[9:12]])
    return out


def bbox(points):
    xs, ys, zs = zip(*points)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


# ---- geometry the parameters imply (mirrors model.scad)
BED_X = 300


def plate_len(p, met, letters, sz):
    """Plate x extent at letter size sz, independent of model.scad's formula."""
    m, cl, n = p["tray_margin"], p["clearance"], len(met)
    train = p["tray_shape"] == "train"
    if n:
        span = (sum((t[1] - t[0]) * sz + 2 * cl for t in met)
                + (n - 1) * (2 * m + COUPLER if train else WALL)
                + (0 if train else SPACE_K * sz * sum(1 for _, gap in letters[1:] if gap)))
    else:
        span = 0.6 * sz
    H = (max(t[3] - t[2] for t in met) if n else 1) * sz + 2 * cl + 2 * m
    lr = span + 2 * m
    if p["tray_shape"] == "cloud":
        return lr + 0.34 * H
    if train:
        return lr + COUPLER + 1.02 * max(0.9 * H, 30)
    return lr


def layout(ov):
    p = dict(D, **ov)
    m, cl = p["tray_margin"], p["clearance"]
    tbl = TABLES.get(p["font"], TABLES["DejaVu Sans:style=Bold"])
    name = p["name"]
    letters = []
    for i, c in enumerate(name):
        if c != " ":
            letters.append((c, i > 0 and name[i - 1] == " " and name[:i].strip() != ""))
    met = [tbl.get(c, GENERIC) for c, _ in letters]
    s = p["letter_size"]
    if plate_len(p, met, letters, s) > BED_X:
        lo, hi = 0.0, s
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if plate_len(p, met, letters, mid) <= BED_X else (lo, mid)
        s = lo
    pw = [(t[1] - t[0]) * s + 2 * cl for t in met]
    gh = [(t[3] - t[2]) * s for t in met]
    n = len(letters)
    train = p["tray_shape"] == "train"
    cell_h = (max(gh) if n else s) + 2 * cl
    H = cell_h + 2 * m
    step = [pw[k] + (2 * m + COUPLER if train else WALL)
            + (SPACE_K * s if not train and k + 1 < n and letters[k + 1][1] else 0)
            for k in range(n)]
    span = sum(step) - (2 * m + COUPLER if train else WALL) if n else s * 0.6
    x0 = -span / 2
    cx = [x0 + sum(step[:k]) + pw[k] / 2 for k in range(n)]
    pd = min(p["pocket_depth"], p["letter_thickness"] - 1)
    return dict(p=p, s=s, m=m, n=n, pw=pw, gh=gh, H=H, span=span, cx=cx, x0=x0,
                cell_h=cell_h, pd=pd, tray_h=p["tray_thickness"] + pd, train=train)


def expected(L):
    """(lo, hi, exact_x) of the tray outline, and the letters' row y centre."""
    p, H, m, span = L["p"], L["H"], L["m"], L["span"]
    Lr = span + 2 * m
    shape = p["tray_shape"]
    if shape == "cloud":
        lo = [-(Lr / 2 + 0.17 * H), -(H / 2 + 0.65 * 0.3 * H)]
        hi = [-lo[0], -lo[1]]
        return lo, hi, True
    if shape == "train":
        wr = max(5, min(12, 0.16 * H))
        Le = max(0.9 * H, 30)
        eng_x1 = L["x0"] - m - COUPLER
        right = (L["cx"][-1] + L["pw"][-1] / 2 + m) if L["n"] else eng_x1
        return [eng_x1 - Le, -(H / 2 + wr)], [right, H / 2], False
    return [-Lr / 2, -H / 2], [Lr / 2, H / 2], True


def colour_of(ov, k):
    return ov.get("letter_color_%d" % (k % 4 + 1), LCOL[k % 4])


# ---- phase 1: combined renders, plus plain single-mesh STLs for volume checks
jobs, times = [], {}
for name, ov in CASES:
    jobs.append("openscad --backend=Manifold %s -o %s/%s.3mf model.scad" % (defines(ov), OUT, name))
    jobs.append("openscad --backend=Manifold %s -o %s/%s_plate.stl model.scad"
                % (defines(ov), OUT, name))
for name, ov in CASES:
    if name in FIT_CASES:
        jobs.append("openscad --backend=Manifold %s -D assembled=true -o %s/%s_asm.stl model.scad"
                    % (defines(ov), OUT, name))
t0 = time.time()
docker("\n".join(jobs[:1]))
print("defaults render (incl. container start): %.1fs" % (time.time() - t0))
docker("\n".join(jobs[1:]))

combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}

# ---- phase 2: one closed render per colour (ScadBuddy's colour wrapper)
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    mats, _, tris = combined[name]
    used = Counter(t[3] for t in tris)
    for i, (n, col) in enumerate(mats):
        if n != "Default" and used.get(i):
            jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                        % (defines(ov), col, OUT, name, col[1:], OUT))
docker("\n".join(jobs))

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def r2(v):
    return tuple(round(x, 2) for x in v)


for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    L = layout(ov)
    p, n = L["p"], L["n"]
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (nm, col) in enumerate(mats) if nm != "Default" and used.get(i)}
    want = {TRAY} | {colour_of(ov, k) for k in range(n)}
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    lo, hi = bbox(verts)
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])
    check(hi[0] - lo[0] <= BED_X + 0.05 and hi[1] - lo[1] <= 320,
          "plate %.1f x %.1f mm fits 300 x 320 (letter size %.2f of %s requested)"
          % (hi[0] - lo[0], hi[1] - lo[1], L["s"], p["letter_size"]))

    # Tray: closed part, outline and height.
    tray = read_stl("%s/%s_%s.stl" % (OUT, name, TRAY[1:]))
    tlo, thi = bbox([v for t in tray for v in t])
    elo, ehi, exact = expected(L)
    check(near(tlo[2], 0) and near(thi[2], L["tray_h"]),
          "tray z %.3f .. %.3f == 0 .. %.2f (floor %.1f + pocket %.1f)"
          % (tlo[2], thi[2], L["tray_h"], p["tray_thickness"], L["pd"]))
    if exact:
        check(all(near(a, b, 0.05) for a, b in zip(tlo[:2] + thi[:2], elo + ehi)),
              "tray outline %s .. %s == %s .. %s"
              % (r2(tlo[:2]), r2(thi[:2]), r2(elo), r2(ehi)))
    else:
        Le = max(0.9 * L["H"], 30)
        check(near(tlo[1], elo[1], 0.05) and near(thi[1], ehi[1], 0.05)
              and near(thi[0], ehi[0], 0.05)
              and elo[0] - 0.05 * Le <= tlo[0] <= elo[0] + 0.1 * Le,
              "train outline %s .. %s ~ %s .. %s (locomotive nose within 0.1 x its length)"
              % (r2(tlo[:2]), r2(thi[:2]), r2(elo), r2(ehi)))

    # Letters: one row in front of the tray, the right height, the right x span.
    if n:
        top = p["letter_thickness"] + (KNOB_H if p["knobs"] else 0)
        llo, lhi = [9e9] * 3, [-9e9] * 3
        for col in {colour_of(ov, k) for k in range(n)}:
            stl = read_stl("%s/%s_%s.stl" % (OUT, name, col[1:]))
            a, b = bbox([v for t in stl for v in t])
            llo = [min(x, y) for x, y in zip(llo, a)]
            lhi = [max(x, y) for x, y in zip(lhi, b)]
            ztop = p["letter_thickness"] + (KNOB_H if p["knobs"] else 0)
            check(near(a[2], 0) and (near(b[2], ztop) or (p["knobs"] and near(b[2], p["letter_thickness"]))),
                  "letter part %s z %.3f .. %.3f (expected 0 .. %.2f)" % (col, a[2], b[2], ztop))
        check(near(lhi[2], top), "tallest letter reaches %.2f (got %.3f)" % (top, lhi[2]))
        row_top = elo[1] - PART_GAP - L["cell_h"] / 2 + max(L["gh"]) / 2
        if p["font"] not in TABLES:     # other faces are shrunk into generic cells
            row_top = min(row_top, lhi[1])
        check(lhi[1] < tlo[1] and near(lhi[1], row_top, 0.3),
              "letters lie in front of the tray, clear of it: row top %.2f ~ %.2f < tray %.2f"
              % (lhi[1], row_top, tlo[1]))
        known = p["font"] in TABLES
        if known and all(c in TABLES[p["font"]] for c in p["name"].replace(" ", "")):
            xl = L["cx"][0] - L["pw"][0] / 2 + p["clearance"]
            xr = L["cx"][-1] + L["pw"][-1] / 2 - p["clearance"]
            # Rounding the glyph corners (letter_round = 0.8) may pull an acute
            # corner in; nothing may stick out past the measured glyph.
            check(xl - 0.05 <= llo[0] <= xl + 0.8 and xr - 0.8 <= lhi[0] <= xr + 0.05,
                  "letter row spans x %.2f .. %.2f (glyph table %.2f .. %.2f, corners -0.8)"
                  % (llo[0], lhi[0], xl, xr))
        pd, lt = L["pd"], p["letter_thickness"]
        check(lt - pd >= 1 - 1e-9, "letters stand %.1f mm proud of the pockets" % (lt - pd))

    # Volumes: pieces are disjoint on the plate, and in their pockets.
    parts_v = sum(volume(read_stl("%s/%s_%s.stl" % (OUT, name, c[1:]))) for c in named)
    plate_v = volume(read_stl("%s/%s_plate.stl" % (OUT, name)))
    check(abs(plate_v - parts_v) <= 1e-4 * parts_v,
          "no two pieces overlap on the plate: union %.1f == sum of parts %.1f mm^3"
          % (plate_v, parts_v))
    if name in FIT_CASES:
        asm_v = volume(read_stl("%s/%s_asm.stl" % (OUT, name)))
        check(abs(asm_v - parts_v) <= 1e-4 * parts_v,
              "letters drop into their pockets without touching the tray: assembled %.1f == %.1f mm^3"
              % (asm_v, parts_v))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
