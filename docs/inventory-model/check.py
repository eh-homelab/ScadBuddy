"""Check the inventory model's examples against its rules (design §7).

Run from the repo root:
    uvx --with pyyaml python -I docs/inventory-model/check.py

It resolves every example item's class and handlers from kinds.yaml and handlers.yaml,
checks every handler's step, class and parameters, every instance, and every
relationship in examples.yaml (including the ones marked `expect: refused`), then checks
that README.md's handler table is the one it resolved. It exits 1 on the first rule
broken. The Go implementation's first test loads these same files (design §8).
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
STEPS = ["receive", "consume", "count", "move", "retire", "cost", "life", "bill", "label", "sync"]
FULL = {"physical": ("nature", "role", "counting"), "service": ("nature", "billing")}
TYPES = {"integer": int, "number": (int, float), "string": str, "boolean": bool, "list": list}


class Broken(Exception):
    pass


def load(name: str) -> dict:
    return yaml.safe_load((HERE / name).read_text())


H = load("handlers.yaml")
R = load("relations.yaml")
K = {k["id"]: k for k in load("kinds.yaml")["kinds"]}
X = load("examples.yaml")
HANDLERS = {h["id"]: h for h in H["handlers"]}
SPECIAL = H["special_types"]
RTYPES = {t["id"]: t for t in R["relation_types"]}
ITEMS = {i["id"]: i for i in X["items"]}
INST = {i["id"]: i for i in X["instances"]}


def chain(kind_id: str) -> list[dict]:
    """The kind and its ancestors, root first."""
    out = []
    while kind_id:
        if kind_id not in K:
            raise Broken(f"unknown kind {kind_id}")
        out.append(K[kind_id])
        kind_id = K[kind_id].get("parent")
    return out[::-1]


def kind_class(kind_id: str) -> tuple[dict, str | None]:
    cls: dict = {}
    special = None
    for k in chain(kind_id):
        for key, val in (k.get("class") or {}).items():
            if key in cls and cls[key] != val:
                raise Broken(f"kind {k['id']} changes {key} from {cls[key]} to {val}")
            cls[key] = val
        if k.get("special"):
            if special:
                raise Broken(f"kind {k['id']} sets a second special type")
            special = k["special"]
    return cls, special


def matches(want: dict, cls: dict, special: str | None) -> bool:
    for key, val in want.items():
        if key == "special":
            if special != val:
                return False
        elif cls.get(key) != val:
            return False
    return True


def check_params(where: str, handler: dict, params: dict) -> None:
    schema = handler.get("params") or {}
    for key in params:
        if key not in schema:
            raise Broken(f"{where}: {handler['id']} has no parameter {key}")
    for key, spec in schema.items():
        if key not in params:
            if spec.get("required"):
                raise Broken(f"{where}: {handler['id']} needs parameter {key}")
            continue
        val = params[key]
        if isinstance(val, bool) and spec["type"] in ("integer", "number"):
            raise Broken(f"{where}: {key} is not a {spec['type']}")
        if not isinstance(val, TYPES[spec["type"]]):
            raise Broken(f"{where}: {key} is not a {spec['type']}")
        if "enum" in spec and val not in spec["enum"]:
            raise Broken(f"{where}: {key}={val} is not one of {spec['enum']}")


def apply_steps(res: dict, steps: dict | None) -> None:
    for step, conf in (steps or {}).items():
        if step not in STEPS:
            raise Broken(f"unknown step {step}")
        if conf.get("handler") and conf["handler"] != res[step][0]:
            res[step] = (conf["handler"], {})
        res[step] = (res[step][0], {**res[step][1], **(conf.get("params") or {})})


def resolve(item: dict) -> tuple[dict, str | None, dict]:
    """Design §4.2: class default, then special type, then kinds root to leaf, then item."""
    cls, special = kind_class(item["kind"])
    nature = cls.get("nature")
    if nature not in FULL or any(k not in cls for k in FULL[nature]):
        raise Broken(f"item {item['id']}: kind {item['kind']} leaves its class open: {cls}")
    if cls.get("role") == "durable" and cls.get("counting") != "unit":
        raise Broken(f"item {item['id']}: a durable thing is counted by unit")
    cols = [d for d in H["defaults"].values() if matches(d["match"], cls, None)]
    if len(cols) != 1:
        raise Broken(f"item {item['id']}: {len(cols)} default columns match {cls}")
    res = {step: (cols[0]["handlers"][step], {}) for step in STEPS}
    if special:
        sp = SPECIAL[special]
        if not matches(sp["needs"], cls, None):
            raise Broken(f"item {item['id']}: special type {special} needs {sp['needs']}, class is {cls}")
        for step, hid in sp["handlers"].items():
            res[step] = (hid, {})
        for prop in sp.get("requires_properties", []):
            if prop not in (item.get("properties") or {}):
                raise Broken(f"item {item['id']}: special type {special} requires property {prop}")
    for k in chain(item["kind"]):
        apply_steps(res, k.get("steps"))
        for prop, spec in (k.get("schema") or {}).items():
            if spec.get("required") and prop not in (item.get("properties") or {}):
                raise Broken(f"item {item['id']}: kind {k['id']} requires property {prop}")
    apply_steps(res, item.get("steps"))
    for step, (hid, params) in res.items():
        h = HANDLERS.get(hid)
        if h is None:
            raise Broken(f"item {item['id']}: unknown handler {hid}")
        if step not in h["steps"]:
            raise Broken(f"item {item['id']}: {hid} does not fill step {step}")
        if not matches(h.get("applies") or {}, cls, special):
            raise Broken(f"item {item['id']}: {hid} does not apply to {cls} / {special}")
        check_params(f"item {item['id']} step {step}", h, params)
    return cls, special, res


RESOLVED = {}
for item in X["items"]:
    RESOLVED[item["id"]] = resolve(item)


def ref_kinds(ref: str) -> tuple[str, list[str], dict]:
    """(target type, the kind chain ids, the item's properties) of `type:id`."""
    target, rid = ref.split(":", 1)
    if target == "kind":
        return target, [k["id"] for k in chain(rid)], {}
    if target == "item":
        item = ITEMS[rid]
    elif target == "unit":
        inst = INST[rid]
        if inst["type"] != "unit":
            raise Broken(f"{ref} is a {inst['type']}, not a unit")
        item = ITEMS[inst["item"]]
    else:
        raise Broken(f"unknown target {target}")
    return target, [k["id"] for k in chain(item["kind"])], item.get("properties") or {}


def end_ok(ref: str, rule: dict) -> str | None:
    target, kinds, _ = ref_kinds(ref)
    if target not in rule["target"]:
        return f"{ref}: a {target} cannot stand here"
    cls, special = kind_class(kinds[-1])
    if not matches(rule.get("class") or {}, cls, special):
        return f"{ref}: class {cls} is not {rule.get('class')}"
    return None


def accepting_rule(container: str, unit: str, accepts: list[dict]) -> dict | None:
    """The `accepts` rule on the container (its unit, item or kinds) that allows the unit."""
    _, ckinds, _ = ref_kinds(container)
    cid = container.split(":", 1)[1]
    owners = {container, f"item:{INST[cid]['item']}"} | {f"kind:{k}" for k in ckinds}
    _, ukinds, uprops = ref_kinds(unit)
    uitem = f"item:{INST[unit.split(':', 1)[1]]['item']}"
    for a in accepts:
        if a["from"] not in owners:
            continue
        if a["to"] != uitem and a["to"] not in {f"kind:{k}" for k in ukinds}:
            continue
        if all(uprops.get(k) == v for k, v in (a.get("where") or {}).items()):
            return a
    return None


def check_relation(rel: dict, accepts: list[dict], held: dict) -> str | None:
    t = RTYPES.get(rel["type"])
    if t is None:
        return f"unknown relationship type {rel['type']}"
    for end in ("from", "to"):
        why = end_ok(rel[end], t[end])
        if why:
            return why
    if rel["type"] == "contains":
        if rel["to"] in held:
            return f"{rel['to']} is already in {held[rel['to']]}"
        rule = accepting_rule(rel["from"], rel["to"], accepts)
        if rule is None:
            return f"{rel['from']} does not accept {rel['to']}"
        inside = [u for u, c in held.items() if c == rel["from"] and accepting_rule(c, u, [rule])]
        if rule.get("max") is not None and len(inside) >= rule["max"]:
            return f"{rel['from']} holds {rule['max']} of {rule['to']} at most, and has {inside}"
    return None


accepts: list[dict] = []
held: dict = {}
for inst in X["instances"]:
    if inst["item"] not in ITEMS:
        raise Broken(f"instance {inst['id']}: unknown item")
    cls, _, _ = RESOLVED[inst["item"]]
    want = "lot" if cls.get("counting") == "lot" else "unit"
    if cls["nature"] == "service":
        if cls["billing"] != "term":
            raise Broken(f"instance {inst['id']}: an {cls['billing']} service has no instances")
        if not inst.get("starts_at") or not inst.get("ends_at"):
            raise Broken(f"instance {inst['id']}: a term needs starts_at and ends_at")
    if inst["type"] != want:
        raise Broken(f"instance {inst['id']}: is a {inst['type']}, its class counts by {want}")

for rel in X["relations"]:
    why = check_relation(rel, accepts, held)
    if rel.get("expect") == "refused":
        if why is None:
            raise Broken(f"allowed what the design must refuse: {rel}")
        print(f"refused as expected: {rel['type']} {rel['from']} -> {rel['to']}: {why}")
        continue
    if why:
        raise Broken(f"refused {rel}: {why}")
    if rel["type"] == "accepts":
        accepts.append(rel)
    if rel["type"] == "contains":
        held[rel["to"]] = rel["from"]

# Replay the history (design §4.5, §4.7). Uses are booked per unit, through its
# container. Every handler value is cached per unit; a row is appended to its history
# only when the value changes. A refresh recomputes and appends only on a change.
uses: list[dict] = []  # unit_uses rows: unit, at, hours, prints, via
cache: dict = {}  # unit -> current value (handler_values)
value_history: list[dict] = []  # handler_value_history
config_history: list[dict] = []  # handler_config_history


def unit_params(unit: str, step: str) -> tuple[str, dict]:
    """The handler and parameters in force for a unit: its item's, then its own (§4.2)."""
    inst = INST[unit.split(":", 1)[1]]
    hid, params = RESOLVED[inst["item"]][2][step]
    own = (inst.get("steps") or {}).get(step) or {}
    return hid, {**params, **(own.get("params") or {})}


def months_between(a, b) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


def life_state(unit: str, at) -> dict:
    """The unit's life handler, answered from its own unit_uses rows up to `at`."""
    inst = INST[unit.split(":", 1)[1]]
    hid, params = unit_params(unit, "life")
    rows_ = [u for u in uses if u["unit"] == unit and u["at"] <= at]
    # A manual reading (like a weighing) sets the measure; later uses count on top of it.
    readings = [u for u in rows_ if u.get("reading") is not None]
    base, after = 0, rows_
    if readings:
        base = readings[-1]["reading"]
        after = [u for u in rows_ if u["seq"] > readings[-1]["seq"] and u.get("reading") is None]
    if hid == "life.hours":
        if params["source"] == "container_run_hours":
            used = base + sum(u["hours"] for u in after if u["via"])
        elif params["source"] == "own_run_hours":
            used = base + sum(u["hours"] for u in after if not u["via"])
        else:
            used = base
        life = params["hours"]
    elif hid == "life.uses":
        used, life = base + sum(u["prints"] for u in after), params["uses"]
    elif hid == "life.months":
        used, life = max(months_between(inst["in_service_at"], at), 0), params["months"]
    else:
        raise Broken(f"{unit}: {hid} is not replayed by the examples")
    share = min(used / life, 1)
    cost = inst["cost_minor"]
    salvage = cost * (params.get("salvage_pct") or 0) / 100
    warn = "warn_pct" in params and share * 100 >= params["warn_pct"]
    return {"used": used, "share": round(share, 4), "warn": warn,
            "book": round(cost - (cost - salvage) * share, 2)}


def recompute(unit: str, at, reason: str) -> bool:
    """Refresh one unit's cached life value; append history only if it changed."""
    value = life_state(unit, at)
    if cache.get(unit) == value:
        return False
    cache[unit] = value
    value_history.append({"unit": unit, "at": at, "reason": reason, **value})
    return True


TRACKED = [f"unit:{i['id']}" for i in X["instances"] if "cost_minor" in i]
for u in TRACKED:
    recompute(u, X["history"][0]["at"], "received")

for ev in X.get("history", []):
    at = ev["at"]
    if ev["do"] == "add":
        why = check_relation(ev["rel"], accepts, held)
        if why:
            raise Broken(f"history {at}: refused {ev['rel']}: {why}")
        held[ev["rel"]["to"]] = ev["rel"]["from"]
        recompute(ev["rel"]["to"], at, "relation")
    elif ev["do"] == "remove":
        if held.get(ev["rel"]["to"]) != ev["rel"]["from"]:
            raise Broken(f"history {at}: {ev['rel']['to']} is not in {ev['rel']['from']}")
        del held[ev["rel"]["to"]]
        recompute(ev["rel"]["to"], at, "relation")
    elif ev["do"] == "run":
        printer = ev["printer"]
        if unit_params(printer, "consume")[0] != "printer.consume_run":
            raise Broken(f"history {at}: {printer} has no printer consume handler")
        inside = [u for u, c in held.items() if c == printer]
        uses.append({"unit": printer, "at": at, "hours": ev["hours"], "prints": ev["prints"], "via": None,
                     "seq": len(uses)})
        for u in inside:
            uses.append({"unit": u, "at": at, "hours": ev["hours"], "prints": ev["prints"], "via": printer,
                         "seq": len(uses)})
        for u in [printer, *inside]:
            if u in TRACKED:
                recompute(u, at, "use")
    elif ev["do"] == "set_params":
        unit = ev["target"]
        inst = INST[unit.split(":", 1)[1]]
        old = unit_params(unit, ev["step"])
        steps = inst.setdefault("steps", {})
        conf = steps.setdefault(ev["step"], {})
        conf["params"] = {**(conf.get("params") or {}), **ev["params"]}
        new = unit_params(unit, ev["step"])
        check_params(f"history {at} {unit}", HANDLERS[new[0]], new[1])
        config_history.append({"target": unit, "at": at, "step": ev["step"], "handler": new[0],
                               "old": old[1], "new": new[1], "actor": ev.get("actor", "person")})
        recompute(unit, at, "params_changed")
    elif ev["do"] == "reading":
        unit = ev["target"]
        if unit_params(unit, ev["step"])[0] not in ("life.hours", "life.uses"):
            raise Broken(f"history {at}: {unit} {ev['step']} takes no manual reading")
        uses.append({"unit": unit, "at": at, "hours": 0, "prints": 0, "via": None, "seq": len(uses),
                     "reading": ev["value"], "actor": ev.get("actor", "person")})
        recompute(unit, at, "manual")
    elif ev["do"] == "refresh":
        scope = TRACKED if ev["scope"] == "all" else [ev["scope"]]
        changed = [u for u in scope if recompute(u, at, "refresh")]
        print(f"refresh {ev['scope']} at {at}: {len(changed)} changed {changed}")
        if "expect_changed" in ev and changed != ev["expect_changed"]:
            raise Broken(f"refresh at {at}: expected {ev['expect_changed']} to change, got {changed}")
    else:
        raise Broken(f"history: unknown event {ev['do']}")

for exp in X.get("expect_life", []):
    got = cache[f"unit:{exp['unit']}"]
    ok = (got["used"] == exp["used"] and abs(got["share"] - exp["share"]) < 1e-4
          and got["warn"] == exp["warn"] and abs(got["book"] - exp["book_value_minor"]) < 0.01)
    if not ok:
        raise Broken(f"cached life of {exp['unit']}: expected {exp}, got {got}")
    print(f"life as expected: {exp['unit']} used {got['used']}, {got['share']:.1%}, "
          f"warn {got['warn']}, book value {got['book']:.2f}")

# The cache is derived: a rebuild from the sources must give the same values (§4.7).
last = X["history"][-1]["at"]
for u in TRACKED:
    if life_state(u, last) != cache[u]:
        raise Broken(f"cache of {u} differs from a rebuild: {cache[u]} vs {life_state(u, last)}")

for unit, rows_ in (X.get("expect_history") or {}).items():
    got = [{k: r[k] for k in ("at", "reason", "used", "share", "book")}
           for r in value_history if r["unit"] == f"unit:{unit}"]
    want = [{**r, "at": r["at"]} for r in rows_]
    if got != want:
        raise Broken(f"history of {unit}: expected {want}, got {got}")
    print(f"history as expected: {unit}: " + "; ".join(
        f"{r['at']} {r['reason']} {r['used']} {r['share']:.2%} {r['book']:.2f}" for r in got))
for exp in X.get("expect_config_history", []):
    if not any(c["target"] == exp["target"] and c["step"] == exp["step"] and c["old"].get(exp["key"]) == exp["old"]
               and c["new"].get(exp["key"]) == exp["new"] for c in config_history):
        raise Broken(f"config history lacks {exp}")
    print(f"config history as expected: {exp['target']} {exp['step']}.{exp['key']} {exp['old']} -> {exp['new']}")

rows = ["| Item | Class | " + " | ".join(STEPS) + " |", "|---|---|" + "---|" * len(STEPS)]
for iid, (cls, special, res) in RESOLVED.items():
    c = "/".join(cls[k] for k in FULL[cls["nature"]]) + (f" + {special}" if special else "")
    cells = ["—" if res[s][0] == "refuse" else f"`{res[s][0]}`" for s in STEPS]
    rows.append(f"| {ITEMS[iid]['name']} | {c} | " + " | ".join(cells) + " |")
table = "\n".join(rows)

if "--print" in sys.argv:
    print(table)
    sys.exit(0)
readme = (HERE / "README.md").read_text()
m = re.search(r"<!-- matrix:start -->\n(.*?)\n<!-- matrix:end -->", readme, re.S)
if not m or m.group(1).strip() != table.strip():
    raise Broken("README.md's table is not the resolved one: run with --print and paste it")
print(f"ok: {len(K)} kinds, {len(HANDLERS)} handlers, {len(ITEMS)} items, "
      f"{len(INST)} instances, {len(X['relations'])} relationships, "
      f"{len(X.get('history', []))} history events, {len(X.get('expect_life', []))} lives, "
      f"{len(value_history)} value history rows")
