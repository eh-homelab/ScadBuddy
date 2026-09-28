import type { Hono } from 'hono'
import { z } from 'zod'
import {
  type McpAuthMode,
  type McpAuthSettings,
  SETTING_MCP_ANONYMOUS_CAP,
  SETTING_MCP_AUTH_MODE,
} from '../auth/authenticate.js'
import { type Tier, TIERS } from '../auth/principal.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'

// /api/v1/ai/mcp/auth (#251, spec §8.3): Settings reads and changes the `/mcp`
// auth mode and the cap on what an anonymous caller may do. Both are
// `ai_settings` keys (`mcp_auth_mode`, `mcp_anonymous_cap`) that /mcp reads on
// every request through auth/authenticate.ts `mcpAuthSettings`; GET answers
// through that same reader, so it reports what /mcp applies (an unknown stored
// value reads as the fail-closed value there, and so here).
//
//   GET   {mode, anonymous_cap}
//   PUT   {mode, anonymous_cap}: both keys in one transaction, so no request
//         sees the new mode with the old cap; answers what GET would
//
// Changing the mode is a settings write, so outward tier (spec §8.1, §8.3):
// PUT passes guard.ts `uiRequestProblem`, as the credential and token routes
// do, and GET passes `uiReadProblem`. `disabled` serves every caller that can
// reach /mcp over HTTPS as `anonymous`, up to the cap (full access by default,
// spec §8.3); outward calls still wait for a human approval in the UI (§8.2),
// which is independent of auth. The UI asks for an explicit confirmation
// before it sends `disabled`; this route takes the operator's word. Each
// change is logged with the peer address.
//
// `oidc` is not set here: the mode may be switched to it only once a discovery
// check against the issuer passes (spec §8.3), which comes with its
// configuration (#262). A stored `oidc` is still reported as it is.
//
// Error bodies are `{ detail }`, like routes/credentials.ts.

/** What this route writes: credentials.ts `SettingsStore` is one. */
export type SettingsWriter = { setMany(values: Record<string, unknown>): Promise<void> }

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
  mode: McpAuthMode
  /** The most an anonymous caller may do while the mode is `disabled`. */
  anonymous_cap: Tier
}

/** The modes this route sets. */
export const SETTABLE_MCP_AUTH_MODES = ['bearer', 'disabled'] as const

const PutBody = z.strictObject({
  mode: z.enum(SETTABLE_MCP_AUTH_MODES),
  anonymous_cap: z.enum(TIERS),
})

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const UNREADABLE = 'the MCP auth settings cannot be read; /mcp refuses every request until they can'
const OIDC_HERE =
  'mode: "oidc" cannot be set here; it is switched on with the OIDC configuration once its discovery check passes (#262)'

function view(settings: McpAuthSettings): McpAuthView {
  return { mode: settings.mode, anonymous_cap: settings.anonymousCap }
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
    const before = await current()
    await w.setMany({ [SETTING_MCP_AUTH_MODE]: body.mode, [SETTING_MCP_ANONYMOUS_CAP]: body.anonymous_cap })
    const after = await current()
    if (!after) return c.json({ detail: UNREADABLE }, 503)
    if (before?.mode !== after.mode || before.anonymousCap !== after.anonymousCap) {
      log(
        `mcp auth: set to mode ${after.mode}, anonymous cap ${after.anonymousCap}` +
          ` (was ${before ? `${before.mode}, ${before.anonymousCap}` : 'unreadable'}; from ${deps.remoteAddress(c) ?? 'unknown peer'})`,
      )
    }
    return c.json(view(after))
  })
}
