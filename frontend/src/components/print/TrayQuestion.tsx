import { useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { SpoolOption, UnknownTray } from '../../api/types'
import { bambuddyBase, useLoadBambuddyLinks } from '../../lib/bambuddyLinks'
import { openExternal } from '../../lib/embed'
import { howFull } from '../../lib/trays'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'
import { Swatch } from './Swatch'

function candidateName(spool: SpoolOption): string {
  return [spool.brand, spool.color_name].filter(Boolean).join(' ') || `${spool.material} spool`
}

type Props = {
  printerId: number
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
export function TrayQuestion({ printerId, tray, spools, onAssigned, onDecline }: Props) {
  useLoadBambuddyLinks()
  const candidates = (tray.candidates ?? [])
    .map((id) => spools.find((spool) => spool.spool_id === id))
    .filter((spool): spool is SpoolOption => spool !== undefined)
  const [picked, setPicked] = useState<number | null>(candidates[0]?.spool_id ?? null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const what = [tray.colour_word, tray.material].filter(Boolean).join(' ')
  const one = candidates.length === 1 ? candidates[0] : undefined
  const base = bambuddyBase()

  async function confirm() {
    if (picked === null) return
    setSaving(true)
    setError(null)
    try {
      await api.assignTraySpool(printerId, tray.ams_id, tray.tray_id, picked)
      onAssigned()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Bambuddy could not record it.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section
      className="rounded-[6px] border border-line bg-surface-2 px-3 py-2.5"
      data-testid={`tray-question-${tray.ams_id}-${tray.tray_id}`}
      aria-label={`Which spool is in ${tray.label}?`}
    >
      <p className="text-[13px] text-ink">
        <span className="mr-1.5 inline-flex align-[-3px]">
          <Swatch colour={tray.colour} size="sm" />
        </span>
        {tray.label} has {what}, but Bambuddy doesn&apos;t know which spool it is.
      </p>
      {candidates.length > 0 && (
        <>
          <p className="mt-1.5 text-[13px] text-ink">
            {one
              ? `Is the ${candidateName(one)} spool${howFull(one) ? ` (${howFull(one)})` : ''} in ${tray.label}?`
              : `Which spool is in ${tray.label}?`}
          </p>
          <ul className="mt-1.5 space-y-1">
            {candidates.map((spool) => (
              <li key={spool.spool_id}>
                <label className="flex cursor-pointer flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-ink">
                  {!one && (
                    <input
                      type="radio"
                      name={`tray-${tray.ams_id}-${tray.tray_id}`}
                      checked={picked === spool.spool_id}
                      onChange={() => setPicked(spool.spool_id)}
                      className="accent-[var(--sb-accent)]"
                    />
                  )}
                  <Swatch colour={spool.colour} size="sm" />
                  <span>{candidateName(spool)}</span>
                  {howFull(spool) && <span className="text-muted">{howFull(spool)}</span>}
                  {spool.storage_location && <span className="text-muted">· kept in {spool.storage_location}</span>}
                  <span className="text-faint">· spool #{spool.spool_id}</span>
                </label>
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {candidates.length > 0 && (
          <Button size="sm" variant="primary" disabled={saving || picked === null} onClick={() => void confirm()}>
            {saving && <Spinner />}
            {one ? 'Yes, that one' : 'Yes, this one'}
          </Button>
        )}
        <Button size="sm" disabled={saving} onClick={onDecline} data-testid="tray-decline">
          {candidates.length > 0 ? 'No / not sure' : 'Print from the tray as it is'}
        </Button>
        {base && (
          <button
            type="button"
            onClick={() => openExternal(`${base}/inventory`)}
            className="text-[12px] text-muted underline decoration-dotted underline-offset-2 hover:text-ink"
          >
            Add it in Bambuddy&apos;s inventory
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-1.5 text-[12px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
