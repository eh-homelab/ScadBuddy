import type { Sql } from 'postgres'
import { type Envelope, type Kek, last4, openSecret, sealSecret } from './secrets.js'

// The Claude credential (issue #255, spec D2 and §9): an Anthropic API key, or a
// gateway base URL plus the gateway's credential. Stored sealed in
// `ai_credentials` (db/migrations.ts); only `kind`, `base_url` and the last four
// characters are ever read back out through a route.

export const CREDENTIAL_KINDS = ['anthropic_api_key', 'gateway'] as const
export type CredentialKind = (typeof CREDENTIAL_KINDS)[number]

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

/** The summary plus which key-encryption key sealed it; for health, never for a route body. */
export type StoredCredential = CredentialSummary & { kekId: string }

/** The store as the routes and health see it; tests substitute an in-memory one. */
export type CredentialRepo = {
  get(): Promise<StoredCredential | undefined>
  reveal(kek: Kek): Promise<Credential | undefined>
  put(update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential>
  delete(): Promise<boolean>
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
  readonly status: 400 | 409 | 503
  constructor(message: string, status: 400 | 409 | 503) {
    super(message)
    this.status = status
  }
}

const ROW_ID = 'default'
/** AAD binding the sealed values to this table and row (secrets.ts). */
export const CREDENTIAL_AAD = `ai_credentials:${ROW_ID}`

type Row = {
  kind: CredentialKind
  base_url: string | null
  secret_sealed: Buffer
  dek_sealed: Buffer
  kek_id: string
  last4: string
  updated_at: Date
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

function stored(row: Pick<Row, 'kind' | 'base_url' | 'last4' | 'updated_at' | 'kek_id'>): StoredCredential {
  return {
    kind: row.kind,
    base_url: row.base_url,
    last4: row.last4,
    updated_at: row.updated_at.toISOString(),
    kekId: row.kek_id,
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
 * Validates a save and seals the new secret. A request without `secret` keeps
 * the stored one, but only when it changes neither `kind` nor `base_url`:
 * pointing an existing secret at a different host would send it somewhere its
 * owner never entered it for, so that needs the secret typed in again.
 * `current` is only consulted when `secret` is omitted.
 */
export function planPut(
  update: CredentialUpdate,
  current: StoredCredential | undefined,
  kek: Kek | undefined,
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
    write: { kind: update.kind, baseUrl, envelope: sealSecret(kek, secret, CREDENTIAL_AAD), last4: last4(secret) },
  }
}

export class CredentialStore implements CredentialRepo {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  async get(): Promise<StoredCredential | undefined> {
    const [row] = await this.sql<Row[]>`
      SELECT kind, base_url, last4, updated_at, kek_id FROM ai_credentials WHERE id = ${ROW_ID}`
    return row ? stored(row) : undefined
  }

  /** Decrypts the stored credential for one query. Throws SealError on a wrong KEK or altered row. */
  async reveal(kek: Kek): Promise<Credential | undefined> {
    const [row] = await this.sql<Row[]>`
      SELECT kind, base_url, secret_sealed, dek_sealed, kek_id FROM ai_credentials WHERE id = ${ROW_ID}`
    if (!row) return undefined
    const envelope: Envelope = { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id }
    const secret = openSecret(kek, envelope, CREDENTIAL_AAD)
    return row.kind === 'gateway'
      ? { kind: 'gateway', baseUrl: row.base_url ?? '', secret }
      : { kind: 'anthropic_api_key', secret }
  }

  async put(update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential> {
    const current = update.secret === undefined ? await this.get() : undefined
    const plan = planPut(update, current, kek)
    if ('keep' in plan) return plan.keep
    const { kind, baseUrl, envelope, last4: tail } = plan.write
    const [row] = await this.sql<Row[]>`
      INSERT INTO ai_credentials (id, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
      VALUES (${ROW_ID}, ${kind}, ${baseUrl}, ${envelope.secretSealed}, ${envelope.dekSealed},
              ${envelope.kekId}, ${tail})
      ON CONFLICT (id) DO UPDATE SET
        kind = EXCLUDED.kind, base_url = EXCLUDED.base_url,
        secret_sealed = EXCLUDED.secret_sealed, dek_sealed = EXCLUDED.dek_sealed,
        kek_id = EXCLUDED.kek_id, last4 = EXCLUDED.last4, updated_at = now()
      RETURNING kind, base_url, last4, updated_at, kek_id`
    if (!row) throw new Error('INSERT ... RETURNING returned no row')
    return stored(row)
  }

  async delete(): Promise<boolean> {
    const rows = await this.sql`DELETE FROM ai_credentials WHERE id = ${ROW_ID}`
    return rows.count > 0
  }
}

/** Non-secret AI settings in `ai_settings`. */
export class SettingsStore {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  async get<T>(key: string): Promise<T | undefined> {
    const [row] = await this.sql<{ value: T }[]>`SELECT value FROM ai_settings WHERE key = ${key}`
    return row?.value
  }

  async set(key: string, value: unknown): Promise<void> {
    await this.sql`
      INSERT INTO ai_settings (key, value) VALUES (${key}, ${this.sql.json(value as never)})
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
  }
}
