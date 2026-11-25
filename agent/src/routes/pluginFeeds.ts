import { createHash } from 'node:crypto'
import type { Context, Hono } from 'hono'
import type { AppDeps } from '../app.js'
import {
  authenticate,
  mcpAuthResolver,
  type McpAuthMode,
  type McpAuthSettings,
  mcpTransportProblem,
  type OidcRuntime,
} from '../auth/authenticate.js'
import { hasTier } from '../auth/principal.js'
import {
  ArchiveMaker,
  FEED_SLUG,
  type FeedEntry,
  feedEntry,
  FeedWriteError,
  marketplaceOf,
  marketplaceUrl,
  PACKAGE_NAME,
  pinKindOf,
  type PluginFeedReader,
  type Unlisted,
} from '../plugins/feed.js'
import { callerAddress, effectiveRequest, requestOrigin } from '../http/origins.js'
import { requestFacts, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// A marketplace feed of plugin packages (#6933, spec 2026-11-21 §6.4, plugins/feed.ts):
//
//   GET /api/v1/ai/plugin-feeds/{feed}/marketplace.json
//   GET /api/v1/ai/plugin-feeds/{feed}/plugins/{name}/versions/{N}/{sha256}.zip
//   GET /api/v1/ai/plugin-feeds/{feed}/plugins/{name}/archives/{sha256}.zip
//
// (the first, a hosted version's zip, named by its number and its zip's sha256, #6931 review
// M2; the last, #6936: a bytes pin's file, served from ScadBuddy's mirror as plugins/feed.ts
// `servedArchive` makes it, named by the served zip's sha256)
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
// front (Cloudflare) keeps an authenticated answer. A hosted zip's URL is made from the origin the
// `marketplace.json` request came on (its Host, or a trusted proxy's X-Forwarded-Proto and
// X-Forwarded-Host, as http/origins.ts `effectiveRequest` reads them), not SCADBUDDY_PUBLIC_URL:
// Claude Code sends the marketplace's `headers` only to archive downloads on the marketplace URL's
// own origin (https://code.claude.com/docs/en/plugins/host-marketplace.md#authenticate-archive-downloads),
// and prod's public URL is the SSO host, which a bearer token cannot pass. A zip request reads only
// its one package of the feed.
//
//   GET /api/v1/ai/plugin-feeds/{feed}
//
// is Settings → MCP access' read (UI-only, `uiReadProblem`): the feed's address, the auth mode,
// and every package the feed names with whether it is listed, and why not.
//
//   PUT    /api/v1/ai/plugin-feeds/{feed}/packages/{name}
//   DELETE /api/v1/ai/plugin-feeds/{feed}/packages/{name}
//
// add an installed package to the feed and take it out (#6936, spec §6.7, §6.8): settings writes
// from the UI only (`uiRequestProblem`, as the package routes), never an assistant's. The package
// keeps its own lifecycle (install, review, approval, re-pin through the package routes); the feed
// lists it only at an approved pin. A PUT answers 201 when it added the package, 200 when it was
// already in; a DELETE 204. A feed or package that is not there is 404. An API that does not
// serve the write route yet (a rolling deploy) is 503.

declare module '../app.js' {
  interface AppDeps {
    /** The feeds' reads (plugins/feed.ts); undefined when this process was not given them (tests). */
    pluginFeeds?: PluginFeedReader | undefined
  }
}

const BASE = '/api/v1/ai/plugin-feeds'
const VERSION = /^[1-9][0-9]{0,8}$/
const ARCHIVE_FILE = /^([0-9a-f]{64})\.zip$/
const FEED_CACHE = 'private, max-age=60'
const ZIP_CACHE = 'private, max-age=31536000, immutable'
const NOT_FOUND = 'no such plugin feed, plugin or version'

/** A package of the feed, as Settings shows it. */
export type PluginFeedPackageView = {
  name: string
  listed: boolean
  /** Why it is not listed; null when listed. */
  reason: Unlisted | null
  /** How it is listed (`hosted`, `git` or `archive`); null when it is not. */
  kind: Extract<FeedEntry, { listed: true }>['kind'] | null
  /** A hosted package's listed version. */
  version: number | null
  /** A git package's pinned commit. */
  commit: string | null
  /** An archive's bytes pin: the sha256 of the mirrored file. */
  archive: string | null
  /** Approved with its refused items (hooks, MCP servers) allowed: every client of the feed runs them. */
  allow_refused: boolean
}

export type PluginFeedView = {
  slug: string
  name: string
  owner: string
  /** What `/plugin marketplace add` takes, on the origin the Settings request came on (as the zip URLs are); null when that origin is unknown. */
  marketplace_url: string | null
  /** What /mcp, and so the feed, applies; null when the settings cannot be read. */
  auth_mode: McpAuthMode | null
  packages: PluginFeedPackageView[]
}

/** The auth mode /mcp applies; null when the settings cannot be read. */
async function authModeOf(read: () => McpAuthSettings | Promise<McpAuthSettings>): Promise<McpAuthMode | null> {
  try {
    return (await read()).mode
  } catch {
    return null
  }
}

function missing(c: Context) {
  return c.json({ error: NOT_FOUND }, 404, { 'Cache-Control': 'no-store' })
}

export const route: RouteModule = {
  register(app: Hono, deps: AppDeps) {
    const isReady = ready(deps)
    const mcp = deps.mcp
    const feeds = deps.pluginFeeds
    const archives = feeds ? new ArchiveMaker(feeds) : undefined
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
      const auth = await authenticate(c.req.raw, {
        settings,
        tokens,
        clientAddress: facts.peer,
        callerAddress: callerAddress(facts, deps.origins),
        oidc,
      })
      if (!auth.ok) return auth.response
      if (!hasTier(auth.principal, 'read')) return c.json({ error: 'reading the feed needs the read tier' }, 403)
      return undefined
    }

    /** The origin the request came on; the transport gate has already passed it. */
    function originOf(c: Context): string | undefined {
      const effective = effectiveRequest(requestFacts(c, deps.remoteAddress), deps.origins)
      return effective.scheme ? requestOrigin(effective.scheme, effective.host) : undefined
    }

    app.get(`${BASE}/:feed/marketplace.json`, async (c) => {
      const stop = await gate(c)
      if (stop) return stop
      const slug = c.req.param('feed')
      if (!FEED_SLUG.test(slug)) return missing(c)
      const origin = originOf(c)
      if (!origin) return c.json({ error: 'the request names no host to make the feed\'s URLs from' }, 400)
      const read = await feeds!.read(slug)
      if (!read.feed) return missing(c)
      const made = await archives!.resolve(read.packages)
      return c.json(marketplaceOf(read.feed, read.packages, origin, made), 200, { 'Cache-Control': FEED_CACHE })
    })

    app.get(`${BASE}/:feed/plugins/:name/versions/:version/:file`, async (c) => {
      const stop = await gate(c)
      if (stop) return stop
      const slug = c.req.param('feed')
      const name = c.req.param('name')
      const file = ARCHIVE_FILE.exec(c.req.param('file'))
      if (!FEED_SLUG.test(slug) || !PACKAGE_NAME.test(name) || !VERSION.test(c.req.param('version')) || !file) return missing(c)
      const version = Number(c.req.param('version'))
      const read = await feeds!.read(slug, name)
      const row = read.feed ? read.packages.find((p) => p.name === name) : undefined
      // Only whether and how it is listed matters here, not its URL. The URL's sha256 must be
      // the listed version's, so a URL of an earlier plugin of the same name is 404.
      const entry = row ? feedEntry(row, '', slug) : undefined
      if (!entry?.listed || entry.kind !== 'hosted' || entry.version !== version || row!.hosted!.zip_sha256 !== file[1]) {
        return missing(c)
      }
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

    app.get(`${BASE}/:feed/plugins/:name/archives/:file`, async (c) => {
      const stop = await gate(c)
      if (stop) return stop
      const slug = c.req.param('feed')
      const name = c.req.param('name')
      const file = ARCHIVE_FILE.exec(c.req.param('file'))
      if (!FEED_SLUG.test(slug) || !PACKAGE_NAME.test(name) || !file) return missing(c)
      const read = await feeds!.read(slug, name)
      const row = read.feed ? read.packages.find((p) => p.name === name) : undefined
      if (!row) return missing(c)
      // Only the current approved bytes pin, as the feed lists it, and only when the zip made
      // from it now is the one the URL names (its sha256), so an immutable URL never serves
      // other bytes. The zip comes from ArchiveMaker (kept, or made under its build limit).
      if (!row.approved || pinKindOf(row) !== 'bytes' || !row.bytes_sha256) return missing(c)
      let made
      try {
        made = await archives!.make(row)
      } catch {
        return c.json({ error: "the package's mirrored file cannot be served now" }, 502, { 'Cache-Control': 'no-store' })
      }
      if (!made || made.sha256 !== file[1]) return missing(c)
      return c.body(new Uint8Array(made.bytes), 200, {
        'Content-Type': 'application/zip',
        'Content-Disposition': `attachment; filename="${name}-${file[1]!.slice(0, 12)}.zip"`,
        'Cache-Control': ZIP_CACHE,
        ETag: `"${made.sha256}"`,
      })
    })

    /** A feed write's answer when the API refused it, or rethrows. */
    function refusedWrite(c: Context, err: unknown) {
      if (err instanceof FeedWriteError) return c.json({ detail: err.message, reason: err.reason }, err.reason === 'not-served' ? 503 : 404)
      throw err
    }

    /** The UI's guard for a feed write; a response when the request stops here. */
    async function writable(c: Context): Promise<Response | undefined> {
      const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'plugin feed changes')
      if (problem) return c.json({ detail: problem }, 403)
      if (!feeds) return c.json({ detail: 'the plugin feed is not served by this process' }, 503)
      if (!(await isReady())) return c.json({ detail: "AI unavailable: the API does not serve the agent's CRUD areas yet" }, 503)
      if (!FEED_SLUG.test(c.req.param('feed') ?? '') || !PACKAGE_NAME.test(c.req.param('name') ?? '')) {
        return c.json({ detail: NOT_FOUND }, 404)
      }
      return undefined
    }

    app.put(`${BASE}/:feed/packages/:name`, async (c) => {
      const stop = await writable(c)
      if (stop) return stop
      try {
        const added = await feeds!.add(c.req.param('feed'), c.req.param('name'))
        return c.json({ name: c.req.param('name'), added }, added ? 201 : 200)
      } catch (err) {
        return refusedWrite(c, err)
      }
    })

    app.delete(`${BASE}/:feed/packages/:name`, async (c) => {
      const stop = await writable(c)
      if (stop) return stop
      try {
        await feeds!.remove(c.req.param('feed'), c.req.param('name'))
        return c.body(null, 204)
      } catch (err) {
        return refusedWrite(c, err)
      }
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
      const authMode = mcp ? await authModeOf(mcp.authSettings) : null
      const origin = originOf(c)
      const made = await archives!.resolve(read.packages)
      const view: PluginFeedView = {
        ...read.feed,
        marketplace_url: origin ? marketplaceUrl(origin, slug) : null,
        auth_mode: authMode,
        packages: read.packages.map((p) => {
          const e = feedEntry(p, '', slug, made)
          return {
            name: p.name,
            listed: e.listed,
            reason: e.listed ? null : e.reason,
            kind: e.listed ? e.kind : null,
            version: e.listed && e.kind === 'hosted' ? e.version : null,
            commit: e.listed && e.kind === 'git' ? e.commit : null,
            archive: e.listed && e.kind === 'archive' ? e.pin : null,
            allow_refused: p.allow_refused ?? false,
          }
        }),
      }
      return c.json(view, 200, { 'Cache-Control': 'no-store' })
    })
  },
}
