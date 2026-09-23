import type {
  BambuddyTargets,
  ConnectionTest,
  FontFamily,
  Job,
  ModelSchema,
  ModelSummary,
  Output,
  ParamValue,
  Problem,
  SendRequest,
  SendResult,
  Settings,
  SettingsUpdate,
} from './types'

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(problem: Problem) {
    super(problem.title)
    this.name = 'ApiError'
    this.status = problem.status
    this.detail = problem.detail ?? problem.title
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
    return {
      title: body.title ?? response.statusText,
      status: body.status ?? response.status,
      detail: body.detail,
    }
  } catch {
    return { title: response.statusText || 'Request failed', status: response.status }
  }
}

export const api = {
  listModels: () => request<ModelSummary[]>('/models'),

  getModel: (slug: string) => request<ModelSummary>(`/models/${encodeURIComponent(slug)}`),

  uploadModel: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<ModelSummary>('/models', { method: 'POST', body })
  },

  deleteModel: (slug: string) =>
    request<void>(`/models/${encodeURIComponent(slug)}`, { method: 'DELETE' }),

  getSchema: (slug: string) => request<ModelSchema>(`/models/${encodeURIComponent(slug)}/schema`),

  render: (slug: string, params: Record<string, ParamValue>) =>
    request<{ job_id: string }>(`/models/${encodeURIComponent(slug)}/render`, {
      method: 'POST',
      body: JSON.stringify({ params }),
    }),

  getJob: (jobId: string) => request<Job>(`/jobs/${encodeURIComponent(jobId)}`),

  previewUrl: (jobId: string) => `${API_BASE}/jobs/${encodeURIComponent(jobId)}/preview.glb`,

  createOutput: (slug: string, jobId: string) =>
    request<Output>(`/models/${encodeURIComponent(slug)}/outputs`, {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId }),
    }),

  listOutputs: (slug: string) => request<Output[]>(`/models/${encodeURIComponent(slug)}/outputs`),

  deleteOutput: (id: string) =>
    request<void>(`/outputs/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  downloadUrl: (id: string) => `${API_BASE}/outputs/${encodeURIComponent(id)}/model.3mf`,

  sendOutput: (id: string, body: SendRequest) =>
    request<SendResult>(`/outputs/${encodeURIComponent(id)}/send`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  putThumbnail: async (outputId: string, png: Blob) => {
    const response = await fetch(
      `${API_BASE}/outputs/${encodeURIComponent(outputId)}/thumbnail.png`,
      { method: 'PUT', body: png, headers: { 'Content-Type': 'image/png' } },
    )
    if (!response.ok) throw new ApiError(await readProblem(response))
  },

  listFonts: () => request<FontFamily[]>('/fonts'),

  getSettings: () => request<Settings>('/settings'),

  putSettings: (body: SettingsUpdate) =>
    request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(body) }),

  testSettings: (body: SettingsUpdate) =>
    request<ConnectionTest>('/settings/test', { method: 'POST', body: JSON.stringify(body) }),

  getBambuddyTargets: () => request<BambuddyTargets>('/settings/targets'),

  registerSidebar: () => request<{ ok: boolean; detail: string }>('/settings/register-sidebar', {
    method: 'POST',
  }),
}
