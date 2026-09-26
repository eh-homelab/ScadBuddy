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
  ModelSummary,
  ModelVersion,
  Output,
  ParamValue,
  PipelineChoices,
  PipelineCreate,
  PipelineView,
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
} from '../api/types'
import { editPath } from '../lib/deeplink'
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
  sources: { 'name-keychain': fixtures.keychainSource } as Record<string, string>,
  settings: { ...fixtures.settings } as Settings,
  printOptions: structuredClone(fixtures.printOptions) as PrintOptionsState,
  jobs: new Map<string, MockJob>(),
  pipelines: [...fixtures.pipelineViews] as PipelineView[],
  /** #86 — per-model default pipelines, the store's `model_pipelines`. */
  modelPipelines: {} as Record<string, number>,
  projects: [...fixtures.projectViews] as ProjectView[],
  /** #79 — per-model projects. No global fallback, unlike the pipeline default. */
  lastProjectId: null as number | null,
  fonts: [...fixtures.fonts] as FontFamily[],
  /** #90 — one git history per model, newest first. */
  versions: structuredClone(fixtures.versions) as Record<string, ModelVersion[]>,
  fontCatalogue: fixtures.fontCatalogue.map((f) => ({ ...f })) as CatalogueFont[],
  catalogueOffline: false,
  sidebarLinkId: 0,
  seq: 0,
}

/** Reset every mutable fixture. Call between tests. */
export function resetMockState(): void {
  state.models = fixtures.models.map((m) => ({ ...m }))
  state.schemas = { ...fixtures.schemas }
  state.outputs = fixtures.outputs.map((o) => ({ ...o }))
  state.sources = { 'name-keychain': fixtures.keychainSource }
  state.settings = { ...fixtures.settings }
  state.printOptions = structuredClone(fixtures.printOptions)
  state.jobs.clear()
  state.pipelines = fixtures.pipelineViews.map((p) => ({ ...p }))
  state.modelPipelines = {}
  state.projects = fixtures.projectViews.map((p) => ({ ...p }))
  state.lastProjectId = null
  state.fonts = fixtures.fonts.map((f) => ({ ...f }))
  state.versions = structuredClone(fixtures.versions)
  state.fontCatalogue = fixtures.fontCatalogue.map((f) => ({ ...f }))
  state.catalogueOffline = false
  state.sidebarLinkId = 0
  state.seq = 0
}

/** Makes `GET /fonts/catalogue` fail, which is the air-gapped case the picker falls back for. */
export function setCatalogueOffline(offline: boolean): void {
  state.catalogueOffline = offline
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
  return entry
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
  http.get(`${base}/models`, () => HttpResponse.json(state.models)),

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
    const model: ModelSummary = {
      slug,
      name: slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
      description: 'Uploaded just now. Open it to see its parameters.',
      tags: ['uploaded'],
      updated_at: new Date().toISOString(),
      has_thumbnail: false,
      has_readme: false,
    }
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
    if (url.hostname === 'makerworld.com' || url.hostname.endsWith('.makerworld.com')) {
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
    }
    state.models = [imported, ...state.models]
    state.schemas[slug] = fixtures.keychainSchema
    state.sources[slug] = 'width = 10;\ncube(width);\n'
    return HttpResponse.json(imported, { status: 201 })
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
    const model = state.models.find((m) => m.slug === slug)
    if (!model) return problem(404, 'Model not found')
    const body = (await request.json()) as {
      source: string
      force?: boolean
      message?: string | null
    }
    const check = checkOf(body.source)
    if (!check.ok && !body.force) return refusal(check)
    state.sources[slug] = body.source
    if (!check.ok) delete state.schemas[slug]
    const version = recordVersion(slug, body.message || `Edit ${slug} source`, [
      { status: 'M', path: 'model.scad' },
    ])
    const updated = { ...model, version: version.commit, updated_at: version.date }
    state.models = state.models.map((m) => (m.slug === slug ? updated : m))
    await delay(120)
    return HttpResponse.json(updated)
  }),

  http.get(`${base}/models/:slug`, ({ params }) => {
    const model = state.models.find((m) => m.slug === params['slug'])
    return model ? HttpResponse.json(model) : problem(404, 'Model not found')
  }),

  http.delete(`${base}/models/:slug`, ({ params }) => {
    if (!state.models.some((m) => m.slug === params['slug'])) {
      return problem(404, 'Not Found', `no model named '${String(params['slug'])}'`)
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
    const patch = entries
      .slice(headIndex, baseIndex < 0 ? headIndex + 1 : baseIndex)
      .map((v) => fixtures.versionPatches[v.commit] ?? '')
      .join('')
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
    } satisfies PipelineChoices)
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
    const printerId = new URL(request.url).searchParams.get('printer_id')
    return HttpResponse.json({
      ...fixtures.filamentOptions,
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
    if (body.filament_plan || typeof body.printer_id === 'number') {
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
