import type { Context, Hono } from 'hono'
import { z } from 'zod'
import type { PluginListingPut, ToolListings } from '../access/listings.js'
import type { AppDeps } from '../app.js'
import { EgressError, type Resolver, systemResolver } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import { BuiltInTools, type BuiltInToolSet } from '../plugins/builtInTools.js'
import type { PluginForwarder } from '../plugins/forwarder.js'
import {
  assertEndpointAllowed,
  normalisePluginUrl,
  PluginError,
  type PluginRepo,
  type PluginSummary,
  type RemotePlugin,
  reservedNameProblem,
  toolPrefix,
} from '../plugins/registry.js'
import { DEFAULT_PLUGIN_TEST_TIMEOUT_MS, type PluginTest, testPlugin } from '../plugins/testConnection.js'
import { type KekStatus, SealError } from '../secrets.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

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
//
// ScadBuddy's own tool sets (#1953, plugins/builtInTools.ts) are listed first,
// `built_in: true`. Anything a remote plugin has and they do not (register, remove, URL,
// header, secret, connection test) answers 409 `{ built_in: true }`.
//
// A plugin's switch, tiers and disabled tools are not fields of POST or PATCH (#4828):
// they are `ai_access` rows, changed through /api/v1/ai/capabilities (spec §4.6). A body
// that names one is refused by the strict schema (400).

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
  /** ScadBuddy's own tool sets (#1953). */
  builtIns: BuiltInTools
  /** Where a good connection test's tools/list is stored (#2171); undefined without a database. */
  listings?: ToolListings
}

export type PluginView = {
  name: string
  kind: PluginSummary['kind']
  built_in: false
  url: string
  enabled: boolean
  /** The prefix of this plugin's tools in the harness. */
  tool_prefix: string
  auth: { header: string; last4: string } | null
  /** False when the stored secret was sealed with a key other than the mounted one. */
  usable: boolean
  /** Why the plugin is not handed to the harness whatever its state (#2127: a name Claude Code now reserves), or null. */
  problem: string | null
  tool_tiers: PluginSummary['tool_tiers']
  disabled_tools: string[]
  created_at: string
  updated_at: string
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

const CreateBody = z.strictObject({
  name: z.string().max(64),
  url: z.string().max(2048),
  auth_header: z.string().max(64).nullable().optional(),
  secret: z.string().min(1).max(4096).optional(),
})
const PatchBody = z.strictObject({
  url: z.string().max(2048).optional(),
  auth_header: z.string().max(64).nullable().optional(),
  secret: z.string().min(1).max(4096).nullable().optional(),
})

export function pluginView(plugin: PluginSummary, kek: KekStatus): PluginView {
  return {
    name: plugin.name,
    kind: plugin.kind,
    built_in: false,
    url: plugin.url,
    enabled: plugin.enabled,
    tool_prefix: toolPrefix(plugin.name),
    auth: plugin.auth_header === null ? null : { header: plugin.auth_header, last4: plugin.secret_last4 ?? '' },
    usable: plugin.kekId === null || (kek.ok && kek.kek.id === plugin.kekId),
    problem: reservedNameProblem(plugin.name),
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

  const builtIn = (name: string): BuiltInToolSet | undefined => deps.builtIns.named(name)
  /** 409 for what a built-in cannot do. */
  const isBuiltIn = (c: Context, name: string, what: string) =>
    c.json({ detail: `"${name}" is built in: it cannot be ${what}`, built_in: true }, 409)

  app.get(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const [builtIns, remote] = await Promise.all([deps.builtIns.list(), repo.list()])
    return c.json([...builtIns, ...remote.map((p) => pluginView(p, deps.kek))])
  })

  app.get(`${base}/:name`, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const set = builtIn(c.req.param('name'))
    if (set) return c.json(await deps.builtIns.view(set))
    const plugin = await repo.get(c.req.param('name'))
    if (!plugin) return c.json({ detail: `no plugin named "${c.req.param('name')}"` }, 404)
    return c.json(pluginView(plugin, deps.kek))
  })

  app.post(base, async (c) => {
    const repo = await store()
    if (typeof repo === 'string') return c.json({ detail: repo }, 503)
    const body = await parseBody(c, CreateBody)
    if (typeof body === 'string') return c.json({ detail: body }, 400)
    if (builtIn(body.name)) return isBuiltIn(c, body.name, 'added: it ships with the agent')
    try {
      // The endpoint is checked before anything is stored (egress + https).
      await assertEndpointAllowed(normalisePluginUrl(body.url), resolveHost)
      const saved = await repo.create(
        {
          name: body.name,
          url: body.url,
          ...(body.auth_header === undefined ? {} : { auth_header: body.auth_header }),
          ...(body.secret === undefined ? {} : { secret: body.secret }),
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
    const set = builtIn(c.req.param('name'))
    if (set) {
      if (body.url !== undefined || body.auth_header !== undefined || body.secret !== undefined) {
        return isBuiltIn(c, set.name, 'given a URL, an auth header or a secret: it runs inside the agent')
      }
      return c.json(await deps.builtIns.view(set))
    }
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
    if (builtIn(c.req.param('name'))) return isBuiltIn(c, c.req.param('name'), 'removed: disable its tools instead')
    if (!(await repo.delete(c.req.param('name')))) {
      return c.json({ detail: `no plugin named "${c.req.param('name')}"` }, 404)
    }
    return c.body(null, 204)
  })

  app.post(`${base}/:name/test`, async (c) => {
    const name = c.req.param('name')
    if (builtIn(name)) return isBuiltIn(c, name, 'tested: it has no endpoint; its tools are listed with it')
    const { status, body, retryAfter } = await testAndList(deps, name)
    if (retryAfter !== undefined) c.header('Retry-After', String(retryAfter))
    if (!('ok' in body)) return c.json(body, status)
    const { stored: _stored, ...sent } = body
    return c.json(sent, status)
  })
}

// One test (or listing refresh) per plugin at a time, whichever route started it: each
// one opens a connection to the endpoint. This set answers a second caller in this process
// without a database round trip; ToolListings.exclusive holds it across replicas (#4994).
const testing = new Set<string>()

// Seconds: a running test ends within its timeout (testConnection.ts), and its lock and slot are
// held until its listing write (ToolListings #putIfCurrent) commits; 2 s more covers that write.
// It assumes the default timeout: a test given a longer one of its own would outlast the hint.
const RETRY_AFTER_S = Math.ceil(DEFAULT_PLUGIN_TEST_TIMEOUT_MS / 1000) + 2
const BUSY = { status: 429, body: { detail: 'a connection test for this plugin is already running' }, retryAfter: RETRY_AFTER_S } as const

/** `retryAfter`: seconds, sent as `Retry-After` with a 429. */
export type TestAnswer = { status: 200 | 400 | 404 | 409 | 429 | 503; body: PluginTest | { detail: string }; retryAfter?: number }

type TestDeps = ReturnType<typeof pluginTestDeps> & { ready: () => Promise<boolean> }

/**
 * Tests the remote plugin `name` against the address its endpoint resolves to now, and
 * stores the listing of a good test in ai_tool_listings (#2171); a failed one stores nothing.
 */
export async function testAndList(
  deps: TestDeps,
  name: string,
): Promise<TestAnswer> {
  if (testing.has(name)) return BUSY
  testing.add(name)
  try {
    if (!deps.plugins) return { status: 503, body: { detail: NO_DATABASE } }
    if (!(await deps.ready())) return { status: 503, body: { detail: NOT_READY } }
    let plugin: RemotePlugin | undefined
    try {
      plugin = await deps.plugins.reveal(name, deps.kek.ok ? deps.kek.kek : undefined)
    } catch (err) {
      if (err instanceof SealError) {
        return { status: 409, body: { detail: `the plugin secret cannot be decrypted: ${err.message}; save it again` } }
      }
      throw err
    }
    if (!plugin) return { status: 404, body: { detail: `no plugin named "${name}"` } }
    let address: string | undefined
    try {
      // Again at test time: the name may resolve differently than at save.
      // The forwarder connects to exactly the address checked here.
      ;[address] = await assertEndpointAllowed(plugin.url, deps.resolveHost ?? systemResolver)
    } catch (err) {
      // assertEndpointAllowed throws EgressError only (a resolver's failure included).
      if (err instanceof EgressError) return { status: 400, body: { detail: err.message } }
      throw err
    }
    if (address === undefined) return { status: 400, body: { detail: 'url host resolves to no address' } }
    const tested = plugin
    const run = async (put?: PluginListingPut): Promise<PluginTest> => {
      const test = await deps.testPlugin(tested, address)
      // Only while the plugin still stands at the URL tested: a delete or a URL change since wins.
      if (test.ok) await put?.(tested.url, test.stored, { truncated: test.truncated })
      return test
    }
    if (!deps.listings) return { status: 200, body: await run() }
    const held = await deps.listings.exclusive(name, run)
    if (held === 'busy') return BUSY
    if (held === 'full') {
      return { status: 429, body: { detail: 'too many connection tests are running; try again shortly' }, retryAfter: RETRY_AFTER_S }
    }
    // The lock client's database is down: the same answer as the ready check above, not a 500.
    if (held === 'unreachable') return { status: 503, body: { detail: NOT_READY } }
    return { status: 200, body: held.value }
  } finally {
    testing.delete(name)
  }
}

/** What the connection test needs from the app's deps: the default test goes through the forwarder. */
export function pluginTestDeps(
  deps: AppDeps,
): Pick<PluginRouteDeps, 'plugins' | 'kek' | 'testPlugin' | 'resolveHost'> & { listings?: ToolListings } {
  return {
    plugins: deps.plugins,
    kek: deps.kek,
    testPlugin:
      deps.testPlugin ??
      ((plugin, address) =>
        deps.pluginForwarder
          ? testPlugin(plugin, address, deps.pluginForwarder)
          : Promise.resolve({
              ok: false,
              detail: 'the plugin forwarder is not running',
              duration_ms: 0,
              server: null,
              tools: [],
              truncated: false,
              stored: [],
            })),
    ...(deps.resolveHost === undefined ? {} : { resolveHost: deps.resolveHost }),
    ...(deps.listings === undefined ? {} : { listings: deps.listings }),
  }
}

declare module '../app.js' {
  interface AppDeps {
    /** The plugin registry (#297); undefined when there is no database. */
    plugins?: PluginRepo | undefined
    /** The plugin connection test; src/plugins/testConnection.ts when omitted. */
    testPlugin?: (plugin: RemotePlugin, address: string) => Promise<PluginTest>
    /** The loopback forwarder plugin traffic goes through (plugins/forwarder.ts); needed by the default test. */
    pluginForwarder?: PluginForwarder
    /** The tools each remote or package source last listed (#2171); undefined without a database. */
    listings?: ToolListings
  }
}

/** The plugin registry routes (#297). */
export const route: RouteModule = {
  register(app, deps) {
    registerPluginRoutes(app, {
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      builtIns: new BuiltInTools(deps.access),
      ...pluginTestDeps(deps),
    })
  },
}
