import { randomUUID } from 'node:crypto'
import {
  type Credential,
  CredentialError,
  type CredentialEvent,
  type CredentialRepo,
  type CredentialStatus,
  type CredentialTransition,
  type CredentialUpdate,
  openCredential,
  planPut,
  STALE_ORDER_MESSAGE,
  type StoredCredential,
} from '../../src/credentials.js'
import { type Envelope, type Kek, SEAL_V1, sealedVersion } from '../../src/secrets.js'

type MemoryRow = Omit<StoredCredential, 'legacyFormat' | 'status' | 'cooldown_until' | 'priority'> & {
  envelope: Envelope
  status: CredentialStatus
  cooldownUntil: number | null
}

/**
 * CredentialStore's behaviour without Postgres: the same `planPut` validation,
 * the same sealing and the same `openCredential`, and the same health rules
 * (an expired cooldown reads as active; disabled is not downgraded by a rate
 * limit; a stale epoch is ignored), kept in memory. The Postgres store itself
 * is covered by test/pg.test.ts.
 */
export class MemoryCredentials implements CredentialRepo {
  /** In priority order. */
  rows: MemoryRow[] = []
  failing = false
  /** When set, reads wait on it (a database that hangs). */
  hang: Promise<void> | undefined
  /** The clock the cooldowns are read against. */
  now: () => number = Date.now

  /** The first credential by priority (the single-credential tests). */
  get row(): MemoryRow | undefined {
    return this.rows[0]
  }

  private view(row: MemoryRow, priority: number): StoredCredential {
    const { envelope, cooldownUntil, status, ...rest } = row
    const cooling = status === 'cooling_down' && cooldownUntil !== null && cooldownUntil > this.now()
    return {
      ...rest,
      priority,
      status: status === 'cooling_down' && !cooling ? 'active' : status,
      cooldown_until: cooling ? new Date(cooldownUntil).toISOString() : null,
      legacyFormat: sealedVersion(envelope.secretSealed) === SEAL_V1,
    }
  }

  private async read(): Promise<void> {
    if (this.hang) await this.hang
    if (this.failing) throw new Error('connection refused')
  }

  private find(id: string | undefined): MemoryRow | undefined {
    return id === undefined ? this.rows[0] : this.rows.find((r) => r.id === id)
  }

  async list(): Promise<StoredCredential[]> {
    await this.read()
    return this.rows.map((r, i) => this.view(r, i))
  }

  async get(id?: string): Promise<StoredCredential | undefined> {
    await this.read()
    const row = this.find(id)
    return row ? this.view(row, this.rows.indexOf(row)) : undefined
  }

  reveal(kek: Kek, id?: string): Promise<Credential | undefined> {
    const row = this.find(id)
    if (!row) return Promise.resolve(undefined)
    try {
      return Promise.resolve(openCredential(kek, row))
    } catch (err) {
      return Promise.reject(err as Error)
    }
  }

  async put(update: CredentialUpdate, kek: Kek | undefined, id?: string): Promise<StoredCredential> {
    const row = this.find(id)
    if (!row) {
      if (id !== undefined) throw new CredentialError(`no credential ${id}`, 404)
      return this.create(update, kek)
    }
    const plan = planPut(update, await this.get(row.id), kek, row.id)
    if ('keep' in plan) return plan.keep
    const { kind, baseUrl, envelope, last4 } = plan.write
    Object.assign(row, {
      kind,
      base_url: baseUrl,
      last4,
      updated_at: new Date().toISOString(),
      kekId: envelope.kekId,
      envelope,
      status: 'active',
      cooldownUntil: null,
      epoch: row.epoch + 1,
    })
    return (await this.get(row.id)) as StoredCredential
  }

  async create(update: CredentialUpdate, kek: Kek | undefined): Promise<StoredCredential> {
    if (update.secret === undefined) throw new CredentialError('secret is required for a new credential', 400)
    const id = randomUUID()
    const plan = planPut(update, undefined, kek, id)
    if ('keep' in plan) throw new Error('planPut kept a credential that does not exist')
    const { kind, baseUrl, envelope, last4 } = plan.write
    this.rows.push({
      id,
      kind,
      base_url: baseUrl,
      last4,
      updated_at: new Date().toISOString(),
      kekId: envelope.kekId,
      envelope,
      status: 'active',
      cooldownUntil: null,
      last_error: null,
      last_error_at: null,
      last_used_at: null,
      epoch: 0,
    })
    return (await this.get(id)) as StoredCredential
  }

  delete(id?: string): Promise<boolean> {
    const row = this.find(id)
    if (!row) return Promise.resolve(false)
    this.rows = this.rows.filter((r) => r !== row)
    return Promise.resolve(true)
  }

  async reorder(ids: readonly string[]): Promise<StoredCredential[]> {
    const wanted = new Set(ids)
    if (wanted.size !== ids.length || wanted.size !== this.rows.length || this.rows.some((r) => !wanted.has(r.id))) {
      throw new CredentialError(STALE_ORDER_MESSAGE, 409)
    }
    this.rows = ids.map((id) => this.rows.find((r) => r.id === id) as MemoryRow)
    return this.list()
  }

  async reset(id: string): Promise<StoredCredential | undefined> {
    const row = this.find(id)
    if (!row) return undefined
    Object.assign(row, { status: 'active', cooldownUntil: null, epoch: row.epoch + 1 })
    return this.get(id)
  }

  async record(id: string, epoch: number, event: CredentialEvent): Promise<CredentialTransition | undefined> {
    const row = this.rows.find((r) => r.id === id && r.epoch === epoch)
    if (!row) return undefined
    const raw = row.status
    const before = (await this.get(id))?.status as CredentialStatus
    const at = new Date().toISOString()
    switch (event.kind) {
      case 'used':
        row.last_used_at = at
        if (before === 'active') Object.assign(row, { status: 'active', cooldownUntil: null })
        break
      case 'disabled':
        Object.assign(row, { status: 'disabled', cooldownUntil: null, last_error: event.reason, last_error_at: at })
        break
      case 'cooling_down':
        if (row.status !== 'disabled') {
          const until = event.until.getTime()
          row.cooldownUntil = before === 'cooling_down' ? Math.max(row.cooldownUntil ?? until, until) : until
          row.status = 'cooling_down'
        }
        Object.assign(row, { last_error: event.reason, last_error_at: at })
        break
      case 'transient':
        Object.assign(row, { last_error: event.reason, last_error_at: at })
        break
    }
    return { before, after: row.status, recovered: raw === 'cooling_down' && row.status === 'active' }
  }
}
