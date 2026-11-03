import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import { browserUser } from '../auth/browserSession.js'
import type { SettingsRepo } from '../credentials.js'
import { EgressError, type Resolver, systemResolver } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import type { PushDispatcher } from '../notify/dispatcher.js'
import { checkPushEndpoint, DEFAULT_PUSH_HOSTS, isHostPattern, PUSH_HOSTS_MAX, pushHostsOf, SETTING_PUSH_HOSTS } from '../notify/pushEgress.js'
import { type PushStore, SubscriptionTakenError } from '../notify/pushStore.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// Web Push (#815 §3), the browser's half of a closed tab's notifications:
//
//   GET    /api/v1/ai/push/key            {"public_key": "<base64url>"}: the VAPID key to subscribe with
//   PUT    /api/v1/ai/push/subscription   {"endpoint", "keys": {"p256dh", "auth"}}: this browser's subscription
//   DELETE /api/v1/ai/push/subscription?endpoint=…: forget it (the user turned notifications off)
//   GET    /api/v1/ai/settings/push-services  {"hosts": [...], "default": [...]}: the push-service allowlist
//   PUT    /api/v1/ai/settings/push-services  {"hosts": [...]}: Settings → Assistant → Notifications
//
// The page subscribes only once the user has granted notifications from a click
// (frontend `agent/webPush.ts`). The subscription is the signed-in person's, so a push
// names only what that person may see; an endpoint another person holds is refused, never
// taken over. Its endpoint is where the agent will POST, so it is checked by
// notify/pushEgress.ts (https, an allowlisted push service, public addresses only) when it
// is stored, and again at each push.

export const PUSH_KEY_PATH = '/api/v1/ai/push/key'
export const PUSH_SUBSCRIPTION_PATH = '/api/v1/ai/push/subscription'
export const PUSH_SERVICES_PATH = '/api/v1/ai/settings/push-services'

export type PushRouteDeps = {
  /** Undefined without the database or the KEK: there is nowhere to keep the keys sealed. */
  push: { store: PushStore; dispatcher: PushDispatcher } | undefined
  /** Where the allowlist is kept; undefined without the database. */
  settings: SettingsRepo | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  resolve?: Resolver
}

declare module '../app.js' {
  interface AppDeps {
    /** Web Push (#815, notify/): left out without the database or the KEK. */
    push?: { store: PushStore; dispatcher: PushDispatcher } | undefined
  }
}

const UNAVAILABLE = 'Web Push needs the database and SCADBUDDY_SECRET_KEY_FILE (its keys are kept sealed)'
const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'push notification settings'

const base64url = (octets: number) =>
  z
    .string()
    .regex(/^[A-Za-z0-9_-]+={0,2}$/)
    .transform((text) => Buffer.from(text, 'base64url'))
    .refine((bytes) => bytes.length === octets, `must be ${octets} octets, base64url`)

const SubscriptionBody = z.strictObject({
  endpoint: z.string().max(2048),
  keys: z.object({ p256dh: base64url(65).refine((b) => b[0] === 4, 'must be an uncompressed P-256 point'), auth: base64url(16) }),
  // PushSubscription.toJSON() also carries this; it is not kept.
  expirationTime: z.number().nullable().optional(),
})

const HostsBody = z.strictObject({
  hosts: z
    .array(z.string().transform((h) => h.trim().toLowerCase()).refine(isHostPattern, 'must be a host name, or *. and one'))
    .max(PUSH_HOSTS_MAX),
})

const problemOf = (err: unknown) =>
  err instanceof z.ZodError ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ') : 'body is not valid JSON'

export function registerPushRoutes(app: Hono, deps: PushRouteDeps): void {
  async function push(): Promise<NonNullable<PushRouteDeps['push']> | string> {
    if (!deps.push) return UNAVAILABLE
    return (await deps.ready()) ? deps.push : NOT_READY
  }
  async function settings(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(PUSH_KEY_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const p = await push()
    if (typeof p === 'string') return c.json({ detail: p }, 503)
    return c.json({ public_key: (await p.store.vapidKeys()).publicKey.toString('base64url') })
  })

  app.put(PUSH_SUBSCRIPTION_PATH, jsonBodyLimit(8 * 1024), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const p = await push()
    if (typeof p === 'string') return c.json({ detail: p }, 503)
    let body: z.infer<typeof SubscriptionBody>
    try {
      body = SubscriptionBody.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: problemOf(err) }, 400)
    }
    try {
      const hosts = pushHostsOf(await deps.settings?.get<unknown>(SETTING_PUSH_HOSTS))
      await checkPushEndpoint(body.endpoint, hosts, deps.resolve ?? systemResolver)
      await p.store.subscribe(browserUser(c), body.endpoint, body.keys)
    } catch (err) {
      if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
      if (err instanceof SubscriptionTakenError) return c.json({ detail: err.message }, 409)
      throw err
    }
    // Anything already waiting is announced to the new subscription at once.
    p.dispatcher.wake()
    return c.body(null, 204)
  })

  app.delete(PUSH_SUBSCRIPTION_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const p = await push()
    if (typeof p === 'string') return c.json({ detail: p }, 503)
    const endpoint = c.req.query('endpoint')
    if (!endpoint) return c.json({ detail: 'endpoint is required' }, 400)
    await p.store.unsubscribe(endpoint, browserUser(c))
    return c.body(null, 204)
  })

  const hostsView = (hosts: readonly string[]) => ({ hosts, default: DEFAULT_PUSH_HOSTS })

  app.get(PUSH_SERVICES_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await settings()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    return c.json(hostsView(pushHostsOf(await s.get<unknown>(SETTING_PUSH_HOSTS))))
  })

  app.put(PUSH_SERVICES_PATH, jsonBodyLimit(32 * 1024), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await settings()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    let body: z.infer<typeof HostsBody>
    try {
      body = HostsBody.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: problemOf(err) }, 400)
    }
    const hosts = [...new Set(body.hosts)]
    await s.set(SETTING_PUSH_HOSTS, hosts, { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) })
    return c.json(hostsView(hosts))
  })
}

/** Web Push's key, subscriptions and push-service allowlist (#815). */
export const route: RouteModule = {
  register(app, deps) {
    registerPushRoutes(app, {
      push: deps.push,
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      ...(deps.resolveHost ? { resolve: deps.resolveHost } : {}),
    })
  },
}
