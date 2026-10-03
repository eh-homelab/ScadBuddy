import { useEffect, useId, useRef } from 'react'
import { Link } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { SessionResource } from '../../api/types'
import { editPath, modelPath } from '../../lib/deeplink'
import { useAsync } from '../../lib/useAsync'

type Kind = SessionResource['type']

/** The groups, in the order a session usually works through them. */
const GROUPS: ReadonlyArray<{ type: Kind; label: string }> = [
  { type: 'model', label: 'Models' },
  { type: 'revision', label: 'Revisions' },
  { type: 'preset', label: 'Presets' },
  { type: 'asset', label: 'Assets' },
  { type: 'render_job', label: 'Renders' },
  { type: 'output', label: 'Outputs' },
  { type: 'print_run', label: 'Print runs' },
  { type: 'print', label: 'Prints' },
  { type: 'unclassified', label: 'Other changes' },
]

const ACTION_LABEL: Record<SessionResource['action'], string> = {
  created: 'created',
  modified: 'changed',
  deleted: 'deleted',
}

/** One resource, however many calls touched it: how it was touched, in order, repeats folded. */
interface Entry {
  type: Kind
  id: string | null
  model: string | null
  actions: SessionResource['action'][]
  tools: string[]
  /** Where its last row sits in the session's rows, to order it against a model's delete. */
  last: number
}

/** Folds the session's rows into one entry per resource; each `unclassified` row stays its own. */
function entries(rows: readonly SessionResource[]): Entry[] {
  const out: Entry[] = []
  const byKey = new Map<string, Entry>()
  rows.forEach((r, index) => {
    const key = r.id === null ? null : `${r.type}\u0000${r.id}`
    const seen = key === null ? undefined : byKey.get(key)
    if (seen) {
      if (seen.actions.at(-1) !== r.action) seen.actions.push(r.action)
      if (!seen.tools.includes(r.tool)) seen.tools.push(r.tool)
      seen.model ??= r.model
      seen.last = index
      return
    }
    const entry: Entry = { type: r.type, id: r.id, model: r.model, actions: [r.action], tools: [r.tool], last: index }
    out.push(entry)
    if (key !== null) byKey.set(key, entry)
  })
  return out
}

/** A commit, shown the way git shortens one; other ids are uuids or numbers and stay whole. */
const shortCommit = (id: string) => (/^[0-9a-f]{12,}$/.test(id) ? id.slice(0, 7) : id)

/**
 * Where each model's last `deleted` row sits. Anything of that model last touched
 * before it belonged to the deleted model, even if one of that slug was made again.
 */
function modelDeletes(rows: readonly SessionResource[]): Map<string, number> {
  const at = new Map<string, number>()
  rows.forEach((r, index) => {
    if (r.type === 'model' && r.action === 'deleted' && r.id !== null) at.set(r.id, index)
  })
  return at
}

/** What the entry is called, and its page in the app; no page once it, or its model, is gone. */
function describe(e: Entry, deletes: ReadonlyMap<string, number>): { name: string; to: string | null } {
  const id = e.id ?? ''
  const on = e.model ? ` · ${e.model}` : ''
  const modelGone = e.type !== 'model' && e.model !== null && (deletes.get(e.model) ?? -1) > e.last
  const gone = e.actions.at(-1) === 'deleted' || modelGone
  const page = (to: string | null) => (gone ? null : to)
  switch (e.type) {
    case 'model':
      return { name: id, to: page(modelPath(id)) }
    case 'revision':
      return {
        name: `${shortCommit(id)}${on}`,
        to: e.model ? page(`${modelPath(e.model)}?version=${encodeURIComponent(id)}`) : null,
      }
    case 'preset':
    case 'asset':
    case 'render_job':
      return { name: `${id}${on}`, to: e.model ? page(modelPath(e.model)) : null }
    case 'output':
      return { name: `${id}${on}`, to: page(editPath(id)) }
    case 'print_run':
      return { name: id, to: page('/prints') }
    case 'print':
      return { name: `queue item ${id}`, to: page('/prints') }
    case 'unclassified':
      return { name: e.tools.join(', '), to: null }
  }
}

interface Props {
  sessionId: string
  /** Changes when the session may have touched more (its status moved or a tool call finished), so the list reads again. */
  refreshKey?: unknown
}

/** #931 — the session view's "Touched" panel: what this session's tool calls changed. */
export function SessionTouched({ sessionId, refreshKey }: Props) {
  const idBase = useId()
  const { data, error, refresh } = useAsync(() => api.listAiSessionResources(sessionId), [sessionId])
  // A background re-read: the list stays on screen, and a failed read keeps it.
  const readAt = useRef(refreshKey)
  useEffect(() => {
    if (Object.is(readAt.current, refreshKey)) return
    readAt.current = refreshKey
    refresh()
  }, [refreshKey, refresh])

  if (error) {
    return (
      <p role="alert" className="px-3 py-2 text-[12px] text-warn">
        {error instanceof ApiError ? error.detail : 'The assistant service did not answer; try again.'}
      </p>
    )
  }
  if (!data) return <p className="px-3 py-2 text-[12px] text-faint">Loading…</p>

  const all = entries(data.resources)
  const deletes = modelDeletes(data.resources)
  if (all.length === 0) {
    return <p className="px-3 py-2 text-[12px] text-muted">Nothing changed by this session yet.</p>
  }

  return (
    <div className="max-h-56 space-y-2 overflow-y-auto px-3 py-2 text-[12px]">
      {GROUPS.map(({ type, label }) => {
        const items = all.filter((e) => e.type === type)
        if (items.length === 0) return null
        const headingId = `${idBase}-${type}`
        return (
          <div key={type} role="group" aria-labelledby={headingId}>
            <h3 id={headingId} className="text-[11px] font-medium uppercase tracking-wide text-faint">
              {label}
            </h3>
            <ul className="mt-0.5 space-y-0.5">
              {items.map((e, i) => {
                const { name, to } = describe(e, deletes)
                return (
                  <li key={e.id ?? `${e.tools.join()}-${i}`} className="flex items-baseline gap-1.5" title={e.tools.join(', ')}>
                    {to ? (
                      <Link to={to} title={e.id ?? undefined} className="min-w-0 truncate font-mono text-accent underline">
                        {name}
                      </Link>
                    ) : (
                      <span title={e.id ?? undefined} className="min-w-0 truncate font-mono">
                        {name}
                      </span>
                    )}
                    <span className="shrink-0 text-faint">{e.actions.map((a) => ACTION_LABEL[a]).join(', ')}</span>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
