import type { Context, Hono } from 'hono'
import { z } from 'zod'
import { Capabilities, CapabilityError, type CapabilityProvider } from '../access/capabilities.js'
import type { ToolListings } from '../access/listings.js'
import { packageProvider } from '../access/packageProvider.js'
import { builtInProviders, remoteProvider } from '../access/providers.js'
import { AccessError } from '../access/store.js'
import type { AppDeps } from '../app.js'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import { listPackageServer } from '../plugins/packages/listing.js'
import { PluginError } from '../plugins/registry.js'
import { jsonBodyLimit, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'
import { pluginTestDeps, testAndList } from './plugins.js'

// /api/v1/ai/capabilities (#2171, spec §4): Settings → Tools & plugins' tree of every
// source the agent's tools come from (access/capabilities.ts), and the user's writes to it.
//
//   GET    /api/v1/ai/capabilities                          { sources }
//   GET    /api/v1/ai/capabilities/:source                  one source
//   PUT    /api/v1/ai/capabilities/:source                  { enabled } switches it
//   GET    /api/v1/ai/capabilities/:source/items/:item      the item with its schemas
//   PUT    /api/v1/ai/capabilities/:source/items/:item      { access } sets it
//   DELETE /api/v1/ai/capabilities/:source/items/:item      back to its default
//   POST   /api/v1/ai/capabilities/:source/refresh          lists a remote plugin's or a package server's tools again
//
// A source or item id is one path segment, percent-encoded (`remote%3Ahs`,
// `package%3Ap%2Fmcp%3As`). Reads are behind `uiReadProblem`, writes and the refresh behind
// `uiRequestProblem` (guard.ts), as the plugin routes are: these are the user's settings
// writes. Every write answers the source as it is after it.

export const CAPABILITIES_PATH = '/api/v1/ai/capabilities'

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

const SourceBody = z.strictObject({ enabled: z.boolean() })
const ItemBody = z.strictObject({ access: z.enum(['off', 'read', 'write', 'outward', 'on']) })

/** No stored listings (no database for them): every remote source reads as never listed. */
const NO_LISTINGS: Pick<ToolListings, 'get' | 'getMany'> = {
  get: () => Promise.resolve(undefined),
  getMany: () => Promise.resolve(new Map()),
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

export const route: RouteModule = {
  register(app: Hono, deps: AppDeps) {
    const isReady = ready(deps)
    let built: Capabilities | undefined

    /** The tree, or why it cannot answer (503). */
    async function capabilities(): Promise<Capabilities | string> {
      if (!deps.access) return NO_DATABASE
      if (!(await isReady())) return NOT_READY
      if (built === undefined) {
        const providers: CapabilityProvider[] = [builtInProviders({ access: deps.access })]
        if (deps.plugins) {
          const tests = { ...pluginTestDeps(deps), ready: isReady }
          providers.push(
            remoteProvider({ plugins: deps.plugins, listings: deps.listings ?? NO_LISTINGS, refresh: (name) => testAndList(tests, name) }),
          )
        }
        if (deps.pluginPackages) {
          const packages = deps.pluginPackages
          const { packageInstaller: installer, pluginForwarder: forwarder, listings } = deps
          const provider = packageProvider({
            packages,
            access: deps.access,
            listings: listings ?? NO_LISTINGS,
            verifiedReviewOf: deps.verifiedPackageReview ?? (() => Promise.reject(new Error('no verified copy can be read here'))),
            ...(deps.packageLoadCheck ? { loadCheck: deps.packageLoadCheck } : {}),
            refresh: (pkg, server) =>
              installer && forwarder && listings
                ? listPackageServer(
                    {
                      packages,
                      installer,
                      forwarder,
                      listings,
                      // The tree's own rule: no connection to a server a turn would not offer.
                      refusal: (p, s) => provider.listingRefusal(p, s),
                      ...(deps.resolveHost ? { resolve: deps.resolveHost } : {}),
                    },
                    pkg,
                    server,
                  )
                : Promise.resolve({ status: 502, body: { detail: 'package servers cannot be listed here: no package cache, forwarder or database' } }),
          })
          providers.push(provider)
        }
        built = new Capabilities(providers)
      }
      return built
    }

    const context = (c: Context): AuditContext => ({ actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) })

    /**
     * Answers `run`'s value, a refusal with its status, or 503 without the database. With
     * `body`, the body is parsed after the database check (a bad one is 400) and passed to `run`.
     */
    async function answer<B = undefined>(c: Context, run: (caps: Capabilities, body: B) => Promise<unknown>, body?: z.ZodType<B>) {
      const caps = await capabilities()
      if (typeof caps === 'string') return c.json({ detail: caps }, 503)
      let parsed = undefined as B
      if (body !== undefined) {
        const read = await parseBody(c, body)
        if (typeof read === 'string') return c.json({ detail: read }, 400)
        parsed = read
      }
      try {
        return c.json(await run(caps, parsed))
      } catch (err) {
        if (err instanceof CapabilityError) {
          if (err.retryAfter !== undefined) c.header('Retry-After', String(err.retryAfter))
          return c.json({ detail: err.message }, err.status)
        }
        // A backstop: the providers already turn these into CapabilityError (access/providers.ts).
        if (err instanceof AccessError) return c.json({ detail: err.message }, 400)
        if (err instanceof PluginError) return c.json({ detail: err.message }, err.status === 422 ? 400 : err.status)
        throw err
      }
    }

    app.on(['PUT', 'POST', 'DELETE'], [CAPABILITIES_PATH, `${CAPABILITIES_PATH}/*`], jsonBodyLimit(4 * 1024), async (c, next) => {
      const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'tool access changes')
      if (problem) return c.json({ detail: problem }, 403)
      await next()
    })
    app.on('GET', [CAPABILITIES_PATH, `${CAPABILITIES_PATH}/*`], async (c, next) => {
      const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'tool access reads')
      if (problem) return c.json({ detail: problem }, 403)
      await next()
    })

    const one = `${CAPABILITIES_PATH}/:source`
    const item = `${one}/items/:item`

    app.get(CAPABILITIES_PATH, (c) => answer(c, async (caps) => ({ sources: await caps.tree() })))

    app.get(one, (c) => answer(c, (caps) => caps.source(c.req.param('source'))))

    app.put(one, (c) => answer(c, (caps, body) => caps.setEnabled(c.req.param('source'), body.enabled, context(c)), SourceBody))

    app.get(item, (c) => answer(c, (caps) => caps.details(c.req.param('source'), c.req.param('item'))))

    app.put(item, (c) =>
      answer(c, (caps, body) => caps.setItem(c.req.param('source'), c.req.param('item'), body.access, context(c)), ItemBody),
    )

    app.delete(item, (c) =>
      answer(c, (caps) => caps.resetItem(c.req.param('source'), c.req.param('item'), context(c))),
    )

    // No body. One test or refresh per plugin at a time (plugins.ts `testAndList`): 429.
    app.post(`${one}/refresh`, (c) =>
      answer(c, async (caps) => {
        const { test, source } = await caps.refresh(c.req.param('source'))
        // The listing is `source`'s items: neither the stored copy nor the test's views are sent again.
        const { stored: _stored, tools: _tools, ...sent } = test
        return { test: sent, source }
      }),
    )
  },
}
