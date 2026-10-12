import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { FlowPending, FlowStep } from '../api/types'
import { Button } from '../components/ui/Button'
import {
  FLOW_POLL_MS,
  FLOW_STATUS_LABEL,
  flowRunTopic,
  isStaleEntry,
  STALE_ENTRY_MESSAGE,
  waitingText,
} from '../lib/flows'
import { formatDuration } from '../lib/format'
import { temporalWorkflowUrl } from '../lib/temporal'
import { useAsync } from '../lib/useAsync'
import { usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'

const FIELD = 'w-full rounded-[6px] border border-line bg-bg px-2 py-1.5 text-[13px]'

/**
 * #1057 — one flow run (`/workflows/runs/:id`, plan 2026-10-09 Task F1): its steps, and
 * what it waits on, answered or approved here (`POST …/answer`, `…/decide`). Live over
 * `workflow-run:<id>`, and read every 5 s while the socket is down. No Reset here: that
 * is the route and its tool (spec §7.4).
 */
export function WorkflowRunPage() {
  const { id = '' } = useParams()
  const view = useAsync(() => api.getFlowRun(id), [id], [flowRunTopic(id)])
  const settings = useAsync(() => api.getSettings(), []).data
  usePollWhileUnavailable(() => view.refresh(), FLOW_POLL_MS)
  const [notice, setNotice] = useState<string>()

  const run = view.data?.run ?? null
  const temporal = run ? temporalWorkflowUrl(settings, run.workflow_id) : null

  async function respond(send: () => Promise<unknown>) {
    setNotice(undefined)
    try {
      await send()
    } catch (error) {
      setNotice(isStaleEntry(error) ? STALE_ENTRY_MESSAGE : error instanceof Error ? error.message : String(error))
    }
    view.refresh()
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/workflows" className="text-[12px] text-muted hover:text-ink">
            Workflows
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">
            {view.data ? `${view.data.name} v${view.data.version}` : id}
          </h1>
          {temporal && (
            <a href={temporal} target="_blank" rel="noreferrer" className="ml-auto text-[12px] text-accent hover:underline">
              Temporal UI ↗
            </a>
          )}
        </div>

        {view.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {view.error instanceof ApiError && view.error.status === 404 ? 'No such flow run.' : view.error.message}
          </p>
        ) : !view.data ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : (
          <>
            <p className="mb-3 text-[13px]">
              <span className="text-muted">Status: </span>
              <span data-testid="flow-run-status">{FLOW_STATUS_LABEL[view.data.status]}</span>
            </p>
            {!view.data.live && view.data.status !== 'starting' && (
              <p className="mb-3 text-[12.5px] text-muted">Live status unavailable; showing the last known state</p>
            )}
            {notice && (
              <p role="alert" className="mb-3 text-[13px] text-warn">
                {notice}
              </p>
            )}
            {view.data.pending.map((entry) => (
              <PendingCard key={entry.call_id} entry={entry} runId={id} respond={respond} />
            ))}
            {run && run.steps.length > 0 && <Steps steps={run.steps} />}
            {run?.result && (
              <section aria-label="Result" className="mt-4">
                <h2 className="mb-1 text-[13px] font-medium">Result</h2>
                <pre className="overflow-x-auto rounded-[6px] border border-line bg-surface-2 p-2 text-[12px] whitespace-pre-wrap">
                  {run.result}
                  {run.result_truncated && '\n…'}
                </pre>
              </section>
            )}
          </>
        )}
      </div>
    </div>
  )
}

interface CardProps {
  entry: FlowPending
  runId: string
  respond: (send: () => Promise<unknown>) => Promise<void>
}

function PendingCard({ entry, runId, respond }: CardProps) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  async function act(send: () => Promise<unknown>) {
    setBusy(true)
    try {
      await respond(send)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      aria-label={waitingText(entry)}
      className="mb-3 rounded-[8px] border border-warn/40 bg-surface-2 p-3"
    >
      <p className="mb-2 text-[13px]">{waitingText(entry)}</p>
      {entry.kind === 'answer' ? (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void act(() => api.answerFlowRun(runId, entry.call_id, text))
          }}
          className="flex flex-col gap-2"
        >
          <textarea aria-label="Your answer" value={text} onChange={(e) => setText(e.target.value)} className={FIELD} rows={2} />
          <div>
            <Button type="submit" variant="primary" size="sm" disabled={busy}>
              Answer
            </Button>
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-2">
          <input
            aria-label="Reason (optional)"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Reason (optional)"
            className={FIELD}
          />
          <div className="flex gap-2">
            <Button
              variant="primary"
              size="sm"
              disabled={busy}
              onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, true, text || undefined))}
            >
              Approve
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={busy}
              onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, false, text || undefined))}
            >
              Deny
            </Button>
          </div>
        </div>
      )}
    </section>
  )
}

function Steps({ steps }: { steps: FlowStep[] }) {
  return (
    <table aria-label="Steps" className="w-full text-left text-[12.5px]">
      <thead className="text-muted">
        <tr>
          <th className="py-1 font-normal">#</th>
          <th className="py-1 font-normal">Call</th>
          <th className="py-1 font-normal">Status</th>
          <th className="py-1 font-normal">Started</th>
          <th className="py-1 font-normal">Duration</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((step) => (
          <tr key={step.call_id} className="border-t border-line">
            <td className="py-1">{step.seq}</td>
            <td className="py-1 font-mono">{step.fn}</td>
            <td className="py-1">{step.error ? `${step.status} (${step.error})` : step.status}</td>
            <td className="py-1">{new Date(step.started_at).toLocaleString()}</td>
            <td className="py-1">
              {step.ended_at
                ? formatDuration((new Date(step.ended_at).getTime() - new Date(step.started_at).getTime()) / 1000)
                : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
