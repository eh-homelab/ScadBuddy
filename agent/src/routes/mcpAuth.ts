import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import {
  defaultOidcConfig,
  DiscoveryError,
  type DiscoveryReport,
  mcpResourceUri,
  type OidcConfig,
  type OidcConfigRepo,
  OidcConfigSchema,
  type OidcProvider,
  resourceMetadataUrl,
  SUPPORTED_ALGORITHMS,
} from '../auth/oidc.js'
import { EgressError } from '../http/egress.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/mcp/oidc (issue #262): the OIDC configuration for `/mcp`,
// stored in `ai_settings` (src/auth/oidc.ts `SettingsOidcConfigRepo`).
//
//   GET          the stored configuration (or the defaults), the resource URI
//                and metadata URL clients will see, and whether it can be enabled
//   PUT          saves it. `enabled: true` is accepted only after a discovery
//                check against the issuer passes, run now: the metadata and the
//                JWKS must both be fetched (issue #262: "so a typo can't lock out
//                every client")
//   POST /test   runs that check for `{ issuer }` without saving
//
// Writes are settings writes, outward tier (spec §8.1, §8.3): they pass the same
// interim UI gate as the credential routes (guard.ts), until approvals (#258).
// Nothing here is secret. Error bodies are `{ detail }`, as in credentials.ts.

export type McpAuthRouteDeps = {
  /** Undefined when there is no database (spec §9, "No database"). */
  repo: OidcConfigRepo | undefined
  ready: () => Promise<boolean>
  provider: OidcProvider
  /** SCADBUDDY_PUBLIC_URL; OIDC cannot be enabled without it. */
  publicUrl: string | undefined
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type OidcView = {
  config: OidcConfig
  /** False until a configuration has been saved. */
  saved: boolean
  /** What tokens must carry in `aud` unless `audience` is set: `<SCADBUDDY_PUBLIC_URL>/mcp`. */
  resource: string | null
  resource_metadata_url: string | null
  supported_algorithms: readonly string[]
  can_enable: boolean
  cannot_enable_reason: string | null
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const NO_PUBLIC_URL =
  'SCADBUDDY_PUBLIC_URL is not set: /mcp has no resource URI for tokens to name as their audience (RFC 8707)'

const TestBody = z.strictObject({ issuer: z.url({ protocol: /^https?$/ }).max(2048) })

function zodDetail(err: unknown): string {
  return err instanceof z.ZodError
    ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
    : 'body is not valid JSON'
}

function discoveryDetail(err: unknown): string | undefined {
  if (err instanceof DiscoveryError || err instanceof EgressError) return err.message
  return undefined
}

export function registerMcpAuthRoutes(app: Hono, deps: McpAuthRouteDeps): void {
  const base = '/api/v1/ai/mcp/oidc'

  async function repo(): Promise<OidcConfigRepo | string> {
    if (!deps.repo) return NO_DATABASE
    return (await deps.ready()) ? deps.repo : NOT_READY
  }

  function view(config: OidcConfig | undefined): OidcView {
    return {
      config: config ?? defaultOidcConfig(),
      saved: config !== undefined,
      resource: deps.publicUrl ? mcpResourceUri(deps.publicUrl) : null,
      resource_metadata_url: deps.publicUrl ? resourceMetadataUrl(deps.publicUrl) : null,
      supported_algorithms: SUPPORTED_ALGORITHMS,
      can_enable: deps.publicUrl !== undefined,
      cannot_enable_reason: deps.publicUrl ? null : NO_PUBLIC_URL,
    }
  }

  /** The discovery check, or a `{ detail }` saying why it failed. */
  async function discover(issuer: string): Promise<DiscoveryReport | { detail: string }> {
    try {
      return await deps.provider.test(issuer)
    } catch (err) {
      const detail = discoveryDetail(err)
      if (detail === undefined) throw err
      return { detail: `discovery against ${issuer} failed: ${detail}` }
    }
  }

  app.get(base, async (c) => {
    const r = await repo()
    if (typeof r === 'string') return c.json({ detail: r }, 503)
    return c.json(view(await r.get()))
  })

  app.on(['PUT', 'DELETE', 'POST'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress)
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })

  app.put(base, async (c) => {
    const r = await repo()
    if (typeof r === 'string') return c.json({ detail: r }, 503)
    let config: OidcConfig
    try {
      config = OidcConfigSchema.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: zodDetail(err) }, 400)
    }
    let discovery: DiscoveryReport | null = null
    if (config.enabled) {
      if (!deps.publicUrl) return c.json({ detail: `OIDC cannot be enabled: ${NO_PUBLIC_URL}` }, 409)
      const result = await discover(config.issuer)
      if ('detail' in result) {
        return c.json({ detail: `OIDC was not enabled (nothing was saved): ${result.detail}` }, 400)
      }
      discovery = result
    }
    await r.put(config, { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) })
    deps.provider.forget(config.issuer)
    return c.json({ ...view(config), discovery })
  })

  app.post(`${base}/test`, async (c) => {
    let body: z.infer<typeof TestBody>
    try {
      body = TestBody.parse(await c.req.json())
    } catch (err) {
      return c.json({ detail: zodDetail(err) }, 400)
    }
    const result = await discover(body.issuer)
    if ('detail' in result) return c.json(result, 400)
    return c.json(result)
  })
}

declare module '../app.js' {
  interface AppDeps {
    /**
     * The OIDC settings routes for /mcp (#262). Left out, there are none. `repo` is
     * undefined exactly when `database` is.
     */
    mcpOidc?: Pick<McpAuthRouteDeps, 'repo' | 'provider' | 'publicUrl'> | undefined
  }
}

/** The OIDC settings routes for /mcp (#262), when `mcpOidc` is given. */
export const route: RouteModule = {
  register(app, deps) {
    if (!deps.mcpOidc) return
    registerMcpAuthRoutes(app, {
      ...deps.mcpOidc,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
