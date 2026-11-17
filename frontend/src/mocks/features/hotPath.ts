import { HttpResponse, http } from 'msw'
import type {
  ChoicesView,
  FilamentOptions,
  PreviewStarted,
  PrinterNow,
  PrinterRow,
  PrintRun,
  PrintRunRequest,
  PrintRunResult,
  ProjectChoices,
  SlicePreview,
  SpoolOption,
  FilamentSpool,
  TraySpoolLink,
  UnknownTray,
} from '../../api/types'
import { choicesView } from '../choices'
import * as fixtures from '../fixtures'
import { mockSettings } from '../handlers'
import { announcePreviewSlice, emitRealtime } from '../realtime'
import { demoToolpath } from '../toolpath'
import { acceptOperation } from './operations'

/**
 * #3818 — the hot path without Bambuddy (plan 2026-10-18, Task 11): render, print
 * dialog, slice, print and follow on a LAN-linked H2C with the native slicer and
 * library, and no Bambuddy URL at all. Off by default; on, it answers ahead of every
 * other mock (one `ALL /api/v1/*` handler, so no route is declared twice, which
 * `features.test.ts` forbids) and passes through whatever it does not own.
 *
 * The switch is `localStorage['sb-mock-hot-path'] === '1'`, read at load and on each
 * `reset()`: the handlers' state lives in the page, a full navigation starts it again,
 * and Playwright's `page.request` never reaches the service worker, so the spec sets it
 * with `addInitScript`. Unit tests call `setHotPath`.
 *
 * Every Bambuddy-only route (the camera, Bambuddy's tray assignment, the
 * projects and their default without `native`) answers 409 "no Bambuddy URL is
 * configured", as the backend does with none set, so a component that still asks
 * Bambuddy shows an error a test catches.
 *
 * The shapes the backend's native LAN print (plan 3a Task 9) will answer are not built
 * yet; this answers as the plan describes them, and moves with them.
 */

const base = '/api/v1'

export const HOT_PATH_KEY = 'sb-mock-hot-path'
export const NO_BAMBUDDY = 'no Bambuddy URL is configured'

/**
 * The one printer: an H2C linked over the LAN and asked to print natively. It is also
 * linked to Bambuddy printer 1, since the backend refuses a run on a row with none
 * (`_not_on_bambuddy`) until plan 3a Task 9; nothing on the native path asks Bambuddy
 * about it, so the walk still makes no Bambuddy call.
 */
export const HOT_PRINTER: PrinterRow = {
  id: '00000000-0000-4000-8000-0000000000c2',
  serial: '31B8BP000000C2',
  kind: 'physical',
  name: 'H2C',
  model: 'H2C',
  bambuddy_printer_id: 1,
  bambuddy_unlinked_id: null,
  bambuddy_active: true,
  lan_host: '192.168.1.62',
  lan_cert_sha256: null,
  has_access_code: true,
  preferred_path: 'native',
  lan_move_pending: false,
  created_at: '2026-10-18T12:00:00Z',
  updated_at: '2026-10-18T12:00:00Z',
}

/** The report's own id, the Bambuddy printer the row links (`status_from_report`). */
const HERE = 1
/** `tray_spool_id(0, 0)` and `tray_spool_id(0, 1)` (`bambuddy/trays.py`). */
export const TAGGED_TRAY = -1
export const UNTAGGED_TRAY = -2
/** The SB spool no tray holds, by its number: offered for linking, never printed from. */
export const SHELF_NUMBER = 2

/** Native library folders (`/print/projects?native=true`). */
export const HOT_FOLDERS = [
  { id: 701, name: 'Keychains', path: 'Keychains', parent_id: null, file_count: 3 },
  { id: 702, name: 'Gifts', path: 'Gifts', parent_id: null, file_count: 1 },
]

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

const state = {
  on: readFlag(HOT_PATH_KEY),
  /** The untagged tray's link, once made. */
  linked: false,
  /** Each background slice, by job id, with how often it has been read. */
  slices: new Map<number, { reads: number; request: PrintRunRequest }>(),
  /** Each run as it finished: its follow (`GET /print/runs/{id}`) reads it so. */
  runs: new Map<string, PrintRun>(),
  /** How often `/print/printers/now` has been read since the print started, or null. */
  printing: null as null | { name: string; reads: number },
  seq: 0,
}

/** Unit tests: the hot path on or off. */
export function setHotPath(on: boolean): void {
  state.on = on
}

export function reset(): void {
  state.on = readFlag(HOT_PATH_KEY)
  state.linked = false
  state.slices.clear()
  state.runs.clear()
  state.printing = null
}

function problem(status: number, detail: string) {
  return HttpResponse.json(
    { title: status === 409 ? 'Conflict' : 'Not Found', status, detail },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

/** Marks the scenario's own refusals, so a spec tells them from any other 409. */
export const NO_BAMBUDDY_HEADER = 'X-Mock-No-Bambuddy'

const noBambuddy = () =>
  HttpResponse.json(
    { title: 'Conflict', status: 409, detail: NO_BAMBUDDY },
    { status: 409, headers: { 'Content-Type': 'application/problem+json', [NO_BAMBUDDY_HEADER]: '1' } },
  )

/** The SB spool in the tagged tray (#6262): found by its tag, Bambu Lab Blue, #1. */
const TAGGED_SPOOL = '7a2c1f0e-0000-4000-8000-000000000001'

function trayOptions(): SpoolOption[] {
  const loaded = (tray: number) => ({ printer_id: HERE, printer_name: HOT_PRINTER.name, ams_id: 0, tray_id: tray })
  const trays: SpoolOption[] = [
    {
      spool_id: TAGGED_TRAY,
      material: 'PLA',
      colour: '#0047BB',
      rgba: '#0047BBFF',
      brand: 'Bambu Lab',
      color_name: 'Blue',
      slicer_filament: 'GFA05',
      remaining_g: 812,
      label_weight_g: 1000,
      loaded: loaded(0),
      extruder: 0,
      side: 'R',
      tray_only: true,
      label: 'AMS-A slot 1',
      colour_word: 'blue',
      remain_percent: 81,
      sb_spool_id: TAGGED_SPOOL,
      sb_number: 1,
      needs_type: false,
    },
    {
      spool_id: UNTAGGED_TRAY,
      material: 'PLA',
      colour: '#FF1493',
      rgba: '#FF1493FF',
      brand: null,
      color_name: state.linked ? 'Pink' : null,
      slicer_filament: null,
      remaining_g: state.linked ? 640 : null,
      label_weight_g: state.linked ? 1000 : null,
      loaded: loaded(1),
      extruder: 0,
      side: 'R',
      tray_only: true,
      label: 'AMS-A slot 2',
      colour_word: 'pink',
      remain_percent: null,
      sb_spool_id: state.linked ? SHELF_SPOOL : null,
      sb_number: state.linked ? SHELF_NUMBER : null,
      needs_type: state.linked ? true : null,
    },
  ]
  return trays
}

/** The shelf spool: the SB spool the untagged tray's answer places (#6262 Task 4). */
const SHELF_SPOOL = '7a2c1f0e-0000-4000-8000-000000000002'

/** `PUT …/trays/{ams}/{tray}/spool`'s answer: the SB spool, now in the tray's place. */
function shelfSpool(): FilamentSpool {
  return {
    id: SHELF_SPOOL,
    number: SHELF_NUMBER,
    type_id: null,
    brand_id: null,
    material_id: null,
    subtype_id: null,
    category_id: null,
    effect_id: null,
    colour_name: 'Pink',
    rgba: '#FF1493FF',
    extra_colours: null,
    diameter_mm: null,
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    label_weight_g: 1000,
    insert_type_id: null,
    low_stock_pct: null,
    cost_per_kg: null,
    location_id: '7a2c1f0e-0000-4000-8000-0000000000a2',
    weight_locked: false,
    note: null,
    grams_left: 640,
    last_weighed_g: null,
    last_weighed_at: null,
    last_dried_at: null,
    last_dried_temp_c: null,
    last_dried_hours: null,
    last_used_at: null,
    archived_at: null,
    created_at: '2026-11-15T07:00:00Z',
    updated_at: '2026-11-15T07:00:00Z',
    low_stock: false,
  }
}

/** The linking list (D1, #4506's `shelf`): an SB spool no tray holds, under its number (#6262). */
function shelfOptions(): SpoolOption[] {
  if (state.linked) return []
  const shelf: SpoolOption = {
    spool_id: SHELF_NUMBER,
    sb_spool_id: SHELF_SPOOL,
    sb_number: SHELF_NUMBER,
    needs_type: true,
    material: 'PLA',
    colour: '#FF1493',
    rgba: '#FF1493FF',
    brand: null,
    color_name: 'Pink',
    remaining_g: 640,
    label_weight_g: 1000,
    loaded: null,
  }
  return [shelf]
}

function unknownTrays(): UnknownTray[] {
  if (state.linked) return []
  return [
    {
      ams_id: 0,
      tray_id: 1,
      label: 'AMS-A slot 2',
      material: 'PLA',
      colour: '#FF1493',
      colour_word: 'pink',
      // `material|RRGGBB`, the tray link's own format (#4506).
      fingerprint: 'PLA|FF1493',
      spool_id: UNTAGGED_TRAY,
      candidates: [SHELF_NUMBER],
    },
  ]
}

/** The filament step on the native path: the trays, each with the SB spool it holds (#6262). */
export function hotFilaments(): FilamentOptions {
  return {
    library_file_id: null,
    source: 'trays',
    printer_id: HERE,
    printer_name: HOT_PRINTER.name,
    track_switch: false,
    nozzles: [],
    slots: (fixtures.filamentOptions.slots ?? []).map((slot) => ({
      ...slot,
      colour_matches: slot.slot_id === 1 ? [TAGGED_TRAY] : [UNTAGGED_TRAY],
    })),
    spools: trayOptions(),
    shelf: shelfOptions(),
    suggested: [
      { slot_id: 1, spool_id: TAGGED_TRAY },
      { slot_id: 2, spool_id: UNTAGGED_TRAY },
    ],
    trays: unknownTrays(),
    warnings: [],
  }
}

function hotChoices(): ChoicesView {
  return {
    ...choicesView,
    printer_id: HERE,
    printers: [{ ...(choicesView.printers ?? [])[0]!, id: HERE, name: HOT_PRINTER.name, model: 'H2C' }],
    filaments: hotFilaments(),
    model_choices: { printer_id: null, filament_plan: [] },
    // The server's word that a run on the H2C goes over the LAN (`print_run.lan_printers`).
    lan_printers: [HOT_PRINTER.id],
  }
}

function startSlice(request: PrintRunRequest): PreviewStarted {
  const jobId = 9500 + ++state.seq
  state.slices.set(jobId, { reads: 0, request })
  return { job_id: jobId, plate_id: request.plate_id ?? 1, nozzle_plan: null, slicer: 'native' }
}

/** The native slicer's read: running at 20 %, then 60 %, then done (slicer Task 14). */
function readSlice(jobId: number) {
  const job = state.slices.get(jobId)
  if (!job) return problem(404, `there is no background slice ${jobId}`)
  job.reads += 1
  if (job.reads < 3) {
    // #265: the dialog reads again on the next event, as the backend's follow sends it.
    announcePreviewSlice('native', jobId)
    return HttpResponse.json({
      job_id: jobId,
      status: 'running',
      slots: [],
      progress: job.reads === 1 ? 20 : 60,
      slicer: 'native',
      warnings: [],
    } satisfies SlicePreview)
  }
  return HttpResponse.json({
    job_id: jobId,
    status: 'completed',
    print_time_seconds: 1820,
    filament_used_g: 6.7,
    slots: (job.request.filament_plan?.slots ?? []).map((slot) => ({ slot_id: slot.slot_id, grams: 3.3 })),
    filament_changes: 6,
    slicer: 'native',
    warnings: [],
  } satisfies SlicePreview)
}

/** The run: 202 while it runs, then a result that names no Bambuddy (no queue on LAN). */
function startRun(subject: string, outputId: string, body: PrintRunRequest): HttpResponse<PrintRun> {
  const now = new Date().toISOString()
  const result: PrintRunResult = {
    route: 'slice_queue',
    library_file_id: 9900 + state.seq,
    printer_id: HERE,
    path: 'native',
    slice_job_id: null,
    sliced_library_file_id: null,
    queue_item_ids: [],
    copies: body.copies ?? 1,
    warnings: [],
    project_id: null,
    folder_id: body.folder_id ?? null,
    bambuddy_url: null,
    lan_awaiting_clear: false,
  }
  const run: PrintRun = {
    id: `hot-run-${++state.seq}`,
    subject,
    output_id: outputId,
    status: 'running',
    created_at: now,
    finished_at: null,
    result: null,
    error: null,
    may_have_queued: false,
    repeated: false,
  }
  state.runs.set(run.id, { ...run, status: 'succeeded', finished_at: now, result })
  state.printing = { name: 'name-keychain', reads: 0 }
  return HttpResponse.json(run, { status: 202 })
}

/** The H2C's card: idle, then once printed 30 %, 80 %, and finished. */
function printerNow(): PrinterNow {
  const idle: PrinterNow = {
    id: HOT_PRINTER.id,
    path: 'native',
    printer_id: HERE,
    name: HOT_PRINTER.name,
    activity: 'idle',
    state: 'IDLE',
    job_name: null,
    progress: null,
    layer: null,
    total_layers: null,
    remaining_minutes: null,
    hms: [],
    awaiting_plate_clear: false,
    archive_id: null,
    linked: false,
    bambuddy_url: null,
    bambuddy_archive_page: false,
    error: null,
  }
  const job = state.printing
  if (!job) return idle
  job.reads += 1
  if (job.reads > 2) return { ...idle, activity: 'finished', state: 'FINISH', job_name: job.name, progress: 100 }
  // Each report while it prints is news on the `printers` topic, as the LAN watch's is.
  setTimeout(() => emitRealtime('printer.report', ['printers', `printer:${HOT_PRINTER.id}`], { printer_id: HOT_PRINTER.id }), 0)
  const progress = job.reads === 1 ? 30 : 80
  return {
    ...idle,
    activity: 'printing',
    state: 'RUNNING',
    job_name: job.name,
    progress,
    layer: Math.round(progress / 2),
    total_layers: 50,
    remaining_minutes: progress === 30 ? 21 : 6,
  }
}

type Route = {
  method: string
  path: RegExp
  answer: (match: RegExpExecArray, request: Request, url: URL) => Response | Promise<Response> | undefined
}

const routes: Route[] = [
  {
    method: 'GET',
    path: /^\/settings$/,
    answer: () =>
      HttpResponse.json({
        ...mockSettings(),
        bambuddy_url: null,
        bambuddy_web_urls: null,
        slicer_backend: 'native',
        library_backend: 'native',
        store_backend: 's3',
      }),
  },
  { method: 'GET', path: /^\/printers$/, answer: () => HttpResponse.json([HOT_PRINTER]) },
  {
    method: 'GET',
    path: /^\/printers\/([^/]+)$/,
    answer: (match) => (match[1] === HOT_PRINTER.id ? HttpResponse.json(HOT_PRINTER) : problem(404, `no printer ${match[1]}`)),
  },
  {
    method: 'PUT',
    path: /^\/printers\/([^/]+)\/trays\/(\d+)\/(\d+)\/spool$/,
    answer: async (match, request) => {
      if (match[1] !== HOT_PRINTER.id) return problem(404, `no printer ${match[1]}`)
      const body = (await request.json()) as TraySpoolLink
      if (body.spool_id !== SHELF_SPOOL) return problem(404, `no spool ${body.spool_id}`)
      state.linked = true
      return HttpResponse.json(shelfSpool())
    },
  },
  { method: 'GET', path: /^\/print\/(?:outputs|library)\/[^/]+\/choices$/, answer: () => HttpResponse.json(hotChoices()) },
  { method: 'GET', path: /^\/print\/(?:outputs|library)\/[^/]+\/filaments$/, answer: () => HttpResponse.json(hotFilaments()) },
  {
    method: 'POST',
    path: /^\/print\/(outputs|library)\/([^/]+)\/preview-slice$/,
    answer: async (match, request) =>
      acceptOperation('preview_slice', `${match[1] === 'outputs' ? 'output' : 'library'}:${match[2]}`, {
        ...startSlice((await request.json()) as PrintRunRequest),
      }),
  },
  {
    method: 'GET',
    path: /^\/print\/(?:outputs|library)\/[^/]+\/preview-slices\/(\d+)\/toolpath$/,
    answer: (match) =>
      state.slices.has(Number(match[1]))
        ? new HttpResponse(demoToolpath(), { headers: { 'Content-Type': 'application/octet-stream' } })
        : undefined,
  },
  {
    method: 'GET',
    path: /^\/print\/(?:outputs|library)\/[^/]+\/preview-slices\/(\d+)$/,
    answer: (match) => (state.slices.has(Number(match[1])) ? readSlice(Number(match[1])) : undefined),
  },
  {
    method: 'POST',
    path: /^\/print\/(outputs|library)\/([^/]+)\/run$/,
    answer: async (match, request) => {
      const output = match[1] === 'outputs'
      return startRun(`${output ? 'output' : 'library'}:${match[2]}`, output ? match[2]! : `library:${match[2]}`, (await request.json()) as PrintRunRequest)
    },
  },
  {
    method: 'GET',
    path: /^\/print\/runs\/([^/]+)$/,
    answer: (match) => {
      const held = state.runs.get(match[1]!)
      return held ? HttpResponse.json(held) : undefined
    },
  },
  { method: 'GET', path: /^\/print\/printers\/now$/, answer: () => HttpResponse.json({ printers: [printerNow()] }) },
  {
    method: 'GET',
    path: /^\/print\/projects$/,
    answer: (_match, _request, url) =>
      url.searchParams.get('native') === 'true'
        ? HttpResponse.json({ projects: [], folders: HOT_FOLDERS, last_project_id: null, sort: 'touched' } satisfies ProjectChoices)
        : noBambuddy(),
  },
  {
    method: 'GET',
    path: /^\/print\/projects\/default$/,
    answer: (_match, _request, url) =>
      url.searchParams.get('native') === 'true' ? HttpResponse.json({ project_id: null, reason: 'none' }) : noBambuddy(),
  },
  // Bambuddy's own: a component that still asks is a bug the spec sees as a 409.
  { method: 'POST', path: /^\/print\/projects$/, answer: noBambuddy },
  { method: 'GET', path: /^\/print\/printers\/[^/]+\/camera(?:\/stream)?$/, answer: noBambuddy },
  { method: 'POST', path: /^\/print\/printers\/[^/]+\/trays\/\d+\/\d+\/spool$/, answer: noBambuddy },
]

/** Ahead of every other feature module while on (`features.ts`), so it is the one that answers. */
export const first = true

export const handlers = [
  http.all(`${base}/*`, ({ request }) => {
    if (!state.on) return undefined
    const url = new URL(request.url)
    const path = url.pathname.slice(base.length)
    for (const route of routes) {
      if (route.method !== request.method) continue
      const match = route.path.exec(path)
      if (match) return route.answer(match, request, url)
    }
    return undefined
  }),
]
