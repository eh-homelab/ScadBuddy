"""Bambu filament ids (``GFA00``, ``GFL99`` …) to names and materials (#2170).

The printer names the filament a hotend last ran (``nozzle_rack[].filament_id``) and an
AMS tray's spool (``tray_info_idx``) by Bambu Studio's ``filament_id``. These are the ids
of Bambu Studio's system filament profiles, cross-checked against Bambuddy's own
built-in table (``backend/app/api/routes/cloud.py``). An id not listed here is unknown:
:func:`filament_material` then answers ``None`` rather than guessing from its letters.
"""

from __future__ import annotations

from typing import NamedTuple


class FilamentId(NamedTuple):
    #: The profile's name, as Bambu Studio lists it: ``Bambu PLA Basic``.
    name: str
    #: The material family a person would say: ``PLA``, ``PETG``, ``PA-CF``.
    material: str


FILAMENT_IDS: dict[str, FilamentId] = {
    "GFA00": FilamentId("Bambu PLA Basic", "PLA"),
    "GFA01": FilamentId("Bambu PLA Matte", "PLA"),
    "GFA02": FilamentId("Bambu PLA Metal", "PLA"),
    "GFA05": FilamentId("Bambu PLA Silk", "PLA"),
    "GFA06": FilamentId("Bambu PLA Silk+", "PLA"),
    "GFA07": FilamentId("Bambu PLA Marble", "PLA"),
    "GFA08": FilamentId("Bambu PLA Sparkle", "PLA"),
    "GFA09": FilamentId("Bambu PLA Tough", "PLA"),
    "GFA11": FilamentId("Bambu PLA Aero", "PLA"),
    "GFA12": FilamentId("Bambu PLA Glow", "PLA"),
    "GFA13": FilamentId("Bambu PLA Dynamic", "PLA"),
    "GFA15": FilamentId("Bambu PLA Galaxy", "PLA"),
    "GFA16": FilamentId("Bambu PLA Wood", "PLA"),
    "GFA50": FilamentId("Bambu PLA-CF", "PLA-CF"),
    "GFB00": FilamentId("Bambu ABS", "ABS"),
    "GFB01": FilamentId("Bambu ASA", "ASA"),
    "GFB02": FilamentId("Bambu ASA-Aero", "ASA"),
    "GFB50": FilamentId("Bambu ABS-GF", "ABS-GF"),
    "GFB51": FilamentId("Bambu ASA-CF", "ASA-CF"),
    "GFB60": FilamentId("PolyLite ABS", "ABS"),
    "GFB61": FilamentId("PolyLite ASA", "ASA"),
    "GFB98": FilamentId("Generic ASA", "ASA"),
    "GFB99": FilamentId("Generic ABS", "ABS"),
    "GFC00": FilamentId("Bambu PC", "PC"),
    "GFC01": FilamentId("Bambu PC FR", "PC"),
    "GFC99": FilamentId("Generic PC", "PC"),
    "GFG00": FilamentId("Bambu PETG Basic", "PETG"),
    "GFG01": FilamentId("Bambu PETG Translucent", "PETG"),
    "GFG02": FilamentId("Bambu PETG HF", "PETG"),
    "GFG50": FilamentId("Bambu PETG-CF", "PETG-CF"),
    "GFG60": FilamentId("PolyLite PETG", "PETG"),
    "GFG96": FilamentId("Generic PETG HF", "PETG"),
    "GFG97": FilamentId("Generic PCTG", "PCTG"),
    "GFG98": FilamentId("Generic PETG-CF", "PETG-CF"),
    "GFG99": FilamentId("Generic PETG", "PETG"),
    "GFL00": FilamentId("PolyLite PLA", "PLA"),
    "GFL01": FilamentId("PolyTerra PLA", "PLA"),
    "GFL03": FilamentId("eSUN PLA+", "PLA"),
    "GFL04": FilamentId("Overture PLA", "PLA"),
    "GFL05": FilamentId("Overture Matte PLA", "PLA"),
    "GFL06": FilamentId("Fiberon PETG-ESD", "PETG"),
    "GFL50": FilamentId("Fiberon PA6-CF", "PA-CF"),
    "GFL51": FilamentId("Fiberon PA6-GF", "PA-GF"),
    "GFL52": FilamentId("Fiberon PA12-CF", "PA-CF"),
    "GFL53": FilamentId("Fiberon PA612-CF", "PA-CF"),
    "GFL54": FilamentId("Fiberon PET-CF", "PET-CF"),
    "GFL55": FilamentId("Fiberon PETG-rCF", "PETG-CF"),
    "GFL95": FilamentId("Generic PLA High Speed", "PLA"),
    "GFL96": FilamentId("Generic PLA Silk", "PLA"),
    "GFL98": FilamentId("Generic PLA-CF", "PLA-CF"),
    "GFL99": FilamentId("Generic PLA", "PLA"),
    "GFN03": FilamentId("Bambu PA-CF", "PA-CF"),
    "GFN04": FilamentId("Bambu PAHT-CF", "PA-CF"),
    "GFN05": FilamentId("Bambu PA6-CF", "PA-CF"),
    "GFN06": FilamentId("Bambu PPA-CF", "PPA-CF"),
    "GFN08": FilamentId("Bambu PA6-GF", "PA-GF"),
    "GFN96": FilamentId("Generic PPA-GF", "PPA-GF"),
    "GFN97": FilamentId("Generic PPA-CF", "PPA-CF"),
    "GFN98": FilamentId("Generic PA-CF", "PA-CF"),
    "GFN99": FilamentId("Generic PA", "PA"),
    "GFP95": FilamentId("Generic PP-GF", "PP-GF"),
    "GFP96": FilamentId("Generic PP-CF", "PP-CF"),
    "GFP97": FilamentId("Generic PP", "PP"),
    "GFP98": FilamentId("Generic PE-CF", "PE-CF"),
    "GFP99": FilamentId("Generic PE", "PE"),
    "GFR98": FilamentId("Generic PHA", "PHA"),
    "GFR99": FilamentId("Generic EVA", "EVA"),
    "GFS00": FilamentId("Bambu Support W", "Support"),
    "GFS01": FilamentId("Bambu Support G", "Support"),
    "GFS02": FilamentId("Bambu Support For PLA", "Support"),
    "GFS03": FilamentId("Bambu Support For PA/PET", "Support"),
    "GFS04": FilamentId("Bambu PVA", "PVA"),
    "GFS05": FilamentId("Bambu Support For PLA/PETG", "Support"),
    "GFS06": FilamentId("Bambu Support for ABS", "Support"),
    "GFS97": FilamentId("Generic BVOH", "BVOH"),
    "GFS98": FilamentId("Generic HIPS", "HIPS"),
    "GFS99": FilamentId("Generic PVA", "PVA"),
    "GFT01": FilamentId("Bambu PET-CF", "PET-CF"),
    "GFT02": FilamentId("Bambu PPS-CF", "PPS-CF"),
    "GFT97": FilamentId("Generic PPS", "PPS"),
    "GFT98": FilamentId("Generic PPS-CF", "PPS-CF"),
    "GFU00": FilamentId("Bambu TPU 95A HF", "TPU"),
    "GFU01": FilamentId("Bambu TPU 95A", "TPU"),
    "GFU02": FilamentId("Bambu TPU for AMS", "TPU"),
    "GFU98": FilamentId("Generic TPU for AMS", "TPU"),
    "GFU99": FilamentId("Generic TPU", "TPU"),
}


def _known(filament_id: str | None) -> FilamentId | None:
    return FILAMENT_IDS.get((filament_id or "").strip().upper())


def filament_name(filament_id: str | None) -> str | None:
    """``GFA00`` -> ``Bambu PLA Basic``; ``None`` for an empty or unknown id."""
    known = _known(filament_id)
    return known.name if known else None


def filament_material(filament_id: str | None) -> str | None:
    """``GFA00`` -> ``PLA``; ``None`` for an empty or unknown id."""
    known = _known(filament_id)
    return known.material if known else None
