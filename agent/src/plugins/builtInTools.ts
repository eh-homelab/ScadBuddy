import { BUILTIN_SOURCES, registrySource, type StoredOverrides, type ToolAccess } from '../access/model.js'
import { itemOverrides, registryOverrides } from '../access/resolve.js'
import { AccessResolver } from '../access/resolver.js'
import type { AccessReader, AccessWriter } from '../access/store.js'
import type { AuditContext } from '../audit/log.js'
import { RISK_TIERS, type RiskTier } from '../harness/permissions.js'
import { ALL_TOOLS } from '../tools/index.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { Tool, ToolOverrides } from '../tools/registry.js'
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
//
// Which sets exist is builtins.ts BUILT_INS; their tools come from the code
// that serves them, never from a list here.
//
// EITHER DIRECTION (#2171). A tool's tier in code is its default: an override may
// raise it, lower it, or disable the tool. Overrides live in `ai_access`, one row
// per changed tool (source `builtin:scadbuddy`, or `builtin:browser` for the
// browser_* tools), and are resolved on every read (access/resolve.ts): a row that
// names a tool that is gone is ignored, and a row the migration copied counts only
// where it still raises, since the old overrides could only raise.

export type BuiltInTool = { name: string; harness_name: string; risk: RiskTier }

export type BuiltInToolSet = {
  name: string
  tool_prefix: string
  tools: readonly BuiltInTool[]
  /** Whether the set as a whole has a switch: its builtins.ts source's switch in ai_access. */
  switchable: boolean
}

export type { StoredOverrides }

/** Where the registry tools' rows live. */
const REGISTRY_SOURCES = [BUILTIN_SOURCES.scadbuddy, BUILTIN_SOURCES.browser] as const

export function builtInToolsSetting(set: string): string {
  return `builtin_tools.${set}`
}

/**
 * The tools of each built-in, from the code that serves them. ScadBuddy's own
 * are always served (its built-in's switch is its skills').
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
}

export function builtInToolSets(tools: readonly Tool[] = ALL_TOOLS): BuiltInToolSet[] {
  return BUILT_INS.flatMap((b) => {
    const source = SOURCES[b.name]
    return source ? [{ name: b.name, ...source(tools) }] : []
  })
}

/** A patch checked against the set: known tools and valid tiers only, in either direction (400 otherwise). */
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
  /** The stored tier for `harnessName` where there is one (raised or lowered), else `tier` (what the seam resolved). */
  tierOf(harnessName: string, tier: RiskTier): RiskTier
  /** The disabled tools' harness names, for `disallowedTools`. */
  disallowed: readonly string[]
  /** Whether `set`'s tool `tool` (bare name) is disabled. */
  isDisabled(set: string, tool: string): boolean
  /** Every changed tier, raised or lowered, as `<harness name>=<tier>`, sorted: what a warm process's fingerprint covers (sessions/turnRunner.ts). */
  raised: readonly string[]
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
    tierOf: (harnessName, tier) => raised.get(harnessName) ?? tier,
    disallowed,
    isDisabled: (set, tool) => disabled.has(`${set}\u0000${tool}`),
    raised: [...raised].map(([name, tier]) => `${name}=${tier}`).sort(),
  }
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
  /** Every tool, with the tier its code gives it: its default. */
  tools: readonly BuiltInTool[]
  /** Changed tiers only, raised or lowered. */
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
}

export class BuiltInTools {
  readonly sets: readonly BuiltInToolSet[]
  readonly #access: (AccessReader & Partial<AccessWriter>) | undefined

  constructor(access: (AccessReader & Partial<AccessWriter>) | undefined, sets: readonly BuiltInToolSet[] = builtInToolSets()) {
    this.#access = access
    this.sets = sets
  }

  named(name: string): BuiltInToolSet | undefined {
    return this.sets.find((s) => s.name === name)
  }

  /** The set's overrides as the gates apply them now (`builtin:browser`'s switch folded in); none without a database. */
  async overrides(set: BuiltInToolSet): Promise<StoredOverrides> {
    return registryOverrides(set.tools, this.#access ? await this.#access.rows(REGISTRY_SOURCES) : [])
  }

  /** Each tool's own stored access, no switch folded in: what `update` patches and the view shows. */
  async stored(set: BuiltInToolSet): Promise<StoredOverrides> {
    return itemOverrides(set.tools, this.#access ? await this.#access.rows(REGISTRY_SOURCES) : [])
  }

  async view(set: BuiltInToolSet): Promise<BuiltInPluginView> {
    const builtIn = builtInNamed(set.name)
    const [overrides, enabled] = await Promise.all([
      this.stored(set),
      set.switchable && builtIn ? builtInEnabled(this.#access, builtIn) : Promise.resolve(true),
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

  /** Stores a checked patch (400 on an unknown tool or tier); omitted fields are kept. */
  async update(
    set: BuiltInToolSet,
    patch: { tool_tiers?: Record<string, string> | undefined; disabled_tools?: readonly string[] | undefined },
    context: AuditContext,
  ): Promise<BuiltInPluginView> {
    const replaceToolsOf = this.#access?.replaceToolsOf?.bind(this.#access)
    if (!replaceToolsOf) throw new PluginError('built-in tool settings need the database', 503)
    const next = validateOverrides(set, patch, await this.stored(set))
    // A tool's own tier is not stored; a disabled tool is `off`, whatever its tier.
    const rows = new Map<string, Record<string, ToolAccess>>(REGISTRY_SOURCES.map((source) => [source, {}]))
    for (const [tool, tier] of Object.entries(next.tool_tiers)) rows.get(registrySource(tool))![tool] = tier
    for (const tool of next.disabled_tools) rows.get(registrySource(tool))![tool] = 'off'
    // Both sources in one transaction; one whose rows do not change is left alone.
    await replaceToolsOf(Object.fromEntries(rows), context)
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
    return new AccessResolver(this.#access, this.named(OWN_PLUGIN_NAME)?.tools ?? []).registryOverrides()
  }
}
