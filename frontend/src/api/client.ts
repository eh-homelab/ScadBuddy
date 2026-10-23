import type {
  InventoryView,
  PrintChoices,
  FilamentProfile,
  ProfilePreview,
  AnalysisReport,
  AnalysisRun,
  AnalyzerDecision,
  ArrangeRequest,
  MeshModelCreate,
  BackgroundTask,
  DecisionCreate,
  FixApply,
  FixPreview,
  FixRequest,
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
  GlyphCoverage,
  HttpRequestSetting,
  BrowserOriginsSetting,
  ModelSetting,
  PrinterCameraSetting,
  ImageSettings,
  MemorySettings,
  WorkspaceFile,
  WorkspaceFileList,
  WorkspaceSettings,
  AttachmentView,
  AiSessionEdit,
  AiSessionView,
  RenderLimits,
  SessionLimits,
  SessionRuntime,
  BlankTemplate,
  SessionModeSetting,
  DurableSubagentsSetting,
  SessionResource,
  ResourceRef,
  InstalledFamily,
  Job,
  CatalogueLibrary,
  DependencyReport,
  LibraryFileObjects,
  LibraryCopyResult,
  LibraryDeleteResult,
  LibraryFileName,
  LibraryFolder,
  LibraryFolderCreate,
  LibraryFolderDeleteResult,
  LibraryFolderUpdate,
  LibraryListing,
  LibrarySearch,
  LibraryMoveResult,
  LibraryRenameResult,
  LibraryRestoreResult,
  LibraryPinRequest,
  InstalledLibrary,
  LibraryCheck,
  LibraryCheckRequest,
  LibraryRepinRequest,
  LibraryUser,
  MediaView,
  MigrateResult,
  ModelPatch,
  DefaultDestination,
  ModelProjectPut,
  ModelProjects,
  ModelPrintChoices,
  NewBatchFile,
  BatchChoice,
  PrintBatch,
  BatchQueueProgress,
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
  PrinterRackUsage,
  RackAlgorithm,
  PrintAgain,
  PrintAttachment,
  PrintDetail,
  TimelapseFrames,
  PrintPage,
  ActivityDetail,
  ActivityFacets,
  ActivityPage,
  PrintProgress,
  PrintersNow,
  PrinterRow,
  PrinterCreate,
  PrinterPatch,
  DiscoveredPrinter,
  LanLink,
  QueueItem,
  ArchiveStats,
  PrintCheck,
  PreviewStarted,
  SlicePreview,
  TrayAssigned,
  PrintRun,
  FlowDecided,
  FlowProposal,
  FlowRunQueue,
  FlowDefinition,
  FlowGrantSpec,
  FlowGrantView,
  FlowHostInterface,
  FlowRegister,
  FlowRunOptions,
  FlowScriptCheck,
  FlowReset,
  FlowResetPoints,
  FlowResetPreview,
  FlowRun,
  FlowRunStarted,
  FlowRunView,
  FlowState,
  FlowStandingGrant,
  FlowSummary,
  FlowTrigger,
  FlowTriggerCreate,
  FlowTriggerUpdate,
  FlowVersion,
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
  CommandAccepting,
  SendRequest,
  SendResult,
  Settings,
  SettingsUpdate,
  SidebarLink,
  SourceCheck,
  SourceFile,
  StoreUsage,
  UpstreamMerge,
  UpstreamStatus,
  UrlImport,
  VersionDiff,
  NeedsBackfillProblem,
  Customer,
  CustomerCreate,
  CustomerPatch,
  Filament,
  FilamentInput,
  FilamentPatch,
  Fulfilment,
  FulfilmentInput,
  ManualQuoteInput,
  Order,
  OrderCreate,
  OrderDetail,
  OrderEstimate,
  OrderEvent,
  OrderItem,
  OrderItemCreate,
  OrderItemPatch,
  OrderPage,
  OrderPatch,
  Payment,
  PaymentCreate,
  PolicyUpdate,
  PriceBook,
  Quote,
  SliceQuoteRequest,
  Shipment,
  ShipmentEdit,
  ShipmentInput,
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
import { apiParams, splitList, type ActivityQuery } from '../lib/activityQuery'
import { ordersApiParams, type OrdersQuery } from '../lib/ordersQuery'
import type { PrintFilters } from '../lib/printsQuery'
import type { DefinitionFile } from '../lib/lsp'
import type { JsonObject } from '../lib/inputs'
import type { Within } from '../lib/traceAction'
import { adoptScope, inReadScope, ReadScope, trackRequest } from '../lib/readCursor'

export const API_BASE = '/api/v1'

/**
 * #1424 — the backend's `THUMBNAIL_VERSION` (`api/media.py`): a media thumbnail is
 * cached as `immutable` only when asked for at this version. Bump the two together;
 * `client.test.ts` fails when they differ (#1691).
 */
export const MEDIA_THUMBNAIL_VERSION = 1

/** What a model thumbnail's URL is keyed on (#179). */
export type ThumbnailKeyed = Pick<
  ModelSummary,
  'slug' | 'version' | 'thumbnail_source' | 'thumbnail_output_id' | 'thumbnail_preview_id'
>

/** #1289 — `GET /jobs/{id}/colours.png`: tile `i` shows where `colours[i]` goes. */
export interface ColourBreakdown {
  image: Blob
  colours: string[]
  columns: number
}

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


/**
 * How long a `GET /settings` in flight is shared. A read that has not answered by then
 * may never (fetch has no timeout): later callers start their own instead of joining
 * it, so one hung request cannot wedge every reader of the settings (#1856).
 */
export const SETTINGS_SHARE_MS = 5_000
/** The `GET /settings` in flight, shared by every caller until it answers (#1039). */
let settingsInFlight: { read: Promise<Settings>; started: number; scope: ReadScope } | undefined
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
 * Several library files asked by id and Bambuddy has none of them (`bambuddy/errors.py`
 * `LIBRARY_FILES_MISSING_PROBLEM`): the names route's 502, told apart from a Bambuddy
 * that failed (`BAMBUDDY_UNAVAILABLE`).
 */
export const BAMBUDDY_LIBRARY_FILES_MISSING = 'https://scadbuddy.dev/problems/bambuddy-library-files-missing'
/** No Bambuddy URL is set (`bambuddy/errors.py` `NOT_CONFIGURED_PROBLEM`). */
export const BAMBUDDY_NOT_CONFIGURED = 'https://scadbuddy.dev/problems/bambuddy-not-configured'
/**
 * #902 — Arrange's refusal of outputs saved before Arrange existed: `code` is
 * `needs_backfill` and `output_ids` names every one, to re-render before arranging.
 */
export const NEEDS_BACKFILL = 'needs_backfill' satisfies NeedsBackfillProblem['code']

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
  // What the answer saw of the event bus, for the read this request is part of (#1039).
  const answered = trackRequest()
  try {
    const response = await fetch(url, init)
    answered(response.headers)
    return response
  } catch (cause) {
    answered(undefined)
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
 * Or the backend said its start may have reached Temporal (`may_have_started`, on a
 * `temporal-unavailable` or `temporal-refused`), or an operation was still running when
 * `command()` stopped following it. Any other problem the backend wrote, a 503 (nothing
 * upstream took it) and an offline browser all mean it did not. For a request with a
 * physical effect (a print), retrying one of these blind can do it twice. A failed print
 * run (#470) says so itself: its `may_have_queued` is whether it had tried to queue,
 * which `runPrint` carries over.
 */
export function mayHaveRun(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  // Its run may be checking still, and will print once it is accepted (#1052).
  if (error.problem.type === STILL_ACCEPTING) return true
  // Temporal may hold a start of it: only the same key follows it (review #1316 (13) 1a).
  if (error.problem.may_have_started === true) return true
  if (error.problem.type === OPERATION_UNFINISHED) return true
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

/** Who sliced a background slice: the `slicer` its start answered with. */
export type Slicer = PreviewStarted['slicer']

/** A Bambuddy read's URL is as it always was; only a native one names its slicer. */
function slicerQuery(slicer: Slicer): string {
  return slicer === 'native' ? '?slicer=native' : ''
}

/** #2329 — a workspace file's route, each path segment encoded (under `API_BASE`). */
const sessionFilePath = (id: string, path: string) =>
  `/ai/sessions/${seg(id)}/files/${path.split('/').map(seg).join('/')}`

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
 * bounds its database work well below this (`RACK_ALGORITHM_WRITE_TIMEOUT`, 5 s, #1129):
 * once a save reaches the store it commits or fails within twice that bound, one for
 * the pool wait and one for the write (#1264). Time before it reaches
 * the store is not bounded, so a save held up there can still land after the next one:
 * each save's version orders them (#1216), and the server refuses the older one.
 */
export const rackAlgorithmSave = { timeoutMs: 25_000 }

let lastRackAlgorithmVersion = 0
/**
 * #1216 — the version a rack-algorithm save is sent with: the clock, but never less than
 * one above the last, so it grows across saves, dialogs and page loads in this browser.
 * A count that started again at 1 on each load would be refused for good after a reload.
 */
export function nextRackAlgorithmVersion(): number {
  lastRackAlgorithmVersion = Math.max(Date.now(), lastRackAlgorithmVersion + 1)
  return lastRackAlgorithmVersion
}

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
 * A still-accepting answer is re-sent for up to `acceptingMs`, unless `bounded`: then it
 * counts toward `reattempts` like any other, for a body too large to send that often.
 * With `finish`, `signal` aborting between re-sends sends once more, at once, instead of
 * giving up: the request may already hold a claim, and that answer names it (review #1066
 * 1.1). Still unanswered then, it gives up. `within` wraps each attempt.
 */
async function reattach<T>(
  attempt: () => Promise<T>,
  signal?: AbortSignal,
  { bounded = false, finish = false, within }: { bounded?: boolean; finish?: boolean; within?: Within } = {},
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
      if (
        accepting && !bounded
          ? performance.now() - began >= printRunPoll.acceptingMs
          : tries++ >= printRunPoll.reattempts
      ) {
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

/** #2340 — the Update a 202 `accepting` named is not known: send the request again, same key. */
export const COMMAND_RESEND = 'https://scadbuddy.dev/problems/command-resend'

function isAccepting(body: unknown): body is CommandAccepting {
  return typeof body === 'object' && body !== null && (body as { status?: unknown }).status === 'accepting'
}

/**
 * #2340 — a command sent with `Prefer: respond-async` (spec 2026-10-13): a 202
 * `accepting` is followed through its `follow_url` to the answer the route would have
 * given, instead of sending the request again every few seconds. A follow answered 409
 * `command-resend` (the Update was never kept, or a closing run rejected it) sends the
 * request again with its key. Followed for up to `acceptingMs`, as the re-sends were.
 * `signal` aborting sends one last follow at once, which names a claim already made.
 */
async function untilAccepted<T>(
  send: () => Promise<T | CommandAccepting>,
  signal?: AbortSignal,
  within?: Within,
): Promise<T> {
  const answer = await untilAnswered(
    send,
    (sent) => (isAccepting(sent) ? sent : undefined),
    // A POST (#2782 review): a render's follow makes its supersede, a change the
    // cross-site gate must see.
    (url) => request<T | CommandAccepting>(url, { method: 'POST' }),
    signal,
    within,
  )
  return answer as T
}

/**
 * `untilAccepted` over any answer shape: `pending` names the 202 `accepting` in an
 * answer, if it is one, and `follow` reads its `follow_url` (#2794: an operation's answer
 * keeps its status, since its own 202 is a different body).
 */
async function untilAnswered<A>(
  send: () => Promise<A>,
  pending: (answer: A) => CommandAccepting | undefined,
  follow: (url: string) => Promise<A>,
  signal?: AbortSignal,
  within?: Within,
): Promise<A> {
  const began = performance.now()
  let answer = await send()
  let last = false
  let followed = false
  for (let accepting = pending(answer); accepting; accepting = pending(answer)) {
    // The last follow after an abort named no claim yet: the abort it is, so a caller
    // that keeps a superseded action quiet still can (review #2805).
    if (last) throw signal?.reason
    if (performance.now() - began >= printRunPoll.acceptingMs) {
      throw new ApiError({
        type: STILL_ACCEPTING,
        title: 'Still starting',
        status: 503,
        detail: 'ScadBuddy is still starting this. It may start anyway.',
      })
    }
    // The server's `retry_after` is advisory here and not waited: the follow is itself
    // a long-poll that waits for the answer, so waiting first only delays it (review
    // #2849). The first follow goes at once; after
    // a follow that came back 202, `intervalMs` keeps a floor (review #2805).
    if (followed) {
      try {
        await wait(printRunPoll.intervalMs, signal)
      } catch {
        last = true
      }
    } else if (signal?.aborted) {
      last = true
    }
    followed = true
    const url = accepting.follow_url.startsWith(API_BASE)
      ? accepting.follow_url.slice(API_BASE.length)
      : accepting.follow_url
    try {
      // `finish`, as the POST's own re-send has: a follow in flight when the signal aborts
      // may have made the supersede's release, so an unanswered one is sent once more to
      // learn what it claimed, never dropped (#3576).
      answer = await reattach(() => follow(url), last ? undefined : signal, { finish: true, within })
    } catch (caught) {
      if (!(caught instanceof ApiError && caught.problem.type === COMMAND_RESEND)) throw caught
      answer = await send()
    }
  }
  return answer
}

/**
 * A Bambuddy write as an operation (#1053, spec 2026-10-01 §4.2): one `Idempotency-Key`
 * per call, which a re-send after an answer that never arrived keeps, so the server
 * answers it with the first outcome and does nothing twice. The route answers its own
 * body, or 202 with an operation still running, followed here through
 * `GET /operations/{id}` to that body, or to the problem the route would have answered.
 */
export async function command<T>(
  path: string,
  init: RequestInit = {},
  /** Where the operation is followed: the backend's, or the agent's own (`/ai/operations`, #1055). */
  operations = '/operations',
  /**
   * The press's key. A caller offering "Try again" after a failure passes the failed
   * attempt's, so the retry follows a start that may have happened rather than doing it
   * a second time; a new press gets a new one.
   */
  key: string = newRequestId(),
): Promise<T> {
  const signal = init.signal ?? undefined
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string> | undefined),
    'Idempotency-Key': key,
    // #2794: the backend's operations answer a slow start 202 `accepting`, followed here;
    // the agent's own (`/ai/operations`) do not.
    ...(operations === '/operations' ? { Prefer: 'respond-async' } : {}),
  }
  type Answer = { status: number; body: T | OperationAccepted | CommandAccepting }
  const first = await untilAnswered<Answer>(
    () => reattach(() => requestWithStatus<T | OperationAccepted | CommandAccepting>(path, { ...init, headers }), signal),
    (answer) => (answer.status === 202 && isAccepting(answer.body) ? answer.body : undefined),
    (url) => requestWithStatus<T | OperationAccepted | CommandAccepting>(url, { method: 'POST' }),
    signal,
  )
  return followOperation<T>(first as { status: number; body: T | OperationAccepted }, signal, operations)
}

/**
 * A multipart upload as a command (#1054). XHR rather than `fetch`, which reports no
 * upload progress; a video runs to a gigabyte. `onProgress` gets the fraction sent, 0
 * to 1. `form` builds the body afresh for each re-send.
 */
function uploadForm<T>(path: string, form: () => FormData, onProgress?: (fraction: number) => void): Promise<T> {
  // A command (#1054): one key for this upload, kept on a re-send after an answer
  // that never arrived, so the server adds the item once.
  const key = newRequestId()
  const send = () =>
    new Promise<{ status: number; body: T | OperationAccepted }>((resolve, reject) => {
      const body = form()
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `${API_BASE}${path}`)
      xhr.setRequestHeader('Accept', 'application/json')
      xhr.setRequestHeader('Idempotency-Key', key)
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable && event.total > 0) onProgress?.(event.loaded / event.total)
      })
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve({ status: xhr.status, body: JSON.parse(xhr.responseText) as T | OperationAccepted })
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
    })
  // Bounded: each re-send streams the whole file again, up to a gigabyte (review 3e final I2).
  return reattach(send, undefined, { bounded: true }).then((first) => followOperation<T>(first))
}

/**
 * A command's first answer to its end: the route's own body, or a 202's operation
 * followed through `GET /operations/{id}` to its result, or to the problem the route
 * would have answered.
 */
async function followOperation<T>(
  first: { status: number; body: T | OperationAccepted },
  signal?: AbortSignal,
  operations = '/operations',
): Promise<T> {
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
    op = await reattach(() => request<Operation>(`${operations}/${seg(id)}`, { signal }), signal)
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
  // A run no worker has picked up yet is a 202 to follow, not a 503 to re-POST (#2793).
  const send = () =>
    reattach(
      () =>
        request<PrintRun | CommandAccepting>(path, {
          method: 'POST',
          headers: { Prefer: 'respond-async' },
          body: JSON.stringify(body),
          signal,
        }),
      signal,
      { within },
    )
  let run = await untilAccepted<PrintRun>(send, signal, within)
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
  // Flows (#1057): runs, and a person's answers and approvals for them. An answer or
  // a decision is an operation, keyed per call (`command`); a stale one is a 409
  // `stale-entry`.
  listFlowRuns: (query: { session?: string; definition_id?: string } = {}) => {
    const params = new URLSearchParams()
    if (query.session) params.set('session', query.session)
    if (query.definition_id) params.set('definition_id', query.definition_id)
    const search = params.toString()
    return request<FlowRun[]>(`/workflow-runs${search ? `?${search}` : ''}`)
  },

  getFlowRun: (id: string) => request<FlowRunView>(`/workflow-runs/${seg(id)}`),
  /** #2477: one of a run's images, from its blob route. */
  flowRunImageUrl: (id: string, name: string) => `${API_BASE}/workflow-runs/${seg(id)}/images/${seg(name)}`,

  answerFlowRun: (id: string, callId: string, answer: string) =>
    command<FlowDecided>(`/workflow-runs/${seg(id)}/answer`, {
      method: 'POST',
      body: JSON.stringify({ call_id: callId, answer }),
    }),

  decideFlowRun: (id: string, callId: string, approved: boolean, reason?: string, proposalHash?: string) =>
    command<FlowDecided>(`/workflow-runs/${seg(id)}/decide`, {
      method: 'POST',
      body: JSON.stringify({
        call_id: callId,
        approved,
        ...(reason ? { reason } : {}),
        // #2482: approve a parent's proposal, exactly the one shown (its hash).
        ...(proposalHash ? { proposal: true, proposal_hash: proposalHash } : {}),
      }),
    }),

  /** #2482 — a run's one queue, from the agent: its own calls, its child sessions' and its grant request. */
  getFlowRunQueue: (id: string) => request<FlowRunQueue>(`/ai/flow-runs/${seg(id)}/queue`),

  /** #2482 — a parent's proposal beside the call it narrows (the person's only). */
  getFlowProposal: (id: string, callId: string) =>
    request<FlowProposal>(`/workflow-runs/${seg(id)}/proposals/${seg(callId)}`),

  // #2350 — the Flows tab: every registered flow version, one with its script, and a
  // run started from it. #2487: its editor's Save, check, completion, diff and restore.
  // #2485: archived and deleted flows too with `archived`.
  listFlows: ({ archived = false }: { archived?: boolean } = {}) =>
    request<FlowSummary[]>(`/workflows${archived ? '?archived=true' : ''}`),

  /** #2485 — archive a flow (every version): hidden, and never started until unarchived. */
  archiveFlow: (name: string) => request<FlowState>(`/workflows/names/${seg(name)}/archive`, { method: 'POST' }),
  unarchiveFlow: (name: string) => request<FlowState>(`/workflows/names/${seg(name)}/unarchive`, { method: 'POST' }),
  /** #2485 — a commit removing the flow's script; its history keeps every version, and a restore brings it back. */
  deleteFlow: (name: string) => request<FlowState>(`/workflows/names/${seg(name)}`, { method: 'DELETE' }),

  /**
   * One flow's versions, newest first: the one place the page reads them from, its git
   * history (#2503). `GET /workflows` lists only each name's newest version.
   */
  listFlowVersions: (name: string) => request<FlowVersion[]>(`/workflows/names/${seg(name)}/versions`),

  getFlow: (id: string) => request<FlowDefinition>(`/workflows/${seg(id)}`),

  /** #2487 — the editor's Save: a new version (a commit), never an edit. A 422 lists `problems`. */
  registerFlow: (body: FlowRegister) =>
    request<FlowDefinition>('/workflows', { method: 'POST', body: JSON.stringify(body) }),

  /** The check a registration runs, storing nothing; with the inputs schema it would be saved with (#2483). */
  checkFlowScript: (script: string, signal?: AbortSignal, inputsSchema?: Record<string, unknown> | null) =>
    request<FlowScriptCheck>('/workflow-script/check', {
      method: 'POST',
      body: JSON.stringify(inputsSchema ? { script, inputs_schema: inputsSchema } : { script }),
      signal,
    }),

  /** What the editor completes: the host functions the check uses, and the manifest's tools.
   * With a flow's inputs schema, `inputs()` and its `FlowInputs` too (#2651). */
  getFlowHostFunctions: (inputsSchema?: Record<string, unknown> | null) =>
    inputsSchema
      ? request<FlowHostInterface>('/workflow-script/host-functions', {
          method: 'POST',
          body: JSON.stringify({ inputs_schema: inputsSchema }),
        })
      : request<FlowHostInterface>('/workflow-script/host-functions'),

  getFlowVersionSource: (name: string, commit: string) =>
    requestText(`/workflows/names/${seg(name)}/versions/${seg(commit)}/source`),

  /** A new version with the script at `commit`: a new commit, type-checked first. */
  restoreFlowVersion: (name: string, commit: string) =>
    request<FlowDefinition>(`/workflows/names/${seg(name)}/versions/${seg(commit)}/restore`, { method: 'POST' }),

  /**
   * A start is keyed per press: a re-send after an answer that never arrived reaches the same run.
   * `options.grant` (#2481) is approved as sent; `options.inputs` (#2483) are the run's typed
   * inputs, and a 422 `flow-inputs` lists the ones that do not fit.
   */
  startFlowRun: (id: string, key: string = newRequestId(), options: FlowRunOptions = {}) =>
    reattach(() =>
      request<FlowRunStarted>(`/workflows/${seg(id)}/runs`, {
        method: 'POST',
        headers: { 'Idempotency-Key': key },
        body: JSON.stringify(options),
      }),
    ),

  // #2481 — a run's grant: a person approves a requested one (as asked, or edited),
  // declines it, or revokes one. Each is final; a second answers 409 `flow-grant-state`.
  approveFlowGrant: (id: string, spec?: FlowGrantSpec) =>
    request<FlowGrantView>(`/workflow-runs/${seg(id)}/grant/approve`, {
      method: 'POST',
      body: JSON.stringify(spec ? { spec } : {}),
    }),
  declineFlowGrant: (id: string) =>
    request<FlowGrantView>(`/workflow-runs/${seg(id)}/grant/decline`, { method: 'POST' }),
  revokeFlowGrant: (id: string) =>
    request<FlowGrantView>(`/workflow-runs/${seg(id)}/grant/revoke`, { method: 'POST' }),

  // #2484 — flow triggers: a schedule or an event that starts runs of a pinned version,
  // as whoever made it. A standing grant is approved as sent from here; only a person
  // approves or declines an agent's.
  /** The newest 200, the route's most. */
  listFlowTriggers: (name?: string) =>
    request<FlowTrigger[]>(`/flow-triggers?limit=200${name ? `&name=${encodeURIComponent(name)}` : ''}`),
  getFlowTrigger: (id: string) => request<FlowTrigger>(`/flow-triggers/${seg(id)}`),
  createFlowTrigger: (body: FlowTriggerCreate) =>
    request<FlowTrigger>('/flow-triggers', { method: 'POST', body: JSON.stringify(body) }),
  updateFlowTrigger: (id: string, body: FlowTriggerUpdate) =>
    request<FlowTrigger>(`/flow-triggers/${seg(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  pauseFlowTrigger: (id: string) => request<FlowTrigger>(`/flow-triggers/${seg(id)}/pause`, { method: 'POST' }),
  resumeFlowTrigger: (id: string) => request<FlowTrigger>(`/flow-triggers/${seg(id)}/resume`, { method: 'POST' }),
  deleteFlowTrigger: (id: string) => request<void>(`/flow-triggers/${seg(id)}`, { method: 'DELETE' }),
  /** `seen` is the trigger's as it was shown: the server refuses (409) when it changed since. */
  attachFlowTriggerGrant: (id: string, spec: FlowGrantSpec, seen: string) =>
    request<FlowStandingGrant>(`/flow-triggers/${seg(id)}/grant`, {
      method: 'POST',
      body: JSON.stringify({ spec, seen }),
    }),
  approveFlowTriggerGrant: (id: string, seen: string, spec?: FlowGrantSpec) =>
    request<FlowStandingGrant>(`/flow-triggers/${seg(id)}/grant/approve`, {
      method: 'POST',
      body: JSON.stringify(spec ? { spec, seen } : { seen }),
    }),
  declineFlowTriggerGrant: (id: string) =>
    request<FlowStandingGrant>(`/flow-triggers/${seg(id)}/grant/decline`, { method: 'POST' }),
  revokeFlowTriggerGrant: (id: string) =>
    request<FlowStandingGrant>(`/flow-triggers/${seg(id)}/grant/revoke`, { method: 'POST' }),

  /** Forgets a run: its payload key, its workflow (stopped if open) and its row. */
  deleteFlowRun: (id: string) => request<void>(`/workflow-runs/${seg(id)}`, { method: 'DELETE' }),

  getFlowResetPoints: (id: string) => request<FlowResetPoints>(`/workflow-runs/${seg(id)}/reset-points`),

  previewFlowReset: (id: string, eventId: number) =>
    request<FlowResetPreview>(`/workflow-runs/${seg(id)}/reset-preview?event_id=${eventId}`),

  /** Pinned to the preview the person saw: a 409 `flow-reset-changed` carries the new one. */
  resetFlowRun: (id: string, preview: Pick<FlowResetPreview, 'event_id' | 'as_of_event_id' | 'workflow_run_id'>) =>
    command<FlowReset>(`/workflow-runs/${seg(id)}/reset`, {
      method: 'POST',
      body: JSON.stringify({
        event_id: preview.event_id,
        as_of_event_id: preview.as_of_event_id,
        workflow_run_id: preview.workflow_run_id,
      }),
    }),

  listModels: () => request<ModelSummary[]>('/models'),

  getModel: (slug: string) => request<ModelSummary>(`/models/${seg(slug)}`),

  /** #2146 — a library file (or an output) opened as an editable mesh model, named `name`. */
  createMeshModel: (body: MeshModelCreate) =>
    command<ModelSummary>('/models/from-mesh', { method: 'POST', body: JSON.stringify(body) }),

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
    command<ParamPreset>(`/models/${seg(slug)}/presets`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updatePreset: (slug: string, id: string, body: ParamPresetUpdate) =>
    command<ParamPreset>(`/models/${seg(slug)}/presets/${seg(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  /** Copies any preset, shipped or saved, to a new saved one with the same values. */
  duplicatePreset: (slug: string, id: string, body: ParamPresetDuplicate) =>
    command<ParamPreset>(`/models/${seg(slug)}/presets/${seg(id)}/duplicate`, {
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

  /** #1290 — the model's `.scad` files, `model.scad` first (`GET /models/{slug}/files`). */
  listSourceFiles: (slug: string) => request<SourceFile[]>(`/models/${seg(slug)}/files`),

  /**
   * #1290 — writes a `.scad` file beside `model.scad` as one revision. With `base`, a
   * 409 naming the `current` revision when the model has moved on since, writing
   * nothing. `model.scad` itself is written with `replaceSource`.
   */
  writeSourceFile: (slug: string, name: string, content: string, base?: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/files/${seg(name)}`, {
      method: 'PUT',
      body: JSON.stringify({ content, message: null, base: base ?? null }),
    }),

  /** #1290 — removes a `.scad` file beside `model.scad` as one revision. */
  deleteSourceFile: (slug: string, name: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/files/${seg(name)}`, { method: 'DELETE' }),


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
   * thumbnails; undefined for a video with no poster, which has none. It is
   * cached as `immutable`, so the URL carries the thumbnail version (#1424).
   * `card` asks for a larger copy, sized for a catalogue card's cover (#1034).
   */
  mediaThumbnailUrl: (slug: string, item: Pick<MediaView, 'id' | 'kind' | 'poster'>, size?: 'card') =>
    item.kind === 'video' && !item.poster
      ? undefined
      : `${API_BASE}/models/${seg(slug)}/media/${seg(item.id)}/thumbnail?v=${MEDIA_THUMBNAIL_VERSION}${size ? `&size=${size}` : ''}`,

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
  ): Promise<ModelSummary> =>
    uploadForm<ModelSummary>(
      `/models/${seg(slug)}/media`,
      () => {
        const body = new FormData()
        body.append('file', file)
        if (options.caption) body.append('caption', options.caption)
        if (options.poster) body.append('poster', options.poster)
        return body
      },
      onProgress,
    ),

  patchMedia: (slug: string, id: string, caption: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/media/${seg(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ caption }),
    }),

  /** `ids` names every item once, in the new order; the first is the cover. */
  reorderMedia: (slug: string, ids: string[]) =>
    command<ModelSummary>(`/models/${seg(slug)}/media/order`, {
      method: 'PUT',
      body: JSON.stringify({ ids }),
    }),

  /**
   * #722 — makes `id` the cover: a template of mine's is moved to the front; a
   * built-in's is a choice of its own (`media_cover`), and null goes back to the one
   * it ships.
   */
  setMediaCover: (slug: string, id: string | null) =>
    command<ModelSummary>(`/models/${seg(slug)}/media/cover`, {
      method: 'PUT',
      body: JSON.stringify({ id }),
    }),

  deleteMedia: (slug: string, id: string) =>
    command<ModelSummary>(`/models/${seg(slug)}/media/${seg(id)}`, { method: 'DELETE' }),

  /**
   * #204 — stores an SVG or PNG for a `// file` parameter. The answer's `id` (the
   * SHA-256 of what the server kept) is the value the render takes.
   */
  uploadAsset: (slug: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return command<Asset>(`/models/${seg(slug)}/assets`, { method: 'POST', body })
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
    // One `Idempotency-Key` (#1053): the server counts a re-send as this one request's
    // claim. A start still accepting is answered 202 and its Update followed (#2340).
    const headers = { 'Idempotency-Key': requestId, Prefer: 'respond-async' }
    const send = () =>
      reattach(
        () =>
          request<RenderAccepted | CommandAccepting>(`/models/${seg(slug)}/render`, {
            method: 'POST',
            headers,
            body: JSON.stringify({
              inputs,
              version: version ?? null,
              ...(supersedes ? { supersedes } : {}),
            }),
          }),
        signal,
        { finish: true },
      )
    return untilAccepted<RenderAccepted>(send, signal)
  },

  getJob: (jobId: string) => request<Job>(`/jobs/${seg(jobId)}`),

  previewUrl: (jobId: string) => `${API_BASE}/jobs/${seg(jobId)}/preview.glb`,

  /**
   * #1289 — the job drawn once per colour (`GET /jobs/{id}/colours.png`): the grid, and
   * its tiles' colours row by row with the grid's width, from the route's own headers.
   */
  getJobColours: async (jobId: string, view: string, signal?: AbortSignal): Promise<ColourBreakdown> => {
    const response = await send(`${API_BASE}/jobs/${seg(jobId)}/colours.png?view=${encodeURIComponent(view)}`, {
      headers: { Accept: 'image/png' },
      ...(signal ? { signal } : {}),
    })
    if (!response.ok) throw new ApiError(await readProblem(response))
    const colours = (response.headers.get('X-ScadBuddy-Colours') ?? '').split(',').filter(Boolean)
    const columns = Number(response.headers.get('X-ScadBuddy-Colour-Columns'))
    if (colours.length === 0 || !Number.isInteger(columns) || columns < 1) {
      throw new ApiError({ title: 'The colour breakdown came without its legend', status: response.status })
    }
    return { image: await response.blob(), colours, columns }
  },

  /** A file under a template's `ui/` (spec 2026-09-27 §4.1): pinned by revision when there is one. */
  uiFileUrl: (slug: string, version: string | undefined, path: string) =>
    `${API_BASE}/models/${seg(slug)}${version ? `/versions/${seg(version)}` : ''}/ui/${path
      .split('/')
      .map(seg)
      .join('/')}`,

  /**
   * spec 2026-09-27 §7 — objects from saved outputs onto plates again; poll the job.
   * #2280 — sent again with one `Idempotency-Key` while the server is still accepting
   * its start, as a render is, so a slow start is followed rather than failed. An
   * aborted `signal` (a closed dialog) stops the re-sends.
   */
  arrangeOutputs: (body: ArrangeRequest, signal?: AbortSignal, requestId: string = newRequestId()) => {
    const headers = { 'Idempotency-Key': requestId }
    return reattach(
      () => request<Job>('/outputs/arrange', { method: 'POST', headers, body: JSON.stringify(body) }),
      signal,
    )
  },

  /**
   * #902 — re-render an output saved before Arrange; poll the job, then the output's
   * `backfill`. #2280 — its render is started like any other, so it is sent again with
   * the same key while the server is still accepting it, until `signal` aborts.
   */
  backfillOutput: (outputId: string, signal?: AbortSignal, requestId: string = newRequestId()) => {
    const headers = { 'Idempotency-Key': requestId }
    return reattach(() => request<Job>(`/outputs/${seg(outputId)}/backfill`, { method: 'POST', headers }), signal)
  },

  /**
   * `index` picks one of a pipeline job's outputs (spec 2026-09-27 §5.2); the first by
   * default. `key` is a retried press's (`command`), so the retry saves one output.
   */
  createOutput: (slug: string, jobId: string, name?: string, inputs?: JsonObject, index?: number, key?: string) =>
    command<Output>(
      `/models/${seg(slug)}/outputs`,
      {
        method: 'POST',
        body: JSON.stringify({
          job_id: jobId,
          name: name ?? null,
          ...(inputs ? { inputs } : {}),
          ...(index !== undefined ? { index } : {}),
        }),
      },
      undefined,
      key,
    ),

  /** Saved inputs brought up to the template's `INPUTS_VERSION` (spec 2026-09-27 §8.2). */
  migrateInputs: (slug: string, inputs: JsonObject, version?: string) =>
    request<MigrateResult>(`/models/${seg(slug)}/inputs/migrate`, {
      method: 'POST',
      body: JSON.stringify({ inputs, version: version ?? null }),
    }),

  /** An extra file a pipeline wrote beside an output (spec 2026-09-27 §5.2). */
  outputFileUrl: (outputId: string, name: string) =>
    `${API_BASE}/outputs/${seg(outputId)}/files/${encodeURIComponent(name)}`,

  listOutputs: (slug: string) => request<Output[]>(`/models/${seg(slug)}/outputs`),

  getOutput: (id: string) => request<Output>(`/outputs/${seg(id)}`),

  /** Resolves an `/edit/{id}` deep link — from the record, or from the 3MF. */
  getEditTarget: (id: string) => request<EditTarget>(`/outputs/${seg(id)}/edit`),

  /** #2288 — a name for the output, or `null` (or blank) for none. */
  renameOutput: (id: string, name: string | null) =>
    command<Output>(`/outputs/${seg(id)}`, { method: 'PATCH', body: JSON.stringify({ name }) }),

  /** #316 — `deleteInboxCopies` also deletes the output's copies in Bambuddy's inbox folder. */
  deleteOutput: (id: string, deleteInboxCopies = false) =>
    command<void>(`/outputs/${seg(id)}${deleteInboxCopies ? '?delete_inbox_copies=true' : ''}`, {
      method: 'DELETE',
    }),

  downloadUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/model.3mf`,

  /**
   * #311 — one print. `printerMedia` also lists what the printer still holds, which
   * asks the printer, so the page does it only when told to.
   */
  getPrint: (archiveId: number, { printerMedia = false } = {}) =>
    request<PrintDetail>(`/prints/${archiveId}${printerMedia ? '?printer_media=1' : ''}`),

  /**
   * #2360 — a print's timelapse stills and Bambuddy's probe of it, read after the detail:
   * Bambuddy makes them with ffmpeg, which can take tens of seconds.
   */
  getTimelapseFrames: (archiveId: number) => request<TimelapseFrames>(`/prints/${archiveId}/timelapse/frames`),

  /** #311 — "Print again": queues the archive on its printer (Bambuddy's reprint is gone). */
  reprint: (archiveId: number) => command<PrintAgain>(`/prints/${archiveId}/reprint`, { method: 'POST' }),

  /** #311 — attaches a timelapse still on the printer to the print. */
  pullTimelapse: (archiveId: number, filename: string) =>
    command<void>(`/prints/${archiveId}/timelapse/pull`, {
      method: 'POST',
      body: JSON.stringify({ filename }),
    }),

  /** #309 — adds a photo or video of the finished object to the print. */
  addPrintAttachment: (
    archiveId: number,
    file: File,
    caption: string,
    onProgress?: (fraction: number) => void,
  ): Promise<PrintAttachment> =>
    uploadForm<PrintAttachment>(
      `/prints/${archiveId}/attachments`,
      () => {
        const body = new FormData()
        body.append('file', file)
        if (caption) body.append('caption', caption)
        return body
      },
      onProgress,
    ),

  /** #309 — an empty caption takes it off. */
  captionPrintAttachment: (archiveId: number, id: number, caption: string) =>
    command<PrintAttachment>(`/prints/${archiveId}/attachments/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ caption }),
    }),

  deletePrintAttachment: (archiveId: number, id: number) =>
    command<void>(`/prints/${archiveId}/attachments/${id}`, { method: 'DELETE' }),

  /** #309 — copies the attachment into the template's gallery. */
  promotePrintAttachment: (archiveId: number, id: number, slug: string) =>
    command<ModelSummary>(`/prints/${archiveId}/attachments/${id}/promote`, {
      method: 'POST',
      body: JSON.stringify({ slug }),
    }),

  outputThumbnailUrl: (id: string) => `${API_BASE}/outputs/${seg(id)}/thumbnail`,

  /** #83 — the 3MF's plates; ScadBuddy's own renders are always one. */
  getOutputPlates: (id: string) => request<OutputPlate[]>(`/outputs/${seg(id)}/plates`),

  outputPlateThumbnailUrl: (id: string, index: number) =>
    `${API_BASE}/outputs/${seg(id)}/plates/${index}/thumbnail`,

  /** #1723 — the output's preview mesh, every part in its colour. */
  /** The output's preview mesh, or with `plate` that one plate of its 3MF (#2169). */
  outputPreviewGlbUrl: (id: string, plate?: number) =>
    `${API_BASE}/outputs/${seg(id)}/preview.glb${plate === undefined ? '' : `?plate=${plate}`}`,

  sendOutput: (id: string, body: SendRequest) =>
    command<SendResult>(`/outputs/${seg(id)}/send`, { method: 'POST', body: JSON.stringify(body) }),

  /** Multipart with a `file` part — not a raw PNG body, and no `.png` in the path. */
  putThumbnail: (outputId: string, png: Blob) => {
    const body = new FormData()
    body.append('file', png, 'thumbnail.png')
    return command<void>(`/outputs/${seg(outputId)}/thumbnail`, { method: 'PUT', body })
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
   * #836 — how this printer's rack nozzle is ranked; `null` forgets it (Least used). A save
   * with a lower `version` than the stored one is refused with 409 (#1216).
   */
  putPrinterRackAlgorithm: (
    printerId: number,
    algorithm: RackAlgorithm | null,
    version: number = nextRackAlgorithmVersion(),
    signal?: AbortSignal,
  ) =>
    request<PrinterRackAlgorithm>(`/print/printers/${printerId}/rack-algorithm`, {
      method: 'PUT',
      body: JSON.stringify({ algorithm, version }),
      signal,
    }),

  /** #1298 — the recorded use of each hotend on this printer's rack, by position. */
  getPrinterRackUsage: (printerId: number) => request<PrinterRackUsage>(`/print/printers/${printerId}/rack-usage`),

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
   * one row per slot any plate uses — for an all-plates print, and `plateIds` those
   * plates' alone (#3652).
   */
  getFilaments: (
    outputId: string,
    query: { printerId?: number | null; plateId?: number; allPlates?: boolean; plateIds?: readonly number[] } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.plateId !== undefined) search.set('plate_id', String(query.plateId))
    if (query.allPlates) search.set('all_plates', 'true')
    for (const plate of query.plateIds ?? []) search.append('plate_ids', String(plate))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FilamentOptions>(`/print/outputs/${seg(outputId)}/filaments${suffix}`)
  },

  /** #2351 — Bambuddy's spool inventory, each spool with where it is loaded. */
  getSpoolInventory: (includeArchived = false) =>
    request<InventoryView>(`/farm/inventory${includeArchived ? '?include_archived=true' : ''}`),

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

  /** #717 — every filament profile, with what it changes and why. */
  listFilamentProfiles: () => request<FilamentProfile[]>('/print/filament-profiles'),

  /** #717 — each profile's changes against the process preset these choices resolve to. */
  previewFilamentProfiles: (choices: PrintChoices, profiles: string[]) =>
    request<ProfilePreview[]>('/print/filament-profiles/preview', {
      method: 'POST',
      body: JSON.stringify({ choices, profiles }),
    }),

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
   * #2169 — slice the dialog's choices in the background, through the run's own path,
   * without queueing; follow it with `getPreviewSlice`.
   */
  previewSlice: (outputId: string, body: PrintRunRequest) =>
    command<PreviewStarted>(`/print/outputs/${seg(outputId)}/preview-slice`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * #2169 — how an output's background slice stands, and what it came to once finished.
   * `slicer` is the one its start answered with: a native slice's id is its own
   * slicer's, so it is read with `?slicer=native` (plan 2026-10-16 Task 14).
   */
  getPreviewSlice: (outputId: string, jobId: number, slicer: Slicer = 'bambuddy') =>
    request<SlicePreview>(`/print/outputs/${seg(outputId)}/preview-slices/${jobId}${slicerQuery(slicer)}`),

  /** #2169 — as `getPreviewSlice`, for a library file's. */
  getLibraryPreviewSlice: (fileId: number, jobId: number, slicer: Slicer = 'bambuddy') =>
    request<SlicePreview>(`/print/library/${fileId}/preview-slices/${jobId}${slicerQuery(slicer)}`),

  /** #2072 — a finished background slice's toolpath, for the plate preview's slice views. */
  previewToolpathUrl: (outputId: string, jobId: number, slicer: Slicer = 'bambuddy') =>
    `${API_BASE}/print/outputs/${seg(outputId)}/preview-slices/${jobId}/toolpath${slicerQuery(slicer)}`,

  /** #2072 — as `previewToolpathUrl`, for a library file's. */
  libraryPreviewToolpathUrl: (fileId: number, jobId: number, slicer: Slicer = 'bambuddy') =>
    `${API_BASE}/print/library/${fileId}/preview-slices/${jobId}/toolpath${slicerQuery(slicer)}`,

  /**
   * #2164 — record in Bambuddy which spool is in a tray it had no spool for. Sent only
   * on the person's own "yes".
   */
  assignTraySpool: (printerId: number, amsId: number, trayId: number, spoolId: number) =>
    command<TrayAssigned>(`/print/printers/${printerId}/trays/${amsId}/${trayId}/spool`, {
      method: 'POST',
      body: JSON.stringify({ spool_id: spoolId }),
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

  /**
   * #284 — a fix's whole diff, where each line lands, whether it can be applied yet, and
   * the fingerprint an apply confirms against. Changes nothing (`post_preview`).
   */
  previewFix: (body: FixRequest) =>
    request<FixPreview>('/analyzers/fixes/preview', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * #284 — record a previewed fix as accepted at its scope (`post_apply`). A 409
   * `analyzer-fix-stale` means the diff, scope, print or base moved since the preview.
   */
  applyFix: (body: FixApply) =>
    request<AnalyzerDecision>('/analyzers/fixes/apply', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #284 — remove a decision, so its finding is open again (`delete_decision`). */
  deleteDecision: (id: string) =>
    request<void>(`/analyzers/decisions/${seg(id)}`, { method: 'DELETE' }),

  /**
   * #79 — Bambuddy's projects, each with the library folder that belongs to it, plus
   * the Settings default (`last_project_id`, #3188). ScadBuddy keeps no project
   * membership: which prints belong to a project is on the project's own page. What it
   * remembers per model is only the default the Customize page's picker opens on (#1660).
   */
  getProjects: () => request<ProjectChoices>('/print/projects'),

  /**
   * #3651 — the project a print of this content opens on: the one project it is all in
   * already, else the Settings default, else none.
   */
  getDefaultProject: (content: { outputIds?: readonly string[]; libraryFileIds?: readonly number[] }) => {
    const search = new URLSearchParams()
    for (const id of content.outputIds ?? []) search.append('output_id', id)
    for (const id of content.libraryFileIds ?? []) search.append('library_file_id', String(id))
    const query = search.toString()
    return request<DefaultDestination>(`/print/projects/default${query ? `?${query}` : ''}`)
  },

  /**
   * `project_id` links an existing project; otherwise `name` creates one. Either way the
   * project comes back with a library folder, because it is the folder — not the project
   * row — that makes Bambuddy's project page list the files.
   */
  createProject: (body: ProjectRequest) =>
    command<ProjectView>('/print/projects', { method: 'POST', body: JSON.stringify(body) }),

  /** #1660 — the project this model was last filed into, per preset it was generated with. */
  getModelProjects: (slug: string) => request<ModelProjects>(`/print/models/${seg(slug)}/projects`),

  /** #1660 — remember the project Generate filed this model into; a `null` project forgets it. */
  putModelProject: (slug: string, body: ModelProjectPut) =>
    request<ModelProjects>(`/print/models/${seg(slug)}/project`, {
      method: 'PUT',
      body: JSON.stringify(body),
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

  /** #3651 — the same, into a library folder of no project. */
  fileIntoFolder: (outputId: string, folderId: number) =>
    command<ProjectFile>(`/outputs/${seg(outputId)}/project-file`, {
      method: 'POST',
      body: JSON.stringify({ folder_id: folderId }),
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

  /** #981 — the printer's live camera, Bambuddy's MJPEG for an `<img>`: closing the
   * image (unmounting it) ends the stream. */
  printerCameraStreamUrl: (printerId: number) => `${API_BASE}/print/printers/${printerId}/camera/stream`,
  /** #796 — one camera frame; `at` makes each refresh a new request. */
  printerCameraUrl: (printerId: number, at: number) => `${API_BASE}/print/printers/${printerId}/camera?at=${at}`,

  /** #977 — every active printer's live state, whoever started its job. */
  getPrintersNow: () => request<PrintersNow>('/print/printers/now'),

  // #3823 — the printer registry: one row per physical printer, keyed by serial, with
  // its Bambuddy link and its LAN link. The LAN access code is sent once (`PUT /lan`,
  // which tests it on the printer first) and never returned.
  listPrinters: () => request<PrinterRow[]>('/printers'),
  createPrinter: (body: PrinterCreate) =>
    request<PrinterRow>('/printers', { method: 'POST', body: JSON.stringify(body) }),
  patchPrinter: (id: string, body: PrinterPatch) =>
    request<PrinterRow>(`/printers/${seg(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deletePrinter: (id: string) => request<void>(`/printers/${seg(id)}`, { method: 'DELETE' }),
  /** Bambuddy's active printers, each with the row linking it, if any. */
  discoverPrinters: () => request<DiscoveredPrinter[]>('/printers/discover'),
  linkPrinterBambuddy: (id: string, bambuddyPrinterId: number) =>
    request<PrinterRow>(`/printers/${seg(id)}/bambuddy`, {
      method: 'PUT',
      body: JSON.stringify({ bambuddy_printer_id: bambuddyPrinterId }),
    }),
  unlinkPrinterBambuddy: (id: string) => request<PrinterRow>(`/printers/${seg(id)}/bambuddy`, { method: 'DELETE' }),
  linkPrinterLan: (id: string, body: LanLink) =>
    request<PrinterRow>(`/printers/${seg(id)}/lan`, { method: 'PUT', body: JSON.stringify(body) }),
  unlinkPrinterLan: (id: string) => request<PrinterRow>(`/printers/${seg(id)}/lan`, { method: 'DELETE' }),

  /** #2351 — Bambuddy's queue in its order; `status` narrows it (finished items stay in it). */
  getPrintQueue: (status?: 'pending' | 'printing' | 'completed' | 'failed' | 'cancelled') =>
    request<QueueItem[]>(`/farm/queue${status ? `?status=${status}` : ''}`),

  /** #2351 — the farm's totals over every Bambuddy archive. */
  getFarmStats: () => request<ArchiveStats>('/farm/stats'),

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
  /** The activity list, newest first, a page at a time; `cursor` is the last page's
   * `next_cursor`. A `kind` or `status` of several comma-joined values is sent repeated. */
  listActivity: (query: ActivityQuery, page: { cursor?: string | null; limit?: number } = {}) => {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(apiParams(query))) {
      if (key === 'kind' || key === 'status') {
        for (const one of splitList(value)) search.append(key, one)
      } else search.set(key, value)
    }
    if (page.limit !== undefined) search.set('limit', String(page.limit))
    if (page.cursor) search.set('cursor', page.cursor)
    const suffix = search.size > 0 ? `?${search.toString()}` : ''
    return request<ActivityPage>(`/activity${suffix}`)
  },
  activityFacets: (since?: string) =>
    request<ActivityFacets>(`/activity/facets${since ? `?since=${encodeURIComponent(since)}` : ''}`),
  getActivity: (id: string, advanced: boolean) =>
    request<ActivityDetail>(`/activity/${seg(id)}${advanced ? '?advanced=1' : ''}`),

  // -- orders (#4249, spec 2026-10-18 §4) -------------------------------------------------
  /** Customers by name, at most `limit`; `q` matches the name, the company and any contact. */
  customers: (query: { q?: string; includeArchived?: boolean; limit?: number } = {}) => {
    const search = new URLSearchParams()
    if (query.q) search.set('q', query.q)
    if (query.includeArchived) search.set('include_archived', 'true')
    if (query.limit !== undefined) search.set('limit', String(query.limit))
    const suffix = search.size > 0 ? `?${search.toString()}` : ''
    return request<Customer[]>(`/customers${suffix}`)
  },
  customer: (id: string) => request<Customer>(`/customers/${seg(id)}`),
  createCustomer: (body: CustomerCreate) =>
    request<Customer>('/customers', { method: 'POST', body: JSON.stringify(body) }),
  updateCustomer: (id: string, body: CustomerPatch) =>
    request<Customer>(`/customers/${seg(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  archiveCustomer: (id: string) => request<Customer>(`/customers/${seg(id)}/archive`, { method: 'POST' }),
  /** Orders, newest first, a page at a time; `cursor` is the last page's `next_cursor`. */
  orders: (query: OrdersQuery, page: { cursor?: string | null; limit?: number } = {}) => {
    const search = new URLSearchParams(ordersApiParams(query))
    if (page.limit !== undefined) search.set('limit', String(page.limit))
    if (page.cursor) search.set('cursor', page.cursor)
    const suffix = search.size > 0 ? `?${search.toString()}` : ''
    return request<OrderPage>(`/orders${suffix}`)
  },
  order: (id: string) => request<OrderDetail>(`/orders/${seg(id)}`),
  orderEvents: (id: string) => request<OrderEvent[]>(`/orders/${seg(id)}/events`),
  createOrder: (body: OrderCreate) => request<Order>('/orders', { method: 'POST', body: JSON.stringify(body) }),
  updateOrder: (id: string, body: OrderPatch) =>
    request<Order>(`/orders/${seg(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  acceptOrder: (id: string) => request<Order>(`/orders/${seg(id)}/accept`, { method: 'POST' }),
  cancelOrder: (id: string, reason?: string) =>
    request<Order>(`/orders/${seg(id)}/cancel`, { method: 'POST', body: JSON.stringify({ reason: reason ?? null }) }),
  holdOrder: (id: string) => request<Order>(`/orders/${seg(id)}/hold`, { method: 'POST' }),
  releaseOrder: (id: string) => request<Order>(`/orders/${seg(id)}/release`, { method: 'POST' }),
  closeOrder: (id: string) => request<Order>(`/orders/${seg(id)}/close`, { method: 'POST' }),
  addPayment: (id: string, body: PaymentCreate) =>
    request<Payment>(`/orders/${seg(id)}/payments`, { method: 'POST', body: JSON.stringify(body) }),
  addOrderItem: (orderId: string, body: OrderItemCreate) =>
    request<OrderItem>(`/orders/${seg(orderId)}/items`, { method: 'POST', body: JSON.stringify(body) }),
  updateOrderItem: (orderId: string, itemId: string, body: OrderItemPatch) =>
    request<OrderItem>(`/orders/${seg(orderId)}/items/${seg(itemId)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteOrderItem: (orderId: string, itemId: string) =>
    request<void>(`/orders/${seg(orderId)}/items/${seg(itemId)}`, { method: 'DELETE' }),
  cancelOrderItem: (orderId: string, itemId: string) =>
    request<OrderItem>(`/orders/${seg(orderId)}/items/${seg(itemId)}/cancel`, { method: 'POST' }),
  quoteOrderItem: (orderId: string, itemId: string, body: ManualQuoteInput) =>
    request<Quote>(`/orders/${seg(orderId)}/items/${seg(itemId)}/quotes`, { method: 'POST', body: JSON.stringify(body) }),
  /** #4250 — the `order_item_quote` operation: render if needed, slice every plate with the
   * dialog's choices, price with the book; followed to the quote as every command is. */
  quoteItem: (orderId: string, itemId: string, body: SliceQuoteRequest) =>
    command<Quote>(`/orders/${seg(orderId)}/items/${seg(itemId)}/quote`, { method: 'POST', body: JSON.stringify(body) }),
  // -- the price book and the rough estimate (#4250, spec 2026-10-18 §5.1, §5.3) -------------
  priceBook: () => request<PriceBook>('/orders/price-book'),
  setPriceBook: (body: PolicyUpdate) => request<PriceBook>('/orders/price-book', { method: 'PUT', body: JSON.stringify(body) }),
  createFilament: (body: FilamentInput) =>
    request<Filament>('/orders/price-book/filaments', { method: 'POST', body: JSON.stringify(body) }),
  updateFilament: (id: string, body: FilamentPatch) =>
    request<Filament>(`/orders/price-book/filaments/${seg(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  archiveFilament: (id: string) => request<Filament>(`/orders/price-book/filaments/${seg(id)}/archive`, { method: 'POST' }),
  /** A rough estimate (§5.3), never a quote: nothing is recorded. */
  orderEstimate: (outputId: string, filamentId: string) =>
    request<OrderEstimate>(`/orders/estimate?${new URLSearchParams({ output_id: outputId, filament_id: filamentId })}`),
  acceptOrderQuote: (orderId: string, itemId: string, quoteId: string) =>
    request<OrderItem>(`/orders/${seg(orderId)}/items/${seg(itemId)}/quotes/${seg(quoteId)}/accept`, { method: 'POST' }),
  recordFulfilment: (orderId: string, itemId: string, body: FulfilmentInput) =>
    request<Fulfilment>(`/orders/${seg(orderId)}/items/${seg(itemId)}/fulfilments`, { method: 'POST', body: JSON.stringify(body) }),
  updateFulfilment: (orderId: string, itemId: string, fulfilmentId: string, body: FulfilmentInput) =>
    request<Fulfilment>(`/orders/${seg(orderId)}/items/${seg(itemId)}/fulfilments/${seg(fulfilmentId)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  acceptFulfilment: (orderId: string, itemId: string, fulfilmentId: string) =>
    request<Fulfilment>(`/orders/${seg(orderId)}/items/${seg(itemId)}/fulfilments/${seg(fulfilmentId)}/accept`, { method: 'POST' }),
  addShipment: (orderId: string, body: ShipmentInput) =>
    request<Shipment>(`/orders/${seg(orderId)}/shipments`, { method: 'POST', body: JSON.stringify(body) }),
  updateShipment: (orderId: string, shipmentId: string, body: ShipmentEdit) =>
    request<Shipment>(`/orders/${seg(orderId)}/shipments/${seg(shipmentId)}`, { method: 'PATCH', body: JSON.stringify(body) }),

  /** #313 — Bambuddy's folder tree and one folder's files; `all` adds sliced files and STLs. */
  /** `fileId` in place of `folderId` lists the folder that file is in (#2165). */
  listLibrary: (query: { folderId: number | null; all: boolean; fileId?: number }) => {
    const search = new URLSearchParams()
    if (query.folderId !== null) search.set('folder_id', String(query.folderId))
    else if (query.fileId !== undefined) search.set('file_id', String(query.fileId))
    if (query.all) search.set('all', 'true')
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<LibraryListing>(`/print/library${suffix}`)
  },

  /** #3630 — library files whose name or folder holds every word, from every folder. */
  searchLibrary: (query: { q: string; all: boolean; fresh?: boolean }, signal?: AbortSignal) => {
    const search = new URLSearchParams({ q: query.q })
    if (query.all) search.set('all', 'true')
    if (query.fresh) search.set('fresh', 'true')
    return request<LibrarySearch>(`/print/library/search?${search}`, { signal })
  },

  /** #2167 — files to Bambuddy's trash; the ones it skipped come back in `skipped`. */
  deleteLibraryFiles: (fileIds: number[]) =>
    command<LibraryDeleteResult>('/print/library/delete', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds }),
    }),

  /** #2167 — a delete's Undo: the files back from Bambuddy's trash. */
  restoreLibraryFiles: (fileIds: number[]) =>
    command<LibraryRestoreResult>('/print/library/restore', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds }),
    }),

  /** #2670 — files into another folder (`null`, the top level), each under its own id. */
  moveLibraryFiles: (fileIds: number[], folderId: number | null) =>
    command<LibraryMoveResult>('/print/library/move', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds, folder_id: folderId }),
    }),

  /** #2670 — a copy of each file in a folder (`null`, the top level), each a new file. */
  copyLibraryFiles: (fileIds: number[], folderId: number | null) =>
    command<LibraryCopyResult>('/print/library/copy', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds, folder_id: folderId }),
    }),

  /** #2670 — a new name for a file, its extension kept. */
  renameLibraryFile: (fileId: number, filename: string) =>
    command<LibraryRenameResult>('/print/library/rename', {
      method: 'POST',
      body: JSON.stringify({ file_id: fileId, filename }),
    }),

  /** #3650 — a folder renamed, or moved under another (`parent_id` 0 is the top level). */
  updateLibraryFolder: (folderId: number, update: LibraryFolderUpdate) =>
    command<LibraryFolder>(`/print/library/folders/${folderId}`, {
      method: 'PATCH',
      body: JSON.stringify(update),
    }),

  /** #3650 — a new folder, at the top level without `parent_id`. */
  createLibraryFolder: (body: LibraryFolderCreate) =>
    command<LibraryFolder>('/print/library/folders', { method: 'POST', body: JSON.stringify(body) }),

  /** #3650 — a folder and everything in it: its files to Bambuddy's trash, then its folders. */
  deleteLibraryFolder: (folderId: number) =>
    command<LibraryFolderDeleteResult>(`/print/library/folders/${folderId}/delete`, { method: 'POST' }),

  libraryThumbnailUrl: (fileId: number) => `${API_BASE}/print/library/${fileId}/thumbnail`,

  libraryPlateThumbnailUrl: (fileId: number, index: number) =>
    `${API_BASE}/print/library/${fileId}/plates/${index}/thumbnail`,

  /** #1723 — one plate of the library file as a preview mesh (#1753). */
  libraryPreviewGlbUrl: (fileId: number, plate: number) =>
    `${API_BASE}/print/library/${fileId}/preview.glb?plate=${plate}`,

  /** #2349 — library files' names by id; `name: null` for one Bambuddy no longer has. */
  libraryFileNames: (ids: readonly number[]) =>
    request<LibraryFileName[]>(`/print/library/names?${ids.map((id) => `ids=${id}`).join('&')}`),
  getLibraryPlates: (fileId: number) => request<OutputPlate[]>(`/print/library/${fileId}/plates`),

  /** #1863 — the objects Arrange reads from a file ScadBuddy did not make. */
  getLibraryObjects: (fileId: number) => request<LibraryFileObjects>(`/print/library/${fileId}/objects`),

  getLibraryChoices: (fileId: number, printerId?: number | null) => {
    const search = new URLSearchParams()
    if (printerId !== null && printerId !== undefined) search.set('printer_id', String(printerId))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<ChoicesView>(`/print/library/${fileId}/choices${suffix}`)
  },

  getLibraryFilaments: (
    fileId: number,
    query: { printerId?: number | null; plateId?: number; allPlates?: boolean; plateIds?: readonly number[] } = {},
  ) => {
    const search = new URLSearchParams()
    if (query.printerId !== null && query.printerId !== undefined) {
      search.set('printer_id', String(query.printerId))
    }
    if (query.plateId !== undefined) search.set('plate_id', String(query.plateId))
    if (query.allPlates) search.set('all_plates', 'true')
    for (const plate of query.plateIds ?? []) search.append('plate_ids', String(plate))
    const suffix = search.size > 0 ? `?${search}` : ''
    return request<FilamentOptions>(`/print/library/${fileId}/filaments${suffix}`)
  },

  /** #742 — followed to its end like an output's run (`runPrint`). */
  runLibraryPrint: (fileId: number, body: PrintRunRequest, signal?: AbortSignal, within?: Within) =>
    followPrintRun(`/print/library/${fileId}/run`, body, signal, within),

  /** #1751 — the file's newest print, as `getPrintProgress` is an output's; `null` when
   * ScadBuddy never queued it. */
  getLibraryPrintProgress: (fileId: number) =>
    request<PrintProgress | null>(`/print/library/${fileId}/progress`),

  /** #1751 — as `attachToProject`; without ids, the file's newest run is filed. */
  attachLibraryToProject: (fileId: number, body: ProjectAttach) =>
    command<AttachResult>(`/print/library/${fileId}/project`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  checkLibraryPrint: (fileId: number, body: PrintRunRequest) =>
    request<PrintCheck>(`/print/library/${fileId}/check`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #2169 — slice the dialog's choices for a library file in the background. */
  previewLibrarySlice: (fileId: number, body: PrintRunRequest) =>
    command<PreviewStarted>(`/print/library/${fileId}/preview-slice`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #2672 — a batch print over library files, opened on the first file's step. */
  createPrintBatch: (files: NewBatchFile[]) =>
    request<PrintBatch>('/print/batches', { method: 'POST', body: JSON.stringify({ files }) }),

  /** #2672 — a batch print as it was left, to resume it. */
  getPrintBatch: (batchId: string) => request<PrintBatch>(`/print/batches/${seg(batchId)}`),

  /** #2718 review — batches kept with a file still being checked, to resume one. */
  listPendingPrintBatches: () => request<PrintBatch[]>('/print/batches'),

  /** #2672 — the step in view: a file's position, or the file count for the review. */
  putPrintBatchStep: (batchId: string, step: number) =>
    request<PrintBatch>(`/print/batches/${seg(batchId)}/step`, {
      method: 'PUT',
      body: JSON.stringify({ step }),
    }),

  /** #2672 — one file's choices, or that it is skipped or removed. */
  putPrintBatchItem: (batchId: string, position: number, choice: BatchChoice) =>
    request<PrintBatch>(`/print/batches/${seg(batchId)}/items/${position}`, {
      method: 'PUT',
      body: JSON.stringify(choice),
    }),

  /**
   * #2672 — starts each chosen file's library run and says what happened to each. A
   * request answers in time rather than for every file: a 202 says where to go on from,
   * and this follows it to the end (#2951), handing each step's batch to `onProgress`.
   */
  queuePrintBatch: async (
    batchId: string,
    onProgress?: (batch: PrintBatch) => void,
    signal?: AbortSignal,
  ): Promise<PrintBatch> => {
    let from = 0
    for (;;) {
      // Aborted (the dialog closed): send none of the rest; the next Queue all does.
      signal?.throwIfAborted()
      const query = from === 0 ? '' : `?from_position=${from}`
      const answer = await requestWithStatus<PrintBatch | BatchQueueProgress>(
        `/print/batches/${seg(batchId)}/queue${query}`,
        { method: 'POST', signal },
      )
      if (answer.status !== 202) return answer.body as PrintBatch
      const progress = answer.body as BatchQueueProgress
      // Not once the dialog is gone, though the 202 arrived (#2718 review).
      if (!signal?.aborted) onProgress?.(progress.batch)
      from = progress.next_position
    }
  },

  /** #2672 — forgets the batch; whatever it queued stays queued. */
  deletePrintBatch: (batchId: string) =>
    request<void>(`/print/batches/${seg(batchId)}`, { method: 'DELETE' }),

  /** #2672 — one run as it stands, for a batch's per-file result. */
  getPrintRun: (runId: string) => request<PrintRun>(`/print/runs/${seg(runId)}`),

  putLibraryChoices: (fileId: number, body: ModelPrintChoices) =>
    request<ModelPrintChoices>(`/print/library/${fileId}/choices`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  listFonts: () => request<FontFamily[]>('/fonts'),

  /** #2358 — the characters of `text` the font has no glyph for, which a render leaves out. */
  getGlyphs: (font: string, text: string) =>
    request<GlyphCoverage>(`/fonts/glyphs?${new URLSearchParams({ font, text })}`),

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
    command<InstalledFamily>('/fonts/install', {
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

  /** #2770 — removes one checkout from the volume; a 409 while any live pin or running render reads it. */
  removeLibraryCheckout: (name: string, commit: string) =>
    command<void>(`/libraries/${seg(name)}?commit=${encodeURIComponent(commit)}`, { method: 'DELETE' }),

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

  /**
   * #1285 — what each `include`/`use` of the saved source resolves to, a library that
   * would provide each unresolved one, and the `font = "…"` families not installed.
   * Read-only; nothing is cloned.
   */
  checkDependencies: (slug: string) =>
    request<DependencyReport>(`/models/${seg(slug)}/dependencies`, { method: 'POST' }),

  /** With `index` (#217), only the invalid entry at that position of `libraries`. */
  unpinModelLibrary: (slug: string, name: string, index?: number) =>
    command<ModelSummary>(
      `/models/${seg(slug)}/libraries/${seg(name)}${index === undefined ? '' : `?index=${index}`}`,
      { method: 'DELETE' },
    ),

  /**
   * #1039 — callers that ask at once share one request: the shell's unit and link
   * loaders and the page all read it on the same load. Nothing is kept once it
   * answers, so a later call always reads afresh, and a save stops later callers
   * joining a read that started before it, as does one that has hung past
   * `SETTINGS_SHARE_MS` (#1856). Each caller gets its own copy.
   */
  getSettings: (): Promise<Settings> => {
    const now = Date.now()
    if (!settingsInFlight || now - settingsInFlight.started >= SETTINGS_SHARE_MS) {
      const scope = new ReadScope()
      const shared = {
        read: inReadScope(scope, () => request<Settings>('/settings')).finally(() => {
          if (settingsInFlight === shared) settingsInFlight = undefined
        }),
        started: now,
        scope,
      }
      settingsInFlight = shared
    }
    // A caller joining it saw only what it saw.
    adoptScope(settingsInFlight.scope)
    return settingsInFlight.read.then((settings) => structuredClone(settings))
  },

  putSettings: (body: SettingsUpdate) => {
    settingsInFlight = undefined
    return request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(body) })
  },

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
   * The assistant's `http_request` tool (#827), served by the agent service. On by
   * default. Fails (404 or 503) when there is no agent or no AI database.
   */
  getHttpRequestSetting: () => request<HttpRequestSetting>('/ai/settings/http-request'),

  putHttpRequestSetting: (enabled: boolean) =>
    request<HttpRequestSetting>('/ai/settings/http-request', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  /**
   * #982 — the headless browser's allow and deny lists, and the origins approved in each
   * chat, served by the agent service. Fails (404 or 503) without the agent or its database.
   */
  getBrowserOrigins: () => request<BrowserOriginsSetting>('/ai/settings/browser-origins'),

  putBrowserOrigins: (lists: { allow: string[]; deny: string[] }) =>
    request<BrowserOriginsSetting>('/ai/settings/browser-origins', { method: 'PUT', body: JSON.stringify(lists) }),

  /** #982 — "Always allow" on a browser approval card: one origin added to the allow list. */
  allowBrowserOrigin: (rule: string) =>
    request<BrowserOriginsSetting>('/ai/settings/browser-origins/allow', { method: 'POST', body: JSON.stringify({ rule }) }),

  /** #982 — "Allow for this chat": the origin opens without asking for the rest of the session. */
  approveBrowserOrigin: (sessionId: string, origin: string) =>
    request<{ origin: string; scope: 'session' }>(`/ai/sessions/${seg(sessionId)}/browser-origins`, {
      method: 'POST',
      body: JSON.stringify({ origin }),
    }),

  revokeBrowserOrigin: (sessionId: string, origin: string) =>
    request<void>(`/ai/sessions/${seg(sessionId)}/browser-origins?origin=${encodeURIComponent(origin)}`, { method: 'DELETE' }),

  /**
   * #1941 — uploads an image the user attached in the assistant panel; its message then
   * sends the id. #2479 — as bytes: the image and its preview are two file parts, each
   * typed, never base64 in JSON.
   */
  uploadAttachment: (image: { mediaType: string; blob: Blob; preview: { mediaType: string; blob: Blob } }) => {
    const body = new FormData()
    body.append('image', new Blob([image.blob], { type: image.mediaType }), 'image')
    body.append('preview', new Blob([image.preview.blob], { type: image.preview.mediaType }), 'preview')
    return request<AttachmentView>('/ai/attachments', { method: 'POST', body })
  },

  /** #1941 — deletes an upload the user removed from the composer before sending it. */
  deleteAttachment: (id: string) =>
    request<void>(`/ai/attachments/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** The long edge the assistant panel scales attached images to, served by the agent service. */
  getImageSettings: () => request<ImageSettings>('/ai/settings/images'),

  putImageSettings: (longEdge: number) =>
    request<ImageSettings>('/ai/settings/images', {
      method: 'PUT',
      body: JSON.stringify({ long_edge: longEdge }),
    }),

  /** The automatic Hindsight recall query's cap, in characters, served by the agent service. */
  getMemorySettings: () => request<MemorySettings>('/ai/settings/memory'),

  putMemorySettings: (recallMaxQueryChars: number) =>
    request<MemorySettings>('/ai/settings/memory', {
      method: 'PUT',
      body: JSON.stringify({ recall_max_query_chars: recallMaxQueryChars }),
    }),

  /** #2329 — the caps on the assistant's workspace (bytes), served by the agent service. */
  getWorkspaceSettings: () => request<WorkspaceSettings>('/ai/settings/workspace'),

  putWorkspaceSettings: (caps: { file_max_bytes: number; workspace_max_bytes: number }) =>
    request<WorkspaceSettings>('/ai/settings/workspace', { method: 'PUT', body: JSON.stringify(caps) }),

  /** #2329 — the files in a session's workspace, with its usage and caps. */
  listAiSessionFiles: (id: string) => request<WorkspaceFileList>(`/ai/sessions/${seg(id)}/files`),

  /**
   * #2329 — one workspace file's bytes. The route always answers as an attachment, so
   * the panel fetches it and hands the blob to `downloadBlob` or an object URL.
   */
  getAiSessionFile: async (id: string, path: string): Promise<Blob> => {
    const response = await send(`${API_BASE}${sessionFilePath(id, path)}`)
    if (!response.ok) throw new ApiError(await readProblem(response))
    return await response.blob()
  },

  /**
   * #2329 — uploads a file into a session's workspace at `path`. A path that exists is
   * refused (409) unless `expectedSha256` is the sha256 it has now.
   */
  uploadAiSessionFile: (id: string, file: File, path: string, expectedSha256?: string) => {
    const body = new FormData()
    body.set('file', file)
    body.set('path', path)
    if (expectedSha256 !== undefined) body.set('expected_sha256', expectedSha256)
    return request<{ file: WorkspaceFile }>(`/ai/sessions/${seg(id)}/files`, { method: 'POST', body })
  },

  /** #2329 — deletes one file from a session's workspace. */
  deleteAiSessionFile: (id: string, path: string) =>
    request<void>(sessionFilePath(id, path), { method: 'DELETE' }),

  /** A session's background tasks (CronCreate, ScheduleWakeup): the active ones first, then the last ended. */
  listAiBackgroundTasks: (id: string) => request<{ tasks: BackgroundTask[] }>(`/ai/sessions/${seg(id)}/background-tasks`),

  /** Cancels one; the session's owner only. */
  cancelAiBackgroundTask: (id: string, taskId: string) =>
    request<{ task: BackgroundTask }>(`/ai/sessions/${seg(id)}/background-tasks/${seg(taskId)}`, { method: 'DELETE' }),

  /** Plan 5d — the mode new assistant sessions get by default, served by the agent service. */
  getSessionMode: () => request<SessionModeSetting>('/ai/settings/session-mode'),

  putSessionMode: (mode: SessionModeSetting['mode']) =>
    request<SessionModeSetting>('/ai/settings/session-mode', {
      method: 'PUT',
      body: JSON.stringify({ mode }),
    }),

  /** Where durable subagents reach ScadBuddy's tools, served by the agent service. */
  getDurableSubagents: () => request<DurableSubagentsSetting>('/ai/settings/durable-subagents'),

  putDurableSubagents: (setting: DurableSubagentsSetting) =>
    request<DurableSubagentsSetting>('/ai/settings/durable-subagents', {
      method: 'PUT',
      body: JSON.stringify(setting),
    }),

  /**
   * #1911 — the switch for the assistant's `get_printer_camera` tool, served by the agent
   * service. On by default. Fails (404 or 503) when there is no agent or no AI database.
   */
  getPrinterCameraSetting: () => request<PrinterCameraSetting>('/ai/settings/printer-camera'),

  putPrinterCameraSetting: (enabled: boolean) =>
    request<PrinterCameraSetting>('/ai/settings/printer-camera', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  /** #1917 — the Claude model the assistant uses (`null`: Claude Code's default), served by the agent service. */
  getModelSetting: () => request<ModelSetting>('/ai/settings/model'),

  putModelSetting: (model: string | null) =>
    request<ModelSetting>('/ai/settings/model', {
      method: 'PUT',
      body: JSON.stringify({ model }),
    }),

  /** #1291 — New model's "Start from blank template", served by the agent service. */
  getBlankTemplate: () => request<BlankTemplate>('/ai/templates/blank'),

  /** #790 — the budget and turn limit new assistant sessions get, served by the agent service. */
  getSessionLimits: () => request<SessionLimits>('/ai/settings/session-limits'),

  putSessionLimits: (limits: SessionLimits) =>
    request<SessionLimits>('/ai/settings/session-limits', {
      method: 'PUT',
      body: JSON.stringify(limits),
    }),

  /** The park hold, warm process cache and turn limit, served by the agent service. */
  getSessionRuntime: () => request<SessionRuntime>('/ai/settings/session-runtime'),

  putSessionRuntime: (settings: Partial<SessionRuntime>) =>
    request<SessionRuntime>('/ai/settings/session-runtime', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),

  /** How many of the assistant's renders run at once, per caller and in all; read live by the agent. */
  getRenderLimits: () => request<RenderLimits>('/ai/settings/render-limits'),

  putRenderLimits: (limits: RenderLimits) =>
    request<RenderLimits>('/ai/settings/render-limits', {
      method: 'PUT',
      body: JSON.stringify(limits),
    }),

  /**
   * #790, #794 — a new session with this one's transcript and a fresh budget: "Continue in
   * a new chat", Fork, and with `upTo` (a reply's message id) "Fork from here", which
   * keeps the conversation only through that reply.
   */
  forkAiSession: (id: string, options: { upTo?: string } = {}) =>
    request<{ session: AiSessionView }>(`/ai/sessions/${encodeURIComponent(id)}/fork`, {
      method: 'POST',
      ...(options.upTo === undefined ? {} : { body: JSON.stringify({ up_to: options.upTo }) }),
    }),

  /** #795, #1885 — renames a session, marks it done, or archives or unarchives it; only the session's owner can. */
  updateAiSession: (id: string, edit: AiSessionEdit) =>
    request<{ session: AiSessionView }>(`/ai/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(edit),
    }),

  /** #1885 — the archived sessions, newest first: the switcher's Archived view. */
  listArchivedAiSessions: (limit: number) =>
    request<{ sessions: AiSessionView[] }>(`/ai/sessions?archived=true&limit=${limit}`),

  /** #790 — adds to one session's budget; only the user can (it spends money). */
  raiseAiSessionBudget: (id: string, addUsd: number) =>
    request<{ session: AiSessionView }>(`/ai/sessions/${encodeURIComponent(id)}/budget`, {
      method: 'POST',
      body: JSON.stringify({ add_usd: addUsd }),
    }),

  /** #1284 — withdraws the session's live handoff offer; `cancelled` is false when there was none. */
  cancelAiSessionOffer: (id: string) =>
    request<{ cancelled: boolean; session: AiSessionView }>(`/ai/sessions/${encodeURIComponent(id)}/handoff`, {
      method: 'DELETE',
    }),

  /** #1288 — one session, as the browser user sees it; 404 when it is gone or not theirs to show. */
  getAiSession: (id: string) => request<AiSessionView>(`/ai/sessions/${encodeURIComponent(id)}`),

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
