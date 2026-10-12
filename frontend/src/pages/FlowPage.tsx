import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { FlowDefinition, FlowRegister, FlowScriptProblem, FlowVersion } from '../api/types'
import { FlowScript } from '../components/flows/FlowScript'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { timeAgo } from '../lib/format'
import { actorText, flowPath, flowRunPath, FLOWS_PATH } from '../lib/flows'
import { useAsync } from '../lib/useAsync'
import { useDebounced } from '../lib/useDebounced'
import { PHONE_QUERY, useMediaQuery } from '../lib/useMediaQuery'

// Monaco's lazy chunk, as the source page loads it (App.tsx): opening a flow on a phone,
// where it is read only, never downloads an editor.
const FlowEditor = lazy(async () => ({ default: (await import('../components/flows/FlowEditor')).FlowEditor }))
const SourceDiff = lazy(async () => ({ default: (await import('../components/SourceDiff')).SourceDiff }))

/** How long typing settles before the script is checked. */
export const FLOW_CHECK_DEBOUNCE_MS = 500

const errorText = (caught: unknown) => (caught instanceof ApiError ? caught.detail : String(caught))

/** A refusal's `problems` (the backend's `flow-script` 422), if it carries any. */
function refusedProblems(caught: unknown): FlowScriptProblem[] | null {
  if (!(caught instanceof ApiError)) return null
  const problems = caught.problem['problems']
  return Array.isArray(problems) ? (problems as FlowScriptProblem[]) : null
}

/** A version's approval timeout as `POST /workflows` takes it: stored 0 is "never". */
function approvalTimeout(seconds: number | null | undefined): Pick<FlowRegister, 'approval_timeout'> {
  if (seconds == null) return {}
  return { approval_timeout: seconds === 0 ? 'never' : seconds }
}

/**
 * #2350 — one registered flow version (`/workflows/flows/:id`): who registered it, its
 * name's other versions, its script, and Start run, which goes to the run.
 *
 * #2487 — the script in an editor (desktop and tablet) that type-checks as you type;
 * Save registers a new version, one commit in the flow's git history (#2503), and
 * nothing is edited in place. Any two versions diff, and Restore makes an older one the
 * newest, again as a new version. On a phone the script and the diff are read only.
 */
export function FlowPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const phone = useMediaQuery(PHONE_QUERY)
  const flow = useAsync(() => api.getFlow(id), [id])
  const name = flow.data?.name
  const versions = useAsync(() => (name ? api.listFlowVersions(name) : Promise.resolve([])), [name, id])
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string>()
  const [restoring, setRestoring] = useState(false)
  const viewed = versions.data?.find((v) => v.id === id)

  async function start() {
    setStarting(true)
    setError(undefined)
    try {
      const run = await api.startFlowRun(id)
      void navigate(flowRunPath(run.id))
    } catch (caught) {
      setError(errorText(caught))
      setStarting(false)
    }
  }

  async function restore(flowName: string, version: FlowVersion) {
    setRestoring(true)
    setError(undefined)
    try {
      const restored = await api.restoreFlowVersion(flowName, version.commit)
      void navigate(flowPath(restored.id))
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setRestoring(false)
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
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
            <aside className="flex flex-col gap-4 md:w-60 md:shrink-0">
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" onClick={() => void start()} disabled={starting}>
                  {starting ? <Spinner /> : 'Start run'}
                </Button>
                {!phone && viewed && !viewed.current && (
                  <Button onClick={() => void restore(flow.data!.name, viewed)} disabled={restoring}>
                    {restoring ? <Spinner /> : `Restore v${viewed.version}`}
                  </Button>
                )}
              </div>
              {error && (
                <p role="alert" className="text-[13px] text-warn">
                  {error}
                </p>
              )}
              <p className="text-[12.5px] text-muted">
                Registered by {actorText(flow.data.created_by)}, {timeAgo(flow.data.created_at)}.
              </p>
              <section aria-label="Versions">
                <h2 className="mb-1 text-[13px] font-medium">Versions</h2>
                {versions.data ? (
                  <ul className="flex flex-col gap-1">
                    {versions.data.map((v) => (
                      <li key={v.id} className="text-[12px] leading-snug">
                        {v.id === id ? (
                          <span aria-current="page" className="text-[13px] font-medium">
                            v{v.version}
                          </span>
                        ) : (
                          <Link to={flowPath(v.id)} className="text-[13px] text-accent hover:underline">
                            v{v.version}
                          </Link>
                        )}{' '}
                        <span className="text-muted">{actorText(v.created_by)}</span>{' '}
                        <span className="text-faint">{timeAgo(v.created_at)}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-[12.5px] text-muted">Loading…</p>
                )}
              </section>
            </aside>
            <div className="flex min-w-0 flex-1 flex-col gap-5">
              <section aria-label="Flow script">
                <h2 className="mb-1 text-[13px] font-medium">Script</h2>
                {phone ? (
                  <FlowScript script={flow.data.script} />
                ) : (
                  <ScriptEditor key={flow.data.id} flow={flow.data} onSaved={(saved) => void navigate(flowPath(saved.id))} />
                )}
              </section>
              {versions.data && versions.data.length > 0 && (
                <Compare
                  key={`${id}:${versions.data.length}`}
                  name={flow.data.name} versions={versions.data} viewed={id} sideBySide={!phone} />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function ScriptEditor({ flow, onSaved }: { flow: FlowDefinition; onSaved: (saved: FlowDefinition) => void }) {
  const [draft, setDraft] = useState(flow.script)
  const checked = useDebounced(draft, FLOW_CHECK_DEBOUNCE_MS)
  const [problems, setProblems] = useState<FlowScriptProblem[] | null>(null)
  const [checkError, setCheckError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string>()
  const help = useAsync(() => api.getFlowHostFunctions(), [])
  const dirty = draft !== flow.script

  useEffect(() => {
    const controller = new AbortController()
    setProblems(null)
    api
      .checkFlowScript(checked, controller.signal)
      .then((result) => {
        setProblems(result.problems)
        setCheckError(undefined)
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setCheckError(errorText(caught))
      })
    return () => controller.abort()
  }, [checked])

  async function save() {
    if (!dirty || saving) return
    setSaving(true)
    setSaveError(undefined)
    try {
      onSaved(await api.registerFlow({ name: flow.name, script: draft, ...approvalTimeout(flow.approval_timeout_s) }))
    } catch (caught) {
      const refused = refusedProblems(caught)
      if (refused) setProblems(refused)
      setSaveError(errorText(caught))
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <Suspense fallback={<p className="text-[13px] text-muted">Loading the editor</p>}>
        <FlowEditor
          version={flow.id}
          name={flow.name}
          initialValue={flow.script}
          onChange={setDraft}
          problems={problems ?? []}
          hostFunctions={help.data}
          onSave={() => void save()}
          label="Script"
        />
      </Suspense>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={() => void save()} disabled={!dirty || saving}>
          {saving ? <Spinner /> : 'Save'}
        </Button>
        <span className="text-[12.5px] text-muted">{dirty ? 'Unsaved changes' : `v${flow.version}, as saved`}</span>
        <span role="status" className="text-[12.5px] text-muted">
          {checkError
            ? `Could not check: ${checkError}`
            : problems === null || checked !== draft
              ? 'Checking…'
              : problems.length === 0
                ? 'No problems'
                : `${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}`}
        </span>
      </div>
      <p className="text-[12px] text-faint">Saving adds a new version; this one never changes.</p>
      {saveError && (
        <p role="alert" className="text-[13px] text-warn">
          {saveError}
        </p>
      )}
      {problems && problems.length > 0 && (
        <ul aria-label="Problems" className="flex flex-col gap-1.5">
          {problems.map((problem, i) => (
            <li key={i} className="rounded-[6px] border border-line bg-surface-2 px-2.5 py-1.5">
              <span className="mr-2 text-[12px] font-medium text-warn">
                {problem.line != null ? `Line ${problem.line}` : 'Script'}
              </span>
              <span className="whitespace-pre-wrap font-mono text-[11.5px]">{problem.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Compare({
  name,
  versions,
  viewed,
  sideBySide,
}: {
  name: string
  versions: FlowVersion[]
  viewed: string
  sideBySide: boolean
}) {
  const at = Math.max(
    0,
    versions.findIndex((v) => v.id === viewed),
  )
  const [base, setBase] = useState(versions[Math.min(at + 1, versions.length - 1)]!.commit)
  const [head, setHead] = useState(versions[at]!.commit)
  const [shown, setShown] = useState<{ base: string; head: string } | null>(null)
  const label = (commit: string) => `v${versions.find((v) => v.commit === commit)?.version ?? '?'}`
  const select = 'rounded-[6px] border border-line bg-surface px-1.5 py-1 text-[13px]'

  return (
    <section aria-label="Compare versions" className="flex flex-col gap-2">
      <h2 className="text-[13px] font-medium">Compare versions</h2>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-0.5 text-[12px] text-muted">
          From
          <select className={select} value={base} onChange={(event) => setBase(event.target.value)}>
            {versions.map((v) => (
              <option key={v.commit} value={v.commit}>
                v{v.version}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-0.5 text-[12px] text-muted">
          To
          <select className={select} value={head} onChange={(event) => setHead(event.target.value)}>
            {versions.map((v) => (
              <option key={v.commit} value={v.commit}>
                v{v.version}
              </option>
            ))}
          </select>
        </label>
        <Button onClick={() => setShown({ base, head })}>Compare</Button>
        {shown && (
          <Button variant="ghost" onClick={() => setShown(null)}>
            Close
          </Button>
        )}
      </div>
      {shown && (
        <VersionDiff name={name} base={shown.base} head={shown.head} title={`${label(shown.base)} → ${label(shown.head)}`} sideBySide={sideBySide} />
      )}
    </section>
  )
}

function VersionDiff({
  name,
  base,
  head,
  title,
  sideBySide,
}: {
  name: string
  base: string
  head: string
  title: string
  sideBySide: boolean
}) {
  const sources = useAsync(
    () => Promise.all([api.getFlowVersionSource(name, base), api.getFlowVersionSource(name, head)]),
    [name, base, head],
  )
  return (
    <div className="rounded-[6px] border border-line">
      <h3 className="px-3 py-1.5 text-[12.5px] font-medium">{title}</h3>
      {sources.error ? (
        <p role="alert" className="px-3 pb-2 text-[13px] text-warn">
          {sources.error.message}
        </p>
      ) : !sources.data ? (
        <p className="px-3 pb-2 text-[13px] text-muted">Loading…</p>
      ) : (
        <Suspense fallback={<p className="px-3 pb-2 text-[13px] text-muted">Loading the diff…</p>}>
          <SourceDiff original={sources.data[0]} modified={sources.data[1]} language="python" sideBySide={sideBySide} />
        </Suspense>
      )}
    </div>
  )
}
