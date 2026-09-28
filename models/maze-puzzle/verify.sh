#!/usr/bin/env bash
# Render models/maze-puzzle with the defaults and each major variation, then
# check concrete facts:
#   - the maze the model echoes is perfect: every cell reachable from the
#     start, openings == cells - 1 (so exactly one route between any two
#     cells), no opening into a missing cell, start and finish distinct;
#   - different seeds give different mazes, the same seed the same maze;
#   - the colour parts, that no geometry is uncoloured, the bounding box the
#     parameters imply, and that everything sits on z=0;
#   - from one closed render per colour (the way ScadBuddy builds its parts):
#     the wall volume equals the outline minus exactly the corridors and
#     openings the echoed maze describes, each part's z range, and that the
#     parts do not overlap (their volumes add up to the volume of the whole);
#   - ScadBuddy's plate convention (spec §6.4): the model echoes `plates = N`,
#     2 exactly when the tray and lid cannot share the bed; then each plate,
#     rendered with -D '$plate=k' as ScadBuddy does (and per colour through the
#     wrapper), holds its own parts, fits the bed on its own, and the plates'
#     per-colour volumes add up to the everything-at-once render's.
#
# The checking runs on the host with python3 and the standard library only:
# the OpenSCAD image has no Python.
set -euo pipefail

cd "$(dirname "$0")"

BASE_IMAGE="${SCADBUDDY_OPENSCAD_IMAGE:-openscad/openscad:dev}"
FONTS_IMAGE="${SCADBUDDY_FONTS_IMAGE:-scadbuddy-verify:local}"
OUT="${OUT_DIR:-.verify}"

mkdir -p "$OUT"

# Render in the ScadBuddy fonts image (this model uses no text, but renders in
# the same image as every other template). Build it when missing.
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
echo "==> rendering with $IMAGE"

IMAGE="$IMAGE" OUT="$OUT" python3 - <<'PY'
import json, math, os, re, shlex, struct, subprocess, sys, zipfile
import xml.etree.ElementTree as ET
from collections import Counter, deque

IMAGE, OUT = os.environ["IMAGE"], os.environ["OUT"]
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
TOL = 0.01

# Defaults and hidden constants, mirrored from model.scad.
D = dict(cells_x=8, cells_y=8, seed=42, cell_size=10, shape="square",
         wall_height=6, wall_thickness=1.6, mode="open_tray", ball_d=6,
         markers=True)
FLOOR_T, BORDER_EXTRA, BALL_CLEAR, LID_HEAD, INLAY = 2, 1.2, 1.0, 0.5, 0.6
LID_T, SKIRT_T, LID_FIT, PART_GAP = 1.6, 1.6, 0.25, 8
FLOOR, WALL, MARKER, LID = "#80DEEA", "#006064", "#FFCA28", "#FFFFFF"

CASES = [
    ("defaults", {}),
    ("seed-43", dict(seed=43)),
    ("seed-7", dict(seed=7)),
    ("defaults-again", {}),
    ("round", dict(shape="round")),
    ("lid-square", dict(mode="ball_lid")),
    ("round-lid-15x15", dict(shape="round", mode="ball_lid", cells_x=15, cells_y=15)),
    ("big-15x15", dict(cells_x=15, cells_y=15, seed=9999)),
    ("small-4x4", dict(cells_x=4, cells_y=4, seed=0)),
    ("round-4x4", dict(cells_x=4, cells_y=4, shape="round")),
    ("round-15x4", dict(cells_x=15, cells_y=4, shape="round", seed=123)),
    ("wide-12x5", dict(cells_x=12, cells_y=5, wall_height=12, seed=5)),
    ("big-ball-widen", dict(ball_d=12, cell_size=6, mode="ball_lid", wall_height=3)),
    ("thick-walls-no-markers", dict(wall_thickness=3, cell_size=16, markers=False)),
    # Tray + lid side by side would pass the 300 mm width: the lid goes behind.
    ("lid-behind-15x6", dict(mode="ball_lid", cells_x=15, cells_y=6)),
    ("round-lid", dict(shape="round", mode="ball_lid", seed=11)),
    # Neither way fits: the lid goes on plate 2, with a note.
    ("lid-too-big-15x15", dict(mode="ball_lid", cells_x=15, cells_y=15, cell_size=16)),
    ("lid-plate-2-15x13", dict(mode="ball_lid", cells_x=15, cells_y=13, cell_size=14, seed=3)),
]
BED = (300, 320)


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


# ---- phase 1: combined renders, keeping each case's echo
logs = {}
for name, ov in CASES:
    logs[name] = docker("openscad --backend=Manifold %s -o %s/%s.3mf model.scad"
                        % (defines(ov), OUT, name))
combined = {name: read_3mf("%s/%s.3mf" % (OUT, name)) for name, _ in CASES}


def echoed_plates(log):
    counts = [int(m.group(1)) for m in re.finditer(r"^ECHO: plates = (\d+)$", log, re.M)]
    return counts[-1] if counts else None


# ---- phase 1b: one render per plate, as ScadBuddy renders a multi-plate template
plates = {name: echoed_plates(logs[name]) or 1 for name, _ in CASES}
plate_jobs = [(name, k, dict(ov, **{"$plate": k})) for name, ov in CASES
              for k in range(1, plates[name] + 1) if plates[name] > 1]
if plate_jobs:
    docker("\n".join("openscad --backend=Manifold %s -o %s/%s_p%d.3mf model.scad"
                      % (defines(pov), OUT, name, k) for name, k, pov in plate_jobs))
per_plate = {(name, k): read_3mf("%s/%s_p%d.3mf" % (OUT, name, k)) for name, k, _ in plate_jobs}

# ---- phase 2: one closed render per colour, plus one of everything
with open("%s/wrap.scad" % OUT, "w") as f:
    f.write('_sb_t = "";\nmodule color(c, alpha = 1) { if (_sb_t == "*" || c == _sb_t) children(); }\n'
            "include <../model.scad>\n")
jobs = []
for name, ov in CASES:
    mats, _, tris = combined[name]
    used = Counter(t[3] for t in tris)
    targets = [col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)]
    for col in targets + ["*"]:
        tag = "all" if col == "*" else col[1:]
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_%s.stl %s/wrap.scad"
                    % (defines(ov), col, OUT, name, tag, OUT))
for name, k, pov in plate_jobs:
    mats, _, tris = per_plate[(name, k)]
    used = Counter(t[3] for t in tris)
    for col in [col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)]:
        jobs.append("openscad --backend=Manifold %s -D '_sb_t=\"%s\"' -o %s/%s_p%d_%s.stl %s/wrap.scad"
                    % (defines(pov), col, OUT, name, k, col[1:], OUT))
docker("\n".join(jobs))

# ---- checks
failures = []


def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def parse_maze(log):
    for line in log.splitlines():
        if line.startswith('ECHO: "MAZE"'):
            return json.loads("[" + line[len("ECHO: "):] + "]")[1:]
    return None


def dims(ov):
    q = lambda k: ov.get(k, D[k])
    W, H, t, bd = q("cells_x"), q("cells_y"), q("wall_thickness"), q("ball_d")
    p = max(q("cell_size"), bd + BALL_CLEAR + t)
    wh = max(q("wall_height"), bd + LID_HEAD) if q("mode") == "ball_lid" else q("wall_height")
    B = t / 2 + BORDER_EXTRA
    R_out = min(W, H) * p / 2 + p / 4 + B
    return dict(W=W, H=H, t=t, p=p, c=p - t, wh=wh, B=B, rc=B + t / 2, R_out=R_out,
                skirt_h=min(4, wh - 0.4), round=q("shape") == "round",
                lid=q("mode") == "ball_lid", markers=q("markers"))


def outline_area(d, grow=0):
    if d["round"]:
        n, r = 96, d["R_out"] + grow
        return 0.5 * n * r * r * math.sin(2 * math.pi / n)
    a, b, r = d["W"] * d["p"] - d["t"], d["H"] * d["p"] - d["t"], d["rc"] + grow
    return a * b + 2 * r * (a + b) + 0.5 * 64 * r * r * math.sin(2 * math.pi / 64)


def outline_box(d):
    if d["round"]:
        cx, cy, r = d["W"] * d["p"] / 2, d["H"] * d["p"] / 2, d["R_out"]
        return (cx - r, cy - r), (cx + r, cy + r)
    return (-d["B"], -d["B"]), (d["W"] * d["p"] + d["B"], d["H"] * d["p"] + d["B"])


mazes = {}
for name, ov in CASES:
    print("\n[%s] %s" % (name, " ".join("%s=%s" % kv for kv in ov.items()) or "defaults"))
    d = dims(ov)
    W, H = d["W"], d["H"]

    # -- the maze is perfect
    mz = parse_maze(logs[name])
    check(mz is not None, "model echoes its maze")
    if mz is None:
        continue
    mW, mH, A, E, N, start, finish = mz
    mazes[name] = (A, E, N)
    cells = [k for k in range(W * H) if A[k]]
    edges = []
    bad = 0
    for k in range(W * H):
        x, y = k % W, k // W
        if E[k]:
            ok = x + 1 < W and A[k] and A[k + 1]
            bad += not ok
            edges.append((k, k + 1))
        if N[k]:
            ok = y + 1 < H and A[k] and A[k + W]
            bad += not ok
            edges.append((k, k + W))
    adj = {k: [] for k in cells}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].append(b)
            adj[b].append(a)
    seen, q = {start}, deque([start])
    while q:
        for n in adj[q.popleft()]:
            if n not in seen:
                seen.add(n)
                q.append(n)
    check((mW, mH) == (W, H), "maze is %d x %d" % (W, H))
    check(bad == 0, "no opening leads out of the grid or into a missing cell (%d bad)" % bad)
    check(len(cells) >= 4, "%d cells in the maze" % len(cells))
    check(len(seen) == len(cells), "every cell reachable from the start (%d of %d)"
          % (len(seen), len(cells)))
    check(len(edges) == len(cells) - 1,
          "openings == cells - 1 (%d == %d): a tree, one route between start and finish"
          % (len(edges), len(cells) - 1))
    check(start != finish and A[start] and A[finish], "start %d and finish %d are distinct cells"
          % (start, finish))
    if not d["round"]:
        check(len(cells) == W * H and start == 0 and finish == W * H - 1,
              "square: all cells, start front-left, finish back-right")

    # -- colours and bbox
    mats, verts, tris = combined[name]
    used = Counter(t[3] for t in tris)
    named = {col for i, (n, col) in enumerate(mats) if n != "Default" and used.get(i)}
    # Lid placement, as in model.scad: right of the tray, else behind it,
    # else on plate 2 (and, drawn with everything, to the right, off the bed).
    (ox0, oy0), (ox1, oy1) = outline_box(d)
    g = LID_FIT + SKIRT_T
    span = (ox1 - ox0, oy1 - oy0)
    pair = [2 * span[i] + PART_GAP + 2 * g for i in (0, 1)]
    lid_at = ((span[0] + g + PART_GAP, 0) if pair[0] <= BED[0] and span[1] + 2 * g <= BED[1]
              else (0, span[1] + g + PART_GAP) if pair[1] <= BED[1] and span[0] + 2 * g <= BED[0]
              else None)
    show_tray, show_lid = True, d["lid"]
    two = d["lid"] and lid_at is None
    check(plates[name] == (2 if two else 1), "echoes plates = %d (got %s)"
          % (2 if two else 1, echoed_plates(logs[name])))
    if two:
        check("the lid is on plate 2" in logs[name], "tray and lid do not fit together: lid on plate 2, with a note")
    tray_cols = {FLOOR, WALL} | ({MARKER} if d["markers"] else set())
    want = tray_cols | ({LID} if show_lid else set())
    check(named == want, "parts are %s (got %s)" % (sorted(want), sorted(named)))
    check(used.get(0, 0) == 0, "Default material has no triangles (got %d)" % used.get(0, 0))

    z_top = FLOOR_T + d["wh"]
    lid_h = LID_T + d["skirt_h"]
    boxes = []
    if show_tray:
        boxes.append(((ox0, oy0, 0.0), (ox1, oy1, z_top)))
    if show_lid:
        lx, ly = lid_at if lid_at is not None else (span[0] + g + PART_GAP, 0)
        boxes.append(((ox0 - g + lx, oy0 - g + ly, 0.0), (ox1 + g + lx, oy1 + g + ly, lid_h)))
    elo = [min(b[0][i] for b in boxes) for i in range(3)]
    ehi = [max(b[1][i] for b in boxes) for i in range(3)]
    lo, hi = bbox(verts)
    check(all(near(a, b) for a, b in zip(lo + hi, tuple(elo) + tuple(ehi))),
          "bbox %s .. %s == %s .. %s"
          % (tuple(round(x, 2) for x in lo), tuple(round(x, 2) for x in hi),
             tuple(round(x, 2) for x in elo), tuple(round(x, 2) for x in ehi)))
    check(near(lo[2], 0), "sits on z=0 (min z %.3f)" % lo[2])
    if not two:
        check(hi[0] - lo[0] <= BED[0] and hi[1] - lo[1] <= BED[1],
              "fits the H2C bed, %d x %d with both nozzles (%.1f x %.1f)"
              % (BED[0], BED[1], hi[0] - lo[0], hi[1] - lo[1]))

    # -- plates: each rendered on its own, as ScadBuddy renders them
    if two:
        tray_box, lid_box = boxes[0], ((ox0 - g, oy0 - g, 0.0), (ox1 + g, oy1 + g, lid_h))
        for k, cols, box in ((1, tray_cols, tray_box), (2, {LID}, lid_box)):
            pm, pv, pt = per_plate[(name, k)]
            pused = Counter(t[3] for t in pt)
            pnamed = {col for i, (n, col) in enumerate(pm) if n != "Default" and pused.get(i)}
            check(pnamed == cols, "plate %d holds %s (got %s)" % (k, sorted(cols), sorted(pnamed)))
            plo, phi = bbox(pv)
            check(all(near(a, b) for a, b in zip(plo + phi, box[0] + box[1])),
                  "plate %d bbox %s .. %s == %s .. %s"
                  % (k, tuple(round(x, 2) for x in plo), tuple(round(x, 2) for x in phi),
                     tuple(round(x, 2) for x in box[0]), tuple(round(x, 2) for x in box[1])))
            check(phi[0] - plo[0] <= BED[0] and phi[1] - plo[1] <= BED[1],
                  "plate %d fits the H2C bed on its own (%.1f x %.1f)"
                  % (k, phi[0] - plo[0], phi[1] - plo[1]))

    # -- closed parts
    parts = {col: read_stl("%s/%s_%s.stl" % (OUT, name, col[1:])) for col in named}
    whole = read_stl("%s/%s_all.stl" % (OUT, name))
    vols = {col: volume(t) for col, t in parts.items()}
    vw = volume(whole)
    check(abs(sum(vols.values()) - vw) <= 0.001 * vw,
          "parts do not overlap: sum of part volumes %.1f == whole %.1f mm^3"
          % (sum(vols.values()), vw))

    if two:
        for col in named:
            vp = sum(volume(read_stl("%s/%s_p%d_%s.stl" % (OUT, name, k, col[1:])))
                     for k in (1, 2) if (k == 2) == (col == LID))
            check(abs(vp - vols[col]) <= 0.001 * vols[col],
                  "%s: the plate's closed part %.1f mm^3 == the everything render's %.1f"
                  % (col, vp, vols[col]))

    zr = {col: (bbox([v for t in s for v in t])[0][2], bbox([v for t in s for v in t])[1][2])
          for col, s in parts.items()}
    if show_lid:
        check(near(zr[LID][0], 0) and near(zr[LID][1], LID_T + d["skirt_h"]),
              "lid prints upside down, z 0 .. %.2f (got %.3f .. %.3f)"
              % (LID_T + d["skirt_h"], *zr[LID]))
    if show_tray:
        check(near(zr[FLOOR][0], 0) and near(zr[FLOOR][1], FLOOR_T),
              "floor z 0 .. %.1f (got %.3f .. %.3f)" % (FLOOR_T, *zr[FLOOR]))
        check(near(zr[WALL][0], FLOOR_T) and near(zr[WALL][1], z_top),
              "walls z %.1f .. %.2f (got %.3f .. %.3f)" % (FLOOR_T, z_top, *zr[WALL]))
        if d["markers"]:
            check(near(zr[MARKER][0], FLOOR_T - INLAY) and near(zr[MARKER][1], FLOOR_T),
                  "markers inlaid z %.1f .. %.1f (got %.3f .. %.3f)"
                  % (FLOOR_T - INLAY, FLOOR_T, *zr[MARKER]))

        # Wall volume from the echoed maze: outline minus cells minus openings.
        corridors = len(cells) * d["c"] ** 2 + len(edges) * d["c"] * d["t"]
        exp_wall = (outline_area(d) - corridors) * d["wh"]
        if d["lid"]:
            check(vols[WALL] < exp_wall and exp_wall - vols[WALL] < 0.02 * exp_wall,
                  "walls %.1f mm^3 = maze walls %.1f less the snap groove" % (vols[WALL], exp_wall))
        else:
            check(abs(vols[WALL] - exp_wall) <= 0.001 * exp_wall,
                  "walls %.1f mm^3 == outline - corridors of the echoed maze %.1f"
                  % (vols[WALL], exp_wall))
        exp_floor = outline_area(d) * FLOOR_T - vols.get(MARKER, 0)
        check(abs(vols[FLOOR] - exp_floor) <= 0.001 * exp_floor,
              "floor %.1f mm^3 == outline x %.0f mm less the marker inlays (%.1f)"
              % (vols[FLOOR], FLOOR_T, exp_floor))

    if name == "big-ball-widen":
        check(near(d["p"], 12 + BALL_CLEAR + d["t"]) and "cell_size widened" in logs[name]
              and "wall_height raised" in logs[name],
              "12 mm ball: pitch widened to %.1f and walls raised to %.1f, with notes"
              % (d["p"], d["wh"]))

print("\n[seeds]")
check(mazes["defaults"] == mazes["defaults-again"], "same seed, same maze")
for a, b in [("defaults", "seed-43"), ("defaults", "seed-7"), ("seed-43", "seed-7")]:
    check(mazes[a] != mazes[b], "%s and %s give different mazes" % (a, b))

if failures:
    print("\nFAILED: %d check(s)" % len(failures))
    sys.exit(1)
print("\nOK: %d cases" % len(CASES))
PY
