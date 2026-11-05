import type { Hono } from 'hono'
import { z } from 'zod'
import { EgressError } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import { CHANNEL_KINDS, ChannelError, type ChannelStore } from '../notify/channels.js'
import { sessionLink } from '../notify/dispatcher.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The out-of-browser notification channels (#815 §3, notify/channels.ts), Settings →
// Assistant → Notifications. Only the person sets them: these routes take the UI's own
// requests only, and no tool reaches them (tools/coverage.ts), so an agent cannot add a
// place for the service to send to. A write is audited (app.ts, kind `settings`).
//
//   GET    /api/v1/ai/notify/channels             {"channels": [ChannelView]}
//   POST   /api/v1/ai/notify/channels             {kind, name, url, target?, token?, enabled?} → 201 ChannelView
//   PUT    /api/v1/ai/notify/channels/{id}        any of those → ChannelView
//   DELETE /api/v1/ai/notify/channels/{id}        → 204
//   POST   /api/v1/ai/notify/channels/{id}/test   → {"ok": true} or {"ok": false, "detail"}
//
// A token is never returned, only `has_token` and its last four characters.

export const NOTIFY_CHANNELS_PATH = '/api/v1/ai/notify/channels'

export type NotifyChannelRouteDeps = {
  /** Undefined without the database or the KEK (tokens are kept sealed). */
  channels: ChannelStore | undefined
  notify: { publicUrl: string | undefined }
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

declare module '../app.js' {
  interface AppDeps {
    /** The notification channels (#815, notify/channels.ts); left out without the database or the KEK. */
    notifyChannels?: ChannelStore | undefined
    /** SCADBUDDY_PUBLIC_URL, for the links notifications carry. */
    publicUrl?: string | undefined
  }
}

const UNAVAILABLE = 'notification channels need the database and SCADBUDDY_SECRET_KEY_FILE (their tokens are kept sealed)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'notification channels'

const Fields = {
  name: z.string().max(100),
  url: z.string().max(2048),
  target: z.string().max(100).nullable(),
  token: z.string().max(4096).nullable(),
  enabled: z.boolean(),
}
const CreateBody = z.strictObject({
  kind: z.enum(CHANNEL_KINDS),
  name: Fields.name,
  url: Fields.url,
  target: Fields.target.optional(),
  token: Fields.token.optional(),
  enabled: Fields.enabled.optional(),
})
const UpdateBody = z.strictObject({
  kind: z.enum(CHANNEL_KINDS).optional(),
  name: Fields.name.optional(),
  url: Fields.url.optional(),
  target: Fields.target.optional(),
  token: Fields.token.optional(),
  enabled: Fields.enabled.optional(),
})

const problemOf = (err: unknown) =>
  err instanceof z.ZodError ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ') : 'body is not valid JSON'

/** Drops the keys a body left out, so an optional field is absent rather than `undefined`. */
function present<T extends object>(value: T): { [K in keyof T]?: Exclude<T[K], undefined> } {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as { [K in keyof T]?: Exclude<T[K], undefined> }
}

export function registerNotifyChannelRoutes(app: Hono, deps: NotifyChannelRouteDeps): void {
  async function store(): Promise<ChannelStore | string> {
    if (!deps.channels) return UNAVAILABLE
    return (await deps.ready()) ? deps.channels : NOT_READY
  }

  app.get(NOTIFY_CHANNELS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    return c.json({ channels: await s.list() })
  })

  app.post(NOTIFY_CHANNELS_PATH, jsonBodyLimit(16 * 1024), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    let body: z.infer<typeof CreateBody>
    try {
      body = CreateBody.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: problemOf(err) }, 400)
    }
    try {
      const id = await s.create({ ...present(body), kind: body.kind, name: body.name, url: body.url })
      return c.json((await s.list()).find((v) => v.id === id), 201)
    } catch (err) {
      if (err instanceof ChannelError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  })

  app.put(`${NOTIFY_CHANNELS_PATH}/:id`, jsonBodyLimit(16 * 1024), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    let body: z.infer<typeof UpdateBody>
    try {
      body = UpdateBody.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: problemOf(err) }, 400)
    }
    const id = c.req.param('id')
    try {
      await s.update(id, present(body))
      return c.json((await s.list()).find((v) => v.id === id))
    } catch (err) {
      if (err instanceof ChannelError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  })

  app.delete(`${NOTIFY_CHANNELS_PATH}/:id`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    return (await s.delete(c.req.param('id'))) ? c.body(null, 204) : c.json({ detail: `no channel ${c.req.param('id')}` }, 404)
  })

  app.post(`${NOTIFY_CHANNELS_PATH}/:id/test`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `tests of ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    const id = c.req.param('id')
    let channel
    try {
      channel = await s.open(id)
    } catch (err) {
      return c.json({ ok: false, detail: `the token cannot be opened (${(err as Error).message}); enter it again` })
    }
    if (!channel) return c.json({ detail: `no channel ${id}` }, 404)
    const click = sessionLink(deps.notify.publicUrl, null)
    try {
      await s.send(channel, { title: 'ScadBuddy test notification', body: 'Notifications from the assistant will arrive here.', ...(click ? { click } : {}) })
      await s.record(id, null)
      return c.json({ ok: true })
    } catch (err) {
      const detail = err instanceof ChannelError || err instanceof EgressError ? err.message : `the send failed (${(err as Error).message})`
      await s.record(id, detail)
      return c.json({ ok: false, detail })
    }
  })
}

/** The notification channels (#815). */
export const route: RouteModule = {
  register(app, deps) {
    registerNotifyChannelRoutes(app, {
      channels: deps.notifyChannels,
      notify: { publicUrl: deps.publicUrl },
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
