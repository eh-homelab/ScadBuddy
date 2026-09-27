import { HttpResponse, delay, http } from 'msw'
import type {
  AttachResult,
  BoundingBox,
  CatalogueFont,
  Diagnostic,
  EligibilityOverview,
  FilamentOptions,
  FontFamily,
  Job,
  LibraryEntry,
  ModelPatch,
  ModelPrintChoices,
  ModelSummary,
  MergePreview,
  ModelVersion,
  Output,
  OutputPlate,
  ParamPreset,
  ParamPresetCreate,
  ParamPresetUpdate,
  ParamValue,
  PipelineChoices,
  PipelineCreate,
  PipelineView,
  Plate,
  PlateFit,
  PresetOptions,
  PresetRef,
  PrintProgress,
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
import {
  MAX_META_BYTES,
  MAX_META_SIZE,
  MAX_THUMBNAIL_BYTES,
  MAX_THUMBNAIL_SIZE,
} from '../lib/modelFolder'
import { resolveOptions } from '../lib/printOptions'
import { keychainGlb } from './glb'
import * as fixtures from './fixtures'

const base = '/api/v1'

interface MockJob extends Job {
  polls: number
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
  printOptions: structuredClone(fixtures.printOptions) as PrintOptionsState,
  jobs: new Map<string, MockJob>(),
  pipelines: [...fixtures.pipelineViews] as PipelineView[],
  /** #86 — per-model default pipelines, the store's `model_pipelines`. */
  modelPipelines: {} as Record<string, number>,
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
  libraries: structuredClone(fixtures.libraries) as LibraryEntry[],
  catalogueOffline: false,
  sidebarLinkId: 0,
  seq: 0,
}

/** Reset every mutable fixture. Call between tests. */
export function resetMockState(): void {
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
  state.printOptions = structuredClone(fixtures.printOptions)
  state.jobs.clear()
  state.pipelines = fixtures.pipelineViews.map((p) => ({ ...p }))
  state.modelPipelines = {}
  state.modelChoices = {}
  state.printerBedTypes = {}
  state.projects = fixtures.projectViews.map((p) => ({ ...p }))
  state.lastProjectId = null
  state.fonts = fixtures.fonts.map((f) => ({ ...f }))
  state.versions = structuredClone(fixtures.versions)
  state.sourceAt = initialSourceAt()
  state.fontCatalogue = fixtures.fontCatalogue.map((f) => ({ ...f }))
  state.libraries = structuredClone(fixtures.libraries)
  state.catalogueOffline = false
  state.sidebarLinkId = 0
  state.seq = 0
}

/** Replaces a template's presets, so a test can start at a state that is slow to build. */
export function setMockPresets(slug: string, presets: ParamPreset[]): void {
  state.presets[slug] = presets
}

/** Makes `GET /fonts/catalogue` fail, which is the air-gapped case the picker falls back for. */
export function setCatalogueOffline(offline: boolean): void {
  state.catalogueOffline = offline
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
  const plan = { ours, base: baseSource, theirs, patch, taken: [], kept: [] }
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

function jobView(job: MockJob): Job {
  const { polls: _polls, ...rest } = job
  return rest
}

function problem(status: number, title: string, detail?: string, extensions: object = {}) {
  return HttpResponse.json(
    { type: 'about:blank', title, status, detail, ...extensions },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

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
  const known = new Set((state.schemas[slug]?.parameters ?? []).map((p) => p.name))
  const unknown = Object.keys(params).filter((key) => !known.has(key))
  if (unknown.length > 0) {
    return problem(422, 'Unprocessable Content', `unknown parameters: ${unknown.join(', ')}`, {
      parameters: unknown,
    })
  }
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

export const handlers = [
  http.get(`${base}/models`, () => HttpResponse.json(state.models.map(view))),

  http.post(`${base}/models`, async ({ request }) => {
    if ((request.headers.get('content-type') ?? '').includes('application/json')) {
      const body = (await request.json()) as {
        name: string
        source: string
        description?: string
        tags?: string[]
        force?: boolean
      }
      const pastedSlug = slugify(body.name)
      if (!pastedSlug) return problem(422, 'Unprocessable Content', 'that name yields no slug')
      if (state.models.some((m) => m.slug === pastedSlug)) {
        return problem(409, 'Conflict', `a model named '${pastedSlug}' already exists`)
      }
      const check = checkOf(body.source)
      if (!check.ok && !body.force) return refusal(check)
      const pasted: ModelSummary = {
        slug: pastedSlug,
        name: body.name,
        description: body.description ?? '',
        tags: body.tags ?? [],
        updated_at: new Date().toISOString(),
        has_thumbnail: false,
        has_readme: false,
        origin: 'mine',
      }
      state.models = [pasted, ...state.models]
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
      // README.md come too; its outputs, and so any plate fallback, do not.
      has_thumbnail: upstream.thumbnail_source === 'model',
      thumbnail_source: upstream.thumbnail_source === 'model' ? 'model' : null,
      thumbnail_output_id: null,
      updated_at: version.date,
      version: version.commit,
      upstream: {
        id,
        path: upstreamPath(id),
        base,
      },
    }
    state.models = [copy, ...state.models]
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
      preview: upstreamState === 'update' ? planMerge(slug, model) : null,
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
        { merged: plan.merged, merge_base: revision, conflicts: 1, taken: [], kept: [] },
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
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const patch = (await request.json()) as ModelPatch
    // #93: only libraries that have been added (and so are pinned) can be declared.
    const missing = (patch.libraries ?? []).filter(
      (name) => !state.libraries.some((entry) => entry.name === name && entry.pin),
    )
    if (missing.length > 0) {
      return problem(422, 'Unprocessable Content', `not added yet: ${missing.join(', ')}`)
    }
    const change = Object.fromEntries(
      Object.entries(patch).filter(([, value]) => value !== null && value !== undefined),
    ) as Partial<ModelSummary>
    const updated = reviseModel(slug, `Update ${slug} metadata`, [
      { status: 'M', path: 'model.json' },
    ], change)
    return updated ? HttpResponse.json(updated) : problem(404, 'Model not found')
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
    const had = state.models.find((m) => m.slug === slug)?.thumbnail_source === 'model'
    const updated = reviseModel(
      slug,
      `Set ${slug} thumbnail`,
      [{ status: had ? 'M' : 'A', path: 'thumbnail.png' }],
      { has_thumbnail: true, thumbnail_source: 'model', thumbnail_output_id: null },
    )
    return updated ? HttpResponse.json(updated) : problem(404, 'Model not found')
  }),

  http.delete(`${base}/models/:slug/thumbnail`, ({ params }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Model not found')
    if (model.thumbnail_source !== 'model') {
      return problem(404, 'Not Found', `'${slug}' has no thumbnail of its own to remove`)
    }
    // The fixtures' generated models fall back to their first output's plate image.
    // Which is the first output: the one whose plate image the backend serves.
    const first = state.outputs
      .filter((o) => o.slug === slug)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))[0]
    const updated = reviseModel(slug, `Remove ${slug} thumbnail`, [
      { status: 'D', path: 'thumbnail.png' },
    ], {
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
    const refused = presetRefusal(slug, name, body.params ?? {}, null)
    if (refused) return refused
    const created: ParamPreset = {
      id: nextHexId(),
      name,
      origin: 'mine',
      params: body.params ?? {},
      updated_at: new Date().toISOString(),
    }
    state.presets[slug] = [...(state.presets[slug] ?? []), created]
    await delay(60)
    return HttpResponse.json(created, { status: 201 })
  }),

  http.patch(`${base}/models/:slug/presets/:id`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const id = String(params['id'])
    if (id.startsWith('template-')) return problem(403, 'Error', `'${id}' is read-only`)
    const existing = (state.presets[slug] ?? []).find((p) => p.id === id)
    if (!existing) return problem(404, 'Preset not found')
    const body = (await request.json()) as ParamPresetUpdate
    const name = body.name?.trim().replace(/\s+/g, ' ')
    const refused = presetRefusal(slug, name ?? existing.name, body.params ?? {}, id)
    if (refused) return refused
    const updated: ParamPreset = {
      ...existing,
      name: name ?? existing.name,
      params: body.params ?? existing.params,
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
    const body = (await request.json()) as { params: Record<string, ParamValue> }
    const schema = state.schemas[slug]
    if (!schema) return problem(404, 'Model not found')

    const known = new Set((schema.parameters ?? []).map((p) => p.name))
    const unknown = Object.keys(body.params).filter((key) => !known.has(key))
    if (unknown.length > 0) {
      return problem(422, 'Unknown parameter', `Not in the model schema: ${unknown.join(', ')}`)
    }

    const jobId = nextHexId()
    state.jobs.set(jobId, {
      id: jobId,
      slug,
      status: 'pending',
      created_at: new Date().toISOString(),
      params: body.params,
      log_tail: [],
      polls: 0,
    })
    return HttpResponse.json(
      { job_id: jobId, status_url: `${base}/jobs/${jobId}` },
      { status: 202 },
    )
  }),

  http.get(`${base}/jobs/:id`, ({ params }) => {
    const job = state.jobs.get(String(params['id']))
    if (!job) return problem(404, 'Job not found')

    job.polls += 1
    if (job.polls === 1) {
      job.status = 'running'
      job.log_tail = ['Compiling design (CSG Tree generation)...']
      return HttpResponse.json(jobView(job))
    }

    if (String(job.params?.['name'] ?? '').toLowerCase() === fixtures.FAILING_NAME) {
      job.status = 'failed'
      job.error = 'openscad exited with 1'
      job.log_tail = fixtures.OPENSCAD_LOG_TAIL
      return HttpResponse.json(jobView(job))
    }

    job.status = 'done'
    job.bbox_mm = bboxOf(job.params ?? {})
    job.colors = colorsOf(job.slug, job.params ?? {})
    job.preview_url = `${base}/jobs/${job.id}/preview.glb`
    job.log_tail = ['Geometries in cache: 12', 'Total rendering time: 0:00:00.412']
    return HttpResponse.json(jobView(job))
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
    const body = (await request.json()) as { job_id: string; name?: string | null }
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
      bbox_mm: job.bbox_mm,
      colors: job.colors ?? [],
      parts: [],
      warnings: [],
    }
    state.outputs = [output, ...state.outputs]
    await delay(120)
    return HttpResponse.json(output, { status: 201 })
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
      model_version: output.model_version ?? null,
      source: 'record',
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
    const libraryFileId = output.library_file_id ?? nextNumber()
    const queued = body.mode === 'queue'
    const pipelineRunId = queued && state.settings.pipeline_id ? nextNumber() : null
    const queueItemId = queued && !pipelineRunId ? nextNumber() : null
    state.outputs = state.outputs.map((o) =>
      o.id === id
        ? {
            ...o,
            library_file_id: libraryFileId,
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

  // --- #86 print picker -------------------------------------------------------------

  http.get(`${base}/print/presets`, ({ request }) => {
    const url = new URL(request.url)
    const source = url.searchParams.get('printer_preset_source') as PresetRef['source'] | null
    const id = url.searchParams.get('printer_preset_id')
    const options: PresetOptions = {
      printer: fixtures.printerPresets,
      process: [],
      filament: [],
      bed_types: fixtures.BED_TYPES,
      printer_preset: null,
    }
    if (!source || !id) return HttpResponse.json(options)
    // Process and filament arrive only once a printer preset is named, filtered to the
    // presets whose `compatible_printers` lists that preset's NAME.
    const chosen = fixtures.printerPresets.find(
      (choice) => choice.ref.source === source && choice.ref.id === id,
    )
    const fits = (compatible: string[] | undefined) =>
      !compatible?.length || !chosen?.name || compatible.includes(chosen.name)
    return HttpResponse.json({
      ...options,
      printer_preset: { source, id },
      process: fixtures.processPresets.filter((c) => fits(c.compatible_printers ?? [])),
      filament: fixtures.filamentPresets.filter((c) => fits(c.compatible_printers ?? [])),
    } satisfies PresetOptions)
  }),

  http.post(`${base}/print/pipelines`, async ({ request }) => {
    const body = (await request.json()) as PipelineCreate
    const named = (ref: { source: string; id: string } | null | undefined) =>
      [...fixtures.printerPresets, ...fixtures.processPresets, ...fixtures.filamentPresets].find(
        (choice) => choice.ref.source === ref?.source && choice.ref.id === ref?.id,
      )?.name ?? null
    // SlicerPipelineCreate has no target fields: Bambuddy targets the new pipeline itself.
    const created: PipelineView = {
      id: nextNumber(),
      name: body.name,
      description: body.description ?? null,
      bed_type: body.bed_type ?? null,
      target_kind: 'specific_printer',
      target_printer_id: 1,
      target_printer_name: '3DP-31B-598',
      target_model_class: null,
      fanout_strategy: 'max_parallel',
      printer_preset: body.printer_preset,
      process_preset: body.process_preset,
      filament_presets: body.filament_presets,
      printer_preset_name: named(body.printer_preset),
      process_preset_name: named(body.process_preset),
      filament_preset_names: body.filament_presets.map(named),
      printer_ids: [1],
    }
    state.pipelines = [...state.pipelines, created]
    await delay(150)
    return HttpResponse.json(created)
  }),

  http.get(`${base}/print/models/:slug/pipelines`, ({ params }) => {
    const slug = String(params['slug'])
    const modelPipelineId = state.modelPipelines[slug] ?? null
    return HttpResponse.json({
      pipelines: state.pipelines,
      printers: fixtures.targets.printers,
      model_pipeline_id: modelPipelineId,
      global_pipeline_id: state.settings.pipeline_id ?? null,
      default_pipeline_id: modelPipelineId ?? state.settings.pipeline_id ?? null,
      model_choices: state.modelChoices[slug] ?? { printer_id: null, filament_plan: [] },
      printer_bed_types: state.printerBedTypes,
    } satisfies PipelineChoices)
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
    const empty = { printer_id: null, filament_plan: [] }
    if (body.printer_id == null && !body.filament_plan?.length) delete state.modelChoices[slug]
    else state.modelChoices[slug] = { ...empty, ...body }
    return HttpResponse.json(state.modelChoices[slug] ?? empty)
  }),

  http.put(`${base}/print/models/:slug/pipeline`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const body = (await request.json()) as { pipeline_id: number | null }
    if (body.pipeline_id === null) delete state.modelPipelines[slug]
    else state.modelPipelines[slug] = body.pipeline_id
    return HttpResponse.json({
      slug,
      pipeline_id: state.modelPipelines[slug] ?? null,
      global_pipeline_id: state.settings.pipeline_id ?? null,
    })
  }),

  http.post(`${base}/print/outputs/:id/eligibility`, async ({ params, request }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    const body = (await request.json()) as { pipeline_ids: number[] | null }
    const ids = body.pipeline_ids ?? state.pipelines.map((pipeline) => pipeline.id)
    const libraryFileId = output.library_file_id ?? nextNumber()
    state.outputs = state.outputs.map((o) =>
      o.id === output.id ? { ...o, library_file_id: libraryFileId } : o,
    )
    await delay(150)
    return HttpResponse.json({
      library_file_id: libraryFileId,
      reports: ids.map((pipelineId) => ({
        pipeline_id: pipelineId,
        // A pipeline created in this session has no recorded report; treat it as ready,
        // which is what a fresh pipeline built for this plate would answer.
        report: fixtures.eligibilityReports[pipelineId] ?? {
          ok: true,
          target_kind: 'specific_printer',
          target_printer_id: 1,
          target_printer_name: '3DP-31B-598',
          target_model_class: null,
          issues: [],
          printer_reports: [],
        },
      })),
    } satisfies EligibilityOverview)
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
    // #78 — no printer, no hardware to read: a class target nobody has narrowed yet. The
    // pipeline's nozzle is echoed from the query, as the server does.
    const hardware =
      printerId === null
        ? { nozzles: [], pipeline_nozzle_diameter: null }
        : { pipeline_nozzle_diameter: search.get('nozzle_diameter') }
    return HttpResponse.json({
      ...fixtures.filamentOptions,
      ...hardware,
      library_file_id: output.library_file_id ?? fixtures.filamentOptions.library_file_id,
      printer_id: printerId === null ? null : Number(printerId),
    } satisfies FilamentOptions)
  }),

  http.post(`${base}/print/outputs/:id/run`, async ({ params, request }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    const body = (await request.json()) as {
      pipeline_id?: number | null
      copies?: number
      force?: boolean
      printer_id?: number | null
      plate_id?: number
      all_plates?: boolean
      bed_type?: string | null
      filament_plan?: { slots?: { slot_id: number; spool_id: number }[] } | null
      project_id?: number | null
    }
    const pipelineId = body.pipeline_id ?? state.settings.pipeline_id ?? null
    if (pipelineId === null) {
      return problem(409, 'Conflict', 'no slicer pipeline is set for this model')
    }
    const report = fixtures.eligibilityReports[pipelineId]
    if (report && !report.ok && !body.force) {
      // Bambuddy turns the SAME report into a 409 here; the backend passes the body
      // through as the `bambuddy_body` extension rather than paraphrasing it.
      return problem(
        409,
        'Conflict',
        `Bambuddy reported a conflict when asked to run slicer pipeline ${pipelineId}`,
        { type: 'https://scadbuddy.dev/problems/pipeline-ineligible', bambuddy_body: report },
      )
    }
    // Resolved as the server does (#124): an omitted `copies` is the remembered quantity,
    // global → the printer the run keys on → this model, else 1.
    const scopePrinter =
      body.printer_id ??
      state.settings.printer_id ??
      state.pipelines.find((p) => p.id === pipelineId)?.target_printer_id ??
      null
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
    const runId = nextNumber()
    const libraryFileId = output.library_file_id ?? nextNumber()
    state.outputs = state.outputs.map((o) =>
      o.id === output.id
        ? { ...o, library_file_id: libraryFileId, pipeline_run_id: runId }
        : o,
    )
    await delay(200)

    /**
     * #87 — the escalation. A `PipelineRunCreateRequest` carries no printer and no
     * filament mapping, so a request that names either cannot go down the pipeline
     * route at all: the backend slices the library file with the pipeline's own presets
     * and posts queue entries itself. `run` is null on that route — there is no
     * pipeline run to report — which is why the success panel has to guard it.
     */
    // #83 — a plate type or any plate but the first cannot ride on a run either.
    const plateChosen =
      Boolean(body.bed_type) || Boolean(body.all_plates) || (body.plate_id ?? 1) !== 1
    if (body.filament_plan || typeof body.printer_id === 'number' || plateChosen) {
      const sliceJobId = nextNumber()
      const queueItemIds = Array.from({ length: copies }, () => nextNumber())
      const queued: PrintRunResult = {
        route: 'slice_queue',
        pipeline_id: pipelineId,
        library_file_id: libraryFileId,
        printer_id: body.printer_id ?? null,
        run: null,
        slice_job_id: sliceJobId,
        sliced_library_file_id: nextNumber(),
        queue_item_ids: queueItemIds,
        copies,
        warnings: fixtures.filamentOptions.warnings,
        project_id: projectId,
        folder_id: folderId,
        bambuddy_url: `${state.settings.bambuddy_url}/queue`,
      }
      return HttpResponse.json(queued)
    }

    const result: PrintRunResult = {
      route: 'pipeline',
      pipeline_id: pipelineId,
      library_file_id: libraryFileId,
      copies,
      run: {
        id: runId,
        pipeline_id: pipelineId,
        pipeline_name: state.pipelines.find((p) => p.id === pipelineId)?.name ?? null,
        source_library_file_id: libraryFileId,
        source_archive_id: null,
        source_filename: null,
        copies,
        copies_completed: 0,
        copies_failed: 0,
        copies_cancelled: 0,
        copies_in_progress: copies,
        status: 'queued',
        slice_job_id: nextNumber(),
        sliced_library_file_id: nextNumber(),
        eligibility_overridden: Boolean(body.force) && Boolean(report && !report.ok),
        error_message: null,
        jobs: Array.from({ length: copies }, (_unused, index) => ({
          id: nextNumber(),
          pipeline_run_id: runId,
          copy_index: index,
          assigned_printer_id: 1,
          assigned_printer_name: '3DP-31B-598',
          queue_entry_id: nextNumber(),
          status: 'queued',
          error_message: null,
        })),
        target_kind: 'specific_printer',
        target_printer_id: 1,
        target_model_class: null,
        fanout_strategy: 'max_parallel',
      },
      project_id: projectId,
      folder_id: folderId,
      bambuddy_url: `${state.settings.bambuddy_url}/queue`,
    }
    return HttpResponse.json(result)
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

  http.post(`${base}/libraries`, async ({ request }) => {
    const body = (await request.json()) as { name: string; url?: string | null; ref?: string | null }
    const known = state.libraries.find((entry) => entry.name === body.name)
    if (!known && !body.url) {
      return problem(404, 'Not Found', `'${body.name}' is not in the catalogue; give a url to add it`)
    }
    const url = body.url ?? known?.url ?? ''
    const ref = body.ref ?? known?.ref ?? ''
    if (ref === fixtures.MISSING_REF) {
      return problem(502, 'Bad Gateway', `git clone failed: Remote branch ${ref} not found`)
    }
    await delay(100)
    state.seq += 1
    const pin = { url, ref, commit: state.seq.toString(16).padStart(40, 'c') }
    const entry: LibraryEntry = known
      ? { ...known, pin }
      : { name: body.name, url, ref, curated: false, pin }
    state.libraries = known
      ? state.libraries.map((row) => (row.name === body.name ? entry : row))
      : [...state.libraries, entry]
    return HttpResponse.json(entry)
  }),

  http.get(`${base}/settings`, () => HttpResponse.json(state.settings)),

  http.put(`${base}/settings`, async ({ request }) => {
    const body = (await request.json()) as {
      bambuddy_url?: string | null
      bambuddy_api_key?: string
      public_url?: string | null
      library_folder_id?: number | null
      pipeline_id?: number | null
      printer_id?: number | null
    }
    state.settings = {
      ...state.settings,
      ...body,
      has_api_key:
        body.bambuddy_api_key === undefined
          ? state.settings.has_api_key
          : body.bambuddy_api_key.length > 0,
    }
    delete (state.settings as { bambuddy_api_key?: string }).bambuddy_api_key
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
