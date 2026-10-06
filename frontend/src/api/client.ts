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
  RevisionSchema,
  DuplicateRequest,
  EditTarget,
  FilamentOptions,
  FontCatalogue,
  FontFamily,
  HeadlessBrowserSetting,
  HttpRequestSetting,
  AiSessionView,
  SessionLimits,
  SessionResource,
  ResourceRef,
  InstalledFamily,
  Job,
  CatalogueLibrary,
  LibraryListing,
  LibraryPinRequest,
  InstalledLibrary,
  LibraryCheck,
  LibraryCheckRequest,
  LibraryRepinRequest,
  LibraryUser,
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
  Plate,
  PlateCatalogue,
  PlateFit,
  PrinterBedType,
  PrinterRackAlgorithm,
  RackAlgorithm,
  PrintAgain,
  PrintDetail,
  PrintPage,
  PrintProgress,
  PrintCheck,
  PrintRun,
  Operation,
  OperationAccepted,
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
  StoreUsage,
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
import type {
  AiConnectionTest,
  AiCredentialCreate,
  AiCredentialEntry,
  AiCredentialList,
  AiCredentialSave,
} from './aiCredential'
import type { PrintFilters } from '../lib/printsQuery'
import type { DefinitionFile } from '../lib/lsp'
import type { JsonObject } from '../lib/inputs'
import type { Within } from '../lib/traceAction'

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
  return (await requestWithStatus<T>(path, init)).body
}

/** `request`, keeping the status: a 202 from an operation's route is not its body. */
async function requestWithStatus<T>(path: string, init?: RequestInit): Promise<{ status: number; body: T }> {
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
    return { status: 204, body: undefined as T }
  }
  return { status: response.status, body: (await response.json()) as T }
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
/** The `type` of the problem for an `/api/v1/ai` read the backend's SPA fallback answered. */
export const AI_NOT_ROUTED = 'urn:scadbuddy:ai-not-routed'
/** The `type` of the problem for a request the offline browser could not send. */
export const OFFLINE = 'urn:scadbuddy:offline'
/**
 * The `type` of the problem `command()` writes when it stops following an operation
 * still running after `printRunPoll.operationFollowMs`. The client writes it, so it is a
 * `urn:scadbuddy:` type like `UNANSWERED`, not a server's `https://scadbuddy.dev/problems/`.
 */
export const OPERATION_UNFINISHED = 'urn:scadbuddy:operation-unfinished'
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

/**
 * A 429's or 503's `Retry-After`, in seconds, as `problem.retry_after` (the agent's
 * connection test, #1000). Only the delay form; an HTTP date is left out.
 */
function withRetryAfter(problem: Problem, header: string | null): Problem {
  if (problem.status !== 429 && problem.status !== 503) return problem
  const seconds = Number(header)
  return header && seconds > 0 ? { ...problem, retry_after: seconds } : problem
}

async function readProblem(response: Response): Promise<Problem> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    return withRetryAfter(unansweredProblem(response.status, response.statusText), response.headers.get('Retry-After'))
  }
  return withRetryAfter(parsedProblem(body, response.status, response.statusText), response.headers.get('Retry-After'))
}

/** `readProblem` for an `XMLHttpRequest` that has finished. */
function xhrProblem(xhr: XMLHttpRequest): Problem {
  let body: unknown
  try {
    body = JSON.parse(xhr.responseText)
  } catch {
    return withRetryAfter(unansweredProblem(xhr.status, xhr.statusText), xhr.getResponseHeader('Retry-After'))
  }
  return withRetryAfter(parsedProblem(body, xhr.status, xhr.statusText), xhr.getResponseHeader('Retry-After'))
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
  // Its run may be checking still, and will print once it is accepted (#1052).
  if (error.problem.type === STILL_ACCEPTING) return true
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
 * How often `runPrint` reads a running print run (#470), how many times it tries a
 * request no ScadBuddy answer described again before giving up, and how long it keeps
 * sending one the server is still accepting (#1052: past the accept's own worst case,
 * three 60 s checks); tests shorten them.
 */
/**
 * `followMs` bounds how long a run is followed (review #1061). The server ends a run whose
 * execution is gone within minutes; this is the backstop, past any run's own length.
 * `operationFollowMs` is the same for an operation (review #1063): it must exceed the
 * longest run, `send`'s 3 attempts of `RUN_TIMEOUT` (300 s, backend
 * `workflows/operation.py`) with 3 s of backoff, plus one `LOST_RUN_INTERVAL` (300 s,
 * `main.py`) for the reconciler to end a lost one: 1203 s. The agent does not follow
 * that long: it follows for `COMMAND_FOLLOW_MS` (the backend's answer deadline plus a
 * margin, agent/src/tools/command.ts), then hands back the running operation for
 * `get_operation`.
 */
export const printRunPoll = {
  intervalMs: 1000,
  reattempts: 3,
  acceptingMs: 240_000,
  followMs: 3_600_000,
  operationFollowMs: 1_260_000,
}

/**
 * How long the print dialog waits for one rack-algorithm save before counting it as
 * failed. Its saves go one at a time, so an unanswered one would otherwise hold every
 * later one back (#1086 review). Aborting only stops the browser waiting, so the server
 * bounds its database work well below this (`RACK_ALGORITHM_WRITE_TIMEOUT`, #1129): once
 * a save reaches the store it commits or fails inside that bound. Time before it reaches
 * the store is not bounded, so a save held up there can still land after the next one;
 * ordering saves explicitly is #1216.
 */
export const rackAlgorithmSave = { timeoutMs: 25_000 }

/**
 * A new `request_id` for one deliberate Print (#470): the server keys the run on it, so
 * a retry of that press re-attaches to its run and the next press is a new print.
 * `getRandomValues`, not `randomUUID`, which only secure contexts have.
 */
export function newRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** ScadBuddy's 503 while Temporal has not yet answered a print's start (#1052). */
export const STILL_ACCEPTING = 'https://scadbuddy.dev/problems/command-still-accepting'
/** Temporal did not answer: nothing was started, or a start that reached it is unknown. */
export const TEMPORAL_UNAVAILABLE = 'https://scadbuddy.dev/problems/temporal-unavailable'

/**
 * The request never got ScadBuddy's own answer: the connection dropped (`send`'s
 * status 0), or a proxy in front answered 502/503/504/524 with a page of its own.
 * Or ScadBuddy answered that the same request is still being accepted, or that Temporal
 * may hold a start of it (`may_have_started`, review #1066 (10) 4): the same key follows it.
 */
function unanswered(caught: unknown): boolean {
  if (!(caught instanceof ApiError)) return false
  if (caught.problem.type === STILL_ACCEPTING) return true
  if (caught.problem.may_have_started === true) return true
  return caught.problem.type === UNANSWERED && [0, 502, 503, 504, 524].includes(caught.status)
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

/**
 * `attempt`, tried again while it goes unanswered: safe only for a request keyed to its run.
 * With `finish`, `signal` aborting between re-sends sends once more, at once, instead of
 * giving up: the request may already hold a claim, and that answer names it (review #1066
 * 1.1). Still unanswered then, it gives up. `within` wraps each attempt.
 */
async function reattach<T>(
  attempt: () => Promise<T>,
  signal?: AbortSignal,
  finish = false,
  within?: Within,
): Promise<T> {
  // Monotonic: the wall clock can step mid-wait (review #1066 (10)).
  const began = performance.now()
  let last = false
  for (let tries = 0; ; ) {
    try {
      return await (within ? within(attempt) : attempt())
    } catch (caught) {
      if (last || !unanswered(caught)) throw caught
      if (signal?.aborted) {
        if (!finish) throw caught
        last = true
        continue
      }
      const accepting = caught instanceof ApiError && caught.problem.type === STILL_ACCEPTING
      if (accepting ? performance.now() - began >= printRunPoll.acceptingMs : tries++ >= printRunPoll.reattempts) {
        throw caught
      }
      // The server's Retry-After paces a re-send it answered (review #1061 4a).
      const after = caught instanceof ApiError ? caught.problem.retry_after : undefined
      try {
        await wait(Math.max(printRunPoll.intervalMs, typeof after === 'number' ? after * 1000 : 0), signal)
      } catch (reason) {
        if (!finish) throw reason
        last = true
      }
    }
  }
}

/**
 * A Bambuddy write as an operation (#1053, spec 2026-10-01 §4.2): one `Idempotency-Key`
 * per call, which a re-send after an answer that never arrived keeps, so the server
 * answers it with the first outcome and does nothing twice. The route answers its own
 * body, or 202 with an operation still running, followed here through
 * `GET /operations/{id}` to that body, or to the problem the route would have answered.
 */
async function command<T>(path: string, init: RequestInit = {}): Promise<T> {
  const signal = init.signal ?? undefined
  const headers = { ...(init.headers as Record<string, string> | undefined), 'Idempotency-Key': newRequestId() }
  const first = await reattach(() => requestWithStatus<T | OperationAccepted>(path, { ...init, headers }), signal)
  if (first.status !== 202) return first.body as T
  let op: Operation = first.body as OperationAccepted
  const began = performance.now()
  while (op.status === 'running') {
    if (performance.now() - began >= printRunPoll.operationFollowMs) {
      throw new ApiError({
        type: OPERATION_UNFINISHED,
        title: 'Still running',
        status: 504,
        detail: `This is still running as operation ${op.id}. It may have been done anyway: check before trying again.`,
      })
    }
    await wait(printRunPoll.intervalMs, signal)
    const id = op.id
    op = await reattach(() => request<Operation>(`/operations/${seg(id)}`, { signal }), signal)
  }
  if (op.status === 'failed') {
    const error = op.error
    throw new ApiError({
      ...error?.extensions,
      type: error?.type,
      title: error?.title ?? 'Failed',
      status: error?.status ?? 500,
      detail: error?.detail ?? 'The operation ended without a result.',
    })
  }
  return (op.result ?? undefined) as T
}

/**
 * A print run to its end (#470, #742): POST `path` (an output's or a library file's
 * `/run`), then follow `GET /print/runs/{id}`. The server answers 202 with a run and
 * slices and queues in the background, since that takes longer than the proxies in
 * front wait. A repeat of the same request (the same `request_id`) is the same run, so
 * re-sending it after an answer that never arrived re-attaches to that run and never
 * queues a second print. `signal` stops following; the run itself goes on. `within`
 * (a traced action's) wraps each attempt at the POST, retries included; the polls are not.
 */
async function followPrintRun(
  path: string,
  body: PrintRunRequest,
  signal?: AbortSignal,
  within?: Within,
): Promise<PrintRunResult> {
  let run = await reattach(
    () => request<PrintRun>(path, { method: 'POST', body: JSON.stringify(body), signal }),
    signal,
    false,
    within,
  )
  const began = performance.now()
  while (run.status === 'running') {
    if (performance.now() - began >= printRunPoll.followMs) {
      throw new ApiError({
        type: 'urn:scadbuddy:print-run-unfinished',
        title: 'Still preparing',
        status: 504,
        detail: 'ScadBuddy is still preparing this print after an hour. Check Bambuddy’s queue before printing it again.',
        // It may be queued by now: the dialog says to check before printing again.
        may_have_queued: true,
      })
    }
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
    return command<ModelSummary>('/models', { method: 'POST', body })
  },

  /** Multipart with a `file` part, like the output thumbnail PUT. */
  setThumbnail: (slug: string, png: Blob) => {
    const body = new FormData()
    body.append('file', png, 'thumbnail.png')
    return command<ModelSummary>(`/models/${seg(slug)}/thumbnail`, { method: 'PUT', body })
  },

  /**
   * Removes the model's own thumbnail. The record that comes back may still have one:
   * a generated model falls back to its first output's plate image.
   */
  removeThumbnail: (slug: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/thumbnail`, { method: 'DELETE' }),

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
    command<ModelSummary>(`/models/${seg(slug)}/readme`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),

  removeReadme: (slug: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/readme`, { method: 'DELETE' }),

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
    command<ModelSummary>('/models', { method: 'POST', body: JSON.stringify(body) }),

  /** #153 — fetched on the server, then created through the same path as a paste. */
  importModel: (body: UrlImport) =>
    command<ModelSummary>('/models/import', { method: 'POST', body: JSON.stringify(body) }),

  getSource: (slug: string) => requestText(`/models/${seg(slug)}/source`),

  /**
   * Replaces the source as one revision in the model's history, named by `message`
   * when given. Parse-checked server-side unless `force`. With `base`, the version the
   * edit was made against, a model that has moved past it is a 409 whose `current`
   * names where it is now, and nothing is written (#1054).
   */
  replaceSource: (slug: string, source: string, force = false, message?: string, base?: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/source`, {
      method: 'PUT',
      body: JSON.stringify({ source, force, message: message ?? null, base: base ?? null }),
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
    command<ModelSummary>(`/models/${seg(slug)}`, { method: 'PATCH', body: JSON.stringify(patch) }),

  /** #156 — a new template of mine copied from `slug`, recording it as `upstream`. */
  duplicateModel: (slug: string, name: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/duplicate`, {
      method: 'POST',
      body: JSON.stringify({ name } satisfies DuplicateRequest),
    }),

  /** 409 while duplicates track it (see `trackingDuplicates`); `force` deletes it anyway. */
  deleteModel: (slug: string, force = false) =>
    command<unknown>(`/models/${seg(slug)}${force ? '?force=true' : ''}`, { method: 'DELETE' }).then(() => undefined),

  /** #157 — a duplicate's upstream: its state, and on `update` the merge it would make. */
  getUpstream: (slug: string) => request<UpstreamStatus>(`/models/${seg(slug)}/upstream`),

  /** A conflicted merge answers 409 with `merged` and `merge_base` and writes nothing. */
  mergeUpstream: (slug: string) =>
    command<UpstreamMerge>(`/models/${seg(slug)}/upstream/merge`, { method: 'POST' }),

  dismissUpstream: (slug: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/upstream/dismiss`, { method: 'POST' }),

  detachUpstream: (slug: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/upstream/detach`, { method: 'POST' }),

  /**
   * Saves the resolution of a conflicted upstream merge: the same write as
   * `replaceSource`, which also advances the duplicate's `base` to `mergeBase`.
   */
  resolveUpstreamMerge: (slug: string, source: string, mergeBase: string, force = false) =>
    command<ModelSummary>(`/models/${seg(slug)}/source?merge_base=${seg(mergeBase)}`, {
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
   * #624 — a small copy of an image or of a video's poster, for a strip of
   * thumbnails; undefined for a video with no poster, which has none.
   */
  mediaThumbnailUrl: (slug: string, item: Pick<MediaView, 'id' | 'kind' | 'poster'>) =>
    item.kind === 'video' && !item.poster
      ? undefined
      : `${API_BASE}/models/${seg(slug)}/media/${seg(item.id)}/thumbnail`,

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

  /**
   * #722 — makes `id` the cover: a template of mine's is moved to the front; a
   * built-in's is a choice of its own (`media_cover`), and null goes back to the one
   * it ships.
   */
  setMediaCover: (slug: string, id: string | null) =>
    request<ModelSummary>(`/models/${seg(slug)}/media/cover`, {
      method: 'PUT',
      body: JSON.stringify({ id }),
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
  getStoreUsage: () => request<StoreUsage>('/store/usage'),

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

  /**
   * #185 — the text of a file a go-to-definition lands in: beside the model, or in a
   * library it pins (`GET /models/{slug}/files/{path}`, `…/libraries/{name}/files/{path}`).
   */
  getDefinitionFile: (slug: string, file: DefinitionFile) => {
    const path = file.path.split('/').map(seg).join('/')
    const base = `/models/${seg(slug)}`
    if (!file.library) return requestText(`${base}/files/${path}`)
    const commit = file.commit ? `?commit=${seg(file.commit)}` : ''
    return requestText(`${base}/libraries/${seg(file.library)}/files/${path}${commit}`)
  },

  /** A `version` reads that revision's schema, and its `ui`, instead of the model's current one. */
  getSchema: (slug: string, version?: string): Promise<CustomizerSchema | RevisionSchema> =>
    version
      ? request<RevisionSchema>(`/models/${seg(slug)}/versions/${seg(version)}/schema`)
      : request<CustomizerSchema>(`/models/${seg(slug)}/schema`),

  listVersions: (slug: string) => request<ModelVersion[]>(`/models/${seg(slug)}/versions`),

  /** `base` omitted diffs against the revision's parent. */
  getVersionDiff: (slug: string, version: string, base?: string) =>
    request<VersionDiff>(
      `/models/${seg(slug)}/versions/${seg(version)}/diff${base ? `?base=${seg(base)}` : ''}`,
    ),

  restoreVersion: (slug: string, version: string) =>
    command<ModelVersion>(`/models/${seg(slug)}/versions/${seg(version)}/restore`, {
      method: 'POST',
    }),

  /**
   * `version` renders an old revision without restoring it ("Customize this version").
   * `supersedes` names the job this render replaces: the server drops it if no worker
   * has started it yet. Refused (503 + `Retry-After`) only when the server sets
   * SCADBUDDY_RENDER_QUEUE_MAX and that many renders already wait. `signal` (a superseded
   * preview) stops the re-sends after one more, sent at once: the request may already
   * hold a claim on a job, and that answer names the job the next render supersedes. A
   * request already sent is never aborted, for the same reason. Unanswered even then, the
   * claim is left to the render it made, which runs to its end (review #1066 1.1).
   * `requestId` is the `Idempotency-Key`: a caller that sends the render again itself
   * passes the same one, so the server counts every send as one claim (review #1066 (7) 3).
   */
  render: (
    slug: string,
    inputs: JsonObject,
    version?: string,
    supersedes?: string,
    signal?: AbortSignal,
    requestId: string = newRequestId(),
  ) => {
    // Sent again while the server is still accepting it (#1053), with one
    // `Idempotency-Key`: the server counts the re-sends as this one request's claim.
    const headers = { 'Idempotency-Key': requestId }
    return reattach(
      () =>
        request<RenderAccepted>(`/models/${seg(slug)}/render`, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            inputs,
            version: version ?? null,
            ...(supersedes ? { supersedes } : {}),
          }),
        }),
      signal,
      true,
    )
  },

  getJob: (jobId: string) => request<Job>(`/jobs/${seg(jobId)}`),

  previewUrl: (jobId: string) => `${API_BASE}/jobs/${seg(jobId)}/preview.glb`,

  /** A file under a template's `ui/` (spec 2026-09-27 §4.1): pinned by revision when there is one. */
  uiFileUrl: (slug: string, version: string | undefined, path: string) =>
    `${API_BASE}/models/${seg(slug)}${version ? `/versions/${seg(version)}` : ''}/ui/${path
      .split('/')
      .map(seg)
      .join('/')}`,

  createOutput: (slug: string, jobId: string, name?: string, inputs?: JsonObject) =>
    request<Output>(`/models/${seg(slug)}/outputs`, {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, name: name ?? null, ...(inputs ? { inputs } : {}) }),
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

  /**
   * #311 — one print. `printerMedia` also lists what the printer still holds, which
   * asks the printer, so the page does it only when told to.
   */
  getPrint: (archiveId: number, { printerMedia = false } = {}) =>
    request<PrintDetail>(`/prints/${archiveId}${printerMedia ? '?printer_media=1' : ''}`),

  /** #311 — "Print again": queues the archive on its printer (Bambuddy's reprint is gone). */
  reprint: (archiveId: number) => command<PrintAgain>(`/prints/${archiveId}/reprint`, { method: 'POST' }),

  /** #311 — attaches a timelapse still on the printer to the print. */
  pullTimelapse: (archiveId: number, filename: string) =>
    command<void>(`/prints/${archiveId}/timelapse/pull`, {
      method: 'POST',
      body: JSON.stringify({ filename }),
    }),

  outputThumbnailUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/thumbnail`,

  /** #83 — the 3MF's plates; ScadBuddy's own renders are always one. */
  getOutputPlates: (id: string) => request<OutputPlate[]>(`/outputs/${seg(id)}/plates`),

  outputPlateThumbnailUrl: (id: string, index: number) =>
    `${API_BASE}/outputs/${seg(id)}/plates/${index}/thumbnail`,

  sendOutput: (id: string, body: SendRequest) =>
    command<SendResult>(`/outputs/${seg(id)}/send`, { method: 'POST', body: JSON.stringify(body) }),

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

  /** #836 — how this printer's rack nozzle is ranked; `null` forgets it (Least used). */
  putPrinterRackAlgorithm: (printerId: number, algorithm: RackAlgorithm | null, signal?: AbortSignal) =>
    request<PrinterRackAlgorithm>(`/print/printers/${printerId}/rack-algorithm`, {
      method: 'PUT',
      body: JSON.stringify({ algorithm }),
      signal,
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
  runPrint: (outputId: string, body: PrintRunRequest, signal?: AbortSignal, within?: Within) =>
    followPrintRun(`/print/outputs/${seg(outputId)}/run`, body, signal, within),

  /**
   * #755 — the check before Print for the body the run would take: `errors` are what
   * it would refuse as a 422, `warnings` its advisories. Reads only.
   */
  checkPrint: (outputId: string, body: PrintRunRequest) =>
    request<PrintCheck>(`/print/outputs/${seg(outputId)}/check`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

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
    command<ProjectView>('/print/projects', { method: 'POST', body: JSON.stringify(body) }),

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
    command<ProjectFile>(`/outputs/${seg(outputId)}/project-file`, {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId }),
    }),

  /** Filed after the run, never during it: a plate's queue item only exists once it has
   * sliced, and an archive only once a print has finished, so the ids come from the
   * progress read (#89). */
  attachToProject: (outputId: string, body: ProjectAttach) =>
    command<AttachResult>(`/print/outputs/${seg(outputId)}/project`, {
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

  /** #308 — the print history, newest first, a page at a time; `cursor` is the last
   * page's `next_cursor`. */
  listPrints: (filters: PrintFilters, page: { cursor?: string | null; limit?: number } = {}) => {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== '') search.set(key, String(value))
    }
    if (page.limit !== undefined) search.set('limit', String(page.limit))
    if (page.cursor) search.set('cursor', page.cursor)
    const suffix = search.size > 0 ? `?${search.toString()}` : ''
    return request<PrintPage>(`/prints${suffix}`)
  },

  /** #313 — Bambuddy's folder tree and one folder's files; `all` adds sliced files and STLs. */
  listLibrary: (query: { folderId: number | null; all: boolean }) => {
    const search = new URLSearchParams()
    if (query.folderId !== null) search.set('folder_id', String(query.folderId))
    if (query.all) search.set('all', 'true')
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<LibraryListing>(`/print/library${suffix}`)
  },

  libraryThumbnailUrl: (fileId: number) => `${API_BASE}/print/library/${fileId}/thumbnail`,

  libraryPlateThumbnailUrl: (fileId: number, index: number) =>
    `${API_BASE}/print/library/${fileId}/plates/${index}/thumbnail`,

  getLibraryPlates: (fileId: number) => request<OutputPlate[]>(`/print/library/${fileId}/plates`),

  getLibraryChoices: (fileId: number, printerId?: number | null) => {
    const search = new URLSearchParams()
    if (printerId !== null && printerId !== undefined) search.set('printer_id', String(printerId))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<ChoicesView>(`/print/library/${fileId}/choices${suffix}`)
  },

  getLibraryFilaments: (
    fileId: number,
    query: { printerId?: number | null; plateId?: number; allPlates?: boolean } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.plateId !== undefined) search.set('plate_id', String(query.plateId))
    if (query.allPlates) search.set('all_plates', 'true')
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FilamentOptions>(`/print/library/${fileId}/filaments${suffix}`)
  },

  /** #742 — followed to its end like an output's run (`runPrint`). */
  runLibraryPrint: (fileId: number, body: PrintRunRequest, signal?: AbortSignal, within?: Within) =>
    followPrintRun(`/print/library/${fileId}/run`, body, signal, within),

  checkLibraryPrint: (fileId: number, body: PrintRunRequest) =>
    request<PrintCheck>(`/print/library/${fileId}/check`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  putLibraryChoices: (fileId: number, body: ModelPrintChoices) =>
    request<ModelPrintChoices>(`/print/library/${fileId}/choices`, {
      method: 'PUT',
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
   * A command (#1054): a clone past the server's deadline is followed to the model.
   */
  pinModelLibrary: (slug: string, name: string, body: LibraryPinRequest) =>
    command<ModelSummary>(`/models/${seg(slug)}/libraries/${seg(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  /** #169 — library checkouts on the volume, with the models whose live pins read each. */
  listInstalledLibraries: () => request<InstalledLibrary[]>('/libraries/installed'),

  /** #169 — every model whose live model.json pins `name`; nulls for an unreadable entry. */
  listLibraryUsers: (name: string) => request<LibraryUser[]>(`/libraries/${seg(name)}/users`),

  /**
   * #169 — a dry run of re-pinning: clones `name` at `ref` from the model's own pin URL
   * and parse-checks the model against it. Nothing is recorded. It holds the server's
   * checkout gate for the whole check, so callers run one at a time, on request.
   */
  checkModelLibrary: (slug: string, name: string, body: LibraryCheckRequest, signal?: AbortSignal) =>
    request<LibraryCheck>(`/models/${seg(slug)}/libraries/${seg(name)}/check`, {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    }),

  /** #169 — re-pins from the URL the model already pins, at `ref`; one commit per model. */
  repinModelLibrary: (slug: string, name: string, body: LibraryRepinRequest) =>
    command<ModelSummary>(`/models/${seg(slug)}/libraries/${seg(name)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  /** With `index` (#217), only the invalid entry at that position of `libraries`. */
  unpinModelLibrary: (slug: string, name: string, index?: number) =>
    command<ModelSummary>(
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

  /** #599 — forgets the printer and nozzle one Bambuddy project last printed on. */
  forgetRememberedProject: (projectId: number | string) =>
    request<RememberedChoices>(`/settings/remembered/projects/${projectId}`, { method: 'DELETE' }),

  registerSidebar: () =>
    command<SidebarLink>('/settings/register-sidebar', { method: 'POST' }),

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

  /**
   * The assistant's `http_request` tool (#827), served by the agent service. On by
   * default. Fails (404 or 503) when there is no agent or no AI database.
   */
  getHttpRequestSetting: () => request<HttpRequestSetting>('/ai/settings/http-request'),

  putHttpRequestSetting: (enabled: boolean) =>
    request<HttpRequestSetting>('/ai/settings/http-request', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  /** #790 — the budget and turn limit new assistant sessions get, served by the agent service. */
  getSessionLimits: () => request<SessionLimits>('/ai/settings/session-limits'),

  putSessionLimits: (limits: SessionLimits) =>
    request<SessionLimits>('/ai/settings/session-limits', {
      method: 'PUT',
      body: JSON.stringify(limits),
    }),

  /** #790 — "Continue in a new chat": a new session with this one's transcript and a fresh budget. */
  forkAiSession: (id: string) =>
    request<{ session: AiSessionView }>(`/ai/sessions/${encodeURIComponent(id)}/fork`, { method: 'POST' }),

  /** #790 — adds to one session's budget; only the user can (it spends money). */
  raiseAiSessionBudget: (id: string, addUsd: number) =>
    request<{ session: AiSessionView }>(`/ai/sessions/${encodeURIComponent(id)}/budget`, {
      method: 'POST',
      body: JSON.stringify({ add_usd: addUsd }),
    }),

  /** #931 — what a session's tool calls created, changed or deleted, oldest first. */
  listAiSessionResources: (id: string) =>
    request<{ resources: SessionResource[] }>(`/ai/sessions/${encodeURIComponent(id)}/resources`),

  /** #931 — the sessions whose tool calls touched a resource, newest first. */
  listAiResourceSessions: (resource: ResourceRef, limit: number) =>
    request<{ sessions: AiSessionView[] }>(
      `/ai/resources/${encodeURIComponent(resource.type)}/${encodeURIComponent(resource.id)}/sessions?limit=${limit}`,
    ),

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

  /**
   * #1000, #1093 — the agent's Claude credentials in priority order: kind, base URL, last
   * four and status only. A non-JSON answer is the backend's SPA fallback (nothing routes
   * `/api/v1/ai/*` to the agent), and rejects with a problem of type `AI_NOT_ROUTED`; a
   * JSON answer that does not parse is a real failure and rejects as it would anywhere.
   */
  listAiCredentials: async (): Promise<AiCredentialList> => {
    const response = await send(`${API_BASE}/ai/credentials/entries`, { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new ApiError(await readProblem(response))
    if (!(response.headers.get('content-type') ?? '').includes('application/json')) {
      throw new ApiError({
        type: AI_NOT_ROUTED,
        title: 'Not routed',
        status: response.status,
        detail: 'The agent service did not answer at /api/v1/ai.',
      })
    }
    return (await response.json()) as AiCredentialList
  },

  /** Added last, so it is tried after every existing one. */
  createAiCredential: (body: AiCredentialCreate) =>
    request<AiCredentialEntry>('/ai/credentials/entries', { method: 'POST', body: JSON.stringify(body) }),

  /** Every id exactly once, first tried first; a stale list answers 409. */
  reorderAiCredentials: (ids: string[]) =>
    request<AiCredentialList>('/ai/credentials/order', { method: 'PUT', body: JSON.stringify({ ids }) }),

  saveAiCredential: (id: string, body: AiCredentialSave) =>
    request<AiCredentialEntry>(`/ai/credentials/entries/${seg(id)}`, { method: 'PUT', body: JSON.stringify(body) }),

  deleteAiCredential: (id: string) =>
    request<AiCredentialList>(`/ai/credentials/entries/${seg(id)}`, { method: 'DELETE' }),

  /** Back to `active`, for one disabled or cooling down. */
  resetAiCredential: (id: string) =>
    request<AiCredentialEntry>(`/ai/credentials/entries/${seg(id)}/reset`, { method: 'POST' }),

  /** Spends real tokens; a 429 carries `problem.retry_after` (seconds). */
  testAiCredential: (id: string) =>
    request<AiConnectionTest>(`/ai/credentials/entries/${seg(id)}/test`, { method: 'POST' }),
}
