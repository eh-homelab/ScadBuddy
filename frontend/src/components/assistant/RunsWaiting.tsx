import { Link } from 'react-router'
import { ATTENTION_POLL_MS, fetchRunsWaiting } from '../../agent/attention'
import { FLOW_RUNS_TOPIC, flowRunPath } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'
import { usePollWhile } from '../../lib/usePollWhileUnavailable'

const TOPICS = [FLOW_RUNS_TOPIC]

/**
 * #2482 — the flow runs whose own calls wait for you, each linking to its page, where
 * they are decided. The header's badge counts them, and they are in no session's list
 * (a run started from the browser has no session), so the panel it opens shows them
 * here. Nothing when none waits, or when the agent does not answer.
 */
export function RunsWaiting() {
  const runs = useAsync(async () => (await fetchRunsWaiting()) ?? [], [], TOPICS)
  usePollWhile(true, () => runs.refresh(), ATTENTION_POLL_MS)
  const waiting = runs.data ?? []
  if (waiting.length === 0) return null
  return (
    <div className="shrink-0 border-b border-line px-3 py-1.5 text-[12.5px]">
      <ul aria-label="Flow runs waiting for you" className="flex flex-col gap-1">
        {waiting.map((run) => (
          <li key={run.id}>
            <Link to={flowRunPath(run.id)} className="text-accent hover:underline">
              {run.name}
            </Link>{' '}
            waits for you on {run.waiting === 1 ? 'a call' : `${run.waiting} calls`}
          </li>
        ))}
      </ul>
    </div>
  )
}
