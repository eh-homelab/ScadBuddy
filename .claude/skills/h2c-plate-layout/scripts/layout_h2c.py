#!/usr/bin/env python3
"""Measure or re-lay a single-plate 3MF for the Bambu H2C.

  layout_h2c.py report in.3mf [--no-sparse]
  layout_h2c.py move in.3mf out.3mf --center CX,CY --tower TX,TY [--no-sparse]

report: the declared plate, each build item's XY footprint (vertices x item matrix,
components followed), the group box, the tower box, and the checks.
move: shift every build item by one delta so the group's XY centre lands on CX,CY,
set the prime tower corner to TX,TY, mark the file as an H2C author layout
(process/printer preset names, 330x320 area, 325 height), then report.
Run with `python3 -I` on downloaded files.
"""
import json
import re
import sys
import zipfile

BED = (330.0, 320.0)
USABLE_X = (25.0, 325.0)        # both nozzles reach it
TOWER_SIDE, BRIM, CLEAR, EDGE = 60.0, 3.0, 5.0, 2.0
WRAP_X, WRAP_Y = (145.0, 251.0), (310.0, 326.0)
NO_SPARSE_RADIUS, NOZZLE_HEIGHT = 96.0, 4.0
H2C_PROCESS, H2C_PRINTER = "0.20mm Standard @BBL H2C", "Bambu Lab H2C 0.4 nozzle"

Mat = tuple  # 12 floats: 3x3 row-major in the 3MF's column order + translation


def parse_matrix(s: str) -> list[float]:
    p = [float(v) for v in s.split()]
    return p if len(p) == 12 else [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def apply(m: list[float], x: float, y: float, z: float) -> tuple[float, float, float]:
    # 3MF: (x y z) * M where M rows are m[0:3], m[3:6], m[6:9] and translation m[9:12]
    return (x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


def compose(a: list[float], b: list[float]) -> list[float]:
    """(point * a) * b as one matrix."""
    r = [0.0] * 12
    for i in range(3):
        for j in range(3):
            r[i * 3 + j] = sum(a[i * 3 + k] * b[k * 3 + j] for k in range(3))
    for j in range(3):
        r[9 + j] = sum(a[9 + k] * b[k * 3 + j] for k in range(3)) + b[9 + j]
    return r


def attrs(tag: str) -> dict:
    return dict(re.findall(r'([\w:]+)="([^"]*)"', tag))


def load(path: str):
    """Objects keyed by (model file, id): Bambu-style 3MFs keep each mesh in
    3D/Objects/object_N.model and reference it from 3D/3dmodel.model by p:path."""
    z = zipfile.ZipFile(path)
    model = z.read("3D/3dmodel.model").decode()
    settings = json.loads(z.read("Metadata/project_settings.config"))
    objects: dict[tuple, dict] = {}
    for name in z.namelist():
        if not name.endswith(".model"):
            continue
        text = model if name == "3D/3dmodel.model" else z.read(name).decode()
        for m in re.finditer(r'<object\b([^>]*)>(.*?)</object>', text, re.S):
            oid, body = attrs(m.group(1)).get("id"), m.group(2)
            verts = [(float(a), float(b), float(c)) for a, b, c in re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', body)]
            comps = []
            for c in re.finditer(r'<component\b([^>]*)/?>', body):
                ca = attrs(c.group(1))
                cpath = ca.get("p:path", "").lstrip("/") or name
                comps.append(((cpath, ca.get("objectid")), parse_matrix(ca.get("transform", ""))))
            objects[(name, oid)] = {"verts": verts, "comps": comps}
    items = []
    for m in re.finditer(r'<item\b([^>]*)/?>', model):
        ia = attrs(m.group(1))
        items.append((m.group(0), ("3D/3dmodel.model", ia.get("objectid")), parse_matrix(ia.get("transform", ""))))
    return z, model, settings, objects, items


def bbox_of(objects, oid: tuple, m: list[float], acc: list):
    o = objects.get(oid)
    if not o:
        return
    for x, y, z in o["verts"]:
        acc.append(apply(m, x, y, z))
    for cid, cm in o["comps"]:
        bbox_of(objects, cid, compose(cm, m), acc)


def box(pts):
    xs, ys, zs = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def tower_box(settings):
    try:
        tx, ty = float(settings["wipe_tower_x"][0]), float(settings["wipe_tower_y"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return (tx, ty, tx + TOWER_SIDE, ty + TOWER_SIDE)


def overlaps(a, b, pad=0.0):
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def gap(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def report(settings, objects, items, no_sparse: bool) -> bool:
    ok = True
    print("plate:", settings.get("printable_area"), "process:", settings.get("print_settings_id"), "printer:", settings.get("printer_settings_id"))
    allpts = []
    for n, (_, oid, m) in enumerate(items, 1):
        pts: list = []
        bbox_of(objects, oid, m, pts)
        if not pts:
            print(f"item {n}: object {oid[1]} has no vertices"); continue
        b = box(pts); allpts += pts
        print(f"item {n}: object {oid[1]} x {b[0]:.1f}..{b[3]:.1f} y {b[1]:.1f}..{b[4]:.1f} z {b[2]:.1f}..{b[5]:.1f}")
    if not allpts:
        print("no geometry"); return False
    g = box(allpts)
    print(f"group: x {g[0]:.1f}..{g[3]:.1f} y {g[1]:.1f}..{g[4]:.1f} height {g[5] - g[2]:.1f}  centre ({(g[0] + g[3]) / 2:.1f}, {(g[1] + g[4]) / 2:.1f})")
    if g[0] < USABLE_X[0] or g[3] > USABLE_X[1] or g[1] < 0 or g[4] > BED[1]:
        print(f"FAIL objects outside the dual-nozzle area x {USABLE_X[0]:.0f}..{USABLE_X[1]:.0f}, y 0..{BED[1]:.0f}"); ok = False
    else:
        print("ok objects inside the dual-nozzle area")
    t = tower_box(settings)
    if t is None:
        print("tower: none declared (a default-pipeline file; the slicer will place one)")
        return ok
    print(f"tower: x {t[0]:.0f}..{t[2]:.0f} y {t[1]:.0f}..{t[3]:.0f} (+{BRIM:.0f} brim)")
    if t[0] - BRIM < USABLE_X[0] + EDGE or t[2] + BRIM > USABLE_X[1] - EDGE or t[1] - BRIM < EDGE or t[3] + BRIM > BED[1] - EDGE:
        print("FAIL tower outside the dual-nozzle area (with brim and edge margin)"); ok = False
    if overlaps(t, (WRAP_X[0], WRAP_Y[0], WRAP_X[1], WRAP_Y[1])):
        print("FAIL tower inside the wrapping exclusion area"); ok = False
    gxy = (g[0], g[1], g[3], g[4])
    d = gap(t, gxy)
    if overlaps(t, gxy, BRIM + CLEAR):
        print(f"FAIL tower within {BRIM + CLEAR:.0f} mm of an object (gap {d:.1f} mm)"); ok = False
    else:
        print(f"ok tower clear of objects (gap {d:.1f} mm)")
    if no_sparse and g[5] - g[2] > NOZZLE_HEIGHT:
        if d < NO_SPARSE_RADIUS:
            print(f"FAIL no-sparse-layers rule: objects {g[5] - g[2]:.1f} mm tall need {NO_SPARSE_RADIUS:.0f} mm from the tower, have {d:.1f}"); ok = False
        else:
            print("ok no-sparse-layers rule satisfied")
    return ok


def move(path_in, path_out, cx, cy, tx, ty):
    z, model, settings, objects, items = load(path_in)
    allpts: list = []
    for _, oid, m in items:
        bbox_of(objects, oid, m, allpts)
    g = box(allpts)
    dx, dy = cx - (g[0] + g[3]) / 2, cy - (g[1] + g[4]) / 2
    new_model = model
    for tag, oid, m in items:
        m2 = list(m); m2[9] += dx; m2[10] += dy
        tr = " ".join("%.5f" % v for v in m2)
        if 'transform="' in tag:
            new_tag = re.sub(r'transform="[^"]+"', f'transform="{tr}"', tag)
        else:
            new_tag = tag.replace("<item ", f'<item transform="{tr}" ', 1)
        new_model = new_model.replace(tag, new_tag, 1)
    settings["wipe_tower_x"] = ["%g" % tx]; settings["wipe_tower_y"] = ["%g" % ty]
    settings["print_settings_id"] = H2C_PROCESS; settings["printer_settings_id"] = H2C_PRINTER
    settings["printable_area"] = ["0x0", f"{BED[0]:.0f}x0", f"{BED[0]:.0f}x{BED[1]:.0f}", f"0x{BED[1]:.0f}"]
    settings["printable_height"] = "325"
    with zipfile.ZipFile(path_out, "w", zipfile.ZIP_DEFLATED) as out:
        for n in z.namelist():
            if n == "3D/3dmodel.model":
                out.writestr(n, new_model)
            elif n == "Metadata/project_settings.config":
                out.writestr(n, json.dumps(settings, indent=4))
            else:
                out.writestr(n, z.read(n))
    print(f"wrote {path_out}: shifted {len(items)} items by ({dx:.1f}, {dy:.1f}), tower at ({tx:g}, {ty:g})")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return 2
    no_sparse = "--no-sparse" in a
    a = [v for v in a if v != "--no-sparse"]
    if a[0] == "report":
        _, _, s, o, i = load(a[1])
        return 0 if report(s, o, i, no_sparse) else 1
    if a[0] == "move":
        cx, cy = (float(v) for v in a[a.index("--center") + 1].split(","))
        tx, ty = (float(v) for v in a[a.index("--tower") + 1].split(","))
        move(a[1], a[2], cx, cy, tx, ty)
        _, _, s, o, i = load(a[2])
        return 0 if report(s, o, i, no_sparse) else 1
    print(__doc__); return 2


sys.exit(main())
