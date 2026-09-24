import type {
  BambuddyTargets,
  EligibilityReport,
  FilamentOptions,
  PipelineView,
  PresetChoice,
  PrintOptions,
  PrintOptionsState,
  ProjectView,
  BoundingBox,
  CatalogueFont,
  CustomizerSchema,
  FontFamily,
  ModelSummary,
  Output,
  Param,
  PrintProgress,
  Settings,
} from '../api/types'

/**
 * `group` carries a default on the wire, so the generated type makes it required —
 * every fixture parameter really does arrive with one.
 */
function param(group: string, rest: Omit<Param, 'group'>): Param {
  return { group, ...rest }
}

/** A bounding box as the API reports it: corners plus the size, not a bare `{x,y,z}`. */
export function bbox(x: number, y: number, z: number): BoundingBox {
  return { min: [-x / 2, -y / 2, 0], max: [x / 2, y / 2, z], size: [x, y, z] }
}

export const keychainSchema: CustomizerSchema = {
  title: 'Name Keychain',
  source_sha256: 'f'.repeat(64),
  groups: ['Text', 'Plate', 'Colours'],
  parameters: [
    param('Text', {
      name: 'name',
      type: 'string',
      initial: 'Reagan',
      caption: 'Name on the tag',
      max_length: 20,
    }),
    param('Text', { name: 'font', type: 'font', initial: 'Liberation Sans:style=Bold', caption: 'Typeface' }),
    param('Text', {
      name: 'text_size',
      type: 'slider',
      initial: 14,
      caption: 'Text size',
      min: 6,
      max: 28,
      step: 0.5,
    }),
    param('Text', {
      name: 'text_depth',
      type: 'slider',
      initial: 1.6,
      caption: 'Raised height',
      min: 0.4,
      max: 4,
      step: 0.2,
    }),
    param('Plate', {
      name: 'thickness',
      type: 'slider',
      initial: 5.2,
      caption: 'Plate thickness',
      min: 2,
      max: 10,
      step: 0.2,
    }),
    param('Plate', { name: 'padding', type: 'number', initial: 6, caption: 'Margin around the text' }),
    param('Plate', { name: 'corner_radius', type: 'integer', initial: 4, caption: 'Corner radius' }),
    param('Plate', { name: 'keyring_hole', type: 'boolean', initial: true, caption: 'Keyring hole' }),
    param('Plate', {
      name: 'hole_side',
      type: 'select',
      initial: 'left',
      caption: 'Hole position',
      options: [
        { name: 'Left', value: 'left' },
        { name: 'Right', value: 'right' },
        { name: 'Top centre', value: 'top' },
      ],
    }),
    param('Colours', { name: 'body_color', type: 'color', initial: '#1B6CA8', caption: 'Plate' }),
    param('Colours', { name: 'text_color', type: 'color', initial: '#E8532F', caption: 'Text' }),
  ],
}

export const gridfinitySchema: CustomizerSchema = {
  title: 'Gridfinity Bin',
  source_sha256: 'a'.repeat(64),
  groups: ['Size', 'Features'],
  parameters: [
    param('Size', { name: 'units_x', type: 'integer', initial: 2, caption: 'Width in grid units' }),
    param('Size', { name: 'units_y', type: 'integer', initial: 1, caption: 'Depth in grid units' }),
    param('Size', {
      name: 'height_units',
      type: 'slider',
      initial: 3,
      caption: 'Height in 7 mm units',
      min: 1,
      max: 12,
      step: 1,
    }),
    param('Features', { name: 'magnets', type: 'boolean', initial: false, caption: 'Magnet holes' }),
    param('Features', { name: 'label_tab', type: 'boolean', initial: true, caption: 'Label tab' }),
    param('Features', { name: 'bin_color', type: 'color', initial: '#2E7D5B', caption: 'Bin' }),
  ],
}

export const models: ModelSummary[] = [
  {
    slug: 'name-keychain',
    name: 'Name Keychain',
    description: 'Two-colour keychain with raised text. The reference model for ScadBuddy.',
    tags: ['keychain', 'two-colour', 'text'],
    updated_at: '2026-09-21T18:04:00Z',
    has_thumbnail: true,
    has_readme: true,
  },
  {
    slug: 'gridfinity-bin',
    name: 'Gridfinity Bin',
    description: 'Parametric storage bin on the 42 mm Gridfinity grid.',
    tags: ['storage', 'gridfinity'],
    updated_at: '2026-09-14T09:12:00Z',
    has_thumbnail: false,
    has_readme: false,
  },
]

export const schemas: Record<string, CustomizerSchema> = {
  'name-keychain': keychainSchema,
  'gridfinity-bin': gridfinitySchema,
}

export const fonts: FontFamily[] = [
  { family: 'Liberation Sans', styles: ['Regular', 'Bold', 'Italic', 'Bold Italic'] },
  { family: 'Liberation Serif', styles: ['Regular', 'Bold'] },
  { family: 'DejaVu Sans', styles: ['Book', 'Bold', 'Oblique'] },
  { family: 'DejaVu Sans Mono', styles: ['Book', 'Bold'] },
  { family: 'Noto Sans', styles: ['Regular', 'Bold'] },
]

/**
 * A slice of the Google Fonts catalogue. `Pacifico` is the acceptance case from issue
 * #82: not in the image, so picking it has to install it first.
 */
export const fontCatalogue: CatalogueFont[] = [
  {
    family: 'Roboto',
    category: 'sans-serif',
    popularity: 1,
    installed: false,
    variants: [
      { weight: 400, italic: false },
      { weight: 700, italic: false },
    ],
  },
  {
    family: 'Noto Sans',
    category: 'sans-serif',
    popularity: 2,
    installed: true,
    variants: [{ weight: 400, italic: false }],
  },
  {
    family: 'Pacifico',
    category: 'handwriting',
    popularity: 3,
    installed: false,
    variants: [{ weight: 400, italic: false }],
  },
  {
    family: 'Lobster Two',
    category: 'display',
    popularity: 4,
    installed: false,
    variants: [
      { weight: 400, italic: false },
      { weight: 700, italic: false },
    ],
  },
  {
    family: 'Playfair Display',
    category: 'serif',
    popularity: 5,
    installed: false,
    variants: [{ weight: 400, italic: false }],
  },
]

/** The family the install route refuses, so the widget's error path is reachable. */
export const UNINSTALLABLE_FONT = 'Playfair Display'

export const outputs: Output[] = [
  {
    id: 'a'.repeat(32),
    slug: 'name-keychain',
    name: 'Reagan',
    job_id: 'b'.repeat(32),
    created_at: '2026-09-21T19:31:00Z',
    has_thumbnail: true,
    params: {
      name: 'Reagan',
      font: 'Liberation Sans:style=Bold',
      text_size: 14,
      text_depth: 1.6,
      thickness: 5.2,
      padding: 6,
      corner_radius: 4,
      keyring_hole: true,
      hole_side: 'left',
      body_color: '#1B6CA8',
      text_color: '#E8532F',
    },
    bbox_mm: bbox(95.7, 34.6, 6.8),
    colors: ['#1B6CA8', '#E8532F'],
    parts: [],
    warnings: [],
    library_file_id: 8812,
    queue_item_id: 4471,
  },
  {
    id: 'c'.repeat(32),
    slug: 'name-keychain',
    name: 'Nova',
    job_id: 'd'.repeat(32),
    created_at: '2026-09-20T11:22:00Z',
    has_thumbnail: false,
    params: {
      name: 'Nova',
      font: 'Liberation Sans:style=Bold',
      text_size: 18,
      text_depth: 2,
      thickness: 5.2,
      padding: 6,
      corner_radius: 4,
      keyring_hole: true,
      hole_side: 'top',
      body_color: '#F2A93B',
      text_color: '#14181F',
    },
    bbox_mm: bbox(78.4, 40.2, 7.2),
    colors: ['#F2A93B', '#14181F'],
    parts: [],
    warnings: [],
    library_file_id: 8790,
  },
  {
    id: 'e'.repeat(32),
    slug: 'name-keychain',
    name: 'Workshop',
    job_id: 'f'.repeat(32),
    created_at: '2026-09-18T09:03:00Z',
    has_thumbnail: false,
    params: {
      name: 'Workshop',
      font: 'DejaVu Sans:style=Bold',
      text_size: 12,
      text_depth: 1.6,
      thickness: 4,
      padding: 5,
      corner_radius: 2,
      keyring_hole: false,
      hole_side: 'left',
      body_color: '#1B6CA8',
      text_color: '#E8532F',
    },
    bbox_mm: bbox(104.1, 30.8, 5.6),
    colors: ['#1B6CA8', '#E8532F'],
    parts: [],
    warnings: [],
  },
]

export const settings: Settings = {
  bambuddy_url: 'https://bambuddy.internal.nullreference.io',
  has_api_key: true,
  public_url: 'https://scadbuddy.internal.nullreference.io',
  library_folder_id: 2,
  pipeline_id: 1,
  printer_id: 1,
  printer_preset: null,
  process_preset: null,
  filament_presets: [],
  bed_type: null,
}

/**
 * #86 — the print picker's pipelines. `Textured PEI · 0.20 mm · AMS` targets the one H2C
 * directly; `Any H2C` targets the printer *class*, which is the case where the picker has
 * to ask which printer, and `Draft · 0.28 mm` is the one that is never eligible.
 */
export const pipelineViews: PipelineView[] = [
  {
    id: 1,
    name: 'Textured PEI · 0.20 mm · AMS',
    description: null,
    bed_type: 'Textured PEI Plate',
    target_kind: 'specific_printer',
    target_printer_id: 1,
    target_printer_name: '3DP-31B-598',
    target_model_class: null,
    fanout_strategy: 'max_parallel',
    printer_preset: { source: 'cloud', id: 'GM041' },
    process_preset: { source: 'cloud', id: 'GP252' },
    filament_presets: [{ source: 'cloud', id: 'GFSA05_22' }],
    printer_preset_name: 'Bambu Lab H2C 0.4 nozzle',
    process_preset_name: '0.20mm Standard @BBL H2C',
    filament_preset_names: ['Bambu PLA Basic @BBL H2C'],
    printer_ids: [1],
  },
  {
    id: 2,
    name: 'Draft · 0.28 mm',
    description: null,
    bed_type: 'Cool Plate',
    target_kind: 'specific_printer',
    target_printer_id: 1,
    target_printer_name: '3DP-31B-598',
    target_model_class: null,
    fanout_strategy: 'max_parallel',
    printer_preset: { source: 'cloud', id: 'GM041' },
    process_preset: { source: 'cloud', id: 'GP260' },
    filament_presets: [{ source: 'cloud', id: 'GFSB00_22' }],
    printer_preset_name: 'Bambu Lab H2C 0.4 nozzle',
    process_preset_name: '0.28mm Draft @BBL H2C',
    filament_preset_names: ['Bambu ABS @BBL H2C'],
    printer_ids: [1],
  },
  {
    id: 3,
    name: 'Any H2C',
    description: null,
    bed_type: 'Textured PEI Plate',
    target_kind: 'printer_class',
    target_printer_id: null,
    target_printer_name: null,
    target_model_class: 'H2C',
    fanout_strategy: 'round_robin',
    printer_preset: { source: 'cloud', id: 'GM041' },
    process_preset: { source: 'cloud', id: 'GP252' },
    filament_presets: [{ source: 'cloud', id: 'GFSA05_22' }],
    printer_preset_name: 'Bambu Lab H2C 0.4 nozzle',
    process_preset_name: '0.20mm Standard @BBL H2C',
    filament_preset_names: ['Bambu PLA Basic @BBL H2C'],
    printer_ids: [1, 2],
  },
]

/** `check-eligibility` answers 200 with the report — only `run` turns it into a 409. */
export const eligibilityReports: Record<number, EligibilityReport> = {
  1: {
    ok: true,
    target_kind: 'specific_printer',
    target_printer_id: 1,
    target_printer_name: '3DP-31B-598',
    target_model_class: null,
    issues: [],
    printer_reports: [],
  },
  2: {
    ok: false,
    target_kind: 'specific_printer',
    target_printer_id: 1,
    target_printer_name: '3DP-31B-598',
    target_model_class: null,
    issues: [
      { kind: 'filament_type_mismatch', slot_index: 0, expected: 'ABS', actual: 'PLA' },
      { kind: 'nozzle_diameter_mismatch', slot_index: null, expected: '0.4', actual: '0.2' },
    ],
    printer_reports: [],
  },
  // Under printer_class, `ok` means at least ONE printer passes; the reasons are per printer.
  3: {
    ok: true,
    target_kind: 'printer_class',
    target_printer_id: null,
    target_printer_name: null,
    target_model_class: 'H2C',
    issues: [],
    printer_reports: [
      { printer_id: 1, printer_name: '3DP-31B-598', ok: true, issues: [] },
      {
        printer_id: 2,
        printer_name: '3DP-77A-114',
        ok: false,
        issues: [{ kind: 'ams_slot_empty', slot_index: 1, expected: 'PLA', actual: 'empty' }],
      },
    ],
  },
}

/**
 * #79 — projects, taken from the recorded Bambuddy (`backend/tests/bambuddy/recordings/`):
 * project 1 `Reagan Keychain` owns library folder 2 `Raegan` — Bambuddy's own spelling,
 * and the pairing that makes its project page list files.
 *
 * `Gridfinity Bins` has no folder at all, which is the ordinary state of a project made
 * in Bambuddy rather than here: linking one creates its folder rather than refusing, and
 * the picker says so before the send.
 */
export const projectViews: ProjectView[] = [
  {
    id: 1,
    name: 'Reagan Keychain',
    description: null,
    colour: '#ef4444',
    status: 'active',
    archive_count: 1,
    queue_count: 0,
    folder_id: 2,
    folder_name: 'Raegan',
  },
  {
    id: 2,
    name: 'Gridfinity Bins',
    description: 'Drawer inserts, printed a few at a time.',
    colour: null,
    status: 'active',
    archive_count: 0,
    queue_count: 3,
    folder_id: null,
    folder_name: null,
  },
]

export const printerPresets: PresetChoice[] = [
  {
    ref: { source: 'cloud', id: 'GM041' },
    name: 'Bambu Lab H2C 0.4 nozzle',
    filament_type: null,
    filament_colour: null,
    compatible_printers: [],
  },
  {
    ref: { source: 'cloud', id: 'GM042' },
    name: 'Bambu Lab H2C 0.2 nozzle',
    filament_type: null,
    filament_colour: null,
    compatible_printers: [],
  },
]

/** Nozzle diameter lives in the process preset's NAME, not a field of its own. */
export const processPresets: PresetChoice[] = [
  {
    ref: { source: 'cloud', id: 'GP243' },
    name: '0.08mm High Quality @BBL H2C 0.2 nozzle',
    filament_type: null,
    filament_colour: null,
    compatible_printers: ['Bambu Lab H2C 0.2 nozzle'],
  },
  {
    ref: { source: 'cloud', id: 'GP252' },
    name: '0.20mm Standard @BBL H2C',
    filament_type: null,
    filament_colour: null,
    compatible_printers: ['Bambu Lab H2C 0.4 nozzle'],
  },
]

export const filamentPresets: PresetChoice[] = [
  {
    ref: { source: 'cloud', id: 'GFSA05_22' },
    name: 'Bambu PLA Basic @BBL H2C',
    filament_type: 'PLA',
    filament_colour: null,
    compatible_printers: ['Bambu Lab H2C 0.4 nozzle'],
  },
  {
    ref: { source: 'cloud', id: 'GFSB00_22' },
    name: 'Bambu ABS @BBL H2C',
    filament_type: 'ABS',
    filament_colour: null,
    compatible_printers: ['Bambu Lab H2C 0.4 nozzle'],
  },
  // An OrcaSlicer import: /local-presets/ is not the `local` tier of /slicer/presets.
  {
    ref: { source: 'local', id: '2' },
    name: 'Cookiecad PETG Magic Dark Magic @H2C',
    filament_type: 'PETG',
    filament_colour: '#3400AD',
    compatible_printers: ['Bambu Lab H2C 0.4 nozzle'],
  },
]

export const BED_TYPES = [
  'Cool Plate',
  'Cool Plate (SuperTack)',
  'Supertack Plate',
  'Engineering Plate',
  'High Temp Plate',
  'Textured PEI Plate',
  'Smooth PEI Plate',
]
/** Bambuddy 1.2.5.5's own ``PrintQueueItemCreate`` defaults, as the backend serves them. */
export const printOptionDefaults: PrintOptions = {
  bed_levelling: 'auto',
  flow_cali: 'auto',
  vibration_cali: true,
  nozzle_offset_cali: 'auto',
  layer_inspect: false,
  timelapse: false,
  use_ams: true,
  quantity: 1,
  manual_start: false,
  insert_at_top: false,
  auto_off_after: false,
  project_id: null,
  preheat_override: 'inherit',
  preheat_chamber_target_override: null,
}

export const printOptions: PrintOptionsState = {
  defaults: printOptionDefaults,
  global_options: {},
  printers: {},
  models: {},
  printer_id: 1,
}

export const targets: BambuddyTargets = {
  // `is_external`, `target_kind` and `fanout_strategy` carry defaults on the wire, so the
  // generated types make them required — they really do arrive on every row.
  folders: [
    { id: 1, name: 'MakerWorld', is_external: false },
    { id: 2, name: 'ScadBuddy', is_external: false },
    { id: 3, name: 'Keychains', is_external: false },
  ],
  pipelines: pipelineViews.slice(0, 2).map((pipeline) => ({
    id: pipeline.id,
    name: pipeline.name,
    bed_type: pipeline.bed_type,
    target_kind: pipeline.target_kind,
    target_printer_id: pipeline.target_printer_id,
    target_model_class: pipeline.target_model_class,
    fanout_strategy: pipeline.fanout_strategy,
    printer_preset: pipeline.printer_preset,
    process_preset: pipeline.process_preset,
    filament_presets: pipeline.filament_presets,
  })),
  printers: [
    { id: 1, name: '3DP-31B-598', model: 'H2C', is_active: true, nozzle_count: 2 },
    { id: 2, name: '3DP-77A-114', model: 'H2C', is_active: true, nozzle_count: 2 },
  ],
}

/**
 * #89 — run tracking. `bambuddy_url` is Bambuddy's queue page for both routes, because
 * that is what the backend hands back (`web_url("/queue")`); a per-entry link is built
 * from it rather than sent.
 */
const QUEUE_URL = `${settings.bambuddy_url}/queue`

/** A pipeline run mid-flight: both copies have reached the queue, neither has printed. */
export const pipelineProgress: PrintProgress = {
  route: 'pipeline',
  stage: 'queued',
  settled: false,
  pipeline_run_id: 12,
  slice_job_id: 21,
  copies: 2,
  copies_completed: 0,
  copies_failed: 0,
  copies_cancelled: 0,
  copies_in_progress: 2,
  error_message: null,
  fix: null,
  copies_detail: [
    {
      copy_index: 0,
      printer_name: '3DP-31B-598',
      queue_entry_id: 4472,
      stage: 'queued',
      message: null,
      waiting_reason: null,
    },
    // A fan-out Bambuddy has not assigned yet: `assigned_printer_name` is null until it
    // picks, which is a normal state and not a missing value to hide.
    {
      copy_index: 1,
      printer_name: null,
      queue_entry_id: 4473,
      stage: 'queued',
      message: null,
      waiting_reason: null,
    },
  ],
  bambuddy_url: QUEUE_URL,
}

/**
 * The real failed run, transcribed from `backend/tests/bambuddy/recordings/pipeline-run.json`
 * as `progress.from_run` normalises it. Its point is that Bambuddy's own fields all say
 * the run is fine — `status: "in_progress"`, `copies_in_progress: 1`, the one job still
 * `pending` — while `completed_at` and `error_message` say it is over. `settled` is the
 * backend's resolution of that contradiction, and the only reason the poll ever stops.
 * `fix` is chosen from `slice_job_id` set with `sliced_library_file_id` still null, not
 * from the wording of the message.
 */
export const failedRunProgress: PrintProgress = {
  route: 'pipeline',
  stage: 'failed',
  settled: true,
  pipeline_run_id: 1,
  slice_job_id: 7,
  copies: 1,
  copies_completed: 0,
  copies_failed: 0,
  copies_cancelled: 0,
  copies_in_progress: 1,
  error_message:
    'Slice failed: The selected printer is not compatible with the process preset in the 3mf.',
  fix:
    'Bambuddy could not slice this plate. Choose a different pipeline or plate, or fix ' +
    'the model, and print again.',
  copies_detail: [
    {
      copy_index: 0,
      printer_name: null,
      queue_entry_id: null,
      stage: 'queued',
      message: null,
      waiting_reason: null,
    },
  ],
  bambuddy_url: QUEUE_URL,
}

/**
 * The slice-and-queue route, waiting its turn. `waiting_reason` is Bambuddy's own
 * sentence for why a queued entry is not printing yet — information, not a failure, so
 * `error_message` stays null beside it.
 */
export const queuedSliceProgress: PrintProgress = {
  route: 'slice_queue',
  stage: 'queued',
  settled: false,
  slice_job_id: 31,
  queue_item_id: 4471,
  copies: 1,
  copies_completed: 0,
  copies_failed: 0,
  copies_cancelled: 0,
  copies_in_progress: 1,
  error_message: null,
  fix: null,
  copies_detail: [
    // The queue route repeats through `quantity`, so there is one row whatever the
    // count — and no `copy_index` to number it by.
    {
      copy_index: null,
      printer_name: '3DP-31B-598',
      queue_entry_id: 4471,
      stage: 'queued',
      message: null,
      waiting_reason: 'No active H2C printers are idle',
    },
  ],
  bambuddy_url: QUEUE_URL,
}

export const FAILING_NAME = 'boom'

export const OPENSCAD_LOG_TAIL = [
  'Compiling design (CSG Tree generation)...',
  'ERROR: Parser error: syntax error in file model.scad, line 42',
  'ERROR: Compilation failed!',
  'WARNING: Object may not be a valid 2-manifold and may need repair!',
  'Execution aborted',
]

/** What `GET /models/{slug}/source` serves, and what the editor opens prefilled. */
export const keychainSource = `/* [Text] */
// Name on the tag
name = "Reagan";
// Text size
text_size = 14; // [6:0.5:28]

/* [Plate] */
thickness = 5.2; // [2:0.2:10]

module tag() {
  cube([text_size * len(name), text_size * 2, thickness]);
}

tag();
`

/** Source the mock refuses, so the paste flow's failure path is reachable. */
export const BROKEN_SOURCE = `size = 10;
cube([size, size, size)
`

/**
 * The mock's stand-in for OpenSCAD's parser: a line that opens a bracket it never
 * closes. Enough to drive the inline-error UI without shipping a real parser.
 */
export function mockParseError(source: string): { line: number } | null {
  const index = source
    .split('\n')
    .findIndex((line) => line.includes('[') && !line.includes(']'))
  return index === -1 ? null : { line: index + 1 }
}

/**
 * #87 — what `GET /print/outputs/{id}/filaments` answers for the two-colour keychain,
 * built from the recorded H2C in `backend/tests/bambuddy/recordings/`.
 *
 * The shape of the estate is the point, because every trap in the picker is one of
 * these rows:
 *
 * - **The AMS ids are the printer's, not indices.** The recorded machine reports
 *   `[0, 1, 128, 2]`, with the AMS-HT at `128` holding one spool; `ams_switch_inlet`
 *   keys them as strings and has AMS 1 on inlet B and the AMS-HT on inlet A, which is
 *   what makes the two extruders' reachability a real question here.
 * - **Two spools are the same colour, one loaded and one nearly spent on a shelf.**
 *   That is what the "loaded only" and "enough for this print" filters are for, and the
 *   spent one (2 g) is the row the sufficiency rule has to hide only when the grams are
 *   actually known.
 * - **One spool has no remaining weight at all.** An untagged spool reports `remain: -1`
 *   and the backend normalises it to `null`; it renders as an em dash and must survive
 *   every filter, because unknown is not empty.
 * - **`suggested` picks a spool that is NOT loaded** for slot 2: the exact colour match
 *   is the Elegoo pink on the shelf, and the auto-matcher prefers an exact colour to a
 *   loaded near-miss. The `not-loaded` warning that follows is an instruction, not a
 *   fault, which is why it reads as an advisory.
 *
 * The server returns `spools` already sorted — loaded in this printer, then loaded
 * elsewhere, then the shelf; most remaining first within each band — so the order here
 * is load-bearing and not alphabetical.
 */
export const filamentOptions: FilamentOptions = {
  library_file_id: 8812,
  printer_id: 1,
  printer_name: '3DP-31B-598',
  slots: [
    // A sliced plate, so the grams are real. `PrintPicker.test.tsx` overrides these to
    // null for the unsliced case, which is what an unmodified upload actually answers.
    { slot_id: 1, material: 'PLA', colour: '#0047BB', used_grams: 4.8 },
    { slot_id: 2, material: 'PLA', colour: '#FF1493', used_grams: 1.9 },
  ],
  spools: [
    {
      spool_id: 9,
      material: 'PETG',
      subtype: 'Basic',
      brand: 'Bambu Lab',
      color_name: 'Misty Blue',
      colour: '#688197',
      slicer_filament: 'GFG00',
      slicer_filament_name: 'Bambu PETG Basic',
      remaining_g: 1000,
      storage_location: null,
      loaded: {
        printer_id: 1,
        printer_name: '3DP-31B-598',
        ams_id: 0,
        tray_id: 1,
      },
    },
    {
      spool_id: 21,
      material: 'PLA',
      subtype: 'Silk',
      brand: 'Bambu Lab',
      color_name: 'Blue',
      colour: '#0047BB',
      slicer_filament: 'GFA05',
      slicer_filament_name: 'Bambu PLA Silk @BBL H2C 0.4 nozzle',
      remaining_g: 812,
      storage_location: null,
      loaded: {
        printer_id: 1,
        printer_name: '3DP-31B-598',
        ams_id: 1,
        tray_id: 0,
      },
    },
    {
      // The AMS-HT: one spool, no slot number to name, and on the other inlet.
      spool_id: 22,
      material: 'PLA',
      subtype: 'Basic',
      brand: 'Bambu Lab',
      color_name: 'Hot Pink',
      colour: '#F5547C',
      slicer_filament: 'GFA00',
      slicer_filament_name: 'Bambu PLA Basic @BBL H2C 0.4 nozzle',
      remaining_g: 640,
      storage_location: null,
      loaded: {
        printer_id: 1,
        printer_name: '3DP-31B-598',
        ams_id: 128,
        tray_id: 0,
      },
    },
    {
      // Loaded, but in the other printer — a legitimate choice that means fetching it.
      spool_id: 30,
      material: 'PLA',
      subtype: 'Basic',
      brand: 'Inland',
      color_name: 'White',
      colour: '#E3E5E5',
      slicer_filament: 'GFL99',
      slicer_filament_name: 'Inland PLA @BBL H2C',
      remaining_g: 877.5,
      storage_location: null,
      loaded: {
        printer_id: 2,
        printer_name: '3DP-77A-114',
        ams_id: 0,
        tray_id: 0,
      },
    },
    {
      spool_id: 24,
      material: 'PETG',
      subtype: 'Magic',
      brand: 'Cookiecad',
      color_name: 'Witchcraft',
      colour: '#7389BC',
      slicer_filament: '2',
      slicer_filament_name: 'Cookiecad PETG Magic Dark Magic (3DFP 7JdoWkaDB) @H2C',
      remaining_g: 823,
      storage_location: 'Shelf B',
      loaded: null,
    },
    {
      spool_id: 27,
      material: 'PLA',
      subtype: 'Basic',
      brand: 'Elegoo',
      color_name: 'Deep Pink',
      colour: '#FF1493',
      slicer_filament: null,
      slicer_filament_name: null,
      // Untagged: Bambuddy reports `remain: -1` for it, which is unknown, not empty.
      remaining_g: null,
      storage_location: 'Shelf B',
      loaded: null,
    },
    {
      // Nearly spent: 2 g against slot 1's 4.8 g, so "enough for this print" hides it.
      spool_id: 26,
      material: 'PLA',
      subtype: 'Silk',
      brand: 'Bambu Lab',
      color_name: 'Blue',
      colour: '#0047BB',
      slicer_filament: 'GFA05',
      slicer_filament_name: 'Bambu PLA Silk @BBL H2C 0.4 nozzle',
      remaining_g: 2,
      storage_location: 'Shelf A',
      loaded: null,
    },
  ],
  suggested: [
    { slot_id: 1, spool_id: 21 },
    { slot_id: 2, spool_id: 27 },
  ],
  warnings: [
    {
      kind: 'not-loaded',
      slot_id: 2,
      message:
        'Load Elegoo PLA Basic Deep Pink into the printer before this prints — it is stored in Shelf B.',
    },
  ],
}
