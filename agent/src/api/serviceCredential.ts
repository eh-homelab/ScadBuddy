import type { Sql } from 'postgres'
import type { Middleware } from 'openapi-fetch'
import { type Kek, openSecret } from '../secrets.js'

// #4975 phase 4 (spec 2026-11-01 §7.1, §7.2): the agent's service credential on every call
// to the backend: the openapi-fetch client (`serviceCredentialMiddleware`) and the workspace
// blob routes' plain fetches (`withServiceCredential`). The backend issues and rotates it; the agent reads the newest live one
// from `service_credentials_agent` (the view its own Postgres role alone is granted, when the
// deployment made one), unseals it under the KEK, and reads again every hour. Without the KEK
// or a credential it sends none, which `SCADBUDDY_AUTH_MODE=off` still serves.

export const REREAD_MS = 60 * 60 * 1000
/** How soon it reads again while it has no token (review of #5211): started before the API minted one, or a failed read. */
export const RETRY_MS = 60 * 1000

/** The AAD binding a sealed token to its row (`backend/scadbuddy/auth/service_credentials.py` `aad`). */
export function credentialAad(id: string, caller: string, scopes: readonly string[]): string {
  return `service_credentials|${id}|${caller}|${[...scopes].sort().join(',')}`
}

type Row = {
  id: string
  caller: string
  scopes: string[]
  secret_sealed: Buffer
  dek_sealed: Buffer
  kek_id: string
}

export class ServiceCredential {
  private token: string | undefined
  private readAt: number | undefined
  private reading: Promise<string | undefined> | undefined

  private readonly sql: Sql
  private readonly kek: Kek | undefined
  private readonly now: () => number
  private readonly log: (message: string) => void

  constructor(
    sql: Sql,
    kek: Kek | undefined,
    now: () => number = Date.now,
    log: (message: string) => void = (message) => console.error(message),
  ) {
    this.sql = sql
    this.kek = kek
    this.now = now
    this.log = log
  }

  async current(): Promise<string | undefined> {
    if (this.kek === undefined) return undefined
    const wait = this.token === undefined ? RETRY_MS : REREAD_MS
    if (this.readAt !== undefined && this.now() - this.readAt < wait) return this.token
    this.reading ??= this.read(this.kek).finally(() => {
      this.reading = undefined
    })
    return this.reading
  }

  private async read(kek: Kek): Promise<string | undefined> {
    try {
      const rows = await this.sql<Row[]>`
        SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id
        FROM service_credentials_agent ORDER BY expires_at DESC LIMIT 1`
      const row = rows[0]
      this.token =
        row === undefined
          ? undefined
          : openSecret(
              kek,
              { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
              credentialAad(row.id, row.caller, row.scopes),
            )
    } catch (error) {
      // Keep the last token: it stays good through the seven days' overlap.
      this.log(`could not read the agent's service credential (${(error as Error).name})`)
    }
    this.readAt = this.now()
    return this.token
  }
}

/**
 * `fetchImpl` with the bearer added to requests under `baseUrl` (the backend) only, so a
 * presigned transfer URL elsewhere never sees it.
 */
export function withServiceCredential(fetchImpl: typeof fetch, credential: ServiceCredential, baseUrl: string): typeof fetch {
  const base = baseUrl.replace(/\/+$/, '') + '/'
  return async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.startsWith(base)) return fetchImpl(input, init)
    const token = await credential.current()
    if (token === undefined) return fetchImpl(input, init)
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
    if (!headers.has('authorization')) headers.set('authorization', `Bearer ${token}`)
    return fetchImpl(input, { ...init, headers })
  }
}

/** `Authorization: Bearer <token>` on every backend call, when there is one. */
export function serviceCredentialMiddleware(credential: ServiceCredential): Middleware {
  return {
    async onRequest({ request }) {
      const token = await credential.current()
      if (token !== undefined && !request.headers.has('authorization')) {
        request.headers.set('authorization', `Bearer ${token}`)
      }
      return request
    },
  }
}
