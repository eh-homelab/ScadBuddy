"""dollhouse-kit: the whole house in one Generate (spec 2026-09-27 §5.4).

Deterministic: no I/O, no clock, no randomness. Every render goes through ctx. The
counting rules are the designer's (`ui/pieces.js` `housePieces`); keep them in step."""

import asyncio

INPUTS_VERSION = 1

LIMITS = {"cols": (1, 4), "rows": (1, 3), "storeys": (1, 3), "windows": (0, 14)}
DEFAULT_HOUSE = {"cols": 2, "rows": 1, "storeys": 1, "windows": 2}

LABELS = {
    "wall": "Wall",
    "wall_window": "Wall with window",
    "wall_door_lower": "Door wall, lower course",
    "wall_door_upper": "Door wall, upper course",
    "door_leaf_lower": "Door leaf, lower half",
    "door_leaf_upper": "Door leaf, upper half",
    "corner_post": "Corner post",
    "floor_tile": "Floor tile",
    "roof_panel": "Roof panel",
    "stairs_lower": "Stairs, lower half",
    "stairs_upper": "Stairs, upper half",
    "railing": "Railing",
    "connectors": "Connectors (keys, pegs, hinge pins)",
}
#: Walls and corner posts come in a lower and an upper course (the model renders them
#: differently), so each course is its own entry. A window wall is always upper.
COURSED = {"wall", "corner_post"}
#: A piece's key holds only what shapes it (§5.4: new wallpaper re-renders walls, not floors).
WALL_STYLE = {
    "exterior",
    "texture_size",
    "wallpaper",
    "pattern_repeats",
    "wainscoting",
    "wainscot_height",
    "baseboard",
    "crown_moulding",
}
WINDOW = {
    "window_style",
    "window_width",
    "window_height",
    "window_sill",
    "window_panes_x",
    "window_panes_y",
    "glass",
    "shutters",
    "flower_box",
    "trim",
}
DOOR = {"door_style", "door_width", "door_height", "door_panels"}
WALLS = {"wall", "wall_window", "wall_door_lower", "wall_door_upper", "corner_post"}
SHAPE = {"piece", "course", "width_units", "depth_units"}


def clamp_house(house):
    """`clampHouse`: a value that is not a number takes the default; the rest is clamped.
    As JS `Number(null)` and `Number("")` are 0, a null or empty value is 0 (then
    clamped to the minimum); a missing key is `undefined`, NaN, the default."""
    out = {}
    for key, (low, high) in LIMITS.items():
        raw = house.get(key, "missing") if isinstance(house, dict) else "missing"
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            raw = 0
        try:
            value = int(float(raw))
        except (TypeError, ValueError, OverflowError):
            out[key] = DEFAULT_HOUSE[key]
            continue
        out[key] = min(high, max(low, value))
    return out


def house_pieces(house):
    """`housePieces`, rule for rule: a coursed piece (wall, corner post) is one entry per
    course, `<piece>:<course>`; every other piece is one entry by its own name. Count-0
    entries are left out."""
    h = clamp_house(house)
    perimeter = 2 * (h["cols"] + h["rows"])
    entries = {}

    def add(piece, course, count):
        if count <= 0:
            return
        coursed = piece in COURSED
        ident = f"{piece}:{course}" if coursed else piece
        entry = entries.setdefault(
            ident,
            {
                "id": ident,
                "piece": piece,
                "course": course,
                "count": 0,
                "label": f"{LABELS[piece]}, {course} course" if coursed else LABELS[piece],
            },
        )
        entry["count"] += count

    for storey in range(h["storeys"]):
        ground = storey == 0
        openings = perimeter - 1 if ground else perimeter
        glazed = min(h["windows"], openings)
        if ground:
            add("wall_door_lower", None, 1)
            add("wall_door_upper", None, 1)
        add("wall", "lower", openings)
        add("wall_window", "upper", glazed)
        add("wall", "upper", openings - glazed)
        add("corner_post", "lower", 4)
        add("corner_post", "upper", 4)
        add("floor_tile", None, h["cols"] * h["rows"])
    add("roof_panel", None, h["cols"] * h["rows"])
    add("stairs_lower", None, h["storeys"] - 1)
    add("stairs_upper", None, h["storeys"] - 1)
    add("railing", None, h["storeys"] - 1)
    add("door_leaf_lower", None, 1)
    add("door_leaf_upper", None, 1)
    add("connectors", None, 1)
    return list(entries.values())


def piece_params(entry, style):
    """`pieceParams` (piece, its course when it has one, one module), plus the style that
    shapes this piece."""
    piece = entry["piece"]
    keep = {k: v for k, v in style.items() if k not in SHAPE}
    if piece not in WALLS:
        keep = {k: v for k, v in keep.items() if k not in WALL_STYLE}
    if piece != "wall_window":
        keep = {k: v for k, v in keep.items() if k not in WINDOW}
    if not piece.startswith(("wall_door", "door_leaf")):
        keep = {k: v for k, v in keep.items() if k not in DOOR}
    params = {**keep, "piece": piece, "width_units": 1, "depth_units": 1}
    if entry["course"]:
        params["course"] = entry["course"]
    return params


def migrate(inputs, from_version):
    if from_version == 0:
        return {**inputs, "house": clamp_house(inputs.get("house") or DEFAULT_HOUSE)}
    return inputs


async def run(ctx, inputs):
    house = clamp_house(inputs.get("house") or DEFAULT_HOUSE)
    style = dict(inputs.get("params", {}))
    entries = house_pieces(house)
    done = 0

    async def one(entry):
        nonlocal done
        part = await ctx.render("model.scad", **piece_params(entry, style))
        done += 1
        ctx.progress(f"Rendered {entry['label']}", done=done, total=len(entries))
        return part

    parts = await asyncio.gather(*(one(e) for e in entries))
    guide = await ctx.activity("assembly_guide", house, entries)
    layout = await ctx.pack([(part, e["count"]) for e, part in zip(entries, parts, strict=True)])
    bom = [
        {"piece": e["id"], "label": e["label"], "count": e["count"], "part": p.piece_key}
        for e, p in zip(entries, parts, strict=True)
    ]
    storeys = house["storeys"]
    name = f"{house['cols']} x {house['rows']} house, {storeys} storey{'s' if storeys > 1 else ''}"
    await ctx.output(plates=layout, name=name, bom=bom, files={"assembly.svg": guide})
