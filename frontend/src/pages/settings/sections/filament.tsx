import { useEffect, useState, type ReactNode } from 'react'
import { ApiError } from '../../../api/client'
import {
  getRfidStatus,
  getSyncStatus,
  reconcileRfid,
  runSync,
  setOverrideMode,
  type RfidReconcileReport,
  type RfidStatus,
  type SyncError,
  type SyncStatus,
} from '../../../api/filament'
import { AppLink } from '../../../components/AppLink'
import { spoolPath } from '../../../components/filament/spoolsQuery'
import { ChoiceSelect } from '../../../components/settings/form'
import { shortId, timeAgo } from '../../../lib/format'
import { useAsync, type AsyncState } from '../../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { FieldRow } from '../controls'
import { docOf } from '../fields'
import { FieldBadge, RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings, type BackendSettings } from '../useBackendSettings'
import { FETCH_NOZZLES, fieldsOf, nozzlesValue, SYNC_KINDS, syncParts, syncValue, type SyncDirection, type SyncKind } from '../values'

export const id = 'filament'
export const area: AreaId = 'printing'
export const title = 'Filament'
export const order = 30

const LINK = 'underline underline-offset-2 hover:text-foreground'

/** #6265 — the four directions each data type takes. */
const DIRECTION_CHOICES = [
  { value: 'off', label: 'Off' },
  { value: 'sb_to_bb', label: 'ScadBuddy → Bambuddy' },
  { value: 'bb_to_sb', label: 'Bambuddy → ScadBuddy' },
  { value: 'two_way', label: 'Both ways (newer change wins)' },
] as const satisfies readonly { value: SyncDirection; label: string }[]

/** Each data type's row: its label and what it moves. */
const SYNC_ROWS: Record<SyncKind, { label: string; help: string }> = {
  spools: {
    label: 'Spools and types',
    help: "A spool's own fields, new spools and the spool catalog. With spools off, no spool is created or linked, so weights, presets, suppliers per spool and a spool's location move only on spools linked earlier. Supplier and location rows themselves sync on their own setting.",
  },
  weights: {
    label: 'Weights',
    help: 'Weight readings, scale weighings, dryings and the weight lock of spools ScadBuddy already tracks. A new spool still gets its first Bambuddy weight reading.',
  },
  tags: {
    label: 'Tags',
    help: 'Tag bindings. A new Bambuddy spool takes its tags whatever this says.',
  },
  suppliers: {
    label: 'Suppliers and prices',
    help: "Supplier rows. Each spool's suppliers and prices only ever go to Bambuddy, where they replace Bambuddy's; they are never taken from it.",
  },
  locations: {
    label: 'Locations',
    help: 'Location rows and where each spool is.',
  },
  presets: {
    label: 'Presets and K values',
    help: "Each spool's filament presets and K values.",
  },
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** What the override mode's status says, in a sentence. */
function statusLine(status: RfidStatus): string {
  switch (status.state) {
    case 'applied':
      return 'Bambuddy no longer adds unknown RFID spools'
    case 'blocked':
      return `Blocked: ${status.detail ?? 'Bambuddy refused the change'}`
    case 'pending':
      return status.detail === 'busy'
        ? 'Another update is running; it applies on the next refresh'
        : `Not applied yet${status.detail ? `: ${status.detail}` : '. It applies on the next refresh'}`
    default:
      return 'Bambuddy adds a spool for each RFID tag it does not know'
  }
}

function reportLine(report: RfidReconcileReport): string {
  const parts = [plural(report.tags.pushed, 'tag sent', 'tags sent')]
  if (report.tags.failed > 0) parts.push(`${plural(report.tags.failed, 'tag was', 'tags were')} not sent; they stay queued for the next refresh`)
  const failedSlots = report.slots?.failed ?? 0
  if (failedSlots > 0) parts.push(`${plural(failedSlots, 'slot was', 'slots were')} not set; ${failedSlots === 1 ? 'it is' : 'they are'} tried again on the next refresh`)
  const noTemps = report.slots?.no_temps ?? 0
  if (noTemps > 0) parts.push(`${plural(noTemps, 'spool has', 'spools have')} no nozzle temperatures, so its slot is not set`)
  const noPreset = report.slots?.no_preset ?? 0
  if (noPreset > 0) parts.push(`${plural(noPreset, 'spool has', 'spools have')} no preset for the nozzle, so ${noPreset === 1 ? 'its slot is' : 'their slots are'} not set`)
  const noNozzle = report.slots?.no_nozzle ?? 0
  if (noNozzle > 0) parts.push(`${plural(noNozzle, 'slot', 'slots')}: the printer does not report the nozzle for ${noNozzle === 1 ? 'its tray, so it is' : 'their trays, so they are'} not set`)
  return `${parts.join('. ')}.`
}

/**
 * #6265 — which way each kind of filament data moves between ScadBuddy and Bambuddy. The six
 * directions are one setting, `filament_sync`, held in the form as one string (`syncParts`,
 * `syncValue`) and saved with "Save Filament". The first row carries the setting's badge,
 * environment variable and error.
 */
function SyncRows({ form }: { form: BackendSettings }) {
  const sync = syncParts(form.value('filament_sync'))
  const doc = docOf(form.settings, 'filament_sync')
  return (
    <>
      {SYNC_KINDS.map((kind, i) => {
        const id = i === 0 ? 'setting-filament_sync' : `setting-filament-sync-${kind}`
        return (
          <FieldRow
            key={kind}
            id={id}
            label={SYNC_ROWS[kind].label}
            help={SYNC_ROWS[kind].help}
            {...(i === 0 ? { env: doc.env, badge: <FieldBadge form={form} name="filament_sync" />, error: form.errors.filament_sync } : {})}
          >
            <ChoiceSelect
              id={id}
              value={sync[kind]}
              onValueChange={(next) => form.setField('filament_sync', syncValue({ ...sync, [kind]: next as SyncDirection }))}
              choices={DIRECTION_CHOICES}
            />
          </FieldRow>
        )
      })}
    </>
  )
}

const KIND_LABELS: Record<SyncError['kind'], string> = {
  spool: 'Spool',
  supplier: 'Supplier',
  location: 'Location',
  insert_type: 'Insert type',
  filament: 'Spool type',
}

/** Where an item that failed to sync is shown, or null for one with no page. */
function itemHref(error: SyncError): string | null {
  switch (error.kind) {
    case 'spool':
      return spoolPath({ id: error.sb_id })
    case 'supplier':
      return `/filament/suppliers/${encodeURIComponent(error.sb_id)}`
    case 'filament':
      return `/filament/types/${encodeURIComponent(error.sb_id)}`
    case 'location':
      return '/filament/locations'
    default:
      return null
  }
}

const PROBLEMS = 'https://scadbuddy.dev/problems/'

/** An error label (`rfid_push.error_label`: a problem type, `:<status>` when Bambuddy answered one) in plain words. */
function syncErrorText(label: string): string {
  if (label === 'name_taken') return 'Not sent: another Bambuddy row has this name. Rename one of them.'
  if (label === 'internal') return 'ScadBuddy failed while it wrote this.'
  const [type, status] = label.startsWith(PROBLEMS) ? label.slice(PROBLEMS.length).split(':') : [label, undefined]
  const code = status ? ` (HTTP ${status})` : ''
  switch (type) {
    case 'bambuddy-rejected':
      return `Bambuddy refused it${code}.`
    case 'bambuddy-unavailable':
      return `Bambuddy did not answer${code}.`
    case 'bambuddy-not-found':
      return `Bambuddy no longer has it${code}.`
    case 'bambuddy-conflict':
      return `Bambuddy reported a conflict${code}.`
    case 'bambuddy-scope':
      return `The Bambuddy API key may not make this change${code}.`
    case 'bambuddy-not-configured':
      return 'No Bambuddy is set up.'
    default:
      return label
  }
}

const SKIPPED_TEXT: Record<NonNullable<SyncStatus['skipped']>, string> = {
  no_bambuddy: 'No pass runs: no Bambuddy is set up.',
  nothing_pushes: 'No pass runs: no data type but tags is set to send to Bambuddy (tags go with the refresh).',
}

/** What "Sync now" says when the server refuses it, in plain words. */
function runProblem(cause: unknown): ReactNode {
  if (!(cause instanceof ApiError)) return 'The sync could not be asked for. Try again.'
  if (cause.status === 409 && cause.problem.code === 'no_bambuddy')
    return (
      <>
        No Bambuddy is set up. Add its address and API key in{' '}
        <AppLink href="/settings/general#connection" className={LINK}>
          Connection
        </AppLink>
        , then try again.
      </>
    )
  if (cause.status === 409 && cause.problem.code === 'nothing_pushes')
    return 'Nothing is set to go to Bambuddy. Set a data type to "ScadBuddy → Bambuddy" or "Both ways", save Filament, then try again.'
  if (cause.status === 503) return 'The sync could not be started. Try again later.'
  return cause.detail
}

/** R-T11-2: after "Sync now", how often the status is read until the pass lands, and for how long. */
const SYNC_POLL_MS = 3_000
const SYNC_POLL_LIMIT_MS = 60_000

function When({ at }: { at: string }) {
  return (
    <time dateTime={at} title={new Date(at).toLocaleString()}>
      {timeAgo(at)}
    </time>
  )
}

/** The last pass's counts, those above zero beyond the written and failed. */
function countsLine(counts: SyncStatus['counts']): string {
  const parts = [`${counts.written ?? 0} written`, `${counts.failed ?? 0} failed`]
  if (counts.deferred) parts.push(`${counts.deferred} deferred to the next pass`)
  if (counts.conflicts) parts.push(plural(counts.conflicts, 'conflict', 'conflicts'))
  if (counts.unmapped_k) parts.push(`${plural(counts.unmapped_k, 'K value', 'K values')} with no Bambuddy match`)
  return `${parts.join(', ')}.`
}

/**
 * #6265 — how the sync stands (`GET /filament/sync/status`): the last pass and the last clean
 * one, why no pass runs, and each item whose last write failed or was held back. "Sync now"
 * asks for a full pass, which runs in the background, then reads the status again.
 */
function SyncStatusRow({ status }: { status: AsyncState<SyncStatus> }) {
  const [pending, setPending] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [problem, setProblem] = useState<ReactNode>(null)
  /** While a pass asked for has not landed: the server's `requested_at` from the run's 202, as an ISO string. */
  const [waiting, setWaiting] = useState<{ requestedAt: string } | null>(null)
  const data = status.data
  const { setData } = status

  // R-T11-2 — the pass runs in the background: read the status every SYNC_POLL_MS until
  // a pass has started at or after the run's `requested_at` (both the server's clock, never the
  // browser's), for at most SYNC_POLL_LIMIT_MS. A timer pass or an earlier nudge's pass that
  // started before the request does not end it (Decision 162).
  // Unmounting stops it.
  useEffect(() => {
    if (!waiting) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let reads = 0
    const tick = async () => {
      reads += 1
      try {
        const next = await getSyncStatus()
        if (stopped) return
        setData(next, { supersede: true })
        if (next.last_pass_started_at && new Date(next.last_pass_started_at) >= new Date(waiting.requestedAt)) {
          setNote('Sync finished.')
          setWaiting(null)
          return
        }
      } catch {
        if (stopped) return
      }
      if (reads * SYNC_POLL_MS >= SYNC_POLL_LIMIT_MS) {
        setNote('The pass is still running. Check back later.')
        setWaiting(null)
        return
      }
      timer = setTimeout(() => void tick(), SYNC_POLL_MS)
    }
    timer = setTimeout(() => void tick(), SYNC_POLL_MS)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [waiting, setData])

  async function syncNow() {
    setPending(true)
    setNote(null)
    setProblem(null)
    try {
      const run = await runSync()
      setNote('Sync started. It runs in the background; this status updates when the pass is done.')
      setWaiting({ requestedAt: run.requested_at })
    } catch (cause) {
      setProblem(runProblem(cause))
    } finally {
      setPending(false)
    }
  }

  return (
    <FieldRow id="filament-sync-status" label="Sync status" help="Each item whose last write to Bambuddy failed stays listed until a later write works.">
      <div id="filament-sync-status" className="flex flex-col gap-2 text-sm">
        {data && (
          <div role="status" className="flex flex-col gap-1">
            <p>
              {data.last_pass_at ? (
                <>
                  Last pass <When at={data.last_pass_at} />: {countsLine(data.counts)}
                </>
              ) : (
                'No pass yet.'
              )}
            </p>
            {data.last_ok_at && data.last_ok_at !== data.last_pass_at && (
              <p className="text-muted-foreground">
                Last success <When at={data.last_ok_at} />.
              </p>
            )}
            {data.last_error && <p className="text-warn">The last pass failed: {syncErrorText(data.last_error)}</p>}
            {data.skipped && <p className="text-muted-foreground">{SKIPPED_TEXT[data.skipped]}</p>}
          </div>
        )}
        {data && data.errors.length > 0 && (
          <ul aria-label="Items not synced" className="flex flex-col gap-2">
            {data.errors.map((error) => {
              const href = itemHref(error)
              // The item's own name (a spool's `#N`); its id only when the item is gone.
              const name = `${KIND_LABELS[error.kind]} ${error.label ?? shortId(error.sb_id)}`
              return (
                <li key={`${error.kind}:${error.sb_id}`} className="flex flex-col gap-0.5">
                  <span className="font-medium">
                    {href ? (
                      <AppLink href={href} className={LINK}>
                        {name}
                      </AppLink>
                    ) : (
                      name
                    )}
                  </span>
                  <span className="text-warn">{syncErrorText(error.error)}</span>
                  <span className="text-muted-foreground">
                    {plural(error.attempts, 'attempt', 'attempts')}, last <When at={error.at} />
                  </span>
                </li>
              )
            })}
          </ul>
        )}
        {status.error && <p className="text-warn">The sync status could not be read. Reload the page to try again.</p>}
        <div>
          <Button type="button" size="sm" variant="outline" disabled={pending || waiting !== null} onClick={() => void syncNow()}>
            Sync now
          </Button>
        </div>
        <p role="status" aria-live="polite" className="text-muted-foreground empty:hidden">
          {note}
        </p>
        {problem && (
          <p role="alert" className="text-warn">
            {problem}
          </p>
        )}
      </div>
    </FieldRow>
  )
}

/**
 * #6625 — the nozzles a 3DFP preset fetch exports for each printer model, one checkbox
 * each. `profile_fetch_nozzles` is held in the form comma-joined and saved with "Save
 * Filament"; none ticked is refused before the save.
 */
function FetchNozzlesRow({ form }: { form: BackendSettings }) {
  const raw = form.value('profile_fetch_nozzles')
  const ticked = raw === '' ? [] : raw.split(',')
  const toggle = (nozzle: string, checked: boolean) =>
    form.setField('profile_fetch_nozzles', nozzlesValue(checked ? [...ticked, nozzle] : ticked.filter((one) => one !== nozzle)))
  const doc = docOf(form.settings, 'profile_fetch_nozzles')
  return (
    <FieldRow
      id="setting-profile_fetch_nozzles"
      {...doc}
      badge={<FieldBadge form={form} name="profile_fetch_nozzles" />}
      error={form.errors.profile_fetch_nozzles}
    >
      <fieldset id="setting-profile_fetch_nozzles" aria-label={doc.label} className="flex flex-wrap gap-x-5 gap-y-2">
        {FETCH_NOZZLES.map((nozzle) => (
          <label key={nozzle} className="flex items-center gap-2 text-sm">
            <Checkbox aria-label={`${nozzle} mm`} checked={ticked.includes(nozzle)} onCheckedChange={(checked) => toggle(nozzle, checked === true)} />
            <span className="sb-num">{nozzle} mm</span>
          </label>
        ))}
      </fieldset>
    </FieldRow>
  )
}

/**
 * #6264 — the RFID override mode (Bambuddy stops adding a spool for an RFID tag it does not
 * know). It is an action with a status, not a draft: the switch writes the mode on the server
 * at once, which answers with where Bambuddy stands.
 */
function OverrideRow() {
  const status = useAsync(() => getRfidStatus(), [])
  const [pending, setPending] = useState(false)
  const [report, setReport] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)

  async function change(call: () => Promise<RfidStatus | RfidReconcileReport>) {
    setPending(true)
    setProblem(null)
    setReport(null)
    try {
      const answer = await call()
      if ('mode' in answer) {
        status.setData({ ...answer.mode, devices: status.data?.devices })
        setReport(reportLine(answer))
      } else {
        status.setData({ ...answer, devices: status.data?.devices })
      }
    } catch (cause) {
      setProblem(cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <FieldRow
        id="setting-filament-rfid-override"
        label="RFID override mode"
        help="On, Bambuddy stops adding a spool for an RFID tag it does not know. ScadBuddy's spools take their place."
      >
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <Switch
              id="setting-filament-rfid-override"
              checked={status.data?.override_mode ?? false}
              disabled={pending || status.data === undefined}
              onCheckedChange={(checked) => void change(() => setOverrideMode(checked))}
            />
            <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void change(() => reconcileRfid())}>
              Apply now
            </Button>
          </div>
          {status.data && (
            <p role="status" className="text-sm text-muted-foreground">
              {statusLine(status.data)}
            </p>
          )}
          {status.error && <p className="text-sm text-warn">The mode could not be read. Reload the page to try again.</p>}
          {report && <p className="text-sm text-muted-foreground">{report}</p>}
          {problem && (
            <p role="alert" className="text-sm text-warn">
              {problem}
            </p>
          )}
        </div>
      </FieldRow>
    </>
  )
}

/**
 * #6262 — the configuration the Filament page leans on: how Bambuddy's spools reach
 * ScadBuddy's own, and the numbers a spool falls back on. The spools themselves, their
 * types and tags are not here; they are on the Filament page.
 */
export function Component() {
  const syncStatus = useAsync(() => getSyncStatus(), [])
  // A saved direction changes `skipped`: read the status again.
  const form = useBackendSettings(id, (s) => fieldsOf(id, s), { onSaved: () => syncStatus.refresh() })
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description={
        <>
          Spools, types and tags are in{' '}
          <AppLink href="/filament" className={LINK}>
            Filament
          </AppLink>
          .
        </>
      }
    >
      <RuntimeRows form={form} section="filament" />
      <SyncRows form={form} />
      <SyncStatusRow status={syncStatus} />
      <OverrideRow />
      <FetchNozzlesRow form={form} />
      <FieldRow
        id="filament-currency"
        label="Currency"
        help="Costs are in the shop's currency, which an order copies when it is made."
      >
        <p id="filament-currency" className="text-sm">
          <span className="sb-num font-medium">{form.settings.orders_currency}</span>
          {' · '}
          <AppLink href="/settings/printing#orders" className={LINK}>
            Change it in Orders
          </AppLink>
        </p>
      </FieldRow>
    </SavedSection>
  )
}
