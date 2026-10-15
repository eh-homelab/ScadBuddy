import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { SessionResource } from '../../api/types'
import { editPath, modelPath } from '../../lib/deeplink'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

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
  { type: 'print_archive', label: 'Print archives' },
  { type: 'library', label: 'Libraries' },
  { type: 'font', label: 'Fonts' },
  { type: 'setting', label: 'Settings' },
  { type: 'project', label: 'Bambuddy projects' },
  { type: 'bambuddy_file', label: 'Bambuddy files' },
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
    // A library is one per model it is pinned on, plus the shared checkout (no model).
    const key = r.id === null ? null : `${r.type}\u0000${r.id}${r.type === 'library' ? `\u0000${r.model ?? ''}` : ''}`
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
    case 'print_archive':
      return { name: `print ${id}`, to: page(`/prints/${encodeURIComponent(id)}`) }
    case 'library':
      // A model's pin goes to the model; the shared checkout, to Settings' Libraries.
      return { name: `${id}${on}`, to: page(e.model ? modelPath(e.model) : '/settings#libraries') }
    case 'font':
      return { name: id, to: null }
    case 'setting':
      return { name: id, to: page('/settings') }
    case 'project':
      return { name: `project ${id}`, to: null }
    case 'bambuddy_file':
      return { name: `library file ${id}`, to: null }
    case 'unclassified':
      return { name: e.tools.join(', '), to: null }
  }
}

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** The summary's buckets, in the order the line names them; anything else is an "other change". */
// `runsOnly` is true when the session recorded no queue item, so its print runs are its prints.
const BUCKETS: ReadonlyArray<{ one: string; many: string; holds: (e: Entry, runsOnly: boolean) => boolean }> = [
  { one: 'new model', many: 'new models', holds: (e) => e.type === 'model' && e.actions[0] === 'created' },
  { one: 'model deleted', many: 'models deleted', holds: (e) => e.type === 'model' && e.actions.at(-1) === 'deleted' },
  { one: 'preset saved', many: 'presets saved', holds: (e) => e.type === 'preset' && e.actions.at(-1) !== 'deleted' },
  { one: 'render', many: 'renders', holds: (e) => e.type === 'render_job' },
  { one: 'output saved', many: 'outputs saved', holds: (e) => e.type === 'output' && e.actions[0] === 'created' },
  {
    one: 'print queued',
    many: 'prints queued',
    holds: (e, runsOnly) => (e.type === 'print' || (runsOnly && e.type === 'print_run')) && e.actions[0] === 'created',
  },
]

/**
 * One plain line of what the session did, #931's "3 revisions to *x*, 1 output, 1 print
 * queued": its new versions per model, then models, presets, renders, outputs and
 * prints, and the rest as "other changes". print_output records a print run beside the
 * queue item it made, so a run counts only when no queue item was recorded.
 */
function summary(all: readonly Entry[]): string {
  const parts: string[] = []
  const counted = new Set<Entry>()
  // By model; a revision whose model is unknown is still a new version, of no named model.
  const versions = new Map<string | null, number>()
  for (const e of all) {
    if (e.type !== 'revision') continue
    versions.set(e.model, (versions.get(e.model) ?? 0) + 1)
    counted.add(e)
  }
  for (const [model, n] of versions) {
    if (model !== null) parts.push(`${count(n, 'new version', 'new versions')} of ${model}`)
  }
  const unnamed = versions.get(null)
  if (unnamed) parts.push(count(unnamed, 'other new version', 'other new versions'))
  const runsOnly = !all.some((e) => e.type === 'print')
  for (const { one, many, holds } of BUCKETS) {
    const items = all.filter((e) => !counted.has(e) && holds(e, runsOnly))
    if (items.length > 0) parts.push(count(items.length, one, many))
    for (const e of items) counted.add(e)
  }
  // A run beside its queue item is the same print, not another change.
  const rest = all.filter((e) => !counted.has(e) && !(e.type === 'print_run' && !runsOnly)).length
  if (rest > 0) parts.push(count(rest, 'other change', 'other changes'))
  return parts.join(', ')
}

interface Undo {
  model: string
  /** The version before the session's first change. */
  before: string
  /** The version the session's last change made, where it names one. */
  last: string | null
  /** Whether a revision's parent was not the session's previous one: someone else edited in between. */
  interleaved: boolean
}

/**
 * The models this session can put back: each one whose first change in the session was
 * a new version naming the version it came from, and that the session neither made nor
 * deleted. A `model` row before it (details edited, or a write whose new version was
 * not known yet) is a change with no recorded parent, so that model is not offered:
 * restoring the first revision's parent would keep that change while saying it undid
 * the session. Restoring adds a new version, so nothing is lost.
 */
function undoable(rows: readonly SessionResource[]): Undo[] {
  const first = new Map<string, string | null>()
  const last = new Map<string, string | null>()
  const interleaved = new Set<string>()
  const skip = new Set<string>()
  for (const r of rows) {
    if (r.type === 'model' && r.id !== null && (r.action !== 'modified' || !first.has(r.id))) skip.add(r.id)
    if (r.type === 'revision' && r.model) {
      if (!first.has(r.model)) first.set(r.model, r.before)
      else if (r.before !== null && !(last.get(r.model) ?? '').startsWith(r.before)) interleaved.add(r.model)
      last.set(r.model, r.after ?? r.id)
    }
  }
  const out: Undo[] = []
  for (const [model, before] of first) {
    if (before === null || skip.has(model)) continue
    out.push({ model, before, last: last.get(model) ?? null, interleaved: interleaved.has(model) })
  }
  return out
}

/**
 * Where the model is now, against the session: `same` when it already matches the
 * version before the session (undone already, here or elsewhere), `movedOn` when its
 * newest version is not the session's last one, so an undo also takes back later edits.
 */
async function undoState(u: Undo): Promise<{ same: boolean; movedOn: boolean }> {
  const current = (await api.getModel(u.model)).version ?? null
  if (current === null) return { same: false, movedOn: u.interleaved }
  if (current === u.before) return { same: true, movedOn: false }
  const diff = await api.getVersionDiff(u.model, current, u.before)
  return { same: (diff.files ?? []).length === 0, movedOn: u.interleaved || (u.last !== null && !current.startsWith(u.last)) }
}

/** "Undo changes to <model>": restores the version before the session, after a confirm. */
function UndoModel({ undo, onDone }: { undo: Undo; onDone: () => void }) {
  const { model, before } = undo
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const state = useAsync(() => undoState(undo), [model, before, undo.last])

  function close() {
    if (busy) return
    setOpen(false)
    setError(null)
  }

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      await api.restoreVersion(model, before)
      setDone(true)
      setOpen(false)
      onDone()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not undo the changes; try again.')
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <p role="status" className="text-muted">
        <span className="font-mono">{model}</span> is back to how it was before this chat.
      </p>
    )
  }
  // Offered once the model's state is known; a failed read still offers it, without the warning.
  if (state.loading || state.data?.same) return null
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="text-accent underline">
        Undo changes to <span className="font-mono">{model}</span>
      </button>
      <Dialog
        open={open}
        title={`Undo this chat's changes to ${model}?`}
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={() => void confirm()} disabled={busy} {...USER_ONLY}>
              {busy ? <Spinner /> : 'Undo changes'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          <span className="font-mono text-ink">{model}</span> goes back to how it was before this
          chat changed it. It is saved as a new version, so the chat&apos;s changes stay in the
          model&apos;s history and can be brought back.
        </p>
        {state.data?.movedOn && (
          <p className="mt-3 text-[13px] text-warn">
            It was also changed outside this chat after the chat started. Those changes are undone
            too, and also stay in its history.
          </p>
        )}
        {state.error && (
          <p className="mt-3 text-[13px] text-muted">
            Could not check whether it was changed outside this chat. If it was, those changes are
            undone too.
          </p>
        )}
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </>
  )
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

  const undo = undoable(data.resources)

  return (
    <div className="max-h-56 space-y-2 overflow-y-auto px-3 py-2 text-[12px]">
      <p className="text-ink">{summary(all)}</p>
      {undo.length > 0 && (
        <ul className="space-y-0.5">
          {undo.map((u) => (
            <li key={`${u.model}:${u.last ?? ''}`}>
              <UndoModel undo={u} onDone={refresh} />
            </li>
          ))}
        </ul>
      )}
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
