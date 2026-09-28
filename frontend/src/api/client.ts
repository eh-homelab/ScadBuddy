import type {
  Asset,
  AssetUsage,
  AttachResult,
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
import type { McpTokenCreate, McpTokenList, MintedMcpToken } from './mcpTokens'

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

/** `readProblem` for an `XMLHttpRequest` that has finished. */
function xhrProblem(xhr: XMLHttpRequest): Problem {
  try {
    const body = JSON.parse(xhr.responseText) as Partial<Problem>
    return {
      ...body,
      title: body.title ?? xhr.statusText,
      status: body.status ?? xhr.status,
      detail: body.detail,
    }
  } catch {
    return { title: xhr.statusText || 'Request failed', status: xhr.status }
  }
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
 * The request never got ScadBuddy's own answer: the connection dropped (fetch's
 * `TypeError`), or a proxy in front answered 502/503/504 with a page of its own. The
 * backend's problems always carry a `detail`.
 */
function unanswered(caught: unknown): boolean {
  if (caught instanceof TypeError) return true
  return (
    caught instanceof ApiError &&
    caught.problem.detail === undefined &&
    [502, 503, 504].includes(caught.status)
  )
}

/** `attempt`, tried again while it goes unanswered: safe only for a request keyed to its run. */
async function reattach<T>(attempt: () => Promise<T>): Promise<T> {
  for (let tries = 0; ; tries++) {
    try {
      return await attempt()
    } catch (caught) {
      if (!unanswered(caught) || tries >= printRunPoll.reattempts) throw caught
      await new Promise((resolve) => setTimeout(resolve, printRunPoll.intervalMs))
    }
  }
}

/** Appended to a failed run that had already tried to queue (#470). */
export const MAY_HAVE_QUEUED =
  "The print may still have been queued: check Bambuddy's queue before printing again."

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
      xhr.onerror = () =>
        reject(new ApiError({ title: 'The upload failed', status: 0, detail: 'The upload failed' }))
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
   */
  runPrint: async (outputId: string, body: PrintRunRequest): Promise<PrintRunResult> => {
    // #470: the server answers 202 with a run and slices and queues in the background,
    // since that takes longer than the proxies in front wait. A repeat of the same
    // request (the same `request_id`) is the same run, so re-sending it after an
    // answer that never arrived re-attaches to that run and never queues a second print.
    let run = await reattach(() =>
      request<PrintRun>(`/print/outputs/${seg(outputId)}/run`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    )
    while (run.status === 'running') {
      await new Promise((resolve) => setTimeout(resolve, printRunPoll.intervalMs))
      const id = run.id
      run = await reattach(() => request<PrintRun>(`/print/runs/${seg(id)}`))
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
        // The run had already tried to queue (a queue call that timed out, or a later
        // plate failing after an earlier one was queued): another Print is a new
        // print, so say where to look first.
        detail: run.may_have_queued && !detail.includes("Bambuddy's queue")
          ? `${detail} ${MAY_HAVE_QUEUED}`
          : detail,
        may_have_queued: run.may_have_queued,
      })
    }
    return run.result
  },

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
}
