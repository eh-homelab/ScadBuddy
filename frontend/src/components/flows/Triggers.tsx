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
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'
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

const EVENT_TEXT: Record<string, string> = {
  'render.finished': 'When a render finishes',
  'model.changed': 'When a model changes',
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
  if (triggers.length === 0) return compact ? null : <p className="text-[12.5px] text-muted">No triggers.</p>
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

  return (
    <li
      data-trigger={trigger.id}
      className="rounded-[8px] border border-line bg-surface-2 px-3 py-2 text-[12.5px]"
      aria-label={`Trigger ${triggerText(trigger)}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[13px] font-medium">{triggerText(trigger)}</span>
        {trigger.version != null && <span className="text-muted">runs v{trigger.version}</span>}
        {trigger.paused && (
          <span className="rounded-[4px] border border-line px-1.5 text-[11.5px] text-muted">
            Paused{trigger.paused_reason ? `: ${trigger.paused_reason}` : ''}
          </span>
        )}
        <span className="ml-auto flex flex-wrap gap-1.5">
          {trigger.paused ? (
            <Button size="sm" disabled={busy} onClick={() => void act(() => api.resumeFlowTrigger(trigger.id))} {...USER_ONLY}>
              Resume
            </Button>
          ) : (
            <Button size="sm" disabled={busy} onClick={() => void act(() => api.pauseFlowTrigger(trigger.id))}>
              Pause
            </Button>
          )}
          {!compact && (
            <Button size="sm" variant="danger" disabled={busy} onClick={() => setOpen('delete')} {...USER_ONLY}>
              Delete
            </Button>
          )}
        </span>
      </div>
      <p className="mt-0.5 text-muted">
        {trigger.kind === 'schedule' && !trigger.paused && next ? `Next ${when(next)}. ` : ''}
        {!compact && `Made by ${actorText(trigger.created_by)}, ${timeAgo(trigger.created_at)}.`}
      </p>
      {fires.length > 0 && (
        <ul aria-label="Last fires" className="mt-1 flex flex-col gap-0.5">
          {fires.map((fire) => (
            <li key={fire.fire_key} className="flex flex-wrap gap-x-2">
              <span className="text-faint">{timeAgo(fire.fired_at)}</span>
              <span>{OUTCOME[fire.outcome]}</span>
              {fire.reason && <span className="text-muted">({fire.reason})</span>}
              {fire.outcome === 'started' && (
                <Link to={flowRunPath(fire.run_id)} className="text-accent hover:underline">
                  {fire.run_status ? `run, ${fire.run_status}` : 'run'}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
      {!compact && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <span>
            <span className="text-muted">Grant: </span>
            <span data-testid="standing-grant-status">
              {grant ? `${grantStatus(grant)}${grant.status === 'requested' ? ` by ${actorText(grant.requested_by)}` : ''}` : 'None'}
            </span>
            {grant?.in_force && grant.expires_at && <span className="text-muted">, until {when(grant.expires_at)}</span>}
          </span>
          {grant?.status === 'requested' && !grant.revoked_at ? (
            <>
              <Button size="sm" disabled={busy} onClick={() => setOpen('approve')} {...USER_ONLY}>
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
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setOpen('revoke')} {...USER_ONLY}>
              Revoke
            </Button>
          ) : (
            <Button size="sm" disabled={busy} onClick={() => setOpen('attach')} {...USER_ONLY}>
              Attach a grant…
            </Button>
          )}
          {!grant?.in_force && (
            <span className="text-muted">Without one, its runs’ outward calls wait for you.</span>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-warn">
          {error}
        </p>
      )}
      {(open === 'attach' || open === 'approve') && (
        <Dialog
          open
          title={open === 'attach' ? 'Attach a standing grant' : 'Review the standing grant'}
          description={`What each run of “${triggerText(trigger)}” may do without asking you, spent across all its runs. At most 30 days.`}
          onClose={() => !busy && setOpen(null)}
        >
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
            onCancel={() => setOpen(null)}
          />
        </Dialog>
      )}
      {(open === 'delete' || open === 'revoke') && (
        <Dialog
          open
          title={open === 'delete' ? 'Delete this trigger?' : 'Revoke its grant?'}
          description={
            open === 'delete'
              ? 'It fires no more, and its grant is revoked, with the grants of the runs it started.'
              : 'Its runs, running and to come, ask you before every outward call.'
          }
          onClose={() => !busy && setOpen(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setOpen(null)} disabled={busy}>
                Cancel
              </Button>
              <Button
                variant="danger"
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    open === 'delete' ? api.deleteFlowTrigger(trigger.id) : api.revokeFlowTriggerGrant(trigger.id),
                  )
                }
              >
                {busy ? <Spinner /> : open === 'delete' ? 'Delete' : 'Revoke'}
              </Button>
            </>
          }
        >
          {error && (
            <p role="alert" className="text-[13px] text-warn">
              {error}
            </p>
          )}
        </Dialog>
      )}
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
      <div className="mb-1 flex items-center gap-2">
        <h2 className="text-[13px] font-medium">Triggers</h2>
        {!disabled && (
          <Button size="sm" className="ml-auto" onClick={() => setCreating(true)} {...USER_ONLY}>
            New trigger…
          </Button>
        )}
      </div>
      {triggers.error ? (
        <p role="alert" className="text-[12.5px] text-warn">
          {triggers.error.message}
        </p>
      ) : !triggers.data ? (
        <p className="text-[12.5px] text-muted">Loading…</p>
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

const CONTROL = 'w-full rounded-[6px] border border-line bg-surface px-2 py-1.5 text-[13px] text-ink'
const LABEL = 'flex flex-col gap-1 text-[13px]'

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
        : { event: { kind: event as 'render.finished' | 'model.changed', ...(slug.trim() ? { slug: slug.trim() } : {}) } }),
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
    <Dialog
      open
      title="New trigger"
      description="Starts runs of this version, as you, on a schedule or when something happens. Its runs ask you before outward calls until you attach a grant."
      onClose={() => !busy && onClose()}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form={formId} disabled={busy} {...USER_ONLY}>
            {busy ? <Spinner /> : 'Create'}
          </Button>
        </>
      }
    >
      <form id={formId} noValidate onSubmit={(e) => void submit(e)} className="flex flex-col gap-3">
        <fieldset className="flex gap-4 text-[13px]">
          <legend className="sr-only">Kind</legend>
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={kind === 'schedule'} onChange={() => setKind('schedule')} />
            On a schedule
          </label>
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={kind === 'event'} onChange={() => setKind('event')} />
            On an event
          </label>
        </fieldset>
        {kind === 'schedule' ? (
          <>
            <label className={LABEL}>
              Repeat
              <select className={CONTROL} value={repeat} onChange={(e) => setRepeat(e.target.value as 'interval' | 'cron')}>
                <option value="interval">Every so many minutes</option>
                <option value="cron">Cron expression</option>
              </select>
            </label>
            {repeat === 'interval' ? (
              <label className={LABEL}>
                Minutes
                <input
                  className={CONTROL}
                  type="number"
                  min={1}
                  step={1}
                  value={minutes}
                  onChange={(e) => setMinutes(e.target.value)}
                />
              </label>
            ) : (
              <label className={LABEL}>
                Cron
                <input className={`${CONTROL} font-mono`} value={cron} onChange={(e) => setCron(e.target.value)} />
              </label>
            )}
            <label className={LABEL}>
              Time zone
              <input className={CONTROL} value={zone} onChange={(e) => setZone(e.target.value)} />
            </label>
          </>
        ) : (
          <>
            <label className={LABEL}>
              Event
              <select className={CONTROL} value={event} onChange={(e) => setEvent(e.target.value)}>
                <option value="render.finished">A render finishes</option>
                <option value="model.changed">A model changes</option>
              </select>
            </label>
            <label className={LABEL}>
              Only for model (optional)
              <input className={CONTROL} value={slug} onChange={(e) => setSlug(e.target.value)} />
            </label>
          </>
        )}
        <label className="flex items-center gap-2 text-[13px]">
          <input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} />
          Skip a fire while its last run is still going
        </label>
        {fields.length > 0 && (
          <section aria-label="Inputs" className="flex flex-col gap-3">
            <h3 className="text-[13px] font-medium">Inputs</h3>
            {rendered}
          </section>
        )}
        {error && (
          <p role="alert" className="whitespace-pre-wrap text-[13px] text-warn">
            {error}
          </p>
        )}
      </form>
    </Dialog>
  )
}
