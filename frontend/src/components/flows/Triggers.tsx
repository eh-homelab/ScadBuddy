import { useId, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { FlowGrantSpec, FlowTrigger, FlowTriggerCreate, FlowTriggerFire } from '../../api/types'

import { inputFields, type InputField } from '../../lib/flowInputs'
import { timeAgo } from '../../lib/format'
import { actorText, flowRunPath } from '../../lib/flows'
import { grantStatus } from '../../lib/grants'
import { useAsync } from '../../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '../ui/spinner'
import { Confirm } from './Confirm'
import { GrantEditor } from './GrantEditor'
import { useInputs } from './inputsForm'

const CHANGED = 'https://scadbuddy.dev/problems/flow-trigger-changed'
const when = (iso: string) => new Date(iso).toLocaleString()

function errorText(caught: unknown): string {
  if (!(caught instanceof ApiError)) return String(caught)
  if (caught.problem.type === CHANGED) return 'This trigger changed since it was shown. Look at it again, then decide.'
  return caught.detail
}

function every(seconds: number): string {
  if (seconds % 86_400 === 0) return seconds === 86_400 ? 'every day' : `every ${seconds / 86_400} days`
  if (seconds % 3_600 === 0) return seconds === 3_600 ? 'every hour' : `every ${seconds / 3_600} hours`
  if (seconds % 60 === 0) return `every ${seconds / 60} minutes`
  return `every ${seconds} seconds`
}

type EventKind = NonNullable<FlowTriggerCreate['event']>['kind']

const EVENT_TEXT: Record<string, string> = {
  'render.finished': 'When a render finishes',
  'model.changed': 'When a model changes',
  'print.finished': 'When a print finishes',
  'print.failed': 'When a print fails',
}

/** A trigger's timing, as one phrase. */
function triggerText(trigger: Pick<FlowTrigger, 'kind' | 'schedule' | 'event'>): string {
  if (trigger.kind === 'event' && trigger.event) {
    const slug = trigger.event.slug ? ` (${trigger.event.slug})` : ''
    return `${EVENT_TEXT[trigger.event.kind] ?? trigger.event.kind}${slug}`
  }
  const schedule = trigger.schedule
  if (!schedule) return 'Schedule'
  const zone = schedule.time_zone && schedule.time_zone !== 'UTC' ? `, ${schedule.time_zone}` : ''
  if (schedule.cron?.length) return `Cron ${schedule.cron.join('; ')}${zone || ' (UTC)'}`
  return `${every(schedule.interval_s ?? 0).replace(/^e/, 'E')}${zone}`
}

const OUTCOME: Record<FlowTriggerFire['outcome'], string> = {
  pending: 'Firing',
  admitted: 'Starting',
  started: 'Started',
  skipped: 'Skipped',
  failed: 'Failed',
}

/**
 * #2484 (spec 2026-10-11 §12) — a flow's triggers, each a schedule or an event that
 * starts runs of one pinned version as whoever made it: when it fires next, its last
 * fires and their runs, and its standing grant. Pausing, deleting and revoking a grant
 * only take authority away. Resuming, attaching a grant (approved as sent from here)
 * and approving or declining an assistant's are a person's own (`USER_ONLY`); each
 * decision names the trigger as it was shown (`seen`), and one that changed since is
 * refused and read again.
 */
export function TriggerList({
  triggers,
  onChanged,
  compact = false,
}: {
  triggers: FlowTrigger[]
  onChanged: () => void
  compact?: boolean
}) {
  if (triggers.length === 0) return compact ? null : <p className="text-[12.5px] text-muted-foreground">No triggers.</p>
  return (
    <ul aria-label="Triggers" className="flex flex-col gap-2">
      {triggers.map((trigger) => (
        <TriggerRow key={trigger.id} trigger={trigger} onChanged={onChanged} compact={compact} />
      ))}
    </ul>
  )
}

type Dialogs = 'attach' | 'approve' | 'delete' | 'revoke' | null

function TriggerRow({ trigger, onChanged, compact }: { trigger: FlowTrigger; onChanged: () => void; compact: boolean }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [open, setOpen] = useState<Dialogs>(null)
  const grant = trigger.grant
  const live = grant != null && !grant.revoked_at && grant.status !== 'declined' && (grant.in_force || grant.status === 'requested')
  const next = trigger.next_fires?.[0]
  const fires = (trigger.fires ?? []).slice(0, compact ? 1 : 5)

  async function act(send: () => Promise<unknown>): Promise<boolean> {
    setBusy(true)
    setError(undefined)
    try {
      await send()
      setOpen(null)
      onChanged()
      return true
    } catch (caught) {
      setError(errorText(caught))
      // A refusal because it changed: read it again, so the next decision is on what it is now.
      if (caught instanceof ApiError && caught.problem.type === CHANGED) {
        setOpen(null)
        onChanged()
      }
      return false
    } finally {
      setBusy(false)
    }
  }

  const confirm = (kind: 'delete' | 'revoke') => (next: boolean) => {
    setOpen(next ? kind : null)
    // A refusal shown in the confirmation goes with it.
    if (!next) setError(undefined)
  }

  return (
    <li data-trigger={trigger.id} aria-label={`Trigger ${triggerText(trigger)}`}>
      <Card size="sm" className="gap-1 px-3 text-[12.5px]">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[13px] font-medium">{triggerText(trigger)}</span>
          {trigger.version != null && <span className="text-muted-foreground">runs v{trigger.version}</span>}
          {trigger.paused && (
            <Badge variant="secondary">Paused{trigger.paused_reason ? `: ${trigger.paused_reason}` : ''}</Badge>
          )}
          <span className="ml-auto flex flex-wrap gap-1.5">
            {trigger.paused ? (
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => void act(() => api.resumeFlowTrigger(trigger.id))}
                {...USER_ONLY}
              >
                Resume
              </Button>
            ) : (
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(() => api.pauseFlowTrigger(trigger.id))}>
                Pause
              </Button>
            )}
            {!compact && (
              <Confirm
                trigger={
                  <Button variant="destructive" size="sm" disabled={busy} {...USER_ONLY}>
                    Delete
                  </Button>
                }
                open={open === 'delete'}
                onOpenChange={confirm('delete')}
                title="Delete this trigger?"
                action="Delete"
                busy={busy}
                error={error}
                onAction={() => void act(() => api.deleteFlowTrigger(trigger.id))}
              >
                <p>It fires no more, and its grant is revoked, with the grants of the runs it started.</p>
              </Confirm>
            )}
          </span>
        </div>
        <p className="text-muted-foreground">
          {trigger.kind === 'schedule' && !trigger.paused && next ? `Next ${when(next)}. ` : ''}
          {!compact && `Made by ${actorText(trigger.created_by)}, ${timeAgo(trigger.created_at)}.`}
        </p>
        {fires.length > 0 && (
          <ul aria-label="Last fires" className="flex flex-col gap-0.5">
            {fires.map((fire) => (
              <li key={fire.fire_key} className="flex flex-wrap gap-x-2">
                <span className="text-faint">{timeAgo(fire.fired_at)}</span>
                <span>{OUTCOME[fire.outcome]}</span>
                {fire.reason && <span className="text-muted-foreground">({fire.reason})</span>}
                {fire.outcome === 'started' && (
                  <Link to={flowRunPath(fire.run_id)} className="text-primary hover:underline">
                    {fire.run_status ? `run, ${fire.run_status}` : 'run'}
                  </Link>
                )}
              </li>
            ))}
          </ul>
        )}
        {!compact && (
          <div className="mt-0.5 flex flex-wrap items-center gap-2">
            <span>
              <span className="text-muted-foreground">Grant: </span>
              <span data-testid="standing-grant-status">
                {grant ? `${grantStatus(grant)}${grant.status === 'requested' ? ` by ${actorText(grant.requested_by)}` : ''}` : 'None'}
              </span>
              {grant?.in_force && grant.expires_at && (
                <span className="text-muted-foreground">, until {when(grant.expires_at)}</span>
              )}
            </span>
            {grant?.status === 'requested' && !grant.revoked_at ? (
              <>
                <Button variant="outline" size="sm" disabled={busy} onClick={() => setOpen('approve')} {...USER_ONLY}>
                  Review…
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void act(() => api.declineFlowTriggerGrant(trigger.id))}
                  {...USER_ONLY}
                >
                  Decline
                </Button>
              </>
            ) : live ? (
              <Confirm
                trigger={
                  <Button size="sm" variant="ghost" disabled={busy} {...USER_ONLY}>
                    Revoke
                  </Button>
                }
                open={open === 'revoke'}
                onOpenChange={confirm('revoke')}
                title="Revoke its grant?"
                action="Revoke"
                busy={busy}
                error={error}
                onAction={() => void act(() => api.revokeFlowTriggerGrant(trigger.id))}
              >
                <p>Its runs, running and to come, ask you before every outward call.</p>
              </Confirm>
            ) : (
              <Button variant="outline" size="sm" disabled={busy} onClick={() => setOpen('attach')} {...USER_ONLY}>
                Attach a grant…
              </Button>
            )}
            {!grant?.in_force && (
              <span className="text-muted-foreground">Without one, its runs’ outward calls wait for you.</span>
            )}
          </div>
        )}
        {error && open === null && (
          <p role="alert" className="text-destructive">
            {error}
          </p>
        )}
        <Dialog open={open === 'attach' || open === 'approve'} onOpenChange={(next) => !next && !busy && setOpen(null)}>
          <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
            <DialogHeader>
              <DialogTitle>{open === 'attach' ? 'Attach a standing grant' : 'Review the standing grant'}</DialogTitle>
              <DialogDescription>
                What each run of “{triggerText(trigger)}” may do without asking you, spent across all its runs. At most 30
                days.
              </DialogDescription>
            </DialogHeader>
            <GrantEditor
              standing
              initial={open === 'approve' ? grant?.spec : undefined}
              submitLabel={open === 'attach' ? 'Attach' : 'Approve'}
              busy={busy}
              onSubmit={(spec: FlowGrantSpec) =>
                void act(() =>
                  open === 'attach'
                    ? api.attachFlowTriggerGrant(trigger.id, spec, trigger.seen)
                    : api.approveFlowTriggerGrant(trigger.id, trigger.seen, spec),
                )
              }
              onCancel={() => !busy && setOpen(null)}
            />
            {error && (
              <p role="alert" className="text-[13px] text-destructive">
                {error}
              </p>
            )}
          </DialogContent>
        </Dialog>
      </Card>
    </li>
  )
}

/** The flow page's section: a version's triggers, and a new one pinned to it. */
export function FlowTriggers({
  name,
  definitionId,
  schema,
  disabled,
}: {
  name: string
  definitionId: string
  schema: Record<string, unknown> | null
  disabled: boolean
}) {
  const triggers = useAsync(() => api.listFlowTriggers(name), [name])
  const [creating, setCreating] = useState(false)
  return (
    <section aria-label="Triggers">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-[13px] font-medium">Triggers</h2>
        {!disabled && (
          <Button variant="outline" size="sm" className="ml-auto" onClick={() => setCreating(true)} {...USER_ONLY}>
            New trigger…
          </Button>
        )}
      </div>
      {triggers.error ? (
        <p role="alert" className="text-[12.5px] text-destructive">
          {triggers.error.message}
        </p>
      ) : !triggers.data ? (
        <Skeleton role="status" aria-label="Loading the triggers" className="h-16 w-full rounded-xl" />
      ) : (
        <TriggerList triggers={triggers.data} onChanged={() => triggers.refresh()} />
      )}
      {creating && (
        <NewTrigger
          definitionId={definitionId}
          fields={inputFields(schema)}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false)
            triggers.refresh()
          }}
        />
      )}
    </section>
  )
}

function NewTrigger({
  definitionId,
  fields,
  onClose,
  onCreated,
}: {
  definitionId: string
  fields: InputField[]
  onClose: () => void
  onCreated: () => void
}) {
  const formId = useId()
  const id = (name: string) => `${formId}-${name}`
  const { parse, rendered } = useInputs(fields)
  const [kind, setKind] = useState<'schedule' | 'event'>('schedule')
  const [repeat, setRepeat] = useState<'interval' | 'cron'>('interval')
  const [minutes, setMinutes] = useState('60')
  const [cron, setCron] = useState('0 9 * * *')
  const [zone, setZone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
  const [event, setEvent] = useState('render.finished')
  const [slug, setSlug] = useState('')
  const [skip, setSkip] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  async function submit(e: FormEvent) {
    e.preventDefault()
    const inputs = fields.length > 0 ? parse() : undefined
    if (inputs === null) return
    // A Schedule's catch-up window: the backend's default, five minutes.
    const timing = { time_zone: zone, offset_s: 0, catchup_window_s: 300 }
    const body: FlowTriggerCreate = {
      definition_id: definitionId,
      overlap: skip ? 'skip' : 'allow_all',
      ...(inputs ? { inputs } : {}),
      ...(kind === 'schedule'
        ? {
            schedule:
              repeat === 'interval'
                ? { interval_s: Math.round(Number(minutes) * 60), ...timing }
                : { cron: [cron.trim()], ...timing },
          }
        : { event: { kind: event as EventKind, ...(slug.trim() ? { slug: slug.trim() } : {}) } }),
    }
    setBusy(true)
    setError(undefined)
    try {
      await api.createFlowTrigger(body)
      onCreated()
    } catch (caught) {
      setError(errorText(caught))
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New trigger</DialogTitle>
          <DialogDescription>
            Starts runs of this version, as you, on a schedule or when something happens. Its runs ask you before
            outward calls until you attach a grant.
          </DialogDescription>
        </DialogHeader>
        <form id={formId} noValidate onSubmit={(e) => void submit(e)} className="flex flex-col gap-4">
          <RadioGroup
            aria-label="Kind"
            value={kind}
            onValueChange={(next) => setKind(next as 'schedule' | 'event')}
            className="flex flex-wrap gap-4"
          >
            <div className="flex items-center gap-2">
              <RadioGroupItem id={id('schedule')} value="schedule" />
              <Label htmlFor={id('schedule')}>On a schedule</Label>
            </div>
            <div className="flex items-center gap-2">
              <RadioGroupItem id={id('event')} value="event" />
              <Label htmlFor={id('event')}>On an event</Label>
            </div>
          </RadioGroup>
          {kind === 'schedule' ? (
            <>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={id('repeat')}>Repeat</Label>
                <Select value={repeat} onValueChange={(next) => setRepeat(next as 'interval' | 'cron')}>
                  <SelectTrigger id={id('repeat')} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="interval">Every so many minutes</SelectItem>
                    <SelectItem value="cron">Cron expression</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {repeat === 'interval' ? (
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={id('minutes')}>Minutes</Label>
                  <Input
                    id={id('minutes')}
                    type="number"
                    min={1}
                    step={1}
                    value={minutes}
                    onChange={(e) => setMinutes(e.target.value)}
                  />
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={id('cron')}>Cron</Label>
                  <Input id={id('cron')} className="font-mono" value={cron} onChange={(e) => setCron(e.target.value)} />
                </div>
              )}
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={id('zone')}>Time zone</Label>
                <Input id={id('zone')} value={zone} onChange={(e) => setZone(e.target.value)} />
              </div>
            </>
          ) : (
            <>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={id('event-kind')}>Event</Label>
                <Select value={event} onValueChange={setEvent}>
                  <SelectTrigger id={id('event-kind')} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="render.finished">A render finishes</SelectItem>
                    <SelectItem value="model.changed">A model changes</SelectItem>
                    <SelectItem value="print.finished">A print finishes</SelectItem>
                    <SelectItem value="print.failed">A print fails</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={id('slug')}>Only for model (optional)</Label>
                <Input id={id('slug')} value={slug} onChange={(e) => setSlug(e.target.value)} />
              </div>
            </>
          )}
          <div className="flex items-center gap-2">
            <Checkbox id={id('skip')} checked={skip} onCheckedChange={(next) => setSkip(next === true)} />
            <Label htmlFor={id('skip')}>Skip a fire while its last run is still going</Label>
          </div>
          {fields.length > 0 && (
            <section aria-label="Inputs" className="flex flex-col gap-3">
              <h3 className="text-[13px] font-medium">Inputs</h3>
              {rendered}
            </section>
          )}
          {error && (
            <p role="alert" className="text-[13px] whitespace-pre-wrap text-destructive">
              {error}
            </p>
          )}
        </form>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={busy} aria-busy={busy} {...USER_ONLY}>
            {busy && <Spinner />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
