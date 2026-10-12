import { Link, NavLink, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { FlowRun, FlowSummary } from '../api/types'
import { timeAgo } from '../lib/format'
import {
  actorText,
  FLOW_POLL_MS,
  FLOW_RUNS_TOPIC,
  FLOW_STATUS_LABEL,
  flowPath,
  flowRunPath,
  FLOWS_PATH,
  waitingText,
} from '../lib/flows'
import { useAsync } from '../lib/useAsync'
import { usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'

const TOPICS = [FLOW_RUNS_TOPIC]

const TAB = 'border-b-2 px-1 pb-1.5 text-[13px]'
const tabClass = ({ isActive }: { isActive: boolean }) =>
  `${TAB} ${isActive ? 'border-accent font-medium text-ink' : 'border-transparent text-muted hover:text-ink'}`

/**
 * #1057 — every flow run (`/workflows`, plan 2026-10-09 Task F1), newest first, with what
 * each waits on. `?session=` keeps the runs one assistant session started (F2). Answers
 * and approvals are given on a run's own page. #2350 — the Flows tab
 * (`/workflows/flows`) lists every registered flow.
 */
export function WorkflowsPage({ tab = 'runs' }: { tab?: 'runs' | 'flows' }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-4">
          <h1 className="text-lg font-semibold tracking-tight">Workflows</h1>
          <p className="mt-0.5 text-[13px] text-muted">
            {tab === 'runs'
              ? 'Runs of the flows an assistant registered: each step, and what it waits on you for.'
              : 'The flows registered here: each one’s versions and script, and a run started from one.'}
          </p>
        </div>
        <nav aria-label="Workflows" className="mb-4 flex gap-4 border-b border-line">
          <NavLink to="/workflows" end className={tabClass}>
            Runs
          </NavLink>
          <NavLink to={FLOWS_PATH} className={tabClass}>
            Flows
          </NavLink>
        </nav>
        {tab === 'runs' ? <Runs /> : <Flows />}
      </div>
    </div>
  )
}

function Runs() {
  const [params] = useSearchParams()
  const session = params.get('session') ?? undefined
  const runs = useAsync(() => api.listFlowRuns({ session }), [session], TOPICS)
  usePollWhileUnavailable(() => runs.refresh(), FLOW_POLL_MS)

  return (
    <>
      {session && (
        <p className="mb-4 text-[13px]">
          Started by assistant session <code className="font-mono text-[12px]">{session}</code>.{' '}
          <Link to="/workflows" className="text-accent hover:underline">
            Show all runs
          </Link>
        </p>
      )}
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
    </>
  )
}

/** Each flow by name: its latest version, and who registered that and when. */
function latestByName(versions: FlowSummary[]): { latest: FlowSummary; count: number }[] {
  const byName = new Map<string, { latest: FlowSummary; count: number }>()
  for (const v of versions) {
    const seen = byName.get(v.name)
    if (!seen) byName.set(v.name, { latest: v, count: 1 })
    else byName.set(v.name, { latest: v.version > seen.latest.version ? v : seen.latest, count: seen.count + 1 })
  }
  return [...byName.values()].sort((a, b) => a.latest.name.localeCompare(b.latest.name))
}

function Flows() {
  const flows = useAsync(() => api.listFlows(), [])
  if (flows.error) {
    return (
      <p role="alert" className="text-[13px] text-warn">
        {flows.error.message}
      </p>
    )
  }
  if (!flows.data) return <p className="text-[13px] text-muted">Loading…</p>
  const named = latestByName(flows.data)
  if (named.length === 0) {
    return <p className="text-[13px] text-muted">No flows registered yet. An assistant registers one with register_flow.</p>
  }
  return (
    <ul aria-label="Flows" className="divide-y divide-line rounded-[8px] border border-line">
      {named.map(({ latest, count }) => (
        <li key={latest.name} data-flow={latest.name} className="px-3 py-2.5">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <Link to={flowPath(latest.id)} className="text-[13px] font-medium hover:underline">
              {latest.name}
            </Link>
            <span className="text-[12px] text-muted">
              v{latest.version}
              {count > 1 ? ` · ${count} versions` : ''}
            </span>
            <span className="ml-auto text-[12px] text-faint">{timeAgo(latest.created_at)}</span>
          </div>
          <p className="mt-0.5 text-[12.5px] text-muted">Registered by {actorText(latest.created_by)}</p>
        </li>
      ))}
    </ul>
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
