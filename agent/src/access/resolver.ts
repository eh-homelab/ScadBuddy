import type { RiskTier } from '../harness/permissions.js'
import { ALL_TOOLS } from '../tools/index.js'
import type { Tool, ToolOverrides } from '../tools/registry.js'
import { type AccessRow, BUILTIN_SOURCES, isToolAccess, registrySource, type StoredOverrides, type ToolAccess } from './model.js'
import { isRegistrySource, pluginOverrides, registryOverrides, sourceEnabled, toolAccess } from './resolve.js'
import type { AccessReader } from './store.js'

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
   * the same rules as `registry`), and `disabled` the whole registry.
   */
  registryOverrides(): ToolOverrides {
    return {
      of: async (tool) => {
        const own = this.risk.get(tool)
        if (own === undefined) return { disabled: false }
        // `item`, not `switchedItem`: a `builtin:browser` `*` off is enforced only on the classic seam until #4757.
        const access = await this.item(registrySource(tool), tool, own)
        if (access === 'off') return { disabled: true }
        return { ...(access === own ? {} : { tier: access }), disabled: false }
      },
      disabled: async () => new Set((await this.registry()).disabled_tools),
    }
  }

  async plugin(source: string): Promise<{ toolTiers: Record<string, RiskTier>; disabledTools: string[] }> {
    return pluginOverrides(source, this.reader ? await this.reader.rows([source]) : [])
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

  /** One indexed read where the reader has one, else its source's rows. */
  private async rowOf(reader: AccessReader, source: string, item: string): Promise<AccessRow | undefined> {
    if (reader.row) return reader.row(source, item)
    return (await reader.rows([source])).find((r) => r.source === source && r.item === item)
  }
}
