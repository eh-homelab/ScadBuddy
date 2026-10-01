import { HttpResponse, delay, http } from 'msw'
import type {
  ArrangeRequest,
  Asset,
  AssetUsage,
  AttachResult,
  ChoicesView,
  BoundingBox,
  CatalogueFont,
  Diagnostic,
  FilamentOptions,
  FontFamily,
  Job,
  CatalogueLibrary,
  ManifestObject,
  MediaView,
  ModelPatch,
  ModelPrintChoices,
  ModelSummary,
  MergePreview,
  ModelVersion,
  Output,
  OutputPlate,
  ParamPreset,
  ParamPresetCreate,
  ParamPresetDuplicate,
  ParamPresetUpdate,
  ParamValue,
  Plate,
  PlateFit,
  PrintProgress,
  PrintRunRequest,
  PrintRunResult,
  PrintOptions,
  PrintOptionsState,
  PrintOptionsUpdate,
  ProjectChoices,
  ProjectRequest,
  ProjectView,
  SendResult,
  Settings,
  SourceCheck,
  Upstream,
  UpstreamState,
  UpstreamStatus,
} from '../api/types'
import { editPath } from '../lib/deeplink'
import { emitRealtime, realtimeHandler } from './realtime'
import { mcpTokenHandlers, resetMcpTokens } from './mcpTokens'
import { mcpOidcHandlers, resetMcpOidcMock } from './mcpOidc'
import {
  MAX_META_BYTES,
  MAX_META_SIZE,
  MAX_SOURCE_CHARS,
  MAX_THUMBNAIL_BYTES,
  MAX_THUMBNAIL_SIZE,
} from '../lib/modelFolder'
import { resolveOptions } from '../lib/printOptions'
import { keychainGlb } from './glb'
import { aiPluginHandlers, resetAiPluginMocks } from './aiPlugins'
import { choicesView } from './choices'
import * as fixtures from './fixtures'

const base = '/api/v1'

/** `ModelPrintChoices()` on the backend: every field at its default. */
const NO_MODEL_CHOICES: Required<ModelPrintChoices> = {
  printer_id: null,
  filament_plan: [],
  nozzles: [],
  tier: null,
  process_name: null,
}

/** The backend's forget rule (`set_model_choices`): the body equals `ModelPrintChoices()`. */
function isNoModelChoices(choices: ModelPrintChoices): boolean {
  return (
    choices.printer_id == null &&
    !choices.filament_plan?.length &&
    !choices.nozzles?.length &&
    choices.tier == null &&
    choices.process_name == null
  )
}

const state = {
  models: [...fixtures.models] as ModelSummary[],
  schemas: { ...fixtures.schemas },
  outputs: [...fixtures.outputs] as Output[],
  sources: {
    'name-keychain': fixtures.keychainSource,
    [fixtures.BUILTIN_SLUG]: fixtures.keychainSource,
  } as Record<string, string>,
  /** #179 — README text per model; a model's `has_readme` follows it. */
  readmes: { 'name-keychain': fixtures.keychainReadme } as Record<string, string>,
  /** Per-template presets, shipped (`template-*`) and saved. */
  presets: structuredClone(fixtures.presets) as Record<string, ParamPreset[]>,
  settings: { ...fixtures.settings } as Settings,
  /** #349 — the agent's headless-browser setting (`ai_settings`), off by default. */
  headlessBrowser: false,
  printOptions: structuredClone(fixtures.printOptions) as PrintOptionsState,
  jobs: new Map<string, Job>(),
  /** spec 2026-09-27 §7 — what each arrange job was built from, read when it is saved. */
  arranged: new Map<string, { sources: string[]; manifest: ManifestObject[] }>(),
  /** The last `POST /outputs/arrange` body, for tests to read back. */
  lastArrange: null as ArrangeRequest | null,
  /** #78 — per-model printer and spools, the store's `model_print_choices`. */
  modelChoices: {} as Record<string, ModelPrintChoices>,
  /** #83 — the plate last printed on each printer, the store's `printer_bed_types`. */
  printerBedTypes: {} as Record<string, string>,
  projects: [...fixtures.projectViews] as ProjectView[],
  /** #79 — per-model projects. No global fallback, unlike the pipeline default. */
  lastProjectId: null as number | null,
  fonts: [...fixtures.fonts] as FontFamily[],
  /** #90 — one git history per model, newest first. */
  versions: structuredClone(fixtures.versions) as Record<string, ModelVersion[]>,
  /** #157 — each revision's `model.scad`, so a merge can read its `base`. */
  sourceAt: initialSourceAt(),
  fontCatalogue: fixtures.fontCatalogue.map((f) => ({ ...f })) as CatalogueFont[],
  libraries: structuredClone(fixtures.libraries) as CatalogueLibrary[],
  /** #204 — uploads for `file` parameters, keyed by their SHA-256 id. */
  assets: new Map<string, { meta: Asset; bytes: ArrayBuffer }>(),
  /** #237 — other files a duplicate's merge takes or keeps; none unless a test sets them. */
  mergeFiles: {} as Record<string, MergeFiles>,
  /** #289 — per-template plates of a multi-plate render; none unless a test sets them. */
  plates: {} as Record<string, NonNullable<Job['plates']>>,
  /** A template pipeline's outputs on every finished render of a slug; none unless set. */
  jobOutputs: {} as Record<string, NonNullable<Job['outputs']>>,
  /** #274 — uploaded media bytes by `<slug>/<file>`; the fixtures' are served by kind. */
  mediaFiles: new Map<string, ArrayBuffer>(),
  catalogueOffline: false,
  sidebarLinkId: 0,
  seq: 0,
  /**
   * Models whose default-render preview is "rendering": the backend makes one in the
   * background for a model created with no thumbnail, so the create answers without
   * it and a later read has it (`landPreviews`).
   */
  pendingPreviews: new Set<string>(),
}

/** Milliseconds a mock render spends pending, then running. */
export const MOCK_JOB_STEP_MS = 15

/**
 * #267 — a mock render moves on by itself, as a real one does, and announces each
 * state over the mock socket (`emitRealtime`), as `render/jobs.py` publishes it.
 * `GET /jobs/:id` only reports.
 */
function runJob(jobId: string): void {
  const announce = (kind: string) => {
    const job = state.jobs.get(jobId)
    if (job) emitRealtime(kind, [`job:${jobId}`], { job_id: jobId, slug: job.slug })
  }
  // Ids restart at every resetMockState, so a timer left by an earlier test checks
  // it is still acting on the job it was started for.
  const started = state.jobs.get(jobId)
  setTimeout(() => {
    const job = state.jobs.get(jobId)
    if (!job || job !== started || job.status !== 'pending') return
    job.status = 'running'
    job.log_tail = ['Compiling design (CSG Tree generation)...']
    announce('job.running')
    emitRealtime('job.progress', [`job:${jobId}`], { job_id: jobId, slug: job.slug, stage: 'render' })
    setTimeout(() => {
      if (state.jobs.get(jobId) !== job || job.status !== 'running') return
      if (String(job.params?.['name'] ?? '').toLowerCase() === fixtures.FAILING_NAME) {
        job.status = 'failed'
        job.error = 'openscad exited with 1'
        job.log_tail = fixtures.OPENSCAD_LOG_TAIL
        announce('job.failed')
        return
      }
      if (String(job.params?.['name'] ?? '').toLowerCase() === fixtures.PICTURELESS_NAME) {
        job.status = 'failed'
        job.error = 'openscad exited with 1'
        job.log_tail = [
          "ERROR: Can't open file '/data/models/name-keychain/pic.svg', import() at line 12",
          'Current top level object is empty.',
        ]
        job.warnings = fixtures.FAILED_JOB_WARNINGS
        announce('job.failed')
        return
      }
      job.status = 'done'
      job.bbox_mm = bboxOf(job.params ?? {})
      job.colors = colorsOf(job.slug, job.params ?? {})
      job.plates = state.plates[job.slug] ?? []
      if (state.jobOutputs[job.slug]) job.outputs = state.jobOutputs[job.slug]
      job.preview_url = `${base}/jobs/${job.id}/preview.glb`
      job.log_tail = ['Geometries in cache: 12', 'Total rendering time: 0:00:00.412']
      job.notes =
        String(job.params?.['name'] ?? '').toLowerCase() === fixtures.NOTED_NAME
          ? fixtures.TEMPLATE_NOTES
          : []
      job.warnings =
        String(job.params?.['name'] ?? '').toLowerCase() === fixtures.WARNED_NAME
          ? fixtures.JOB_WARNINGS
          : []
      announce('job.done')
    }, MOCK_JOB_STEP_MS)
  }, MOCK_JOB_STEP_MS)
}

/** Reset every mutable fixture. Call between tests. */
export function resetMockState(): void {
  resetAiPluginMocks()
  resetMcpOidcMock()
  state.models = fixtures.models.map((m) => ({ ...m }))
  state.schemas = { ...fixtures.schemas }
  state.outputs = fixtures.outputs.map((o) => ({ ...o }))
  state.sources = {
    'name-keychain': fixtures.keychainSource,
    [fixtures.BUILTIN_SLUG]: fixtures.keychainSource,
  }
  state.readmes = { 'name-keychain': fixtures.keychainReadme }
  state.presets = structuredClone(fixtures.presets)
  state.settings = { ...fixtures.settings }
  state.headlessBrowser = false
  state.printOptions = structuredClone(fixtures.printOptions)
  state.jobs.clear()
  state.arranged.clear()
  state.lastArrange = null
  state.modelChoices = {}
  state.printerBedTypes = {}
  state.projects = fixtures.projectViews.map((p) => ({ ...p }))
  state.lastProjectId = null
  state.fonts = fixtures.fonts.map((f) => ({ ...f }))
  state.versions = structuredClone(fixtures.versions)
  state.sourceAt = initialSourceAt()
  state.fontCatalogue = fixtures.fontCatalogue.map((f) => ({ ...f }))
  state.libraries = structuredClone(fixtures.libraries)
  state.assets.clear()
  state.mergeFiles = {}
  state.plates = {}
  state.jobOutputs = {}
  state.mediaFiles.clear()
  state.catalogueOffline = false
  state.sidebarLinkId = 0
  state.seq = 0
  state.pendingPreviews.clear()
  resetMcpTokens()
}

/** As the backend's `PreviewScheduler.request`: queue a default render of `slug`. */
function renderPreviewLater(model: ModelSummary): void {
  if (!model.thumbnail_source) state.pendingPreviews.add(model.slug)
}

/**
 * The pending previews "finish": each model that still has no image of its own and no
 * output to fall back on shows its default-render preview, keyed by a new render id --
 * the precedence `Catalogue.thumbnail_source` applies. Called on every read of a model.
 */
function landPreviews(): void {
  for (const slug of state.pendingPreviews) {
    state.pendingPreviews.delete(slug)
    const model = state.models.find((m) => m.slug === slug)
    if (!model || model.thumbnail_source || state.outputs.some((o) => o.slug === slug)) continue
    state.seq += 1
    Object.assign(model, {
      has_thumbnail: true,
      thumbnail_source: 'preview',
      thumbnail_output_id: null,
      thumbnail_preview_id: state.seq.toString(16).padStart(16, '0'),
    })
  }
}

/** Replaces a template's presets, so a test can start at a state that is slow to build. */
export function setMockPresets(slug: string, presets: ParamPreset[]): void {
  state.presets[slug] = presets
}

type MergeFiles = Pick<MergePreview, 'taken' | 'kept'>

/**
 * #237 — the files besides `model.scad` that merging `slug`'s upstream takes (unchanged
 * here since `base`) or keeps (changed on both sides). The mock tracks no other files,
 * so without this both lists are empty.
 */
export function setMockMergeFiles(slug: string, files: MergeFiles): void {
  state.mergeFiles[slug] = files
}

/**
 * #289 — the plates every finished render of `slug` reports, as a template that asks for
 * more than one plate would (spec §6.4). Unset, a render is one plate: `plates: []`.
 */
export function setMockPlates(slug: string, plates: NonNullable<Job['plates']>): void {
  state.plates[slug] = plates
}

/** Every finished render of `slug` reports these outputs, as a template's pipeline does. */
export function setMockJobOutputs(slug: string, outputs: NonNullable<Job['outputs']>): void {
  state.jobOutputs[slug] = outputs
}

/** Makes `GET /fonts/catalogue` fail, which is the air-gapped case the picker falls back for. */
/** #274 — the deployment's `SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES`, which nothing else can change. */
export function setMockUploadLimit(bytes: number): void {
  state.settings = { ...state.settings, media_upload_max_bytes: bytes }
}

/** #279 — replaces a template's media list, e.g. with a video whose file is gone. */
export function setMockMedia(slug: string, media: MediaView[]): void {
  state.models = state.models.map((m) => (m.slug === slug ? { ...m, media } : m))
}

export function setCatalogueOffline(offline: boolean): void {
  state.catalogueOffline = offline
}

/** The body of the last `POST /outputs/arrange`, or null when none was sent. */
export function lastArrangeRequest(): ArrangeRequest | null {
  return state.lastArrange
}

function initialSourceAt(): Record<string, string> {
  return {
    [fixtures.versionIds.raised]: fixtures.keychainSource,
    [fixtures.versionIds.synced]: fixtures.keychainSource,
  }
}

/**
 * #179 — one details change: records its revision and replaces the model's record,
 * the way every catalogue change lands as a commit on the real backend.
 */
function reviseModel(
  slug: string,
  message: string,
  files: ModelVersion['files'],
  change: Partial<ModelSummary>,
): ModelSummary | null {
  const model = state.models.find((m) => m.slug === slug)
  if (!model) return null
  const version = recordVersion(slug, message, files)
  const updated = { ...model, ...change, version: version.commit }
  state.models = state.models.map((m) => (m.slug === slug ? updated : m))
  // As the API serves it: a duplicate's record carries its `upstream_state`.
  return view(updated)
}

/** Adds a revision to the head of a model's history and returns it. */
function recordVersion(
  slug: string,
  message: string,
  files: ModelVersion['files'],
): ModelVersion {
  state.seq += 1
  const sha = state.seq.toString(16).padStart(40, 'e')
  const entry: ModelVersion = {
    commit: sha,
    short: sha.slice(0, 7),
    author: 'ScadBuddy',
    date: new Date().toISOString(),
    message,
    files,
    current: true,
  }
  const existing = (state.versions[slug] ?? []).map((v) => ({ ...v, current: false }))
  state.versions[slug] = [entry, ...existing]
  const source = state.sources[slug]
  if (source !== undefined) state.sourceAt[sha] = source
  return entry
}

/**
 * #157 — `state_of` in `library/upstream.py`: the upstream's current revision is its
 * newest commit, and a duplicate whose `base` is that revision is current.
 */
function upstreamStateOf(model: ModelSummary): UpstreamState | null {
  const upstream = model.upstream
  if (!upstream) return null
  if (!state.models.some((m) => m.slug === upstream.id)) return 'gone'
  const revision = state.versions[upstream.id]?.[0]?.commit
  if (!revision || revision === upstream.base) return 'current'
  return revision === upstream.dismissed ? 'dismissed' : 'update'
}

/** Where an upstream lives in the models repository (`model_path`). */
function upstreamPath(id: string): string {
  return id.startsWith('builtin:') ? `_builtin/${id.slice('builtin:'.length)}` : id
}

/**
 * `_advance_base` in `library/catalogue.py`: a fresh upstream at `revision`, where it
 * lives now, with nothing dismissed. Used by a clean merge and a resolved one alike.
 */
function advanceBase(upstream: Upstream, revision: string): Upstream {
  return { id: upstream.id, path: upstreamPath(upstream.id), base: revision, dismissed: null }
}

/** A record as the API serves it: a duplicate's carries its `upstream_state`. */
function view(model: ModelSummary): ModelSummary {
  const upstreamState = upstreamStateOf(model)
  return upstreamState ? { ...model, upstream_state: upstreamState } : model
}

function eol(text: string): string {
  return text === '' || text.endsWith('\n') ? text : `${text}\n`
}

/**
 * The mock's `git merge-file -p --diff3`: a side that did not change takes the other
 * side, and when both changed the whole file is one conflict. Enough to drive both
 * paths without shipping a real three-way merge.
 */
function planMerge(slug: string, model: ModelSummary): MergePreview {
  const upstream = model.upstream as Upstream
  const ours = state.sources[slug] ?? ''
  const baseSource = (upstream.base && state.sourceAt[upstream.base]) || ''
  const theirs = state.sources[upstream.id] ?? ''
  // `diff_dirs` in `library/history.py`: headed by the upstream's slug, `_builtin/` aside.
  const patch = sourcePatch(upstream.id.replace(/^builtin:/, ''), baseSource, theirs)
  const { taken = [], kept = [] } = state.mergeFiles[slug] ?? {}
  const plan = { ours, base: baseSource, theirs, patch, taken, kept }
  if (ours === baseSource || ours === theirs) return { ...plan, merged: theirs, clean: true }
  if (theirs === baseSource) return { ...plan, merged: ours, clean: true }
  const merged =
    `<<<<<<< ${slug}/model.scad\n${eol(ours)}` +
    `||||||| ${upstream.id} at base\n${eol(baseSource)}` +
    `=======\n${eol(theirs)}>>>>>>> ${upstream.id}/model.scad\n`
  return { ...plan, merged, clean: false }
}

/** `has_conflict_markers` in `library/upstream.py`. */
function hasConflictMarkers(source: string): boolean {
  return /^(<{7}|\|{7}|={7}|>{7})(?: |$)/m.test(source)
}

/**
 * One hunk around whatever changed between two sources: what the version diff route
 * serves for a revision the fixtures carry no recorded patch for.
 */
function sourcePatch(slug: string, before: string, after: string): string {
  if (before === after) return ''
  const a = before.replace(/\n$/, '').split('\n')
  const b = after.replace(/\n$/, '').split('\n')
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1
  let end = 0
  while (
    end < a.length - start &&
    end < b.length - start &&
    a[a.length - 1 - end] === b[b.length - 1 - end]
  ) {
    end += 1
  }
  const removed = a.slice(start, a.length - end)
  const added = b.slice(start, b.length - end)
  return [
    `diff --git a/${slug}/model.scad b/${slug}/model.scad`,
    `--- a/${slug}/model.scad`,
    `+++ b/${slug}/model.scad`,
    `@@ -${start + 1},${removed.length} +${start + 1},${added.length} @@`,
    ...removed.map((line) => `-${line}`),
    ...added.map((line) => `+${line}`),
    '',
  ].join('\n')
}

/** A duplicate an upstream action applies to, or the problem the backend answers. */
function upstreamAction(slug: string, allowed: UpstreamState[], refusal: string) {
  const model = state.models.find((m) => m.slug === slug)
  if (!model) return problem(404, 'Not Found', `no model named '${slug}'`)
  const upstream = model.upstream
  if (!upstream) {
    return problem(404, 'Not Found', `'${slug}' is not a duplicate, so it has no upstream`)
  }
  const upstreamState = upstreamStateOf(model) as UpstreamState
  if (!allowed.includes(upstreamState)) {
    return problem(409, 'Conflict', `'${slug}' ${refusal}`, { state: upstreamState })
  }
  const revision = state.versions[upstream.id]?.[0]?.commit ?? ''
  return { model, upstream, revision }
}

/** Rewrites a duplicate's `upstream` in `model.json` as one commit. */
function writeUpstream(model: ModelSummary, upstream: Upstream | null, message: string) {
  const version = recordVersion(model.slug, message, [{ status: 'M', path: 'model.json' }])
  const updated = { ...model, upstream, version: version.commit, updated_at: version.date }
  state.models = state.models.map((m) => (m.slug === model.slug ? updated : m))
  return view(updated)
}

/** Job and output ids are 32 hex characters — the routes reject anything else. */
function nextHexId(): string {
  state.seq += 1
  return state.seq.toString(16).padStart(32, '0')
}

function nextNumber(): number {
  state.seq += 1
  return 8800 + state.seq
}

/**
 * #316 — the output's library copy in `folderId` (the inbox when null), recording a new
 * one when there is none. The mock has one target, so the folder alone is the key.
 */
function copyIn(output: Output, folderId: number | null): number {
  const folder = folderId ?? state.settings.library_folder_id ?? null
  const existing = (output.library_files ?? []).find((copy) => copy.folder_id === folder)
  if (existing) return existing.id
  const id = nextNumber()
  state.outputs = state.outputs.map((o) =>
    o.id === output.id
      ? {
          ...o,
          library_files: [
            ...(o.library_files ?? []),
            { id, folder_id: folder, target_key: 'Bambu Lab H2C', sliced: [] },
          ],
        }
      : o,
  )
  return id
}

function num(params: Record<string, ParamValue>, key: string, fallback: number): number {
  const value = params[key]
  return typeof value === 'number' ? value : fallback
}

function colorsOf(slug: string, params: Record<string, ParamValue>): string[] {
  const schema = state.schemas[slug]
  if (!schema) return []
  const colors = (schema.parameters ?? [])
    .filter((param) => param.type === 'color')
    .map((param) => String(params[param.name] ?? param.initial))
  return colors.length > 0 ? colors : ['#9AA4B2']
}

/** Cheap stand-in for the real render: geometry that actually tracks the parameters. */
function bboxOf(params: Record<string, ParamValue>): BoundingBox {
  const name = String(params['name'] ?? 'Model')
  const textSize = num(params, 'text_size', 14)
  const padding = num(params, 'padding', 6)
  const thickness = num(params, 'thickness', 5.2)
  const depth = num(params, 'text_depth', 1.6)
  const unitsX = num(params, 'units_x', 0)
  if (unitsX > 0) {
    return fixtures.bbox(
      round(unitsX * 42),
      round(num(params, 'units_y', 1) * 42),
      round(num(params, 'height_units', 3) * 7),
    )
  }
  return fixtures.bbox(
    round(Math.max(name.length, 1) * textSize * 0.62 + padding * 2),
    round(textSize * 1.8 + padding * 2),
    round(thickness + depth),
  )
}

function plateFor(model: string | null): Plate | undefined {
  if (!model) return undefined
  const key = model.toLowerCase()
  return Object.entries(fixtures.plates).find(
    ([code, plate]) =>
      code.toLowerCase() === key || plate.name.toLowerCase() === key || plate.model?.toLowerCase() === key,
  )?.[1]
}

function round(value: number): number {
  return Math.round(value * 10) / 10
}

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

/** `library/assets.py`'s `sniff`: the kind comes from the bytes, never the name. */
function sniffAsset(bytes: ArrayBuffer): Asset['kind'] | undefined {
  const head = new Uint8Array(bytes.slice(0, 4096))
  if (PNG_MAGIC.every((byte, index) => head[index] === byte)) return 'png'
  return new TextDecoder().decode(head).includes('<svg') ? 'svg' : undefined
}

export const ASSET_REFUSAL = 'only SVG and PNG files can be attached'

/**
 * What `POST /models/{slug}/assets` does with a file, exported so a jsdom test can
 * reach it: jsdom's `File` cannot cross into Node's `fetch` as a multipart body.
 */
export async function storeAsset(file: Blob & { name?: string }): Promise<Asset | undefined> {
  const bytes = await file.arrayBuffer()
  const kind = sniffAsset(bytes)
  if (!kind) return undefined
  const meta: Asset = {
    id: await sha256Hex(bytes),
    name: file.name || `upload.${kind}`,
    kind,
    size: bytes.byteLength,
    width: kind === 'png' ? 96 : null,
    height: kind === 'png' ? 96 : null,
  }
  state.assets.set(meta.id, { meta, bytes })
  return meta
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function problem(status: number, title: string, detail?: string, extensions: object = {}) {
  return HttpResponse.json(
    { type: 'about:blank', title, status, detail, ...extensions },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}


/**
 * A body FastAPI refused while parsing it, before any route ran: `_validation_error`
 * in core/problems.py answers every one with the same detail and puts the reason in
 * `errors`, so a caller reads the field's message there, never in `detail`.
 */
function shapeRefusal(msg: string) {
  return problem(422, 'Unprocessable Content', 'the request did not match the expected shape', {
    errors: [{ loc: ['body', 'presets'], msg }],
  })
}

/**
 * As `_require_png`, on create and on PUT alike: a model thumbnail is a PNG by its
 * bytes -- not its name or type -- and at most `MAX_THUMBNAIL_BYTES`. The 422 the
 * backend answers, or null when the upload passes.
 */
async function thumbnailRefusal(upload: File) {
  const bytes = new Uint8Array(await upload.arrayBuffer())
  if (bytes.length < PNG_MAGIC.length || PNG_MAGIC.some((byte, i) => bytes[i] !== byte)) {
    return problem(422, 'Unprocessable Content', 'the thumbnail is not a PNG')
  }
  if (bytes.length > MAX_THUMBNAIL_BYTES) {
    return problem(
      422,
      'Unprocessable Content',
      `the thumbnail is too large: ${bytes.length} bytes, ` +
        `and a thumbnail is at most ${MAX_THUMBNAIL_BYTES} bytes (${MAX_THUMBNAIL_SIZE})`,
    )
  }
  return null
}

/**
 * A multipart text field as the backend receives it: FastAPI reads an empty string
 * as the field being absent.
 */
function formText(form: FormData, name: string): string | undefined {
  const value = form.get(name)
  return typeof value === 'string' && value !== '' ? value : undefined
}

/**
 * `_parse_tags`: a JSON array, or a comma-separated list; blank is no tags. A string
 * comes back when the backend would refuse the field, and is its 422 detail.
 */
function parseFormTags(raw: string | undefined): string[] | undefined | string {
  if (raw === undefined) return undefined
  const text = raw.trim()
  // Blank is absent, so the model.json's tags stand; an explicit `[]` still clears.
  if (!text) return undefined
  if (text.startsWith('[')) {
    let decoded: unknown
    try {
      decoded = JSON.parse(text)
    } catch {
      return 'tags is not valid JSON'
    }
    return Array.isArray(decoded) ? decoded.map(String) : 'tags must be a list'
  }
  return text
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean)
}

/** The value unless it is blank or only whitespace, which counts as absent. */
function nonBlank(value: string | undefined): string | undefined {
  return value?.trim() ? value : undefined
}

/** `_first_name`: the first candidate that is not blank, stripped; the slug never is. */
function firstName(...candidates: (string | undefined)[]): string {
  for (const candidate of candidates) {
    if (candidate?.trim()) return candidate.trim()
  }
  return ''
}

// ── #274: media, as `library/media.py` and `api/media.py` hold it ────────────

const MiB = 1024 * 1024
/** `MAX_IMAGE_BYTES`: images are committed to the template's history. */
const MAX_MEDIA_IMAGE_BYTES = 10 * MiB
const MEDIA_ACCEPTED = 'a PNG, JPEG or WebP image, or an MP4 or WebM video'
const IMMUTABLE_CACHE_CONTROL = 'private, max-age=31536000, immutable'

interface SniffedMedia {
  kind: MediaView['kind']
  extension: string
  contentType: string
}

/** `sniff_kind`: the type comes from the bytes, never the name. */
function sniffMedia(bytes: Uint8Array): SniffedMedia | undefined {
  const at = (offset: number, magic: number[]) => magic.every((byte, i) => bytes[offset + i] === byte)
  const ascii = (text: string) => [...text].map((c) => c.charCodeAt(0))
  if (at(0, PNG_MAGIC)) return { kind: 'image', extension: 'png', contentType: 'image/png' }
  if (at(0, [0xff, 0xd8, 0xff])) return { kind: 'image', extension: 'jpg', contentType: 'image/jpeg' }
  if (at(0, ascii('RIFF')) && at(8, ascii('WEBP'))) {
    return { kind: 'image', extension: 'webp', contentType: 'image/webp' }
  }
  if (at(4, ascii('ftyp'))) return { kind: 'video', extension: 'mp4', contentType: 'video/mp4' }
  if (at(0, [0x1a, 0x45, 0xdf, 0xa3])) {
    return { kind: 'video', extension: 'webm', contentType: 'video/webm' }
  }
  return undefined
}

function legacyItem(): MediaView {
  return {
    id: 'thumbnail',
    file: 'thumbnail.png',
    kind: 'image',
    caption: '',
    poster: null,
    missing: false,
    content_type: 'image/png',
    size: 67,
  }
}

/** A model's media: a model with no `media` lists its own thumbnail as the legacy item. */
function mediaOf(model: ModelSummary): MediaView[] {
  return model.media ?? (model.thumbnail_source === 'model' ? [legacyItem()] : [])
}

/** The first write gives the legacy item an id of its own, as `_edit_media` does. */
function converted(media: MediaView[]): MediaView[] {
  return media.map((item) => {
    if (item.id !== 'thumbnail') return item
    const id = nextMediaId()
    return { ...item, id, file: `${id}.png` }
  })
}

function nextMediaId(): string {
  return nextHexId().slice(-12)
}

/** Where the catalogue thumbnail comes from once the media is `media` (`_cover`). */
function coverOf(slug: string, media: MediaView[]): Partial<ModelSummary> {
  const cover = media.some((item) => !item.missing && (item.kind === 'image' || item.poster))
  if (cover) {
    return {
      has_thumbnail: true,
      thumbnail_source: 'model',
      thumbnail_output_id: null,
      thumbnail_preview_id: null,
    }
  }
  const first = state.outputs
    .filter((o) => o.slug === slug)
    .sort((a, b) => a.created_at.localeCompare(b.created_at))[0]
  return {
    has_thumbnail: first !== undefined,
    thumbnail_source: first ? 'output' : null,
    thumbnail_output_id: first?.id ?? null,
  }
}

type ChangedFiles = NonNullable<ModelVersion['files']>

function writeMedia(
  slug: string,
  message: string,
  files: ChangedFiles,
  media: MediaView[],
): ModelSummary | null {
  return reviseModel(slug, message, [{ status: 'M', path: 'model.json' }, ...files], {
    media,
    ...coverOf(slug, media),
  })
}

/** The model a media write is for, or the problem the backend answers first. */
function mediaTarget(slug: string, write: boolean): ModelSummary | Response {
  const refused = write ? refuseBuiltin(slug) : undefined
  if (refused) return refused
  const model = state.models.find((m) => m.slug === slug)
  return model ?? problem(404, 'Not Found', `no model named '${slug}'`)
}

function noMediaItem(slug: string, id: string) {
  return problem(404, 'Not Found', `'${slug}' has no media item '${id}'`)
}

function mediaBytes(slug: string, file: string, kind: MediaView['kind']): ArrayBuffer {
  const stored = state.mediaFiles.get(`${slug}/${file}`)
  if (stored) return stored
  const base64 = kind === 'video' ? fixtures.MEDIA_MP4_BASE64 : fixtures.MEDIA_PNG_BASE64
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0)).buffer
}

/** A multipart file part as `_staged` reads it: absent, or sent empty, is none. */
async function stagedPart(form: FormData, name: string) {
  const part = form.get(name)
  if (part === null || typeof part === 'string' || part.size === 0) return undefined
  const bytes = await part.arrayBuffer()
  return { bytes, size: bytes.byteLength, sniffed: sniffMedia(new Uint8Array(bytes.slice(0, 64))) }
}

/**
 * `require_mine` in `api/models.py`: a built-in is refused before the model is even
 * looked up, with the backend's problem (403 is not in its title table, so "Error").
 */
function refuseBuiltin(slug: string) {
  return slug.startsWith('builtin:')
    ? problem(403, 'Error', `'${slug}' is a built-in template and is read-only`)
    : undefined
}

/** `library/presets.py`'s limits: the longest name, and the most presets a template keeps. */
export const MAX_PRESET_NAME = 80
export const MAX_PRESETS = 200
/** `library/slugs.py`'s `SLUG_PATTERN` and `MAX_SLUG_LENGTH`: what a template preset's `id` may be. */
export const PRESET_ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/
export const MAX_PRESET_ID = 100
/** `library/presets.py`'s bounds on a preset's description and tags. */
export const MAX_PRESET_DESCRIPTION = 2000
export const MAX_PRESET_TAGS = 20
export const MAX_PRESET_TAG = 40

/**
 * `template_preset_keys` in `library/presets.py`: a preset's explicit id, else its name
 * as a slug with `-2`, `-3` on a clash, else `preset-<n>` when the name has no slug
 * characters. An explicit id is never reused by a derived key.
 */
function templatePresetKeys(presets: { id?: string | null; name: string }[]): string[] {
  const taken = new Set(presets.flatMap((preset) => (preset.id ? [preset.id] : [])))
  return presets.map((preset, index) => {
    if (preset.id) return preset.id
    const base = slugify(preset.name) || `preset-${index + 1}`
    let key = base
    for (let suffix = 2; taken.has(key); suffix++) key = `${base}-${suffix}`
    taken.add(key)
    return key
  })
}

/**
 * Why a preset's values are refused, as `require_valid_preset_params` words it, or
 * undefined: an unknown parameter, then each value's type as `build_defines` checks
 * it, then a dropdown value that is not one of its options.
 */
function valueRefusal(slug: string, params: Record<string, ParamValue>) {
  const byName = new Map((state.schemas[slug]?.parameters ?? []).map((p) => [p.name, p]))
  const unknown = Object.keys(params).filter((key) => !byName.has(key))
  if (unknown.length > 0) {
    return problem(422, 'Unprocessable Content', `unknown parameters: ${unknown.join(', ')}`, {
      parameters: unknown,
    })
  }
  // Then each value's type, as `build_defines` checks it, and a dropdown's options, as
  // the preset routes check them.
  for (const [key, value] of Object.entries(params)) {
    const param = byName.get(key)!
    const options = (param.options ?? []).map((option) => option.value)
    const expected =
      param.type === 'boolean'
        ? 'boolean'
        : ['string', 'color', 'font'].includes(param.type) ||
            (param.type === 'select' && options.some((option) => typeof option === 'string'))
          ? 'string'
          : 'number'
    if (typeof value !== expected) {
      return problem(
        422,
        'Unprocessable Content',
        `parameter '${key}' expects a ${expected}, got ${JSON.stringify(value)}`,
      )
    }
    if (options.length > 0 && !options.includes(value)) {
      return problem(
        422,
        'Unprocessable Content',
        `${JSON.stringify(value)} is not one of the options of '${key}'`,
        { parameters: [key] },
      )
    }
  }
  return undefined
}

/** Why a preset save is refused, as the server words it, or undefined. */
function presetRefusal(
  slug: string,
  name: string,
  params: Record<string, ParamValue>,
  own: string | null,
) {
  if (!name) return problem(422, 'Unprocessable Content', 'a preset needs a name')
  if (name.length > MAX_PRESET_NAME) {
    return problem(
      422,
      'Unprocessable Content',
      `a preset name is at most ${MAX_PRESET_NAME} characters`,
    )
  }
  const refused = valueRefusal(slug, params)
  if (refused) return refused
  // After the values, as the server checks them: they are validated in the route,
  // and only then does the store count the presets and compare the names.
  const saved = (state.presets[slug] ?? []).filter((p) => p.origin === 'mine')
  if (own === null && saved.length >= MAX_PRESETS) {
    return problem(409, 'Conflict', `a template keeps at most ${MAX_PRESETS} presets`)
  }
  const clash = (state.presets[slug] ?? []).some(
    (p) => p.id !== own && p.name.toLowerCase() === name.toLowerCase(),
  )
  if (clash) {
    return problem(409, 'Conflict', `'${slug}' already has a preset named '${name}'`, { name })
  }
  return undefined
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '')
}

function checkOf(source: string): SourceCheck {
  const failure = fixtures.mockParseError(source)
  if (!failure) {
    return {
      ok: true,
      checked: true,
      timed_out: false,
      diagnostics: [],
      log_tail: [],
      // The real check derives the schema in the same run, so it can say how many
      // parameters the source yields; the mock answers with the keychain's.
      parameters: fixtures.keychainSchema.parameters?.length ?? 0,
    }
  }
  const diagnostic: Diagnostic = {
    severity: 'error',
    message: 'Parser error: syntax error',
    line: failure.line,
    file: 'model.scad',
  }
  return {
    ok: false,
    checked: true,
    timed_out: false,
    diagnostics: [diagnostic],
    log_tail: [`ERROR: Parser error: syntax error in file model.scad, line ${failure.line}`],
  }
}

function refusal(check: SourceCheck) {
  return problem(422, 'Unprocessable Content', 'OpenSCAD could not parse the source', {
    diagnostics: check.diagnostics,
    log_tail: check.log_tail,
  })
}

/**
 * `params` sent beside `inputs` must be the same values, as the backend's
 * `normalize_inputs` holds them; an empty `params` is not a claim about them.
 */
function paramsClash(
  params: Record<string, ParamValue> | null | undefined,
  inputs: Record<string, unknown> | null | undefined,
): boolean {
  if (!params || !inputs || Object.keys(params).length === 0) return false
  const other = (inputs['params'] ?? {}) as Record<string, ParamValue>
  const names = Object.keys(params)
  return names.length !== Object.keys(other).length || names.some((name) => other[name] !== params[name])
}

/** Inputs as the backend records them: a given `v` is kept, a missing one is 0. */
function withVersion(inputs: Record<string, unknown>): Record<string, unknown> {
  return { ...inputs, v: inputs['v'] ?? 0 }
}

export const handlers = [
  realtimeHandler,
  // The agent service's plugin routes (#297), under /api/v1/ai.
  ...aiPluginHandlers,
  // The agent service's routes (#251); the rest of this list is the backend.
  ...mcpTokenHandlers,
  ...mcpOidcHandlers,

  http.get(`${base}/models`, () => {
    landPreviews()
    return HttpResponse.json(state.models.map(view))
  }),

  http.post(`${base}/models`, async ({ request }) => {
    if ((request.headers.get('content-type') ?? '').includes('application/json')) {
      const body = (await request.json()) as {
        name: string
        source: string
        description?: string
        tags?: string[]
        force?: boolean
        libraries?: string[]
      }
      const pastedSlug = slugify(body.name)
      if (!pastedSlug) return problem(422, 'Unprocessable Content', 'that name yields no slug')
      if (state.models.some((m) => m.slug === pastedSlug)) {
        return problem(409, 'Conflict', `a model named '${pastedSlug}' already exists`)
      }
      // #169 — curated names only, each pinned at the catalogue's ref.
      const named = [...new Set(body.libraries ?? [])]
      const unknown = named.filter((name) => !state.libraries.some((entry) => entry.name === name))
      if (unknown.length > 0) {
        return problem(
          422,
          'Unprocessable Content',
          `not in the library catalogue: ${unknown.join(', ')}`,
        )
      }
      const check = checkOf(body.source)
      if (!check.ok && !body.force) return refusal(check)
      const libraries = state.libraries
        .filter((entry) => named.includes(entry.name))
        .map((entry) => {
          state.seq += 1
          const commit = state.seq.toString(16).padStart(40, 'c')
          return { name: entry.name, url: entry.url, ref: entry.ref, commit }
        })
      const pasted: ModelSummary = {
        slug: pastedSlug,
        name: body.name,
        description: body.description ?? '',
        tags: body.tags ?? [],
        libraries,
        updated_at: new Date().toISOString(),
        has_thumbnail: false,
        has_readme: false,
        origin: 'mine',
      }
      state.models = [pasted, ...state.models]
      renderPreviewLater(pasted)
      // A forced save stores source OpenSCAD cannot parse, so no schema is derived —
      // the customizer then opens onto the 422 the real backend answers.
      if (check.ok) state.schemas[pastedSlug] = fixtures.keychainSchema
      state.sources[pastedSlug] = body.source
      await delay(120)
      return HttpResponse.json(pasted, { status: 201 })
    }

    const form = await request.formData()
    const file = form.get('file')
    const part = (name: string) => {
      const value = form.get(name)
      return value !== null && typeof value !== 'string' ? (value as File) : null
    }
    const meta = part('meta')
    const thumbnailPart = part('thumbnail')
    const readmePart = part('readme')
    // As `_read_meta_file`: a model.json that is not JSON, or not an object, is a 422.
    let metaFields: { name?: string; description?: string; tags?: string[] } | null = null
    if (meta) {
      // As `_read_meta_part`: the cap is checked before the part is decoded.
      if (meta.size > MAX_META_BYTES) {
        return problem(
          422,
          'Unprocessable Content',
          `the model.json is too large: ${meta.size} bytes, ` +
            `and a model.json is at most ${MAX_META_BYTES} bytes (${MAX_META_SIZE})`,
        )
      }
      let parsed: unknown
      try {
        parsed = JSON.parse(await meta.text())
      } catch {
        return problem(422, 'Unprocessable Content', 'the model.json is not valid JSON')
      }
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return problem(422, 'Unprocessable Content', 'the model.json is not an object')
      }
      metaFields = parsed as { name?: string; description?: string; tags?: string[] }
    }
    const tagsField = parseFormTags(formText(form, 'tags'))
    if (typeof tagsField === 'string') return problem(422, 'Unprocessable Content', tagsField)
    // Not `instanceof File`: the entry's class differs between the browser worker
    // and the Node interceptor, so it is duck-typed instead.
    const filename = typeof file === 'string' || file === null ? '' : ((file as File).name ?? '')
    if (!filename) {
      return problem(422, 'Missing file', 'Upload a .scad file.')
    }
    if (!filename.endsWith('.scad')) {
      return problem(415, 'Unsupported file type', 'ScadBuddy accepts .scad source files.')
    }
    // As `create_model`: the uploaded source is held to MAX_SOURCE_CHARS, in code points.
    const characters = [...(await (file as File).text())].length
    if (characters > MAX_SOURCE_CHARS) {
      return problem(
        422,
        'Unprocessable Content',
        `the source is too large: ${characters} characters, ` +
          `and this route reads at most ${MAX_SOURCE_CHARS}`,
      )
    }
    const slug = filename
      .replace(/\.scad$/, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '')
    if (thumbnailPart) {
      const refused = await thumbnailRefusal(thumbnailPart)
      if (refused) return refused
    }
    // As `create_model` resolves them: the form field, then the model.json, then
    // the default -- and a name is the first that is not blank, stripped.
    const model: ModelSummary = {
      slug,
      name: firstName(formText(form, 'name'), metaFields?.name, slug),
      // Blank is absent, as for the name; a non-blank description is kept as given.
      description: nonBlank(formText(form, 'description')) ?? metaFields?.description ?? '',
      tags: tagsField ?? metaFields?.tags ?? [],
      updated_at: new Date().toISOString(),
      has_thumbnail: thumbnailPart !== null,
      thumbnail_source: thumbnailPart ? 'model' : null,
      has_readme: readmePart !== null,
      origin: 'mine',
    }
    if (readmePart) state.readmes[slug] = await readmePart.text()
    state.models = [model, ...state.models.filter((m) => m.slug !== slug)]
    renderPreviewLater(model)
    state.schemas[slug] = fixtures.keychainSchema
    await delay(150)
    return HttpResponse.json(model, { status: 201 })
  }),

  // Mirrors the backend's resolvers (#153): https only, MakerWorld refused, anything
  // else taken as the file itself and named after it.
  http.post(`${base}/models/import`, async ({ request }) => {
    const body = (await request.json()) as { url: string; name?: string | null }
    await delay(120)
    let url: URL
    try {
      url = new URL(body.url.trim())
    } catch {
      return problem(422, 'Unprocessable Content', `'${body.url}' is not a URL`)
    }
    if (url.protocol !== 'https:') {
      return problem(422, 'Unprocessable Content', 'only https URLs can be imported')
    }
    const host = url.hostname.replace(/\.$/, '')
    if (host === 'makerworld.com' || host.endsWith('.makerworld.com')) {
      return problem(
        422,
        'Unprocessable Content',
        "MakerWorld only serves a model's files to a signed-in account, so ScadBuddy cannot " +
          'fetch them. Download the .scad from the model page and use Upload, or paste a ' +
          'link to the raw file instead.',
      )
    }
    const file = decodeURIComponent(url.pathname.split('/').pop() ?? '').replace(/\.scad$/i, '')
    const name = body.name || file || url.hostname
    const slug = slugify(name)
    if (state.models.some((m) => m.slug === slug)) {
      return problem(409, 'Conflict', `a model named '${slug}' already exists`)
    }
    const imported: ModelSummary = {
      slug,
      name,
      description: '',
      tags: [],
      origin_url: body.url,
      updated_at: new Date().toISOString(),
      has_thumbnail: false,
      has_readme: false,
      origin: 'mine',
    }
    state.models = [imported, ...state.models]
    renderPreviewLater(imported)
    state.schemas[slug] = fixtures.keychainSchema
    state.sources[slug] = 'width = 10;\ncube(width);\n'
    return HttpResponse.json(imported, { status: 201 })
  }),

  // #156 — any template, built-in or mine, copied to a new one of mine that records
  // it as `upstream`, with `base` its current revision (`catalogue.duplicate`).
  http.post(`${base}/models/:slug/duplicate`, async ({ params, request }) => {
    const id = String(params['slug'])
    const upstream = state.models.find((m) => m.slug === id)
    if (!upstream) return problem(404, 'Not Found', `no model named '${id}'`)
    const body = (await request.json()) as { name: string }
    const slug = slugify(body.name)
    if (!slug) {
      return problem(422, 'Unprocessable Content', `'${body.name}' does not yield a usable slug`)
    }
    if (state.models.some((m) => m.slug === slug)) {
      return problem(409, 'Conflict', `a model named '${slug}' already exists`)
    }
    const base = state.versions[id]?.[0]?.commit ?? null
    const source = state.sources[id]
    if (source !== undefined) state.sources[slug] = source
    const version = recordVersion(slug, `Duplicate ${id} as ${slug}`, [
      { status: 'A', path: 'model.scad' },
    ])
    const copy: ModelSummary = {
      ...upstream,
      slug,
      name: body.name,
      origin: 'mine',
      origin_url: null,
      // #179: the copy is the upstream's directory, so its thumbnail.png and
      // README.md come too; its outputs, and so any plate fallback, do not -- nor
      // its default-render preview, a derived file the copy gets rendered afresh.
      has_thumbnail: upstream.thumbnail_source === 'model',
      thumbnail_source: upstream.thumbnail_source === 'model' ? 'model' : null,
      thumbnail_output_id: null,
      thumbnail_preview_id: null,
      updated_at: version.date,
      version: version.commit,
      upstream: {
        id,
        path: upstreamPath(id),
        base,
      },
    }
    state.models = [copy, ...state.models]
    // #274: `duplicate` copies `media/`, videos (which are not in git) included.
    for (const [key, bytes] of [...state.mediaFiles]) {
      if (key.startsWith(`${id}/`)) state.mediaFiles.set(`${slug}/${key.slice(id.length + 1)}`, bytes)
    }
    renderPreviewLater(copy)
    const schema = state.schemas[id]
    if (schema) state.schemas[slug] = { ...schema, title: body.name }
    // #179: the copy is the upstream's directory, so its README comes too.
    const readme = state.readmes[id]
    if (readme !== undefined) state.readmes[slug] = readme
    // Its shipped presets are in the copied directory; the saved ones are copied.
    const copied = state.presets[id]
    if (copied) {
      state.presets[slug] = copied.map((preset) =>
        preset.origin === 'mine' ? { ...preset, id: nextHexId() } : { ...preset },
      )
    }
    await delay(120)
    return HttpResponse.json(view(copy), { status: 201 })
  }),

  // #157 — a duplicate's upstream, and taking, dismissing or detaching it.
  http.get(`${base}/models/:slug/upstream`, ({ params }) => {
    const slug = String(params['slug'])
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Not Found', `no model named '${slug}'`)
    if (!model.upstream) {
      return problem(404, 'Not Found', `'${slug}' is not a duplicate, so it has no upstream`)
    }
    const upstreamState = upstreamStateOf(model) as UpstreamState
    const status: UpstreamStatus = {
      state: upstreamState,
      upstream: model.upstream,
      revision:
        upstreamState === 'gone' ? null : (state.versions[model.upstream.id]?.[0]?.commit ?? null),
      preview:
        upstreamState === 'update' || upstreamState === 'dismissed' ? planMerge(slug, model) : null,
    }
    return HttpResponse.json(status)
  }),

  http.post(`${base}/models/:slug/upstream/merge`, async ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const found = upstreamAction(slug, ['update', 'dismissed'], 'has no upstream update to merge')
    if (found instanceof Response) return found
    const { model, upstream, revision } = found
    const plan = planMerge(slug, model)
    if (!plan.clean) {
      return problem(
        409,
        'Conflict',
        `the merge into '${slug}' has 1 conflict(s); resolve them and save with ` +
          `PUT /models/${slug}/source?merge_base=${revision}`,
        { merged: plan.merged, merge_base: revision, conflicts: 1, taken: plan.taken, kept: plan.kept },
      )
    }
    state.sources[slug] = plan.merged
    const version = recordVersion(slug, `Merge ${upstream.id} into ${slug}`, [
      { status: 'M', path: 'model.scad' },
    ])
    const updated: ModelSummary = {
      ...model,
      version: version.commit,
      updated_at: version.date,
      upstream: advanceBase(upstream, revision),
    }
    state.models = state.models.map((m) => (m.slug === slug ? updated : m))
    await delay(120)
    return HttpResponse.json({ model: view(updated), taken: plan.taken, kept: plan.kept })
  }),

  http.post(`${base}/models/:slug/upstream/dismiss`, ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const found = upstreamAction(slug, ['update', 'dismissed'], 'has no upstream update to dismiss')
    if (found instanceof Response) return found
    const { model, upstream, revision } = found
    return HttpResponse.json(
      writeUpstream(
        model,
        { ...upstream, dismissed: revision },
        `Dismiss ${upstream.id} update in ${slug}`,
      ),
    )
  }),

  http.post(`${base}/models/:slug/upstream/detach`, ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const found = upstreamAction(slug, ['gone'], 'stays linked: its upstream still exists')
    if (found instanceof Response) return found
    const { model, upstream } = found
    return HttpResponse.json(writeUpstream(model, null, `Detach ${slug} from ${upstream.id}`))
  }),

  http.post(`${base}/models/check`, async ({ request }) => {
    const body = (await request.json()) as { source: string; slug?: string | null }
    await delay(80)
    return HttpResponse.json(checkOf(body.source))
  }),

  http.get(`${base}/models/:slug/source`, ({ params }) => {
    const source = state.sources[String(params['slug'])]
    return source === undefined
      ? problem(404, 'Model not found')
      : HttpResponse.text(source, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
  }),

  http.put(`${base}/models/:slug/source`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Model not found')
    const body = (await request.json()) as {
      source: string
      force?: boolean
      message?: string | null
    }
    // #157 — `merge_base` saves the resolution of a conflicted upstream merge.
    const mergeBase = new URL(request.url).searchParams.get('merge_base')
    if (mergeBase !== null && hasConflictMarkers(body.source)) {
      return problem(
        422,
        'Unprocessable Content',
        'the source still has conflict markers; resolve every conflict first',
      )
    }
    const check = checkOf(body.source)
    if (!check.ok && !body.force) return refusal(check)
    const upstream = model.upstream
    if (mergeBase !== null) {
      if (!upstream) {
        return problem(
          409,
          'Conflict',
          `'${slug}' is not a duplicate, so it has no merge to resolve`,
        )
      }
      if (!(state.versions[upstream.id] ?? []).some((v) => v.commit === mergeBase)) {
        return problem(
          422,
          'Unprocessable Content',
          `${mergeBase} is not a revision of ${upstream.id}`,
        )
      }
    }
    state.sources[slug] = body.source
    if (!check.ok) delete state.schemas[slug]
    const resolved = mergeBase !== null && upstream ? advanceBase(upstream, mergeBase) : null
    const message =
      body.message || (resolved ? `Merge ${resolved.id} into ${slug}` : `Edit ${slug} source`)
    const version = recordVersion(slug, message, [{ status: 'M', path: 'model.scad' }])
    const updated = {
      ...model,
      version: version.commit,
      updated_at: version.date,
      ...(resolved ? { upstream: resolved } : {}),
    }
    state.models = state.models.map((m) => (m.slug === slug ? updated : m))
    await delay(120)
    return HttpResponse.json(view(updated))
  }),

  http.patch(`${base}/models/:slug`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const { presets: defined, ...patch } = (await request.json()) as ModelPatch
    // #326: the template's own presets, replaced whole, checked in the server's order.
    // First the body's shape -- each preset's name and id, then the list's length and
    // uniqueness -- which is FastAPI parsing it into `ModelPatch` before the route runs,
    // so it is refused (422) even for a built-in or a model that is not there -- with
    // the generic detail every `RequestValidationError` gets, the reason in `errors`.
    const cleaned: NonNullable<typeof defined> = []
    if (defined) {
      for (const preset of defined) {
        // The raw length first, as pydantic checks `max_length` before `_clean_name`
        // collapses the whitespace.
        if (preset.name.length > MAX_PRESET_NAME) {
          return shapeRefusal(`a preset name is at most ${MAX_PRESET_NAME} characters`)
        }
        const name = preset.name.trim().replace(/\s+/g, ' ')
        if (!name) return shapeRefusal('a preset needs a name')
        if (
          preset.id !== undefined &&
          preset.id !== null &&
          (!PRESET_ID_PATTERN.test(preset.id) || preset.id.length > MAX_PRESET_ID)
        ) {
          return shapeRefusal(`'${preset.id}' is not a preset id`)
        }
        if ((preset.description ?? '').length > MAX_PRESET_DESCRIPTION) {
          return shapeRefusal(
            `a preset description is at most ${MAX_PRESET_DESCRIPTION} characters`,
          )
        }
        const tags = preset.tags ?? []
        if (tags.length > MAX_PRESET_TAGS || tags.some((tag) => tag.length > MAX_PRESET_TAG)) {
          return shapeRefusal(
            `a preset has at most ${MAX_PRESET_TAGS} tags of at most ${MAX_PRESET_TAG} characters`,
          )
        }
        cleaned.push({ ...preset, name })
      }
      if (cleaned.length > MAX_PRESETS) {
        return shapeRefusal(`a template defines at most ${MAX_PRESETS} presets`)
      }
      const names = new Set<string>()
      const ids = new Set<string>()
      for (const preset of cleaned) {
        const folded = preset.name.toLowerCase()
        if (names.has(folded)) {
          return shapeRefusal(`two presets are named '${preset.name}'`)
        }
        names.add(folded)
        if (preset.id) {
          if (ids.has(preset.id)) {
            return shapeRefusal(`two presets have the id '${preset.id}'`)
          }
          ids.add(preset.id)
        }
      }
    }
    // Then the route: a built-in is read-only, and a missing model is a 404, before the
    // values are checked against its schema by `require_valid_preset_params`.
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    if (defined) {
      for (const preset of cleaned) {
        const refused = valueRefusal(slug, preset.params ?? {})
        if (refused) return refused
      }
      // A name is one preset's in the picker: none of the template's is a saved one's.
      const saved = (state.presets[slug] ?? []).filter((preset) => preset.origin === 'mine')
      const clash = cleaned.find((preset) =>
        saved.some((other) => other.name.toLowerCase() === preset.name.toLowerCase()),
      )
      if (clash) {
        return problem(
          409,
          'Conflict',
          `'${slug}' already has a saved preset named '${clash.name}'`,
          { name: clash.name },
        )
      }
      const keys = templatePresetKeys(cleaned)
      const shipped: ParamPreset[] = cleaned.map((preset, index) => ({
        id: `template-${keys[index]}`,
        name: preset.name,
        origin: 'template',
        params: preset.params ?? {},
      }))
      state.presets[slug] = [...shipped, ...saved]
    }
    const change = Object.fromEntries(
      Object.entries(patch).filter(([, value]) => value !== null && value !== undefined),
    ) as Partial<ModelSummary>
    const updated = reviseModel(slug, `Update ${slug} metadata`, [
      { status: 'M', path: 'model.json' },
    ], change)
    return updated ? HttpResponse.json(updated) : problem(404, 'Model not found')
  }),

  // #274 — a template's images and videos (`api/media.py`).
  http.get(`${base}/models/:slug/media/:id`, ({ params }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    const model = mediaTarget(slug, false)
    if (model instanceof Response) return model
    const item = mediaOf(model).find((entry) => entry.id === id)
    if (!item || item.missing) return noMediaItem(slug, id)
    return HttpResponse.arrayBuffer(mediaBytes(slug, item.file, item.kind), {
      headers: {
        'Content-Type': item.content_type,
        'Cache-Control': item.id === 'thumbnail' ? 'no-cache' : IMMUTABLE_CACHE_CONTROL,
      },
    })
  }),

  http.get(`${base}/models/:slug/media/:id/poster`, ({ params }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    const model = mediaTarget(slug, false)
    if (model instanceof Response) return model
    const item = mediaOf(model).find((entry) => entry.id === id)
    if (!item?.poster) return problem(404, 'Not Found', `'${slug}' has no poster for '${id}'`)
    return HttpResponse.arrayBuffer(mediaBytes(slug, item.poster, 'image'), {
      headers: { 'Content-Type': 'image/png', 'Cache-Control': IMMUTABLE_CACHE_CONTROL },
    })
  }),

  http.post(`${base}/models/:slug/media`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const model = mediaTarget(slug, true)
    if (model instanceof Response) return model
    const form = await request.formData()
    const upload = await stagedPart(form, 'file')
    if (!upload) return problem(422, 'Unprocessable Content', 'the upload has no `file` part')
    const limit = state.settings.media_upload_max_bytes
    if (upload.size > limit) {
      return problem(
        413,
        'Content Too Large',
        `a media upload is at most ${limit / MiB} MB (SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES), and this one is larger`,
      )
    }
    const poster = await stagedPart(form, 'poster')
    for (const [part, what] of [[upload, 'the upload'], [poster, 'the poster']] as const) {
      if (part && !part.sniffed) {
        return problem(415, 'Unsupported Media Type', `${what} is not ${MEDIA_ACCEPTED}`)
      }
      if (part?.sniffed?.kind === 'image' && part.size > MAX_MEDIA_IMAGE_BYTES) {
        return problem(
          413,
          'Content Too Large',
          'an image is at most 10 MB: images are kept in the template\'s history',
        )
      }
    }
    const kind = upload.sniffed!
    if (poster && kind.kind !== 'video') {
      return problem(422, 'Unprocessable Content', 'only a video takes a poster')
    }
    if (poster && poster.sniffed!.kind !== 'image') {
      return problem(415, 'Unsupported Media Type', 'the poster is not a PNG, JPEG or WebP image')
    }
    const id = nextMediaId()
    const item: MediaView = {
      id,
      file: `${id}.${kind.extension}`,
      kind: kind.kind,
      caption: formText(form, 'caption') ?? '',
      poster: poster ? `${id}-poster.${poster.sniffed!.extension}` : null,
      missing: false,
      content_type: kind.contentType,
      size: upload.size,
    }
    state.mediaFiles.set(`${slug}/${item.file}`, upload.bytes)
    if (poster && item.poster) state.mediaFiles.set(`${slug}/${item.poster}`, poster.bytes)
    // Videos are not committed (the models' `.gitignore`); images and posters are.
    const files: ChangedFiles = [
      ...(item.kind === 'image' ? [{ status: 'A', path: `media/${item.file}` }] : []),
      ...(item.poster ? [{ status: 'A', path: `media/${item.poster}` }] : []),
    ]
    const updated = writeMedia(slug, `Add media to ${slug}`, files, [
      ...converted(mediaOf(model)),
      item,
    ])
    await delay(120)
    return updated ? HttpResponse.json(updated) : problem(404, 'Not Found')
  }),

  http.patch(`${base}/models/:slug/media/:id`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    const model = mediaTarget(slug, true)
    if (model instanceof Response) return model
    const { caption } = (await request.json()) as { caption: string }
    const media = mediaOf(model)
    if (!media.some((item) => item.id === id)) return noMediaItem(slug, id)
    const updated = writeMedia(
      slug,
      `Caption ${slug} media`,
      [],
      converted(media.map((item) => (item.id === id ? { ...item, caption } : item))),
    )
    return HttpResponse.json(updated)
  }),

  http.put(`${base}/models/:slug/media/order`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const model = mediaTarget(slug, true)
    if (model instanceof Response) return model
    const { ids } = (await request.json()) as { ids: string[] }
    const media = mediaOf(model)
    const byId = new Map(media.map((item) => [item.id, item]))
    const permutation =
      ids.length === media.length && new Set(ids).size === ids.length && ids.every((id) => byId.has(id))
    if (!permutation) {
      return problem(422, 'Unprocessable Content', 'the order must name every media item exactly once')
    }
    const updated = writeMedia(
      slug,
      `Reorder ${slug} media`,
      [],
      converted(ids.map((id) => byId.get(id)!)),
    )
    return HttpResponse.json(updated)
  }),

  http.delete(`${base}/models/:slug/media/:id`, ({ params }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    const model = mediaTarget(slug, true)
    if (model instanceof Response) return model
    const media = mediaOf(model)
    const gone = media.find((item) => item.id === id)
    if (!gone) return noMediaItem(slug, id)
    const files: ChangedFiles = [
      ...(gone.kind === 'image' ? [{ status: 'D', path: gone.id === 'thumbnail' ? gone.file : `media/${gone.file}` }] : []),
      ...(gone.poster ? [{ status: 'D', path: `media/${gone.poster}` }] : []),
    ]
    const updated = writeMedia(
      slug,
      `Remove media from ${slug}`,
      files,
      converted(media.filter((item) => item.id !== id)),
    )
    return HttpResponse.json(updated)
  }),

  // Multipart with a `file` part, like the output thumbnail PUT.
  http.put(`${base}/models/:slug/thumbnail`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    const upload = (await request.formData()).get('file')
    if (upload === null || typeof upload === 'string') {
      return problem(422, 'Unprocessable Content', 'the upload needs a file part')
    }
    const notPng = await thumbnailRefusal(upload as File)
    if (notPng) return notPng
    const model = state.models.find((m) => m.slug === slug)!
    const had = model.thumbnail_source === 'model'
    // #274: with media, the PNG is the cover: it replaces a first image, or goes in
    // front of a first video, under a new id (`write_thumbnail`).
    const media = mediaOf(model)
    const legacy = media.length === 0 || (media.length === 1 && media[0]!.id === 'thumbnail')
    const id = nextMediaId()
    const cover: MediaView = { ...legacyItem(), id, file: `${id}.png` }
    const updated = reviseModel(
      slug,
      `Set ${slug} thumbnail`,
      [{ status: had ? 'M' : 'A', path: 'thumbnail.png' }],
      // As `write_thumbnail`: an image of its own drops any default-render preview.
      {
        has_thumbnail: true,
        thumbnail_source: 'model',
        thumbnail_output_id: null,
        media: legacy
          ? [legacyItem()]
          : [cover, ...converted(media[0]!.kind === 'image' ? media.slice(1) : media)],
        thumbnail_preview_id: null,
      },
    )
    return updated ? HttpResponse.json(updated) : problem(404, 'Model not found')
  }),

  http.delete(`${base}/models/:slug/thumbnail`, ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Model not found')
    const media = mediaOf(model)
    if (model.thumbnail_source !== 'model' || media[0]?.kind !== 'image') {
      return problem(404, 'Not Found', `'${slug}' has no thumbnail of its own to remove`)
    }
    // #274: with more media, the next item may be the cover now (`delete_thumbnail`).
    if (media.length > 1) {
      return HttpResponse.json(
        writeMedia(slug, `Remove ${slug} thumbnail`, [], converted(media.slice(1))),
      )
    }
    // The fixtures' generated models fall back to their first output's plate image.
    // Which is the first output: the one whose plate image the backend serves.
    const first = state.outputs
      .filter((o) => o.slug === slug)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))[0]
    const updated = reviseModel(slug, `Remove ${slug} thumbnail`, [
      { status: 'D', path: 'thumbnail.png' },
    ], {
      media: [],
      has_thumbnail: first !== undefined,
      thumbnail_source: first ? 'output' : null,
      thumbnail_output_id: first?.id ?? null,
    })
    return HttpResponse.json(updated)
  }),

  http.get(`${base}/models/:slug/readme`, ({ params }) => {
    const slug = String(params['slug'])
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    const readme = state.readmes[slug]
    return readme === undefined
      ? problem(404, 'Not Found', `'${slug}' has no README`)
      : HttpResponse.text(readme, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } })
  }),

  http.put(`${base}/models/:slug/readme`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const { content } = (await request.json()) as { content: string }
    const had = state.readmes[slug] !== undefined
    const updated = reviseModel(slug, `Set ${slug} README`, [
      { status: had ? 'M' : 'A', path: 'README.md' },
    ], { has_readme: true })
    if (!updated) return problem(404, 'Model not found')
    state.readmes[slug] = content
    return HttpResponse.json(updated)
  }),

  http.delete(`${base}/models/:slug/readme`, ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    if (state.readmes[slug] === undefined) {
      return problem(404, 'Not Found', `'${slug}' has no README to remove`)
    }
    delete state.readmes[slug]
    const updated = reviseModel(slug, `Remove ${slug} README`, [
      { status: 'D', path: 'README.md' },
    ], { has_readme: false })
    return HttpResponse.json(updated)
  }),

  http.get(`${base}/models/:slug`, ({ params }) => {
    landPreviews()
    const model = state.models.find((m) => m.slug === params['slug'])
    return model ? HttpResponse.json(view(model)) : problem(404, 'Model not found')
  }),

  http.delete(`${base}/models/:slug`, ({ params, request }) => {
    const refused = refuseBuiltin(String(params['slug']))
    if (refused) return refused
    if (!state.models.some((m) => m.slug === params['slug'])) {
      return problem(404, 'Not Found', `no model named '${String(params['slug'])}'`)
    }
    const slugs = state.models.filter((m) => m.upstream?.id === params['slug']).map((m) => m.slug)
    if (slugs.length && new URL(request.url).searchParams.get('force') !== 'true') {
      return problem(
        409,
        'Conflict',
        `${slugs.length} template(s) are duplicates of '${String(params['slug'])}' and would ` +
          'lose their upstream; delete with ?force=true to go ahead',
        { duplicates: slugs.length, slugs },
      )
    }
    state.models = state.models.filter((m) => m.slug !== params['slug'])
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${base}/models/:slug/versions`, ({ params }) => {
    const slug = String(params['slug'])
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    return HttpResponse.json(state.versions[slug] ?? [])
  }),

  http.get(`${base}/models/:slug/versions/:commit/diff`, ({ params, request }) => {
    const slug = String(params['slug'])
    const commit = String(params['commit'])
    const entries = state.versions[slug] ?? []
    const head = entries.find((v) => v.commit === commit)
    if (!head) return problem(404, 'Revision not found')
    // Entries are newest-first, so "everything between head and base" is the slice
    // from head up to (not including) base. Concatenating their patches is close
    // enough for a mock and keeps the text recognisable in assertions.
    const requested = new URL(request.url).searchParams.get('base')
    const headIndex = entries.findIndex((v) => v.commit === commit)
    const baseIndex = requested ? entries.findIndex((v) => v.commit === requested) : headIndex + 1
    const recorded = entries
      .slice(headIndex, baseIndex < 0 ? headIndex + 1 : baseIndex)
      .map((v) => fixtures.versionPatches[v.commit] ?? '')
      .join('')
    // A revision made in the mock has no recorded patch: diff the sources it kept.
    const parent = requested ?? entries[headIndex + 1]?.commit
    const before = parent === undefined ? undefined : state.sourceAt[parent]
    const after = state.sourceAt[commit]
    const patch =
      recorded ||
      (before !== undefined && after !== undefined ? sourcePatch(slug, before, after) : '')
    return HttpResponse.json({
      slug,
      base: requested ?? (entries[headIndex + 1]?.commit ?? ''),
      head: commit,
      files: head.files,
      patch,
    })
  }),

  http.post(`${base}/models/:slug/versions/:commit/restore`, ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const commit = String(params['commit'])
    const entries = state.versions[slug] ?? []
    const target = entries.find((v) => v.commit === commit)
    if (!target) return problem(404, 'Revision not found')
    const created = recordVersion(slug, `Restore ${slug} to ${target.short}`, target.files)
    const model = state.models.find((m) => m.slug === slug)
    if (model) model.version = created.commit
    return HttpResponse.json(created)
  }),

  http.get(`${base}/models/:slug/versions/:commit/schema`, ({ params }) => {
    const schema = state.schemas[String(params['slug'])]
    return schema ? HttpResponse.json(schema) : problem(404, 'Model not found')
  }),

  http.get(`${base}/models/:slug/versions/:commit/source`, ({ params }) => {
    const slug = String(params['slug'])
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    return HttpResponse.text(`// ${String(params['commit']).slice(0, 7)}\ncube(10);\n`)
  }),

  http.get(`${base}/models/:slug/schema`, ({ params }) => {
    const slug = String(params['slug'])
    const schema = state.schemas[slug]
    if (schema) return HttpResponse.json(schema)
    return state.models.some((model) => model.slug === slug)
      ? problem(
          422,
          'Unprocessable Content',
          "OpenSCAD could not build a customizer schema from this model's source",
        )
      : problem(404, 'Model not found')
  }),

  // Per-template presets. Values are checked against the schema as a render is.
  http.get(`${base}/models/:slug/presets`, ({ params }) => {
    const slug = String(params['slug'])
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    return HttpResponse.json(state.presets[slug] ?? [])
  }),

  http.post(`${base}/models/:slug/presets`, async ({ params, request }) => {
    const slug = String(params['slug'])
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    const body = (await request.json()) as ParamPresetCreate
    const name = body.name.trim().replace(/\s+/g, ' ')
    if (paramsClash(body.params, body.inputs)) {
      return problem(422, 'Unprocessable Content', 'params and inputs.params disagree; send inputs only')
    }
    const inputs = body.inputs ?? { params: body.params ?? {} }
    const presetParams = (inputs['params'] ?? {}) as Record<string, ParamValue>
    const refused = presetRefusal(slug, name, presetParams, null)
    if (refused) return refused
    const created: ParamPreset = {
      id: nextHexId(),
      name,
      origin: 'mine',
      params: presetParams,
      inputs: withVersion(inputs),
      updated_at: new Date().toISOString(),
    }
    state.presets[slug] = [...(state.presets[slug] ?? []), created]
    await delay(60)
    return HttpResponse.json(created, { status: 201 })
  }),

  http.post(`${base}/models/:slug/presets/:id/duplicate`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    if (!state.models.some((m) => m.slug === slug)) return problem(404, 'Model not found')
    const source = (state.presets[slug] ?? []).find((p) => p.id === id)
    if (!source) return problem(404, 'Preset not found')
    const body = (await request.json()) as ParamPresetDuplicate
    const name = body.name.trim().replace(/\s+/g, ' ')
    const refused = presetRefusal(slug, name, source.params, null)
    if (refused) return refused
    const copy: ParamPreset = {
      id: nextHexId(),
      name,
      origin: 'mine',
      params: { ...source.params },
      inputs: source.inputs ?? { params: { ...source.params }, v: 0 },
      updated_at: new Date().toISOString(),
    }
    state.presets[slug] = [...(state.presets[slug] ?? []), copy]
    await delay(60)
    return HttpResponse.json(copy, { status: 201 })
  }),

  http.patch(`${base}/models/:slug/presets/:id`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    if (id.startsWith('template-')) return problem(403, 'Error', `'${id}' is read-only`)
    const existing = (state.presets[slug] ?? []).find((p) => p.id === id)
    if (!existing) return problem(404, 'Preset not found')
    const body = (await request.json()) as ParamPresetUpdate
    const name = body.name?.trim().replace(/\s+/g, ' ')
    if (paramsClash(body.params, body.inputs)) {
      return problem(422, 'Unprocessable Content', 'params and inputs.params disagree; send inputs only')
    }
    // `params` alone keeps the preset's other inputs keys, as the backend's update does.
    const current = existing.inputs ?? { params: existing.params, v: 0 }
    const inputs = body.inputs ?? (body.params ? { ...current, params: body.params } : undefined)
    const presetParams = inputs ? ((inputs['params'] ?? {}) as Record<string, ParamValue>) : undefined
    const refused = presetRefusal(slug, name ?? existing.name, presetParams ?? {}, id)
    if (refused) return refused
    const updated: ParamPreset = {
      ...existing,
      name: name ?? existing.name,
      params: presetParams ?? existing.params,
      ...(inputs ? { inputs: withVersion(inputs) } : {}),
      updated_at: new Date().toISOString(),
    }
    state.presets[slug] = (state.presets[slug] ?? []).map((p) => (p.id === id ? updated : p))
    await delay(60)
    return HttpResponse.json(updated)
  }),

  http.delete(`${base}/models/:slug/presets/:id`, ({ params }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    if (id.startsWith('template-')) return problem(403, 'Error', `'${id}' is read-only`)
    const presets = state.presets[slug] ?? []
    if (!presets.some((p) => p.id === id)) return problem(404, 'Preset not found')
    state.presets[slug] = presets.filter((p) => p.id !== id)
    return new HttpResponse(null, { status: 204 })
  }),

  http.post(`${base}/models/:slug/render`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const body = (await request.json()) as {
      inputs?: { params?: Record<string, ParamValue> }
      params?: Record<string, ParamValue>
    }
    const renderParams = body.inputs?.params ?? body.params ?? {}
    const schema = state.schemas[slug]
    if (!schema) return problem(404, 'Model not found')

    const known = new Set((schema.parameters ?? []).map((p) => p.name))
    const unknown = Object.keys(renderParams).filter((key) => !known.has(key))
    if (unknown.length > 0) {
      return problem(422, 'Unknown parameter', `Not in the model schema: ${unknown.join(', ')}`)
    }
    // #204 — `file_assets`: empty, the model's default, one of its samples, or an
    // uploaded id; never a path.
    for (const param of schema.parameters ?? []) {
      const value = renderParams[param.name]
      if (param.type !== 'file' || value === undefined || value === '' || value === param.initial) {
        continue
      }
      if (typeof value === 'string' && (param.samples ?? []).includes(value)) continue
      if (typeof value !== 'string' || !state.assets.has(value)) {
        return problem(
          422,
          'Unprocessable Content',
          `parameter '${param.name}' is not an uploaded or sample file: '${String(value)}'`,
        )
      }
    }

    const jobId = nextHexId()
    state.jobs.set(jobId, {
      id: jobId,
      slug,
      status: 'pending',
      created_at: new Date().toISOString(),
      params: renderParams,
      inputs: withVersion({ ...(body.inputs ?? {}), params: renderParams }),
      log_tail: [],
    })
    runJob(jobId)
    return HttpResponse.json(
      { job_id: jobId, status_url: `${base}/jobs/${jobId}` },
      { status: 202 },
    )
  }),

  http.post(`${base}/models/:slug/assets`, async ({ params, request }) => {
    const slug = String(params['slug'])
    if (!state.models.some((model) => model.slug === slug)) {
      return problem(404, 'Not Found', `no model named '${slug}'`)
    }
    // Duck-typed, as on `POST /models`: the entry's class differs between the
    // browser worker and the Node interceptor.
    const file = (await request.formData()).get('file')
    if (file === null || typeof file === 'string') {
      return problem(422, 'Unprocessable Content', 'no file part')
    }
    const stored = await storeAsset(file)
    return stored
      ? HttpResponse.json(stored, { status: 201 })
      : problem(422, 'Unprocessable Content', ASSET_REFUSAL)
  }),

  // #296 — the server's defaults for the caps.
  http.get(`${base}/assets/usage`, () => {
    const metas = [...state.assets.values()].map((asset) => asset.meta)
    return HttpResponse.json({
      count: metas.length,
      bytes: metas.reduce((total, meta) => total + meta.size, 0),
      max_count: 10_000,
      max_total_bytes: 1_000_000_000,
    } satisfies AssetUsage)
  }),

  // #426 — the blob store's usage, over every kind.
  http.get(`${base}/store/usage`, () =>
    HttpResponse.json({
      backend: 'local',
      count: 3,
      bytes: 4096,
      max_count: 200000,
      max_total_bytes: 53687091200,
      by_kind: { piece: 4096 },
    }),
  ),

  http.get(`${base}/models/:slug/assets/:id`, ({ params }) => {
    const asset = state.assets.get(String(params['id']))
    return asset ? HttpResponse.json(asset.meta) : problem(404, 'Not Found', 'no uploaded file')
  }),

  http.get(`${base}/models/:slug/assets/:id/content`, ({ params }) => {
    const asset = state.assets.get(String(params['id']))
    if (!asset) return problem(404, 'Not Found', 'no uploaded file')
    const type = asset.meta.kind === 'svg' ? 'image/svg+xml' : 'image/png'
    return HttpResponse.arrayBuffer(asset.bytes, { headers: { 'Content-Type': type } })
  }),

  // #204 — a sample file the template ships; only a listed name is served.
  http.get(`${base}/models/:slug/samples/:name`, ({ params }) => {
    const sample = fixtures.sampleFiles[String(params['slug'])]?.[String(params['name'])]
    if (!sample) return problem(404, 'Not Found', 'no such sample')
    const bytes =
      sample.type === 'image/png'
        ? Uint8Array.from(atob(sample.body), (char) => char.charCodeAt(0))
        : new TextEncoder().encode(sample.body)
    return HttpResponse.arrayBuffer(bytes.buffer, { headers: { 'Content-Type': sample.type } })
  }),

  // spec 2026-09-27 §7 / §10 — Arrange refuses what the API refuses, then finishes at
  // once. Its plates are what the writer reports: `plates` lists every plate of the
  // new file and is empty when there is one. The mock's packer: more than four copies
  // take a second plate.
  http.post(`${base}/outputs/arrange`, async ({ request }) => {
    const body = (await request.json()) as ArrangeRequest
    state.lastArrange = body
    const manifest: ManifestObject[] = []
    for (const object of body.objects) {
      const source = state.outputs.find((o) => o.id === object.output_id)
      if (!source) return problem(404, 'Not Found', `no output with id '${object.output_id}'`)
      if ((source.manifest ?? []).length === 0) {
        return problem(
          409,
          'Conflict',
          `output ${source.id} was saved before outputs recorded their objects; generate it again to arrange it`,
        )
      }
      const entry = (source.manifest ?? []).find((m) => m.part === object.part)
      if (!entry) {
        return problem(422, 'Unprocessable Content', `output ${source.id} has no object ${object.part}`)
      }
      if (object.count > 0) {
        manifest.push({ ...entry, count: object.count, source_output: entry.source_output ?? source.id })
      }
    }
    if (manifest.length === 0) {
      return problem(422, 'Unprocessable Content', 'nothing to arrange: every count is 0')
    }
    const first = state.outputs.find((o) => o.id === body.objects[0]?.output_id)
    if (!first?.bbox_mm) return problem(409, 'Conflict', 'the output has no dimensions')
    const colors = body.colours ?? first.colors ?? []
    const copies = manifest.reduce((sum, m) => sum + m.count, 0)
    const bbox = first.bbox_mm
    const jobId = nextHexId()
    const job: Job = {
      id: jobId,
      slug: first.slug,
      status: 'done',
      created_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      params: {},
      log_tail: [],
      bbox_mm: bbox,
      colors,
      plates: copies > 4 ? [1, 2].map((index) => ({ index, bbox_mm: bbox, colors })) : [],
    }
    state.jobs.set(jobId, job)
    state.arranged.set(jobId, {
      sources: [...new Set(body.objects.map((o) => o.output_id))],
      manifest,
    })
    return HttpResponse.json(job, { status: 202 })
  }),

  http.get(`${base}/jobs/:id`, ({ params }) => {
    const job = state.jobs.get(String(params['id']))
    if (!job) return problem(404, 'Job not found')
    return HttpResponse.json(job)
  }),

  http.get(`${base}/jobs/:id/preview.glb`, ({ params }) => {
    const job = state.jobs.get(String(params['id']))
    if (!job || !job.bbox_mm) return problem(404, 'Preview not ready')
    const [x, y, z] = job.bbox_mm.size
    const glb = keychainGlb(job.colors ?? ['#9AA4B2'], { x, y, z })
    return HttpResponse.arrayBuffer(glb.buffer.slice(0) as ArrayBuffer, {
      headers: { 'Content-Type': 'model/gltf-binary' },
    })
  }),

  http.post(`${base}/models/:slug/outputs`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const body = (await request.json()) as {
      job_id: string
      name?: string | null
      inputs?: Record<string, unknown>
    }
    const job = state.jobs.get(body.job_id)
    if (!job || job.status !== 'done' || !job.bbox_mm) {
      return problem(409, 'Job not finished', 'Wait for the render to finish before generating.')
    }
    const output: Output = {
      id: nextHexId(),
      slug,
      name: body.name ?? null,
      job_id: job.id,
      created_at: new Date().toISOString(),
      has_thumbnail: false,
      params: job.params,
      // The inputs sent with Generate, else the job's own, as the backend records them.
      inputs: withVersion(body.inputs ?? job.inputs ?? { params: job.params }),
      bbox_mm: job.bbox_mm,
      colors: job.colors ?? [],
      parts: [],
      warnings: [],
      // An arranged output keeps its objects and where they came from (§7); a render's
      // output has no manifest in the mock.
      manifest: state.arranged.get(job.id)?.manifest ?? [],
      arranged_from: state.arranged.get(job.id)?.sources ?? [],
      bom: [],
      files: [],
      record: null,
    }
    state.outputs = [output, ...state.outputs]
    await delay(120)
    return HttpResponse.json(output, { status: 201 })
  }),

  // Inputs migration (spec 2026-09-27 §8.2): the mock templates have no `migrate`, so
  // the inputs come back as they are, at the version they carry.
  http.post(`${base}/models/:slug/inputs/migrate`, async ({ request }) => {
    const body = (await request.json()) as { inputs: Record<string, unknown> }
    const v = typeof body.inputs.v === 'number' ? body.inputs.v : 0
    return HttpResponse.json({ inputs: body.inputs, from_version: v, to_version: v })
  }),

  http.get(`${base}/models/:slug/outputs`, ({ params }) =>
    HttpResponse.json(state.outputs.filter((o) => o.slug === params['slug'])),
  ),

  http.get(`${base}/outputs/:id`, ({ params }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    return output ? HttpResponse.json(output) : problem(404, 'Output not found')
  }),

  http.get(`${base}/outputs/:id/edit`, ({ params }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    return HttpResponse.json({
      output_id: output.id,
      slug: output.slug,
      name: output.name ?? null,
      params: output.params ?? {},
      inputs: output.inputs ?? { params: output.params, v: 0 },
      model_version: output.model_version ?? null,
      source: 'record',
      arranged_from: output.arranged_from ?? [],
    })
  }),

  http.delete(`${base}/outputs/:id`, ({ params }) => {
    state.outputs = state.outputs.filter((o) => o.id !== params['id'])
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${base}/outputs/:id/model.3mf`, ({ params }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    // A 3MF is a zip; the mock serves a stub so the download path is exercised.
    return HttpResponse.arrayBuffer(new Uint8Array([0x50, 0x4b, 0x03, 0x04]).buffer, {
      headers: {
        'Content-Type': 'model/3mf',
        'Content-Disposition': `attachment; filename="${output.slug}-${output.id}.3mf"`,
      },
    })
  }),

  // #83 — every ScadBuddy render is one plate; a test overrides this for a multi-plate 3MF.
  http.get(`${base}/outputs/:id/plates`, ({ params }) => {
    if (!state.outputs.some((o) => o.id === params['id'])) return problem(404, 'Output not found')
    return HttpResponse.json([{ index: 1, has_thumbnail: true }] satisfies OutputPlate[])
  }),

  // Multipart with a `file` part, at /thumbnail — not a raw PNG body at /thumbnail.png.
  http.put(`${base}/outputs/:id/thumbnail`, async ({ params, request }) => {
    const form = await request.formData()
    if (!form.get('file')) return problem(422, 'Missing file')
    state.outputs = state.outputs.map((o) =>
      o.id === params['id'] ? { ...o, has_thumbnail: true } : o,
    )
    return new HttpResponse(null, { status: 204 })
  }),

  http.post(`${base}/outputs/:id/send`, async ({ params, request }) => {
    const id = String(params['id'])
    const body = (await request.json()) as { mode: 'library' | 'queue'; copies?: number }
    const output = state.outputs.find((o) => o.id === id)
    if (!output) return problem(404, 'Output not found')
    if (!state.settings.has_api_key) {
      return problem(409, 'Bambuddy is not connected', 'Add an API key on the settings page.')
    }
    await delay(250)
    const libraryFileId = copyIn(output, null)
    const queued = body.mode === 'queue'
    const pipelineRunId = queued && state.settings.pipeline_id ? nextNumber() : null
    const queueItemId = queued && !pipelineRunId ? nextNumber() : null
    state.outputs = state.outputs.map((o) =>
      o.id === id
        ? {
            ...o,
            pipeline_run_id: pipelineRunId,
            queue_item_id: queueItemId,
          }
        : o,
    )
    const result: SendResult = {
      mode: body.mode,
      library_file_id: libraryFileId,
      filename: `${output.slug}-${output.name ?? output.id}.3mf`,
      pipeline_run_id: pipelineRunId,
      queue_item_id: queueItemId,
      bambuddy_url: `${state.settings.bambuddy_url}${queued ? '/queue' : '/library'}`,
      edit_url: state.settings.public_url
        ? `${state.settings.public_url.replace(/\/$/, '')}${editPath(id)}`
        : null,
    }
    return HttpResponse.json(result)
  }),

  // --- spec 2026-09-27: the spool-first print dialog ------------------------------

  /**
   * One read for the whole dialog. `printer_id` narrows it to that printer; without one
   * the server opens on the model's remembered printer, as it does here.
   */
  http.get(`${base}/print/outputs/:id/choices`, ({ params, request }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    const slug = output.slug
    const remembered = state.modelChoices[slug] ?? NO_MODEL_CHOICES
    const asked = new URL(request.url).searchParams.get('printer_id')
    const printerId =
      asked !== null ? Number(asked) : (remembered.printer_id ?? choicesView.printer_id ?? null)
    const printerName =
      (choicesView.printers ?? []).find((printer) => printer.id === printerId)?.name ?? null
    const bed = state.printerBedTypes[String(printerId)]
    return HttpResponse.json({
      ...choicesView,
      printer_id: printerId,
      ...(bed ? { bed_type: bed } : {}),
      filaments: { ...choicesView.filaments, printer_id: printerId, printer_name: printerName },
      model_choices: remembered,
    } satisfies ChoicesView)
  }),

  http.put(`${base}/print/printers/:id/bed-type`, async ({ params, request }) => {
    const printerId = String(params['id'])
    const body = (await request.json()) as { bed_type: string | null }
    if (body.bed_type === null) delete state.printerBedTypes[printerId]
    else state.printerBedTypes[printerId] = body.bed_type
    return HttpResponse.json({
      printer_id: Number(printerId),
      bed_type: state.printerBedTypes[printerId] ?? null,
    })
  }),

  http.put(`${base}/print/models/:slug/choices`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const body = (await request.json()) as ModelPrintChoices
    // Forget only an all-default body, as the backend does: a remembered nozzle, tier or
    // process with no printer and no plan is still kept.
    if (isNoModelChoices(body)) delete state.modelChoices[slug]
    else state.modelChoices[slug] = { ...NO_MODEL_CHOICES, ...body }
    return HttpResponse.json(state.modelChoices[slug] ?? NO_MODEL_CHOICES)
  }),

  /**
   * #87 — the aggregation the picker reads. `printer_id` scopes it: the server reports
   * `loaded` against that printer, so without one a spool in the other machine would
   * look local. The recorded estate is in `fixtures.filamentOptions`.
   */
  http.get(`${base}/print/outputs/:id/filaments`, ({ params, request }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    const search = new URL(request.url).searchParams
    const printerId = search.get('printer_id')
    // #78 — no printer, no hardware to read.
    const hardware = printerId === null ? { nozzles: [] } : {}
    return HttpResponse.json({
      ...fixtures.filamentOptions,
      ...hardware,
      library_file_id:
        output.library_files?.[0]?.id ?? fixtures.filamentOptions.library_file_id,
      printer_id: printerId === null ? null : Number(printerId),
    } satisfies FilamentOptions)
  }),

  /**
   * spec 2026-09-27 §4 — the spool-first run. No pipeline: the server resolves every
   * preset from `choices` and the spool plan, then slices and queues. A request without
   * `choices` is the old pipeline shape, which the backend answers with a 422.
   */
  http.post(`${base}/print/outputs/:id/run`, async ({ params, request }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    const body = (await request.json()) as Partial<PrintRunRequest>
    if (!body.choices) return problem(422, 'Unprocessable Content', 'choices: Field required')
    const sizes = new Set(body.choices.nozzles.map((nozzle) => nozzle.size))
    if (sizes.size > 1) {
      return problem(
        422,
        'Unprocessable Content',
        "The two nozzles are different sizes. Bambuddy can't slice mixed nozzle sizes yet.",
      )
    }
    // Resolved as the server does (#124): an omitted `copies` is the remembered quantity,
    // global → the printer the run names → this model, else 1.
    const scopePrinter = body.printer_id ?? state.printOptions.printer_id ?? null
    const copies =
      body.copies ??
      resolveOptions(
        state.printOptions.global_options,
        scopePrinter === null ? undefined : state.printOptions.printers?.[String(scopePrinter)],
        state.printOptions.models?.[output.slug],
      ).quantity ??
      1
    // #79 — the project's own library folder replaces the one from Settings for this
    // send, which is what puts the file on Bambuddy's project page.
    const projectId = body.project_id ?? state.lastProjectId
    const folderId =
      projectId === null
        ? null
        : (state.projects.find((project) => project.id === projectId)?.folder_id ?? null)
    const libraryFileId = copyIn(output, folderId)
    const queueItemIds = [nextNumber()]
    state.outputs = state.outputs.map((o) =>
      o.id === output.id
        ? { ...o, pipeline_run_id: null, queue_item_id: queueItemIds[0] }
        : o,
    )
    await delay(200)
    const warnings = body.choices.nozzles.some((nozzle) => nozzle.flow === 'high_flow')
      ? [
          {
            kind: 'hf-unsupported' as const,
            message:
              "Bambuddy slices this as Standard flow; High Flow presets aren't supported by Bambuddy yet.",
          },
        ]
      : []
    return HttpResponse.json({
      route: 'slice_queue',
      library_file_id: libraryFileId,
      printer_id: body.printer_id ?? null,
      slice_job_id: nextNumber(),
      sliced_library_file_id: nextNumber(),
      queue_item_ids: queueItemIds,
      copies,
      warnings,
      project_id: projectId,
      folder_id: folderId,
      bambuddy_url: `${state.settings.bambuddy_url}/queue`,
    } satisfies PrintRunResult)
  }),

  // --- #79 projects -----------------------------------------------------------------

  http.get(`${base}/print/projects`, () =>
    HttpResponse.json({
      projects: state.projects,
      last_project_id: state.lastProjectId,
    } satisfies ProjectChoices),
  ),

  http.post(`${base}/print/projects`, async ({ request }) => {
    const body = (await request.json()) as ProjectRequest
    const linked =
      body.project_id === undefined || body.project_id === null
        ? undefined
        : state.projects.find((project) => project.id === body.project_id)
    if (body.project_id !== undefined && body.project_id !== null && !linked) {
      return problem(404, 'Not Found', `no project ${body.project_id}`)
    }
    if (!linked && !body.name) {
      return problem(400, 'Bad Request', 'a new project needs a name')
    }
    const base_ = linked ?? {
      id: nextNumber(),
      name: body.name ?? '',
      description: body.description ?? null,
      colour: body.colour ?? null,
      status: 'active',
      archive_count: 0,
      queue_count: 0,
      folder_id: null,
      folder_name: null,
    }
    // Creating a project creates its library folder, and linking one that has none
    // creates it too — a project with no folder lists no files on Bambuddy's own page.
    const saved: ProjectView = {
      ...base_,
      folder_id: base_.folder_id ?? nextNumber(),
      folder_name: base_.folder_name ?? base_.name,
    }
    state.projects = [saved, ...state.projects.filter((project) => project.id !== saved.id)]
    await delay(150)
    return HttpResponse.json(saved)
  }),

  http.post(`${base}/print/outputs/:id/project`, async ({ params, request }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    const body = (await request.json()) as { project_id?: number | null; queue_item_ids: number[] }
    const projectId = body.project_id ?? state.lastProjectId
    if (projectId === null) {
      return problem(409, 'Conflict', 'this output has no project, so there is nothing to file it under')
    }
    // An archive only exists once a print has finished, so the mock reports none: the
    // caller attaches again later rather than the run pretending it already happened.
    return HttpResponse.json({
      project_id: projectId,
      queue_item_ids: body.queue_item_ids,
      archive_ids: [],
    } satisfies AttachResult)
  }),

  /**
   * #89 — following the print. Which route answers is read off what the output recorded,
   * the way the backend does it, so an output that has never been printed answers `200
   * null` rather than a 404: never printed is an answer, not a missing resource.
   */
  http.get(`${base}/print/outputs/:id/progress`, ({ params }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    if (output.pipeline_run_id) {
      return HttpResponse.json({
        ...fixtures.pipelineProgress,
        pipeline_run_id: output.pipeline_run_id,
      } satisfies PrintProgress)
    }
    if (output.queue_item_id) {
      return HttpResponse.json({
        ...fixtures.queuedSliceProgress,
        queue_item_id: output.queue_item_id,
      } satisfies PrintProgress)
    }
    return HttpResponse.json(null)
  }),

  http.get(`${base}/fonts`, () => HttpResponse.json(state.fonts)),

  http.get(`${base}/fonts/catalogue`, ({ request }) => {
    if (state.catalogueOffline) {
      return problem(503, 'Service Unavailable', 'the Google Fonts catalogue is unavailable')
    }
    const url = new URL(request.url)
    const needle = (url.searchParams.get('q') ?? '').toLowerCase()
    const category = url.searchParams.get('category') ?? ''
    const limit = Number(url.searchParams.get('limit') ?? 60)
    const matched = state.fontCatalogue
      .filter((font) => font.family.toLowerCase().includes(needle))
      .filter((font) => !category || font.category === category)
    if (needle) {
      matched.sort((a, b) => {
        const rank = (family: string) => (family.toLowerCase().startsWith(needle) ? 0 : 1)
        return rank(a.family) - rank(b.family) || (a.popularity ?? 0) - (b.popularity ?? 0)
      })
    }
    return HttpResponse.json({
      source: 'google-fonts-metadata',
      fetched_at: '2026-09-22T12:00:00Z',
      total: matched.length,
      fonts: matched.slice(0, limit),
    })
  }),

  http.post(`${base}/fonts/install`, async ({ request }) => {
    const body = (await request.json()) as { family: string }
    const row = state.fontCatalogue.find((font) => font.family === body.family)
    if (!row) return problem(404, 'Not Found', `'${body.family}' is not in the Google Fonts catalogue`)
    if (row.family === fixtures.UNINSTALLABLE_FONT) {
      return problem(502, 'Bad Gateway', `'${row.family}' could not be downloaded: gstatic said no`)
    }
    await delay(100)
    const styles = (row.variants ?? []).map((variant) =>
      variant.weight === 700 ? 'Bold' : variant.italic ? 'Italic' : 'Regular',
    )
    row.installed = true
    state.fonts = [
      ...state.fonts.filter((font) => font.family !== row.family),
      { family: row.family, styles },
    ]
    return HttpResponse.json({ family: row.family, styles, files: [], licence: 'OFL.txt' })
  }),

  http.get(`${base}/libraries`, () => HttpResponse.json(state.libraries)),

  // #93 — pins are per model: PUT clones at `ref` and pins it into this model alone.
  http.put(`${base}/models/:slug/libraries/:name`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const name = String(params['name'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Not Found', `no model named '${slug}'`)
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) {
      return problem(422, 'Unprocessable Content', `'${name}' is not a library directory name`)
    }
    const body = (await request.json()) as { url?: string | null; ref?: string | null }
    const known = state.libraries.find((entry) => entry.name === name)
    if (!known && !body.url) {
      return problem(404, 'Not Found', `'${name}' is not in the catalogue; give a url to add it`)
    }
    const url = body.url ?? known?.url ?? ''
    const ref = body.ref ?? known?.ref ?? ''
    if (!ref) return problem(422, 'Unprocessable Content', `give a ref to pin '${name}' at`)
    if (ref === fixtures.MISSING_REF) {
      return problem(502, 'Bad Gateway', `git clone failed: Remote branch ${ref} not found`)
    }
    await delay(100)
    state.seq += 1
    const pin = { name, url, ref, commit: state.seq.toString(16).padStart(40, 'c') }
    const current = model.libraries ?? []
    const libraries = current.some((row) => row.name === name)
      ? current.map((row) => (row.name === name ? pin : row))
      : [...current, pin]
    const updated = { ...model, libraries }
    state.models = state.models.map((m) => (m.slug === slug ? updated : m))
    return HttpResponse.json(view(updated))
  }),

  http.delete(`${base}/models/:slug/libraries/:name`, async ({ params }) => {
    const slug = String(params['slug'])
    const name = String(params['name'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Not Found', `no model named '${slug}'`)
    const current = model.libraries ?? []
    if (!current.some((row) => row.name === name)) {
      return problem(404, 'Not Found', `'${slug}' does not declare '${name}'`)
    }
    await delay(50)
    const updated = { ...model, libraries: current.filter((row) => row.name !== name) }
    state.models = state.models.map((m) => (m.slug === slug ? updated : m))
    return HttpResponse.json(view(updated))
  }),

  http.get(`${base}/settings`, () => HttpResponse.json(state.settings)),

  // #349 — served by the agent service, not the backend (agent/src/routes/headlessBrowser.ts).
  http.get(`${base}/ai/settings/headless-browser`, () =>
    HttpResponse.json({ enabled: state.headlessBrowser }),
  ),

  http.put(`${base}/ai/settings/headless-browser`, async ({ request }) => {
    const body = (await request.json()) as { enabled?: unknown }
    if (typeof body.enabled !== 'boolean') {
      return HttpResponse.json({ detail: 'enabled: expected boolean' }, { status: 400 })
    }
    state.headlessBrowser = body.enabled
    return HttpResponse.json({ enabled: state.headlessBrowser })
  }),

  http.put(`${base}/settings`, async ({ request }) => {
    const body = (await request.json()) as {
      bambuddy_url?: string | null
      bambuddy_api_key?: string
      bambuddy_render_api_key?: string
      store_backend?: Settings['store_backend'] | null
      public_url?: string | null
      library_folder_id?: number | null
      pipeline_id?: number | null
      printer_id?: number | null
      display_unit?: Settings['display_unit'] | null
    }
    state.settings = {
      ...state.settings,
      ...body,
      // #274: read-only, SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES; a PUT does not store one.
      media_upload_max_bytes: state.settings.media_upload_max_bytes,
      display_unit: body.display_unit === undefined ? state.settings.display_unit : (body.display_unit ?? 'mm'),
      has_api_key:
        body.bambuddy_api_key === undefined
          ? state.settings.has_api_key
          : body.bambuddy_api_key.length > 0,
      has_render_api_key:
        typeof body.bambuddy_render_api_key === 'string'
          ? body.bambuddy_render_api_key.length > 0
          : state.settings.has_render_api_key,
      store_backend: body.store_backend ?? state.settings.store_backend,
    }
    state.settings.render_key_fallback = state.settings.has_api_key && !state.settings.has_render_api_key
    delete (state.settings as { bambuddy_api_key?: string }).bambuddy_api_key
    delete (state.settings as { bambuddy_render_api_key?: string }).bambuddy_render_api_key
    await delay(120)
    return HttpResponse.json(state.settings)
  }),

  // #81 — the server resolves Bambuddy's code or the profile name, else the default.
  http.get(`${base}/plate`, ({ request }) =>
    HttpResponse.json(
      plateFor(new URL(request.url).searchParams.get('model')) ??
        plateFor(state.settings.default_plate ?? null) ??
        fixtures.defaultPlate,
    ),
  ),

  // The axes only: the real route also runs the send's placement, which a test that
  // needs a prime-tower refusal overrides this handler to answer with.
  http.get(`${base}/plate/fit`, ({ request }) => {
    const search = new URL(request.url).searchParams
    const plate =
      plateFor(search.get('model')) ??
      plateFor(state.settings.default_plate ?? null) ??
      fixtures.defaultPlate
    const { usable } = plate
    const limits = [
      ['X', Number(search.get('x')), usable.max_x - usable.min_x],
      ['Y', Number(search.get('y')), usable.max_y - usable.min_y],
      ['Z', Number(search.get('z')), plate.height],
    ] as const
    return HttpResponse.json({
      plate,
      overshoots: limits
        .filter(([, size, limit]) => size > limit)
        .map(([axis, size, limit]) => ({ axis, size, limit })),
      problem: null,
    } satisfies PlateFit)
  }),

  http.get(`${base}/plates`, () =>
    HttpResponse.json({
      default: plateFor(state.settings.default_plate ?? null) ?? fixtures.defaultPlate,
      plates: Object.values(fixtures.plates),
    }),
  ),

  // Takes no body: the server tests what it has stored.
  http.get(`${base}/settings/print-options`, () => HttpResponse.json(state.printOptions)),

  // Mirrors the server: one scope is replaced wholesale, and an all-unset overlay
  // removes it rather than storing an empty object.
  http.put(`${base}/settings/print-options`, async ({ request }) => {
    const body = (await request.json()) as PrintOptionsUpdate
    const options = body.options as PrintOptions
    const empty = Object.values(options).every((value) => value === null || value === undefined)
    if (body.scope === 'global') {
      state.printOptions.global_options = empty ? {} : options
    } else {
      const map = body.scope === 'printer' ? state.printOptions.printers : state.printOptions.models
      if (!map || !body.key) return problem(422, 'Unprocessable', 'the scope needs a key')
      if (empty) delete map[body.key]
      else map[body.key] = options
    }
    // Shaped like the real response, which is declared `response_model=PrintOptionsView`
    // and so cannot carry `printer_id` however much the server-side object holds. Reusing
    // the GET's object here would let a component that reads `printer_id` off a PUT
    // result pass in tests and break in the browser.
    const { defaults, global_options, printers, models } = state.printOptions
    return HttpResponse.json({ defaults, global_options, printers, models })
  }),

  http.post(`${base}/settings/test`, async () => {
    await delay(200)
    if (!state.settings.bambuddy_url?.startsWith('http')) {
      return problem(409, 'Conflict', 'no Bambuddy URL is configured')
    }
    if (!state.settings.has_api_key) {
      return HttpResponse.json({
        ok: false,
        detail: "Bambuddy refused the API key when asked to list the printers. The key needs the 'Read Status' scope",
        printers: [],
      })
    }
    return HttpResponse.json({
      ok: true,
      detail: 'Connected. Bambuddy reports 3DP-31B-598.',
      printers: fixtures.targets.printers,
    })
  }),

  http.get(`${base}/settings/targets`, () => {
    if (!state.settings.bambuddy_url) return problem(409, 'Conflict', 'no Bambuddy URL is configured')
    return HttpResponse.json(fixtures.targets)
  }),

  http.post(`${base}/settings/register-sidebar`, async () => {
    await delay(200)
    const created = state.sidebarLinkId === 0
    if (created) state.sidebarLinkId = 3
    return HttpResponse.json({
      id: state.sidebarLinkId,
      name: 'ScadBuddy',
      url: state.settings.public_url ?? '',
      icon: 'shapes',
      open_in_new_tab: false,
      created,
      embed_path: `/external/${state.sidebarLinkId}`,
    })
  }),
]
