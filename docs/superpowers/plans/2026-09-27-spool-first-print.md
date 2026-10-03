# Spool-first Print Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the print picker's pipeline step with spool / nozzle / quality / plate choices from which ScadBuddy derives every slicer preset, then slices and queues through Bambuddy.

**Architecture:** A pure `resolver.py` turns the dialog's choices plus Bambuddy's preset catalogue into a `SliceRequest` and queue fields. A `/print/outputs/{id}/choices` route feeds the dialog; `/print/outputs/{id}/run` takes the new request and always slices and queues, reusing `slice_and_queue` (refactored to take presets instead of a `Pipeline`). The frontend `PrintPicker` loses its pipeline list and gains Nozzle, Quality and Plate steps with a Simple/Advanced toggle.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, httpx + respx, pytest, mypy strict, ruff; React + TypeScript + Vite, Vitest, Playwright with msw.

**Spec:** `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` (read it first; §-references below are to it).

## Global Constraints

- Printer model string is `H2C`; Bambu printer preset names are `Bambu Lab H2C <size> nozzle`.
- Nozzle sizes offered: `0.2`, `0.4`, `0.6`, `0.8` (strings, as Bambuddy spells them).
- Tier table is §4.2 verbatim, keyed by **process name** (not id), so it resolves in the cloud tier (ids `GPxxx`) and the standard tier (id == name) alike.
- ScadBuddy sends **no `ams_mapping`** (§6). It sent no `nozzle_rack_choice` either
  until #836 (`2026-10-01-rack-nozzle-selection-design.md`), which supersedes that
  decision.
- Default plate `Textured PEI Plate`; plate strings are `BED_TYPES` in `pipelines.py`.
- US spelling in new UI text and docs ("color"); existing identifiers such as `filament_colours` / `colour` keep their names.
- Every backend change passes `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` in `backend/`.
- Every frontend change passes `pnpm lint && pnpm typecheck && pnpm test && pnpm build` in `frontend/`.
- After any API model/route change: `cd backend && uv run --frozen python -m scadbuddy.tools.export_openapi`, then `cd frontend && pnpm gen:api`.
- Commit with explicit pathspecs only; never `git add -A`.

## Spec amendments found while planning (confirm with the user before Task 3)

1. **Plate-compatibility warnings (§4.4, second paragraph) are dropped from this project.** Bambuddy exposes no filament preset values: `GET /slicer/preset-values?slot=filament` answers `400 "Only the 'process' slot is supported"` (measured 2026-09-27). Asking with `slot=process` and a filament id happens to resolve cloud presets, but returns no plate temperatures for standard-tier presets and does not resolve local ones — relying on it would be building on an accident. The preselect + swap reminder stays.
2. **The one-click send bar (`SendDialog`, `POST /outputs/{id}/send`) and Settings' default pipeline stay pipeline-based** in this project. The spec's scope is the print picker; converting the send bar is a follow-up issue (Task 10 files it).
3. **Printer choice.** Without a pipeline there is no target, so the dialog picks a printer: the remembered one for this model, else `settings.printer_id`, else the first active printer. Only one printer exists today; the control is a plain select.
4. **Plate preselect order:** last print's `bed_type` from `/archives/?printer_id=` → ScadBuddy's remembered `printer_bed_types` (#83) → `Textured PEI Plate`.

## Review Focus

1. **A Bambu spool whose `slicer_filament` is a base id (`GFA00`) and no per-nozzle rows** — must resolve by `slicer_filament_name` + `compatible_printers` to the size-specific Bambu preset, never fall through to Generic. Test in Task 3.
2. **Preset catalogue without the cloud tier** (Bambu Cloud logged out) — tiers and Bambu filament lookups must still resolve through the standard tier. Test in Task 2 and Task 3.
3. **A spool with no preset anywhere for the chosen size** (TPU at 0.2) — must be a slot error that disables Print, not a slice failure at Bambuddy. Test in Task 3 and Task 8.
4. **The printer offline or `/status` failing** while opening the dialog — choices must still open with all four sizes offered and nothing marked installed. Test in Task 4.
5. **No archives for the printer** (new printer, or archives purged) — plate falls back to the remembered plate, then Textured PEI. Test in Task 4.

---

### Task 1: Run the four live checks and record the results (spec §5)

No code. Results decide whether Task 6 is built.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` (§5 — add a "Results" table)

- [ ] **Step 1: Test 1 — create an HF printer preset and slice with it.** Base the setting on Bambu's machine profile, overriding only the flow type:

```bash
B=https://bambuddy.internal.nullreference.io/api/v1
curl -s -X POST $B/local-presets/ -H 'content-type: application/json' -d '{
  "name": "ScadBuddy · H2C 0.4 · L-HF R-Std",
  "preset_type": "printer",
  "setting": {"type": "machine", "name": "ScadBuddy · H2C 0.4 · L-HF R-Std",
              "inherits": "Bambu Lab H2C 0.4 nozzle", "from": "User",
              "default_nozzle_volume_type": ["High Flow", "Standard"]}
}' | tee /tmp/hf-preset.json
```

Upload any small ScadBuddy 3MF to the library (or reuse a library file id from the History page), then slice:

```bash
PRESET_ID=$(python3 -c "import json;print(json.load(open('/tmp/hf-preset.json'))['id'])")
curl -s -X POST $B/library/files/<FILE_ID>/slice -H 'content-type: application/json' -d "{
  \"printer_preset\": {\"source\": \"local\", \"id\": \"$PRESET_ID\"},
  \"process_preset\": {\"source\": \"cloud\", \"id\": \"GP252\"},
  \"filament_presets\": [{\"source\": \"cloud\", \"id\": \"GFSA00_22\"}],
  \"bed_type\": \"Textured PEI Plate\", \"export_3mf\": true}"
```

Poll `GET $B/slice-jobs/<job_id>` until `completed`, download the sliced file (`GET $B/library/files/<result.library_file_id>/download`), and read `Metadata/project_settings.config`:

```bash
unzip -p sliced.3mf Metadata/project_settings.config | python3 -c "
import json,sys; s=json.load(sys.stdin)
print(s.get('nozzle_volume_type'), s.get('default_nozzle_volume_type'), s.get('filament_max_volumetric_speed'))"
```

Pass: the file records `High Flow` for the left extruder. Record the three printed values.

- [ ] **Step 2: Test 2 — ask the user first**, then queue that sliced file with `"manual_start": true` on printer 1 and read the queue item's `waiting_reason` / `error_message` and the printer's HMS messages. Nothing may start printing. Pass: no nozzle-mismatch message (Bambuddy #3136). Cancel the item afterwards (`POST $B/queue/<id>/cancel`).

- [ ] **Step 3: Test 3 — mixed sizes.** Create a second local printer preset inheriting `Bambu Lab H2C 0.4 nozzle` with `"nozzle_diameter": ["0.2", "0.4"]`, slice as in Step 1. Record whether the slice job fails and its exact message.

- [ ] **Step 4: Test 4 — 3DFP preset on HF.** Slice as in Step 1 with the HF preset and a 3DFP filament preset (e.g. local id of `Insignia PLA+/Pro -- Other -- WHITE (3DFP Ks5G96TQn) @H2C`; find it with `curl -s $B/local-presets/ | jq '.filament[]|select(.name|test("Ks5G96TQn"))|{id,name}'`). Read `filament_max_volumetric_speed` in the sliced settings: it must be a two/three-entry list, not an error.

- [ ] **Step 5: Delete the test presets** (`DELETE $B/local-presets/<id>` for each) so Task 6's find-by-name starts clean.

- [ ] **Step 6: Write the results into spec §5** as a table: `# | Result | Measured value | Date`. If test 1 or 2 failed, add one line under §4.1: "HF is Advanced-only and labelled 'Bambuddy may slice as Standard' (test N failed on 2026-..-..)."

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-09-27-spool-first-print-design.md
git commit -m "docs(spec): record the spool-first live checks (§5)"
```

---

### Task 2: Wire shapes and recordings — archives, rack, H2C presets

**Files:**
- Modify: `backend/scadbuddy/bambuddy/models.py` (add `Archive`, `LocalPresetCreate`)
- Modify: `backend/scadbuddy/bambuddy/client.py` (add `archives`, `create_local_preset`)
- Create: `backend/tests/bambuddy/recordings/archives.json`
- Create: `backend/tests/bambuddy/recordings/printer-status-rack.json`
- Create: `backend/tests/bambuddy/recordings/slicer-presets-h2c.json`
- Modify: `backend/tests/bambuddy/recordings/README.md`
- Test: `backend/tests/bambuddy/test_client.py`

**Interfaces:**
- Produces: `Archive(id: int, printer_id: int | None, status: str | None, bed_type: str | None, started_at: datetime | None, completed_at: datetime | None, created_at: datetime | None)`; `BambuddyClient.archives(*, printer_id: int, limit: int = 20) -> list[Archive]`; `LocalPresetCreate(name: str, preset_type: str, setting: dict[str, Any])`; `BambuddyClient.create_local_preset(preset: LocalPresetCreate) -> LocalPreset`.

- [ ] **Step 1: Record the three bodies** (GETs only; scrub serials):

```bash
cd backend/tests/bambuddy/recordings
B=https://bambuddy.internal.nullreference.io/api/v1
curl -s "$B/archives/?printer_id=1&limit=5" | python3 -c "
import json,sys; rows=json.load(sys.stdin)
keep=('id','printer_id','status','bed_type','started_at','completed_at','created_at','print_name')
json.dump([{k:r.get(k) for k in keep} for r in rows], sys.stdout, indent=2)" > archives.json
curl -s "$B/printers/1/status" | python3 -c "
import json,sys; d=json.load(sys.stdin)
for r in d.get('nozzle_rack',[]): r['serial_number']='REDACTED'
json.dump(d, sys.stdout, indent=2)" > printer-status-rack.json
curl -s "$B/slicer/presets" | python3 -c "
import json,sys; d=json.load(sys.stdin)
def h2c(rows): return [r for r in rows if 'H2C' in (r.get('name') or '')]
out={t:{k:h2c(d[t][k]) for k in ('printer','process','filament')} for t in ('cloud','standard')}
out['local']={'printer':[],'process':[],'filament':[]}; out['orca_cloud']=out['local']
json.dump(out, sys.stdout, indent=2)" > slicer-presets-h2c.json
```

Add a dated "Added for spool-first print (2026-09-27)" table to `README.md` listing the three files and their source routes.

- [ ] **Step 2: Write the failing tests** in `backend/tests/bambuddy/test_client.py`:

```python
@respx.mock
async def test_archives_are_read_for_one_printer(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives.json"))
    )
    rows = await bambuddy.archives(printer_id=1, limit=5)
    assert route.calls.last.request.url.params["printer_id"] == "1"
    assert route.calls.last.request.url.params["limit"] == "5"
    assert rows and all(row.printer_id == 1 for row in rows)
    assert rows[0].bed_type == "Textured PEI Plate"


@respx.mock
async def test_creating_a_local_preset_posts_name_type_and_setting(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(
            200,
            json={"id": 91, "name": "X", "preset_type": "printer", "source": "manual"},
        )
    )
    made = await bambuddy.create_local_preset(
        LocalPresetCreate(name="X", preset_type="printer", setting={"inherits": "Y"})
    )
    assert made.id == 91
    assert json.loads(route.calls.last.request.content) == {
        "name": "X",
        "preset_type": "printer",
        "setting": {"inherits": "Y"},
    }


def test_the_rack_recording_parses_every_slot() -> None:
    status = PrinterStatus.model_validate(recording("printer-status-rack.json"))
    assert {slot.nozzle_diameter for slot in status.nozzle_rack} >= {"0.2", "0.4"}
    assert any(slot.nozzle_type.startswith("HH") for slot in status.nozzle_rack)
```

(`API`, `recording` and the `bambuddy` fixture already exist in this file's imports / `tests/bambuddy/conftest.py`; add `LocalPresetCreate`, `PrinterStatus`, `json` to the imports.)

- [ ] **Step 3: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_client.py -k "archives or local_preset or rack" -v`
Expected: FAIL — `ImportError: cannot import name 'LocalPresetCreate'`.

- [ ] **Step 4: Implement.** In `models.py`, after `LocalPresetCatalogue`:

```python
class LocalPresetCreate(BambuddyModel):
    """``POST /api/v1/local-presets/`` — a preset created by hand rather than imported.

    ``setting`` is the preset JSON as Bambu Studio would store it; Bambuddy keeps it
    verbatim and resolves ``inherits`` at slice time.
    """

    name: str
    preset_type: Literal["filament", "printer", "process"]
    setting: dict[str, Any]
```

and after `Printer`:

```python
class Archive(BambuddyModel):
    """A row of ``GET /api/v1/archives/`` — one past print (or upload).

    ``bed_type`` is the plate the file was sliced for. An upload that never printed has
    ``printer_id: null``; only rows with a printer are evidence of what was on its bed.
    """

    id: int
    printer_id: int | None = None
    status: str | None = None
    bed_type: str | None = None
    print_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
```

In `client.py`, beside `local_presets`:

```python
    async def create_local_preset(self, preset: LocalPresetCreate) -> LocalPreset:
        response = await self._send(
            "POST", "/api/v1/local-presets/", json=preset.model_dump(mode="json")
        )
        return LocalPreset.model_validate(response.json())

    async def archives(self, *, printer_id: int, limit: int = 20) -> list[Archive]:
        """This printer's recent archives. Bambuddy's order is not documented, so callers
        sort; ``limit`` keeps the read small."""
        response = await self._send(
            "GET", "/api/v1/archives/", params={"printer_id": printer_id, "limit": limit}
        )
        return [Archive.model_validate(row) for row in self._rows(response, what="archives")]
```

Match `_send`'s real keyword names by reading its signature at `client.py:128` first; adjust `json=`/`params=` to it if it differs.

- [ ] **Step 5: Run the tests** — same command. Expected: PASS. Then the full backend gate from Global Constraints.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/bambuddy/models.py backend/scadbuddy/bambuddy/client.py \
  backend/tests/bambuddy/test_client.py backend/tests/bambuddy/recordings/archives.json \
  backend/tests/bambuddy/recordings/printer-status-rack.json \
  backend/tests/bambuddy/recordings/slicer-presets-h2c.json \
  backend/tests/bambuddy/recordings/README.md
git commit -m "feat(bambuddy): archives, local-preset create, and H2C rack/preset recordings"
```

---

### Task 3: The resolver (spec §4)

**Files:**
- Create: `backend/scadbuddy/bambuddy/resolver.py`
- Test: `backend/tests/bambuddy/test_resolver.py`

**Interfaces:**
- Consumes: `PresetChoice` and `_catalogue` (`pipelines.py`); `SpoolOption`, `SlotNeed`, `FilamentPlan`, `FilamentOptions`, `queue_filaments`, `FilamentWarning` (`filaments.py`); `PresetRef`, `SpoolFilamentPreset`, `SliceRequest` (`models.py`).
- Produces (used by Tasks 4, 5, 6):

```python
NozzleSize = Literal["0.2", "0.4", "0.6", "0.8"]
FlowType = Literal["standard", "high_flow"]
Tier = Literal["fine", "standard", "draft"]

class NozzleChoice(BaseModel):          # one per extruder, index 0 then 1
    size: NozzleSize
    flow: FlowType = "standard"

class PrintChoices(BaseModel):
    nozzles: list[NozzleChoice]          # length 1 or 2
    tier: Tier | None = "standard"
    process_name: str | None = None      # Advanced: wins over tier
    bed_type: str = "Textured PEI Plate"
    filament_overrides: dict[int, PresetRef] = {}   # Advanced, by slot_id
    allow_mixed_sizes: bool = False

class Resolved(BaseModel):
    printer_preset_name: str             # e.g. "Bambu Lab H2C 0.2 nozzle" or the HF preset name
    printer_preset: PresetRef | None     # None when the HF preset must be created (Task 6)
    process_preset: PresetRef | None
    filament_presets: list[PresetRef]
    filament_colours: list[str]
    bed_type: str
    warnings: list[FilamentWarning]
    errors: list[FilamentWarning]        # non-empty => Print disabled / run refused (422)

TIERS: dict[NozzleSize, dict[Tier, str]]   # §4.2, process names
def printer_preset_name(nozzles: list[NozzleChoice]) -> str
def resolve(options: FilamentOptions, plan: FilamentPlan, choices: PrintChoices,
            catalogue: _Catalogue, spool_presets: dict[int, list[SpoolFilamentPreset]]) -> Resolved
```

`FilamentWarning.kind` gains `"mixed-sizes"` and `"no-process"`; `WarningKind` in `filaments.py` is extended (the frontend schema regenerates in Task 7).

- [ ] **Step 1: Write the failing tests.** Build catalogues by hand so each test states exactly what Bambuddy holds:

```python
"""Spec §4 — the resolver, from hand-built catalogues and the H2C recording."""

from __future__ import annotations

import pytest

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, SlotNeed, SpoolOption
from scadbuddy.bambuddy.models import PresetCatalogue, PresetRef, SlotChoice, SpoolFilamentPreset
from scadbuddy.bambuddy.pipelines import PresetChoice, _Catalogue, _choice
from scadbuddy.bambuddy.resolver import (
    TIERS,
    NozzleChoice,
    PrintChoices,
    printer_preset_name,
    resolve,
)
from tests.bambuddy.conftest import recording

H2C = "Bambu Lab H2C {} nozzle"


def row(source: str, id_: str, name: str, *sizes: str, kind: str | None = None) -> PresetChoice:
    return PresetChoice(
        ref=PresetRef(source=source, id=id_),  # type: ignore[arg-type]
        name=name,
        filament_type=kind,
        compatible_printers=[H2C.format(size) for size in sizes],
    )


def recorded() -> _Catalogue:
    catalogue = PresetCatalogue.model_validate(recording("slicer-presets-h2c.json"))
    tiers = (catalogue.cloud, catalogue.standard)
    return _Catalogue(
        printer=[_choice(p) for tier in tiers for p in tier.printer],
        process=[_choice(p) for tier in tiers for p in tier.process],
        filament=[_choice(p) for tier in tiers for p in tier.filament],
    )


def without_cloud() -> _Catalogue:
    whole = recorded()
    keep = lambda rows: [r for r in rows if r.ref.source != "cloud"]  # noqa: E731
    return _Catalogue(keep(whole.printer), keep(whole.process), keep(whole.filament))


def spool(spool_id: int, material: str, preset: str | None, name: str | None) -> SpoolOption:
    return SpoolOption(
        spool_id=spool_id, material=material, colour="#112233",
        slicer_filament=preset, slicer_filament_name=name,
    )


def options(*spools: SpoolOption) -> FilamentOptions:
    return FilamentOptions(
        library_file_id=41,
        slots=[SlotNeed(slot_id=i + 1, colour="#FFFFFF") for i in range(len(spools))],
        spools=list(spools),
    )


def plan(*spool_ids: int) -> FilamentPlan:
    return FilamentPlan(
        slots=[SlotChoice(slot_id=i + 1, spool_id=s) for i, s in enumerate(spool_ids)]
    )


BASIC = spool(1, "PLA", "GFA00", "Bambu PLA Basic")
PETG = spool(2, "PETG", "GFG00", "Bambu PETG Basic")
TPU = spool(3, "TPU", None, None)
THIRD_PARTY = spool(4, "PLA", "51", "Insignia PLA @H2C")


@pytest.mark.parametrize("size", ["0.2", "0.4", "0.6", "0.8"])
@pytest.mark.parametrize("tier", ["fine", "standard", "draft"])
def test_every_tier_names_a_process_bambu_ships_for_that_size(size: str, tier: str) -> None:
    catalogue = recorded()
    name = TIERS[size][tier]  # type: ignore[index]
    hits = [p for p in catalogue.process if p.name == name]
    assert hits, f"{name} is not in the recorded H2C catalogue"
    assert all(H2C.format(size) in p.compatible_printers for p in hits)


def test_the_0_2_fine_tier_is_the_0_08_high_quality_process() -> None:
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")], tier="fine"),
        recorded(), {},
    )
    assert resolved.process_preset == PresetRef(source="cloud", id="GP243")
    assert resolved.printer_preset == PresetRef(source="cloud", id="GM042")
    assert resolved.errors == []


def test_a_bambu_spool_resolves_to_its_size_specific_preset_by_name() -> None:
    """Review focus 1: ``GFA00`` is a base id; the 0.2 profile is found by name."""
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]), recorded(), {},
    )
    assert resolved.filament_presets == [PresetRef(source="cloud", id="GFSA00_23")]


def test_without_the_cloud_tier_everything_resolves_through_standard() -> None:
    """Review focus 2: Bambu Cloud logged out."""
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")], tier="fine"),
        without_cloud(), {},
    )
    assert resolved.printer_preset == PresetRef(
        source="standard", id="Bambu Lab H2C 0.2 nozzle"
    )
    assert resolved.process_preset is not None
    assert resolved.process_preset.id == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert resolved.filament_presets[0].id == "Bambu PLA Basic @BBL H2C 0.2 nozzle"


def test_a_spools_own_per_nozzle_row_wins_over_everything_but_an_override() -> None:
    catalogue = recorded()
    catalogue.filament.append(row("local", "34", "Insignia PLA @H2C 0.2n", "0.2", kind="PLA"))
    resolved = resolve(
        options(THIRD_PARTY), plan(4),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]), catalogue,
        {4: [SpoolFilamentPreset(printer_model="H2C", nozzle_diameter="0.2",
                                 slicer_filament="34")]},
    )
    assert resolved.filament_presets == [PresetRef(source="local", id="34")]


def test_an_advanced_override_wins() -> None:
    override = PresetRef(source="cloud", id="GFSL99_22")
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")], filament_overrides={1: override}),
        recorded(), {},
    )
    assert resolved.filament_presets == [override]


def test_with_nothing_of_its_own_a_spool_falls_back_to_generic_for_its_material() -> None:
    resolved = resolve(
        options(spool(5, "PETG", None, None)), plan(5),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]), recorded(), {},
    )
    assert resolved.filament_presets[0].id in {
        "GFSG99_22", "Generic PETG @BBL H2C 0.2 nozzle",
    }
    assert [w.kind for w in resolved.warnings] == ["no-preset"]


def test_tpu_at_0_2_is_a_slot_error_not_a_guess() -> None:
    """Review focus 3."""
    resolved = resolve(
        options(TPU), plan(3),
        PrintChoices(nozzles=[NozzleChoice(size="0.2")]), recorded(), {},
    )
    assert [(e.kind, e.slot_id) for e in resolved.errors] == [("no-preset", 1)]


def test_mixed_sizes_are_an_error_without_the_override_and_a_warning_with_it() -> None:
    nozzles = [NozzleChoice(size="0.2"), NozzleChoice(size="0.4")]
    refused = resolve(options(BASIC), plan(1), PrintChoices(nozzles=nozzles), recorded(), {})
    assert [e.kind for e in refused.errors] == ["mixed-sizes"]
    allowed = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=nozzles, allow_mixed_sizes=True), recorded(), {},
    )
    assert "mixed-sizes" in [w.kind for w in allowed.warnings]


@pytest.mark.parametrize(
    ("flows", "expected"),
    [
        (("standard", "standard"), "Bambu Lab H2C 0.4 nozzle"),
        (("high_flow", "standard"), "ScadBuddy · H2C 0.4 · L-HF R-Std"),
        (("standard", "high_flow"), "ScadBuddy · H2C 0.4 · L-Std R-HF"),
        (("high_flow", "high_flow"), "ScadBuddy · H2C 0.4 · L-HF R-HF"),
    ],
)
def test_hf_on_either_side_names_the_scadbuddy_printer_preset(
    flows: tuple[str, str], expected: str
) -> None:
    nozzles = [NozzleChoice(size="0.4", flow=f) for f in flows]  # type: ignore[arg-type]
    assert printer_preset_name(nozzles) == expected


def test_an_hf_preset_bambuddy_does_not_hold_yet_resolves_to_none_for_task_6() -> None:
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4", flow="high_flow"),
                              NozzleChoice(size="0.4")]),
        recorded(), {},
    )
    assert resolved.printer_preset is None
    assert resolved.printer_preset_name == "ScadBuddy · H2C 0.4 · L-HF R-Std"


def test_an_advanced_process_name_wins_over_the_tier() -> None:
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4")], tier="standard",
                     process_name="0.08mm High Quality @BBL H2C"),
        recorded(), {},
    )
    assert resolved.process_preset == PresetRef(source="cloud", id="GP244")


def test_a_process_for_another_size_is_refused() -> None:
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4")],
                     process_name="0.08mm High Quality @BBL H2C 0.2 nozzle"),
        recorded(), {},
    )
    assert [e.kind for e in resolved.errors] == ["no-process"]


def test_colours_and_bed_type_pass_through() -> None:
    resolved = resolve(
        options(BASIC), plan(1),
        PrintChoices(nozzles=[NozzleChoice(size="0.4")], bed_type="Cool Plate"),
        recorded(), {},
    )
    assert resolved.filament_colours == ["#112233"]
    assert resolved.bed_type == "Cool Plate"
```

Check the recorded Generic PETG 0.2 id before trusting the test's set: `jq '.cloud.filament[]|select(.name=="Generic PETG @BBL H2C 0.2 nozzle").id' backend/tests/bambuddy/recordings/slicer-presets-h2c.json`, and pin the exact id in the assertion.

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scadbuddy.bambuddy.resolver'`.

- [ ] **Step 3: Implement `resolver.py`**

```python
"""Spool-first printing (spec 2026-09-27 §4): choices in, every slicer preset out.

A pure function. Callers read Bambuddy (catalogue, spools, per-spool presets) and pass
it in, so nothing here makes a network call and every rule is tested from recordings.

Presets are matched by **name and compatible printer**, never by id: the cloud tier
spells an id ``GP243`` and the standard tier spells the same preset by its name, and
either tier may be the only one present (Bambu Cloud logged out).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, FilamentWarning, _label
from scadbuddy.bambuddy.models import PresetRef, SpoolFilamentPreset
from scadbuddy.bambuddy.pipelines import PresetChoice, _Catalogue

NozzleSize = Literal["0.2", "0.4", "0.6", "0.8"]
FlowType = Literal["standard", "high_flow"]
Tier = Literal["fine", "standard", "draft"]

PRINTER_MODEL = "H2C"
DEFAULT_BED = "Textured PEI Plate"

#: Spec §4.2. Bambu's own default for a size is Standard on 0.2/0.4 and Draft on
#: 0.6/0.8, where it is already the thickest profile Bambu ships.
TIERS: dict[str, dict[str, str]] = {
    "0.2": {
        "fine": "0.08mm High Quality @BBL H2C 0.2 nozzle",
        "standard": "0.10mm Standard @BBL H2C 0.2 nozzle",
        "draft": "0.12mm Balanced Quality @BBL H2C 0.2 nozzle",
    },
    "0.4": {
        "fine": "0.12mm High Quality @BBL H2C",
        "standard": "0.20mm Standard @BBL H2C",
        "draft": "0.24mm Standard @BBL H2C",
    },
    "0.6": {
        "fine": "0.18mm Balanced Quality @BBL H2C 0.6 nozzle",
        "standard": "0.24mm Balanced Quality @BBL H2C 0.6 nozzle",
        "draft": "0.30mm Standard @BBL H2C 0.6 nozzle",
    },
    "0.8": {
        "fine": "0.24mm Balanced Quality @BBL H2C 0.8 nozzle",
        "standard": "0.32mm Balanced Quality @BBL H2C 0.8 nozzle",
        "draft": "0.40mm Standard @BBL H2C 0.8 nozzle",
    },
}

#: Source order when the same preset is in several tiers: the cloud id is what every
#: existing pipeline and the filament intake use.
_SOURCE_ORDER = {"cloud": 0, "local": 1, "standard": 2, "orca_cloud": 3}


class NozzleChoice(BaseModel):
    size: NozzleSize
    flow: FlowType = "standard"


class PrintChoices(BaseModel):
    nozzles: list[NozzleChoice] = Field(min_length=1, max_length=2)
    tier: Tier | None = "standard"
    process_name: str | None = None
    bed_type: str = Field(default=DEFAULT_BED, max_length=64)
    filament_overrides: dict[int, PresetRef] = Field(default_factory=dict)
    allow_mixed_sizes: bool = False


class Resolved(BaseModel):
    printer_preset_name: str
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
    filament_colours: list[str] = Field(default_factory=list)
    bed_type: str = DEFAULT_BED
    warnings: list[FilamentWarning] = Field(default_factory=list)
    errors: list[FilamentWarning] = Field(default_factory=list)


def _bambu_printer(size: str) -> str:
    return f"Bambu Lab {PRINTER_MODEL} {size} nozzle"


def printer_preset_name(nozzles: list[NozzleChoice]) -> str:
    """Bambu's own printer preset, or ScadBuddy's HF variant of it (spec §4.1)."""
    size = nozzles[0].size
    if all(nozzle.flow == "standard" for nozzle in nozzles):
        return _bambu_printer(size)
    sides = [("HF" if nozzle.flow == "high_flow" else "Std") for nozzle in nozzles]
    left, right = (sides + sides)[:2]
    return f"ScadBuddy · {PRINTER_MODEL} {size} · L-{left} R-{right}"


def _best(rows: list[PresetChoice]) -> PresetRef | None:
    if not rows:
        return None
    return min(rows, key=lambda row: _SOURCE_ORDER.get(row.ref.source, 9)).ref


def _named(rows: list[PresetChoice], name: str, printer: str | None = None) -> PresetRef | None:
    return _best(
        [
            row
            for row in rows
            if row.name == name and (printer is None or printer in row.compatible_printers)
        ]
    )


def _fits(rows: list[PresetChoice], prefix: str, printer: str) -> PresetRef | None:
    """The preset whose name is ``prefix`` or ``prefix + " <size> nozzle"`` and which
    declares ``printer`` — Bambu names its 0.4 (or 0.6/0.8) profile without a suffix."""
    return _best(
        [
            row
            for row in rows
            if (row.name == prefix or row.name.startswith(f"{prefix} "))
            and printer in row.compatible_printers
        ]
    )


def resolve(
    options: FilamentOptions,
    plan: FilamentPlan,
    choices: PrintChoices,
    catalogue: _Catalogue,
    spool_presets: dict[int, list[SpoolFilamentPreset]],
) -> Resolved:
    warnings: list[FilamentWarning] = []
    errors: list[FilamentWarning] = []
    sizes = {nozzle.size for nozzle in choices.nozzles}
    size = choices.nozzles[0].size
    if len(sizes) > 1:
        mixed = FilamentWarning(
            kind="mixed-sizes",
            message=(
                "The two nozzles are different sizes. The firmware may refuse this print."
            ),
        )
        (warnings if choices.allow_mixed_sizes else errors).append(mixed)

    printer = _bambu_printer(size)
    name = printer_preset_name(choices.nozzles)
    printer_ref = _named(catalogue.printer, name)

    process_name = choices.process_name or TIERS[size][choices.tier or "standard"]
    process_ref = _named(catalogue.process, process_name, printer)
    if process_ref is None:
        errors.append(
            FilamentWarning(
                kind="no-process",
                message=f"{process_name!r} is not a process Bambuddy has for a {size} mm nozzle.",
            )
        )

    by_id = {option.spool_id: option for option in options.spools}
    presets: list[PresetRef] = []
    colours: list[str] = []
    for slot in options.slots:
        option = by_id.get(plan.spool_for(slot.slot_id) or -1)
        colours.append((option.colour if option else slot.colour) or "#FFFFFF")
        ref = choices.filament_overrides.get(slot.slot_id)
        if ref is None and option is not None:
            own = [
                row.slicer_filament
                for row in spool_presets.get(option.spool_id, [])
                if row.printer_model == PRINTER_MODEL and row.nozzle_diameter == size
            ]
            ref = next(
                (
                    choice.ref
                    for preset_id in own
                    for choice in catalogue.filament
                    if choice.ref.id == preset_id
                ),
                None,
            )
            if ref is None and option.slicer_filament_name:
                ref = _fits(catalogue.filament, f"{option.slicer_filament_name} @BBL H2C", printer)
            if ref is None:
                ref = _fits(catalogue.filament, f"Generic {option.material} @BBL H2C", printer)
                if ref is not None:
                    warnings.append(
                        FilamentWarning(
                            kind="no-preset",
                            slot_id=slot.slot_id,
                            message=(
                                f"{_label(option)} has no {size} mm preset of its own, so "
                                f"Bambu's Generic {option.material} is used."
                            ),
                        )
                    )
        if ref is None:
            what = _label(option) if option else f"Slot {slot.slot_id}"
            errors.append(
                FilamentWarning(
                    kind="no-preset",
                    slot_id=slot.slot_id,
                    message=(
                        f"{what} has no slicer preset for a {size} mm nozzle. Pick one under "
                        "Advanced."
                    ),
                )
            )
            continue
        presets.append(ref)

    return Resolved(
        printer_preset_name=name,
        printer_preset=printer_ref,
        process_preset=process_ref,
        filament_presets=presets,
        filament_colours=colours,
        bed_type=choices.bed_type,
        warnings=warnings,
        errors=errors,
    )
```

Extend `WarningKind` in `filaments.py`:

```python
WarningKind = Literal[
    "not-loaded", "low-filament", "no-choice", "no-preset", "no-fan-out",
    "nozzle-mismatch", "mixed-sizes", "no-process", "not-installed", "plate-differs",
]
```

(`not-installed` and `plate-differs` are produced in Task 4.) `_label` and `_Catalogue` are module-private today; importing them across modules is acceptable within the package — if ruff's private-import rule fires, rename them to `label` / `Catalogue` and update their existing call sites in the same commit.

- [ ] **Step 4: Run the resolver tests** — same command. Expected: PASS. Then the full backend gate.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/resolver.py backend/scadbuddy/bambuddy/filaments.py \
  backend/tests/bambuddy/test_resolver.py
git commit -m "feat(print): resolver derives printer, process and filament presets from choices"
```

---

### Task 4: The choices route (`GET /print/outputs/{id}/choices`)

**Files:**
- Create: `backend/scadbuddy/bambuddy/choices.py`
- Modify: `backend/scadbuddy/api/printing.py` (add route)
- Test: `backend/tests/bambuddy/test_choices.py`, `backend/tests/api/test_print_choices.py`

**Interfaces:**
- Consumes: `TIERS`, `NozzleSize` (Task 3); `BambuddyClient.archives`, `printer_status`, `printers` (Task 2 / existing); `filament_options_for_output` (existing, `pipelines.py`).
- Produces:

```python
class InstalledNozzle(BaseModel):
    size: str; flow: FlowType; count: int

class TierOption(BaseModel):
    tier: Tier; process_name: str

class ChoicesView(BaseModel):
    printer_id: int | None
    printers: list[Printer]
    nozzle_sizes: list[str]                     # always ["0.2","0.4","0.6","0.8"]
    installed: list[InstalledNozzle]            # empty when status unreadable
    tiers: dict[str, list[TierOption]]          # by size
    processes: dict[str, list[str]]             # Advanced: every compatible process name, by size
    bed_types: list[str]
    last_bed_type: str | None                   # from archives
    bed_type: str                               # the preselect (amendment 4)
    filaments: FilamentOptions                  # unchanged filament step payload

def installed_nozzles(status: PrinterStatus | None) -> list[InstalledNozzle]
def last_bed_type(archives: list[Archive]) -> str | None
def plate_warning(chosen: str, last: str | None, printer_name: str | None) -> FilamentWarning | None
def nozzle_warning(size: str, installed: list[InstalledNozzle]) -> FilamentWarning | None
async def choices_for_output(client, store, meta, settings, *, printer_id: int | None) -> ChoicesView
```

- [ ] **Step 1: Write the failing unit tests** (`tests/bambuddy/test_choices.py`):

```python
from datetime import UTC, datetime

from scadbuddy.bambuddy.choices import (
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_warning,
)
from scadbuddy.bambuddy.models import Archive, PrinterStatus
from tests.bambuddy.conftest import recording


def test_the_rack_is_counted_by_size_and_flow() -> None:
    status = PrinterStatus.model_validate(recording("printer-status-rack.json"))
    found = {(n.size, n.flow): n.count for n in installed_nozzles(status)}
    assert found[("0.2", "standard")] >= 1
    assert found[("0.4", "high_flow")] >= 1


def test_an_unreadable_status_installs_nothing() -> None:
    """Review focus 4."""
    assert installed_nozzles(None) == []


def test_the_newest_print_that_ran_names_the_plate_and_uploads_do_not_count() -> None:
    def at(hour: int) -> datetime:
        return datetime(2026, 9, 27, hour, tzinfo=UTC)

    rows = [
        Archive(id=1, printer_id=1, status="completed", bed_type="Cool Plate", started_at=at(1)),
        Archive(id=2, printer_id=1, status="cancelled", bed_type="Textured PEI Plate",
                started_at=at(5)),
        Archive(id=3, printer_id=None, status="archived", bed_type="Engineering Plate",
                created_at=at(9)),
    ]
    assert last_bed_type(rows) == "Textured PEI Plate"


def test_no_archives_names_no_plate() -> None:
    """Review focus 5."""
    assert last_bed_type([]) is None


def test_a_different_plate_is_a_swap_reminder_and_the_same_one_is_not() -> None:
    warning = plate_warning("Cool Plate", "Textured PEI Plate", "3DP-31B-598")
    assert warning is not None and warning.kind == "plate-differs"
    assert "Swap to Cool Plate" in warning.message
    assert plate_warning("Textured PEI Plate", "Textured PEI Plate", "x") is None
    assert plate_warning("Cool Plate", None, "x") is None


def test_a_size_not_in_the_rack_warns() -> None:
    status = PrinterStatus.model_validate(recording("printer-status-rack.json"))
    installed = installed_nozzles(status)
    assert nozzle_warning("0.8", installed) is not None
    assert nozzle_warning("0.4", installed) is None
    assert nozzle_warning("0.8", []) is None  # nothing known, nothing claimed
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_choices.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scadbuddy.bambuddy.choices'`.

- [ ] **Step 3: Implement `choices.py`**

```python
"""What the spool-first print dialog offers (spec §2–§3), read from Bambuddy once."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentWarning
from scadbuddy.bambuddy.models import Archive, Printer, PrinterStatus
from scadbuddy.bambuddy.pipelines import BED_TYPES, _catalogue, filament_options_for_output
from scadbuddy.bambuddy.resolver import DEFAULT_BED, TIERS, FlowType, Tier
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

SIZES = ["0.2", "0.4", "0.6", "0.8"]
_RAN = {"completed", "cancelled", "failed"}


class InstalledNozzle(BaseModel):
    size: str
    flow: FlowType
    count: int


class TierOption(BaseModel):
    tier: Tier
    process_name: str


class ChoicesView(BaseModel):
    printer_id: int | None = None
    printers: list[Printer] = Field(default_factory=list)
    nozzle_sizes: list[str] = Field(default_factory=lambda: list(SIZES))
    installed: list[InstalledNozzle] = Field(default_factory=list)
    tiers: dict[str, list[TierOption]] = Field(default_factory=dict)
    processes: dict[str, list[str]] = Field(default_factory=dict)
    bed_types: list[str] = Field(default_factory=lambda: list(BED_TYPES))
    last_bed_type: str | None = None
    bed_type: str = DEFAULT_BED
    filaments: FilamentOptions


def installed_nozzles(status: PrinterStatus | None) -> list[InstalledNozzle]:
    """Rack slots by size and flow. The second letter of ``nozzle_type`` is the flow
    (``HH01`` high flow, ``HS01`` standard) — inferred from the codes present (spec §3)."""
    if status is None:
        return []
    counts: Counter[tuple[str, FlowType]] = Counter()
    for slot in status.nozzle_rack:
        if not slot.nozzle_diameter or len(slot.nozzle_type) < 2:
            continue
        flow: FlowType = "high_flow" if slot.nozzle_type[1] == "H" else "standard"
        counts[(slot.nozzle_diameter, flow)] += 1
    return [
        InstalledNozzle(size=size, flow=flow, count=count)
        for (size, flow), count in sorted(counts.items())
    ]


def last_bed_type(archives: list[Archive]) -> str | None:
    ran = [row for row in archives if row.printer_id is not None and row.status in _RAN]

    def when(row: Archive) -> datetime:
        return row.started_at or row.completed_at or row.created_at or datetime.min

    newest = max(ran, key=when, default=None)
    return newest.bed_type if newest else None


def plate_warning(chosen: str, last: str | None, printer_name: str | None) -> FilamentWarning | None:
    if last is None or chosen == last:
        return None
    return FilamentWarning(
        kind="plate-differs",
        message=(
            f"The {printer_name or 'printer'}'s last print used {last}. "
            f"Swap to {chosen} before this starts."
        ),
    )


def nozzle_warning(size: str, installed: list[InstalledNozzle]) -> FilamentWarning | None:
    if not installed or any(nozzle.size == size for nozzle in installed):
        return None
    return FilamentWarning(
        kind="not-installed",
        message=f"No {size} mm nozzle is installed. Install one before this prints.",
    )


async def choices_for_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None,
) -> ChoicesView:
    printers = [row for row in await client.printers() if row.is_active]
    remembered = settings.model_print_choices.get(meta.slug)
    printer_id = (
        printer_id
        or (remembered.printer_id if remembered else None)
        or settings.printer_id
        or (printers[0].id if printers else None)
    )
    status: PrinterStatus | None = None
    archives: list[Archive] = []
    if printer_id is not None:
        try:
            status = await client.printer_status(printer_id)
        except (ApiError, ValueError):
            logger.info("printer status unreadable; offering every nozzle size unmarked")
        try:
            archives = await client.archives(printer_id=printer_id)
        except (ApiError, ValueError):
            logger.info("archives unreadable; the plate falls back to the remembered one")
    catalogue = await _catalogue(client)
    processes = {
        size: sorted(
            {
                row.name
                for row in catalogue.process
                if f"Bambu Lab H2C {size} nozzle" in row.compatible_printers
            }
        )
        for size in SIZES
    }
    last = last_bed_type(archives)
    bed = (
        last
        or (settings.printer_bed_types.get(str(printer_id)) if printer_id is not None else None)
        or DEFAULT_BED
    )
    filaments = await filament_options_for_output(
        client, store, meta, settings, printer_id=printer_id
    )
    return ChoicesView(
        printer_id=printer_id,
        printers=printers,
        installed=installed_nozzles(status),
        tiers={
            size: [TierOption(tier=tier, process_name=name) for tier, name in names.items()]  # type: ignore[arg-type]
            for size, names in TIERS.items()
        },
        processes=processes,
        last_bed_type=last,
        bed_type=bed,
        filaments=filaments,
    )
```

Check `ModelPrintChoices` has `printer_id` (`settings_store.py:33`) and `printer_bed_types` is keyed by `str(printer_id)` (it is, per `put_printer_bed_type`).

- [ ] **Step 4: Add the route** in `api/printing.py`, beside `get_filaments`:

```python
@router.get(
    "/outputs/{output_id}/choices",
    response_model=ChoicesView,
    summary="What the print dialog offers for this output",
)
async def get_choices(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """Printers, installed nozzles, quality tiers and processes, plates with the last one
    used, and the filament step — one read for the whole dialog (spec §3)."""
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await choices_for_output(client, outputs, meta, settings, printer_id=printer_id)
```

- [ ] **Step 5: Write the API test** (`tests/api/test_print_choices.py`):

```python
from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_print import printers_route
from tests.api.test_print_filaments import inventory_routes, prepared
from tests.api.test_send import BASE, upload_route
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"


def h2c_presets() -> None:
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets-h2c.json"))
    )
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )


@respx.mock
def test_choices_offer_every_size_the_rack_and_the_last_plate(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )
    respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives.json"))
    )

    body = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1").json()

    assert body["nozzle_sizes"] == ["0.2", "0.4", "0.6", "0.8"]
    assert {(n["size"], n["flow"]) for n in body["installed"]} >= {("0.2", "standard")}
    assert body["tiers"]["0.2"][0] == {
        "tier": "fine", "process_name": "0.08mm High Quality @BBL H2C 0.2 nozzle",
    }
    assert "0.08mm High Quality @BBL H2C" in body["processes"]["0.4"]
    assert body["last_bed_type"] == "Textured PEI Plate"
    assert body["filaments"]["slots"]


@respx.mock
def test_an_offline_printer_still_opens_the_dialog(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=[]))

    response = client.get(f"/api/v1/print/outputs/{output_id}/choices?printer_id=1")

    assert response.status_code == 200
    assert response.json()["installed"] == []
    assert response.json()["bed_type"] == "Textured PEI Plate"
```

`filament_options_for_output` reads `/printers/1/status` too (for nozzles) — with the 503 it will raise. Wrap that read in `filament_options_for_output` the same way (`try/except (ApiError, ValueError)` → `options.nozzles = []`), and keep its existing tests green.

- [ ] **Step 6: Run** `uv run --frozen pytest tests/bambuddy/test_choices.py tests/api/test_print_choices.py -v` → PASS; then the full backend gate; then regenerate `backend/openapi.json`.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/bambuddy/choices.py backend/scadbuddy/api/printing.py \
  backend/scadbuddy/bambuddy/pipelines.py backend/openapi.json \
  backend/tests/bambuddy/test_choices.py backend/tests/api/test_print_choices.py
git commit -m "feat(print): choices route — installed nozzles, tiers, processes, last plate"
```

---

### Task 5: Run with choices — always slice and queue

**Files:**
- Modify: `backend/scadbuddy/bambuddy/dispatch.py` (`slice_and_queue` takes a `SlicePlan`)
- Modify: `backend/scadbuddy/bambuddy/pipelines.py` (`PrintRunRequest`, `run_for_output`)
- Modify: `backend/scadbuddy/bambuddy/send.py` (`target_for` accepts an explicit nozzle size and printer id)
- Test: `backend/tests/api/test_print_run_choices.py`; update `backend/tests/bambuddy/test_dispatch.py`, `backend/tests/api/test_print_filaments.py`, `test_print_options_picker.py`, `test_print_plates.py`, `test_print_projects.py`

**Interfaces:**
- Consumes: `resolve`, `PrintChoices`, `Resolved` (Task 3); `ensure_printer_preset` (Task 6 — until it exists, an HF choice with `printer_preset is None` is a 422 "HF printer preset not available yet").
- Produces:

```python
class SlicePlan(BaseModel):          # dispatch.py
    printer_preset: PresetRef
    process_preset: PresetRef
    filament_presets: list[PresetRef]
    filament_colours: list[str]
    bed_type: str

async def slice_and_queue(client, *, library_file_id: int, plan: SlicePlan,
                          printer_id: int, filaments: QueueFilaments | None = None,
                          plate_id: int = 1, copies: int = 1, project_id: int | None = None,
                          options: PrintOptions | None = None) -> QueueOutcome

class PrintRunRequest(BaseModel):    # pipelines.py — pipeline_id, force, bed_type removed
    printer_id: int | None = None
    filament_plan: FilamentPlan
    choices: PrintChoices
    copies: int | None
    plate_id: int = 1
    all_plates: bool = False
    project_id: int | None = None
    options: PrintOptions

class PrintRunResult(BaseModel):     # pipeline_id and run removed; route always "slice_queue"
```

- [ ] **Step 1: Write the failing API test** (`tests/api/test_print_run_choices.py`):

```python
from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_print import printers_route
from tests.api.test_print_choices import h2c_presets
from tests.api.test_print_filaments import (
    inventory_routes,
    prepared,
    queue_route,
    slice_routes,
)
from tests.api.test_send import BASE, upload_route

API = f"{BASE}/api/v1"


def spool_preset_routes() -> None:
    respx.route(method="GET", path__regex=r"/api/v1/inventory/spools/\d+/filament-presets").mock(
        return_value=httpx.Response(200, json=[])
    )


def body(**choices: Any) -> dict[str, Any]:
    return {
        "printer_id": 1,
        "copies": 1,
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 5}]},
        "choices": {"nozzles": [{"size": "0.2"}], "tier": "fine", **choices},
    }


@respx.mock
def test_choices_slice_with_derived_presets_and_queue_without_a_pipeline(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    spool_preset_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    assert response.status_code == 200, response.text
    assert response.json()["route"] == "slice_queue"
    slice_body = json.loads(sliced.calls.last.request.content)
    assert slice_body["printer_preset"] == {"source": "cloud", "id": "GM042"}
    assert slice_body["process_preset"] == {"source": "cloud", "id": "GP243"}
    assert slice_body["bed_type"] == "Textured PEI Plate"
    sent = json.loads(queued.calls.last.request.content)
    assert sent["printer_id"] == 1
    assert sent.get("ams_mapping") is None
    # Superseded by #836: true only when no group prints from the rack.
    assert sent.get("nozzle_rack_choice") is None
    assert not respx.calls.call_count or all(
        "/slicer-pipelines" not in str(call.request.url) for call in respx.calls
    )


@respx.mock
def test_a_resolver_error_is_a_422_before_anything_is_sliced(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    printers_route()
    inventory_routes()
    h2c_presets()
    spool_preset_routes()
    sliced = slice_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=body(nozzles=[{"size": "0.2"}, {"size": "0.4"}]),
    )

    assert response.status_code == 422
    assert "different sizes" in response.json()["detail"]
    assert not sliced.called
```

Spool ids 9 and 5 are the ones `inventory-spools.json` already holds (see `test_a_plan_is_sliced_and_queued_with_the_mapping_on_the_wire`).

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/api/test_print_run_choices.py -v`
Expected: FAIL — 422 on the request body (`choices` unknown / `pipeline_id` required path).

- [ ] **Step 3: Refactor `dispatch.py`.** Replace the module docstring's "**The pipeline is not bypassed.**" paragraph with: "Presets come from the resolver (spec 2026-09-27 §4), which supersedes the earlier rule that the slice borrows a pipeline's presets." Add `SlicePlan` and change `slice_and_queue`:

```python
class SlicePlan(BaseModel):
    """Everything the slice needs, already resolved (spec 2026-09-27 §4)."""

    printer_preset: PresetRef
    process_preset: PresetRef
    filament_presets: list[PresetRef]
    filament_colours: list[str]
    bed_type: str


async def slice_and_queue(
    client: BambuddyClient,
    *,
    library_file_id: int,
    plan: SlicePlan,
    printer_id: int,
    filaments: QueueFilaments | None = None,
    plate_id: int = 1,
    copies: int = 1,
    project_id: int | None = None,
    options: PrintOptions | None = None,
) -> QueueOutcome:
    accepted = await client.slice(
        library_file_id,
        SliceRequest(
            printer_preset=plan.printer_preset,
            process_preset=plan.process_preset,
            filament_presets=plan.filament_presets,
            filament_colours=plan.filament_colours,
            bed_type=plan.bed_type,
            plate=plate_id,
        ),
    )
    # ... unchanged: await_slice, failure → 502, missing sliced id → 502 ...
    remembered = options.queue_fields() if options is not None else {}
    remembered.pop("quantity", None)
    remembered.pop("project_id", None)
    item = await client.enqueue(
        QueueItemCreate(
            **remembered,
            printer_id=printer_id,
            library_file_id=sliced,
            quantity=copies,
            plate_id=plate_id,
            filament_overrides=filaments.filament_overrides if filaments else None,
            required_filament_types=filaments.required_filament_types if filaments else None,
            project_id=project_id,
        )
    )
    return QueueOutcome(
        slice_job_id=accepted.job_id,
        sliced_library_file_id=sliced,
        queue_item_ids=[item.id],
        printer_id=printer_id,
    )
```

Delete `target_of` and `QueueOutcome.target_model` once no caller remains (`send.py`'s `_queue_send` is the send bar — keep it working by building a `SlicePlan` from the pipeline there: `SlicePlan(printer_preset=pipeline.printer_preset, process_preset=pipeline.process_preset, filament_presets=..., filament_colours=..., bed_type=pipeline.bed_type or DEFAULT_BED)` and its printer from `target_of`; if that keeps `target_of` alive, keep it in `send.py` rather than `dispatch.py`).

- [ ] **Step 4: Rewrite `run_for_output`** around the resolver. Keep the upload, project, folder, options and per-plate recording logic exactly; replace the pipeline branches:

```python
async def run_for_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: PrintRunRequest,
) -> PrintRunResult:
    """Slice with presets derived from the dialog's choices, then queue (spec §4).

    Always the slice-and-queue route: there is no pipeline to run. Bambuddy still
    decides AMS tray and extruder placement at dispatch — no ``ams_mapping`` is sent.
    """
    plate_ids = ...  # unchanged
    printer_id = request.printer_id or settings.printer_id
    if printer_id is None:
        raise not_configured("no printer is chosen and none is configured")
    target = await target_for(
        client, settings, meta.slug,
        printer_id=printer_id, nozzle_diameter=request.choices.nozzles[0].size,
    )
    project_id = request.project_id or settings.last_project_id
    folder_id = await folder_for(client, project_id) if project_id is not None else None
    meta, library_file_id = await ensure_uploaded(
        client, store, meta, settings, target=target, folder_id=folder_id
    )
    print_options = resolve_print_options(
        settings, meta.slug, printer_id, request_scope(request.copies, request.options)
    ).model_copy(update={"project_id": None})
    copies = print_options.quantity or 1
    catalogue = await _catalogue(client)

    outcomes: list[QueueOutcome] = []
    sent: list[PlateSend] = []
    warnings: list[FilamentWarning] = []
    plate_options: list[FilamentOptions] = []
    for plate_id in plate_ids:
        options = await gather_options(
            client, library_file_id=library_file_id, printer_id=printer_id,
            plate_id=plate_id, fallback_colours=list(meta.colors),
        )
        spool_presets = {
            spool_id: await client.spool_filament_presets(spool_id)
            for spool_id in {choice.spool_id for choice in request.filament_plan.slots}
        }
        resolved = resolve(options, request.filament_plan, request.choices, catalogue, spool_presets)
        if resolved.errors:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                " ".join(error.message for error in resolved.errors),
            )
        printer_preset = resolved.printer_preset or await ensure_printer_preset(
            client, resolved.printer_preset_name, request.choices.nozzles
        )
        assert resolved.process_preset is not None  # an error above otherwise
        outcome = await slice_and_queue(
            client,
            library_file_id=library_file_id,
            plan=SlicePlan(
                printer_preset=printer_preset,
                process_preset=resolved.process_preset,
                filament_presets=resolved.filament_presets,
                filament_colours=resolved.filament_colours,
                bed_type=resolved.bed_type,
            ),
            printer_id=printer_id,
            filaments=queue_filaments(options, request.filament_plan),
            plate_id=plate_id,
            copies=copies,
            project_id=project_id,
            options=print_options,
        )
        sent = _record_queued(store, meta, plate_id, outcome, project_id, sent)
        outcomes.append(outcome)
        plate_options.append(options)
        for warning in [*resolved.warnings, *check(options, request.filament_plan, copies=copies)]:
            if warning.kind != "low-filament" and warning not in warnings:
                warnings.append(warning)
    warnings += [
        warning
        for warning in check(across_plates(plate_options), request.filament_plan, copies=copies)
        if warning.kind == "low-filament"
    ]
    store.set_printer_bed_type(printer_id, request.choices.bed_type)
    return _queued(client, outcomes, library_file_id, project_id, folder_id,
                   copies=copies, warnings=warnings)
```

Until Task 6 lands, define in `pipelines.py`:

```python
async def ensure_printer_preset(
    client: BambuddyClient, name: str, nozzles: list[NozzleChoice]
) -> PresetRef:
    raise ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"Bambuddy has no printer preset {name!r}, and High Flow presets are not "
        "created yet.",
    )
```

Drop `pipeline_id` from `_queued` and `PrintRunResult`; drop `run`, keep `route: Literal["slice_queue"] = "slice_queue"` so the frontend's existing `route` branch still type-checks until Task 8.

Change `target_for` in `send.py` to accept `printer_id: int | None = None, nozzle_diameter: str | None = None`; when both are given, skip the pipeline lookup: resolve the model from `client.printers()` by id and use `nozzle_diameter` directly for `Target`. Keep the pipeline-based path for the send bar.

- [ ] **Step 5: Update the old tests.** In `test_print_filaments.py`, `test_print_options_picker.py`, `test_print_plates.py`, `test_print_projects.py`, and `test_print.py`: every `POST .../run` that sends `pipeline_id` either (a) becomes a `choices` request asserting the same queue-side behavior (options, plates, projects, copies, recording), or (b) is deleted when it only tested pipeline-run behavior (`route == "pipeline"`, `run.jobs`, eligibility 409/`force`, "uses the model's default pipeline", "lays the file out for that pipeline"). List every deleted test name in the commit message body. `test_dispatch.py` switches to `SlicePlan`.

- [ ] **Step 6: Run** the new test, then the full backend gate. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/bambuddy/dispatch.py backend/scadbuddy/bambuddy/pipelines.py \
  backend/scadbuddy/bambuddy/send.py backend/openapi.json backend/tests/api/test_print_run_choices.py \
  backend/tests/api/test_print.py backend/tests/api/test_print_filaments.py \
  backend/tests/api/test_print_options_picker.py backend/tests/api/test_print_plates.py \
  backend/tests/api/test_print_projects.py backend/tests/bambuddy/test_dispatch.py
git commit -m "feat(print)!: run slices with resolved presets; the picker no longer runs pipelines"
```

---

### Task 6: High Flow printer presets (only if Task 1 tests 1–2 passed)

If they failed: skip this task, and in Task 8 render the HF toggle only under Advanced with the label "Bambuddy may slice as Standard"; the `ensure_printer_preset` stub from Task 5 stays and its message names the failed test.

**Files:**
- Create: `backend/scadbuddy/bambuddy/printer_presets.py`
- Modify: `backend/scadbuddy/bambuddy/pipelines.py` (replace the stub with an import)
- Test: `backend/tests/bambuddy/test_printer_presets.py`

**Interfaces:**
- Consumes: `LocalPresetCreate`, `create_local_preset`, `local_presets` (Task 2); `NozzleChoice` (Task 3).
- Produces: `async def ensure_printer_preset(client, name: str, nozzles: list[NozzleChoice]) -> PresetRef`; `def hf_setting(name: str, nozzles: list[NozzleChoice]) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import json

import httpx
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.printer_presets import ensure_printer_preset, hf_setting
from scadbuddy.bambuddy.resolver import NozzleChoice
from tests.bambuddy.conftest import API

NAME = "ScadBuddy · H2C 0.4 · L-HF R-Std"
NOZZLES = [NozzleChoice(size="0.4", flow="high_flow"), NozzleChoice(size="0.4")]


def test_the_setting_inherits_bambus_preset_and_changes_only_the_flow_type() -> None:
    assert hf_setting(NAME, NOZZLES) == {
        "type": "machine",
        "name": NAME,
        "from": "User",
        "inherits": "Bambu Lab H2C 0.4 nozzle",
        "default_nozzle_volume_type": ["High Flow", "Standard"],
    }


@respx.mock
async def test_an_existing_preset_is_found_by_name_and_not_recreated(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json={"printer": [
            {"id": 7, "name": NAME, "preset_type": "printer", "source": "manual"}
        ], "filament": [], "process": []})
    )
    created = respx.post(f"{API}/local-presets/")
    assert await ensure_printer_preset(bambuddy, NAME, NOZZLES) == PresetRef(source="local", id="7")
    assert not created.called


@respx.mock
async def test_a_missing_preset_is_created_once(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json={"printer": [], "filament": [], "process": []})
    )
    created = respx.post(f"{API}/local-presets/").mock(
        return_value=httpx.Response(
            200, json={"id": 8, "name": NAME, "preset_type": "printer", "source": "manual"}
        )
    )
    assert await ensure_printer_preset(bambuddy, NAME, NOZZLES) == PresetRef(source="local", id="8")
    assert json.loads(created.calls.last.request.content)["setting"]["inherits"] == (
        "Bambu Lab H2C 0.4 nozzle"
    )
```

If `tests/bambuddy/conftest.py` has no `API` constant, define `API = "<its base>/api/v1"` locally from the `config()` fixture's base URL.

- [ ] **Step 2: Run** `uv run --frozen pytest tests/bambuddy/test_printer_presets.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""ScadBuddy's High Flow printer presets (spec 2026-09-27 §4.1).

On the H2C the nozzle flow type is a printer setting, ``default_nozzle_volume_type``,
one value per extruder. Bambuddy's slice request has no printer overrides, so each
flow combination is a local printer preset inheriting Bambu's, found by name and
created on first use. Verified against the live Bambuddy in spec §5.
"""

from __future__ import annotations

from typing import Any

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import LocalPresetCreate, PresetRef
from scadbuddy.bambuddy.resolver import NozzleChoice, _bambu_printer

_FLOW = {"standard": "Standard", "high_flow": "High Flow"}


def hf_setting(name: str, nozzles: list[NozzleChoice]) -> dict[str, Any]:
    flows = [_FLOW[nozzle.flow] for nozzle in nozzles]
    return {
        "type": "machine",
        "name": name,
        "from": "User",
        "inherits": _bambu_printer(nozzles[0].size),
        "default_nozzle_volume_type": (flows + flows)[:2],
    }


async def ensure_printer_preset(
    client: BambuddyClient, name: str, nozzles: list[NozzleChoice]
) -> PresetRef:
    existing = next(
        (row for row in (await client.local_presets()).printer if row.name == name), None
    )
    if existing is not None:
        return existing.ref()
    made = await client.create_local_preset(
        LocalPresetCreate(name=name, preset_type="printer", setting=hf_setting(name, nozzles))
    )
    return made.ref()
```

Delete the stub in `pipelines.py` and import `ensure_printer_preset` from here. Add an API test to `test_print_run_choices.py`: an HF choice with the preset absent creates it (POST `/local-presets/`) and slices with `{"source": "local", "id": "<new id>"}`.

- [ ] **Step 4: Run** the tests and the full backend gate → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/printer_presets.py backend/scadbuddy/bambuddy/pipelines.py \
  backend/tests/bambuddy/test_printer_presets.py backend/tests/api/test_print_run_choices.py
git commit -m "feat(print): find or create ScadBuddy's High Flow printer presets"
```

---

### Task 7: Remove the picker-only pipeline routes

**Files:**
- Modify: `backend/scadbuddy/api/printing.py`, `backend/scadbuddy/bambuddy/pipelines.py`
- Modify: `frontend/src/api/client.ts`
- Delete: `frontend/src/components/NewPipelineForm.tsx` (and its test if present)
- Regenerate: `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `frontend/public/mockServiceWorker.js`

Routes removed: `GET /print/presets`, `POST /print/pipelines`, `GET /print/models/{slug}/pipelines`, `PUT /print/models/{slug}/pipeline`, `POST /print/outputs/{id}/eligibility`. Before deleting each, `git grep` for its client method (`getPrintPresets`, `createPipeline`, `getModelPipelines`, `putModelPipeline`, `checkEligibility`) — **keep** any the send bar or Settings still call; only the picker's go.

- [ ] **Step 1:** Delete the routes and their backend helpers (`preset_options`, `create_pipeline`, `describe_pipelines`, `check_pipelines`, `PipelineReport`, `EligibilityOverview`, `PipelineView`, `PipelineChoices`, `PipelineDefault`, `PresetOptions`) where nothing else imports them — let `mypy` and `ruff` find the stragglers. `StoredSettings.model_pipelines` stays (stored data; removing the field would drop it on the next write) but is no longer read by the picker.
- [ ] **Step 2:** Delete the tests of the removed routes from `tests/api/test_print.py` (listing, eligibility, create, model default). Keep `BED_TYPES`-based tests.
- [ ] **Step 3:** Export openapi, `pnpm gen:api`, `pnpm exec msw init public --save`; remove the dead client methods and `NewPipelineForm.tsx`.
- [ ] **Step 4:** Run both gates → PASS. `pnpm typecheck` will fail in `PrintPicker.tsx` until Task 9; if so, do this task's frontend half together with Task 9 and commit them as one.
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/api/printing.py backend/scadbuddy/bambuddy/pipelines.py \
  backend/openapi.json backend/tests/api/test_print.py
git commit -m "refactor(print)!: drop the picker's pipeline routes"
```

---

### Task 8: Frontend steps — Nozzles, Quality, Plate

**Files:**
- Create: `frontend/src/components/print/NozzleStep.tsx`, `QualityStep.tsx`, `PlateStep.tsx`
- Create: `frontend/src/components/print/steps.test.tsx`
- Modify: `frontend/src/api/types.ts` (re-export `ChoicesView`, `PrintChoices`, `NozzleChoice`, `InstalledNozzle`, `TierOption` from `schema.d.ts` the way the file already re-exports others)
- Modify: `frontend/src/api/client.ts` (`getChoices`, and `runPipeline` renamed `runPrint` taking the new `PrintRunRequest`)

**Interfaces:**
- Consumes: `ChoicesView` (Task 4 schema).
- Produces:

```ts
export function NozzleStep(props: {
  sizes: string[]; installed: InstalledNozzle[]; advanced: boolean
  value: NozzleChoice[]; onChange: (next: NozzleChoice[]) => void
}): JSX.Element
export function QualityStep(props: {
  size: string; tiers: TierOption[]; processes: string[]; advanced: boolean
  tier: 'fine' | 'standard' | 'draft' | null; processName: string | null
  onChange: (next: { tier: 'fine' | 'standard' | 'draft' | null; processName: string | null }) => void
}): JSX.Element
export function PlateStep(props: {
  bedTypes: string[]; value: string; lastBedType: string | null; printerName: string | null
  onChange: (next: string) => void
}): JSX.Element
```

- [ ] **Step 1: Write the failing tests** (`steps.test.tsx`, Vitest + Testing Library as `FilamentPicker.test.tsx` does):

```tsx
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { NozzleStep } from './NozzleStep'
import { PlateStep } from './PlateStep'
import { QualityStep } from './QualityStep'

const installed = [
  { size: '0.2', flow: 'standard' as const, count: 1 },
  { size: '0.4', flow: 'high_flow' as const, count: 2 },
]

describe('NozzleStep', () => {
  it('marks installed sizes and warns on one that is not', () => {
    render(
      <NozzleStep sizes={['0.2', '0.4', '0.6', '0.8']} installed={installed} advanced={false}
        value={[{ size: '0.8', flow: 'standard' }, { size: '0.8', flow: 'standard' }]} onChange={vi.fn()} />,
    )
    expect(screen.getByRole('radio', { name: /0\.2 mm.*installed/i })).toBeInTheDocument()
    expect(screen.getByText(/No 0\.8 mm nozzle is installed/i)).toBeInTheDocument()
  })

  it('sets Standard or High Flow per side, and one size for both in Simple mode', () => {
    const onChange = vi.fn()
    render(
      <NozzleStep sizes={['0.2', '0.4']} installed={installed} advanced={false}
        value={[{ size: '0.4', flow: 'standard' }, { size: '0.4', flow: 'standard' }]} onChange={onChange} />,
    )
    fireEvent.click(screen.getByRole('radio', { name: /left.*high flow/i }))
    expect(onChange).toHaveBeenLastCalledWith([
      { size: '0.4', flow: 'high_flow' }, { size: '0.4', flow: 'standard' },
    ])
    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    expect(onChange).toHaveBeenLastCalledWith([
      { size: '0.2', flow: 'standard' }, { size: '0.2', flow: 'standard' },
    ])
  })

  it('offers a size per side only in Advanced mode', () => {
    const { rerender } = render(
      <NozzleStep sizes={['0.2', '0.4']} installed={installed} advanced={false}
        value={[{ size: '0.4', flow: 'standard' }, { size: '0.4', flow: 'standard' }]} onChange={vi.fn()} />,
    )
    expect(screen.queryByLabelText(/right nozzle size/i)).toBeNull()
    rerender(
      <NozzleStep sizes={['0.2', '0.4']} installed={installed} advanced
        value={[{ size: '0.4', flow: 'standard' }, { size: '0.4', flow: 'standard' }]} onChange={vi.fn()} />,
    )
    expect(screen.getByLabelText(/right nozzle size/i)).toBeInTheDocument()
  })
})

describe('QualityStep', () => {
  const tiers = [
    { tier: 'fine' as const, process_name: '0.08mm High Quality @BBL H2C 0.2 nozzle' },
    { tier: 'standard' as const, process_name: '0.10mm Standard @BBL H2C 0.2 nozzle' },
    { tier: 'draft' as const, process_name: '0.12mm Balanced Quality @BBL H2C 0.2 nozzle' },
  ]

  it('shows Fine / Standard / Draft with the layer height in Simple mode', () => {
    render(<QualityStep size="0.2" tiers={tiers} processes={[]} advanced={false}
      tier="standard" processName={null} onChange={vi.fn()} />)
    expect(screen.getByRole('radio', { name: /Fine.*0\.08mm/ })).toBeInTheDocument()
  })

  it('lists every process in Advanced mode, preselecting the tier', () => {
    const onChange = vi.fn()
    render(<QualityStep size="0.2" tiers={tiers} advanced tier="fine" processName={null}
      processes={tiers.map((t) => t.process_name)} onChange={onChange} />)
    const select = screen.getByLabelText(/process/i) as HTMLSelectElement
    expect(select.value).toBe('0.08mm High Quality @BBL H2C 0.2 nozzle')
    fireEvent.change(select, { target: { value: '0.10mm Standard @BBL H2C 0.2 nozzle' } })
    expect(onChange).toHaveBeenLastCalledWith({
      tier: null, processName: '0.10mm Standard @BBL H2C 0.2 nozzle',
    })
  })
})

describe('PlateStep', () => {
  it('names the last plate as a guess and reminds to swap when it differs', () => {
    render(<PlateStep bedTypes={['Textured PEI Plate', 'Cool Plate']} value="Cool Plate"
      lastBedType="Textured PEI Plate" printerName="H2C" onChange={vi.fn()} />)
    expect(screen.getByText(/Last print used: Textured PEI Plate/)).toBeInTheDocument()
    expect(screen.getByText(/Swap to Cool Plate before this starts/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run** `cd frontend && pnpm test src/components/print/steps.test.tsx` → FAIL (modules missing).

- [ ] **Step 3: Implement the three components.** Follow the markup and class conventions of `PrintOptionsDisclosure.tsx` (read it first). `NozzleStep`:

```tsx
import type { InstalledNozzle, NozzleChoice } from '../../api/types'

type Props = {
  sizes: string[]
  installed: InstalledNozzle[]
  advanced: boolean
  value: NozzleChoice[]
  onChange: (next: NozzleChoice[]) => void
}

const SIDES = ['Left', 'Right'] as const

export function NozzleStep({ sizes, installed, advanced, value, onChange }: Props) {
  const has = (size: string) => installed.some((n) => n.size === size)
  const set = (side: number, patch: Partial<NozzleChoice>) =>
    onChange(value.map((n, i) => (i === side ? { ...n, ...patch } : n)))
  const setBothSizes = (size: string) => onChange(value.map((n) => ({ ...n, size })))
  const missing = [...new Set(value.map((n) => n.size))].filter(
    (size) => installed.length > 0 && !has(size),
  )
  return (
    <fieldset>
      <legend>Nozzles</legend>
      {!advanced && (
        <div role="radiogroup" aria-label="Nozzle size">
          {sizes.map((size) => (
            <label key={size}>
              <input type="radio" name="nozzle-size" checked={value[0]?.size === size}
                onChange={() => setBothSizes(size)} />
              {size} mm{has(size) ? ' (installed)' : ''}
            </label>
          ))}
        </div>
      )}
      {SIDES.map((side, index) => (
        <div key={side} role="radiogroup" aria-label={`${side} nozzle`}>
          {advanced && (
            <label>
              {side} nozzle size
              <select aria-label={`${side} nozzle size`} value={value[index]?.size}
                onChange={(e) => set(index, { size: e.target.value as NozzleChoice['size'] })}>
                {sizes.map((size) => <option key={size} value={size}>{size} mm</option>)}
              </select>
            </label>
          )}
          {(['standard', 'high_flow'] as const).map((flow) => (
            <label key={flow}>
              <input type="radio" name={`flow-${side}`} checked={value[index]?.flow === flow}
                aria-label={`${side} ${flow === 'standard' ? 'Standard' : 'High Flow'}`}
                onChange={() => set(index, { flow })} />
              {flow === 'standard' ? 'Standard' : 'High Flow'}
            </label>
          ))}
        </div>
      ))}
      {missing.map((size) => (
        <p key={size} role="status">No {size} mm nozzle is installed. Install one before this prints.</p>
      ))}
    </fieldset>
  )
}
```

`QualityStep` renders three radios labelled `Fine — 0.08mm` (the layer height is the process name's leading `0.xxmm`) in Simple mode, and a `<select aria-label="Process">` over `processes` in Advanced mode whose value is `processName ?? tiers.find(t => t.tier === tier)?.process_name`; choosing a process calls `onChange({ tier: null, processName })`, choosing a tier calls `onChange({ tier, processName: null })`. `PlateStep` renders a `<select aria-label="Plate">` over `bedTypes`, the line `Last print used: {lastBedType}` when set, and `The {printerName}'s last print used {lastBedType}. Swap to {value} before this starts.` when they differ.

- [ ] **Step 4: Run** the tests and the full frontend gate → PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/print/ frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(print-ui): nozzle, quality and plate steps"
```

---

### Task 9: Rewire `PrintPicker` — Simple/Advanced, no pipelines

**Files:**
- Modify: `frontend/src/components/PrintPicker.tsx`, `PrintPicker.test.tsx`
- Modify: `frontend/src/mocks/*` (msw handlers: add `/print/outputs/:id/choices`, update `/run`)
- Modify: `frontend/e2e/*.spec.ts` that open the picker

**Interfaces:**
- Consumes: `NozzleStep`, `QualityStep`, `PlateStep` (Task 8); `api.getChoices`, `api.runPrint` (Task 8); existing `FilamentPicker`, `PrintOptionsDisclosure`, `ProjectPicker`, `PrintProgressPanel`.

- [ ] **Step 1: Write the failing component tests** in `PrintPicker.test.tsx`, replacing the pipeline-list cases (keep the options, project, copies and progress cases, pointing them at the new mock):

```tsx
it('opens on the choices, with Simple mode and no pipeline list', async () => {
  renderPicker()
  expect(await screen.findByRole('group', { name: /nozzles/i })).toBeInTheDocument()
  expect(screen.queryByTestId('run-pipeline')).toBeNull()
  expect(screen.queryByText(/pipeline/i)).toBeNull()
})

it('sends the choices and the spool plan in one run request', async () => {
  const run = vi.spyOn(api, 'runPrint').mockResolvedValue(queuedResult)
  renderPicker()
  fireEvent.click(await screen.findByRole('radio', { name: /0\.2 mm/i }))
  fireEvent.click(screen.getByRole('radio', { name: /Fine/ }))
  fireEvent.click(screen.getByRole('button', { name: /^Print$/ }))
  await waitFor(() => expect(run).toHaveBeenCalled())
  const [, body] = run.mock.calls[0]!
  expect(body.choices).toMatchObject({ nozzles: [{ size: '0.2' }, { size: '0.2' }], tier: 'fine' })
  expect(body.filament_plan.slots.length).toBeGreaterThan(0)
  expect(body).not.toHaveProperty('pipeline_id')
})

it('disables Print and names the slot when a spool has no preset for the size', async () => {
  vi.spyOn(api, 'runPrint').mockRejectedValue(
    new ApiError(422, 'Generic TPU has no slicer preset for a 0.2 mm nozzle. Pick one under Advanced.'),
  )
  renderPicker()
  fireEvent.click(await screen.findByRole('radio', { name: /0\.2 mm/i }))
  fireEvent.click(screen.getByRole('button', { name: /^Print$/ }))
  expect(await screen.findByText(/no slicer preset for a 0\.2 mm nozzle/)).toBeInTheDocument()
})

it('shows the Advanced process list and per-slot preset override only when toggled', async () => {
  renderPicker()
  expect(screen.queryByLabelText(/process/i)).toBeNull()
  fireEvent.click(await screen.findByRole('switch', { name: /advanced/i }))
  expect(screen.getByLabelText(/process/i)).toBeInTheDocument()
})
```

Build `renderPicker`, `queuedResult` and the `getChoices` mock from a fixture shaped like Task 4's API test response (put it in `src/mocks/choices.ts` and reuse it in the msw handler).

- [ ] **Step 2: Run** `pnpm test src/components/PrintPicker.test.tsx` → FAIL.

- [ ] **Step 3: Rewire `PrintPicker.tsx`:**
  - Delete the pipeline state and effects: `selected`, `reports`, eligibility loading, `setAsDefault`, `creating` / `NewPipelineForm`, `targetLabel`, `presetSummary`, `pipelineBed`, `pipelineNozzle`, and the "every pipeline failed" banner.
  - Load `api.getChoices(outputId, printerId)` once per open/printer change; seed state: `nozzles = [{size:'0.4',flow:'standard'} ×2]` unless the model's remembered choices say otherwise, `tier='standard'`, `processName=null`, `bedType = choices.bed_type`, `advanced=false`, spool plan from `choices.filaments.suggested` (as today).
  - Render in order: printer select (only when `choices.printers.length > 1`), `FilamentPicker` (existing props, fed from `choices.filaments`), `NozzleStep`, `QualityStep` (props from `choices.tiers[size]`, `choices.processes[size]`), `PlateStep`, `PrintOptionsDisclosure`, `ProjectPicker`, copies, an `Advanced` switch (`role="switch"`), and a `Print` button.
  - Advanced mode additionally shows, per slot, a `<select>` of filament presets compatible with the chosen size; source the list from `choices.filaments.spools` presets plus the catalogue rows `getChoices` returns — if that list is not in `ChoicesView`, add `filament_presets: dict[size, list[PresetChoice]]` to it in Task 4's style (backend test first) before wiring it here.
  - `Print` calls `api.runPrint(outputId, { printer_id, filament_plan, choices: { nozzles, tier, process_name: processName, bed_type: bedType, filament_overrides, allow_mixed_sizes: advanced && nozzles[0].size !== nozzles[1].size }, copies, plate_id, all_plates, project_id, options })`; a 422 shows its `detail` above the button and leaves the dialog open.
  - Keep: remembered options, project default, copies, progress tracking after a run (`result.queue_item_ids`), `onPrinterModel` reporting (from the selected printer's `model`), and `putModelChoices` (now also storing nothing pipeline-related).
  - Update the file's header comment to describe the spool-first flow and cite the 2026-09-27 spec.

- [ ] **Step 4: Update msw handlers and e2e.** Add a `GET */print/outputs/:id/choices` handler returning the fixture; change `POST */run` to assert `choices` exists and return a `slice_queue` result; update Playwright specs that clicked a pipeline row to pick 0.2 + Fine and press Print.

- [ ] **Step 5: Run** the full frontend gate and `pnpm exec playwright test` → PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/PrintPicker.tsx frontend/src/components/PrintPicker.test.tsx \
  frontend/src/mocks frontend/e2e frontend/src/api frontend/public/mockServiceWorker.js
git rm frontend/src/components/NewPipelineForm.tsx
git commit -m "feat(print-ui)!: spool-first print picker with Simple and Advanced modes"
```

---

### Task 10: Docs, issues, and the live acceptance run

**Files:**
- Modify: `docs/superpowers/specs/2026-09-24-print-flow-design.md` (one note under §2)
- Modify: `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` (amendments 1–4 from this plan, into §4.4 / §7)
- Modify: `README.md` user guide section on printing, if it describes pipelines in the picker

- [ ] **Step 1:** Under 2026-09-24 §2's "The pipeline is never bypassed" paragraph add: "> Superseded for the print picker by `2026-09-27-spool-first-print-design.md` §0; the one-click send bar still runs the configured pipeline."
- [ ] **Step 2:** Fold this plan's four amendments into the 2026-09-27 spec.
- [ ] **Step 3: Ask the user before any GitHub write**, then: comment on #84 linking the spec; file a follow-up issue "Send bar and Settings: spool-first one-click send (replace the default pipeline)"; link #83 and #190.
- [ ] **Step 4: Live acceptance** against the deployed build (after the user merges and ArgoCD rolls it): print the `name-keychain` (issue #43's acceptance model) with 0.2 + Fine on two Bambu PLA Basic spools, manual start; confirm the queue item shows the 0.2 printer preset, `0.08mm High Quality`, and both spools' colors. Record the result in the spec §5 table.
- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-09-24-print-flow-design.md \
  docs/superpowers/specs/2026-09-27-spool-first-print-design.md README.md
git commit -m "docs: spool-first print flow supersedes the pipeline-first picker"
```
