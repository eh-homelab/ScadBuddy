import { chunks, type CrudTarget, crudOf, qs, SOURCES_PER_CALL } from '../api/crudStores.js'
import { type AuditContext, type AuditSink, SYSTEM_ACTOR } from '../audit/log.js'
import { type Access, itemKindIn } from './items.js'
import { type AccessRow, BUILTIN_SOURCES, isToolAccess, SOURCE_ITEM, type ToolAccess } from './model.js'

export type AccessReader = {
  rows(sources: readonly string[]): Promise<AccessRow[]>
  /** One item's row, in one indexed read; absent: `rows` of its source. */
  row?(source: string, item: string): Promise<AccessRow | undefined>
}

export type AccessWriter = {
  /** A tool or built-in takes a tier or `off`; a hook, skill or subagent `on` or `off` (checkItem). */
  setItem(source: string, item: string, access: Access, context?: AuditContext): Promise<void>
  resetItem(source: string, item: string, context?: AuditContext): Promise<void>
  setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void>
  replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void>
  /** `replaceTools` for several sources in one transaction; a source whose rows would not change is left alone. */
  replaceToolsOf(bySource: Readonly<Record<string, Readonly<Record<string, ToolAccess>>>>, context?: AuditContext): Promise<void>
  forget(source: string): Promise<void>
}

/** A write the model does not allow (400). Nothing was stored. */
export class AccessError extends Error {
  readonly status = 400
}

const SOURCE_RE = /^(builtin:(scadbuddy|browser|http|own-plugin|claude-code)|remote:[a-z][a-z0-9-]*|package:.+)$/

function checkSource(source: string): void {
  if (!SOURCE_RE.test(source)) throw new AccessError(`unknown access source ${JSON.stringify(source)}`)
}

/** A source's `*` switch: every source has one but builtin:scadbuddy, whose tools no switch turns off. */
export function checkSwitch(source: string): void {
  checkSource(source)
  if (source === BUILTIN_SOURCES.scadbuddy) throw new AccessError(`${JSON.stringify(source)} has no switch`)
}

function checkTool(item: string, access: unknown): asserts access is ToolAccess {
  if (item === '' || item === SOURCE_ITEM) throw new AccessError(`${JSON.stringify(item)} is not a tool name`)
  if (!isToolAccess(access)) throw new AccessError(`a tool takes off, read, write or outward, not ${JSON.stringify(access)}`)
}

/**
 * One item's write as the model allows it (#4757, spec §4.1). Only a package's own source and
 * the own plugin hold skills, subagents, hooks and built-ins (items.ts `itemKindIn`); every
 * other item is a tool, whatever its name. A tool or built-in takes off, read, write or
 * outward; a hook, skill or subagent on or off. A named item needs its name, and a hook
 * `hook:<event>:<16 hex>`.
 */
export function checkItem(source: string, item: string, access: unknown): asserts access is Access {
  const kind = itemKindIn(source, item)
  if (kind === 'tool') {
    checkTool(item, access)
    return
  }
  if (kind === 'hook' ? !HOOK_RE.test(item) : item.indexOf(':') === item.length - 1) {
    throw new AccessError(`${JSON.stringify(item)} is not a ${kind} item`)
  }
  if (kind === 'builtin') checkTool(item, access)
  else if (access !== 'on' && access !== 'off') throw new AccessError(`a ${kind} takes on or off, not ${JSON.stringify(access)}`)
}

const HOOK_RE = /^hook:[^:]+:[0-9a-f]{16}$/

/** `<actor.kind>:<actor.id>`; `system` without a context. */
export function updatedBy(context?: AuditContext): string {
  return context ? `${context.actor.kind}:${context.actor.id}` : 'system'
}

/** One source's change: its tool rows replaced, its rows forgotten, or single items set (`null` deletes). */
export type AccessEdit =
  | { source: string; kind: 'items'; items: Readonly<Record<string, string | null>> }
  | { source: string; kind: 'replace'; tools: Readonly<Record<string, ToolAccess>> }
  | { source: string; kind: 'forget' }

const toolsDetail = (edits: readonly AccessEdit[]): string =>
  edits.map((e) => `${e.source}/* tools = ${JSON.stringify(e.kind === 'replace' ? e.tools : {})}`).join('; ')

/**
 * The tool access, kept by the API (area `access`). This checks each write against the model and
 * records the audit row; the API runs the write's transaction, its warm eviction included.
 */
export class AccessStore implements AccessReader, AccessWriter {
  private readonly crud: AccessRows
  private readonly audit: AuditSink | undefined
  constructor(crud: AccessRows, audit?: AuditSink) {
    this.crud = crud
    this.audit = audit
  }

  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    if (sources.length === 0) return []
    return this.crud.rows(sources)
  }

  async row(source: string, item: string): Promise<AccessRow | undefined> {
    return this.crud.row(source, item)
  }

  async setItem(source: string, item: string, access: Access, context?: AuditContext): Promise<void> {
    checkSource(source)
    checkItem(source, item, access)
    await this.write(context, [{ source, kind: 'items', items: { [item]: access } }], () => `${source}/${item} = ${JSON.stringify(access)}`)
  }

  async resetItem(source: string, item: string, context?: AuditContext): Promise<void> {
    checkSource(source)
    if (item === '' || item === SOURCE_ITEM) throw new AccessError(`${JSON.stringify(item)} is not a tool name`)
    await this.write(context, [{ source, kind: 'items', items: { [item]: null } }], () => `${source}/${item} reset`)
  }

  async setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void> {
    checkSwitch(source)
    const access = enabled ? 'on' : 'off'
    await this.write(context, [{ source, kind: 'items', items: { [SOURCE_ITEM]: access } }], () => `${source}/${SOURCE_ITEM} = ${JSON.stringify(access)}`)
  }

  async replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void> {
    checkSource(source)
    for (const [item, access] of Object.entries(tools)) checkTool(item, access)
    await this.write(context, [{ source, kind: 'replace', tools }], toolsDetail)
  }

  async replaceToolsOf(bySource: Readonly<Record<string, Readonly<Record<string, ToolAccess>>>>, context?: AuditContext): Promise<void> {
    for (const [source, tools] of Object.entries(bySource)) {
      checkSource(source)
      for (const [item, access] of Object.entries(tools)) checkTool(item, access)
    }
    const edits: AccessEdit[] = Object.entries(bySource).map(([source, tools]) => ({ source, kind: 'replace', tools }))
    await this.write(context, edits, toolsDetail, { skipUnchanged: true })
  }

  async forget(source: string): Promise<void> {
    checkSource(source)
    await this.write(undefined, [{ source, kind: 'forget' }], () => `${source} forgotten`)
  }

  /**
   * One transaction on the API with the eviction (a warm process holds its tools), then the
   * audit row. With `skipUnchanged`, an edit that would leave its source's rows as they are is
   * dropped, and a write left with none records nothing.
   */
  private async write(
    context: AuditContext | undefined,
    edits: readonly AccessEdit[],
    detailOf: (edits: readonly AccessEdit[]) => string,
    options: { skipUnchanged?: boolean } = {},
  ): Promise<void> {
    const startedAt = new Date()
    const by = updatedBy(context)
    let applied: readonly AccessEdit[] = edits
    let failure: unknown
    try {
      const indices = await this.crud.write(edits, by, options.skipUnchanged ?? false)
      applied = indices.flatMap((i) => (edits[i] ? [edits[i]] : []))
    } catch (err) {
      failure = err
    }
    if (failure === undefined && applied.length === 0) return
    await this.audit?.record({
      kind: 'settings',
      action: 'ai_access',
      surface: context?.surface ?? 'system',
      actor: context?.actor ?? SYSTEM_ACTOR,
      clientIp: context?.clientIp,
      outcome: failure === undefined ? 'ok' : 'error',
      detail: `${detailOf(applied)}${failure === undefined ? '' : ` (failed: ${failure instanceof Error ? failure.message : String(failure)})`}`,
      startedAt,
      finishedAt: new Date(),
    })
    if (failure !== undefined) throw failure
  }
}

/** Where `AccessStore` reads and writes the rows. */
export type AccessRows = Pick<CrudAccessRows, 'rows' | 'row' | 'write'>

/**
 * The access rows through the API's CRUD routes, area `access` (backend
 * api/crud_agent_access.py).
 */
export class CrudAccessRows {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    const out: AccessRow[] = []
    for (const part of chunks(sources, SOURCES_PER_CALL)) {
      out.push(...(await this.#call<{ rows: AccessRow[] }>('the tool access', '/access/rows', { body: { sources: part } })).rows)
    }
    return out
  }

  async row(source: string, item: string): Promise<AccessRow | undefined> {
    const { row } = await this.#call<{ row: AccessRow | null }>('a tool access', `/access/row${qs({ source, item })}`)
    return row ?? undefined
  }

  /** The places in `edits` of the edits written. */
  async write(edits: readonly AccessEdit[], by: string, skipUnchanged: boolean): Promise<number[]> {
    const body = { by, edits, skip_unchanged: skipUnchanged }
    return (await this.#call<{ applied: number[] }>('a tool access written', '/access/write', { body })).applied
  }
}

