import { useState } from 'react'
import { ApiError } from '../../../api/client'
import { getRfidStatus, reconcileRfid, setOverrideMode, type RfidReconcileReport, type RfidStatus } from '../../../api/filament'
import { AppLink } from '../../../components/AppLink'
import { ChoiceSelect } from '../../../components/settings/form'
import { useAsync } from '../../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { FieldRow } from '../controls'
import { docOf } from '../fields'
import { FieldBadge, RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings, type BackendSettings } from '../useBackendSettings'
import { fieldsOf, syncParts } from '../values'

export const id = 'filament'
export const area: AreaId = 'printing'
export const title = 'Filament'
export const order = 30

const LINK = 'underline underline-offset-2 hover:text-foreground'

const SPOOL_CHOICES = [
  { value: 'off', label: 'Off' },
  { value: 'bb_to_sb', label: 'Bambuddy → ScadBuddy' },
] as const

const TAG_CHOICES = [
  { value: 'off', label: 'Off' },
  { value: 'sb_to_bb', label: "Send ScadBuddy's bindings" },
] as const

const WEIGHT_CHOICES = [
  { value: 'off', label: 'Off' },
  { value: 'bb_to_sb', label: 'Bambuddy → ScadBuddy' },
] as const

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
 * What moves between ScadBuddy and Bambuddy: the spools and weighings it pulls and the tag
 * bindings it sends. The choices are one setting, `filament_sync`, held in the form as `spools|tags|weights`
 * and saved with "Save Filament".
 */
function SyncRows({ form }: { form: BackendSettings }) {
  const [spools, tags, weights] = syncParts(form.value('filament_sync'))
  return (
    <>
      <FieldRow
        id="setting-filament_sync"
        {...docOf(form.settings, 'filament_sync')}
        badge={<FieldBadge form={form} name="filament_sync" />}
        error={form.errors.filament_sync}
      >
        <ChoiceSelect id="setting-filament_sync" value={spools} onValueChange={(next) => form.setField('filament_sync', `${next}|${tags}|${weights}`)} choices={SPOOL_CHOICES} />
      </FieldRow>
      <FieldRow
        id="setting-filament-tags"
        label="Tags to Bambuddy"
        help="Send each tag binding to Bambuddy, so its spools carry the same tags."
      >
        <ChoiceSelect id="setting-filament-tags" value={tags} onValueChange={(next) => form.setField('filament_sync', `${spools}|${next}|${weights}`)} choices={TAG_CHOICES} />
      </FieldRow>
      <FieldRow
        id="setting-filament-weights"
        label="Weights from Bambuddy"
        help="Book Bambuddy's scale weighings and weight readings. Off, no scale weighing is booked for any spool and no weight reading for a spool ScadBuddy already tracks; only a new spool still gets its first Bambuddy weight reading."
      >
        <ChoiceSelect id="setting-filament-weights" value={weights} onValueChange={(next) => form.setField('filament_sync', `${spools}|${tags}|${next}`)} choices={WEIGHT_CHOICES} />
      </FieldRow>
    </>
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
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
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
      <OverrideRow />
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
