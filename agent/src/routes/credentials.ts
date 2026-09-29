import type { Hono } from 'hono'
import { z } from 'zod'
import {
  type Credential,
  CREDENTIAL_KINDS,
  CredentialError,
  type CredentialRepo,
  normaliseBaseUrl,
  type StoredCredential,
} from '../credentials.js'
import type { ConnectionTest } from '../harness/testConnection.js'
import { assertGatewayHostAllowed, EgressError, type Resolver, systemResolver } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import { type KekStatus, SealError } from '../secrets.js'
import { type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/credentials (issue #255). The ingress routes /api/v1/ai/* to this
// service (spec §4.2). No route ever returns the secret: reads give `kind`,
// `base_url` and the last four characters (spec §8.6, "never returned by any
// route"). Error bodies use `{ detail }`, the backend's FastAPI shape, so the
// frontend handles both services' errors the same way.

export type CredentialRouteDeps = {
  /** Undefined when there is no database (spec §9, "No database"). */
  credentials: CredentialRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  kek: KekStatus
  testConnection: (credential: Credential) => Promise<ConnectionTest>
  remoteAddress: RemoteAddress
  /** Which origins may write (src/http/origins.ts). */
  origins: OriginPolicy
  /** Resolves a gateway host for the SSRF check (src/http/egress.ts); the system resolver by default. */
  resolveHost?: Resolver
  /** Minimum time between the end of one connection test and the start of the next. */
  testCooldownMs?: number
  /** Clock, for tests. */
  now?: () => number
}

export type CredentialView = {
  configured: boolean
  kind: StoredCredential['kind'] | null
  base_url: string | null
  last4: string | null
  updated_at: string | null
  /** False when the stored secret was sealed with a key other than the mounted one, or in the old format. */
  usable: boolean
  /** Whether a PUT with a secret can succeed, and if not, why. */
  can_save: boolean
  cannot_save_reason: string | null
}

const PutBody = z.strictObject({
  kind: z.enum(CREDENTIAL_KINDS),
  base_url: z.string().max(2048).nullable().optional(),
  secret: z.string().min(1).max(4096).optional(),
})

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
export const DEFAULT_TEST_COOLDOWN_MS = 10_000

export function view(stored: StoredCredential | undefined, kek: KekStatus): CredentialView {
  return {
    configured: stored !== undefined,
    kind: stored?.kind ?? null,
    base_url: stored?.base_url ?? null,
    last4: stored?.last4 ?? null,
    updated_at: stored?.updated_at ?? null,
    usable: stored !== undefined && !stored.legacyFormat && kek.ok && kek.kek.id === stored.kekId,
    can_save: kek.ok,
    // `reason` is the public one (no file path or errno; secrets.ts `loadKek`).
    cannot_save_reason: kek.ok ? null : `no key-encryption key: ${kek.reason}`,
  }
}

export function registerCredentialRoutes(app: Hono, deps: CredentialRouteDeps): void {
  const base = '/api/v1/ai/credentials'
  const resolveHost = deps.resolveHost ?? systemResolver
  const cooldownMs = deps.testCooldownMs ?? DEFAULT_TEST_COOLDOWN_MS
  const now = deps.now ?? Date.now

  /** The store once migrations are current, or a 503 response. */
  async function store(): Promise<CredentialRepo | string> {
    if (!deps.credentials) return NO_DATABASE
    return (await deps.ready()) ? deps.credentials : NOT_READY
  }

  app.get(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    return c.json(view(await repo.get(), deps.kek))
  })

  // Every write is outward tier; see guard.ts for what is and is not checked.
  app.on(['PUT', 'DELETE', 'POST'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress)
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })

  app.put(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    let body: z.infer<typeof PutBody>
    try {
      body = PutBody.parse(await c.req.json())
    } catch (err) {
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    try {
      // A gateway host that is (or resolves to) link-local or cloud metadata
      // is refused before anything is stored (src/http/egress.ts).
      if (body.kind === 'gateway' && body.base_url) {
        await assertGatewayHostAllowed(normaliseBaseUrl(body.base_url), resolveHost)
      }
      const saved = await repo.put(
        {
          kind: body.kind,
          ...(body.base_url === undefined ? {} : { base_url: body.base_url }),
          ...(body.secret === undefined ? {} : { secret: body.secret }),
        },
        deps.kek.ok ? deps.kek.kek : undefined,
      )
      return c.json(view(saved, deps.kek))
    } catch (err) {
      if (err instanceof CredentialError) return c.json({ detail: err.message }, err.status)
      if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
      throw err
    }
  })

  app.delete(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    await repo.delete()
    return c.json(view(undefined, deps.kek))
  })

  // One connection test at a time, and at most one per cooldown: each test
  // starts a Claude Code process and spends real tokens.
  let testing = false
  let lastTestEnded = Number.NEGATIVE_INFINITY

  app.post(`${base}/test`, async (c) => {
    const waitMs = testing ? cooldownMs : lastTestEnded + cooldownMs - now()
    if (waitMs > 0) {
      c.header('Retry-After', String(Math.max(1, Math.ceil(waitMs / 1000))))
      return c.json(
        {
          detail: testing
            ? 'a connection test is already running'
            : 'a connection test ran moments ago; try again shortly',
        },
        429,
      )
    }
    testing = true
    try {
      const repo = await store()
      if (typeof repo === 'string') return c.json({ detail: repo }, 503)
      if (!deps.kek.ok) return c.json({ detail: `no key-encryption key: ${deps.kek.reason}` }, 503)
      let credential: Credential | undefined
      try {
        credential = await repo.reveal(deps.kek.kek)
      } catch (err) {
        if (err instanceof SealError) {
          return c.json({ detail: `the stored credential cannot be decrypted: ${err.message}; save it again` }, 409)
        }
        throw err
      }
      if (!credential) return c.json({ detail: 'no credential is configured' }, 404)
      if (credential.kind === 'gateway') {
        // Again at test time: the name may resolve differently than at save.
        try {
          await assertGatewayHostAllowed(credential.baseUrl, resolveHost)
        } catch (err) {
          if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
          throw err
        }
      }
      const result = await deps.testConnection(credential)
      lastTestEnded = now()
      return c.json(result)
    } finally {
      testing = false
    }
  })
}

declare module '../app.js' {
  interface AppDeps {
    testConnection: (credential: Credential) => Promise<ConnectionTest>
    /** Connection-test cooldown; this file's default when omitted. */
    testCooldownMs?: number
    /** Clock for the connection-test cooldown; Date.now when omitted. */
    now?: () => number
  }
}

/** The Claude credential routes (#255). */
export const route: RouteModule = {
  register(app, deps) {
    registerCredentialRoutes(app, {
      credentials: deps.credentials,
      ready: ready(deps),
      kek: deps.kek,
      testConnection: deps.testConnection,
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      ...(deps.resolveHost === undefined ? {} : { resolveHost: deps.resolveHost }),
      ...(deps.testCooldownMs === undefined ? {} : { testCooldownMs: deps.testCooldownMs }),
      ...(deps.now === undefined ? {} : { now: deps.now }),
    })
  },
}
