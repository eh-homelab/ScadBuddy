import type {
  AnalysisReport,
  AnalysisRun,
  AnalyzerDecision,
  DecisionCreate,
  Asset,
  AssetUsage,
  AttachResult,
  BambuddyStatus,
  BambuddyTargets,
  ChoicesView,
  ConnectionTest,
  CustomizerSchema,
  DuplicateRequest,
  EditTarget,
  FilamentOptions,
  FontCatalogue,
  FontFamily,
  HeadlessBrowserSetting,
  InstalledFamily,
  Job,
  CatalogueLibrary,
  LibraryPinRequest,
  MediaView,
  ModelPatch,
  LastProject,
  ModelPrintChoices,
  ModelSummary,
  ModelVersion,
  Output,
  OutputPlate,
  ParamPreset,
  ParamPresetCreate,
  ParamPresetDuplicate,
  ParamPresetUpdate,
  PastedSource,
  ParamValue,
  Plate,
  PlateCatalogue,
  PlateFit,
  PrinterBedType,
  PrintProgress,
  PrintRun,
  PrintRunRequest,
  PrintRunResult,
  PrintOptionsState,
  PrintOptionsUpdate,
  PrintOptionsView,
  Problem,
  ProjectAttach,
  ProjectChoices,
  ProjectFile,
  ProjectRequest,
  ProjectView,
  RememberedChoices,
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
import type {
  McpAuthSetting,
  McpAuthUpdate,
  McpTokenCreate,
  McpTokenList,
  MintedMcpToken,
} from './mcpTokens'

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

  /** A bare `(status, detail)` is the shorthand tests and callers use for a plain refusal. */
  constructor(problemOrStatus: Problem | number, detail?: string) {
    const problem: Problem =
      typeof problemOrStatus === 'number'
        ? { title: detail ?? String(problemOrStatus), status: problemOrStatus, detail }
        : problemOrStatus
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
  const response = await send(`${API_BASE}${path}`, {
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
  const response = await send(`${API_BASE}${path}`, { headers: { Accept: 'text/plain' } })
  if (!response.ok) {
    throw new ApiError(await readProblem(response))
  }
  return await response.text()
}

/**
 * The `type` of a problem the client wrote itself because ScadBuddy's server never
 * described the failure: a proxy's own error page in front of it (Envoy's 504 at 15 s,
 * Cloudflare's 524 at ~100 s), or no answer at all (#470).
 */
export const UNANSWERED = 'urn:scadbuddy:unanswered'
/** The `type` of the problem for a request the offline browser could not send. */
export const OFFLINE = 'urn:scadbuddy:offline'
/**
 * The backend's problem for a Bambuddy call that timed out, dropped or answered an
 * error (`bambuddy/errors.py` `UNAVAILABLE_PROBLEM`): the call may have been the
 * enqueue, and Bambuddy may have done it.
 */
export const BAMBUDDY_UNAVAILABLE = 'https://scadbuddy.dev/problems/bambuddy-unavailable'

/**
 * The failure no problem body explained, said by its status. The detail is what the
 * person reads; the status stays on the problem, and in the sentence, for a bug report.
 * Over HTTP/2 there is no status text, so the title falls back to the code.
 */
function unansweredProblem(status: number, statusText: string): Problem {
  const code = `HTTP ${status}`
  const detail =
    status === 504 || status === 524 || status === 408
      ? `The server took too long to answer (${code}).`
      : status === 502 || status === 503
        ? `The server is not answering right now (${code}).`
        : status === 413
          ? `That is too large for the server to accept (${code}).`
          : status >= 500
            ? `The server hit an error it did not describe (${code}).`
            : `The server refused the request without saying why (${code}).`
  return { type: UNANSWERED, title: statusText || code, status, detail }
}

/** A body is a problem when it says something: a `title` or a `detail`. */
function parsedProblem(body: unknown, status: number, statusText: string): Problem {
  if (typeof body !== 'object' || body === null) return unansweredProblem(status, statusText)
  const problem = body as Partial<Problem>
  if (problem.title === undefined && problem.detail === undefined) {
    return unansweredProblem(status, statusText)
  }
  // Spread first so the standard members win, but the extensions survive.
  return {
    ...problem,
    title: problem.title ?? (statusText || `HTTP ${status}`),
    status: problem.status ?? status,
    detail: problem.detail,
  }
}

async function readProblem(response: Response): Promise<Problem> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    return unansweredProblem(response.status, response.statusText)
  }
  return parsedProblem(body, response.status, response.statusText)
}

/** `readProblem` for an `XMLHttpRequest` that has finished. */
function xhrProblem(xhr: XMLHttpRequest): Problem {
  let body: unknown
  try {
    body = JSON.parse(xhr.responseText)
  } catch {
    return unansweredProblem(xhr.status, xhr.statusText)
  }
  return parsedProblem(body, xhr.status, xhr.statusText)
}

/** `fetch`, with a request that got no answer as an `ApiError`. An abort is passed through. */
async function send(url: string, init?: RequestInit): Promise<Response> {
  // Read before sending: a connection that goes offline while a long request waits
  // may already have delivered it, so that is "no answer", not "could not send".
  const offline = navigator.onLine === false
  try {
    return await fetch(url, init)
  } catch (cause) {
    if (init?.signal?.aborted) throw cause
    throw new ApiError(
      offline
        ? {
            type: OFFLINE,
            title: 'Offline',
            status: 0,
            detail: 'This browser is offline, so ScadBuddy could not reach its server.',
          }
        : {
            type: UNANSWERED,
            title: 'No answer',
            status: 0,
            detail:
              'ScadBuddy could not reach its server, or the connection dropped before it answered.',
          },
    )
  }
}

/**
 * Whether a failed request may still have done its work: the server's own answer never
 * arrived, because a proxy gave up waiting (502/504/524) or the connection dropped; or
 * the backend's own call to Bambuddy got no answer, which may have been the enqueue.
 * Any other problem the backend wrote, a 503 (nothing upstream took it) and an offline
 * browser all mean it did not. For a request with a physical effect (a print), retrying
 * one of these blind can do it twice. A failed print run (#470) says so itself: its
 * `may_have_queued` is whether it had tried to queue, which `runPrint` carries over.
 */
export function mayHaveRun(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (typeof error.problem.may_have_queued === 'boolean') return error.problem.may_have_queued
  if (error.problem.type === BAMBUDDY_UNAVAILABLE) return bambuddyUnanswered(error.problem)
  if (error.problem.type !== UNANSWERED) return false
  return [0, 502, 504, 524].includes(error.status)
}

/**
 * A `bambuddy-unavailable` problem is either a timeout or a dropped connection
 * (`errors.py` `map_transport`: 504 or 502, no `bambuddy_status`), or a status Bambuddy
 * answered (`map_response`'s fallback: 502 with `bambuddy_status`). An answer is a "no",
 * unless it is a proxy's in front of Bambuddy that gave up waiting.
 */
function bambuddyUnanswered(problem: Problem): boolean {
  const answered = problem.bambuddy_status
  if (typeof answered === 'number') return [502, 504, 524].includes(answered)
  return problem.status === 502 || problem.status === 504
}

const seg = encodeURIComponent

/**
 * How often `runPrint` reads a running print run (#470), and how many times it tries a
 * request no ScadBuddy answer described again before giving up; tests shorten it.
 */
export const printRunPoll = { intervalMs: 1000, reattempts: 3 }

/**
 * A new `request_id` for one deliberate Print (#470): the server keys the run on it, so
 * a retry of that press re-attaches to its run and the next press is a new print.
 * `getRandomValues`, not `randomUUID`, which only secure contexts have.
 */
export function newRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * The request never got ScadBuddy's own answer: the connection dropped (`send`'s
 * status 0), or a proxy in front answered 502/503/504/524 with a page of its own.
 */
function unanswered(caught: unknown): boolean {
  return (
    caught instanceof ApiError &&
    caught.problem.type === UNANSWERED &&
    [0, 502, 503, 504, 524].includes(caught.status)
  )
}

/** `ms` of waiting that `signal` cuts short, rejecting with its reason. */
function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason)
      return
    }
    const abort = () => {
      clearTimeout(timer)
      reject(signal?.reason)
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', abort, { once: true })
  })
}

/** `attempt`, tried again while it goes unanswered: safe only for a request keyed to its run. */
async function reattach<T>(attempt: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  for (let tries = 0; ; tries++) {
    try {
      return await attempt()
    } catch (caught) {
      if (signal?.aborted || !unanswered(caught) || tries >= printRunPoll.reattempts) throw caught
      await wait(printRunPoll.intervalMs, signal)
    }
  }
}

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

  /** Copies any preset, shipped or saved, to a new saved one with the same values. */
  duplicatePreset: (slug: string, id: string, body: ParamPresetDuplicate) =>
    request<ParamPreset>(`/models/${seg(slug)}/presets/${seg(id)}/duplicate`, {
      method: 'POST',
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

  /** #274 — one image or video of a template. Its id never changes its contents. */
  mediaUrl: (slug: string, item: Pick<MediaView, 'id'>) =>
    `${API_BASE}/models/${seg(slug)}/media/${seg(item.id)}`,

  /** #274 — a video's poster image, or undefined when the item has none. */
  mediaPosterUrl: (slug: string, item: Pick<MediaView, 'id' | 'poster'>) =>
    item.poster ? `${API_BASE}/models/${seg(slug)}/media/${seg(item.id)}/poster` : undefined,

  /**
   * #274 — adds an image or video as the template's last item. XHR rather than
   * `fetch`, which reports no upload progress; a video runs to a gigabyte.
   * `onProgress` gets the fraction sent, 0 to 1.
   */
  uploadMedia: (
    slug: string,
    file: File,
    options: { caption?: string; poster?: File } = {},
    onProgress?: (fraction: number) => void,
  ) =>
    new Promise<ModelSummary>((resolve, reject) => {
      const body = new FormData()
      body.append('file', file)
      if (options.caption) body.append('caption', options.caption)
      if (options.poster) body.append('poster', options.poster)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `${API_BASE}/models/${seg(slug)}/media`)
      xhr.setRequestHeader('Accept', 'application/json')
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable && event.total > 0) onProgress?.(event.loaded / event.total)
      })
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText) as ModelSummary)
          return
        }
        reject(new ApiError(xhrProblem(xhr)))
      }
      // Like a dropped `fetch` (`send`): the server never answered, so the upload may
      // have landed, and `mayHaveRun` says so.
      xhr.onerror = () =>
        reject(
          new ApiError({
            type: UNANSWERED,
            title: 'The upload failed',
            status: 0,
            detail: 'The upload failed',
          }),
        )
      xhr.onabort = () =>
        reject(new ApiError({ title: 'The upload was cancelled', status: 0 }))
      xhr.send(body)
    }),

  patchMedia: (slug: string, id: string, caption: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/media/${seg(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ caption }),
    }),

  /** `ids` names every item once, in the new order; the first is the cover. */
  reorderMedia: (slug: string, ids: string[]) =>
    request<ModelSummary>(`/models/${seg(slug)}/media/order`, {
      method: 'PUT',
      body: JSON.stringify({ ids }),
    }),

  deleteMedia: (slug: string, id: string) =>
    request<ModelSummary>(`/models/${seg(slug)}/media/${seg(id)}`, { method: 'DELETE' }),

  /**
   * #204 — stores an SVG or PNG for a `// file` parameter. The answer's `id` (the
   * SHA-256 of what the server kept) is the value the render takes.
   */
  uploadAsset: (slug: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<Asset>(`/models/${seg(slug)}/assets`, { method: 'POST', body })
  },

  getAsset: (slug: string, id: string) =>
    request<Asset>(`/models/${seg(slug)}/assets/${seg(id)}`),

  /** #296 — the upload store's size against its caps, for Settings. */
  getAssetUsage: () => request<AssetUsage>('/assets/usage'),

  assetContentUrl: (slug: string, id: string) =>
    `${API_BASE}/models/${seg(slug)}/assets/${seg(id)}/content`,

  /**
   * A sample file the template ships beside its source (a `file` parameter's
   * `samples`), at `version` when customizing an older revision.
   */
  sampleContentUrl: (slug: string, name: string, version?: string) =>
    `${API_BASE}/models/${seg(slug)}/samples/${seg(name)}${version ? `?version=${seg(version)}` : ''}`,

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

  /**
   * `version` renders an old revision without restoring it ("Customize this version").
   * `supersedes` names the job this render replaces: the server drops it if no worker
   * has started it yet. Refused (503 + `Retry-After`) only when the server sets
   * SCADBUDDY_RENDER_QUEUE_MAX and that many renders already wait.
   */
  render: (
    slug: string,
    params: Record<string, ParamValue>,
    version?: string,
    supersedes?: string,
  ) =>
    request<RenderAccepted>(`/models/${seg(slug)}/render`, {
      method: 'POST',
      body: JSON.stringify({
        params,
        version: version ?? null,
        ...(supersedes ? { supersedes } : {}),
      }),
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

  /** #316 — `deleteInboxCopies` also deletes the output's copies in Bambuddy's inbox folder. */
  deleteOutput: (id: string, deleteInboxCopies = false) =>
    request<void>(`/outputs/${seg(id)}${deleteInboxCopies ? '?delete_inbox_copies=true' : ''}`, {
      method: 'DELETE',
    }),

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
   * `plateId` is what the print dialog reads another plate of a multi-plate 3MF by;
   * plate 1 already arrives inside `getChoices`. `allPlates` reads every plate at once —
   * one row per slot any plate uses — for an all-plates print.
   */
  getFilaments: (
    outputId: string,
    query: { printerId?: number | null; plateId?: number; allPlates?: boolean } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.plateId !== undefined) search.set('plate_id', String(query.plateId))
    if (query.allPlates) search.set('all_plates', 'true')
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FilamentOptions>(`/print/outputs/${seg(outputId)}/filaments${suffix}`)
  },

  /**
   * spec 2026-09-27 §3 — one read for the whole spool-first print dialog: printers,
   * installed nozzles, quality tiers and processes per nozzle size, plates with the
   * last one used, and the filament step. `printerId` narrows the per-printer bits
   * (mounted nozzles, last plate) the same way `getFilaments`'s does.
   */
  getChoices: (outputId: string, printerId?: number | null) => {
    const search = new URLSearchParams()
    if (printerId !== null && printerId !== undefined) search.set('printer_id', String(printerId))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<ChoicesView>(`/print/outputs/${seg(outputId)}/choices${suffix}`)
  },

  /**
   * spec 2026-09-27 §4 — the spool-first run: no pipeline is named, every slicer preset
   * is derived server-side from the dialog's spools, nozzles, quality and plate.
   * `signal` stops following the run (the dialog went away); the run itself goes on.
   */
  runPrint: async (
    outputId: string,
    body: PrintRunRequest,
    signal?: AbortSignal,
  ): Promise<PrintRunResult> => {
    // #470: the server answers 202 with a run and slices and queues in the background,
    // since that takes longer than the proxies in front wait. A repeat of the same
    // request (the same `request_id`) is the same run, so re-sending it after an
    // answer that never arrived re-attaches to that run and never queues a second print.
    let run = await reattach(
      () =>
        request<PrintRun>(`/print/outputs/${seg(outputId)}/run`, {
          method: 'POST',
          body: JSON.stringify(body),
          signal,
        }),
      signal,
    )
    while (run.status === 'running') {
      await wait(printRunPoll.intervalMs, signal)
      const id = run.id
      run = await reattach(() => request<PrintRun>(`/print/runs/${seg(id)}`, { signal }), signal)
    }
    if (run.status === 'failed' || !run.result) {
      const error = run.error
      const detail = error?.detail ?? 'The print run ended without a result.'
      throw new ApiError({
        ...error?.extensions,
        // The problem's type, so the failure reads as a synchronous answer would have.
        type: error?.type,
        title: error?.title ?? 'Print failed',
        status: error?.status ?? 500,
        detail,
        // Whether the run had already tried to queue (a queue call that timed out, a
        // later plate failing after an earlier one was queued, or a run lost while
        // queueing): `mayHaveRun` reads it, so the dialog says to check the queue.
        may_have_queued: run.may_have_queued,
      })
    }
    return run.result
  },

  /**
   * #284 — judge an output against the request the print dialog would send. Reads only:
   * nothing is uploaded, sliced or queued (`post_run`, backend/scadbuddy/api/analyzers.py).
   */
  runAnalyzers: (body: AnalysisRun) =>
    request<AnalysisReport>('/analyzers/run', { method: 'POST', body: JSON.stringify(body) }),

  /**
   * #284 — ignore or suppress a finding at a scope; a suppression needs a reason. It
   * replaces an earlier decision about the same rule and instance at that scope
   * (`post_decision`, backend/scadbuddy/api/analyzers.py).
   */
  createDecision: (body: DecisionCreate) =>
    request<AnalyzerDecision>('/analyzers/decisions', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #284 — remove a decision, so its finding is open again (`delete_decision`). */
  deleteDecision: (id: string) =>
    request<void>(`/analyzers/decisions/${seg(id)}`, { method: 'DELETE' }),

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

  /** #317 — the project both pickers open on; `null` is "No project". */
  rememberProject: (projectId: number | null) =>
    request<LastProject>('/print/projects/last', {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId } satisfies LastProject),
    }),

  /**
   * #317 — put a generated output's editable 3MF in the project's Bambuddy folder.
   * Idempotent: the same project again answers with the file already there.
   */
  fileIntoProject: (outputId: string, projectId: number) =>
    request<ProjectFile>(`/outputs/${seg(outputId)}/project-file`, {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId }),
    }),

  /** Filed after the run, never during it: a plate's queue item only exists once it has
   * sliced, and an archive only once a print has finished, so the ids come from the
   * progress read (#89). */
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

  /** With `index` (#217), only the invalid entry at that position of `libraries`. */
  unpinModelLibrary: (slug: string, name: string, index?: number) =>
    request<ModelSummary>(
      `/models/${seg(slug)}/libraries/${seg(name)}${index === undefined ? '' : `?index=${index}`}`,
      { method: 'DELETE' },
    ),

  getSettings: () => request<Settings>('/settings'),

  putSettings: (body: SettingsUpdate) =>
    request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(body) }),

  getPrintOptions: () => request<PrintOptionsState>('/settings/print-options'),

  /** Replaces one scope wholesale; an all-unset `options` clears it. */
  putPrintOptions: (body: PrintOptionsUpdate) =>
    request<PrintOptionsView>('/settings/print-options', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  /** Tests what is *stored*, so the key never travels back out of the server. */
  testSettings: () => request<ConnectionTest>('/settings/test', { method: 'POST' }),

  getBambuddyTargets: () => request<BambuddyTargets>('/settings/targets'),

  /** #322 — Bambuddy's version and whether it captures a finish photo; read-only. */
  getBambuddyStatus: () => request<BambuddyStatus>('/settings/bambuddy'),

  /** #322 — what the print dialog remembers. Each entry is forgotten by its own route. */
  getRemembered: () => request<RememberedChoices>('/settings/remembered'),

  forgetAllRemembered: () => request<RememberedChoices>('/settings/remembered', { method: 'DELETE' }),

  registerSidebar: () =>
    request<SidebarLink>('/settings/register-sidebar', { method: 'POST' }),

  /**
   * The AI agent's headless browser (#349), served by the agent service under
   * `/api/v1/ai/*`. Fails (404 or 503) when there is no agent or no AI database.
   */
  getHeadlessBrowserSetting: () =>
    request<HeadlessBrowserSetting>('/ai/settings/headless-browser'),

  putHeadlessBrowserSetting: (enabled: boolean) =>
    request<HeadlessBrowserSetting>('/ai/settings/headless-browser', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  /** #251 — the agent service's MCP bearer tokens: metadata only. */
  listMcpTokens: () => request<McpTokenList>('/ai/mcp-tokens'),

  /** The plaintext token is in this response and nowhere else. */
  createMcpToken: (body: McpTokenCreate) =>
    request<MintedMcpToken>('/ai/mcp-tokens', { method: 'POST', body: JSON.stringify(body) }),

  revokeMcpToken: (id: string) =>
    request<undefined>(`/ai/mcp-tokens/${seg(id)}`, { method: 'DELETE' }),

  /** #251 — the /mcp auth mode and anonymous cap (AI design spec §8.3). */
  getMcpAuth: () => request<McpAuthSetting>('/ai/mcp/auth'),

  setMcpAuth: (body: McpAuthUpdate) =>
    request<McpAuthSetting>('/ai/mcp/auth', { method: 'PUT', body: JSON.stringify(body) }),
}
