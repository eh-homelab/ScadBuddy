import type {
  AttachResult,
  BambuddyTargets,
  ConnectionTest,
  CustomizerSchema,
  DuplicateRequest,
  EditTarget,
  EligibilityOverview,
  FilamentOptions,
  FontCatalogue,
  FontFamily,
  InstalledFamily,
  Job,
  CatalogueLibrary,
  LibraryPinRequest,
  ModelPatch,
  ModelPrintChoices,
  ModelSummary,
  ModelVersion,
  Output,
  OutputPlate,
  ParamPreset,
  ParamPresetCreate,
  ParamPresetUpdate,
  PastedSource,
  ParamValue,
  PipelineChoices,
  PipelineCreate,
  PipelineDefault,
  PipelineView,
  Plate,
  PlateCatalogue,
  PlateFit,
  PresetOptions,
  PresetRef,
  PrinterBedType,
  PrintProgress,
  PrintRunRequest,
  PrintRunResult,
  PrintOptionsState,
  PrintOptionsUpdate,
  PrintOptionsView,
  Problem,
  ProjectAttach,
  ProjectChoices,
  ProjectRequest,
  ProjectView,
  RenderAccepted,
  SendRequest,
  SendResult,
  Settings,
  SettingsUpdate,
  SidebarLink,
  SourceCheck,
  UpstreamMerge,
  UpstreamStatus,
  UrlImport,
  VersionDiff,
} from './types'

export const API_BASE = '/api/v1'

/** What a model thumbnail's URL is keyed on (#179). */
export type ThumbnailKeyed = Pick<
  ModelSummary,
  'slug' | 'version' | 'thumbnail_source' | 'thumbnail_output_id' | 'thumbnail_preview_id'
>

/** The optional parts of a model upload besides its source (#179). */
export interface UploadExtras {
  /** What the source is called on the wire, which is where the slug comes from. */
  filename?: string
  /** A bundled model's `model.json`. */
  meta?: File
  thumbnail?: File
  readme?: File
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  readonly problem: Problem

  constructor(problem: Problem) {
    // The detail is the sentence written for a person ("OpenSCAD could not build a
    // customizer schema from this model's source"); the title is the status name.
    super(problem.detail ?? problem.title)
    this.name = 'ApiError'
    this.status = problem.status
    this.detail = problem.detail ?? problem.title
    this.problem = problem
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body instanceof FormData || init?.body instanceof Blob
        ? {}
        : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    throw new ApiError(await readProblem(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

/**
 * For the routes that answer with a text body rather than JSON: the model's
 * source (`GET /models/{slug}/source`, text/plain) and its README
 * (`GET /models/{slug}/readme`, text/markdown).
 */
async function requestText(path: string): Promise<string> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { Accept: 'text/plain' } })
  if (!response.ok) {
    throw new ApiError(await readProblem(response))
  }
  return await response.text()
}

async function readProblem(response: Response): Promise<Problem> {
  try {
    const body = (await response.json()) as Partial<Problem>
    // Spread first so the standard members win, but the extensions survive.
    return {
      ...body,
      title: body.title ?? response.statusText,
      status: body.status ?? response.status,
      detail: body.detail,
    }
  } catch {
    return { title: response.statusText || 'Request failed', status: response.status }
  }
}

const seg = encodeURIComponent

export const api = {
  listModels: () => request<ModelSummary[]>('/models'),

  getModel: (slug: string) => request<ModelSummary>(`/models/${seg(slug)}`),

  /**
   * `extras` are the other files of a bundled model's directory (#179). `filename`
   * renames the source on the wire, because the server takes the slug from it: a
   * dropped `models/<slug>/` sends `model.scad`, which would otherwise become `model`.
   */
  uploadModel: (file: File, extras: UploadExtras = {}) => {
    const body = new FormData()
    body.append('file', file, extras.filename ?? file.name)
    if (extras.meta) body.append('meta', extras.meta)
    if (extras.thumbnail) body.append('thumbnail', extras.thumbnail)
    if (extras.readme) body.append('readme', extras.readme)
    return request<ModelSummary>('/models', { method: 'POST', body })
  },

  /** Multipart with a `file` part, like the output thumbnail PUT. */
  setThumbnail: (slug: string, png: Blob) => {
    const body = new FormData()
    body.append('file', png, 'thumbnail.png')
    return request<ModelSummary>(`/models/${seg(slug)}/thumbnail`, { method: 'PUT', body })
  },

  /**
   * Removes the model's own thumbnail. The record that comes back may still have one:
   * a generated model falls back to its first output's plate image.
   */
  removeThumbnail: (slug: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/thumbnail`, { method: 'DELETE' }),

  /** The README's Markdown, or null when the model has none. */
  getReadme: async (slug: string): Promise<string | null> => {
    try {
      return await requestText(`/models/${seg(slug)}/readme`)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) return null
      throw caught
    }
  },

  setReadme: (slug: string, content: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/readme`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),

  removeReadme: (slug: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/readme`, { method: 'DELETE' }),

  /** The template's shipped presets, then the ones saved on it. */
  listPresets: (slug: string) => request<ParamPreset[]>(`/models/${seg(slug)}/presets`),

  createPreset: (slug: string, body: ParamPresetCreate) =>
    request<ParamPreset>(`/models/${seg(slug)}/presets`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updatePreset: (slug: string, id: string, body: ParamPresetUpdate) =>
    request<ParamPreset>(`/models/${seg(slug)}/presets/${seg(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  deletePreset: (slug: string, id: string) =>
    request<void>(`/models/${seg(slug)}/presets/${seg(id)}`, { method: 'DELETE' }),

  /** The pasted-source twin of `uploadModel`: same route, JSON body, same code path. */
  createModelFromSource: (body: PastedSource) =>
    request<ModelSummary>('/models', { method: 'POST', body: JSON.stringify(body) }),

  /** #153 — fetched on the server, then created through the same path as a paste. */
  importModel: (body: UrlImport) =>
    request<ModelSummary>('/models/import', { method: 'POST', body: JSON.stringify(body) }),

  getSource: (slug: string) => requestText(`/models/${seg(slug)}/source`),

  /**
   * Replaces the source as one revision in the model's history, named by `message`
   * when given. Parse-checked server-side unless `force`.
   */
  replaceSource: (slug: string, source: string, force = false, message?: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/source`, {
      method: 'PUT',
      body: JSON.stringify({ source, force, message: message ?? null }),
    }),

  /**
   * Parse-only: runs OpenSCAD over the source and saves nothing. `slug` names the
   * model the source belongs to, so its `include` of a sibling file resolves against
   * that model's directory instead of an empty one.
   *
   * `signal` matters here: the server runs these checks under its own small
   * concurrency budget, so a superseded keystroke's check must be abandoned on the
   * wire rather than merely ignored on arrival — otherwise it holds a permit the
   * check the user is waiting for needs.
   */
  checkSource: (source: string, slug?: string, signal?: AbortSignal) =>
    request<SourceCheck>('/models/check', {
      method: 'POST',
      body: JSON.stringify({ source, slug: slug ?? null }),
      signal,
    }),

  /** Metadata: name, description, tags. Libraries have their own routes below. */
  updateModel: (slug: string, patch: ModelPatch) =>
    request<ModelSummary>(`/models/${seg(slug)}`, { method: 'PATCH', body: JSON.stringify(patch) }),

  /** #156 — a new template of mine copied from `slug`, recording it as `upstream`. */
  duplicateModel: (slug: string, name: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/duplicate`, {
      method: 'POST',
      body: JSON.stringify({ name } satisfies DuplicateRequest),
    }),

  /** 409 while duplicates track it (see `trackingDuplicates`); `force` deletes it anyway. */
  deleteModel: (slug: string, force = false) =>
    request<void>(`/models/${seg(slug)}${force ? '?force=true' : ''}`, { method: 'DELETE' }),

  /** #157 — a duplicate's upstream: its state, and on `update` the merge it would make. */
  getUpstream: (slug: string) => request<UpstreamStatus>(`/models/${seg(slug)}/upstream`),

  /** A conflicted merge answers 409 with `merged` and `merge_base` and writes nothing. */
  mergeUpstream: (slug: string) =>
    request<UpstreamMerge>(`/models/${seg(slug)}/upstream/merge`, { method: 'POST' }),

  dismissUpstream: (slug: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/upstream/dismiss`, { method: 'POST' }),

  detachUpstream: (slug: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/upstream/detach`, { method: 'POST' }),

  /**
   * Saves the resolution of a conflicted upstream merge: the same write as
   * `replaceSource`, which also advances the duplicate's `base` to `mergeBase`.
   */
  resolveUpstreamMerge: (slug: string, source: string, mergeBase: string, force = false) =>
    request<ModelSummary>(`/models/${seg(slug)}/source?merge_base=${seg(mergeBase)}`, {
      method: 'PUT',
      body: JSON.stringify({ source, force, message: null }),
    }),


  /**
   * The `v` param is only there to change the URL when the image does: an `<img>`
   * already on the page does not refetch the same URL. It joins the model's
   * revision (a thumbnail set or removed is a commit) with where the image comes
   * from and, for the output fallback, which output -- that fallback moves with no
   * commit when the covering output is deleted or another becomes the first (#179)
   * -- and, for the default-render preview, which render, as a re-render after a
   * source edit is a new image with no commit of its own.
   */
  modelThumbnailUrl: (model: ThumbnailKeyed) => {
    const key = [
      model.version,
      model.thumbnail_source,
      model.thumbnail_output_id,
      model.thumbnail_preview_id,
    ]
      .map((part) => part ?? '')
      .join('.')
    return `${API_BASE}/models/${seg(model.slug)}/thumbnail${key === '...' ? '' : `?v=${seg(key)}`}`
  },

  /** The editor's openscad-lsp socket: a saved model's directory, or a scratch one. */
  languageServerPath: (slug?: string) =>
    slug ? `${API_BASE}/models/${seg(slug)}/lsp` : `${API_BASE}/lsp`,

  /** A `version` reads that revision's schema instead of the model's current one. */
  getSchema: (slug: string, version?: string) =>
    request<CustomizerSchema>(
      version
        ? `/models/${seg(slug)}/versions/${seg(version)}/schema`
        : `/models/${seg(slug)}/schema`,
    ),

  listVersions: (slug: string) => request<ModelVersion[]>(`/models/${seg(slug)}/versions`),

  /** `base` omitted diffs against the revision's parent. */
  getVersionDiff: (slug: string, version: string, base?: string) =>
    request<VersionDiff>(
      `/models/${seg(slug)}/versions/${seg(version)}/diff${base ? `?base=${seg(base)}` : ''}`,
    ),

  restoreVersion: (slug: string, version: string) =>
    request<ModelVersion>(`/models/${seg(slug)}/versions/${seg(version)}/restore`, {
      method: 'POST',
    }),

  /** `version` renders an old revision without restoring it ("Customize this version"). */
  render: (slug: string, params: Record<string, ParamValue>, version?: string) =>
    request<RenderAccepted>(`/models/${seg(slug)}/render`, {
      method: 'POST',
      body: JSON.stringify({ params, version: version ?? null }),
    }),

  getJob: (jobId: string) => request<Job>(`/jobs/${seg(jobId)}`),

  previewUrl: (jobId: string) => `${API_BASE}/jobs/${seg(jobId)}/preview.glb`,

  createOutput: (slug: string, jobId: string, name?: string) =>
    request<Output>(`/models/${seg(slug)}/outputs`, {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, name: name ?? null }),
    }),

  listOutputs: (slug: string) => request<Output[]>(`/models/${seg(slug)}/outputs`),

  getOutput: (id: string) => request<Output>(`/outputs/${seg(id)}`),

  /** Resolves an `/edit/{id}` deep link — from the record, or from the 3MF. */
  getEditTarget: (id: string) => request<EditTarget>(`/outputs/${seg(id)}/edit`),

  deleteOutput: (id: string) => request<void>(`/outputs/${seg(id)}`, { method: 'DELETE' }),

  downloadUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/model.3mf`,

  outputThumbnailUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/thumbnail`,

  /** #83 — the 3MF's plates; ScadBuddy's own renders are always one. */
  getOutputPlates: (id: string) => request<OutputPlate[]>(`/outputs/${seg(id)}/plates`),

  outputPlateThumbnailUrl: (id: string, index: number) =>
    `${API_BASE}/outputs/${seg(id)}/plates/${index}/thumbnail`,

  sendOutput: (id: string, body: SendRequest) =>
    request<SendResult>(`/outputs/${seg(id)}/send`, { method: 'POST', body: JSON.stringify(body) }),

  /** Multipart with a `file` part — not a raw PNG body, and no `.png` in the path. */
  putThumbnail: (outputId: string, png: Blob) => {
    const body = new FormData()
    body.append('file', png, 'thumbnail.png')
    return request<void>(`/outputs/${seg(outputId)}/thumbnail`, { method: 'PUT', body })
  },

  /**
   * #86 — the print picker. `printerPreset` is what narrows the process and filament
   * tiers: unfiltered they are thousands of rows, so the server only sends them once a
   * printer preset is named.
   */
  getPrintPresets: (printerPreset?: PresetRef) => {
    const query = printerPreset
      ? `?printer_preset_source=${seg(printerPreset.source)}&printer_preset_id=${seg(printerPreset.id)}`
      : ''
    return request<PresetOptions>(`/print/presets${query}`)
  },

  createPipeline: (body: PipelineCreate) =>
    request<PipelineView>('/print/pipelines', { method: 'POST', body: JSON.stringify(body) }),

  getModelPipelines: (slug: string) =>
    request<PipelineChoices>(`/print/models/${seg(slug)}/pipelines`),

  /** `null` clears this model's default, falling back to the global one. */
  putModelPipeline: (slug: string, pipelineId: number | null) =>
    request<PipelineDefault>(`/print/models/${seg(slug)}/pipeline`, {
      method: 'PUT',
      body: JSON.stringify({ pipeline_id: pipelineId }),
    }),

  /** #78 — replaces this model's remembered printer and spools; empty forgets them. */
  putModelChoices: (slug: string, body: ModelPrintChoices) =>
    request<ModelPrintChoices>(`/print/models/${seg(slug)}/choices`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  /** #83 — the plate on this printer, which the picker opens on next; `null` forgets it. */
  putPrinterBedType: (printerId: number, bedType: string | null) =>
    request<PrinterBedType>(`/print/printers/${printerId}/bed-type`, {
      method: 'PUT',
      body: JSON.stringify({ bed_type: bedType }),
    }),

  /** Uploads the 3MF if Bambuddy has not got it yet, then asks each pipeline. */
  checkEligibility: (outputId: string, pipelineIds?: number[]) =>
    request<EligibilityOverview>(`/print/outputs/${seg(outputId)}/eligibility`, {
      method: 'POST',
      body: JSON.stringify({ pipeline_ids: pipelineIds ?? null }),
    }),

  /**
   * #87 — one read per output, because the join is the server's job. The spool
   * inventory, where each spool is assigned, the printer's live AMS state and the
   * per-nozzle slicer presets are four separate Bambuddy routes; doing that join in the
   * browser would mean four round trips and ScadBuddy's own copy of the rules.
   *
   * `printerId` is what makes `loaded` mean "loaded *here*" and what makes reachability
   * answerable at all — without one the server can say where a spool is but not whether
   * the chosen slot can reach it.
   *
   * `nozzleDiameter` is the pipeline's, as `PipelineView.nozzle_diameter` reported it;
   * the server compares it with the printer's mounted nozzles (#78).
   */
  getFilaments: (
    outputId: string,
    query: { printerId?: number | null; nozzleDiameter?: string | null; plateId?: number } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.nozzleDiameter) search.set('nozzle_diameter', query.nozzleDiameter)
    if (query.plateId !== undefined) search.set('plate_id', String(query.plateId))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FilamentOptions>(`/print/outputs/${seg(outputId)}/filaments${suffix}`)
  },

  runPipeline: (outputId: string, body: PrintRunRequest) =>
    request<PrintRunResult>(`/print/outputs/${seg(outputId)}/run`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * #79 — Bambuddy's projects, each with the library folder that belongs to it, plus
   * the one the last send went to so the picker opens where it was left. ScadBuddy
   * models no relationship between a model and a project: which prints belong to a
   * project is on the project's own page.
   */
  getProjects: () => request<ProjectChoices>('/print/projects'),

  /**
   * `project_id` links an existing project; otherwise `name` creates one. Either way the
   * project comes back with a library folder, because it is the folder — not the project
   * row — that makes Bambuddy's project page list the files.
   */
  createProject: (body: ProjectRequest) =>
    request<ProjectView>('/print/projects', { method: 'POST', body: JSON.stringify(body) }),

  /** Filed after the run, never during it: a pipeline run's `jobs[].queue_entry_id` is
   * null when Bambuddy answers 202, and an archive only exists once a print has finished,
   * so the ids come from the progress read (#89). */
  attachToProject: (outputId: string, body: ProjectAttach) =>
    request<AttachResult>(`/print/outputs/${seg(outputId)}/project`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * #89 — how the last print of this output is going. A `null` body is the answer for
   * an output that has never been printed, so it is passed straight through: turning it
   * into an error here would make "not printed yet" indistinguishable from a broken read.
   */
  getPrintProgress: (outputId: string) =>
    request<PrintProgress | null>(`/print/outputs/${seg(outputId)}/progress`),

  listFonts: () => request<FontFamily[]>('/fonts'),

  /** The Google Fonts catalogue, fetched and cached server-side — no API key in the browser. */
  listFontCatalogue: (query: { q?: string; category?: string; limit?: number } = {}) => {
    const search = new URLSearchParams()
    if (query.q) search.set('q', query.q)
    if (query.category) search.set('category', query.category)
    if (query.limit !== undefined) search.set('limit', String(query.limit))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FontCatalogue>(`/fonts/catalogue${suffix}`)
  },

  /** Downloads the family onto the data volume so the renderer can resolve it. */
  installFont: (family: string) =>
    request<InstalledFamily>('/fonts/install', {
      method: 'POST',
      body: JSON.stringify({ family }),
    }),

  /** #81 — an absent or unknown model answers with the configured default plate. */
  getPlate: (model: string | null) =>
    request<Plate>(model ? `/plate?${new URLSearchParams({ model })}` : '/plate'),

  /**
   * #81 — whether a model of `size` can be sent to `model`'s printer, judged by the same
   * placement the send runs. `colours` above one needs a prime tower, as it does there.
   */
  getPlateFit: (model: string | null, size: number[], colours: number) => {
    const [x = 0, y = 0, z = 0] = size
    const search = new URLSearchParams({
      x: String(x),
      y: String(y),
      z: String(z),
      colours: String(Math.max(colours, 1)),
    })
    if (model) search.set('model', model)
    return request<PlateFit>(`/plate/fit?${search}`)
  },

  listPlates: () => request<PlateCatalogue>('/plates'),

  /** #93 — the curated catalogue: libraries the server knows, each with a suggested ref. */
  listLibraries: () => request<CatalogueLibrary[]>('/libraries'),

  /**
   * Clones the library at `ref` server-side and pins the resolved commit into this
   * model only. `url`/`ref` default to the catalogue's; re-pinning is the same call.
   */
  pinModelLibrary: (slug: string, name: string, body: LibraryPinRequest) =>
    request<ModelSummary>(`/models/${seg(slug)}/libraries/${seg(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  unpinModelLibrary: (slug: string, name: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/libraries/${seg(name)}`, { method: 'DELETE' }),

  getSettings: () => request<Settings>('/settings'),

  putSettings: (body: SettingsUpdate) =>
    request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(body) }),

  /**
   * `slug` so the server resolves the printer *this model's* pipeline aims at (#86), and
   * `pipelineId` when the picker has chosen a different one (#145).
   */
  getPrintOptions: (slug?: string, pipelineId?: number | null) => {
    const query = new URLSearchParams()
    if (slug) query.set('slug', slug)
    if (pipelineId !== undefined && pipelineId !== null) query.set('pipeline_id', String(pipelineId))
    const qs = query.toString()
    return request<PrintOptionsState>(`/settings/print-options${qs ? `?${qs}` : ''}`)
  },

  /** Replaces one scope wholesale; an all-unset `options` clears it. */
  putPrintOptions: (body: PrintOptionsUpdate) =>
    request<PrintOptionsView>('/settings/print-options', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  /** Tests what is *stored*, so the key never travels back out of the server. */
  testSettings: () => request<ConnectionTest>('/settings/test', { method: 'POST' }),

  getBambuddyTargets: () => request<BambuddyTargets>('/settings/targets'),

  registerSidebar: () =>
    request<SidebarLink>('/settings/register-sidebar', { method: 'POST' }),
}
