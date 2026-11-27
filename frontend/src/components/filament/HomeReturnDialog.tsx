import { ArrowDownToLineIcon, PackageIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import { getLocation, listSpoolEvents, moveSpool, type LocationDetail, type Spool } from '../../api/filament'
import { problemCode } from '../../api/unknownTrays'
import { homeReturnTitle, spoolCode } from '../../lib/homeReturns'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { IdentifySpool } from './IdentifySpool'
import { RgbaSwatch } from './RgbaSwatch'

const grams = (value: number) => `${Number(value.toFixed(1))} g`
const reasonOf = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')

/** Where the spool came out of: the printer and the slot of its newest move to no place. */
async function cameFrom(spoolId: string): Promise<string | null> {
  const events = await listSpoolEvents(spoolId)
  const out = [...events]
    .sort((a, b) => b.at.localeCompare(a.at) || b.seq - a.seq)
    .find((event) => event.kind === 'moved' && event.data.to == null && typeof event.data.from === 'string')
  if (!out) return null
  const tray = await getLocation(out.data.from as string)
  const printer = tray.printer_id != null ? tray.path[0]?.name : null
  return printer && printer !== tray.name ? `${printer} ${tray.name}` : tray.name
}

/**
 * #7167 — "S-12 came out of <printer> <slot>. Which spool goes into <box>?" The spool's home is a
 * dry box (a dry-storage place of capacity 1). The person names the spool that goes in
 * (`IdentifySpool`): nothing is chosen for them, the unloaded spool included. The box's own spool
 * or an empty box is a plain move (`if_occupied: 'refuse'`); any other spool replaces the box's
 * spool, which is archived at 0 g with its grams booked as non-print waste (`replace`). Each move
 * names the unloaded spool in `answers`, which clears its prompt. A 409 `box_full` (another spool
 * went in meanwhile) reads the box again and shows it. The form is mounted with the dialog's
 * content, so each opening starts afresh.
 */
export function HomeReturnDialog({
  spool,
  open,
  onOpenChange,
  onDone,
}: {
  /** The unloaded spool, waiting for its home answer. */
  spool: Spool
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The move went through: `moved` is the spool now in the box, `box` its name. */
  onDone: (moved: Spool, box: string) => void
}) {
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="sm:max-w-lg">
        <HomeReturnForm spool={spool} busy={busy} setBusy={setBusy} onOpenChange={onOpenChange} onDone={onDone} />
      </DialogContent>
    </Dialog>
  )
}

function HomeReturnForm({
  spool,
  busy,
  setBusy,
  onOpenChange,
  onDone,
}: {
  spool: Spool
  busy: boolean
  setBusy: (busy: boolean) => void
  onOpenChange: (open: boolean) => void
  onDone: (moved: Spool, box: string) => void
}) {
  const home = spool.home_location_id ?? null
  const [box, setBox] = useState<LocationDetail | null>(null)
  const [from, setFrom] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [chosen, setChosen] = useState<Spool | null>(null)
  const [error, setError] = useState<string | null>(null)
  const code = spoolCode(spool)

  useEffect(() => {
    if (!home) return
    let alive = true
    getLocation(home).then(
      (place) => {
        if (alive) setBox(place)
      },
      (cause: unknown) => {
        if (alive) setLoadError(`The dry box could not be read: ${reasonOf(cause)}`)
      },
    )
    // The title still reads without it.
    cameFrom(spool.id).then(
      (where) => {
        if (alive) setFrom(where)
      },
      () => {},
    )
    return () => {
      alive = false
    }
  }, [home, spool.id])

  // A dry box holds one spool at most.
  const occupant = box?.spools[0] ?? null
  const replaces = chosen !== null && occupant !== null && occupant.id !== chosen.id
  const title = box ? `${code} came out of ${from ?? 'its tray'}. Which spool goes into ${box.name}?` : homeReturnTitle(spool)

  async function put() {
    if (!chosen || !box || busy) return
    setBusy(true)
    setError(null)
    try {
      const moved = await moveSpool(chosen.id, box.id, { ifOccupied: replaces ? 'replace' : 'refuse', answers: spool.id })
      onDone(moved, box.name)
    } catch (cause) {
      if (problemCode(cause) === 'box_full') {
        // Another spool went in meanwhile: show the box as it is now, and let the person decide again.
        try {
          const now = await getLocation(box.id)
          setBox(now)
          const held = now.spools[0]
          setError(held ? `${held.code} went into ${now.name} meanwhile. Check which spool goes in, then try again.` : reasonOf(cause))
        } catch (again) {
          setError(reasonOf(again))
        }
      } else setError(reasonOf(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle className="text-balance">{title}</DialogTitle>
        <DialogDescription>Name the spool you put in the box: scan its tag, read its label, or pick it from the list.</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-4">
        {!home && <p className="text-[13px] text-warn">This spool has no dry box to go back to.</p>}
        {loadError && (
          <p role="alert" className="text-[13px] text-warn">
            {loadError}
          </p>
        )}
        {box && (
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg bg-muted px-3 py-2 text-[13px]">
            <PackageIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
            {occupant ? (
              <>
                {occupant.colour_rgba && <RgbaSwatch rgba={occupant.colour_rgba} size="sm" />}
                <span>
                  {occupant.code} is in {box.name} now{occupant.grams_left != null ? `, with ${grams(occupant.grams_left)} left` : ''}.
                </span>
              </>
            ) : (
              <span>{box.name} is empty.</span>
            )}
          </p>
        )}
        <IdentifySpool
          onIdentified={(next) => {
            setChosen(next)
            setError(null)
          }}
        />
        {chosen && box && (
          <div aria-live="polite" className={cn('flex flex-col gap-1 border-l-[3px] py-1 pl-3 text-[13px]', replaces ? 'border-warn' : 'border-primary')}>
            <p className="flex items-center gap-2 font-medium">
              <ArrowDownToLineIcon aria-hidden="true" className="size-4" />
              {occupant?.id === chosen.id ? `${spoolCode(chosen)} stays in ${box.name}.` : `${spoolCode(chosen)} goes into ${box.name}.`}
            </p>
            {replaces && occupant && (
              <p className="text-warn">
                {occupant.code} comes out of {box.name} for good: it is archived at 0 g
                {occupant.grams_left != null && occupant.grams_left > 0 ? `, and its ${grams(occupant.grams_left)} are booked as non-print waste.` : '.'}
              </p>
            )}
          </div>
        )}
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
          Not now
        </Button>
        <Button type="button" variant={replaces ? 'destructive' : 'default'} disabled={!chosen || !box || busy} onClick={() => void put()}>
          Put in box
        </Button>
      </DialogFooter>
    </>
  )
}
