import { Link } from 'react-router'
import { api } from '../../api/client'
import type { FlowQueueEntry, FlowRunQueue } from '../../api/types'
import { FLOW_RUNS_TOPIC, flowRunPath, hopUntilText } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'

const TOPICS = [FLOW_RUNS_TOPIC]
const OPEN = new Set(['running', 'waiting'])

/**
 * #1057 — the flow runs this session started (spec 2026-10-01 §7.3, plan Task F2), as a
 * link to the Workflows page filtered to them. Nothing when it started none, or when
 * they cannot be read.
 *
 * #2482 — and, grouped by run, what each open run's queue waits on (spec 2026-10-11
 * §4.5): the run's own calls, its child sessions' and its grant request, each with where
 * it waits (with this session, or with you) and why it is outside the grant. Each is
 * decided on its run's page, which this links to.
 */
export function SessionFlowRuns({ sessionId }: { sessionId: string }) {
  const runs = useAsync(() => api.listFlowRuns({ session: sessionId }), [sessionId], TOPICS)
  const open = (runs.data ?? []).filter((r) => OPEN.has(r.status)).map((r) => r.id)
  const queues = useAsync(() => readQueues(open), [open.join(',')], TOPICS)
  const count = runs.data?.length ?? 0
  if (count === 0) return null
  const waiting = (queues.data ?? []).filter((q) => q.entries.length > 0)
  return (
    <div className="shrink-0 border-b border-line px-3 py-1.5 text-[12.5px]">
      <Link to={`/workflows?session=${encodeURIComponent(sessionId)}`} className="text-accent hover:underline">
        Flow runs started here ({count})
      </Link>
      {waiting.length > 0 && (
        <ul aria-label="Waiting in runs started here" className="mt-1 flex flex-col gap-1">
          {waiting.map((q) => (
            <li key={q.run_id}>
              <Link to={flowRunPath(q.run_id)} className="text-accent hover:underline">
                {q.name}
              </Link>{' '}
              waits on {q.entries.length}
              <ul className="ml-3 list-disc text-[12px] text-muted">
                {q.entries.map((e) => (
                  <li key={e.id}>{entryText(e)}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Every open run's queue; one the agent cannot read is left out. */
async function readQueues(ids: readonly string[]): Promise<FlowRunQueue[]> {
  const read = await Promise.all(ids.map((id) => api.getFlowRunQueue(id).catch(() => undefined)))
  return read.filter((q): q is FlowRunQueue => q !== undefined)
}

/** One line of a run's queue: what, where it waits, and why. */
function entryText(e: FlowQueueEntry): string {
  if (e.source === 'grant') return 'its grant, waiting for your approval'
  const what = e.source === 'flow' ? e.fn : `${e.tool} (in a session it talks to)`
  const where =
    e.hop === 'parent'
      ? `with this session${e.hop_until ? ` until ${hopUntilText(e.hop_until)}` : ''}`
      : 'with you'
  return `${what}: ${where}${e.reason ? `; outside the grant: ${e.reason}` : ''}`
}
