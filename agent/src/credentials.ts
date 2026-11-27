import { CrudReadError, crudRead } from './api/crudRead.js'
import { type AuditContext, type AuditSink, SYSTEM_ACTOR } from './audit/log.js'
import { isRuntimeKey } from './sessions/runtimeSettings.js'
import { evictsWarmOnWrite } from './sessions/warmEvict.js'
import { SealError } from './secrets.js'

// The Claude credentials (issue #255, spec D2 and §9; several since #1093):
// each an Anthropic API key, a Claude Code OAuth token (`claude setup-token`),
// or a gateway base URL plus the gateway's credential. Stored sealed in `ai_credentials` on the
// API, which seals and opens them with its KEK (#5299 D5), one row each, in priority order: a query uses the first one that is usable and
// falls back to the next (harness/fallback.ts). Only `kind`, `base_url`, the
// last four characters, the optional name (#3710) and the row's health are ever
// read back out through a route.
//
// Health (#1093): `status` is `active`, `cooling_down` (rate limited until
// `cooldown_until`) or `disabled` (refused for good until a person resets it
// or saves a new secret). A cooldown that has passed reads back as `active`
// (on the database's clock, so every pod agrees) without anything having to
// write it back. The API runs every state write as one conditional UPDATE, so
// pods that race each keep the strongest verdict: `disabled` is
// never downgraded by a rate limit, and of two cooldowns the later wins.

// #7983: the pool is shared with the fleet's login rotator (design
// 2026-11-26-fleet-session-registry-and-rotator-design.md §6.1). A `claude_login` row holds a
// whole Claude Code login (`claudeAiOauth`), which the agent cannot run; `scope` says who may use
// a row: `agent`, `fleet` or `both`; a `claude_login` is `fleet` only. The API's reveal answers
// the agent its own rows only (scope `agent` or `both`), in its SQL: that is the boundary. The
// agent's own `agentUses` filter is a second, defensive check. Settings lists every row, with
// its scope.

export const CREDENTIAL_KINDS = ['anthropic_api_key', 'claude_login', 'claude_oauth_token', 'gateway'] as const
export type CredentialKind = (typeof CREDENTIAL_KINDS)[number]

/** The kinds the agent runs a turn on, and the kinds Settings saves. */
export const AGENT_CREDENTIAL_KINDS = ['anthropic_api_key', 'claude_oauth_token', 'gateway'] as const
export type AgentCredentialKind = (typeof AGENT_CREDENTIAL_KINDS)[number]

export const CREDENTIAL_SCOPES = ['agent', 'fleet', 'both'] as const
export type CredentialScope = (typeof CREDENTIAL_SCOPES)[number]

/** Whether the agent may use the row: a kind it runs, in scope `agent` or `both`. */
export function agentUses(c: Pick<StoredCredential, 'kind' | 'scope'>): boolean {
  return (AGENT_CREDENTIAL_KINDS as readonly string[]).includes(c.kind) && c.scope !== 'fleet'
}

export const CREDENTIAL_STATUSES = ['active', 'cooling_down', 'disabled'] as const
export type CredentialStatus = (typeof CREDENTIAL_STATUSES)[number]

/** A usable credential, decrypted. Lives only for the length of one query's set-up. */
export type Credential =
  | { kind: 'anthropic_api_key' | 'claude_oauth_token'; secret: string }
  | { kind: 'gateway'; baseUrl: string; secret: string }

/** What routes may return. */
export type CredentialSummary = {
  kind: CredentialKind
  /** The person's label for it (#3710), or null; see `normaliseCredentialName`. */
  name: string | null
  base_url: string | null
  last4: string
  updated_at: string
  /** Who may use it (#7983); see `agentUses`. */
  scope: CredentialScope
  /** A `claude_login`'s account, in clear (#7983); null on every other kind. */
  account_uuid: string | null
  organization_uuid: string | null
  subscription_type: string | null
  rate_limit_tier: string | null
  email: string | null
}

/**
 * One stored credential: the summary, its place and health, plus which
 * key-encryption key sealed it and in which format (for health and rotation,
 * never for a route body as such).
 */
export type StoredCredential = CredentialSummary & {
  id: string
  /** 0 is tried first. */
  priority: number
  /** As of the database's clock: a cooldown that has passed is `active`. */
  status: CredentialStatus
  /** Set only while `status` is `cooling_down`. */
  cooldown_until: string | null
  /** Why it was last refused, redacted of the secret; cleared by a reset and by a new secret. */
  last_error: string | null
  last_error_at: string | null
  last_used_at: string | null
  /** Bumped by a new secret and by a reset; failures are recorded against the epoch a query read. */
  epoch: number
  kekId: string
  /** True when sealed by #354's v1 format, which did not bind kind and base_url; it will not open. */
  legacyFormat: boolean
  /** The machine that holds a `claude_login` now, and since when (#7983); null when none does. */
  leased_to: string | null
  leased_at: string | null
  lease_epoch: number
}

/** What a query learned about a credential (harness/fallback.ts). */
export type CredentialEvent =
  | { kind: 'used' }
  | { kind: 'disabled'; reason: string }
  /**
   * `replacing`: the provisional time an earlier report of this same rate
   * limit recorded; if the row still holds it, `until` replaces it outright
   * (it may be sooner), otherwise the later of the two wins as usual.
   */
  | { kind: 'cooling_down'; until: Date; reason: string; replacing?: Date }
  | { kind: 'transient'; reason: string }

/** The status before and after a recorded event; `recovered` when an expired cooldown was cleared by a success. */
export type CredentialTransition = { before: CredentialStatus; after: CredentialStatus; recovered: boolean }

/** The API's key that opens the stored credentials, by its id (#5299 D5); the agent holds no KEK. */
export type OpenerStatus = { ok: true; kek: { readonly id: string } } | { ok: false; reason: string }

/** What a plaintext is revealed for, on the API's audit row (#5299 D5). */
export type RevealPurpose = 'turn' | 'redact' | 'test'
/** The turn a reveal is for, which the API's audit row names (each a UUID; anything else is left out). */
export type RevealIds = { sessionId?: string | undefined; turnId?: string | undefined }

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function wireIds(ids: RevealIds): { session_id?: string; turn_id?: string } {
  return {
    ...(ids.sessionId && UUID_RE.test(ids.sessionId) ? { session_id: ids.sessionId } : {}),
    ...(ids.turnId && UUID_RE.test(ids.turnId) ? { turn_id: ids.turnId } : {}),
  }
}

/** The store as the routes, health and the harness see it; tests substitute an in-memory one. */
export type CredentialRepo = {
  /** Every credential, in priority order. */
  list(): Promise<StoredCredential[]>
  /** One credential; the first the agent uses when `id` is omitted (the single-credential routes). */
  get(id?: string): Promise<StoredCredential | undefined>
  /** The credential decrypted by the API, which audits the reveal (`purpose`). */
  reveal(id?: string, purpose?: RevealPurpose, ids?: RevealIds): Promise<Credential | undefined>
  /**
   * Every credential in priority order, with its secret decrypted where the
   * key opens it (undefined where it does not, every one without a key), all
   * from one read: each row's `epoch` is that of the secret beside it.
   */
  revealAll(purpose?: RevealPurpose, ids?: RevealIds): Promise<{ stored: StoredCredential; credential: Credential | undefined }[]>
  /** Who opens the secrets: the API's key. */
  opener(): Promise<OpenerStatus>
  /**
   * Saves `update` over a credential (the first the agent uses when `id` is
   * omitted; one is created when there is none). A new secret makes it
   * `active` again. Throws CredentialError 404 for an unknown `id`, and 409 for a
   * `claude_login` or a fleet row (#7983): the API changes the agent's rows only.
   */
  put(update: CredentialUpdate, id?: string): Promise<StoredCredential>
  /** A new credential, last in priority. `secret` is required; refused (409) past MAX_CREDENTIALS. */
  create(update: CredentialUpdate): Promise<StoredCredential>
  /** Deletes a credential (the first the agent uses when `id` is omitted); the rest close up. 409 for a fleet row. */
  delete(id?: string): Promise<boolean>
  /**
   * Puts the agent's credentials in this order; `ids` must name every row the agent uses exactly
   * once and no fleet row (else CredentialError 409). The fleet's rows keep their priorities.
   */
  reorder(ids: readonly string[]): Promise<StoredCredential[]>
  /** Makes a credential `active` again, ending a cooldown or a disable. 409 for a fleet row. */
  reset(id: string): Promise<StoredCredential | undefined>
  /**
   * Sets or clears a credential's name (#3710), and nothing else: the secret, its health and
   * its epoch stay. Undefined when there is no such credential; CredentialError 400 for a name
   * `normaliseCredentialName` refuses.
   */
  rename(id: string, name: string | null): Promise<StoredCredential | undefined>
  /** Records what a query learned; ignored (undefined) when the row is gone or its epoch moved on. */
  record(id: string, epoch: number, event: CredentialEvent): Promise<CredentialTransition | undefined>
}

export type CredentialUpdate = {
  kind: CredentialKind
  base_url?: string | null
  /** Omitted: keep the stored secret (see `put`). */
  secret?: string
  /** Read by `create` only; `rename` changes the name of a stored one (#3710). */
  name?: string | null
  /** Omitted: keep the stored scope (a new row: `agent`, or `fleet` for a `claude_login`). */
  scope?: CredentialScope
  /** A `claude_login`'s account email; omitted keeps the stored one. */
  email?: string | null
}

/** A request the store refuses; `status` is the HTTP status the route answers with. */
export class CredentialError extends Error {
  override name = 'CredentialError'
  readonly status: 400 | 404 | 409 | 503
  constructor(message: string, status: 400 | 404 | 409 | 503) {
    super(message)
    this.status = status
  }
}

/** The most credentials stored at once; the order route accepts no more ids than this. */
export const MAX_CREDENTIALS = 100

export const TOO_MANY_MESSAGE = `at most ${MAX_CREDENTIALS} Claude credentials can be stored; delete one first`

/** The most characters (code points) a credential's name has, after trimming (#3710). */
export const CREDENTIAL_NAME_MAX = 60

/**
 * A credential's name as stored (#3710): trimmed, and null when nothing is left. It is
 * shown only in text for the person (`credentialDisplayLabel`, quoted as data). It never
 * goes into a log, an audit row or a prompt (`credentialLabel` leaves it out). It is refused (CredentialError 400) past
 * CREDENTIAL_NAME_MAX characters, or with a control, format or line/paragraph separator
 * character (\p{Cc}, \p{Cf}, \p{Zl}, \p{Zp}): a name is one line of plain text, and a bidi
 * override could make a row read as another. The table's check repeats the length, trim
 * and control-character rules.
 */
export function normaliseCredentialName(raw: string | null | undefined): string | null {
  const name = (raw ?? '').trim()
  if (!name) return null
  if ([...name].length > CREDENTIAL_NAME_MAX) {
    throw new CredentialError(`name must be at most ${CREDENTIAL_NAME_MAX} characters`, 400)
  }
  if (/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(name)) {
    throw new CredentialError('name must be one line of plain text, with no control or formatting characters', 400)
  }
  return name
}

/**
 * AAD binding the sealed values to this table and row AND to the columns that
 * decide where the secret is sent: `kind` and the normalised `base_url`. With
 * those outside the AAD, anyone able to write the table (but without the KEK)
 * could re-point `base_url` and have the next query deliver the token to
 * their host; now the edited row fails authentication instead. JSON-encoded so
 * no base URL can forge a field boundary. The row id is in it too, so a sealed
 * secret copied onto another row (say, one with a higher priority) does not
 * open there.
 *
 * NO LEGACY FALLBACK. Rows sealed by #354 (format v1, AAD `ai_credentials:default`)
 * do not open with this AAD, and there is deliberately no fallback to the old
 * one: a fallback would keep the unbound form valid for exactly the rows an
 * attacker would target. #354 had merged only just before this change and no
 * deployment ran the agent (README "The agent sidecar": "Nothing deploys it
 * yet"), so no real row can exist; a development database that has one reports
 * it (`legacyFormat`) and the secret is entered again.
 */
export function credentialAad(id: string, kind: CredentialKind, baseUrl: string | null): string {
  return `ai_credentials:${id}:${JSON.stringify({ kind, base_url: baseUrl })}`
}

export const LEGACY_FORMAT_MESSAGE =
  'the stored credential was saved in an older format that did not bind its kind and base URL; save it again'

/** Normalises a gateway base URL: http(s) only, no credentials, query or fragment, no trailing slash. */
export function normaliseBaseUrl(raw: string): string {
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    throw new CredentialError('base_url is not a valid URL', 400)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new CredentialError(`base_url must be http(s), not ${url.protocol}//`, 400)
  }
  if (url.username || url.password) {
    throw new CredentialError('base_url must not carry credentials; put the token in secret', 400)
  }
  // A bare `?` or `#` leaves `search` and `hash` empty but stays in the URL: refused too, as the
  // API refuses any `?` or `#` (backend agent_data/credentials.py `check_base_url`).
  if (url.search || url.hash || url.href.includes('?') || url.href.includes('#')) {
    throw new CredentialError('base_url must not have a query or fragment', 400)
  }
  return url.toString().replace(/\/+$/, '')
}

type Labelled = Pick<StoredCredential, 'priority' | 'kind' | 'base_url' | 'last4'>

/** The kind and the last four characters: "API key …abcd". */
function credentialWhat(c: Labelled): string {
  const what =
    c.kind === 'gateway'
      ? `gateway ${c.base_url ?? ''}`
      : c.kind === 'claude_oauth_token'
        ? 'OAuth token'
        : c.kind === 'claude_login'
          ? 'Claude login'
          : 'API key'
  return `${what}${c.last4 ? ` …${c.last4}` : ''}`
}

/** For logs, audit rows and the model: which credential, never its secret or its name. */
export function credentialLabel(c: Labelled): string {
  return `credential ${c.priority + 1} (${credentialWhat(c)})`
}

/**
 * For text the person sees only (#3710): `credentialLabel` with the credential's name, as a
 * JSON string so it reads as quoted data whatever it holds. Never for a log, an audit row or
 * anything sent to the model. Without a name it is `credentialLabel`.
 */
export function credentialDisplayLabel(c: Labelled & Pick<StoredCredential, 'name'>): string {
  if (!c.name) return credentialLabel(c)
  return `credential ${c.priority + 1} ${JSON.stringify(c.name)} (${credentialWhat(c)})`
}

/** Whether the key that opens the credentials (`OpenerStatus`) opens `c`: sealed under it, in the current format. */
export function opensWith(c: StoredCredential, kek: OpenerStatus): boolean {
  return kek.ok && !c.legacyFormat && c.kekId === kek.kek.id
}

/** The soonest a cooling-down credential that the mounted key opens is usable again. */
export function soonestRecovery(list: readonly StoredCredential[], kek: OpenerStatus): Date | undefined {
  const times = list
    .filter((c) => c.status === 'cooling_down' && c.cooldown_until !== null && opensWith(c, kek))
    .map((c) => Date.parse(c.cooldown_until as string))
  return times.length ? new Date(Math.min(...times)) : undefined
}

/**
 * Why no credential is usable, one clause per credential. `label` names each one:
 * `credentialLabel` by default; `credentialDisplayLabel` for text only the person sees.
 */
export function describeUnusable(
  list: readonly StoredCredential[],
  kek: OpenerStatus,
  label: (c: StoredCredential) => string = credentialLabel,
): string {
  if (list.length === 0) return 'no Claude credential is configured'
  const clauses = list.map((c) => {
    const name = label(c)
    if (!opensWith(c, kek)) return `${name} cannot be opened with the mounted key-encryption key`
    if (c.status === 'disabled') return `${name} is disabled${c.last_error ? ` (${c.last_error})` : ''}`
    if (c.status === 'cooling_down') return `${name} is rate limited until ${c.cooldown_until ?? 'later'}`
    return `${name} could not be opened`
  })
  const soonest = soonestRecovery(list, kek)
  return (
    `no Claude credential is usable: ${clauses.join('; ')}` +
    (soonest ? `. The first is usable again at ${soonest.toISOString()}` : '. A person has to reset one in Settings')
  )
}

/** The order `reorder` refuses: not every credential exactly once. */
export const STALE_ORDER_MESSAGE =
  'the order must name every stored credential exactly once; the list changed since it was read, so reload it'

/**
 * Non-secret AI settings in `ai_settings`. Every write is recorded in the
 * audit log when one is given (#258: settings writes are audited), with the
 * key and the new value: the table holds no secrets by contract (its
 * migration: "Never put a secret here"), and the value is capped.
 */
/** The settings the routes and the session code read and write (`SettingsStore`); `AppDeps.settings`. */
export type SettingsRepo = {
  get<T>(key: string): Promise<T | undefined>
  /** `context` says who wrote it, for the audit row (`SettingsStore.set`). */
  set(key: string, value: unknown, context: AuditContext): Promise<void>
}

/** Where `SettingsStore` reads and writes: the API's CRUD routes, area `settings` (`CrudSettings`). */
export type SettingsRows = Pick<CrudSettings, 'get' | 'write' | 'compareAndSet'>

export class SettingsStore {
  private readonly rows: SettingsRows
  private readonly audit: AuditSink | undefined
  constructor(rows: SettingsRows, audit?: AuditSink) {
    this.rows = rows
    this.audit = audit
  }

  async get<T>(key: string): Promise<T | undefined> {
    return await this.rows.get<T>(key)
  }

  /** `context` says who wrote it; ScadBuddy itself when omitted. */
  async set(key: string, value: unknown, context?: AuditContext): Promise<void> {
    const startedAt = new Date()
    let failure: unknown
    try {
      await this.rows.write({ [key]: value }, null)
    } catch (err) {
      failure = err
    }
    await this.audit?.record({
      kind: 'settings',
      action: key,
      surface: context?.surface ?? 'system',
      actor: context?.actor ?? SYSTEM_ACTOR,
      clientIp: context?.clientIp,
      outcome: failure === undefined ? 'ok' : 'error',
      detail: `${key} = ${JSON.stringify(value) ?? 'undefined'}${failure === undefined ? '' : ` (failed: ${failure instanceof Error ? failure.message : String(failure)})`}`,
      startedAt,
      finishedAt: new Date(),
    })
    if (failure !== undefined) throw failure
  }

  /**
   * Sets several keys in one transaction, so a reader on any replica sees all
   * of them change or none (e.g. the MCP auth mode with its anonymous cap).
   *
   * With `check`, a compare-and-set (`CrudSettings.compareAndSet`): nothing is
   * written unless `check` returns true over the current values. Answers
   * whether the values were written.
   *
   * Each key written gets its own audit row, as `set` writes one; `context`
   * says who wrote them, ScadBuddy itself when omitted. A check that refuses
   * writes nothing and records nothing.
   */
  async setMany(
    values: Record<string, unknown>,
    check?: (current: { get<T>(key: string): Promise<T | undefined> }) => Promise<boolean>,
    context?: AuditContext,
  ): Promise<boolean> {
    const startedAt = new Date()
    let written: boolean
    try {
      written = await this.#setMany(values, check)
    } catch (err) {
      await this.#recordMany(values, context, startedAt, err)
      throw err
    }
    if (written) await this.#recordMany(values, context, startedAt, undefined)
    return written
  }

  async #recordMany(values: Record<string, unknown>, context: AuditContext | undefined, startedAt: Date, failure: unknown): Promise<void> {
    if (!this.audit) return
    const finishedAt = new Date()
    for (const [key, value] of Object.entries(values)) {
      await this.audit.record({
        kind: 'settings',
        action: key,
        surface: context?.surface ?? 'system',
        actor: context?.actor ?? SYSTEM_ACTOR,
        clientIp: context?.clientIp,
        outcome: failure === undefined ? 'ok' : 'error',
        detail: `${key} = ${JSON.stringify(value) ?? 'undefined'}${failure === undefined ? '' : ` (failed: ${failure instanceof Error ? failure.message : String(failure)})`}`,
        startedAt,
        finishedAt,
      })
    }
  }

  async #setMany(
    values: Record<string, unknown>,
    check?: (current: { get<T>(key: string): Promise<T | undefined> }) => Promise<boolean>,
  ): Promise<boolean> {
    return check ? await this.rows.compareAndSet(values, check) : await this.rows.write(values, null)
  }
}

// --- The CRUD routes (#5299 step 4, D5) ------------------------------------------------
//
// The credentials and settings behind the API's CRUD routes (backend api/crud_agent_secrets.py,
// areas `credentials` and `settings`): the API seals and opens with its KEK, so these work with
// no KEK in this process, and runs each write as one transaction, under the table lock, row
// lock and NOTIFYs it needs.

type WireStored = Omit<StoredCredential, 'kekId' | 'legacyFormat'> & { kek_id: string; legacy_format: boolean }
type WireCredential = { kind: CredentialKind; base_url?: string | null; secret: string }

/** #7983's columns as an API from before them answers: every row is the agent's, with no account or lease. */
const BEFORE_SCOPES = {
  scope: 'agent',
  account_uuid: null,
  organization_uuid: null,
  subscription_type: null,
  rate_limit_tier: null,
  email: null,
  leased_to: null,
  leased_at: null,
  lease_epoch: 0,
} as const

function fromWire(row: WireStored): StoredCredential {
  const { kek_id, legacy_format, ...rest } = row
  return { ...BEFORE_SCOPES, ...rest, kekId: kek_id, legacyFormat: legacy_format }
}

/**
 * The credential as the agent runs it. The API's reveal never answers the agent a fleet row
 * (#7983: `scope` `agent` or `both` only, in its SQL), so a `claude_login` never reaches here;
 * should one, it is undefined, never run.
 */
function credentialFromWire(c: WireCredential): Credential | undefined {
  if (c.kind === 'claude_login') return undefined
  if (c.kind === 'gateway') {
    if (!c.base_url) throw new SealError('gateway credential has no base_url')
    return { kind: 'gateway', baseUrl: c.base_url, secret: c.secret }
  }
  return { kind: c.kind, secret: c.secret }
}

/** The statuses a refusal of the credential store carries (`CredentialError`). */
const REFUSAL_STATUSES = new Set([400, 404, 409, 503])

/** A CRUD route's answer, with the store's own refusals as `CredentialError` and a sealed row as `SealError`. */
async function credentialCall<T>(run: () => Promise<T>): Promise<T> {
  try {
    return await run()
  } catch (err) {
    if (err instanceof CrudReadError && err.fromRoute) {
      if (err.reason === 'credential-sealed') throw new SealError(err.detail || 'the credential does not open')
      if (err.reason === 'credential-refused' && err.status !== undefined && REFUSAL_STATUSES.has(err.status)) {
        throw new CredentialError(err.detail, err.status as 400 | 404 | 409 | 503)
      }
      if (err.reason === 'kek-missing') throw new CredentialError(`no key-encryption key on the API: ${err.detail}`, 503)
    }
    throw err
  }
}

/** How long `CrudCredentials` keeps the API's key status: a turn's set-up reads it with every list. */
const OPENER_CACHE_MS = 10_000

/** `ai_credentials` through the API's CRUD routes, area `credentials` (see above). */
export class CrudCredentials implements CredentialRepo {
  readonly #base: string
  readonly #fetch: typeof fetch
  #opener: { at: number; status: OpenerStatus } | undefined

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #call<T>(step: string, path: string, body?: unknown): Promise<T> {
    return credentialCall(() => crudRead<T>(this.#fetch, this.#base, step, path, body === undefined ? {} : { method: 'POST', body }))
  }

  async opener(): Promise<OpenerStatus> {
    if (this.#opener && Date.now() - this.#opener.at < OPENER_CACHE_MS) return this.#opener.status
    const key = await this.#call<{ ok: boolean; kek_id: string | null; reason: string | null }>("the API's key-encryption key", '/credentials/key')
    const status: OpenerStatus =
      key.ok && key.kek_id ? { ok: true, kek: { id: key.kek_id } } : { ok: false, reason: key.reason ?? 'the API has no key-encryption key' }
    this.#opener = { at: Date.now(), status }
    return status
  }

  async list(): Promise<StoredCredential[]> {
    return (await this.#call<{ credentials: WireStored[] }>('the credentials', '/credentials')).credentials.map(fromWire)
  }

  async get(id?: string): Promise<StoredCredential | undefined> {
    const query = id === undefined ? '' : `?${new URLSearchParams({ id }).toString()}`
    const { credential } = await this.#call<{ credential: WireStored | null }>('a credential', `/credentials/one${query}`)
    return credential ? fromWire(credential) : undefined
  }

  async reveal(id?: string, purpose: RevealPurpose = 'test', ids: RevealIds = {}): Promise<Credential | undefined> {
    const { credential } = await this.#call<{ credential: WireCredential | null }>('a credential opened', '/credentials/reveal', {
      ...(id === undefined ? {} : { id }),
      purpose,
      ...wireIds(ids),
    })
    return credential ? credentialFromWire(credential) : undefined
  }

  async revealAll(
    purpose: RevealPurpose = 'turn',
    ids: RevealIds = {},
  ): Promise<{ stored: StoredCredential; credential: Credential | undefined }[]> {
    const { credentials } = await this.#call<{ credentials: { stored: WireStored; credential: WireCredential | null }[] }>(
      'the credentials opened',
      '/credentials/reveal',
      { all: true, purpose, ...wireIds(ids) },
    )
    return credentials.map((row) => ({ stored: fromWire(row.stored), credential: row.credential ? credentialFromWire(row.credential) : undefined }))
  }

  /** A save as the API takes it: the base URL and name as this process normalises them (the AAD binds the URL as spelled here). */
  #fields(update: CredentialUpdate): Record<string, unknown> {
    const baseUrl = update.kind === 'gateway' && update.base_url ? normaliseBaseUrl(update.base_url) : update.base_url
    return {
      kind: update.kind,
      ...(baseUrl ? { base_url: baseUrl } : {}),
      ...(update.secret === undefined ? {} : { secret: update.secret }),
      ...(update.name === undefined ? {} : { name: normaliseCredentialName(update.name) }),
      ...(update.scope === undefined ? {} : { scope: update.scope }),
      ...(update.email === undefined ? {} : { email: update.email ?? '' }),
    }
  }

  async put(update: CredentialUpdate, id?: string): Promise<StoredCredential> {
    const { credential } = await this.#call<{ credential: WireStored }>('a credential saved', '/credentials/save', {
      ...(id === undefined ? {} : { id }),
      update: this.#fields(update),
    })
    return fromWire(credential)
  }

  async create(update: CredentialUpdate): Promise<StoredCredential> {
    if (update.secret === undefined) throw new CredentialError('secret is required for a new credential', 400)
    const { credential } = await this.#call<{ credential: WireStored }>('a credential created', '/credentials', this.#fields(update))
    return fromWire(credential)
  }

  async delete(id?: string): Promise<boolean> {
    return (await this.#call<{ deleted: boolean }>('a credential deleted', '/credentials/delete', id === undefined ? {} : { id })).deleted
  }

  async reorder(ids: readonly string[]): Promise<StoredCredential[]> {
    return (await this.#call<{ credentials: WireStored[] }>('the credentials ordered', '/credentials/order', { ids })).credentials.map(fromWire)
  }

  async reset(id: string): Promise<StoredCredential | undefined> {
    const { credential } = await this.#call<{ credential: WireStored | null }>('a credential reset', '/credentials/reset', { id })
    return credential ? fromWire(credential) : undefined
  }

  async rename(id: string, name: string | null): Promise<StoredCredential | undefined> {
    const { credential } = await this.#call<{ credential: WireStored | null }>('a credential renamed', '/credentials/name', {
      id,
      name: normaliseCredentialName(name),
    })
    return credential ? fromWire(credential) : undefined
  }

  async record(id: string, epoch: number, event: CredentialEvent): Promise<CredentialTransition | undefined> {
    const wire =
      event.kind === 'cooling_down'
        ? { kind: event.kind, reason: event.reason, until: event.until.toISOString(), ...(event.replacing ? { replacing: event.replacing.toISOString() } : {}) }
        : event
    const { transition } = await this.#call<{ transition: CredentialTransition | null }>('what a query learned', '/credentials/events', { id, epoch, event: wire })
    return transition ?? undefined
  }
}

/** A settings key as the agent read it, for a compare-and-set: `present` false for no row. */
type Expected = { key: string; present: boolean; value?: unknown }

/** How many times a compare-and-set reads again after another writer moved a key it read. */
export const SETTINGS_CAS_TRIES = 5
/** The most keys one `GET …/settings` takes (backend api/crud_agent_secrets.py `SETTINGS_MAX`). */
export const SETTINGS_READ_MAX = 50

/** `ai_settings` through the API's CRUD routes, area `settings` (see above). */
export class CrudSettings {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async get<T>(key: string): Promise<T | undefined> {
    return (await this.getMany([key])).get(key) as T | undefined
  }

  /**
   * The keys' values, one `GET …/settings` per SETTINGS_READ_MAX keys (the route's cap); a key
   * with no row is absent from the map.
   */
  async getMany(keys: readonly string[]): Promise<Map<string, unknown>> {
    const found = new Map<string, unknown>()
    const unique = [...new Set(keys)]
    for (let at = 0; at < unique.length; at += SETTINGS_READ_MAX) {
      const params = new URLSearchParams(unique.slice(at, at + SETTINGS_READ_MAX).map((key): [string, string] => ['key', key]))
      const { values } = await crudRead<{ values: Record<string, unknown> }>(this.#fetch, this.#base, 'settings', `/settings?${params.toString()}`)
      for (const [key, value] of Object.entries(values)) found.set(key, value)
    }
    return found
  }

  /**
   * Writes `values` in one transaction on the API, only while each of `expect` still holds (no
   * check with null); whether it wrote. The API sends the NOTIFYs the keys need on commit.
   */
  async write(values: Record<string, unknown>, expect: Expected[] | null): Promise<boolean> {
    const keys = Object.keys(values)
    const notify = { warm_evict: keys.some(evictsWarmOnWrite), runtime: keys.some(isRuntimeKey) }
    const body = { values, ...(expect === null ? {} : { expect }), notify }
    return (await crudRead<{ written: boolean }>(this.#fetch, this.#base, 'settings written', '/settings', { method: 'POST', body })).written
  }

  /**
   * `setMany`'s compare-and-set: `check` runs over values read from the API, and the write goes
   * through only while each value it read still holds, compared under the table's lock on the
   * API. A writer in between means reading again, at most SETTINGS_CAS_TRIES times. Each try
   * reads in one `GET …/settings` the keys it writes and every key an earlier try's check read;
   * a key the check asks for beyond those is one more read, and is in the next try's batch.
   */
  async compareAndSet(
    values: Record<string, unknown>,
    check: (current: { get<T>(key: string): Promise<T | undefined> }) => Promise<boolean>,
  ): Promise<boolean> {
    const wanted = new Set(Object.keys(values))
    for (let attempt = 0; attempt < SETTINGS_CAS_TRIES; attempt++) {
      const batch = await this.getMany([...wanted])
      const fetched = new Set(wanted)
      const read = new Map<string, unknown>()
      const current = {
        get: async <T>(key: string): Promise<T | undefined> => {
          if (!fetched.has(key)) {
            const more = await this.getMany([key])
            if (more.has(key)) batch.set(key, more.get(key))
            fetched.add(key)
            wanted.add(key)
          }
          read.set(key, batch.get(key))
          return batch.get(key) as T | undefined
        },
      }
      if (!(await check(current))) return false
      const expect = [...read].map(([key, value]) => (value === undefined ? { key, present: false } : { key, present: true, value }))
      if (await this.write(values, expect)) return true
    }
    throw new Error(`the settings ${Object.keys(values).join(', ')} kept changing; ${SETTINGS_CAS_TRIES} tries to write them failed`)
  }
}
