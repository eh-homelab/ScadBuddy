import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import type { Sql } from 'postgres'
import type { Risk } from '../tools/registry.js'
import { unwrapUntrusted } from '../safety/untrusted.js'

// What a session touched (#931): every resource a session's tool calls
// created, changed or deleted, one `ai_session_resources` row each
// (db/migrations/20261001T1824Z_session_resources.sql).
//
// Derived inline, per tool call, from the call's parsed input and its result
// (tools/registry.ts runToolWithOutcome, for any call that carries a
// session), by the per-tool extractors in
// EXTRACTORS below. Only calls that succeeded are recorded, plus the failed
// calls of the few tools whose error still names what they made
// (RECORDED_WHEN_FAILED); a refused or denied call changed nothing. A `write` or `outward` tool with no
// extractor is recorded as one `unclassified` row naming the tool, so a gap
// shows in the list instead of the call vanishing from it; a `read` tool with
// no extractor records nothing.
//
// Recording never fails the call: a row that cannot be written is reported
// through `onError` and the call's result goes back unchanged, as the audit
// log does (audit/log.ts).

export const RESOURCE_TYPES = [
  'model',
  'revision',
  'preset',
  'asset',
  'render_job',
  'output',
  'print_run',
  'print',
  'unclassified',
] as const
export type ResourceType = (typeof RESOURCE_TYPES)[number]

/** The kinds a session list can be filtered by (#931's reverse lookup): every kind with an id. */
export const LOOKUP_TYPES = [
  'model',
  'revision',
  'preset',
  'asset',
  'render_job',
  'output',
  'print_run',
  'print',
] as const satisfies readonly Exclude<ResourceType, 'unclassified'>[]
export type LookupType = (typeof LOOKUP_TYPES)[number]

/**
 * A resource to find the sessions of. A `model` matches every row of that
 * model (`model_slug`: the model itself, its revisions, presets, assets,
 * renders and outputs); any other kind matches its own id.
 */
export type ResourceRef = { type: LookupType; id: string }

export const RESOURCE_ACTIONS = ['created', 'modified', 'deleted'] as const
export type ResourceAction = (typeof RESOURCE_ACTIONS)[number]

/**
 * One resource one call touched. `model` is the model it belongs to (its own
 * slug for a model), so a session's revisions can be grouped by model and a
 * model's sessions found. `before`/`after` are what it was and became where
 * that has an id: a revision's parent and new commit, a model's version.
 */
export type Touch = {
  type: ResourceType
  /** The resource's id; null only for `unclassified`. */
  id: string | null
  action: ResourceAction
  model?: string | null
  before?: string | null
  after?: string | null
}

export type Extractor = (input: Record<string, unknown>, result: unknown) => Touch[]

function str(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function field(value: unknown, key: string): unknown {
  return value !== null && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined
}

/**
 * A call that made a revision of `slug`: the ModelRecord it answers names the
 * new commit. `before` is the parent only where the call names it (`base`,
 * which apply_patch requires and update_source takes); the other revision
 * tools leave it null. With no new commit in the answer the model is recorded
 * as changed, never its slug as a revision id.
 */
function revision(input: Record<string, unknown>, result: unknown): Touch[] {
  const slug = str(field(result, 'slug')) ?? str(input.slug)
  const after = str(field(result, 'version'))
  if (!slug) return []
  if (!after) return [{ type: 'model', id: slug, action: 'modified', model: slug }]
  return [{ type: 'revision', id: after, action: 'created', model: slug, before: str(input.base), after }]
}

/** A model the call made: the ModelRecord's slug and version. */
function modelCreated(result: unknown, from?: string | null): Touch[] {
  const slug = str(field(result, 'slug'))
  if (!slug) return []
  return [{ type: 'model', id: slug, action: 'created', model: slug, before: from ?? null, after: str(field(result, 'version')) }]
}

function output(result: unknown, slug: string | null): Touch[] {
  const id = str(field(result, 'id'))
  return id ? [{ type: 'output', id, action: 'created', model: str(field(result, 'slug')) ?? slug }] : []
}

/** Tool name → extractor. A name that is not a registered tool fails test/touched.test.ts. */
export const EXTRACTORS: Readonly<Record<string, Extractor>> = {
  // Models.
  create_model: (_input, result) => modelCreated(result),
  import_model: (_input, result) => modelCreated(result),
  create_from_template: (input, result) => modelCreated(result, input.from === 'blank' ? null : str(input.from)),
  duplicate_model: (input, result) => modelCreated(result, str(input.slug)),
  update_model_details: (input, result) => {
    const slug = str(input.slug)
    if (!slug) return []
    return [{ type: 'model', id: slug, action: 'modified', model: slug, after: str(field(result, 'version')) }]
  },
  delete_model: (input) => {
    const slug = str(input.slug)
    return slug ? [{ type: 'model', id: slug, action: 'deleted', model: slug }] : []
  },
  // Revisions: each answers the ModelRecord at its new commit.
  update_source: revision,
  apply_patch: revision,
  write_source_file: revision,
  delete_source_file: revision,
  // `commit` is the revision restored FROM, not the new commit's parent, so it is not `before`.
  restore_version: revision,
  // A merge is a revision (its answer wraps the ModelRecord); dismiss and detach change the model.
  update_from_upstream: (input, result) => {
    if (input.action === 'merge') return revision(input, field(result, 'model'))
    const slug = str(field(result, 'slug')) ?? str(input.slug)
    return slug ? [{ type: 'model', id: slug, action: 'modified', model: slug, after: str(field(result, 'version')) }] : []
  },
  set_readme: revision,
  delete_readme: revision,
  set_model_thumbnail: revision,
  delete_model_thumbnail: revision,
  // Presets.
  save_preset: (input, result) => {
    const id = str(field(result, 'id'))
    return id ? [{ type: 'preset', id, action: 'created', model: str(input.slug) }] : []
  },
  update_preset: (input) => {
    const id = str(input.preset_id)
    return id ? [{ type: 'preset', id, action: 'modified', model: str(input.slug) }] : []
  },
  duplicate_preset: (input, result) => {
    const id = str(field(result, 'id'))
    return id ? [{ type: 'preset', id, action: 'created', model: str(input.slug), before: str(input.preset_id) }] : []
  },
  delete_preset: (input) => {
    const id = str(input.preset_id)
    return id ? [{ type: 'preset', id, action: 'deleted', model: str(input.slug) }] : []
  },
  // Assets.
  upload_asset: (input, result) => {
    const id = str(field(result, 'id'))
    return id ? [{ type: 'asset', id, action: 'created', model: str(input.slug) }] : []
  },
  // Renders and outputs.
  render_model: (input, result) => {
    const slug = str(input.slug)
    const job = str(field(result, 'job_id'))
    return [
      ...(job ? [{ type: 'render_job' as const, id: job, action: 'created' as const, model: slug, after: str(field(result, 'model_version')) }] : []),
      ...output(field(result, 'output'), slug),
    ]
  },
  save_output: (input, result) => output(result, str(input.slug)),
  delete_output: (input) => {
    const id = str(input.output_id)
    return id ? [{ type: 'output', id, action: 'deleted' }] : []
  },
  // Prints. `print` is always a Bambuddy queue item id, whichever tool queued it;
  // `print_run` is ScadBuddy's own run (backend bambuddy/runs.py PrintRun.id).
  print_output: (input, result) => {
    const run = str(field(result, 'id'))
    const output = str(input.output_id)
    const items = field(field(result, 'result'), 'queue_item_ids')
    return [
      ...(run ? [{ type: 'print_run' as const, id: run, action: 'created' as const, before: output }] : []),
      ...(Array.isArray(items) ? items : [])
        .filter((item): item is number | string => typeof item === 'number' || typeof item === 'string')
        .map((item) => ({ type: 'print' as const, id: String(item), action: 'created' as const, before: output })),
    ]
  },
  // Answers Bambuddy's new queue item; `before` is the archive printed again.
  print_again: (input, result) => {
    const item = field(result, 'queue_item_id')
    const archive = input.archive_id
    if (typeof item !== 'number' && typeof item !== 'string') return []
    return [{ type: 'print', id: String(item), action: 'created', before: archive == null ? null : String(archive) }]
  },
}

/** A tool result's JSON (the first text block, out of its untrusted-data envelope); undefined when it is not JSON. */
export function resultJson(result: CallToolResult): unknown {
  const block = result.content.find((b) => b.type === 'text')
  if (!block || block.type !== 'text') return undefined
  try {
    return JSON.parse(unwrapUntrusted(block.text)) as unknown
  } catch {
    return undefined
  }
}

/**
 * Write tools that change no resource of the kinds recorded here: session
 * control (it changes ScadBuddy's own session state, which the session list
 * already shows), pairing a browser tab, and confirm_action itself (the call
 * it ran is recorded as that tool). Not `unclassified`: there is nothing to
 * classify. A name that is not a registered tool fails test/touched.test.ts.
 */
export const TOUCHES_NOTHING: ReadonlySet<string> = new Set([
  'sessions_start',
  'sessions_send',
  'sessions_fork',
  'sessions_interrupt',
  'sessions_handoff',
  'sessions_accept_handoff',
  'sessions_cancel_handoff',
  'sessions_approve',
  'sessions_deny',
  'browser_pair',
  'confirm_action',
])

/**
 * Tools whose error result still names what they made, so a failed call is
 * recorded too: a render_model whose render failed, or whose save_output did,
 * created its job all the same, and its error result is the job's summary.
 */
export const RECORDED_WHEN_FAILED: ReadonlySet<string> = new Set(['render_model'])

/**
 * What a call touched: its extractor's rows, one `unclassified` row for a
 * write with none, else nothing. A failed call (`ok` false) touched nothing
 * unless its tool is in RECORDED_WHEN_FAILED.
 */
export function touchesOf(
  tool: { name: string; risk: Risk },
  input: Record<string, unknown>,
  result: CallToolResult,
  ok = true,
): Touch[] {
  if (!ok && !RECORDED_WHEN_FAILED.has(tool.name)) return []
  if (TOUCHES_NOTHING.has(tool.name)) return []
  const extract = EXTRACTORS[tool.name]
  if (extract) return extract(input, resultJson(result))
  return tool.risk === 'read' ? [] : [{ type: 'unclassified', id: null, action: 'modified' }]
}

export type TouchedCall = {
  sessionId: string
  tool: { name: string; risk: Risk }
  input: Record<string, unknown>
  result: CallToolResult
  /** False for a call that ran and failed (outcome `error`); refused and denied calls are never reported. */
  ok?: boolean
}

/**
 * Where runToolWithOutcome reports a session's calls that ran (ToolServices.touched): the
 * successful ones, and the failed ones with `ok` false, which record nothing unless
 * their tool is in RECORDED_WHEN_FAILED. Never throws.
 */
export interface TouchedSink {
  record(call: TouchedCall): Promise<void>
}

export type TouchedRecord = {
  type: ResourceType
  id: string | null
  action: ResourceAction
  model: string | null
  before: string | null
  after: string | null
  tool: string
  at: string
}

type Row = {
  resource_type: ResourceType
  resource_id: string | null
  action: ResourceAction
  model_slug: string | null
  before_id: string | null
  after_id: string | null
  tool: string
  at: Date
}

/**
 * The most rows one call records; past it, the rest are dropped and one
 * `unclassified` row says so. A print of every plate is one queue item per
 * plate; a real call stays far below this.
 */
export const MAX_TOUCHES_PER_CALL = 100

/** The longest id stored; longer ones are cut (they come from a tool result). */
export const ID_MAX = 300

/** At most ID_MAX UTF-16 units, cut between code points: never half a surrogate pair. */
function bounded(value: string | null | undefined): string | null {
  if (value == null || value.length <= ID_MAX) return value ?? null
  const kept = value.slice(0, ID_MAX)
  const last = kept.charCodeAt(kept.length - 1)
  return last >= 0xd800 && last <= 0xdbff ? kept.slice(0, -1) : kept
}

export class SessionResources implements TouchedSink {
  private readonly sql: Sql
  private readonly onError: ((err: unknown) => void) | undefined

  constructor(sql: Sql, onError?: (err: unknown) => void) {
    this.sql = sql
    this.onError = onError
  }

  async record(call: TouchedCall): Promise<void> {
    try {
      const touches = touchesOf(call.tool, call.input, call.result, call.ok ?? true)
      if (touches.length === 0) return
      const kept =
        touches.length > MAX_TOUCHES_PER_CALL
          ? [...touches.slice(0, MAX_TOUCHES_PER_CALL - 1), { type: 'unclassified' as const, id: null, action: 'modified' as const }]
          : touches
      const rows = kept.map((t) => ({
        session_id: call.sessionId,
        tool: call.tool.name,
        resource_type: t.type,
        resource_id: bounded(t.id),
        action: t.action,
        model_slug: bounded(t.model),
        before_id: bounded(t.before),
        after_id: bounded(t.after),
      }))
      await this.sql`INSERT INTO ai_session_resources ${this.sql(rows)}`
    } catch (err) {
      this.onError?.(err)
    }
  }

  /** Everything the session touched, oldest first. */
  async list(sessionId: string): Promise<TouchedRecord[]> {
    const rows = await this.sql<Row[]>`
      SELECT resource_type, resource_id, action, model_slug, before_id, after_id, tool, at
      FROM ai_session_resources WHERE session_id = ${sessionId}::uuid ORDER BY id`
    return rows.map((r) => ({
      type: r.resource_type,
      id: r.resource_id,
      action: r.action,
      model: r.model_slug,
      before: r.before_id,
      after: r.after_id,
      tool: r.tool,
      at: r.at.toISOString(),
    }))
  }
}
