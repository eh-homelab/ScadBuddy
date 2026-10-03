import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { type AuditContext, type AuditSink, SYSTEM_ACTOR } from './audit/log.js'
import {
  type Envelope,
  type Kek,
  type KekStatus,
  last4,
  openSecret,
  rewrap,
  SEAL_V1,
  SealError,
  sealedVersion,
  sealSecret,
} from './secrets.js'

// The Claude credentials (issue #255, spec D2 and §9; several since #1093):
// each an Anthropic API key, or a gateway base URL plus the gateway's
// credential. Stored sealed in `ai_credentials` (db/migrations/), one row
// each, in priority order: a query uses the first one that is usable and
// falls back to the next (harness/fallback.ts). Only `kind`, `base_url`, the
// last four characters and the row's health are ever read back out through a
// route.
//
// Health (#1093): `status` is `active`, `cooling_down` (rate limited until
// `cooldown_until`) or `disabled` (refused for good until a person resets it
// or saves a new secret). A cooldown that has passed reads back as `active`
// (computed in SQL, on the database's clock, so every pod agrees) without
// anything having to write it back. Every state write is one conditional
// UPDATE, so pods that race each keep the strongest verdict: `disabled` is
// never downgraded by a rate limit, and of two cooldowns the later wins.

export const CREDENTIAL_KINDS = ['anthropic_api_key', 'gateway'] as const
export type CredentialKind = (typeof CREDENTIAL_KINDS)[number]

export const CREDENTIAL_STATUSES = ['active', 'cooling_down', 'disabled'] as const
export type CredentialStatus = (typeof CREDENTIAL_STATUSES)[number]

/** A usable credential, decrypted. Lives only for the length of one query's set-up. */
export type Credential =
  | { kind: 'anthropic_api_key'; secret: string }
  | { kind: 'gateway'; baseUrl: string; secret: string }

/** What routes may return. */
export type CredentialSummary = {
  kind: CredentialKind
  base_url: string | null
  last4: string
  updated_at: string
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
}

/** What a query learned about a credential (harness/fallback.ts). */
export type CredentialEvent =
  | { kind: 'used' }
  | { kind: 'disabled'; reason: string }
  | { kind: 'cooling_down'; until: Date; reason: string }
  | { kind: 'transient'; reason: string }

/** The status before and after a recorded event; `recovered` when an expired cooldown was cleared by a success. */
export type CredentialTransition = { before: CredentialStatus; after: CredentialStatus; recovered: boolean }

/** The store as the routes, health and the harness see it; tests substitute an in-memory one. */
export type CredentialRepo = {
  /** Every credential, in priority order. */
  list(): Promise<StoredCredential[]>
  /** One credential; the first by priority when `id` is omitted (the single-credential routes). */
  get(id?: string): Promise<StoredCredential | undefined>
  reveal(kek: Kek, id?: string): Promise<Credential | undefined>
  /**
   * Saves `update` over a credential (the first by priority when `id` is
   * omitted; one is created when there is none). A new secret makes it
   * `active` again. Throws CredentialError 404 for an unknown `id`.
   */
  put(update: CredentialUpdate, kek: Kek | undefined, id?: string): Promise<StoredCredential>
  /** A new credential, last in priority. `secret` is required. */
  create(update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential>
  /** Deletes a credential (the first by priority when `id` is omitted); the rest close up. */
  delete(id?: string): Promise<boolean>
  /** Puts the credentials in this order; `ids` must name every one exactly once (else CredentialError 409). */
  reorder(ids: readonly string[]): Promise<StoredCredential[]>
  /** Makes a credential `active` again, ending a cooldown or a disable. */
  reset(id: string): Promise<StoredCredential | undefined>
  /** Records what a query learned; ignored (undefined) when the row is gone or its epoch moved on. */
  record(id: string, epoch: number, event: CredentialEvent): Promise<CredentialTransition | undefined>
}

export type CredentialUpdate = {
  kind: CredentialKind
  base_url?: string | null
  /** Omitted: keep the stored secret (see `put`). */
  secret?: string
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

/** The id the single credential had before #1093; that row keeps it. */
export const LEGACY_ROW_ID = 'default'

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

type Row = {
  id: string
  priority: number
  kind: CredentialKind
  base_url: string | null
  secret_sealed: Buffer
  dek_sealed: Buffer
  kek_id: string
  last4: string
  updated_at: Date
  seal_version: number
  status: CredentialStatus
  cooldown_until: Date | null
  last_error: string | null
  last_error_at: Date | null
  last_used_at: Date | null
  epoch: number
}

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
  if (url.search || url.hash) {
    throw new CredentialError('base_url must not have a query or fragment', 400)
  }
  return url.toString().replace(/\/+$/, '')
}

const iso = (d: Date | null): string | null => (d ? d.toISOString() : null)

function stored(row: Omit<Row, 'secret_sealed' | 'dek_sealed'>): StoredCredential {
  return {
    id: row.id,
    priority: row.priority,
    kind: row.kind,
    base_url: row.base_url,
    last4: row.last4,
    updated_at: row.updated_at.toISOString(),
    status: row.status,
    cooldown_until: iso(row.cooldown_until),
    last_error: row.last_error,
    last_error_at: iso(row.last_error_at),
    last_used_at: iso(row.last_used_at),
    epoch: row.epoch,
    kekId: row.kek_id,
    legacyFormat: row.seal_version === SEAL_V1,
  }
}

export type PutPlan =
  | { keep: StoredCredential }
  | {
      write: {
        kind: CredentialKind
        baseUrl: string | null
        envelope: Envelope
        last4: string
      }
    }

/**
 * Validates a save of row `id` and seals the new secret. A request without
 * `secret` keeps the stored one, but only when it changes neither `kind` nor
 * `base_url`: pointing an existing secret at a different host would send it
 * somewhere its owner never entered it for, so that needs the secret typed in
 * again. `current` is only consulted when `secret` is omitted.
 */
export function planPut(
  update: CredentialUpdate,
  current: StoredCredential | undefined,
  kek: Kek | undefined,
  id: string,
): PutPlan {
  let baseUrl: string | null = null
  if (update.kind === 'gateway') {
    if (!update.base_url) throw new CredentialError('kind "gateway" needs base_url', 400)
    baseUrl = normaliseBaseUrl(update.base_url)
  } else if (update.base_url) {
    throw new CredentialError('base_url applies to kind "gateway" only', 400)
  }

  if (update.secret === undefined) {
    if (!current) throw new CredentialError('no credential is stored yet; secret is required', 400)
    if (current.kind !== update.kind || current.base_url !== baseUrl) {
      throw new CredentialError(
        'changing kind or base_url needs the secret again: the stored one is not sent to a new destination',
        409,
      )
    }
    if (current.legacyFormat) throw new CredentialError(LEGACY_FORMAT_MESSAGE, 409)
    return { keep: current }
  }

  const secret = update.secret.trim()
  if (!secret) throw new CredentialError('secret is empty', 400)
  if (/\s/.test(secret)) throw new CredentialError('secret must not contain whitespace', 400)
  if (!kek) {
    throw new CredentialError(
      'credentials cannot be saved: no key-encryption key is configured (SCADBUDDY_SECRET_KEY_FILE, spec §9)',
      503,
    )
  }
  return {
    write: {
      kind: update.kind,
      baseUrl,
      envelope: sealSecret(kek, secret, credentialAad(id, update.kind, baseUrl)),
      last4: last4(secret),
    },
  }
}

/** Decrypts a stored row. Throws SealError on a wrong KEK, an altered row, or a v1 row. */
export function openCredential(
  kek: Kek,
  row: { id: string; kind: CredentialKind; base_url: string | null; envelope: Envelope },
): Credential {
  if (sealedVersion(row.envelope.secretSealed) === SEAL_V1) throw new SealError(LEGACY_FORMAT_MESSAGE)
  const secret = openSecret(kek, row.envelope, credentialAad(row.id, row.kind, row.base_url))
  if (row.kind === 'gateway') {
    if (row.base_url === null) throw new SealError('gateway credential has no base_url')
    return { kind: 'gateway', baseUrl: row.base_url, secret }
  }
  return { kind: 'anthropic_api_key', secret }
}

/** For logs and audit: which credential, never its secret. */
export function credentialLabel(c: Pick<StoredCredential, 'priority' | 'kind' | 'base_url' | 'last4'>): string {
  const what = c.kind === 'gateway' ? `gateway ${c.base_url ?? ''}` : 'API key'
  return `credential ${c.priority + 1} (${what}${c.last4 ? ` …${c.last4}` : ''})`
}

/** Whether the mounted key opens `c`: sealed under it, in the current format. */
export function opensWith(c: StoredCredential, kek: KekStatus): boolean {
  return kek.ok && !c.legacyFormat && c.kekId === kek.kek.id
}

/** The soonest a cooling-down credential that the mounted key opens is usable again. */
export function soonestRecovery(list: readonly StoredCredential[], kek: KekStatus): Date | undefined {
  const times = list
    .filter((c) => c.status === 'cooling_down' && c.cooldown_until !== null && opensWith(c, kek))
    .map((c) => Date.parse(c.cooldown_until as string))
  return times.length ? new Date(Math.min(...times)) : undefined
}

/** Why no credential is usable, one clause per credential. */
export function describeUnusable(list: readonly StoredCredential[], kek: KekStatus): string {
  if (list.length === 0) return 'no Claude credential is configured'
  const clauses = list.map((c) => {
    const name = credentialLabel(c)
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

export type RewrapResult = { rewrapped: number; failed: number }

type Db = Sql | TransactionSql

/** The table's own lock, against other structural writers (create, reorder, delete); readers are not blocked. */
async function lockForWrite(tx: TransactionSql): Promise<void> {
  await tx`LOCK TABLE ai_credentials IN SHARE ROW EXCLUSIVE MODE`
}

export class CredentialStore implements CredentialRepo {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  /** Rows as of the database's clock: an expired cooldown reads as `active`. */
  private async rows(db: Db, id?: string): Promise<StoredCredential[]> {
    const rows = await db<Omit<Row, 'secret_sealed' | 'dek_sealed'>[]>`
      SELECT id, priority, kind, base_url, last4, updated_at, kek_id,
             get_byte(secret_sealed, 0) AS seal_version,
             CASE WHEN status = 'cooling_down' AND cooldown_until <= now() THEN 'active' ELSE status END AS status,
             CASE WHEN status = 'cooling_down' AND cooldown_until > now() THEN cooldown_until END AS cooldown_until,
             last_error, last_error_at, last_used_at, epoch
      FROM ai_credentials
      ${id === undefined ? db`` : db`WHERE id = ${id}`}
      ORDER BY priority`
    return rows.map(stored)
  }

  list(): Promise<StoredCredential[]> {
    return this.rows(this.sql)
  }

  async get(id?: string): Promise<StoredCredential | undefined> {
    return (await this.rows(this.sql, id))[0]
  }

  async reveal(kek: Kek, id?: string): Promise<Credential | undefined> {
    const [row] = await this.sql<Row[]>`
      SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id FROM ai_credentials
      ${id === undefined ? this.sql`` : this.sql`WHERE id = ${id}`}
      ORDER BY priority LIMIT 1`
    if (!row) return undefined
    return openCredential(kek, {
      id: row.id,
      kind: row.kind,
      base_url: row.base_url,
      envelope: { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
    })
  }

  async put(update: CredentialUpdate, kek: Kek | undefined, id?: string): Promise<StoredCredential> {
    return await this.sql.begin(async (tx) => {
      await lockForWrite(tx)
      const [current] = await this.rows(tx, id)
      if (!current) {
        if (id !== undefined) throw new CredentialError(`no credential ${id}`, 404)
        return await this.insert(tx, update, kek)
      }
      const plan = planPut(update, current, kek, current.id)
      if ('keep' in plan) return plan.keep
      const { kind, baseUrl, envelope, last4: tail } = plan.write
      await tx`
        UPDATE ai_credentials SET
          kind = ${kind}, base_url = ${baseUrl},
          secret_sealed = ${envelope.secretSealed}, dek_sealed = ${envelope.dekSealed},
          kek_id = ${envelope.kekId}, last4 = ${tail}, updated_at = now(),
          status = 'active', cooldown_until = NULL, last_error = NULL, last_error_at = NULL, epoch = epoch + 1
        WHERE id = ${current.id}`
      return await this.one(tx, current.id)
    })
  }

  async create(update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential> {
    if (update.secret === undefined) throw new CredentialError('secret is required for a new credential', 400)
    return await this.sql.begin(async (tx) => {
      await lockForWrite(tx)
      return await this.insert(tx, update, kek)
    })
  }

  private async insert(tx: TransactionSql, update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential> {
    const id = randomUUID()
    const plan = planPut(update, undefined, kek, id)
    if ('keep' in plan) throw new Error('planPut kept a credential that does not exist')
    const { kind, baseUrl, envelope, last4: tail } = plan.write
    await tx`
      INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
      VALUES (${id}, (SELECT COALESCE(MAX(priority) + 1, 0) FROM ai_credentials), ${kind}, ${baseUrl},
              ${envelope.secretSealed}, ${envelope.dekSealed}, ${envelope.kekId}, ${tail})`
    return await this.one(tx, id)
  }

  private async one(db: Db, id: string): Promise<StoredCredential> {
    const [row] = await this.rows(db, id)
    if (!row) throw new Error(`credential ${id} vanished inside its own transaction`)
    return row
  }

  async delete(id?: string): Promise<boolean> {
    return await this.sql.begin(async (tx) => {
      await lockForWrite(tx)
      const deleted = await tx`
        DELETE FROM ai_credentials
        WHERE id = ${id === undefined ? tx`(SELECT id FROM ai_credentials ORDER BY priority LIMIT 1)` : id}`
      if (deleted.count === 0) return false
      await tx`
        UPDATE ai_credentials c SET priority = n.rn - 1
        FROM (SELECT id, row_number() OVER (ORDER BY priority) AS rn FROM ai_credentials) n
        WHERE c.id = n.id AND c.priority <> n.rn - 1`
      return true
    })
  }

  async reorder(ids: readonly string[]): Promise<StoredCredential[]> {
    return await this.sql.begin(async (tx) => {
      await lockForWrite(tx)
      const current = await tx<{ id: string }[]>`SELECT id FROM ai_credentials`
      const wanted = new Set(ids)
      if (wanted.size !== ids.length || wanted.size !== current.length || current.some((r) => !wanted.has(r.id))) {
        throw new CredentialError(STALE_ORDER_MESSAGE, 409)
      }
      await tx`
        UPDATE ai_credentials c SET priority = v.ord - 1
        FROM unnest(${tx.array([...ids])}::text[]) WITH ORDINALITY AS v(id, ord)
        WHERE c.id = v.id`
      return await this.rows(tx)
    })
  }

  async reset(id: string): Promise<StoredCredential | undefined> {
    const updated = await this.sql`
      UPDATE ai_credentials SET status = 'active', cooldown_until = NULL, last_error = NULL, last_error_at = NULL,
        epoch = epoch + 1
      WHERE id = ${id}`
    return updated.count === 0 ? undefined : await this.get(id)
  }

  async record(id: string, epoch: number, event: CredentialEvent): Promise<CredentialTransition | undefined> {
    // `before` locks the row, so the CASEs below and the transition returned
    // see the same values even when several pods report at once.
    const sql = this.sql
    const before = sql`
      SELECT id, status AS raw,
             CASE WHEN status = 'cooling_down' AND cooldown_until <= now() THEN 'active' ELSE status END AS status
      FROM ai_credentials WHERE id = ${id} AND epoch = ${epoch} FOR UPDATE`
    let rows: { before: CredentialStatus; raw: CredentialStatus; after: CredentialStatus }[]
    switch (event.kind) {
      case 'used':
        rows = await sql`
          WITH b AS (${before})
          UPDATE ai_credentials c SET
            last_used_at = now(),
            status = CASE WHEN b.status = 'active' THEN 'active' ELSE c.status END,
            cooldown_until = CASE WHEN b.status = 'active' THEN NULL ELSE c.cooldown_until END
          FROM b WHERE c.id = b.id
          RETURNING b.status AS before, b.raw AS raw, c.status AS after`
        break
      case 'disabled':
        rows = await sql`
          WITH b AS (${before})
          UPDATE ai_credentials c SET
            status = 'disabled', cooldown_until = NULL, last_error = ${event.reason}, last_error_at = now()
          FROM b WHERE c.id = b.id
          RETURNING b.status AS before, b.raw AS raw, c.status AS after`
        break
      case 'cooling_down':
        rows = await sql`
          WITH b AS (${before})
          UPDATE ai_credentials c SET
            status = CASE WHEN c.status = 'disabled' THEN 'disabled' ELSE 'cooling_down' END,
            cooldown_until = CASE WHEN c.status = 'disabled' THEN NULL
              ELSE GREATEST(${event.until}::timestamptz, CASE WHEN b.status = 'cooling_down' THEN c.cooldown_until END) END,
            last_error = ${event.reason}, last_error_at = now()
          FROM b WHERE c.id = b.id
          RETURNING b.status AS before, b.raw AS raw, c.status AS after`
        break
      case 'transient':
        rows = await sql`
          WITH b AS (${before})
          UPDATE ai_credentials c SET last_error = ${event.reason}, last_error_at = now()
          FROM b WHERE c.id = b.id
          RETURNING b.status AS before, b.raw AS raw, c.status AS after`
        break
    }
    const [row] = rows
    if (!row) return undefined
    return { before: row.before, after: row.after, recovered: row.raw === 'cooling_down' && row.after === 'active' }
  }

  /**
   * Key rotation (spec §9, "Rotating it re-wraps the data keys only"): every
   * row whose data key is sealed under `previous` gets it re-sealed under
   * `current`. The sealed secret, and `updated_at`, are left as they are. A
   * row that `previous` cannot open (altered, or v1) is left for the operator
   * and counted in `failed`; the update is conditional on `kek_id` so two pods
   * rotating at once do not overwrite each other's work.
   */
  async rewrapFrom(previous: Kek, current: Kek): Promise<RewrapResult> {
    const result: RewrapResult = { rewrapped: 0, failed: 0 }
    if (previous.id === current.id) return result
    const rows = await this.sql<Row[]>`
      SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id FROM ai_credentials WHERE kek_id = ${previous.id}`
    for (const row of rows) {
      let next: Envelope
      try {
        if (sealedVersion(row.secret_sealed) === SEAL_V1) throw new SealError(LEGACY_FORMAT_MESSAGE)
        next = rewrap(
          previous,
          current,
          { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
          credentialAad(row.id, row.kind, row.base_url),
        )
      } catch (err) {
        if (err instanceof SealError) {
          result.failed++
          continue
        }
        throw err
      }
      const updated = await this.sql`
        UPDATE ai_credentials SET dek_sealed = ${next.dekSealed}, kek_id = ${next.kekId}
        WHERE id = ${row.id} AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    }
    return result
  }
}

/**
 * Non-secret AI settings in `ai_settings`. Every write is recorded in the
 * audit log when one is given (#258: settings writes are audited), with the
 * key and the new value: the table holds no secrets by contract (its
 * migration: "Never put a secret here"), and the value is capped.
 */
export class SettingsStore {
  private readonly sql: Sql
  private readonly audit: AuditSink | undefined
  constructor(sql: Sql, audit?: AuditSink) {
    this.sql = sql
    this.audit = audit
  }

  async get<T>(key: string): Promise<T | undefined> {
    const [row] = await this.sql<{ value: T }[]>`SELECT value FROM ai_settings WHERE key = ${key}`
    return row?.value
  }

  /** `context` says who wrote it; ScadBuddy itself when omitted. */
  async set(key: string, value: unknown, context?: AuditContext): Promise<void> {
    const startedAt = new Date()
    let failure: unknown
    try {
      await this.sql`
        INSERT INTO ai_settings (key, value) VALUES (${key}, ${this.sql.json(value as never)})
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
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
   * With `check`, a compare-and-set: the table is locked against other writers
   * (readers are not blocked), `check` reads the current values inside the
   * transaction, and nothing is written unless it returns true. Answers
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
    return await this.sql.begin(async (tx) => {
      if (check) {
        await tx`LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE`
        const current = {
          get: async <T>(key: string): Promise<T | undefined> => {
            const [row] = await tx<{ value: T }[]>`SELECT value FROM ai_settings WHERE key = ${key}`
            return row?.value
          },
        }
        if (!(await check(current))) return false
      }
      for (const [key, value] of Object.entries(values)) {
        await tx`
          INSERT INTO ai_settings (key, value) VALUES (${key}, ${tx.json(value as never)})
          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
      }
      return true
    })
  }
}
