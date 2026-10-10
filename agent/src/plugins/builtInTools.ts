import type { AuditContext } from '../audit/log.js'
import {
  BROWSER_TOOL_TIERS,
  PLUGIN_NAME as BROWSER_PLUGIN_NAME,
  TOOL_PREFIX as BROWSER_TOOL_PREFIX,
} from '../harness/headlessBrowser.js'
import { raiseTier, RISK_TIERS, type RiskTier } from '../harness/permissions.js'
import { ALL_TOOLS } from '../tools/index.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { Tool, ToolOverride, ToolOverrides } from '../tools/registry.js'
import { BUILT_INS, builtInEnabled, builtInNamed } from './packages/builtins.js'
import { OWN_PLUGIN_NAME } from './packages/vet.js'
import { PluginError } from './registry.js'

// ScadBuddy's own tool sets as plugins (#1953): listed in /api/v1/ai/plugins
// beside the remote MCP plugins, with the same per-tool controls (a tier, and
// disabled), but never added, removed or pointed elsewhere.
//
//   - `scadbuddy`: every registry tool (tools/index.ts ALL_TOOLS), served
//     in-process to the harness as `mcp__scadbuddy__<name>`, over /mcp, and to
//     a durable session's `agent-tools` activities.
//   - `playwright`: the headless browser's tools (harness/headlessBrowser.ts
//     BROWSER_TOOL_TIERS), harness only.
//
// Which sets exist is builtins.ts BUILT_INS; their tools come from the code
// that serves them, never from a list here.
//
// TIGHTEN ONLY. A tool's tier in code is the least it runs at: an override may
// raise it (read → write → outward) or disable the tool, never lower it, so
// Settings can only make a tool ask more. Overrides live in `ai_settings`,
// one key per set (`builtin_tools.<name>`), and are re-checked on every read:
// an entry that no longer raises (the code's tier went up) or names a tool that
// is gone is ignored.

export type BuiltInTool = { name: string; harness_name: string; risk: RiskTier }

export type BuiltInToolSet = {
  name: string
  tool_prefix: string
  tools: readonly BuiltInTool[]
  /** Whether the set as a whole has a switch: builtins.ts's setting for it. */
  switchable: boolean
}

/** What Settings stores for one set: raised tiers and disabled tools, by bare tool name. */
export type StoredOverrides = { tool_tiers: Record<string, RiskTier>; disabled_tools: string[] }

const NONE: StoredOverrides = { tool_tiers: {}, disabled_tools: [] }

export function builtInToolsSetting(set: string): string {
  return `builtin_tools.${set}`
}

/**
 * The tools of each built-in, from the code that serves them. ScadBuddy's own
 * are always served (its built-in's switch is its skills'); the headless
 * browser's are on with the browser.
 */
const SOURCES: Readonly<Record<string, (tools: readonly Tool[]) => Omit<BuiltInToolSet, 'name'>>> = {
  [OWN_PLUGIN_NAME]: (tools) => {
    const prefix = `mcp__${SERVER_NAME}__`
    return {
      tool_prefix: prefix,
      tools: tools.map((t) => ({ name: t.name, harness_name: `${prefix}${t.name}`, risk: t.risk })),
      switchable: false,
    }
  },
  [BROWSER_PLUGIN_NAME]: () => ({
    tool_prefix: BROWSER_TOOL_PREFIX,
    tools: Object.entries(BROWSER_TOOL_TIERS).map(([name, risk]) => ({
      name,
      harness_name: `${BROWSER_TOOL_PREFIX}${name}`,
      risk,
    })),
    switchable: true,
  }),
}

export function builtInToolSets(tools: readonly Tool[] = ALL_TOOLS): BuiltInToolSet[] {
  return BUILT_INS.flatMap((b) => {
    const source = SOURCES[b.name]
    return source ? [{ name: b.name, ...source(tools) }] : []
  })
}

/** Keeps what still raises a known tool, so a stale or hand-edited row never lowers or invents anything. */
function sanitise(set: BuiltInToolSet, stored: unknown): StoredOverrides {
  if (typeof stored !== 'object' || stored === null) return NONE
  const raw = stored as { tool_tiers?: unknown; disabled_tools?: unknown }
  const risk = new Map(set.tools.map((t) => [t.name, t.risk]))
  const tool_tiers: Record<string, RiskTier> = {}
  if (typeof raw.tool_tiers === 'object' && raw.tool_tiers !== null) {
    for (const [tool, tier] of Object.entries(raw.tool_tiers)) {
      const own = risk.get(tool)
      if (own === undefined || !(RISK_TIERS as readonly unknown[]).includes(tier)) continue
      if (raiseTier(own, tier as RiskTier) !== own) tool_tiers[tool] = tier as RiskTier
    }
  }
  const disabled_tools = Array.isArray(raw.disabled_tools)
    ? [...new Set(raw.disabled_tools.filter((t): t is string => typeof t === 'string' && risk.has(t)))].sort()
    : []
  return { tool_tiers, disabled_tools }
}

/** A patch checked against the set: known tools only, and every tier at or above the tool's own (400 otherwise). */
export function validateOverrides(
  set: BuiltInToolSet,
  patch: { tool_tiers?: Record<string, string> | undefined; disabled_tools?: readonly string[] | undefined },
  current: StoredOverrides,
): StoredOverrides {
  const risk = new Map(set.tools.map((t) => [t.name, t.risk]))
  let tool_tiers = current.tool_tiers
  if (patch.tool_tiers !== undefined) {
    tool_tiers = {}
    for (const [tool, tier] of Object.entries(patch.tool_tiers)) {
      const own = risk.get(tool)
      if (own === undefined) throw new PluginError(`tool_tiers: ${set.name} has no tool "${tool}"`, 400)
      if (!(RISK_TIERS as readonly string[]).includes(tier)) {
        throw new PluginError(`tool_tiers.${tool} must be one of ${RISK_TIERS.join(', ')}`, 400)
      }
      if (raiseTier(own, tier as RiskTier) !== tier) {
        throw new PluginError(
          `tool_tiers.${tool}: ${tool} is "${own}" in ScadBuddy's code; a built-in tool's tier can be raised, ` +
            `never lowered below that`,
          400,
        )
      }
      // Its own tier is no override.
      if (tier !== own) tool_tiers[tool] = tier as RiskTier
    }
  }
  let disabled_tools = current.disabled_tools
  if (patch.disabled_tools !== undefined) {
    for (const tool of patch.disabled_tools) {
      if (!risk.has(tool)) throw new PluginError(`disabled_tools: ${set.name} has no tool "${tool}"`, 400)
    }
    disabled_tools = [...new Set(patch.disabled_tools)].sort()
  }
  return { tool_tiers, disabled_tools }
}

/**
 * Every built-in's overrides for one harness turn, by the names Claude Code
 * gives the tools. Read once per turn, as the remote plugins are.
 */
export type BuiltInPolicy = {
  /** `tier` (what the seam resolved for `harnessName`) raised by its override. */
  tierOf(harnessName: string, tier: RiskTier): RiskTier
  /** The disabled tools' harness names, for `disallowedTools`. */
  disallowed: readonly string[]
  /** Whether `set`'s tool `tool` (bare name) is disabled. */
  isDisabled(set: string, tool: string): boolean
}

export function builtInPolicy(entries: readonly { set: BuiltInToolSet; overrides: StoredOverrides }[]): BuiltInPolicy {
  const raised = new Map<string, RiskTier>()
  const disabled = new Set<string>()
  const disallowed: string[] = []
  for (const { set, overrides } of entries) {
    for (const [tool, tier] of Object.entries(overrides.tool_tiers)) raised.set(`${set.tool_prefix}${tool}`, tier)
    for (const tool of overrides.disabled_tools) {
      disabled.add(`${set.name}\u0000${tool}`)
      disallowed.push(`${set.tool_prefix}${tool}`)
    }
  }
  return {
    tierOf: (harnessName, tier) => raiseTier(tier, raised.get(harnessName)),
    disallowed,
    isDisabled: (set, tool) => disabled.has(`${set}\u0000${tool}`),
  }
}

type Settings = {
  get<T>(key: string): Promise<T | undefined>
  set(key: string, value: unknown, context: AuditContext): Promise<void>
}

/** A built-in tool set as /api/v1/ai/plugins lists it (routes/plugins.ts). */
export type BuiltInPluginView = {
  name: string
  kind: 'built_in'
  built_in: true
  /** The set's switch (builtins.ts); always true for a set without one. */
  enabled: boolean
  /** Whether `enabled` can be changed here: the set has a switch. */
  switchable: boolean
  tool_prefix: string
  /** Every tool, with the tier its code gives it: the least it can be set to. */
  tools: readonly BuiltInTool[]
  /** Raised tiers only. */
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
}

export class BuiltInTools {
  readonly sets: readonly BuiltInToolSet[]
  readonly #settings: Settings | undefined

  constructor(settings: Settings | undefined, sets: readonly BuiltInToolSet[] = builtInToolSets()) {
    this.#settings = settings
    this.sets = sets
  }

  named(name: string): BuiltInToolSet | undefined {
    return this.sets.find((s) => s.name === name)
  }

  /** The set's overrides as stored now; none without a database. */
  async overrides(set: BuiltInToolSet): Promise<StoredOverrides> {
    return sanitise(set, await this.#settings?.get<unknown>(builtInToolsSetting(set.name)))
  }

  async view(set: BuiltInToolSet): Promise<BuiltInPluginView> {
    const builtIn = builtInNamed(set.name)
    const [overrides, enabled] = await Promise.all([
      this.overrides(set),
      set.switchable && builtIn ? builtInEnabled(this.#settings, builtIn) : Promise.resolve(true),
    ])
    return {
      name: set.name,
      kind: 'built_in',
      built_in: true,
      enabled,
      switchable: set.switchable,
      tool_prefix: set.tool_prefix,
      tools: set.tools,
      tool_tiers: overrides.tool_tiers,
      disabled_tools: overrides.disabled_tools,
    }
  }

  list(): Promise<BuiltInPluginView[]> {
    return Promise.all(this.sets.map((s) => this.view(s)))
  }

  /** Stores a checked patch (400 on a lowered tier or an unknown tool); omitted fields are kept. */
  async update(
    set: BuiltInToolSet,
    patch: { tool_tiers?: Record<string, string> | undefined; disabled_tools?: readonly string[] | undefined },
    context: AuditContext,
  ): Promise<BuiltInPluginView> {
    if (!this.#settings) throw new PluginError('built-in tool settings need the database', 503)
    const next = validateOverrides(set, patch, await this.overrides(set))
    await this.#settings.set(builtInToolsSetting(set.name), next, context)
    return this.view(set)
  }

  /** Every set's overrides, for one harness turn. */
  async policy(): Promise<BuiltInPolicy> {
    return builtInPolicy(await Promise.all(this.sets.map(async (set) => ({ set, overrides: await this.overrides(set) }))))
  }

  /**
   * The registry's overrides (ToolServices `toolOverrides`): ScadBuddy's own
   * set, read at each call, so a change applies to the next call on every
   * path (the harness's server, /mcp, the durable activities).
   */
  registryOverrides(): ToolOverrides {
    const own = this.named(OWN_PLUGIN_NAME)
    const read = async (): Promise<StoredOverrides> => (own ? this.overrides(own) : NONE)
    return {
      of: async (tool): Promise<ToolOverride> => {
        const stored = await read()
        const tier = stored.tool_tiers[tool]
        return { ...(tier === undefined ? {} : { tier }), disabled: stored.disabled_tools.includes(tool) }
      },
      disabled: async () => new Set((await read()).disabled_tools),
    }
  }
}
