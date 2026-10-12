import { useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { FlowResetPreview, FlowStep } from '../../api/types'
import { USER_ONLY } from '../../agent/dom'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

const RESET_CHANGED = 'https://scadbuddy.dev/problems/flow-reset-changed'
const FIELD = 'w-full rounded-[6px] border border-line bg-bg px-2 py-1.5 text-[13px]'

interface Again {
  key: string
  label: string
  outward: boolean
}

/**
 * The host calls a Reset to the point runs again: the recorded steps from its first one
 * on, and any outward call the preview found that no step records. Outward ones (a print,
 * an arrange) are sent again as new effects.
 */
function runsAgain(steps: FlowStep[], firstStep: number | null | undefined, preview: FlowResetPreview): Again[] {
  const outward = new Set(preview.calls.map((c) => c.call_id))
  const again: Again[] = steps
    .filter((s) => firstStep != null && s.seq >= firstStep)
    .map((s) => ({ key: s.call_id, label: `#${s.seq} ${s.fn}`, outward: s.outward || outward.has(s.call_id) }))
  const listed = new Set(again.map((a) => a.key))
  for (const call of preview.calls) {
    if (!listed.has(call.call_id)) again.push({ key: call.call_id, label: call.fn, outward: true })
  }
  return again
}

/**
 * #2350 — Reset a run to an earlier point (spec 2026-10-01 §7.4), on its page: pick a
 * point (`GET …/reset-points`), see what would run again (`GET …/reset-preview`), and
 * confirm. The Reset is pinned to the preview the person saw: when more outward calls
 * landed since, the route refuses with the new preview, which replaces the old one here.
 */
export function ResetPanel({ runId, steps, onReset }: { runId: string; steps: FlowStep[]; onReset: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <section aria-label="Reset" className="mt-4 rounded-[8px] border border-line p-3">
      <div className="flex items-center gap-2">
        <h2 className="text-[13px] font-medium">Reset</h2>
        <p className="text-[12.5px] text-muted">Run the flow again from an earlier point.</p>
        <Button size="sm" className="ml-auto" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide' : 'Preview a reset'}
        </Button>
      </div>
      {open && <ResetChooser runId={runId} steps={steps} onReset={onReset} />}
    </section>
  )
}

function ResetChooser({ runId, steps, onReset }: { runId: string; steps: FlowStep[]; onReset: () => void }) {
  const points = useAsync(() => api.getFlowResetPoints(runId), [runId])
  const [eventId, setEventId] = useState<number>()
  const [preview, setPreview] = useState<FlowResetPreview>()
  const [previewError, setPreviewError] = useState<string>()
  const [notice, setNotice] = useState<string>()
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  const point = points.data?.points.find((p) => p.event_id === eventId)
  const again = preview && preview.event_id === eventId ? runsAgain(steps, point?.first_step, preview) : undefined
  const outward = again?.filter((a) => a.outward) ?? []

  async function choose(next: number) {
    setEventId(next)
    setPreview(undefined)
    setPreviewError(undefined)
    setNotice(undefined)
    try {
      setPreview(await api.previewFlowReset(runId, next))
    } catch (caught) {
      setPreviewError(caught instanceof ApiError ? caught.detail : String(caught))
    }
  }

  async function reset() {
    if (!preview) return
    setBusy(true)
    setError(undefined)
    try {
      await api.resetFlowRun(runId, preview)
      setConfirming(false)
      setPreview(undefined)
      setEventId(undefined)
      setNotice('Reset. The run continues from that point.')
      points.refresh()
      onReset()
    } catch (caught) {
      if (caught instanceof ApiError && caught.problem.type === RESET_CHANGED) {
        // The route's new preview when outward calls landed since; none when another
        // Reset moved the run, whose points are then other events.
        const next = caught.problem['preview'] as FlowResetPreview | undefined
        setConfirming(false)
        setNotice(caught.detail)
        if (next) setPreview(next)
        else {
          setPreview(undefined)
          setEventId(undefined)
          points.refresh()
        }
      } else {
        setError(caught instanceof ApiError ? caught.detail : String(caught))
      }
    } finally {
      setBusy(false)
    }
  }

  const shown = notice && (
    <p role="status" className="mt-2 text-[13px] text-warn">
      {notice}
    </p>
  )
  if (points.error) {
    return (
      <p role="alert" className="mt-2 text-[13px] text-warn">
        {points.error.message}
      </p>
    )
  }
  if (!points.data) return <p className="mt-2 text-[13px] text-muted">Loading the points a reset can go back to…</p>
  if (points.data.points.length === 0) {
    return (
      <>
        {shown}
        <p className="mt-2 text-[13px] text-muted">There is no point to reset this run to yet.</p>
      </>
    )
  }

  const byStep = new Map(steps.map((s) => [s.seq, s]))
  return (
    <div className="mt-3 flex flex-col gap-3">
      {shown}
      <label className="flex flex-col gap-1 text-[12.5px] text-muted">
        Reset to
        <select
          aria-label="Reset point"
          className={FIELD}
          value={eventId ?? ''}
          onChange={(e) => void choose(Number(e.target.value))}
        >
          <option value="" disabled>
            Choose a point…
          </option>
          {points.data.points.map((p) => {
            const step = p.first_step != null ? byStep.get(p.first_step) : undefined
            const where =
              p.first_step == null
                ? 'after the last step'
                : `before step ${p.first_step}${step ? ` (${step.fn})` : ''}`
            return (
              <option key={p.event_id} value={p.event_id}>
                {`Event ${p.event_id}: ${where}`}
              </option>
            )
          })}
        </select>
      </label>
      {previewError && (
        <p role="alert" className="text-[13px] text-warn">
          {previewError}
        </p>
      )}
      {eventId !== undefined && !again && !previewError && <p className="text-[13px] text-muted">Reading the preview…</p>}
      {preview && again && !preview.valid && (
        <p className="text-[13px] text-warn">A reset can&apos;t go back to this point any more. Choose another.</p>
      )}
      {preview && again && preview.valid && (
        <>
          <div>
            <h3 className="mb-1 text-[12.5px] font-medium">Runs again</h3>
            {again.length === 0 ? (
              <p className="text-[13px] text-muted">No recorded call runs again.</p>
            ) : (
              <ul aria-label="Calls that run again" className="flex flex-col gap-1 text-[12.5px]">
                {again.map((a) => (
                  <li key={a.key} data-outward={a.outward || undefined} className="flex items-center gap-2">
                    <span className="font-mono">{a.label}</span>
                    {a.outward && (
                      <span className="rounded-[4px] border border-warn/50 px-1.5 text-[11px] text-warn">
                        Outward: sent again
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-[12.5px] text-muted">
              Answers and approvals given after this point are undone, so those calls wait for you again.
            </p>
          </div>
          <div>
            <Button variant="danger" size="sm" onClick={() => setConfirming(true)}>
              Reset to this point…
            </Button>
          </div>
        </>
      )}
      <Dialog
        open={confirming}
        title="Reset this run?"
        onClose={() => !busy && setConfirming(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirming(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void reset()} disabled={busy} {...USER_ONLY}>
              {busy ? <Spinner /> : 'Reset run'}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 text-[13px] text-muted">
          <p>The run continues from event {eventId}, on the same script, and every call after it runs again.</p>
          {outward.length > 0 ? (
            <>
              <p className="text-warn">
                {outward.length === 1 ? 'This outward call is' : `These ${outward.length} outward calls are`} sent again,
                as new effects:
              </p>
              <ul aria-label="Outward calls sent again" className="list-disc pl-5 font-mono text-[12.5px]">
                {outward.map((a) => (
                  <li key={a.key}>{a.label}</li>
                ))}
              </ul>
            </>
          ) : (
            <p>No outward call (a print, an arrange) is sent again.</p>
          )}
          {error && (
            <p role="alert" className="text-warn">
              {error}
            </p>
          )}
        </div>
      </Dialog>
    </div>
  )
}
