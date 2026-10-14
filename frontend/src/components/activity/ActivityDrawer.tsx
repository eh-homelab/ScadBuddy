import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { Activity, ActivityDetail } from '../../api/types'
import { formatDuration, timeAgo } from '../../lib/format'
import { useAdvanced } from '../../lib/advanced'
import { safeHttpUrl } from '../../lib/safeUrl'
import { temporalWorkflowUrl } from '../../lib/temporal'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { statusLabel } from '../prints/status'
import { principalLabel } from './labels'

const ADVANCED_KEY = 'scadbuddy.activity.advanced'

interface Props {
  id: string
  onClose: () => void
}

interface Stage {
  name: string
  status: string
}

/** The stages a source recorded in `detail.stages`; anything not shaped like one is left out. */
function stagesOf(detail: Record<string, unknown>): Stage[] {
  const raw = detail.stages
  if (!Array.isArray(raw)) return []
  return raw.flatMap((s: unknown) => {
    if (typeof s !== 'object' || s === null) return []
    const { name, label, status } = s as Record<string, unknown>
    const text = typeof name === 'string' ? name : typeof label === 'string' ? label : null
    return text === null ? [] : [{ name: text, status: typeof status === 'string' ? status : '' }]
  })
}

/** The error in words: a string, or an object carrying a `detail`. */
function errorText(error: unknown): string | null {
  if (typeof error === 'string') return error
  if (typeof error === 'object' && error !== null) {
    const { detail, message } = error as Record<string, unknown>
    if (typeof detail === 'string') return detail
    if (typeof message === 'string') return message
  }
  return null
}

function timing(row: Activity): string {
  if (!row.created_at) return ''
  const started = `Started ${timeAgo(row.created_at)}`
  if (!row.finished_at) return started
  return `${started}, took ${formatDuration((Date.parse(row.finished_at) - Date.parse(row.created_at)) / 1000)}`
}

/**
 * A link the backend emitted: an app path goes through the router; an `/api/` path is a
 * plain same-origin anchor (the router has no such page); an absolute URL opens in a new
 * tab only when it is http(s). Anything else is not shown.
 */
function ActivityLinkItem({ link }: { link: ActivityDetail['links'][number] }) {
  const cls = 'text-accent hover:underline'
  if (link.href.startsWith('/api/')) {
    return <a href={link.href} className={cls}>{link.label}</a>
  }
  if (link.href.startsWith('/') && !link.href.startsWith('//')) {
    return <Link to={link.href} className={cls}>{link.label}</Link>
  }
  const href = safeHttpUrl(link.href)
  if (!href) return null
  return <a href={href} target="_blank" rel="noreferrer" className={cls}>{link.label}</a>
}

function Json({ label, value }: { label: string; value: unknown }) {
  if (value === undefined) return null
  return (
    <section>
      <h3 className="mb-1 text-[12px] text-muted">{label}</h3>
      <pre className="overflow-x-auto rounded border border-line bg-surface-2 p-2 text-[12px]">
        {JSON.stringify(value, null, 2)}
      </pre>
    </section>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 text-[13px]">
      <dt className="w-24 shrink-0 text-muted">{label}</dt>
      <dd className="min-w-0 break-all">{children}</dd>
    </div>
  )
}

/** One activity row's detail, in a drawer: Simple in words, Advanced with ids and raw JSON. */
export function ActivityDrawer({ id, onClose }: Props) {
  // A child opened from the list replaces the row shown, until the page names another.
  const [trail, setTrail] = useState<{ from: string; ids: string[] }>({ from: id, ids: [] })
  const ids = trail.from === id ? trail.ids : []
  const current = ids.at(-1) ?? id
  const [advanced, toggleAdvanced] = useAdvanced(ADVANCED_KEY)
  const view = useAsync(() => api.getActivity(current, advanced), [current, advanced], [`activity:${current}`])
  const settings = useAsync(() => api.getSettings(), []).data

  const row = view.data?.row
  const detail = view.data?.detail ?? {}
  const stages = stagesOf(detail)
  // Simple mode's detail has no `error` (the API drops it); the source's `summary` says it in words.
  const error = errorText(detail.error) ?? (typeof detail.summary === 'string' ? detail.summary : null)
  const temporal = row ? temporalWorkflowUrl(settings, row.workflow_id) : null

  return (
    <Dialog open title={row?.title ?? 'Activity'} onClose={onClose} placement="right">
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-2">
          {ids.length > 0 && (
            <Button size="sm" variant="ghost" onClick={() => setTrail({ from: id, ids: ids.slice(0, -1) })}>
              Back
            </Button>
          )}
          <span id="activity-advanced" className="ml-auto text-[13px] text-ink">
            Advanced
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={advanced}
            aria-labelledby="activity-advanced"
            onClick={toggleAdvanced}
            className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors ${
              advanced ? 'border-accent bg-accent' : 'border-line-strong bg-surface-3'
            }`}
          >
            <span
              className={`absolute top-[2px] size-3.5 rounded-full transition-[left] ${
                advanced ? 'left-[18px] bg-accent-ink' : 'left-[2px] bg-muted'
              }`}
            />
          </button>
        </div>
        {view.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load this: {view.error.message}
          </p>
        ) : !row ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : (
          <>
            <dl className="flex flex-col gap-1.5">
              <Row label="Status">{statusLabel(row.status)}</Row>
              <Row label="Started by">{principalLabel(row.principal)}</Row>
              <Row label="When">{timing(row)}</Row>
              {advanced && (
                <>
                  {row.workflow_id && <Row label="Workflow">{row.workflow_id}</Row>}
                  {row.run_id && <Row label="Run">{row.run_id}</Row>}
                  {temporal && (
                    <Row label="Temporal">
                      <a href={temporal} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                        Temporal UI ↗
                      </a>
                    </Row>
                  )}
                  {row.traceparent && <Row label="Trace">{row.traceparent}</Row>}
                  {row.facets && Object.keys(row.facets).length > 0 && (
                    <Row label="Facets">{JSON.stringify(row.facets)}</Row>
                  )}
                  {row.via_tool && (
                    <Row label="Via tool">
                      <Link to="/settings#plugins" className="text-accent hover:underline">
                        {row.via_tool}
                      </Link>
                    </Row>
                  )}
                </>
              )}
            </dl>
            {error && (
              <p className={`text-[13px] ${row.status === 'failed' ? 'text-warn' : 'text-muted'}`}>{error}</p>
            )}
            {stages.length > 0 && (
              <ol aria-label="Stages" className="flex flex-col gap-1 text-[13px]">
                {stages.map((s, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-muted">{s.status ? statusLabel(s.status) : ''}</span>
                    <span>{s.name}</span>
                  </li>
                ))}
              </ol>
            )}
            {(view.data?.links.length ?? 0) > 0 && (
              <ul aria-label="Links" className="flex flex-col gap-1 text-[13px]">
                {view.data?.links.map((l) => (
                  <li key={l.href}>
                    <ActivityLinkItem link={l} />
                  </li>
                ))}
              </ul>
            )}
            {(view.data?.children.length ?? 0) > 0 && (
              <section>
                <h3 className="mb-1 text-[12px] text-muted">Parts</h3>
                <ul className="flex flex-col gap-1 text-[13px]">
                  {view.data?.children.map((c) => (
                    <li key={c.id} className="flex gap-2">
                      <span className="text-muted">{statusLabel(c.status)}</span>
                      <button
                        type="button"
                        className="text-left hover:underline"
                        onClick={() => setTrail({ from: id, ids: [...ids, c.id] })}
                      >
                        {c.title}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {advanced && (
              <>
                <Json label="Request" value={detail.request} />
                <Json label="Result" value={detail.result} />
                <Json label="Error" value={detail.error} />
              </>
            )}
          </>
        )}
      </div>
    </Dialog>
  )
}
