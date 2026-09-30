import type { Hono } from 'hono'
import { z } from 'zod'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import {
  type McpAuthMode,
  type McpAuthSettings,
  mcpAuthSettings,
  SETTING_MCP_ANONYMOUS_CAP,
  SETTING_MCP_AUTH_MODE,
  type SettingsReader,
} from '../auth/authenticate.js'
import { type Tier, TIERS } from '../auth/principal.js'
import { forwardedClient, type OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, requestFacts, uiReadProblem, uiRequestProblem } from './guard.js'
import { mcpAuthOf, ready, type RouteModule } from './module.js'

// /api/v1/ai/mcp/auth (#251, spec §8.3): Settings reads and changes the `/mcp`
// auth mode and the cap on what an anonymous caller may do. Both are
// `ai_settings` keys (`mcp_auth_mode`, `mcp_anonymous_cap`) that /mcp reads on
// every request through auth/authenticate.ts `mcpAuthSettings`; GET answers
// through that same reader, so it reports what /mcp applies (an unknown stored
// value reads as the fail-closed value there, and so here).
//
//   GET   {mode, configured_mode, anonymous_cap}
//         `mode` is what /mcp applies; `configured_mode` is what the
//         `mcp_auth_mode` key gives (`bearer` or `disabled`). They differ while
//         an enabled OIDC configuration (#262, `ai_settings.mcp_oidc`) makes the
//         mode `oidc`: OIDC wins even over a stored `disabled`.
//   PUT   {mode, anonymous_cap, expected: {mode, anonymous_cap}}: both keys in
//         one transaction, so no request sees the new mode with the old cap.
//         A compare-and-set: `expected` is the `configured_mode` and cap the
//         page showed, and a stored value that no longer matches answers 409
//         and writes nothing, so a stale Settings tab cannot turn auth off
//         without the confirmation the current state would have asked for.
//         Answers what GET would.
//
// Changing the mode is a settings write, so outward tier (spec §8.1, §8.3):
// PUT passes guard.ts `uiRequestProblem`, as the credential and token routes
// do, and GET passes `uiReadProblem`. `disabled` serves every caller that can
// reach /mcp over HTTPS as `anonymous`, up to the cap (full access by default,
// spec §8.3); outward calls still wait for a human approval in the UI (§8.2),
// which is independent of auth. The UI asks for an explicit confirmation
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
    check: (current: SettingsReader) => Promise<boolean>,
    context: AuditContext,
  ): Promise<boolean>
}

export type McpAuthModeRouteDeps = {
  /** Undefined when there is no database (spec §9, "No database"). */
  settings: SettingsWriter | undefined
  /** The reader /mcp uses (auth/authenticate.ts `mcpAuthSettings`). */
  authSettings: () => McpAuthSettings | Promise<McpAuthSettings>
  /** Applies migrations; the routes answer 503 until it resolves true. */
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
}

/** The modes this route sets. */
export const SETTABLE_MCP_AUTH_MODES = ['bearer', 'disabled'] as const
export type SettableMcpAuthMode = (typeof SETTABLE_MCP_AUTH_MODES)[number]

const Setting = z.strictObject({
  mode: z.enum(SETTABLE_MCP_AUTH_MODES),
  anonymous_cap: z.enum(TIERS),
})

const PutBody = Setting.extend({ expected: Setting })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const UNREADABLE = 'the MCP auth settings cannot be read; /mcp refuses every request until they can'
const CHANGED =
  'the MCP auth setting changed since this page loaded it; nothing was saved. Reload it and choose again'
const SAVED_UNREADABLE =
  'saved, but the MCP auth settings cannot be read back; /mcp refuses every request until they can'
const OIDC_HERE =
  'mode: "oidc" cannot be set here; it is switched on with the OIDC configuration once its discovery check passes (#262)'

function configured(settings: McpAuthSettings): SettableMcpAuthMode {
  return settings.configuredMode ?? (settings.mode === 'disabled' ? 'disabled' : 'bearer')
}

function view(settings: McpAuthSettings): McpAuthView {
  return { mode: settings.mode, configured_mode: configured(settings), anonymous_cap: settings.anonymousCap }
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
    if (!deps.settings) return NO_DATABASE
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
    // The keys as the page last saw them, resolved as /mcp resolves them
    // (without OIDC, which this route does not write), read under the lock.
    let before: McpAuthSettings | undefined
    const written = await w.setMany(
      { [SETTING_MCP_AUTH_MODE]: body.mode, [SETTING_MCP_ANONYMOUS_CAP]: body.anonymous_cap },
      async (stored) => {
        before = await mcpAuthSettings(stored, () => {})()
        return configured(before) === body.expected.mode && before.anonymousCap === body.expected.anonymous_cap
      },
      { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) },
    )
    if (!written) return c.json({ detail: CHANGED }, 409)
    if (before && (configured(before) !== body.mode || before.anonymousCap !== body.anonymous_cap)) {
      const facts = requestFacts(c, deps.remoteAddress)
      const client = forwardedClient(facts, deps.origins)
      const peer = logSafe(facts.peer ?? 'unknown peer')
      log(
        `mcp auth: mcp_auth_mode set to ${body.mode}, anonymous cap ${body.anonymous_cap}` +
          ` (was ${configured(before)}, ${before.anonymousCap}; from ${client ? `${logSafe(client)} via ${peer}` : peer})`,
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
     * Where Settings writes the /mcp auth mode and anonymous cap (credentials.ts
     * `SettingsStore`). The routes read them back through `mcp.authSettings`.
     * Undefined (or left out) when there is no database: the routes then answer 503.
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
