import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import { FlowScript } from '../components/flows/FlowScript'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { timeAgo } from '../lib/format'
import { actorText, flowPath, flowRunPath, FLOWS_PATH } from '../lib/flows'
import { useAsync } from '../lib/useAsync'

/**
 * #2350 — one registered flow version (`/workflows/flows/:id`): who registered it, its
 * name's other versions, its script (read only), and Start run, which goes to the run.
 * Version history, diffs and an editor come with flows in git (#2503, #2487).
 */
export function FlowPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const flow = useAsync(() => api.getFlow(id), [id])
  const name = flow.data?.name
  const versions = useAsync(() => (name ? api.listFlowVersions(name) : Promise.resolve([])), [name])
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string>()

  async function start() {
    setStarting(true)
    setError(undefined)
    try {
      const run = await api.startFlowRun(id)
      void navigate(flowRunPath(run.id))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setStarting(false)
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex flex-wrap items-baseline gap-2">
          <Link to="/workflows" className="text-[12px] text-muted hover:text-ink">
            Workflows
          </Link>
          <span className="text-faint">/</span>
          <Link to={FLOWS_PATH} className="text-[12px] text-muted hover:text-ink">
            Flows
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">{flow.data ? `${flow.data.name} v${flow.data.version}` : id}</h1>
        </div>
        {flow.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {flow.error instanceof ApiError && flow.error.status === 404 ? 'No such flow.' : flow.error.message}
          </p>
        ) : !flow.data ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : (
          <div className="flex flex-col gap-4 md:flex-row-reverse md:items-start">
            <aside className="flex flex-col gap-3 md:w-56 md:shrink-0">
              <div>
                <Button variant="primary" onClick={() => void start()} disabled={starting}>
                  {starting ? <Spinner /> : 'Start run'}
                </Button>
                {error && (
                  <p role="alert" className="mt-2 text-[13px] text-warn">
                    {error}
                  </p>
                )}
              </div>
              <p className="text-[12.5px] text-muted">
                Registered by {actorText(flow.data.created_by)}, {timeAgo(flow.data.created_at)}.
              </p>
              <section aria-label="Versions">
                <h2 className="mb-1 text-[13px] font-medium">Versions</h2>
                {versions.data ? (
                  <ul className="flex flex-wrap gap-1.5 md:flex-col md:gap-0.5">
                    {versions.data.map((v) => (
                      <li key={v.id}>
                        {v.id === id ? (
                          <span aria-current="page" className="text-[13px] font-medium">
                            v{v.version}
                          </span>
                        ) : (
                          <Link to={flowPath(v.id)} className="text-[13px] text-accent hover:underline">
                            v{v.version}
                          </Link>
                        )}{' '}
                        <span className="text-[12px] text-faint">{timeAgo(v.created_at)}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-[12.5px] text-muted">Loading…</p>
                )}
              </section>
            </aside>
            <section aria-label="Flow script" className="min-w-0 flex-1">
              <h2 className="mb-1 text-[13px] font-medium">Script</h2>
              <FlowScript script={flow.data.script} />
            </section>
          </div>
        )}
      </div>
    </div>
  )
}
