#!/usr/bin/env python3
"""Receipt lines -> Bambuddy spools, and 3DFP profiles -> per-nozzle spool presets.

  intake.py apply lines.json [--dry-run]
  intake.py link profiles/ [--printer H2C] [--map SPOOL=FP ...] [--dry-run]

lines.json is a list of receipt lines. Cost per spool is always
(line subtotal + that line's tax) / qty, and the arithmetic is written into the
spool note so the number can be audited later.

  Bambu line:  {"order": "...", "date": "YYYY-MM-DD", "sku": "A00-W1-1.75-1000-SPL",
                "code": "10100", "price": 14.94, "tax": 0.97, "qty": 1}
               code = the 5-digit color code on the invoice ("Jade White (10100)").
               A SKU ending SPLFREE is a refill; that is noted, nothing else changes.
  Other line:  {"order": ..., "date": ..., "sku": ..., "price": ..., "tax": ..., "qty": ...,
                "brand": "Insignia", "material": "PLA", "subtype": "Plus",
                "color": "White", "rgba": "FAFAFAFF", "fp": 30214,
                "preset": ["GFL99", "Generic PLA"]}
               fp = 3dfilamentprofiles detail id; `link` keys on it via the note.
  Either:      "update": <spool id>  -> price an existing spool instead of creating one
               "spools": N           -> create N spools from a line whose qty covers more
                                        (e.g. 1 of a 4-spool bundle: qty 4, spools 1)
               "tax_basis": "..."    -> how the tax was derived when the receipt has no
                                        per-line tax, e.g. "receipt-wide 7.0%"
               "sku": use the receipt's SKU; with none printed, the product name.
"""
import argparse, glob, io, json, os, re, subprocess, sys, urllib.request, uuid, zipfile

B = os.environ.get("BAMBUDDY", "https://bambuddy.internal.nullreference.io") + "/api/v1"
COLOR_TABLE = "/app/squashfs-root/resources/profiles/BBL/filament/filaments_color_codes.json"
# fila_type from the color table -> (material, subtype) as Bambuddy's RFID
# parser stores them. The matcher links a scanned tag to an untagged spool only
# on an exact material + RGBA match (subtype may be NULL), so these must agree.
BAMBU_MAT = {"PLA Basic": ("PLA", "Basic"), "PLA Matte": ("PLA", "Matte"), "PLA Translucent": ("PLA", "Translucent"),
             "PLA Glow": ("PLA", "Glow"), "PETG Basic": ("PETG", "Basic"), "PETG HF": ("PETG", "HF"),
             "PETG Translucent": ("PETG", "Translucent"), "PETG-CF": ("PETG-CF", None),
             "TPU for AMS": ("TPU", "for AMS")}


def req(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    r = urllib.request.Request(B + path, method=method, data=data, headers={"content-type": ctype})
    with urllib.request.urlopen(r) as f:
        t = f.read()
        return json.loads(t) if t else None


def color_table():
    out = subprocess.run(["kubectl", "-n", "bambuddy", "exec", "deploy/bambuddy-slicer", "--", "cat", COLOR_TABLE],
                         check=True, capture_output=True).stdout
    return {d["fila_color_code"]: d for d in json.loads(out)["data"]}


def cost_note(line):
    per = round((line["price"] + line["tax"]) / line["qty"], 2)
    split = f" (line ${line['price']:.2f} + ${line['tax']:.2f} tax for {line['qty']})" if line["qty"] > 1 \
        else f" ${line['price']:.2f} + ${line['tax']:.2f} tax"
    tax = f" ({line['tax_basis']})" if line.get("tax_basis") else ""
    note = f"{line['order']}, {line['date']}, SKU {line['sku']}:{split}{tax} = ${per:.2f}/spool"
    if line["sku"].endswith("SPLFREE"):
        note += " (refill, on reusable spool)"
    return per, note


def spool_from_line(line, codes):
    per, note = cost_note(line)
    if "code" in line:
        c = codes[line["code"]]
        cols = [x.lstrip("#").upper() for x in c["fila_color"]]
        mat, sub = BAMBU_MAT.get(c["fila_type"], (None, None))
        effect = None
        if c["fila_type"] == "PLA Silk":  # multi-color silk: A05-M* dual, A05-T* tri (Bambuddy's own rule)
            mat, sub = "PLA", ("Dual Color" if line["sku"].split("-")[1].startswith("M") else "Tri Color")
            effect = "dual-color" if sub == "Dual Color" else "tri-color"
        if mat is None:
            sys.exit(f"no material mapping for fila_type {c['fila_type']!r}; add it to BAMBU_MAT")
        spool = dict(brand="Bambu Lab", material=mat, subtype=sub, color_name=c["fila_color_name"]["en"].split(" (")[0],
                     rgba=cols[0], extra_colors=",".join(cols[1:]) or None, effect_type=effect,
                     core_weight=250, core_weight_catalog_id=24,
                     # /cloud/builtin-filaments omits some real ids (GFA17 PLA Translucent);
                     # they are still valid, since it is what the printer reads off the tag.
                     slicer_filament=c["fila_id"], slicer_filament_name="Bambu " + c["fila_type"])
    else:
        spool = dict(brand=line["brand"], material=line["material"], subtype=line.get("subtype"),
                     color_name=line["color"], rgba=line.get("rgba"))
        if line.get("preset"):
            spool.update(slicer_filament=line["preset"][0], slicer_filament_name=line["preset"][1])
        if line.get("fp"):
            note += f"\n3dfilamentprofiles: https://3dfilamentprofiles.com/filament/details/{line['fp']}"
    spool.update(label_weight=1000, cost_per_kg=per, note=note)
    return {k: v for k, v in spool.items() if v is not None}


def apply(args):
    lines = json.load(open(args.lines))
    codes = color_table() if any("code" in l for l in lines) else {}
    created = 0
    for line in lines:
        if "update" in line:
            per, note = cost_note(line)
            patch = {"cost_per_kg": per, "note": note}
            print("update", line["update"], patch["cost_per_kg"], patch["note"].splitlines()[0])
            if not args.dry_run:
                req("PATCH", f"/inventory/spools/{line['update']}", patch)
            continue
        spool = spool_from_line(line, codes)
        n = line.get("spools", line["qty"])
        print("create", n, spool["brand"], spool["material"], spool.get("subtype"), spool["color_name"],
              spool.get("rgba"), spool["cost_per_kg"])
        if not args.dry_run:
            req("POST", "/inventory/spools/bulk", {"spool": spool, "quantity": n})
        created += n
    print(f"{'would create' if args.dry_run else 'created'} {created} spools")


def nozzle(name):
    m = re.search(r"@\S+( (0\.[268])n)?$", name)
    return (m.group(2) if m and m.group(2) else "0.4")


def link(args):
    files = sorted(glob.glob(os.path.join(args.profiles, "*__*.json")))
    by_fp = {}
    for f in files:
        by_fp.setdefault(os.path.basename(f).split("__", 1)[0], []).append(f)
    if not args.dry_run and files:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in files:
                z.write(f, os.path.basename(f).split("__", 1)[1])
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"profiles.zip\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        res = req("POST", "/local-presets/import", raw=body, ctype=f"multipart/form-data; boundary={boundary}")
        print("import:", res)
        if res.get("errors"):
            sys.exit("import reported errors; not linking")
    presets = {p["name"]: p for p in req("GET", "/local-presets/")["filament"]}
    per_fp = {}
    for fid, fs in by_fp.items():
        for f in fs:
            name = json.load(open(f))["name"]
            p = presets.get(name)
            if p is None and not args.dry_run:
                sys.exit(f"{name!r} not in Bambuddy after import")
            per_fp.setdefault(fid, {})[nozzle(name)] = p or {"id": "?", "name": name}
    targets = {}
    for s in req("GET", "/inventory/spools"):
        m = re.search(r"filament/details/(\d+)", s.get("note") or "")
        if m and m.group(1) in per_fp:
            targets[s["id"]] = m.group(1)
    for pair in args.map:
        sid, fid = pair.split("=")
        targets[int(sid)] = fid
    for sid, fid in sorted(targets.items()):
        ps = per_fp[fid]
        if "0.4" not in ps:
            sys.exit(f"fp {fid} has no 0.4 profile; the spool default needs one")
        rows = [{"printer_model": args.printer, "nozzle_diameter": nz, "slicer_filament": str(p["id"]),
                 "slicer_filament_name": p["name"]} for nz, p in sorted(ps.items())]
        print(sid, fid, sorted(ps), ps["0.4"]["name"])
        if not args.dry_run:
            req("PUT", f"/inventory/spools/{sid}/filament-presets", rows)
            req("PATCH", f"/inventory/spools/{sid}",
                {"slicer_filament": str(ps["0.4"]["id"]), "slicer_filament_name": ps["0.4"]["name"]})


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sp = ap.add_subparsers(dest="cmd", required=True)
a = sp.add_parser("apply"); a.add_argument("lines"); a.add_argument("--dry-run", action="store_true"); a.set_defaults(fn=apply)
l = sp.add_parser("link"); l.add_argument("profiles"); l.add_argument("--printer", default="H2C")
l.add_argument("--map", nargs="*", default=[], metavar="SPOOL=FP"); l.add_argument("--dry-run", action="store_true")
l.set_defaults(fn=link)
args = ap.parse_args()
args.fn(args)
