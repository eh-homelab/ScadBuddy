import { useRef, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { FlowResetPreview, FlowStep } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { ChevronRightIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Confirm } from './Confirm'

const RESET_CHANGED = 'https://scadbuddy.dev/problems/flow-reset-changed'

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
    <Card size="sm" role="region" aria-label="Reset" className="mt-4">
      <CardContent>
        <Collapsible open={open} onOpenChange={setOpen}>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[13px] font-medium">Reset</h2>
            <p className="text-[12.5px] text-muted-foreground">Run the flow again from an earlier point.</p>
            <CollapsibleTrigger asChild>
              <Button variant="outline" size="sm" className="ml-auto">
                <ChevronRightIcon className={`transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
                {open ? 'Hide' : 'Preview a reset'}
              </Button>
            </CollapsibleTrigger>
          </div>
          <CollapsibleContent>
            <ResetChooser runId={runId} steps={steps} onReset={onReset} />
          </CollapsibleContent>
        </Collapsible>
      </CardContent>
    </Card>
  )
}

function ResetChooser({ runId, steps, onReset }: { runId: string; steps: FlowStep[]; onReset: () => void }) {
  const points = useAsync(() => api.getFlowResetPoints(runId), [runId])
  const [eventId, setEventId] = useState<number>()
  const chosen = useRef<number | undefined>(undefined)
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
    chosen.current = next
    setEventId(next)
    setPreview(undefined)
    setPreviewError(undefined)
    setNotice(undefined)
    try {
      const read = await api.previewFlowReset(runId, next)
      // A preview of a point chosen before this one, answered late, is not shown.
      if (chosen.current === next) setPreview(read)
    } catch (caught) {
      if (chosen.current === next) setPreviewError(caught instanceof ApiError ? caught.detail : String(caught))
    }
  }

  async function reset() {
    if (!preview) return
    setBusy(true)
    setError(undefined)
    try {
      await api.resetFlowRun(runId, preview)
      setConfirming(false)
      chosen.current = undefined
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
          chosen.current = undefined
          setPreview(undefined)
          setEventId(undefined)
        }
        // Read with it, so what runs again is labelled from the same history.
        points.refresh()
      } else {
        setError(caught instanceof ApiError ? caught.detail : String(caught))
      }
    } finally {
      setBusy(false)
    }
  }

  const shown = notice && (
    <p role="status" className="mt-2 text-[13px] text-destructive">
      {notice}
    </p>
  )
  if (points.error) {
    return (
      <p role="alert" className="mt-2 text-[13px] text-destructive">
        {points.error.message}
      </p>
    )
  }
  if (!points.data) {
    return (
      <div role="status" aria-label="Loading the points a reset can go back to" className="mt-3 flex flex-col gap-2">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-8 w-full" />
      </div>
    )
  }
  if (points.data.points.length === 0) {
    return (
      <>
        {shown}
        <p className="mt-2 text-[13px] text-muted-foreground">There is no point to reset this run to yet.</p>
      </>
    )
  }

  const byStep = new Map(steps.map((s) => [s.seq, s]))
  return (
    <div className="mt-3 flex flex-col gap-3">
      {shown}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`reset-point-${runId}`} className="text-[12.5px] font-normal text-muted-foreground">
          Reset to
        </Label>
        <Select value={eventId === undefined ? '' : String(eventId)} onValueChange={(next) => void choose(Number(next))}>
          <SelectTrigger id={`reset-point-${runId}`} aria-label="Reset point" className="w-full">
            <SelectValue placeholder="Choose a point…" />
          </SelectTrigger>
          <SelectContent>
            {points.data.points.map((p) => {
              const step = p.first_step != null ? byStep.get(p.first_step) : undefined
              const where =
                p.first_step == null
                  ? 'after the last step'
                  : `before step ${p.first_step}${step ? ` (${step.fn})` : ''}`
              return (
                <SelectItem key={p.event_id} value={String(p.event_id)}>
                  {`Event ${p.event_id}: ${where}`}
                </SelectItem>
              )
            })}
          </SelectContent>
        </Select>
      </div>
      {previewError && (
        <p role="alert" className="text-[13px] text-destructive">
          {previewError}
        </p>
      )}
      {eventId !== undefined && !again && !previewError && (
        <div role="status" aria-label="Reading the preview" className="flex flex-col gap-1.5">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-3 w-40" />
        </div>
      )}
      {preview && again && !preview.valid && (
        <p className="text-[13px] text-destructive">A reset can&apos;t go back to this point any more. Choose another.</p>
      )}
      {preview && again && preview.valid && (
        <>
          <div>
            <h3 className="mb-1 text-[12.5px] font-medium">Runs again</h3>
            {again.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">No recorded call runs again.</p>
            ) : (
              <ul aria-label="Calls that run again" className="flex flex-col gap-1 text-[12.5px]">
                {again.map((a) => (
                  <li key={a.key} data-outward={a.outward || undefined} className="flex items-center gap-2">
                    <span className="font-mono">{a.label}</span>
                    {a.outward && (
                      <Badge variant="destructive">Outward: sent again</Badge>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-[12.5px] text-muted-foreground">
              Answers and approvals given after this point are undone, so those calls wait for you again.
            </p>
          </div>
          <div>
            <Confirm
              trigger={
                <Button variant="destructive" size="sm">
                  Reset to this point…
                </Button>
              }
              open={confirming}
              onOpenChange={setConfirming}
              title="Reset this run?"
              action="Reset run"
              busy={busy}
              error={error}
              onAction={() => void reset()}
            >
              <p>The run continues from event {eventId}, on the same script, and every call after it runs again.</p>
              {outward.length > 0 ? (
                <>
                  <p className="text-destructive">
                    {outward.length === 1 ? 'This outward call is' : `These ${outward.length} outward calls are`} sent
                    again, as new effects:
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
            </Confirm>
          </div>
        </>
      )}
    </div>
  )
}
