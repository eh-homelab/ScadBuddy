import { Link, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { FlowRun } from '../api/types'
import { timeAgo } from '../lib/format'
import { FLOW_POLL_MS, FLOW_RUNS_TOPIC, FLOW_STATUS_LABEL, flowRunPath, waitingText } from '../lib/flows'
import { useAsync } from '../lib/useAsync'
import { usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'

const TOPICS = [FLOW_RUNS_TOPIC]

/**
 * #1057 — every flow run (`/workflows`, plan 2026-10-09 Task F1), newest first, with what
 * each waits on. `?session=` keeps the runs one assistant session started (F2). Answers
 * and approvals are given on a run's own page.
 */
export function WorkflowsPage() {
  const [params] = useSearchParams()
  const session = params.get('session') ?? undefined
  const runs = useAsync(() => api.listFlowRuns({ session }), [session], TOPICS)
  usePollWhileUnavailable(() => runs.refresh(), FLOW_POLL_MS)

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5">
          <h1 className="text-lg font-semibold tracking-tight">Workflows</h1>
          <p className="mt-0.5 text-[13px] text-muted">
            Runs of the flows an assistant registered: each step, and what it waits on you for.
          </p>
          {session && (
            <p className="mt-2 text-[13px]">
              Started by assistant session <code className="font-mono text-[12px]">{session}</code>.{' '}
              <Link to="/workflows" className="text-accent hover:underline">
                Show all runs
              </Link>
            </p>
          )}
        </div>
        {runs.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {runs.error.message}
          </p>
        ) : runs.loading && !runs.data ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : runs.data && runs.data.length > 0 ? (
          <ul aria-label="Flow runs" className="divide-y divide-line rounded-[8px] border border-line">
            {runs.data.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </ul>
        ) : (
          <p className="text-[13px] text-muted">No flow runs yet.</p>
        )}
      </div>
    </div>
  )
}

function RunRow({ run }: { run: FlowRun }) {
  return (
    <li data-run={run.id} className="px-3 py-2.5">
      <div className="flex items-baseline gap-2">
        <Link to={flowRunPath(run.id)} className="text-[13px] font-medium hover:underline">
          {run.name} v{run.version}
        </Link>
        <span className="text-[12px] text-muted">{FLOW_STATUS_LABEL[run.status]}</span>
        <span className="ml-auto text-[12px] text-faint">{timeAgo(run.created_at)}</span>
      </div>
      {run.waiting_on.map((entry) => (
        <p key={entry.call_id} className="mt-1 text-[12.5px] text-warn">
          {waitingText(entry)}
        </p>
      ))}
    </li>
  )
}
