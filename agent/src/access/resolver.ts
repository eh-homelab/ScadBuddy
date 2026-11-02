import type { RiskTier } from '../harness/permissions.js'
import { ALL_TOOLS } from '../tools/index.js'
import type { Tool, ToolOverrides } from '../tools/registry.js'
import { builtinItem, itemKindIn, strictest } from './items.js'
import { type AccessRow, BUILTIN_SOURCES, isToolAccess, packageSource, registrySource, type StoredOverrides, type ToolAccess } from './model.js'
import { isRegistrySource, pluginOverrides, registryOverrides, sourceEnabled, toolAccess } from './resolve.js'
import type { AccessReader } from './store.js'

/** One package's (or the own plugin's) items as stored: what is switched off, and each built-in's tier. */
export type PackageAccess = {
  off: { skills: Set<string>; subagents: Set<string>; hooks: Set<string>; builtins: Set<string> }
  builtinTiers: Map<string, ToolAccess>
}

const OFF_SET = { skill: 'skills', subagent: 'subagents', hook: 'hooks' } as const

/**
 * Every item's effective access (#2171, spec §4.4), read from `ai_access` at each call.
 * Without a reader everything is at its default. A read that fails rejects: the gates fail closed.
 */
export class AccessResolver {
  private readonly reader: AccessReader | undefined
  private readonly tools: readonly Pick<Tool, 'name' | 'risk'>[]
  private readonly risk: ReadonlyMap<string, RiskTier>
  constructor(reader: AccessReader | undefined, tools: readonly Pick<Tool, 'name' | 'risk'>[] = ALL_TOOLS) {
    this.reader = reader
    this.tools = tools
    this.risk = new Map(tools.map((t) => [t.name, t.risk]))
  }

  /** The registry tools' overrides, in one read of `builtin:scadbuddy` and `builtin:browser`. */
  async registry(): Promise<StoredOverrides> {
    const rows = this.reader ? await this.reader.rows([BUILTIN_SOURCES.scadbuddy, BUILTIN_SOURCES.browser]) : []
    return registryOverrides(this.tools, rows)
  }

  /**
   * The registry's overrides as a tool sees them: each call reads its tool's one row (`item`,
   * the same rules as `registry`; a browser tool's with `builtin:browser`'s switch,
   * `switchedItem`), and `disabled` the whole registry.
   */
  registryOverrides(): ToolOverrides {
    return {
      of: async (tool) => {
        const own = this.risk.get(tool)
        if (own === undefined) return { disabled: false }
        const source = registrySource(tool)
        const access = source === BUILTIN_SOURCES.browser ? await this.switchedItem(source, tool, own) : await this.item(source, tool, own)
        if (access === 'off') return { disabled: true }
        return { ...(access === own ? {} : { tier: access }), disabled: false }
      },
      disabled: async () => new Set((await this.registry()).disabled_tools),
    }
  }

  async plugin(source: string): Promise<{ toolTiers: Record<string, RiskTier>; disabledTools: string[] }> {
    return pluginOverrides(source, this.reader ? await this.reader.rows([source]) : [])
  }

  /** Each source's switch, tool tiers and disabled tools, in one read (a turn's package servers). */
  async pluginSources(
    sources: readonly string[],
  ): Promise<Map<string, { enabled: boolean; toolTiers: Record<string, RiskTier>; disabledTools: string[] }>> {
    const rows = this.reader && sources.length > 0 ? await this.reader.rows(sources) : []
    return new Map(sources.map((s) => [s, { enabled: sourceEnabled(s, rows), ...pluginOverrides(s, rows) }]))
  }

  async enabled(source: string): Promise<boolean> {
    return sourceEnabled(source, this.reader ? await this.reader.rows([source]) : [])
  }

  /**
   * A source's switch and one item's stored access (undefined: none stored, so its default
   * applies), in one read. For a tool whose default depends on its input (http_request's
   * method); not for the registry sources, whose migrated rows only raise (`item`).
   */
  async entry(source: string, item: string): Promise<{ enabled: boolean; access: ToolAccess | undefined }> {
    const rows = this.reader ? await this.reader.rows([source]) : []
    const row = rows.find((r) => r.source === source && r.item === item)
    return { enabled: sourceEnabled(source, rows), access: row && isToolAccess(row.access) ? row.access : undefined }
  }

  /**
   * One item's access with its source's switch, in one read: `off` when the source is
   * switched off as a whole, else as `item`.
   */
  async switchedItem(source: string, item: string, declared: RiskTier): Promise<ToolAccess> {
    if (!this.reader) return declared
    const rows = await this.reader.rows([source])
    if (!sourceEnabled(source, rows)) return 'off'
    return toolAccess(declared, rows.find((r) => r.source === source && r.item === item), isRegistrySource(source))
  }

  /** One item's access: its row, else `declared`. */
  async item(source: string, item: string, declared: RiskTier): Promise<ToolAccess> {
    if (!this.reader) return declared
    return toolAccess(declared, await this.rowOf(this.reader, source, item), isRegistrySource(source))
  }

  /** An on/off item (a skill, subagent or hook): `false` only for an `off` row. */
  async switchOn(source: string, item: string): Promise<boolean> {
    if (!this.reader) return true
    return (await this.rowOf(this.reader, source, item))?.access !== 'off'
  }

  /**
   * A built-in's access across `packages`, the packages that name it: each one's
   * `builtin:<name>` row, `outward` where it has none, and the strictest of those wins
   * (`outward` for none).
   */
  async builtin(name: string, packages: readonly string[]): Promise<ToolAccess> {
    if (packages.length === 0 || !this.reader) return 'outward'
    const sources = packages.map(packageSource)
    const item = builtinItem(name)
    const rows = await this.reader.rows(sources)
    const each = sources.map((source) => {
      const access = rows.find((r) => r.source === source && r.item === item)?.access
      return isToolAccess(access) ? access : 'outward'
    })
    return strictest(each) ?? 'outward'
  }

  /**
   * One of Claude Code's own built-ins (access/nativeBuiltins.ts): its item under
   * `builtin:claude-code`. With the source switched off it is `off`; else its row, if one is
   * stored. With no row it is `declared`, and a package can switch one `off` by default on:
   * then `builtin` over the `packages` that name it. A source or row a person switched off
   * stays off whatever a package names. `preloaded` is the source's rows when the caller read
   * them once for every built-in (nativeBuiltins.ts `nativeBuiltinTools`).
   */
  async native(name: string, declared: ToolAccess, packages: readonly string[], preloaded?: readonly AccessRow[]): Promise<ToolAccess> {
    const rows = preloaded ?? (this.reader ? await this.reader.rows([BUILTIN_SOURCES.claudeCode]) : [])
    if (!sourceEnabled(BUILTIN_SOURCES.claudeCode, rows)) return 'off'
    const row = rows.find((r) => r.source === BUILTIN_SOURCES.claudeCode && r.item === name)
    if (row && isToolAccess(row.access)) return row.access
    if (declared !== 'off' || packages.length === 0) return declared
    return this.builtin(name, packages)
  }

  /** Every row of the given sources, in one read (the call rule's, items.ts `offCallItem`); none without a reader. */
  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    return this.reader && sources.length > 0 ? this.reader.rows(sources) : []
  }

  /** Every given source's switched-off items and built-in tiers, in one read. */
  async packageAccess(sources: readonly string[]): Promise<Map<string, PackageAccess>> {
    const rows = this.reader && sources.length > 0 ? await this.reader.rows(sources) : []
    const out = new Map<string, PackageAccess>(
      sources.map((s) => [s, { off: { skills: new Set(), subagents: new Set(), hooks: new Set(), builtins: new Set() }, builtinTiers: new Map() }]),
    )
    for (const row of rows) {
      const into = out.get(row.source)
      if (!into) continue
      const kind = itemKindIn(row.source, row.item)
      if (kind === 'tool') continue
      const name = row.item.slice(row.item.indexOf(':') + 1)
      if (kind === 'builtin') {
        if (row.access === 'off') into.off.builtins.add(name)
        else if (isToolAccess(row.access)) into.builtinTiers.set(name, row.access)
      } else if (row.access === 'off') {
        into.off[OFF_SET[kind]].add(name)
      }
    }
    return out
  }

  /** One indexed read where the reader has one, else its source's rows. */
  private async rowOf(reader: AccessReader, source: string, item: string): Promise<AccessRow | undefined> {
    if (reader.row) return reader.row(source, item)
    return (await reader.rows([source])).find((r) => r.source === source && r.item === item)
  }
}
