import { useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { SpoolOption, UnknownTray } from '../../api/types'
import { bambuddyBase, useLoadBambuddyLinks } from '../../lib/bambuddyLinks'
import { openExternal } from '../../lib/embed'
import { providerLabel } from '../../lib/filaments'
import { howFull, linkOf } from '../../lib/trays'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Spinner } from '../ui/spinner'
import { Swatch } from './Swatch'

function candidateName(spool: SpoolOption): string {
  return [spool.brand, spool.color_name].filter(Boolean).join(' ') || `${spool.material} spool`
}

/** "Bambuddy's record", or "ScadBuddy's and Bambuddy's records", for the candidate's line. */
function providersOf(spool: SpoolOption): string {
  const names = Object.keys(spool.records ?? {}).map(providerLabel)
  if (names.length === 0) return ''
  return `${names.join(' and ')}${names.length === 1 ? "'s record" : "'s records"}`
}

/**
 * The printer the answer is recorded for: a Bambuddy printer, whose assignment it writes,
 * or (#3818, plan 2026-10-18 D1) a native run's registry printer, which has no Bambuddy
 * id. There its candidates are the spool providers' records no tray holds, and "yes"
 * links one through ScadBuddy's own `PUT /printers/{printer}/trays/{ams}/{tray}/spool`,
 * never Bambuddy's assignment.
 */
type Target = { printerId: number; native?: undefined } | { native: { printer: string }; printerId?: undefined }

type Props = Target & {
  tray: UnknownTray
  spools: readonly SpoolOption[]
  /** Bambuddy now knows the spool: re-read the dialog's choices. */
  onAssigned: () => void
  onDecline: () => void
}

/**
 * #2164 — a filled tray Bambuddy has no spool for (untagged filament): which spool is
 * it? "Yes" records it in Bambuddy (`POST /inventory/assignments`, the Manage Inventory
 * scope); "No / not sure" records nothing, offers the tray itself as a choice and is not
 * asked again until the tray changes.
 */
export function TrayQuestion({ tray, spools, onAssigned, onDecline, ...target }: Props) {
  const native = target.native
  useLoadBambuddyLinks()
  const candidates = (tray.candidates ?? [])
    .map((id) => spools.find((spool) => spool.spool_id === id))
    .filter((spool): spool is SpoolOption => spool !== undefined && (!native || linkOf(spool) !== null))
  const [picked, setPicked] = useState<number | null>(candidates[0]?.spool_id ?? null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const what = [tray.colour_word, tray.material].filter(Boolean).join(' ')
  const one = candidates.length === 1 ? candidates[0] : undefined
  const base = native ? null : bambuddyBase()

  async function confirm() {
    if (picked === null) return
    setSaving(true)
    setError(null)
    try {
      if (target.native) {
        const spool = candidates.find((entry) => entry.spool_id === picked)
        const link = spool ? linkOf(spool) : null
        if (link) await api.linkTraySpool(target.native.printer, tray.ams_id, tray.tray_id, link)
      } else {
        await api.assignTraySpool(target.printerId, tray.ams_id, tray.tray_id, picked)
      }
      onAssigned()
    } catch (cause) {
      const fallback = native ? 'ScadBuddy could not link it.' : 'Bambuddy could not record it.'
      setError(cause instanceof ApiError ? cause.detail : fallback)
    } finally {
      setSaving(false)
    }
  }

  const spoolRow = (spool: (typeof candidates)[number]) => (
    <>
      <Swatch colour={spool.colour} size="sm" />
      <span>{candidateName(spool)}</span>
      {howFull(spool) && <span className="text-muted-foreground">{howFull(spool)}</span>}
      {spool.storage_location && <span className="text-muted-foreground">· kept in {spool.storage_location}</span>}
      {native ? (
        providersOf(spool) && <span className="text-muted-foreground">· {providersOf(spool)}</span>
      ) : (
        <span className="text-muted-foreground">· spool #{spool.spool_id}</span>
      )}
    </>
  )

  return (
    <Card
      size="sm"
      role="region"
      className="gap-2 px-3"
      data-testid={`tray-question-${tray.ams_id}-${tray.tray_id}`}
      aria-label={`Which spool is in ${tray.label}?`}
    >
      <p className="text-sm">
        <span className="mr-1.5 inline-flex align-[-3px]">
          <Swatch colour={tray.colour} size="sm" />
        </span>
        {native
          ? `${tray.label} has ${what}, but no spool record is linked to it.`
          : <>{tray.label} has {what}, but Bambuddy doesn&apos;t know which spool it is.</>}
      </p>
      {candidates.length > 0 && (
        <>
          <p className="text-sm">
            {one
              ? `Is the ${candidateName(one)} spool${howFull(one) ? ` (${howFull(one)})` : ''} in ${tray.label}?`
              : `Which spool is in ${tray.label}?`}
          </p>
          {one ? (
            <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">{spoolRow(one)}</p>
          ) : (
            <RadioGroup
              aria-label={`Spool in ${tray.label}`}
              value={picked === null ? '' : String(picked)}
              onValueChange={(next) => setPicked(Number(next))}
              className="gap-1.5"
            >
              {candidates.map((spool) => (
                <Label key={spool.spool_id} className="cursor-pointer flex-wrap gap-x-2 gap-y-0.5 text-xs font-normal">
                  <RadioGroupItem value={String(spool.spool_id)} />
                  {spoolRow(spool)}
                </Label>
              ))}
            </RadioGroup>
          )}
        </>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {candidates.length > 0 && (
          <Button type="button" size="sm" disabled={saving || picked === null} onClick={() => void confirm()}>
            {saving && <Spinner />}
            {one ? 'Yes, that one' : 'Yes, this one'}
          </Button>
        )}
        <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onDecline} data-testid="tray-decline">
          {candidates.length > 0 ? 'No / not sure' : 'Print from the tray as it is'}
        </Button>
        {base && (
          <Button type="button" size="sm" variant="link" onClick={() => openExternal(`${base}/inventory`)}>
            Add it in Bambuddy&apos;s inventory
          </Button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </Card>
  )
}
