"""Every source the bundled analyzers cite, with the line each one quotes (#284).

Quotes are copied verbatim from the page as it read on the ``accessed`` date, so a
reader can search for them. Bambu Studio's source is pinned to one commit rather
than ``master``, so a quoted line and its line anchor cannot drift apart. Characters a
linter would read as look-alikes (en dash, arrow, degree sign) are escaped.
"""

from __future__ import annotations

from datetime import date

from scadbuddy.analyzers.model import Source

ACCESSED = date(2026, 9, 28)

#: bambulab/BambuStudio ``master`` on 2026-09-28.
BAMBU_STUDIO_COMMIT = "f977235e6d736c4c0b650520ac5a5b72cbfe9244"
_STUDIO = f"https://github.com/bambulab/BambuStudio/blob/{BAMBU_STUDIO_COMMIT}"
_PRINT_CONFIG = f"{_STUDIO}/src/libslic3r/PrintConfig.cpp"
_PROCESS_COMMON = f"{_STUDIO}/resources/profiles/BBL/process/fdm_process_common.json"

#: This repository at the commit the analyzers were written against.
SCADBUDDY_COMMIT = "c635f84c925bac5f256627a8b15d889f8788889b"
_REPO = f"https://github.com/eh-homelab/ScadBuddy/blob/{SCADBUDDY_COMMIT}"

SILK_GUIDE_URL = "https://wiki.bambulab.com/en/x1/manual/printing-with-silk-filaments"
SILK_GUIDE_TITLE = "Bambu PLA Silk / Silk+ Filament Printing Guide | Bambu Lab Wiki"

# --- mesh -------------------------------------------------------------------------

MESH_DEFECT_SEVERITY = Source(
    url="https://wiki.bambulab.com/en/software/bambu-studio/release/release-note-2-7-1",
    title="Bambu Studio V2.7.1 Release Note | Bambu Lab Wiki",
    quote="Non\u2011manifold edges are reported as Error, and open edges as Info.",
    accessed=ACCESSED,
    supports=["SB1001", "SB1002"],
)

FIX_MODEL = Source(
    url="https://wiki.bambulab.com/en/software/bambu-studio/fix-model",
    title="Fix Model | Bambu Lab Wiki",
    quote=(
        "Bambu Studio will automatically repair models to a certain degree, but it may "
        "be necessary to fix the model using a dedicated service."
    ),
    accessed=ACCESSED,
    supports=["SB1001", "SB1002"],
)

# --- overhangs --------------------------------------------------------------------

SUPPORT_THRESHOLD_ANGLE = Source(
    url=f"{_PRINT_CONFIG}#L6005-L6013",
    title="Bambu Studio PrintConfig.cpp: support_threshold_angle",
    quote="Support will be generated for overhangs whose slope angle is below the threshold.",
    accessed=ACCESSED,
    supports=["SB1003", "support_threshold_angle"],
)

PROCESS_COMMON_THRESHOLD = Source(
    url=f"{_PROCESS_COMMON}#L147",
    title="Bambu Studio fdm_process_common.json (every BBL process preset inherits it)",
    quote='"support_threshold_angle": "30",',
    accessed=ACCESSED,
    supports=["SB1003", "support_threshold_angle"],
)

PROCESS_COMMON_SUPPORT = Source(
    url=f"{_PROCESS_COMMON}#L31",
    title="Bambu Studio fdm_process_common.json (every BBL process preset inherits it)",
    quote='"enable_support": "0",',
    accessed=ACCESSED,
    supports=["SB1003", "enable_support"],
)

ENABLE_SUPPORT = Source(
    url=f"{_PRINT_CONFIG}#L5597-L5601",
    title="Bambu Studio PrintConfig.cpp: enable_support",
    quote='def->tooltip = L("Enable support generation.");',
    accessed=ACCESSED,
    supports=["enable_support"],
)

# --- plate ------------------------------------------------------------------------

PRINTABLE_HEIGHT = Source(
    url=f"{_PRINT_CONFIG}#L1106-L1108",
    title="Bambu Studio PrintConfig.cpp: printable_height",
    quote="Maximum printable height which is limited by mechanism of printer",
    accessed=ACCESSED,
    supports=["SB4001"],
)

PLATE_PROFILES = Source(
    url=f"{_REPO}/backend/scadbuddy/render/plate_profiles.py#L1-L10",
    title="ScadBuddy plate_profiles.py, generated from Bambu Studio's machine profiles",
    quote=(
        "Each entry is keyed by the machine profile's ``printer_model`` and carries, in\n"
        "millimetres, the values those profiles declare:"
    ),
    accessed=ACCESSED,
    supports=["SB4001"],
)

# --- silk PLA ---------------------------------------------------------------------

SILK_OUTER_WALL = Source(
    url=SILK_GUIDE_URL,
    title=SILK_GUIDE_TITLE,
    quote=("Go to Process \u2192 Speed and set Outer wall to 40\u201360 mm/s. Try 50 mm/s first."),
    accessed=ACCESSED,
    supports=["SB2001", "outer_wall_speed"],
)

SILK_TEMPERATURE = Source(
    url=SILK_GUIDE_URL,
    title=SILK_GUIDE_TITLE,
    quote=("We recommend setting the Nozzle first layer and other layers to 235\u2103."),
    accessed=ACCESSED,
    supports=["SB2001", "nozzle_temperature", "nozzle_temperature_initial_layer"],
)

SILK_TEMPERATURE_RANGE = Source(
    url=SILK_GUIDE_URL,
    title=SILK_GUIDE_TITLE,
    quote="This temperature is typically about 210\u2103 to 240\u2103.",
    accessed=ACCESSED,
    supports=["SB2001", "nozzle_temperature"],
)

SILK_NOZZLE = Source(
    url=SILK_GUIDE_URL,
    title=SILK_GUIDE_TITLE,
    quote="0.6mm and 0.8mm nozzles are not recommended.",
    accessed=ACCESSED,
    supports=["SB2002"],
)

SILK_PLATE = Source(
    url=SILK_GUIDE_URL,
    title=SILK_GUIDE_TITLE,
    quote="The Cool Plate SuperTack is not recommended.",
    accessed=ACCESSED,
    supports=["SB2003"],
)

OUTER_WALL_SPEED = Source(
    url=f"{_PRINT_CONFIG}#L2245-L2249",
    title="Bambu Studio PrintConfig.cpp: outer_wall_speed",
    quote=(
        "Speed of outer wall which is outermost and visible. "
        "It's used to be slower than inner wall speed to get better quality."
    ),
    accessed=ACCESSED,
    supports=["outer_wall_speed"],
)

NOZZLE_TEMPERATURE = Source(
    url=f"{_PRINT_CONFIG}#L6080-L6083",
    title="Bambu Studio PrintConfig.cpp: nozzle_temperature",
    quote="Nozzle temperature for layers after the initial one",
    accessed=ACCESSED,
    supports=["nozzle_temperature"],
)

NOZZLE_TEMPERATURE_INITIAL = Source(
    url=f"{_PRINT_CONFIG}#L3493-L3496",
    title="Bambu Studio PrintConfig.cpp: nozzle_temperature_initial_layer",
    quote="Nozzle temperature to print initial layer when using this filament",
    accessed=ACCESSED,
    supports=["nozzle_temperature_initial_layer"],
)

# --- ScadBuddy's own print-flow rules ---------------------------------------------

_PRINT_FLOW = f"{_REPO}/docs/superpowers/specs/2026-09-24-print-flow-design.md"

LOW_FILAMENT_RULE = Source(
    url=f"{_PRINT_FLOW}#the-two-warnings",
    title='ScadBuddy print-flow design §4, "The two warnings"',
    quote=(
        "**Not enough filament**: `remaining_g < used_grams \u00d7 copies`, only when the "
        "slice has reported grams."
    ),
    accessed=ACCESSED,
    supports=["SB3002"],
)

ELIGIBILITY_RULE = Source(
    url=f"{_PRINT_FLOW}#the-two-warnings",
    title='ScadBuddy print-flow design §4, "The two warnings"',
    quote=(
        "Whether two filaments can share a plate, whether a printer can run the job, "
        "whether a slot can reach the extruder it slices for — those are Bambuddy's "
        "questions, and its eligibility report (#86) answers them in the same dialog."
    ),
    accessed=ACCESSED,
    supports=["SB5001"],
)

ANALYZER_CRASH = Source(
    url="https://github.com/eh-homelab/ScadBuddy/issues/284",
    title="#284 Print analyzers & fixers",
    quote=("A crashed analyzer is itself a visible diagnostic (`SB0001`), never a silent skip."),
    accessed=ACCESSED,
    supports=["SB0001"],
)
