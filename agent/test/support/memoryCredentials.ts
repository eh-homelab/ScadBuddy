import { randomUUID } from 'node:crypto'
import {
  type Credential,
  CredentialError,
  type CredentialEvent,
  type CredentialRepo,
  type CredentialStatus,
  type CredentialTransition,
  type CredentialUpdate,
  credentialAad,
  LEGACY_FORMAT_MESSAGE,
  normaliseBaseUrl,
  normaliseCredentialName,
  MAX_CREDENTIALS,
  type OpenerStatus,
  STALE_ORDER_MESSAGE,
  TOO_MANY_MESSAGE,
  type StoredCredential,
  type CredentialKind,
  type CredentialScope,
  agentUses,
} from '../../src/credentials.js'
import { type Envelope, type Kek, last4, openSecret, SEAL_V1, SealError, sealedVersion, sealSecret } from '../../src/secrets.js'

// The sealing the API does (backend agent_data/credentials.py), for the in-memory store below.

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

/**
 * A row's scope after `update` (#7983), as the API sets it: the one asked, else the stored one,
 * else `agent`. A `claude_login` is the fleet's only: its scope is `fleet`, and the API refuses
 * any other (422; 400 here, as `CredentialError` has no 422).
 */
export function scopeOf(update: CredentialUpdate, current: Pick<StoredCredential, 'scope'> | undefined): CredentialScope {
  if (update.kind === 'claude_login') {
    if (update.scope !== undefined && update.scope !== 'fleet') {
      throw new CredentialError('kind "claude_login" is the fleet\'s only: its scope must be "fleet"', 400)
    }
    return 'fleet'
  }
  return update.scope ?? current?.scope ?? 'agent'
}

type Sealed = { id: string; kind: CredentialKind; base_url: string | null; envelope: Envelope }

/** A stored row's plaintext, of any kind. Throws SealError on a wrong KEK, an altered row, or a v1 row. */
export function openSealed(kek: Kek, row: Sealed): string {
  if (sealedVersion(row.envelope.secretSealed) === SEAL_V1) throw new SealError(LEGACY_FORMAT_MESSAGE)
  return openSecret(kek, row.envelope, credentialAad(row.id, row.kind, row.base_url))
}

/** Decrypts a stored row as the agent runs it. Throws SealError as `openSealed` does, and for a `claude_login`. */
export function openCredential(kek: Kek, row: Sealed): Credential {
  const secret = openSealed(kek, row)
  if (row.kind === 'claude_login') throw new SealError("a claude_login is the fleet's: the agent does not run it")
  if (row.kind === 'gateway') {
    if (row.base_url === null) throw new SealError('gateway credential has no base_url')
    return { kind: 'gateway', baseUrl: row.base_url, secret }
  }
  return { kind: row.kind, secret }
}


type MemoryRow = Omit<StoredCredential, 'legacyFormat' | 'status' | 'cooldown_until' | 'priority'> & {
  envelope: Envelope
  status: CredentialStatus
  cooldownUntil: number | null
}

/**
 * The API's credential store in memory: the same validation (`planPut`), sealing under
 * `kek` (the API's key; undefined is an API with none) and `openCredential`, and the same
 * health rules (an expired cooldown reads as active; disabled is not downgraded by a rate
 * limit; a stale epoch is ignored).
 */
export class MemoryCredentials implements CredentialRepo {
  /** The API's key: it seals new secrets and opens stored ones. */
  kek: Kek | undefined
  /** Why the API has no key, when `kek` is undefined. */
  readonly noKeyReason: string

  constructor(kek?: Kek, noKeyReason = 'SCADBUDDY_SECRET_KEY_FILE is not set') {
    this.kek = kek
    this.noKeyReason = noKeyReason
  }

  opener(): Promise<OpenerStatus> {
    return Promise.resolve(this.kek ? { ok: true, kek: this.kek } : { ok: false, reason: this.noKeyReason })
  }

  /** `create`, sealed under `other`: a credential saved under another key. */
  async createUnder(other: Kek, update: CredentialUpdate): Promise<StoredCredential> {
    const own = this.kek
    this.kek = other
    try {
      return await this.create(update)
    } finally {
      this.kek = own
    }
  }

  /** `put`, sealed under `other`. */
  async putUnder(other: Kek, update: CredentialUpdate, id?: string): Promise<StoredCredential> {
    const own = this.kek
    this.kek = other
    try {
      return await this.put(update, id)
    } finally {
      this.kek = own
    }
  }

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

  /** The rows the agent's reveal reads, as the API's: never a fleet row (#7983). */
  private get agentsOwn(): MemoryRow[] {
    return this.rows.filter(agentUses)
  }

  reveal(id?: string): Promise<Credential | undefined> {
    const row = id === undefined ? this.agentsOwn[0] : this.agentsOwn.find((r) => r.id === id)
    if (!row) return Promise.resolve(undefined)
    try {
      if (!this.kek) throw new SealError('no key-encryption key is mounted to open the credential')
      return Promise.resolve(openCredential(this.kek, row))
    } catch (err) {
      return Promise.reject(err as Error)
    }
  }

  async revealAll(): Promise<{ stored: StoredCredential; credential: Credential | undefined }[]> {
    await this.read()
    const kek = this.kek
    return this.agentsOwn.map((row) => {
      let credential: Credential | undefined
      try {
        credential = kek ? openCredential(kek, row) : undefined
      } catch {
        credential = undefined
      }
      return { stored: this.view(row, this.rows.indexOf(row)), credential }
    })
  }

  async put(update: CredentialUpdate, id?: string): Promise<StoredCredential> {
    const row = this.find(id)
    if (!row) {
      if (id !== undefined) throw new CredentialError(`no credential ${id}`, 404)
      return this.create(update)
    }
    const current = await this.get(row.id)
    const plan = planPut(update, current, this.kek, row.id)
    const scope = scopeOf(update, current)
    if ('keep' in plan) {
      row.scope = scope
      return (await this.get(row.id)) as StoredCredential
    }
    const { kind, baseUrl, envelope, last4 } = plan.write
    Object.assign(row, {
      scope,
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
      epoch: row.epoch + 1,
    })
    return (await this.get(row.id)) as StoredCredential
  }

  async create(update: CredentialUpdate): Promise<StoredCredential> {
    if (update.secret === undefined) throw new CredentialError('secret is required for a new credential', 400)
    if (this.rows.length >= MAX_CREDENTIALS) throw new CredentialError(TOO_MANY_MESSAGE, 409)
    const id = randomUUID()
    const name = normaliseCredentialName(update.name)
    const plan = planPut(update, undefined, this.kek, id)
    if ('keep' in plan) throw new Error('planPut kept a credential that does not exist')
    const { kind, baseUrl, envelope, last4 } = plan.write
    this.rows.push({
      id,
      scope: scopeOf(update, undefined),
      account_uuid: null,
      organization_uuid: null,
      subscription_type: null,
      rate_limit_tier: null,
      email: kind === 'claude_login' ? (update.email ?? null) : null,
      leased_to: null,
      leased_at: null,
      lease_epoch: 0,
      kind,
      name,
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
    Object.assign(row, { status: 'active', cooldownUntil: null, last_error: null, last_error_at: null, epoch: row.epoch + 1 })
    return this.get(id)
  }

  async rename(id: string, name: string | null): Promise<StoredCredential | undefined> {
    const row = this.find(id)
    if (!row) return undefined
    row.name = normaliseCredentialName(name)
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
          row.cooldownUntil =
            event.replacing !== undefined && row.cooldownUntil === event.replacing.getTime()
              ? until
              : before === 'cooling_down'
                ? Math.max(row.cooldownUntil ?? until, until)
                : until
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
