import type { Context, Hono } from 'hono'
import { z } from 'zod'
import {
  type Credential,
  CREDENTIAL_KINDS,
  CredentialError,
  type CredentialRepo,
  normaliseBaseUrl,
  opensWith,
  soonestRecovery,
  type StoredCredential,
} from '../credentials.js'
import type { ConnectionTest } from '../harness/testConnection.js'
import { assertGatewayHostAllowed, EgressError, type Resolver, systemResolver } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import { type KekStatus, SealError } from '../secrets.js'
import { jsonBodyLimit, type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/credentials (issue #255; several credentials since #1093). The
// ingress routes /api/v1/ai/* to this service (spec §4.2). No route ever
// returns a secret: reads give `kind`, `base_url`, the last four characters
// and the credential's health (spec §8.6, "never returned by any route").
// Error bodies use `{ detail }`, the backend's FastAPI shape, so the frontend
// handles both services' errors the same way.
//
// Two sets of routes over the same rows:
//
//   GET|PUT|DELETE /api/v1/ai/credentials, POST /api/v1/ai/credentials/test
//     The single-credential routes #255 began with, kept for Settings as it is
//     (#1000). They act on the FIRST credential by priority: PUT saves over it
//     (creating one when there is none), DELETE removes it (so the next one
//     moves up), test tests it.
//
//   /api/v1/ai/credentials/entries[/:id[/test|/reset]], /api/v1/ai/credentials/order
//     Every credential (#1093): list, create, save, delete, test, reset, and
//     the order a query tries them in.

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

/** One credential in the list (#1093). */
export type CredentialEntryView = {
  id: string
  /** 0 is tried first. */
  priority: number
  kind: StoredCredential['kind']
  base_url: string | null
  last4: string
  updated_at: string
  /** False when the stored secret was sealed with a key other than the mounted one, or in the old format. */
  usable: boolean
  /** `cooling_down` until `cooldown_until`, then `active` again on its own; `disabled` until reset or saved with a new secret. */
  status: StoredCredential['status']
  cooldown_until: string | null
  /** Why it was last refused (the reason it is disabled or cooling down), without the secret. */
  last_error: string | null
  last_error_at: string | null
  last_used_at: string | null
}

export type CredentialListView = {
  /** In priority order. */
  credentials: CredentialEntryView[]
  /** Whether any credential can be used now. */
  usable_now: boolean
  /** When none can: the soonest a rate-limited one is usable again; null if every one needs a reset. */
  recovers_at: string | null
  /** Whether a save with a secret can succeed, and if not, why. */
  can_save: boolean
  cannot_save_reason: string | null
}

const SaveBody = z.strictObject({
  kind: z.enum(CREDENTIAL_KINDS),
  base_url: z.string().max(2048).nullable().optional(),
  secret: z.string().min(1).max(4096).optional(),
})

const CreateBody = SaveBody.extend({ secret: z.string().min(1).max(4096) })

const OrderBody = z.strictObject({ ids: z.array(z.string().min(1).max(64)).max(100) })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
/**
 * Says "no AI database here" to a machine: the Settings section hides on it
 * (frontend `AiCredentialSection` `notDeployed`) instead of matching the text above.
 */
export const NO_DATABASE_CODE = 'no_database'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
export const DEFAULT_TEST_COOLDOWN_MS = 10_000
/** The largest credential body read: a secret and a base URL, with room to spare. */
const BODY_MAX = 16 * 1024

function cannotSave(kek: KekStatus): string | null {
  // `reason` is the public one (no file path or errno; secrets.ts `loadKek`).
  return kek.ok ? null : `no key-encryption key: ${kek.reason}`
}

export function view(stored: StoredCredential | undefined, kek: KekStatus): CredentialView {
  return {
    configured: stored !== undefined,
    kind: stored?.kind ?? null,
    base_url: stored?.base_url ?? null,
    last4: stored?.last4 ?? null,
    updated_at: stored?.updated_at ?? null,
    usable: stored !== undefined && opensWith(stored, kek),
    can_save: kek.ok,
    cannot_save_reason: cannotSave(kek),
  }
}

export function entryView(stored: StoredCredential, kek: KekStatus): CredentialEntryView {
  return {
    id: stored.id,
    priority: stored.priority,
    kind: stored.kind,
    base_url: stored.base_url,
    last4: stored.last4,
    updated_at: stored.updated_at,
    usable: opensWith(stored, kek),
    status: stored.status,
    cooldown_until: stored.cooldown_until,
    last_error: stored.last_error,
    last_error_at: stored.last_error_at,
    last_used_at: stored.last_used_at,
  }
}

export function listView(list: readonly StoredCredential[], kek: KekStatus): CredentialListView {
  const usableNow = list.some((c) => c.status === 'active' && opensWith(c, kek))
  return {
    credentials: list.map((c) => entryView(c, kek)),
    usable_now: usableNow,
    recovers_at: usableNow ? null : (soonestRecovery(list, kek)?.toISOString() ?? null),
    can_save: kek.ok,
    cannot_save_reason: cannotSave(kek),
  }
}

type Parsed<T> = { ok: true; body: T } | { ok: false; response: Response }

/** The request's JSON body, checked against `schema`; a 415 or 400 response otherwise. */
async function jsonBody<T>(c: Context, schema: z.ZodType<T>): Promise<Parsed<T>> {
  const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
  if (type !== 'application/json') {
    return { ok: false, response: c.json({ detail: 'request body must be application/json' }, 415) }
  }
  try {
    return { ok: true, body: schema.parse(await c.req.json()) }
  } catch (err) {
    const detail =
      err instanceof z.ZodError
        ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
        : 'body is not valid JSON'
    return { ok: false, response: c.json({ detail }, 400) }
  }
}

export function registerCredentialRoutes(app: Hono, deps: CredentialRouteDeps): void {
  const base = '/api/v1/ai/credentials'
  const entries = `${base}/entries`
  const resolveHost = deps.resolveHost ?? systemResolver
  const cooldownMs = deps.testCooldownMs ?? DEFAULT_TEST_COOLDOWN_MS
  const now = deps.now ?? Date.now
  const kek = deps.kek

  /** The store once migrations are current, or a 503 response. */
  async function store(): Promise<CredentialRepo | string> {
    if (!deps.credentials) return NO_DATABASE
    return (await deps.ready()) ? deps.credentials : NOT_READY
  }

  /** The 503 for `store()`'s refusal, with `code` when there is no database at all. */
  function unavailable(detail: string) {
    return { detail, ...(detail === NO_DATABASE ? { code: NO_DATABASE_CODE } : {}) }
  }

  /** Runs `fn` with the store, mapping the store's refusals to their statuses. */
  async function withStore(c: Context, fn: (repo: CredentialRepo) => Promise<Response>): Promise<Response> {
    const repo = await store()
    if (typeof repo === 'string') return c.json(unavailable(repo), 503)
    try {
      return await fn(repo)
    } catch (err) {
      if (err instanceof CredentialError) return c.json({ detail: err.message }, err.status)
      if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
      throw err
    }
  }

  /** Validates a save: a gateway host that is (or resolves to) link-local or cloud metadata is refused before anything is stored. */
  async function checkGateway(body: z.infer<typeof SaveBody>): Promise<void> {
    if (body.kind === 'gateway' && body.base_url) {
      await assertGatewayHostAllowed(normaliseBaseUrl(body.base_url), resolveHost)
    }
  }

  const update = (body: z.infer<typeof SaveBody>) => ({
    kind: body.kind,
    ...(body.base_url === undefined ? {} : { base_url: body.base_url }),
    ...(body.secret === undefined ? {} : { secret: body.secret }),
  })
  const kekOrUndefined = kek.ok ? kek.kek : undefined

  // Every write is outward tier; see guard.ts for what is and is not checked.
  app.on(['PUT', 'DELETE', 'POST'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress)
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })
  const limit = jsonBodyLimit(BODY_MAX)

  // --- The single-credential routes (#255): the first credential by priority.

  app.get(base, (c) => withStore(c, async (repo) => c.json(view(await repo.get(), kek))))

  app.put(base, limit, async (c) => {
    const parsed = await jsonBody(c, SaveBody)
    if (!parsed.ok) return parsed.response
    return withStore(c, async (repo) => {
      await checkGateway(parsed.body)
      return c.json(view(await repo.put(update(parsed.body), kekOrUndefined), kek))
    })
  })

  app.delete(base, (c) =>
    withStore(c, async (repo) => {
      await repo.delete()
      return c.json(view(undefined, kek))
    }),
  )

  // --- Every credential (#1093).

  app.get(entries, (c) => withStore(c, async (repo) => c.json(listView(await repo.list(), kek))))

  app.post(entries, limit, async (c) => {
    const parsed = await jsonBody(c, CreateBody)
    if (!parsed.ok) return parsed.response
    return withStore(c, async (repo) => {
      await checkGateway(parsed.body)
      return c.json(entryView(await repo.create(update(parsed.body), kekOrUndefined), kek), 201)
    })
  })

  app.put(`${base}/order`, limit, async (c) => {
    const parsed = await jsonBody(c, OrderBody)
    if (!parsed.ok) return parsed.response
    return withStore(c, async (repo) => c.json(listView(await repo.reorder(parsed.body.ids), kek)))
  })

  app.put(`${entries}/:id`, limit, async (c) => {
    const parsed = await jsonBody(c, SaveBody)
    if (!parsed.ok) return parsed.response
    return withStore(c, async (repo) => {
      await checkGateway(parsed.body)
      return c.json(entryView(await repo.put(update(parsed.body), kekOrUndefined, c.req.param('id')), kek))
    })
  })

  app.delete(`${entries}/:id`, (c) =>
    withStore(c, async (repo) => {
      const id = c.req.param('id')
      if (!(await repo.delete(id))) return c.json({ detail: `no credential ${id}` }, 404)
      return c.json(listView(await repo.list(), kek))
    }),
  )

  app.post(`${entries}/:id/reset`, (c) =>
    withStore(c, async (repo) => {
      const id = c.req.param('id')
      const reset = await repo.reset(id)
      return reset ? c.json(entryView(reset, kek)) : c.json({ detail: `no credential ${id}` }, 404)
    }),
  )

  // One connection test at a time, and at most one per cooldown, across both
  // test routes: each test starts a Claude Code process and spends real tokens.
  // A test reports; it does not change the credential's status (Reset does).
  let testing = false
  let lastTestEnded = Number.NEGATIVE_INFINITY

  async function runTest(c: Context, id: string | undefined): Promise<Response> {
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
      if (typeof repo === 'string') return c.json(unavailable(repo), 503)
      if (!kek.ok) return c.json({ detail: `no key-encryption key: ${kek.reason}` }, 503)
      let credential: Credential | undefined
      try {
        credential = await repo.reveal(kek.kek, id)
      } catch (err) {
        if (err instanceof SealError) {
          return c.json({ detail: `the stored credential cannot be decrypted: ${err.message}; save it again` }, 409)
        }
        throw err
      }
      if (!credential) {
        return c.json({ detail: id === undefined ? 'no credential is configured' : `no credential ${id}` }, 404)
      }
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
  }

  app.post(`${base}/test`, (c) => runTest(c, undefined))
  app.post(`${entries}/:id/test`, (c) => runTest(c, c.req.param('id')))
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

/** The Claude credential routes (#255, #1093). */
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
