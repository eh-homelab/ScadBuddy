import type { Context, Hono } from 'hono'
import { z } from 'zod'
import { RISK_TIERS } from '../harness/permissions.js'
import { EgressError, type Resolver, systemResolver } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import {
  assertEndpointAllowed,
  normalisePluginUrl,
  PluginError,
  type PluginRepo,
  type PluginSummary,
  type RemotePlugin,
  toolPrefix,
} from '../plugins/registry.js'
import type { PluginTest } from '../plugins/testConnection.js'
import { type KekStatus, SealError } from '../secrets.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'

// /api/v1/ai/plugins (issue #297): the admin surface of the plugin registry
// (src/plugins/registry.ts, table `ai_plugins`).
//
//   GET    /api/v1/ai/plugins              list
//   GET    /api/v1/ai/plugins/:name        one
//   POST   /api/v1/ai/plugins              register  (201; 409 when the name is taken)
//   PATCH  /api/v1/ai/plugins/:name        update    (omitted fields are kept)
//   DELETE /api/v1/ai/plugins/:name        remove
//   POST   /api/v1/ai/plugins/:name/test   connect, one tools/list, report tools and tiers
//
// Every write, and the test (it sends the stored secret to the endpoint), goes
// through the same guard as the credential routes (guard.ts: HTTPS through a
// trusted proxy or loopback, the UI's origin, JSON bodies). Changing a plugin
// is a settings write, outward tier (spec §8.1). No route returns the secret:
// views carry the header NAME and the value's last four characters.

export type PluginRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  plugins: PluginRepo | undefined
  ready: () => Promise<boolean>
  kek: KekStatus
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  resolveHost?: Resolver
  /** Runs the connection test against the checked `address` (testConnection.ts). */
  testPlugin: (plugin: RemotePlugin, address: string) => Promise<PluginTest>
}

export type PluginView = {
  name: string
  kind: PluginSummary['kind']
  url: string
  enabled: boolean
  /** The prefix of this plugin's tools in the harness. */
  tool_prefix: string
  auth: { header: string; last4: string } | null
  /** False when the stored secret was sealed with a key other than the mounted one. */
  usable: boolean
  tool_tiers: PluginSummary['tool_tiers']
  disabled_tools: string[]
  created_at: string
  updated_at: string
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

const Tiers = z.record(z.string(), z.enum(RISK_TIERS))
const CreateBody = z.strictObject({
  name: z.string().max(64),
  url: z.string().max(2048),
  enabled: z.boolean().optional(),
  auth_header: z.string().max(64).nullable().optional(),
  secret: z.string().min(1).max(4096).optional(),
  tool_tiers: Tiers.optional(),
  disabled_tools: z.array(z.string().max(128)).max(256).optional(),
})
const PatchBody = z.strictObject({
  url: z.string().max(2048).optional(),
  enabled: z.boolean().optional(),
  auth_header: z.string().max(64).nullable().optional(),
  secret: z.string().min(1).max(4096).nullable().optional(),
  tool_tiers: Tiers.optional(),
  disabled_tools: z.array(z.string().max(128)).max(256).optional(),
})

export function pluginView(plugin: PluginSummary, kek: KekStatus): PluginView {
  return {
    name: plugin.name,
    kind: plugin.kind,
    url: plugin.url,
    enabled: plugin.enabled,
    tool_prefix: toolPrefix(plugin.name),
    auth: plugin.auth_header === null ? null : { header: plugin.auth_header, last4: plugin.secret_last4 ?? '' },
    usable: plugin.kekId === null || (kek.ok && kek.kek.id === plugin.kekId),
    tool_tiers: plugin.tool_tiers,
    disabled_tools: plugin.disabled_tools,
    created_at: plugin.created_at,
    updated_at: plugin.updated_at,
  }
}

async function parseBody<T extends z.ZodType>(c: Context, schema: T): Promise<z.infer<T> | string> {
  try {
    return schema.parse(await c.req.json())
  } catch (err) {
    return err instanceof z.ZodError
      ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
      : 'body is not valid JSON'
  }
}

export function registerPluginRoutes(app: Hono, deps: PluginRouteDeps): void {
  const base = '/api/v1/ai/plugins'
  const resolveHost = deps.resolveHost ?? systemResolver
  const kek = () => (deps.kek.ok ? deps.kek.kek : undefined)

  async function store(): Promise<PluginRepo | string> {
    if (!deps.plugins) return NO_DATABASE
    return (await deps.ready()) ? deps.plugins : NOT_READY
  }

  /** Maps the registry's refusals to responses; anything else is a 500. */
  function refusal(c: Context, err: unknown) {
    if (err instanceof PluginError) return c.json({ detail: err.message }, err.status)
    if (err instanceof EgressError) return c.json({ detail: err.message }, 400)
    throw err
  }

  app.on(['POST', 'PATCH', 'PUT', 'DELETE'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'plugin changes')
    if (problem) return c.json({ detail: problem }, 403)
    // guard.ts checks the body type of PUT only; these routes also read POST
    // and PATCH bodies, so a cross-origin HTML form (which cannot send JSON
    // without a preflight) is refused here too. POST .../test reads no body.
    const readsBody = c.req.method === 'PATCH' || (c.req.method === 'POST' && !c.req.path.endsWith('/test'))
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (readsBody && type !== 'application/json') return c.json({ detail: 'request body must be application/json' }, 403)
    await next()
  })

  // Reads show endpoints, header names and tier maps (never the secret):
  // same-origin or loopback only (guard.ts `uiReadProblem`).
  app.on('GET', [base, `${base}/*`], async (c, next) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'plugin reads')
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })

  app.get(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    return c.json((await repo.list()).map((p) => pluginView(p, deps.kek)))
  })

  app.get(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const plugin = await repo.get(c.req.param('name'))
    if (!plugin) return c.json({ detail: `no plugin named "${c.req.param('name')}"` }, 404)
    return c.json(pluginView(plugin, deps.kek))
  })

  app.post(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, CreateBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    try {
      // The endpoint is checked before anything is stored (egress + https).
      await assertEndpointAllowed(normalisePluginUrl(body.url), resolveHost)
      const saved = await repo.create(
        {
          name: body.name,
          url: body.url,
          ...(body.enabled === undefined ? {} : { enabled: body.enabled }),
          ...(body.auth_header === undefined ? {} : { auth_header: body.auth_header }),
          ...(body.secret === undefined ? {} : { secret: body.secret }),
          ...(body.tool_tiers === undefined ? {} : { tool_tiers: body.tool_tiers }),
          ...(body.disabled_tools === undefined ? {} : { disabled_tools: body.disabled_tools }),
        },
        kek(),
      )
      return c.json(pluginView(saved, deps.kek), 201)
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.patch(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, PatchBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    try {
      if (body.url !== undefined) await assertEndpointAllowed(normalisePluginUrl(body.url), resolveHost)
      const saved = await repo.update(c.req.param('name'), body, kek())
      return c.json(pluginView(saved, deps.kek))
    } catch (err) {
      return refusal(c, err)
    }
  })

  app.delete(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    if (!(await repo.delete(c.req.param('name')))) {
      return c.json({ detail: `no plugin named "${c.req.param('name')}"` }, 404)
    }
    return c.body(null, 204)
  })

  // One test per plugin at a time: each one opens a connection to the endpoint.
  const testing = new Set<string>()

  app.post(`${base}/:name/test`, async (c) => {
    const name = c.req.param('name')
    if (testing.has(name)) return c.json({ detail: 'a connection test for this plugin is already running' }, 429)
    testing.add(name)
    try {
      const repo = await store()
      if (typeof repo === 'string') return c.json({ detail: repo }, 503)
      let plugin: RemotePlugin | undefined
      try {
        plugin = await repo.reveal(name, kek())
      } catch (err) {
        if (err instanceof SealError) {
          return c.json({ detail: `the plugin secret cannot be decrypted: ${err.message}; save it again` }, 409)
        }
        throw err
      }
      if (!plugin) return c.json({ detail: `no plugin named "${name}"` }, 404)
      let address: string | undefined
      try {
        // Again at test time: the name may resolve differently than at save.
        // The forwarder connects to exactly the address checked here.
        ;[address] = await assertEndpointAllowed(plugin.url, resolveHost)
      } catch (err) {
        return refusal(c, err)
      }
      if (address === undefined) return c.json({ detail: 'url host resolves to no address' }, 400)
      return c.json(await deps.testPlugin(plugin, address))
    } finally {
      testing.delete(name)
    }
  })
}
