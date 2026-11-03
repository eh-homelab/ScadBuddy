import type { Hono } from 'hono'
import { z } from 'zod'
import { browserUser } from '../auth/browserSession.js'
import { assertHostAllowed, assertSecureUrl, EgressError, type Resolver, systemResolver } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import type { PushDispatcher } from '../notify/dispatcher.js'
import type { PushStore } from '../notify/pushStore.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// Web Push (#815 §3), the browser's half of a closed tab's notifications:
//
//   GET    /api/v1/ai/push/key            {"public_key": "<base64url>"}: the VAPID key to subscribe with
//   PUT    /api/v1/ai/push/subscription   {"endpoint", "keys": {"p256dh", "auth"}}: this browser's subscription
//   DELETE /api/v1/ai/push/subscription?endpoint=…: forget it (the user turned notifications off)
//
// The page subscribes only once the user has granted notifications from a click
// (frontend `agent/webPush.ts`). The subscription is the signed-in person's, so a push
// names only what that person may see. Its endpoint is where the agent will POST, so it is
// checked like any other egress (http/egress.ts) when it is stored, and again at each push.

export const PUSH_KEY_PATH = '/api/v1/ai/push/key'
export const PUSH_SUBSCRIPTION_PATH = '/api/v1/ai/push/subscription'

export type PushRouteDeps = {
  /** Undefined without the database or the KEK: there is nowhere to keep the keys sealed. */
  push: { store: PushStore; dispatcher: PushDispatcher } | undefined
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

export function registerPushRoutes(app: Hono, deps: PushRouteDeps): void {
  async function push(): Promise<PushRouteDeps['push'] | string> {
    if (!deps.push) return UNAVAILABLE
    return (await deps.ready()) ? deps.push : NOT_READY
  }

  app.get(PUSH_KEY_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const p = await push()
    if (typeof p === 'string') return c.json({ detail: p }, 503)
    return c.json({ public_key: (await p!.store.vapidKeys()).publicKey.toString('base64url') })
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
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    try {
      const target = assertSecureUrl(body.endpoint, 'endpoint')
      await assertHostAllowed(target.href, deps.resolve ?? systemResolver, 'endpoint', 'a push service')
    } catch (err) {
      if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
      throw err
    }
    await p!.store.subscribe(browserUser(c), body.endpoint, body.keys)
    // Anything already waiting is announced to the new subscription at once.
    p!.dispatcher.wake()
    return c.body(null, 204)
  })

  app.delete(PUSH_SUBSCRIPTION_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const p = await push()
    if (typeof p === 'string') return c.json({ detail: p }, 503)
    const endpoint = c.req.query('endpoint')
    if (!endpoint) return c.json({ detail: 'endpoint is required' }, 400)
    await p!.store.unsubscribe(endpoint, browserUser(c))
    return c.body(null, 204)
  })
}

/** Web Push's key and subscriptions (#815). */
export const route: RouteModule = {
  register(app, deps) {
    registerPushRoutes(app, {
      push: deps.push,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      ...(deps.resolveHost ? { resolve: deps.resolveHost } : {}),
    })
  },
}
