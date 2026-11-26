import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import {
  type Credential,
  credentialAad,
  CredentialError,
  type CredentialEvent,
  type CredentialKind,
  type CredentialRepo,
  type CredentialScope,
  type CredentialStatus,
  type CredentialTransition,
  type CredentialUpdate,
  LEGACY_FORMAT_MESSAGE,
  MAX_CREDENTIALS,
  normaliseCredentialName,
  type OpenerStatus,
  opensWith,
  STALE_ORDER_MESSAGE,
  type StoredCredential,
  TOO_MANY_MESSAGE,
} from '../../../src/credentials.js'
import { type Envelope, type Kek, rewrap, SEAL_V1, SealError, sealedVersion } from '../../../src/secrets.js'
import { openCredential, planPut, scopeOf } from '../memoryCredentials.js'

type Row = {
  id: string
  priority: number
  kind: CredentialKind
  name: string | null
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
  scope: CredentialScope
  account_uuid: string | null
  organization_uuid: string | null
  subscription_type: string | null
  rate_limit_tier: string | null
  email: string | null
  leased_to: string | null
  leased_at: Date | null
  lease_epoch: string | number
}

const iso = (d: Date | null): string | null => (d ? d.toISOString() : null)

function stored(row: Omit<Row, 'secret_sealed' | 'dek_sealed'>): StoredCredential {
  return {
    id: row.id,
    priority: row.priority,
    kind: row.kind,
    name: row.name,
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
    scope: row.scope,
    account_uuid: row.account_uuid,
    organization_uuid: row.organization_uuid,
    subscription_type: row.subscription_type,
    rate_limit_tier: row.rate_limit_tier,
    email: row.email,
    leased_to: row.leased_to,
    leased_at: iso(row.leased_at),
    lease_epoch: Number(row.lease_epoch),
  }
}

type Db = Sql | TransactionSql

/** The table's own lock, against other structural writers (create, reorder, delete); readers are not blocked. */
async function lockForWrite(tx: TransactionSql): Promise<void> {
  await tx`LOCK TABLE ai_credentials IN SHARE ROW EXCLUSIVE MODE`
}

/** `openCredential`, or undefined for a row the key does not open (altered, wrong key, v1). */
function tryOpen(kek: Kek, row: Pick<Row, 'id' | 'kind' | 'base_url' | 'secret_sealed' | 'dek_sealed' | 'kek_id'>): Credential | undefined {
  try {
    return openCredential(kek, {
      id: row.id,
      kind: row.kind,
      base_url: row.base_url,
      envelope: { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
    })
  } catch (err) {
    if (err instanceof SealError) return undefined
    throw err
  }
}

/**
 * `ai_credentials` over SQL on the test database, sealed under `kek` (the API's key): what the
 * API's `credentials` routes run (backend api/crud_agent_secrets.py), as the agent once ran them
 * itself. `kek` undefined is an API with no key.
 */
export class SqlCredentials implements CredentialRepo {
  private readonly sql: Sql
  readonly kek: Kek | undefined
  constructor(sql: Sql, kek?: Kek) {
    this.sql = sql
    this.kek = kek
  }

  opener(): Promise<OpenerStatus> {
    return Promise.resolve(this.kek ? { ok: true, kek: this.kek } : { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' })
  }

  /** Rows as of the database's clock: an expired cooldown reads as `active`. */
  private async rows(db: Db, id?: string): Promise<StoredCredential[]> {
    const rows = await db<Omit<Row, 'secret_sealed' | 'dek_sealed'>[]>`
      SELECT id, priority, kind, name, base_url, last4, updated_at, kek_id,
             get_byte(secret_sealed, 0) AS seal_version,
             CASE WHEN status = 'cooling_down' AND cooldown_until <= now() THEN 'active' ELSE status END AS status,
             CASE WHEN status = 'cooling_down' AND cooldown_until > now() THEN cooldown_until END AS cooldown_until,
             last_error, last_error_at, last_used_at, epoch, scope, account_uuid, organization_uuid,
             subscription_type, rate_limit_tier, email, leased_to, leased_at, lease_epoch
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

  async reveal(id?: string): Promise<Credential | undefined> {
    const kek = this.kek
    const [row] = await this.sql<Row[]>`
      SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id FROM ai_credentials
      WHERE scope IN ('agent', 'both') AND kind <> 'claude_login'
      ${id === undefined ? this.sql`` : this.sql`AND id = ${id}`}
      ORDER BY priority LIMIT 1`
    if (!row) return undefined
    if (!kek) throw new SealError('no key-encryption key is mounted to open the credential')
    return openCredential(kek, {
      id: row.id,
      kind: row.kind,
      base_url: row.base_url,
      envelope: { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
    })
  }

  async revealAll(): Promise<{ stored: StoredCredential; credential: Credential | undefined }[]> {
    const kek = this.kek
    const rows = await this.sql<Row[]>`
      SELECT id, priority, kind, name, base_url, last4, updated_at, kek_id, secret_sealed, dek_sealed,
             get_byte(secret_sealed, 0) AS seal_version,
             CASE WHEN status = 'cooling_down' AND cooldown_until <= now() THEN 'active' ELSE status END AS status,
             CASE WHEN status = 'cooling_down' AND cooldown_until > now() THEN cooldown_until END AS cooldown_until,
             last_error, last_error_at, last_used_at, epoch, scope, account_uuid, organization_uuid,
             subscription_type, rate_limit_tier, email, leased_to, leased_at, lease_epoch
      FROM ai_credentials WHERE scope IN ('agent', 'both') AND kind <> 'claude_login' ORDER BY priority`
    return rows.map((row) => {
      const view = stored(row)
      return { stored: view, credential: kek && opensWith(view, { ok: true, kek }) ? tryOpen(kek, row) : undefined }
    })
  }

  async put(update: CredentialUpdate, id?: string): Promise<StoredCredential> {
    const kek = this.kek
    return await this.sql.begin(async (tx) => {
      await lockForWrite(tx)
      const [current] = await this.rows(tx, id)
      if (!current) {
        if (id !== undefined) throw new CredentialError(`no credential ${id}`, 404)
        return await this.insert(tx, update, kek)
      }
      const plan = planPut(update, current, kek, current.id)
      const scope = scopeOf(update, current)
      if ('keep' in plan) {
        if (scope === current.scope) return plan.keep
        await tx`UPDATE ai_credentials SET scope = ${scope} WHERE id = ${current.id}`
        await tx`SELECT pg_notify('scadbuddy_warm_evict', '*')`
        return await this.one(tx, current.id)
      }
      const { kind, baseUrl, envelope, last4: tail } = plan.write
      await tx`
        UPDATE ai_credentials SET
          kind = ${kind}, base_url = ${baseUrl}, scope = ${scope},
          secret_sealed = ${envelope.secretSealed}, dek_sealed = ${envelope.dekSealed},
          kek_id = ${envelope.kekId}, last4 = ${tail}, updated_at = now(),
          status = 'active', cooldown_until = NULL, last_error = NULL, last_error_at = NULL, epoch = epoch + 1
        WHERE id = ${current.id}`
      // Every warm Claude Code holds a credential in its env (spec 2026-10-13 §6.7): the
      // caches are told on commit (sessions/warmEvict.ts), as by every write below.
      await tx`SELECT pg_notify('scadbuddy_warm_evict', '*')`
      return await this.one(tx, current.id)
    })
  }

  async create(update: CredentialUpdate): Promise<StoredCredential> {
    const kek = this.kek
    if (update.secret === undefined) throw new CredentialError('secret is required for a new credential', 400)
    return await this.sql.begin(async (tx) => {
      await lockForWrite(tx)
      return await this.insert(tx, update, kek)
    })
  }

  private async insert(tx: TransactionSql, update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential> {
    // Under the table lock (lockForWrite), so racing creates cannot pass the cap together.
    const [count] = await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_credentials`
    if ((count?.n ?? 0) >= MAX_CREDENTIALS) throw new CredentialError(TOO_MANY_MESSAGE, 409)
    const id = randomUUID()
    const name = normaliseCredentialName(update.name)
    const plan = planPut(update, undefined, kek, id)
    if ('keep' in plan) throw new Error('planPut kept a credential that does not exist')
    const { kind, baseUrl, envelope, last4: tail } = plan.write
    await tx`
      INSERT INTO ai_credentials (id, priority, kind, name, base_url, secret_sealed, dek_sealed, kek_id, last4, scope)
      VALUES (${id}, (SELECT COALESCE(MAX(priority) + 1, 0) FROM ai_credentials), ${kind}, ${name}, ${baseUrl},
              ${envelope.secretSealed}, ${envelope.dekSealed}, ${envelope.kekId}, ${tail}, ${scopeOf(update, undefined)})`
    await tx`SELECT pg_notify('scadbuddy_warm_evict', '*')`
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
      await tx`SELECT pg_notify('scadbuddy_warm_evict', '*')`
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
      await tx`SELECT pg_notify('scadbuddy_warm_evict', '*')`
      return await this.rows(tx)
    })
  }

  async reset(id: string): Promise<StoredCredential | undefined> {
    const updated = await this.sql`
      UPDATE ai_credentials SET status = 'active', cooldown_until = NULL, last_error = NULL, last_error_at = NULL,
        epoch = epoch + 1
      WHERE id = ${id}`
    if (updated.count === 0) return undefined
    await this.sql`SELECT pg_notify('scadbuddy_warm_evict', '*')`
    return await this.get(id)
  }

  async rename(id: string, name: string | null): Promise<StoredCredential | undefined> {
    // No warm eviction: a Claude Code process never sees the name.
    const updated = await this.sql`UPDATE ai_credentials SET name = ${normaliseCredentialName(name)} WHERE id = ${id}`
    if (updated.count === 0) return undefined
    return await this.get(id)
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
              WHEN ${event.replacing ?? null}::timestamptz IS NOT NULL AND c.cooldown_until = ${event.replacing ?? null}::timestamptz
                THEN ${event.until}::timestamptz
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
   * Key rotation (spec §9, "Rotating it re-wraps the data keys only"), as the API runs it: every
   * row whose data key is sealed under `previous` gets it re-sealed under
   * `current`. The sealed secret, and `updated_at`, are left as they are. A
   * row that `previous` cannot open (altered, or v1) is left for the operator
   * and counted in `failed`; the update is conditional on `kek_id` so two pods
   * rotating at once do not overwrite each other's work.
   */
  async rewrapFrom(previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
    const result = { rewrapped: 0, failed: 0 }
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

