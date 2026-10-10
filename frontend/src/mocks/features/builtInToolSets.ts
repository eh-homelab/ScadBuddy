import { HttpResponse, http } from 'msw'
import type { BuiltInToolSet, BuiltInToolSetPatch, RiskTier } from '../../api/aiPlugins'
import { remotePluginList } from '../aiPlugins'

/**
 * #1953 — ScadBuddy's own tool sets in `GET /api/v1/ai/plugins` (agent/src/routes/plugins.ts,
 * agent/src/plugins/builtInTools.ts), listed before the remote plugins (`../aiPlugins.ts`). The
 * rules the UI depends on are kept: a tier may be raised, never lowered below the tool's own
 * (400); only a set with a switch (`playwright`) can be enabled or disabled; adding, removing or
 * testing one answers 409 with `built_in: true`. Any other plugin name that `../aiPlugins.ts`
 * does not know ends here as a 404.
 */

const base = '/api/v1/ai/plugins'
const TIERS: RiskTier[] = ['read', 'write', 'outward']

function initial(): BuiltInToolSet[] {
  return [
    {
      name: 'scadbuddy',
      kind: 'built_in',
      built_in: true,
      enabled: true,
      switchable: false,
      tool_prefix: 'mcp__scadbuddy__',
      tools: [
        { name: 'list_models', harness_name: 'mcp__scadbuddy__list_models', risk: 'read' },
        { name: 'save_model', harness_name: 'mcp__scadbuddy__save_model', risk: 'write' },
        { name: 'send_to_printer', harness_name: 'mcp__scadbuddy__send_to_printer', risk: 'outward' },
      ],
      tool_tiers: {},
      disabled_tools: [],
    },
    {
      name: 'playwright',
      kind: 'built_in',
      built_in: true,
      enabled: false,
      switchable: true,
      tool_prefix: 'mcp__plugin_playwright_playwright__',
      tools: [
        { name: 'browser_navigate', harness_name: 'mcp__plugin_playwright_playwright__browser_navigate', risk: 'read' },
        { name: 'browser_click', harness_name: 'mcp__plugin_playwright_playwright__browser_click', risk: 'write' },
      ],
      tool_tiers: {},
      disabled_tools: [],
    },
  ]
}

const state = { sets: new Map<string, BuiltInToolSet>() }

export function reset(): void {
  state.sets = new Map(initial().map((s) => [s.name, s]))
}
reset()

const builtIn = (name: string, what: string) =>
  HttpResponse.json({ detail: `"${name}" is built in: it cannot be ${what}`, built_in: true }, { status: 409 })
const bad = (detail: string) => HttpResponse.json({ detail }, { status: 400 })
const missing = (name: string) => HttpResponse.json({ detail: `no plugin named "${name}"` }, { status: 404 })

export const handlers = [
  http.get(base, () => HttpResponse.json([...state.sets.values(), ...remotePluginList()])),

  http.get(`${base}/:name`, ({ params }) => {
    const name = String(params.name)
    const set = state.sets.get(name)
    if (set) return HttpResponse.json(set)
    const remote = remotePluginList().find((p) => p.name === name)
    return remote ? HttpResponse.json(remote) : missing(name)
  }),

  http.patch(`${base}/:name`, async ({ params, request }) => {
    const name = String(params.name)
    const set = state.sets.get(name)
    if (!set) return missing(name)
    const body = (await request.json()) as BuiltInToolSetPatch & Record<string, unknown>
    if ('url' in body || 'auth_header' in body || 'secret' in body) return builtIn(name, 'given a URL, an auth header or a secret')
    if (body.enabled !== undefined && !set.switchable) return builtIn(name, 'switched off as a whole')
    const risk = new Map(set.tools.map((t) => [t.name, t.risk]))
    let tool_tiers = set.tool_tiers
    if (body.tool_tiers) {
      tool_tiers = {}
      for (const [tool, tier] of Object.entries(body.tool_tiers)) {
        const own = risk.get(tool)
        if (!own) return bad(`tool_tiers: ${name} has no tool "${tool}"`)
        if (TIERS.indexOf(tier) < TIERS.indexOf(own)) {
          return bad(`tool_tiers.${tool}: ${tool} is "${own}" in ScadBuddy's code; a built-in tool's tier can be raised, never lowered below that`)
        }
        if (tier !== own) tool_tiers[tool] = tier
      }
    }
    if (body.disabled_tools?.some((t) => !risk.has(t))) return bad(`disabled_tools: ${name} has no such tool`)
    const next: BuiltInToolSet = {
      ...set,
      tool_tiers,
      ...(body.disabled_tools ? { disabled_tools: [...new Set(body.disabled_tools)].sort() } : {}),
      ...(body.enabled === undefined ? {} : { enabled: body.enabled }),
    }
    state.sets.set(name, next)
    return HttpResponse.json(next)
  }),

  http.delete(`${base}/:name`, ({ params }) => {
    const name = String(params.name)
    return state.sets.has(name) ? builtIn(name, 'removed: disable its tools instead') : missing(name)
  }),

  http.post(`${base}/:name/test`, ({ params }) => {
    const name = String(params.name)
    return state.sets.has(name) ? builtIn(name, 'tested: it has no endpoint') : missing(name)
  }),
]
