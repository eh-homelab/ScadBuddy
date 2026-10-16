import { useContext } from 'react'
import { Link } from 'react-router'
import { RunsWaitingContext } from '../../agent/attention'
import { flowRunPath } from '../../lib/flows'

/**
 * #2482 — the flow runs started from the browser whose own calls wait for you, each
 * linking to its page, where they are decided. The header's badge counts them, and they
 * are in no session's list, so the panel it opens shows them here. Read from the badge's
 * own poll (`RunsWaitingContext`), never a second one. Nothing when none waits.
 */
export function RunsWaiting() {
  const waiting = useContext(RunsWaitingContext) ?? []
  if (waiting.length === 0) return null
  return (
    <div className="shrink-0 border-b border-line px-3 py-1.5 text-[12.5px]">
      <ul aria-label="Flow runs waiting for you" className="flex flex-col gap-1">
        {waiting.map((run) => (
          <li key={run.id}>
            <Link to={flowRunPath(run.id)} className="text-primary hover:underline">
              {run.name}
            </Link>{' '}
            waits for you on {run.waiting === 1 ? 'a call' : `${run.waiting} calls`}
          </li>
        ))}
      </ul>
    </div>
  )
}
