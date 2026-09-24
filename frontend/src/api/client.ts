import type {
  BambuddyTargets,
  ConnectionTest,
  CustomizerSchema,
  EditTarget,
  EligibilityOverview,
  FontCatalogue,
  FontFamily,
  InstalledFamily,
  Job,
  ModelSummary,
  Output,
  ParamValue,
  PipelineChoices,
  PipelineCreate,
  PipelineDefault,
  PipelineView,
  PresetOptions,
  PresetRef,
  PrintRunRequest,
  PrintRunResult,
  PrintOptionsState,
  PrintOptionsUpdate,
  PrintOptionsView,
  Problem,
  RenderAccepted,
  SendRequest,
  SendResult,
  Settings,
  SettingsUpdate,
  SidebarLink,
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

  getSchema: (slug: string) => request<CustomizerSchema>(`/models/${seg(slug)}/schema`),

  render: (slug: string, params: Record<string, ParamValue>) =>
    request<RenderAccepted>(`/models/${seg(slug)}/render`, {
      method: 'POST',
      body: JSON.stringify({ params }),
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

  runPipeline: (outputId: string, body: PrintRunRequest) =>
    request<PrintRunResult>(`/print/outputs/${seg(outputId)}/run`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

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
