import type {
  AttachResult,
  BambuddyTargets,
  ConnectionTest,
  CustomizerSchema,
  EligibilityOverview,
  FilamentOptions,
  FontCatalogue,
  FontFamily,
  InstalledFamily,
  Job,
  ModelSummary,
  ModelVersion,
  Output,
  ParamValue,
  PipelineChoices,
  PipelineCreate,
  PipelineDefault,
  PipelineView,
  PresetOptions,
  PresetRef,
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
  VersionDiff,
} from './types'

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  readonly problem: Problem

  constructor(problem: Problem) {
    super(problem.title)
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

  uploadModel: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<ModelSummary>('/models', { method: 'POST', body })
  },

  deleteModel: (slug: string) => request<void>(`/models/${seg(slug)}`, { method: 'DELETE' }),

  modelThumbnailUrl: (slug: string) => `${API_BASE}/models/${seg(slug)}/thumbnail`,

  /** A `version` reads that revision's schema instead of the model's current one. */
  getSchema: (slug: string, version?: string) =>
    request<CustomizerSchema>(
      version
        ? `/models/${seg(slug)}/versions/${seg(version)}/schema`
        : `/models/${seg(slug)}/schema`,
    ),

  /** #90 — replaces the source as one revision. The hook the paste/edit path calls. */
  putSource: (slug: string, source: string, message?: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/source`, {
      method: 'PUT',
      body: JSON.stringify({ source, message: message ?? null }),
    }),

  listVersions: (slug: string) => request<ModelVersion[]>(`/models/${seg(slug)}/versions`),

  getVersionSourceUrl: (slug: string, version: string) =>
    `${API_BASE}/models/${seg(slug)}/versions/${seg(version)}/source`,

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

  deleteOutput: (id: string) => request<void>(`/outputs/${seg(id)}`, { method: 'DELETE' }),

  downloadUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/model.3mf`,

  outputThumbnailUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/thumbnail`,

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
   */
  getFilaments: (
    outputId: string,
    query: { printerId?: number | null; pipelineId?: number | null; plateId?: number } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.pipelineId !== null && query.pipelineId !== undefined) {
      search.set('pipeline_id', String(query.pipelineId))
    }
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

  getSettings: () => request<Settings>('/settings'),

  putSettings: (body: SettingsUpdate) =>
    request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(body) }),

  /** `slug` so the server resolves the printer *this model's* pipeline aims at (#86). */
  getPrintOptions: (slug?: string) =>
    request<PrintOptionsState>(
      slug ? `/settings/print-options?slug=${seg(slug)}` : '/settings/print-options',
    ),

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
