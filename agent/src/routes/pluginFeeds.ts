import { createHash } from 'node:crypto'
import type { Context, Hono } from 'hono'
import type { AppDeps } from '../app.js'
import { authenticate, mcpAuthResolver, type McpAuthMode, mcpTransportProblem, type OidcRuntime } from '../auth/authenticate.js'
import { hasTier } from '../auth/principal.js'
import { FEED_SLUG, feedEntry, marketplaceOf, marketplaceUrl, PACKAGE_NAME, type PluginFeedReader } from '../plugins/feed.js'
import { requestFacts, uiReadProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// A marketplace feed of plugin packages (#6933, spec 2026-11-21 §6.4, plugins/feed.ts):
//
//   GET /api/v1/ai/plugin-feeds/{feed}/marketplace.json
//   GET /api/v1/ai/plugin-feeds/{feed}/plugins/{name}/versions/{N}.zip
//
// for Claude Code sessions outside ScadBuddy. Each request passes the gates /mcp passes, in the
// same order and through the same code (mcp/http.ts): HTTPS and Origin (`mcpTransportProblem`),
// then the auth mode and the principal (`authenticate`): an `sbmcp_` bearer token of any tier, an
// OIDC access token in `oidc` mode, or anyone in `disabled` mode up to the anonymous cap, which
// always includes `read`. A token is checked as every MCP call checks it (`last_used_at`
// stamped, a revoked or expired one refused at once). Settings that cannot be read fail closed.
//
// A package the feed does not list (not in it, no approved pin, a pending re-pin, a draft, a
// version that is not its approved pin) is 404 on these paths. The feed is `private,
// max-age=60`, so `/plugin marketplace update` sees a new pin within a minute; a version's zip
// never changes and is `private, max-age=31536000, immutable`. `private`, so no shared cache in
// front (Cloudflare) keeps an authenticated answer. Every URL in the feed is made from
// SCADBUDDY_PUBLIC_URL: these links leave the browser. Without it the feed is 503.
//
//   GET /api/v1/ai/plugin-feeds/{feed}
//
// is Settings → MCP access' read (UI-only, `uiReadProblem`): the feed's address, the auth mode,
// and every package the feed names with whether it is listed, and why not.

declare module '../app.js' {
  interface AppDeps {
    /** The feeds' reads (plugins/feed.ts); undefined when this process was not given them (tests). */
    pluginFeeds?: PluginFeedReader | undefined
  }
}

const BASE = '/api/v1/ai/plugin-feeds'
const ZIP_FILE = /^([1-9][0-9]{0,8})\.zip$/
const FEED_CACHE = 'private, max-age=60'
const ZIP_CACHE = 'private, max-age=31536000, immutable'
const NOT_FOUND = 'no such plugin feed, plugin or version'
const NO_PUBLIC_URL = 'SCADBUDDY_PUBLIC_URL is not set, so the feed has no address to give its plugins'

/** A package of the feed, as Settings shows it. */
export type PluginFeedPackageView = {
  name: string
  listed: boolean
  /** Why it is not listed: `not-approved`, `no-version` or `unpinnable`; null when listed. */
  reason: string | null
  /** `hosted` or `git` when listed. */
  kind: string | null
  /** A hosted package's listed version. */
  version: number | null
  /** A git package's pinned commit. */
  commit: string | null
}

export type PluginFeedView = {
  slug: string
  name: string
  owner: string
  /** What `/plugin marketplace add` takes; null without SCADBUDDY_PUBLIC_URL. */
  marketplace_url: string | null
  /** What /mcp, and so the feed, applies; null when the settings cannot be read. */
  auth_mode: McpAuthMode | null
  packages: PluginFeedPackageView[]
}

function missing(c: Context) {
  return c.json({ error: NOT_FOUND }, 404, { 'Cache-Control': 'no-store' })
}

export const route: RouteModule = {
  register(app: Hono, deps: AppDeps) {
    const isReady = ready(deps)
    const mcp = deps.mcp
    const feeds = deps.pluginFeeds
    const publicUrl = mcp?.publicUrl
    const resolveAuth = mcp ? mcpAuthResolver(mcp.authSettings, mcp.tokens) : undefined
    const oidc: OidcRuntime | undefined = mcp?.oidc ? { provider: mcp.oidc, publicUrl } : undefined

    /** The /mcp gates; a response when the request stops here. */
    async function gate(c: Context): Promise<Response | undefined> {
      if (!resolveAuth || !feeds) return c.json({ error: 'the plugin feed is not served by this process' }, 503)
      const facts = requestFacts(c, deps.remoteAddress)
      const refused = mcpTransportProblem(facts, deps.origins, c.req.url)
      if (refused) return refused
      if (!(await isReady())) return c.json({ error: "AI unavailable: the API does not serve the agent's CRUD areas yet" }, 503)
      const { settings, tokens } = await resolveAuth()
      const auth = await authenticate(c.req.raw, { settings, tokens, clientAddress: facts.peer, oidc })
      if (!auth.ok) return auth.response
      if (!hasTier(auth.principal, 'read')) return c.json({ error: 'reading the feed needs the read tier' }, 403)
      if (!publicUrl) return c.json({ error: NO_PUBLIC_URL }, 503)
      return undefined
    }

    app.get(`${BASE}/:feed/marketplace.json`, async (c) => {
      const stop = await gate(c)
      if (stop) return stop
      const slug = c.req.param('feed')
      if (!FEED_SLUG.test(slug)) return missing(c)
      const read = await feeds!.read(slug)
      if (!read.feed) return missing(c)
      return c.json(marketplaceOf(read.feed, read.packages, publicUrl!), 200, { 'Cache-Control': FEED_CACHE })
    })

    app.get(`${BASE}/:feed/plugins/:name/versions/:file`, async (c) => {
      const stop = await gate(c)
      if (stop) return stop
      const slug = c.req.param('feed')
      const name = c.req.param('name')
      const file = ZIP_FILE.exec(c.req.param('file'))
      if (!FEED_SLUG.test(slug) || !PACKAGE_NAME.test(name) || !file) return missing(c)
      const version = Number(file[1])
      const read = await feeds!.read(slug)
      const row = read.feed ? read.packages.find((p) => p.name === name) : undefined
      const entry = row ? feedEntry(row, publicUrl!, slug) : undefined
      if (!entry?.listed || entry.kind !== 'hosted' || entry.version !== version) return missing(c)
      const zip = await feeds!.zip(name, version)
      if (!zip) return missing(c)
      const sha256 = createHash('sha256').update(zip.bytes).digest('hex')
      if (sha256 !== row!.hosted!.zip_sha256) {
        console.error(`plugin feed: the zip of ${name} version ${version} does not match its sha256; not served`)
        return c.json({ error: "the version's zip does not match its recorded sha256" }, 502, { 'Cache-Control': 'no-store' })
      }
      return c.body(new Uint8Array(zip.bytes), 200, {
        'Content-Type': 'application/zip',
        'Content-Disposition': `attachment; filename="${name}-${version}.zip"`,
        'Cache-Control': ZIP_CACHE,
        ETag: `"${sha256}"`,
      })
    })

    app.get(`${BASE}/:feed`, async (c) => {
      const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'Plugin feed reads')
      if (problem) return c.json({ detail: problem }, 403)
      if (!feeds) return c.json({ detail: 'the plugin feed is not served by this process' }, 503)
      if (!(await isReady())) return c.json({ detail: "AI unavailable: the API does not serve the agent's CRUD areas yet" }, 503)
      const slug = c.req.param('feed')
      if (!FEED_SLUG.test(slug)) return c.json({ detail: NOT_FOUND }, 404)
      const read = await feeds.read(slug)
      if (!read.feed) return c.json({ detail: NOT_FOUND }, 404)
      const authMode: McpAuthMode | null = mcp
        ? await (async () => (await mcp.authSettings()).mode)().catch(() => null)
        : null
      const base = publicUrl ?? ''
      const view: PluginFeedView = {
        ...read.feed,
        marketplace_url: publicUrl ? marketplaceUrl(publicUrl, slug) : null,
        auth_mode: authMode,
        packages: read.packages.map((p) => {
          const e = feedEntry(p, base, slug)
          return {
            name: p.name,
            listed: e.listed,
            reason: e.listed ? null : e.reason,
            kind: e.listed ? e.kind : null,
            version: e.listed && e.kind === 'hosted' ? e.version : null,
            commit: e.listed && e.kind === 'git' ? e.commit : null,
          }
        }),
      }
      return c.json(view, 200, { 'Cache-Control': 'no-store' })
    })
  },
}
