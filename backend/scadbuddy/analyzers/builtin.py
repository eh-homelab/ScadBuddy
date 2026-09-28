"""The bundled analyzers: a small set, each resting on a quoted source (#284).

A rule is only here if its threshold is someone else's number, quoted in
:mod:`scadbuddy.analyzers.sources`. Measurements ScadBuddy has but cannot yet judge
against a citable threshold (thinnest wall, smallest feature, height-to-base ratio)
are deliberately absent; see the module list in the PR that added this.

IDs follow #284: ``SB0xxx`` the framework, ``SB1xxx`` geometry, ``SB2xxx`` material,
``SB3xxx`` inventory and history, ``SB4xxx`` plate.
"""

from __future__ import annotations

from scadbuddy.analyzers import sources as src
from scadbuddy.analyzers.context import AnalysisContext, FilamentSlot
from scadbuddy.analyzers.model import (
    Analyzer,
    AnalyzerDiagnostic,
    DiagnosticLocation,
    Evidence,
    Fix,
    change,
)
from scadbuddy.bambuddy.filaments import check as check_filaments
from scadbuddy.render.bambu3mf import plates_of, replate_3mf
from scadbuddy.render.geometry import OVERHANG_ANGLES, MeshEdge
from scadbuddy.render.plate import PlateFitError, fit_problem, overshoots

#: Located edges a mesh diagnostic carries per part; the counts are always complete.
MAX_EDGES_PER_DIAGNOSTIC = 100

# --- SB0001: the framework's own ----------------------------------------------------


def _never(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    return []


CRASHED = Analyzer(
    id="SB0001",
    name="analyzer-crashed",
    title="An analyzer failed",
    severity="warning",
    category="analyzer",
    description="An analyzer raised instead of reporting; its checks did not run.",
    sources=(src.ANALYZER_CRASH,),
    check=_never,
)


# --- SB1001 / SB1002: mesh defects --------------------------------------------------


def _mesh(
    context: AnalysisContext, analyzer: Analyzer, *, kind: str, attribute: str
) -> list[AnalyzerDiagnostic]:
    geometry = context.geometry
    assert geometry is not None
    found: list[AnalyzerDiagnostic] = []
    for part in geometry.parts:
        count = getattr(part, attribute)
        if not part.edges_checked or not count:
            continue
        edges: list[MeshEdge] = [
            edge for edge in geometry.edges if edge.part == part.part and edge.kind == kind
        ]
        label = "non-manifold" if kind == "non_manifold" else "open"
        found.append(
            analyzer.diagnose(
                key=f"part-{part.part}",
                message=(
                    f"The {part.colour} part ({part.name}) has {count} {label} "
                    f"edge{'s' if count != 1 else ''}."
                ),
                why=(
                    "Measured on the closed per-colour solid ScadBuddy renders for this "
                    "colour, never on the preview split, whose seams are open by design. "
                    "Common causes in OpenSCAD are coincident faces, zero-thickness walls "
                    "and minkowski()/hull() edge cases. Bambu Studio reports these as "
                    "named in the sources, and can repair some of them."
                ),
                location=DiagnosticLocation(
                    kind="mesh",
                    part=part.part,
                    colour=part.colour,
                    bbox=part.bbox,
                    edges=edges[:MAX_EDGES_PER_DIAGNOSTIC],
                    edges_truncated=len(edges) > MAX_EDGES_PER_DIAGNOSTIC
                    or geometry.edges_truncated,
                ),
                evidence=[
                    Evidence(label=f"{label} edges", value=count, origin="geometry"),
                    Evidence(label="triangles", value=part.triangles, origin="geometry"),
                ],
            )
        )
    return found


def _non_manifold(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    return _mesh(context, analyzer, kind="non_manifold", attribute="non_manifold_edges")


def _open_edges(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    return _mesh(context, analyzer, kind="open", attribute="open_edges")


NON_MANIFOLD = Analyzer(
    id="SB1001",
    name="non-manifold-mesh",
    title="Non-manifold edges",
    severity="error",
    category="geometry",
    description=(
        "Edges shared by three or more triangles in a part's closed solid. Error, as "
        "Bambu Studio reports them."
    ),
    sources=(src.MESH_DEFECT_SEVERITY, src.FIX_MODEL),
    check=_non_manifold,
    needs=frozenset({"geometry"}),
)

OPEN_EDGES = Analyzer(
    id="SB1002",
    name="open-mesh",
    title="Open edges",
    severity="info",
    category="geometry",
    description=(
        "Edges used by one triangle only in a part's closed solid. Info, as Bambu "
        "Studio reports them."
    ),
    sources=(src.MESH_DEFECT_SEVERITY, src.FIX_MODEL),
    check=_open_edges,
    needs=frozenset({"geometry"}),
)


# --- SB1003: overhangs past the default support threshold ---------------------------

#: ``support_threshold_angle`` in every BBL process preset (``fdm_process_common.json``),
#: measured from the horizontal. The geometry analysis measures overhang *below*
#: horizontal from the vertical, so a face at 60 deg or more there is a slope of 30 deg
#: or less.
SUPPORT_THRESHOLD_DEG = 30
_OVERHANG_BUCKET = 90 - SUPPORT_THRESHOLD_DEG
# The geometry analysis must measure that bucket, or SB1003 would silently find nothing.
assert _OVERHANG_BUCKET in OVERHANG_ANGLES, (_OVERHANG_BUCKET, OVERHANG_ANGLES)

ENABLE_SUPPORT_FIX = "enable-support"


def _overhangs(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    geometry = context.geometry
    assert geometry is not None
    bucket = next(
        (row for row in geometry.overhangs if row.min_angle_deg == _OVERHANG_BUCKET), None
    )
    if bucket is None or bucket.faces == 0:
        return []
    fix = Fix(
        id=ENABLE_SUPPORT_FIX,
        title="Turn on supports",
        description=(
            "Slice with a process preset derived from the resolved one, with supports on. "
            "The threshold angle is left at the base preset's."
        ),
        changes=[
            change(
                "derived_process_preset",
                "enable_support",
                "1",
                base_note=(
                    "Bambuddy exposes no preset contents; every BBL system process preset "
                    'inherits "enable_support": "0" from fdm_process_common.json.'
                ),
                sources=[src.ENABLE_SUPPORT, src.PROCESS_COMMON_SUPPORT],
            )
        ],
    )
    return [
        analyzer.diagnose(
            message=(
                f"{bucket.area_mm2:.0f} mm\u00b2 of overhang slopes {SUPPORT_THRESHOLD_DEG}"
                "\u00b0 or less from horizontal, where Bambu Studio's default threshold "
                "generates support once supports are on."
            ),
            why=(
                "Bambu's process presets generate support below a 30\u00b0 slope and leave "
                "supports off. The area is an upper bound: the geometry analysis cannot "
                "tell a bridge between two pillars from an unsupported ceiling, and "
                "ScadBuddy cannot read whether the resolved process preset turns supports on."
            ),
            location=DiagnosticLocation(kind="mesh", bbox=bucket.bbox),
            evidence=[
                Evidence(
                    label=f"overhang area at {_OVERHANG_BUCKET}\u00b0 or more below horizontal",
                    value=round(bucket.area_mm2, 1),
                    unit="mm\u00b2",
                    origin="geometry",
                ),
                Evidence(label="faces", value=bucket.faces, origin="geometry"),
            ],
            fixes=[fix],
        )
    ]


OVERHANGS = Analyzer(
    id="SB1003",
    name="overhang-support",
    title="Overhangs past the support threshold",
    severity="info",
    category="geometry",
    description=(
        "Overhang area at slopes Bambu Studio's default threshold (30\u00b0) would "
        "support, while its default process presets leave supports off."
    ),
    sources=(
        src.SUPPORT_THRESHOLD_ANGLE,
        src.PROCESS_COMMON_THRESHOLD,
        src.PROCESS_COMMON_SUPPORT,
    ),
    check=_overhangs,
    needs=frozenset({"geometry"}),
    fix_ids=(ENABLE_SUPPORT_FIX,),
)


# --- SB2001-SB2003: silk PLA --------------------------------------------------------

SILK_OUTER_WALL_SPEED = 50
SILK_NOZZLE_TEMPERATURE = 235
SILK_FIX = "silk-gloss"
#: Nozzles the silk guide says are not recommended, in millimetres.
SILK_UNRECOMMENDED_NOZZLES = (0.6, 0.8)
#: Both spellings of the Cool Plate SuperTack the resolver's ``BED_TYPES`` offers.
SUPERTACK = frozenset({"Supertack Plate", "Cool Plate (SuperTack)"})


def _is_silk(slot: FilamentSlot) -> bool:
    """Silk PLA by the spool's subtype or by its slicer preset's name.

    Both, because the inventory records Bambu's tri-colour silk as subtype
    ``Tri Color`` with the preset ``Bambu PLA Silk`` (``inventory-spools.json``).
    """
    material = (slot.material or "").lower()
    subtype = (slot.subtype or "").lower()
    preset = (slot.preset_name or "").lower()
    if material and material != "pla":
        return False
    return "silk" in subtype or ("silk" in preset and "pla" in preset)


def _silk_slots(context: AnalysisContext) -> list[FilamentSlot]:
    return [slot for slot in context.filaments if _is_silk(slot)]


def _slot_evidence(slots: list[FilamentSlot]) -> list[Evidence]:
    return [
        Evidence(
            label=f"slot {slot.slot_id}",
            value=" ".join(part for part in (slot.brand, slot.material, slot.subtype) if part)
            or slot.preset_name,
            origin="bambuddy:/inventory/spools",
        )
        for slot in slots
    ]


def _silk_gloss(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    slots = _silk_slots(context)
    if not slots:
        return []
    preset_note = "Bambuddy exposes no preset contents, so the base value is not known."
    changes = [
        change(
            "derived_process_preset",
            "outer_wall_speed",
            SILK_OUTER_WALL_SPEED,
            unit="mm/s",
            base_note=preset_note,
            sources=[src.SILK_OUTER_WALL, src.OUTER_WALL_SPEED],
        )
    ]
    for slot in slots:
        for setting, definition in (
            ("nozzle_temperature", src.NOZZLE_TEMPERATURE),
            ("nozzle_temperature_initial_layer", src.NOZZLE_TEMPERATURE_INITIAL),
        ):
            changes.append(
                change(
                    "filament_overrides",
                    setting,
                    SILK_NOZZLE_TEMPERATURE,
                    unit="\u00b0C",
                    slot_id=slot.slot_id,
                    base_note=preset_note,
                    sources=[src.SILK_TEMPERATURE, src.SILK_TEMPERATURE_RANGE, definition],
                )
            )
    fix = Fix(
        id=SILK_FIX,
        title="Apply Bambu's silk settings",
        description=(
            f"Outer wall at {SILK_OUTER_WALL_SPEED} mm/s (the guide's 40\u201360 mm/s, "
            f"starting value) and nozzle at {SILK_NOZZLE_TEMPERATURE} \u00b0C for the "
            "first and other layers of each silk slot."
        ),
        changes=changes,
    )
    slot_list = ", ".join(str(slot.slot_id) for slot in slots)
    return [
        analyzer.diagnose(
            message=(
                f"Silk PLA in slot{'s' if len(slots) > 1 else ''} {slot_list}: Bambu "
                "recommends a slower, consistent outer wall and a hotter nozzle for gloss."
            ),
            why=(
                "Bambu's silk guide names outer wall speed, nozzle temperature and speed "
                "consistency as what decides silk gloss, and gives the values quoted in "
                "the sources."
            ),
            location=DiagnosticLocation(kind="filament_slot", slot_id=slots[0].slot_id),
            evidence=_slot_evidence(slots),
            sources=[src.SILK_OUTER_WALL, src.SILK_TEMPERATURE, src.SILK_TEMPERATURE_RANGE],
            fixes=[fix],
            slots=[slot.slot_id for slot in slots],
        )
    ]


def _silk_nozzle(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    slots = _silk_slots(context)
    sizes = context.base.nozzle_sizes

    def millimetres(size: str) -> float | None:
        try:
            return float(size)
        except ValueError:
            return None

    wide = sorted({size for size in sizes if millimetres(size) in SILK_UNRECOMMENDED_NOZZLES})
    if not slots or not wide:
        return []
    return [
        analyzer.diagnose(
            message=(
                f"This slices for a {' and '.join(wide)} mm nozzle, which Bambu does not "
                "recommend for silk PLA."
            ),
            location=DiagnosticLocation(kind="choices", setting="nozzles"),
            evidence=[
                Evidence(label="nozzle", value=size, unit="mm", origin="choices") for size in wide
            ]
            + _slot_evidence(slots),
            slots=[slot.slot_id for slot in slots],
        )
    ]


def _silk_plate(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    slots = _silk_slots(context)
    if not slots or context.bed_type not in SUPERTACK:
        return []
    return [
        analyzer.diagnose(
            message="Bambu does not recommend the Cool Plate SuperTack for silk PLA.",
            location=DiagnosticLocation(kind="plate", setting="bed_type"),
            evidence=[
                Evidence(label="bed type", value=context.bed_type, origin="choices"),
                *_slot_evidence(slots),
            ],
            slots=[slot.slot_id for slot in slots],
        )
    ]


SILK_GLOSS = Analyzer(
    id="SB2001",
    name="silk-pla",
    title="Silk PLA settings",
    severity="info",
    category="material",
    description=(
        "Silk PLA on the plate: Bambu's silk guide values for outer wall speed and "
        "nozzle temperature, as a diff against the resolved presets."
    ),
    sources=(src.SILK_OUTER_WALL, src.SILK_TEMPERATURE, src.SILK_TEMPERATURE_RANGE),
    check=_silk_gloss,
    needs=frozenset({"filaments"}),
    fix_ids=(SILK_FIX,),
)

SILK_NOZZLE = Analyzer(
    id="SB2002",
    name="silk-pla-nozzle",
    title="Nozzle not recommended for silk PLA",
    severity="warning",
    category="material",
    description="Silk PLA sliced for a 0.6 or 0.8 mm nozzle.",
    sources=(src.SILK_NOZZLE,),
    check=_silk_nozzle,
    needs=frozenset({"filaments", "choices"}),
)

SILK_PLATE = Analyzer(
    id="SB2003",
    name="silk-pla-plate",
    title="Plate not recommended for silk PLA",
    severity="warning",
    category="material",
    description="Silk PLA on the Cool Plate SuperTack.",
    sources=(src.SILK_PLATE,),
    check=_silk_plate,
    needs=frozenset({"filaments", "choices"}),
)


# --- SB3002: not enough filament ----------------------------------------------------


def _low_filament(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    options = context.filament_options
    plan = context.request.filament_plan
    assert options is not None and plan is not None
    copies = context.copies
    slots = {slot.slot_id: slot for slot in options.slots}
    spools = {option.spool_id: option for option in options.spools}
    found: list[AnalyzerDiagnostic] = []
    # The print dialog's own rule, not a second copy of it (print-flow spec §4).
    for warning in check_filaments(options, plan, copies=copies):
        if warning.kind != "low-filament" or warning.slot_id is None:
            continue
        slot = slots[warning.slot_id]
        option = spools[plan.spool_for(warning.slot_id) or -1]
        assert slot.used_grams is not None and option.remaining_g is not None
        found.append(
            analyzer.diagnose(
                key=f"slot-{warning.slot_id}",
                message=warning.message,
                location=DiagnosticLocation(kind="filament_slot", slot_id=warning.slot_id),
                evidence=[
                    Evidence(
                        label="remaining",
                        value=round(option.remaining_g, 1),
                        unit="g",
                        origin="bambuddy:/printers/{id}/inventory-remain"
                        if option.loaded is not None
                        else "bambuddy:/inventory/spools",
                    ),
                    Evidence(
                        label="needed per copy",
                        value=round(slot.used_grams, 1),
                        unit="g",
                        origin="bambuddy:/library/files/{id}/filament-requirements",
                    ),
                    Evidence(label="copies", value=copies, origin="request"),
                ],
                slots=[warning.slot_id],
            )
        )
    return found


LOW_FILAMENT = Analyzer(
    id="SB3002",
    name="low-filament",
    title="Not enough filament",
    severity="warning",
    category="ams",
    description=(
        "A chosen spool has less left than this print needs, when the slice has "
        "reported grams. Advisory, as the print dialog's own warning is."
    ),
    sources=(src.LOW_FILAMENT_RULE,),
    check=_low_filament,
    needs=frozenset({"inventory"}),
)


# --- SB4001: plate fit --------------------------------------------------------------


def _multi_plate_problem(context: AnalysisContext) -> tuple[int, str | None] | None:
    """``(plates, problem)`` from the send's own check, or ``None`` without a 3MF.

    :func:`~scadbuddy.render.bambu3mf.replate_3mf` is what the send runs before an
    upload, and it checks every plate of a multi-plate output (#289) with its own box
    and colours; the output's ``bbox_mm`` spans every plate and would misjudge them.
    """
    path = context.model_3mf
    plate = context.plate
    if path is None or plate is None or not path.is_file():
        return None
    count = len(plates_of(path))
    try:
        replate_3mf(path.read_bytes(), plate)
    except PlateFitError as error:
        return count, str(error)
    return count, None


def _plate_fit(context: AnalysisContext, analyzer: Analyzer) -> list[AnalyzerDiagnostic]:
    plate = context.plate
    output = context.output
    assert plate is not None and output is not None
    name = plate.model or "the default plate"
    checked = _multi_plate_problem(context)
    plates = checked[0] if checked is not None else 1
    if plates > 1:
        assert checked is not None
        problem = checked[1]
        over: list[tuple[str, float, float]] = []
    else:
        size = output.bbox_mm.size
        over = list(overshoots(size, plate))
        problem = None if over else fit_problem(size, plate, tower=len(output.colors) > 1)
    if not over and problem is None:
        return []
    evidence = [
        Evidence(label=f"model {axis}", value=round(want, 1), unit="mm", origin="output")
        for axis, want, _ in over
    ] + [
        Evidence(label=f"{name} {axis} limit", value=round(have, 1), unit="mm", origin="plate")
        for axis, _, have in over
    ]
    if problem is not None:
        evidence.append(Evidence(label="placement", value=problem, origin="plate"))
    if plates > 1:
        evidence.append(Evidence(label="plates", value=plates, origin="3mf"))
    message = (
        f"This does not fit {name}: "
        + ", ".join(f"{axis} {want:.1f} mm > {have:.1f} mm" for axis, want, have in over)
        + "."
        if over
        else f"This does not fit {name}: {problem}"
    )
    return [
        analyzer.diagnose(
            message=message,
            why=(
                "X and Y are checked against the area every extruder reaches, Z against "
                "the printer's printable height, and a multi-colour print also needs "
                "room for its prime tower; a multi-plate output is checked plate by "
                "plate. The send refuses the same plates."
            ),
            location=DiagnosticLocation(kind="plate", bbox=output.bbox_mm if plates == 1 else None),
            evidence=evidence,
        )
    ]


PLATE_FIT = Analyzer(
    id="SB4001",
    name="plate-fit",
    title="Does not fit the plate",
    severity="error",
    category="plate",
    description="The model, or model plus prime tower, is larger than the printer's plate.",
    sources=(src.PLATE_PROFILES, src.PRINTABLE_HEIGHT),
    check=_plate_fit,
    needs=frozenset({"output", "plate"}),
)


#: Every bundled analyzer, in id order. SB0001 is reported by the runner, not run.
BUILTIN: tuple[Analyzer, ...] = (
    CRASHED,
    NON_MANIFOLD,
    OPEN_EDGES,
    OVERHANGS,
    SILK_GLOSS,
    SILK_NOZZLE,
    SILK_PLATE,
    LOW_FILAMENT,
    PLATE_FIT,
)
