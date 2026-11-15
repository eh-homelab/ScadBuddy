import { WAIT_FIRST_MS, WAIT_MAX_MS } from './backendUp.js'
import { CrudReadError, crudRead } from './crudRead.js'

// The rolling deploy (#5299 D9, as #5298's workers do): a new agent may meet an API pod older
// than an area's routes. `CrudAreas` reads `GET …/crud/agent/areas` from the start, backing off,
// until the API lists every area the agent wants; until then each area stays on SQL, and once
// listed it goes through the routes for good. An area is listed only once all its routes are in
// (backend api/crud_areas.py `CRUD_AREAS`). The reads of the backend's tables (areas `events`
// and `flows`) need no wait: they have no SQL left (api/crudRead.ts).

/**
 * The areas of the agent's settings, Claude credentials and payload data keys, which the API
 * seals and opens with the KEK (#5299 step 4, D5; backend api/crud_agent_secrets.py).
 */
export const SECRET_AREAS = ['settings', 'credentials', 'payload-keys'] as const

/**
 * The area of forgetting a subject (its tombstone, key and flow images), the key deletes no
 * forget makes, and a flow run's images (#5299 step 6; backend api/crud_agent_forget.py).
 */
export const FORGET_AREAS = ['forget'] as const

/**
 * The area of the workspace files, which the API keeps, seals and opens (#5299 step 7; backend
 * api/crud_agent_workspace.py; workspace/crudStore.ts).
 */
export const WORKSPACE_AREAS = ['workspace'] as const

/**
 * The areas of a session's images, the staged attachments and the saved http_request bodies
 * (#5299 step 7; backend api/crud_agent_blobs.py; sessions/crudBlobs.ts, attachments/crudStore.ts).
 */
export const BLOB_AREAS = ['session-blobs', 'attachments', 'http-bodies'] as const

/**
 * The areas of a session's background tasks (with a Monitor's events and a Workflow's journal)
 * and of the agent's command record, `ai_operations` (#5299 step 8; backend
 * api/crud_agent_tasks.py; background/store.ts, operations/store.ts).
 */
export const TASK_AREAS = ['background-tasks', 'operations'] as const

/**
 * The tool-call gate's areas (#5299 step 10). `gate`, its writes: an entry opened, resolved or
 * decided, a flow's refusal and a request's response read (backend api/crud_agent_gate.py;
 * gate/crudStore.ts); a run grant's check goes through its route whether or not it is listed.
 * `pending-input`: the projection's reads, the orphan sweep's cancels, a durable turn's response
 * read and its fenced span mark, and a call's recorded approval (backend
 * api/crud_agent_pending.py; gate/crudPending.ts). `audit`: the audit log's rows written, listed
 * and pruned (backend api/crud_agent_audit.py; audit/crudAudit.ts). `approvals`: the classic
 * approvals, each decision written once with its events and audit row (backend
 * api/crud_agent_approvals.py; approvals/crudApprovals.ts).
 */
export const GATE_AREAS = ['gate', 'pending-input', 'audit', 'approvals', 'questions'] as const

export type CrudAreasOptions = {
  fetch?: typeof fetch
  /** The areas the agent waits for. */
  wanted: readonly string[]
  log?: (message: string) => void
  /** The back-off between reads of the areas, doubling (`WAIT_FIRST_MS` to `WAIT_MAX_MS`). */
  firstMs?: number
  maxMs?: number
}

/** Which of the agent's CRUD areas the API serves (see above). */
export class CrudAreas {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #wanted: readonly string[]
  readonly #log: (message: string) => void
  readonly #firstMs: number
  readonly #maxMs: number
  readonly #listed = new Set<string>()
  #timer: NodeJS.Timeout | undefined
  #started = false
  #stopped = false
  /** The kinds of reason for waiting already logged, each once (`refresh`). */
  readonly #logged = new Set<string>()

  constructor(baseUrl: string, options: CrudAreasOptions) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#wanted = options.wanted
    this.#log = options.log ?? ((m) => console.log(m))
    this.#firstMs = options.firstMs ?? WAIT_FIRST_MS
    this.#maxMs = options.maxMs ?? WAIT_MAX_MS
  }

  /** Whether the API serves `area`: once true, true for good. */
  has(area: string): boolean {
    return this.#listed.has(area)
  }

  /** Whether every wanted area is served. */
  get complete(): boolean {
    return this.#wanted.every((a) => this.#listed.has(a))
  }

  /** Reads the areas until every wanted one is listed, in the background; never throws. */
  start(): void {
    if (this.#started) return
    this.#started = true
    void this.#poll(this.#firstMs)
  }

  stop(): void {
    this.#stopped = true
    clearTimeout(this.#timer)
  }

  /**
   * One read of the areas; whether every wanted one is listed now. Logs each kind of reason for
   * waiting once (the areas missing, a status and whose answer it was, a transport failure),
   * never again for a detail that varies.
   */
  async refresh(): Promise<boolean> {
    let why: string
    let kind: string
    try {
      const { areas } = await crudRead<{ areas: unknown }>(this.#fetch, this.#base, 'its areas', '/areas')
      const before = this.complete
      for (const area of Array.isArray(areas) ? areas : []) {
        if (typeof area === 'string' && this.#wanted.includes(area)) this.#listed.add(area)
      }
      if (this.complete) {
        if (!before) this.#log(`CRUD areas: the agent reads ${this.#wanted.join(', ')} through the backend's CRUD routes`)
        return true
      }
      why = `missing ${this.#wanted.filter((a) => !this.#listed.has(a)).join(', ')}`
      kind = why
    } catch (error) {
      const refusal = error instanceof CrudReadError ? error : undefined
      if (refusal?.fromRoute && (refusal.status === 401 || refusal.status === 403)) {
        // The route's own refusal, not an older pod's or a proxy's: no live credential, or one
        // without `crud.agent` (#6234 review).
        why = `the backend refused the agent's service credential (HTTP ${refusal.status}); check SCADBUDDY_SERVICE_TOKEN_FILE or the credential view`
      } else {
        why = (error as Error).message
      }
      // A status (and whose answer it was), or a transport failure's message, which names only
      // its kind: a problem's detail that varies (a request id) logs nothing more.
      kind = refusal?.status === undefined ? why : `HTTP ${refusal.status}${refusal.fromRoute ? ' route' : ''}`
    }
    // Each kind once: a refused credential is not hidden behind an earlier "missing", and the
    // set stays as small as the kinds a wait meets.
    if (!this.#logged.has(kind)) {
      this.#logged.add(kind)
      this.#log(`CRUD areas: waiting for the backend's agent areas (${why}); reading over SQL until it lists them`)
    }
    return false
  }

  async #poll(delay: number): Promise<void> {
    if (this.#stopped || (await this.refresh()) || this.#stopped) return
    this.#timer = setTimeout(() => void this.#poll(Math.min(delay * 2, this.#maxMs)), delay)
    this.#timer.unref()
  }
}
