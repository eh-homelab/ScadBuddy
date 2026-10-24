import { useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { SpoolOption, TraySpoolLink, UnknownTray } from '../../api/types'
import { bambuddyBase, useLoadBambuddyLinks } from '../../lib/bambuddyLinks'
import { openExternal } from '../../lib/embed'
import { providerLabel } from '../../lib/filaments'
import { howFull, linkKey, linkOf } from '../../lib/trays'
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
function providersOf(spool: SpoolOption, labels?: Readonly<Record<string, string>>): string {
  const names = Object.keys(spool.records ?? {}).map((provider) => providerLabel(provider, labels))
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
  /**
   * The spool is recorded: re-read the dialog's choices. A native link says what it
   * claimed, so the dialog can stop offering it to another tray until that read lands.
   */
  onAssigned: (link?: TraySpoolLink) => void
  onDecline: () => void
  /** The providers' own names (`FilamentOptions.provider_labels`, #4516). */
  providerLabels?: Readonly<Record<string, string>>
}

/**
 * #2164 — a filled tray Bambuddy has no spool for (untagged filament): which spool is
 * it? "Yes" records it in Bambuddy (`POST /inventory/assignments`, the Manage Inventory
 * scope); "No / not sure" records nothing, offers the tray itself as a choice and is not
 * asked again until the tray changes.
 */
export function TrayQuestion({ tray, spools, onAssigned, onDecline, providerLabels, ...target }: Props) {
  const native = target.native
  useLoadBambuddyLinks()
  const candidates = (tray.candidates ?? [])
    .map((id) => spools.find((spool) => spool.spool_id === id))
    .filter((spool): spool is SpoolOption => spool !== undefined && (!native || linkOf(spool) !== null))
  /**
   * What a candidate is picked by: natively its link's identity (`linkKey`), since a
   * shelf row's `spool_id` is positional and a re-read may move the record to another;
   * on the Bambuddy path the spool's own id.
   */
  // Every candidate has a link natively (filtered above); the index only keeps keys apart.
  const keyOf = (spool: SpoolOption) =>
    (native ? linkKey(linkOf(spool)) : String(spool.spool_id)) ?? `unlinked:${candidates.indexOf(spool)}`
  const [picked, setPicked] = useState<string | null>(candidates[0] ? keyOf(candidates[0]) : null)
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
      const spool = candidates.find((entry) => keyOf(entry) === picked)
      if (target.native) {
        const link = spool ? linkOf(spool) : null
        if (!link) {
          // Nothing to name: never report a link that was not made.
          setError('ScadBuddy has no record of that spool to link.')
          return
        }
        await api.linkTraySpool(target.native.printer, tray.ams_id, tray.tray_id, link)
        onAssigned(link)
      } else {
        if (!spool) return
        await api.assignTraySpool(target.printerId, tray.ams_id, tray.tray_id, spool.spool_id)
        onAssigned()
      }
    } catch (cause) {
      const fallback = native ? 'ScadBuddy could not link it.' : 'Bambuddy could not record it.'
      setError(cause instanceof ApiError ? cause.detail : fallback)
    } finally {
      setSaving(false)
    }
  }

  const spoolRow = (spool: (typeof candidates)[number]) => {
    const providers = native ? providersOf(spool, providerLabels) : ''
    return (
    <>
      <Swatch colour={spool.colour} size="sm" />
      <span>{candidateName(spool)}</span>
      {howFull(spool) && <span className="text-muted-foreground">{howFull(spool)}</span>}
      {spool.storage_location && <span className="text-muted-foreground">· kept in {spool.storage_location}</span>}
      {native ? (
        providers && <span className="text-muted-foreground">· {providers}</span>
      ) : (
        <span className="text-muted-foreground">· spool #{spool.spool_id}</span>
      )}
    </>
    )
  }

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
              value={picked ?? ''}
              onValueChange={setPicked}
              className="gap-1.5"
            >
              {candidates.map((spool) => (
                <Label key={keyOf(spool)} className="cursor-pointer flex-wrap gap-x-2 gap-y-0.5 text-xs font-normal">
                  <RadioGroupItem value={keyOf(spool)} />
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
