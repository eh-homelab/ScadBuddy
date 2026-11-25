import type { Hono } from 'hono'
import { z } from 'zod'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import {
  DEFAULT_ANONYMOUS_NETWORKS,
  type McpAuthMode,
  type McpAuthSettings,
  mcpAuthSettings,
  SETTING_MCP_ANONYMOUS_CAP,
  SETTING_MCP_ANONYMOUS_NETWORKS,
  SETTING_MCP_AUTH_MODE,
  type SettingsReader,
} from '../auth/authenticate.js'
import { type Tier, TIERS } from '../auth/principal.js'
import { forwardedClient, type OriginPolicy, parseCidr } from '../http/origins.js'
import { type RemoteAddress, requestFacts, uiReadProblem, uiRequestProblem } from './guard.js'
import { mcpAuthOf, ready, type RouteModule } from './module.js'

// /api/v1/ai/mcp/auth (#251, spec §8.3): Settings reads and changes the `/mcp`
// auth mode, the cap on what an anonymous caller may do, and the networks an
// anonymous caller must come from (#7767). They are `ai_settings` keys
// (`mcp_auth_mode`, `mcp_anonymous_cap`, `mcp_anonymous_networks`) that /mcp reads on
// every request through auth/authenticate.ts `mcpAuthSettings`; GET answers
// through that same reader, so it reports what /mcp applies (an unknown stored
// value reads as the fail-closed value there, and so here).
//
//   GET   {mode, configured_mode, anonymous_cap, anonymous_networks,
//          default_anonymous_networks}
//         `mode` is what /mcp applies; `configured_mode` is what the
//         `mcp_auth_mode` key gives (`bearer` or `disabled`). They differ while
//         an enabled OIDC configuration (#262, `ai_settings.mcp_oidc`) makes the
//         mode `oidc`: OIDC wins even over a stored `disabled`.
//         `anonymous_networks` is the list /mcp applies (the default when the
//         key is unset, empty when the stored value is not a list of networks);
//         `default_anonymous_networks` is the default.
//   PUT   {mode, anonymous_cap, anonymous_networks?, expected: {mode,
//          anonymous_cap, anonymous_networks?}}: the keys in one transaction,
//         so no request sees the new mode with the old cap or networks.
//         A compare-and-set: `expected` is what the page showed, and a stored
//         value that no longer matches answers 409 and writes nothing, so a
//         stale Settings tab cannot turn auth off without the confirmation the
//         current state would have asked for. Without `anonymous_networks`
//         the stored networks stay as they are; with it, `expected` must name
//         the networks the page showed. Each network is an IP address or a
//         CIDR range, at most MAX_NETWORKS of them; anything else is a 400.
//         Answers what GET would.
//
// Changing the mode is a settings write, so outward tier (spec §8.1, §8.3):
// PUT passes guard.ts `uiRequestProblem`, as the credential and token routes
// do, and GET passes `uiReadProblem`. `disabled` serves a caller that reaches
// /mcp over HTTPS from the anonymous networks as `anonymous`, up to the cap
// (full access by default, spec §8.3), and asks any other caller for a bearer
// token, as `bearer` mode does (#7767). Outward calls still wait for a human
// approval in the UI (§8.2), which is independent of auth. The UI asks for an explicit confirmation
// before it sends `disabled`; this route takes the operator's word. Each
// change is logged as soon as it commits, with the client a trusted proxy
// names in `X-Forwarded-For` and the socket peer.
//
// `oidc` is not set here: it is on while the OIDC configuration is enabled
// (#262, routes/mcpAuth.ts). A stored `oidc` without an enabled configuration
// reads as `bearer`, as /mcp applies it.
//
// Error bodies are `{ detail }`, like routes/credentials.ts.

/** What this route writes: credentials.ts `SettingsStore` is one. */
export type SettingsWriter = {
  /** Writes `values` only when `check`, reading inside the same transaction, returns true. */
  /** `context` says who wrote them, for the audit rows (#831). */
  setMany(
    values: Record<string, unknown>,
    check: ((current: SettingsReader) => Promise<boolean>) | undefined,
    context: AuditContext,
  ): Promise<boolean>
}

export type McpAuthModeRouteDeps = {
  /** Undefined when this process was not given it (tests). */
  settings: SettingsWriter | undefined
  /** The reader /mcp uses (auth/authenticate.ts `mcpAuthSettings`). */
  authSettings: () => McpAuthSettings | Promise<McpAuthSettings>
  /** True once the API serves the agent's CRUD areas; the routes answer 503 until then. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  /** Where a change is recorded; console.log when omitted. */
  log?: (line: string) => void
}

export type McpAuthView = {
  /** What /mcp applies. */
  mode: McpAuthMode
  /** What `mcp_auth_mode` gives; `mode` differs from it while OIDC is enabled. */
  configured_mode: SettableMcpAuthMode
  /** The most an anonymous caller may do while the mode is `disabled`. */
  anonymous_cap: Tier
  /** The networks an anonymous caller must come from while the mode is `disabled` (#7767). */
  anonymous_networks: string[]
  /** What `anonymous_networks` is when it was never set. */
  default_anonymous_networks: string[]
}

/** The modes this route sets. */
export const SETTABLE_MCP_AUTH_MODES = ['bearer', 'disabled'] as const
export type SettableMcpAuthMode = (typeof SETTABLE_MCP_AUTH_MODES)[number]

/** The most networks the list may hold. */
export const MAX_NETWORKS = 64

const Network = z
  .string()
  .trim()
  .superRefine((value, ctx) => {
    if (!parseCidr(value)) ctx.addIssue({ code: 'custom', message: `${JSON.stringify(value)} is not an IP address or CIDR range` })
  })

const Setting = z.strictObject({
  mode: z.enum(SETTABLE_MCP_AUTH_MODES),
  anonymous_cap: z.enum(TIERS),
  anonymous_networks: z.array(Network).max(MAX_NETWORKS).optional(),
})

const PutBody = Setting.extend({ expected: Setting })

const NOT_CONFIGURED = 'AI features are not set up in this process'
const NOT_READY = "AI unavailable: the API does not serve the agent's CRUD areas yet"
const UNREADABLE = 'the MCP auth settings cannot be read; /mcp refuses every request until they can'
const CHANGED =
  'the MCP auth setting changed since this page loaded it; nothing was saved. Reload it and choose again'
const SAVED_UNREADABLE =
  'saved, but the MCP auth settings cannot be read back; /mcp refuses every request until they can'
const NETWORKS_UNEXPECTED =
  'expected.anonymous_networks: required when anonymous_networks is sent (the networks this page showed)'
const OIDC_HERE =
  'mode: "oidc" cannot be set here; it is switched on with the OIDC configuration once its discovery check passes (#262)'

function configured(settings: McpAuthSettings): SettableMcpAuthMode {
  return settings.configuredMode ?? (settings.mode === 'disabled' ? 'disabled' : 'bearer')
}

function view(settings: McpAuthSettings): McpAuthView {
  return {
    mode: settings.mode,
    configured_mode: configured(settings),
    anonymous_cap: settings.anonymousCap,
    anonymous_networks: [...settings.anonymousNetworks],
    default_anonymous_networks: [...DEFAULT_ANONYMOUS_NETWORKS],
  }
}

function sameNetworks(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((entry, i) => entry === b[i])
}

function networksText(networks: readonly string[]): string {
  return networks.length ? networks.join(' ') : 'none'
}

/** An address for a log line: anything else a header could carry becomes `?`. */
function logSafe(address: string): string {
  return address.replace(/[^\w.:[\]-]/g, '?').slice(0, 64)
}

function zodDetail(err: unknown): string {
  return err instanceof z.ZodError
    ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
    : 'body is not valid JSON'
}

export function registerMcpAuthModeRoutes(app: Hono, deps: McpAuthModeRouteDeps): void {
  const base = '/api/v1/ai/mcp/auth'
  const log = deps.log ?? ((line: string) => console.log(line))

  async function writer(): Promise<SettingsWriter | string> {
    if (!deps.settings) return NOT_CONFIGURED
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  async function current(): Promise<McpAuthSettings | undefined> {
    try {
      return await deps.authSettings()
    } catch {
      return undefined
    }
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'MCP auth reads')
    if (problem) return c.json({ detail: problem }, 403)
    const w = await writer()
    if (typeof w === 'string') return c.json({ detail: w }, 503)
    const settings = await current()
    if (!settings) return c.json({ detail: UNREADABLE }, 503)
    c.header('Cache-Control', 'no-store')
    return c.json(view(settings))
  })

  app.on(['POST', 'DELETE', 'PUT', 'PATCH'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'MCP auth changes')
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })

  app.put(base, async (c) => {
    const w = await writer()
    if (typeof w === 'string') return c.json({ detail: w }, 503)
    let raw: unknown
    try {
      raw = await c.req.json()
    } catch {
      return c.json({ detail: 'body is not valid JSON' }, 400)
    }
    if ((raw as { mode?: unknown } | null)?.mode === 'oidc') return c.json({ detail: OIDC_HERE }, 400)
    let body: z.infer<typeof PutBody>
    try {
      body = PutBody.parse(raw)
    } catch (err) {
      return c.json({ detail: zodDetail(err) }, 400)
    }
    const networks = body.anonymous_networks
    const expectedNetworks = body.expected.anonymous_networks
    if (networks !== undefined && expectedNetworks === undefined) return c.json({ detail: NETWORKS_UNEXPECTED }, 400)
    const values: Record<string, unknown> = {
      [SETTING_MCP_AUTH_MODE]: body.mode,
      [SETTING_MCP_ANONYMOUS_CAP]: body.anonymous_cap,
    }
    if (networks !== undefined) values[SETTING_MCP_ANONYMOUS_NETWORKS] = networks
    // The keys as the page last saw them, resolved as /mcp resolves them
    // (without OIDC, which this route does not write), read under the lock.
    let before: McpAuthSettings | undefined
    const written = await w.setMany(
      values,
      async (stored) => {
        before = await mcpAuthSettings(stored, () => {})()
        return (
          configured(before) === body.expected.mode &&
          before.anonymousCap === body.expected.anonymous_cap &&
          (expectedNetworks === undefined || sameNetworks(before.anonymousNetworks, expectedNetworks))
        )
      },
      { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) },
    )
    if (!written) return c.json({ detail: CHANGED }, 409)
    const networksChanged = before !== undefined && networks !== undefined && !sameNetworks(before.anonymousNetworks, networks)
    if (before && (configured(before) !== body.mode || before.anonymousCap !== body.anonymous_cap || networksChanged)) {
      const facts = requestFacts(c, deps.remoteAddress)
      const client = forwardedClient(facts, deps.origins)
      const peer = logSafe(facts.peer ?? 'unknown peer')
      const now = networksChanged ? `, anonymous networks ${networksText(networks!)}` : ''
      const was = networksChanged ? `, ${networksText(before.anonymousNetworks)}` : ''
      log(
        `mcp auth: mcp_auth_mode set to ${body.mode}, anonymous cap ${body.anonymous_cap}${now}` +
          ` (was ${configured(before)}, ${before.anonymousCap}${was}; from ${client ? `${logSafe(client)} via ${peer}` : peer})`,
      )
    }
    const after = await current()
    if (!after) return c.json({ detail: SAVED_UNREADABLE }, 503)
    return c.json(view(after))
  })
}

declare module '../app.js' {
  interface AppDeps {
    /**
     * Where Settings writes the /mcp auth mode, anonymous cap and anonymous networks (credentials.ts
     * `SettingsStore`). The routes read them back through `mcp.authSettings`.
     * Undefined (or left out) when this process was not given it (tests): the routes then answer 503.
     */
    aiSettings?: SettingsWriter | undefined
  }
}

/** The /mcp auth-mode routes (#251). */
export const route: RouteModule = {
  register(app, deps) {
    registerMcpAuthModeRoutes(app, {
      settings: deps.database ? deps.aiSettings : undefined,
      authSettings: mcpAuthOf(deps),
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
