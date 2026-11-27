import { useContext, useId, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import { dismissUnknownTray, problemCode, resolveUnknownTray, routeMissing, type UnknownSpoolCandidate, type UnknownSpoolTray, type UnknownTrayResolved } from '../../api/unknownTrays'
import { filamentLinks } from '../../lib/filamentLinks'
import { NeedsYouContext } from '../../lib/needsYou'
import { codeFromScan, prefillNetWeight, unknownSpoolTitle } from '../../lib/unknownSpools'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { RgbaSwatch } from './RgbaSwatch'
import { UnknownSpoolAreaOff } from './UnknownSpoolAreaOff'

const DECIMAL = /^\d+(\.\d+)?$/

function failureText(cause: unknown, code: string): string {
  const kind = problemCode(cause)
  if (kind === 'tray_changed') return 'This tray changed since the dialog opened. Close it and open it again.'
  if (kind === 'code_unknown') return `No spool has the code ${code}.`
  if (cause instanceof ApiError) return cause.detail
  return 'The server could not be reached. Try again.'
}

const gramsText = (grams: number | null) => (grams === null ? 'grams not known' : `${Number(grams.toFixed(1))} g left`)

/**
 * #7143 — tells ScadBuddy which spool is in a tray it could not match. The person types or scans
 * the spool's code, or picks one of the ranked candidates, and gives the net weight of the
 * filament. The tray's `seen_at` goes back exactly as it was listed, so the server can tell that
 * the tray changed meanwhile. The form is mounted with the dialog's content: each opening starts afresh.
 */
export function UnknownSpoolDialog({
  tray,
  gone = false,
  open,
  onOpenChange,
  onResolved,
}: {
  /** The tray as it was listed when the person opened the dialog; the dialog keeps that copy (see the form). */
  tray: UnknownSpoolTray
  /** The list no longer has the tray: another client linked it, or the slot was emptied. */
  gone?: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
  onResolved: (resolved: UnknownTrayResolved) => void
}) {
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="sm:max-w-lg">
        <UnknownSpoolForm tray={tray} gone={gone} busy={busy} setBusy={setBusy} onOpenChange={onOpenChange} onResolved={onResolved} />
      </DialogContent>
    </Dialog>
  )
}

function UnknownSpoolForm({
  tray: listed,
  gone,
  busy,
  setBusy,
  onOpenChange,
  onResolved,
}: {
  tray: UnknownSpoolTray
  gone: boolean
  busy: boolean
  setBusy: (busy: boolean) => void
  onOpenChange: (open: boolean) => void
  onResolved: (resolved: UnknownTrayResolved) => void
}) {
  const ctx = useContext(NeedsYouContext)
  // The tray as it was when the dialog opened. A later poll changes `listed`, but the person
  // decides on what they saw: the `seen_at` sent is this copy's, so the server's check still
  // guards the tray they saw. After a `tray_changed` they close the dialog and open it again.
  const [tray] = useState(listed)
  const [code, setCode] = useState('')
  const [picked, setPicked] = useState<string | null>(null)
  const [weight, setWeight] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [off, setOff] = useState(false)
  const ids = useId()

  const typed = code.trim()
  const trimmedWeight = weight.trim()
  const weightOk = DECIMAL.test(trimmedWeight) && Number(trimmedWeight) > 0
  const hasTarget = typed !== '' || picked !== null
  const ready = hasTarget && weightOk && !busy && !gone

  function changeCode(text: string) {
    setCode(codeFromScan(text) ?? text)
    setPicked(null)
    setError(null)
  }

  function pick(candidate: UnknownSpoolCandidate) {
    setPicked(candidate.spool_id)
    setCode('')
    setError(null)
    if (weight === '') {
      const net = prefillNetWeight(tray.remain_pct, candidate.label_weight_g)
      if (net !== null) setWeight(String(net))
    }
  }

  async function link() {
    if (!ready) return
    setBusy(true)
    setError(null)
    setOff(false)
    // `seen_at` is the string the list gave, never a `Date` (a round trip loses the microseconds).
    const common = { net_weight_g: Number(trimmedWeight), seen_at: tray.seen_at }
    try {
      const resolved = await resolveUnknownTray(tray.id, typed !== '' ? { code: typed, ...common } : { spool_id: picked!, ...common })
      onResolved(resolved)
    } catch (cause) {
      setOff(routeMissing(cause))
      setError(routeMissing(cause) ? null : failureText(cause, typed))
      if (problemCode(cause) === 'tray_changed') ctx?.refresh()
    } finally {
      setBusy(false)
    }
  }

  async function dismiss() {
    setBusy(true)
    setError(null)
    try {
      await dismissUnknownTray(tray.id)
    } catch (cause) {
      // A tray that is gone already is what Dismiss wanted.
      if (problemCode(cause) !== 'tray_gone') {
        setError(failureText(cause, ''))
        setBusy(false)
        return
      }
    }
    setBusy(false)
    ctx?.refresh()
    onOpenChange(false)
  }

  return (
    <form
      className="contents"
      onSubmit={(event) => {
        event.preventDefault()
        void link()
      }}
    >
      <DialogHeader>
        <DialogTitle>{unknownSpoolTitle(tray)}</DialogTitle>
        <DialogDescription className="flex flex-wrap items-center gap-x-2">
          <span>The printer reports</span>
          {tray.colour_rgba && <RgbaSwatch rgba={tray.colour_rgba} size="sm" />}
          <span className="font-medium text-foreground">{tray.material ?? 'an unknown material'}</span>
          <span>and no spool is linked to it.</span>
        </DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-code`}>Spool code</Label>
          <Input
            id={`${ids}-code`}
            value={code}
            placeholder="S-142, or scan the label"
            autoComplete="off"
            onChange={(event) => changeCode(event.target.value)}
            onPaste={(event) => {
              const scanned = codeFromScan(event.clipboardData.getData('text'))
              if (scanned === null) return
              event.preventDefault()
              changeCode(scanned)
            }}
            className="max-w-56"
          />
        </div>
        {tray.candidates.length > 0 && (
          <RadioGroup
            aria-label="Spools that may be in the tray"
            value={picked ?? ''}
            onValueChange={(next) => {
              const candidate = tray.candidates.find((one) => one.spool_id === next)
              if (candidate) pick(candidate)
            }}
            className="max-h-56 gap-1 overflow-y-auto"
          >
            {tray.candidates.map((one) => (
              <div key={one.spool_id} className="flex items-center gap-2 rounded-md px-1 py-1 has-[[data-state=checked]]:bg-muted">
                <RadioGroupItem value={one.spool_id} id={`${ids}-${one.spool_id}`} />
                <Label htmlFor={`${ids}-${one.spool_id}`} className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0 font-normal">
                  {one.rgba && <RgbaSwatch rgba={one.rgba} size="sm" gramsLeft={one.grams_left} labelWeight={one.label_weight_g} />}
                  <span className="font-medium">{one.code}</span>{' '}
                  {one.material && <span>{one.material}</span>}{' '}
                  <span className="text-muted-foreground">{gramsText(one.grams_left)},</span>{' '}
                  <span className="text-muted-foreground">{one.place_name ?? 'No place yet'}</span>
                </Label>
              </div>
            ))}
          </RadioGroup>
        )}
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-weight`}>Net weight (g)</Label>
          <Input
            id={`${ids}-weight`}
            inputMode="decimal"
            value={weight}
            onChange={(event) => setWeight(event.target.value)}
            aria-invalid={trimmedWeight === '' || weightOk ? undefined : true}
            className="max-w-40"
          />
          <p className="text-[13px] text-muted-foreground">The filament left on the spool, without the spool itself.</p>
        </div>
        <p className="text-[13px] text-muted-foreground">
          Not in the list?{' '}
          <Link to={filamentLinks.invoices} className="underline underline-offset-2">
            Add from an invoice
          </Link>
        </p>
        {gone && (
          <p role="status" className="text-[13px] text-warn">
            This tray is not waiting any more. Someone linked a spool to it, or the slot was emptied. Close this dialog.
          </p>
        )}
        {off && (
          <p className="text-[13px]">
            <UnknownSpoolAreaOff placeId={tray.place_id} role="alert" />
          </p>
        )}
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
      </div>
      <DialogFooter className="sm:justify-between">
        <div className="flex flex-col gap-1 sm:max-w-56">
          <Button type="button" variant="ghost" size="sm" className="self-start" disabled={busy || gone} onClick={() => void dismiss()}>
            Dismiss
          </Button>
          <p className="text-xs text-muted-foreground">Only for a slot that is no longer there; if a spool is still loaded, it comes back on the next report.</p>
        </div>
        <div className="flex gap-2">
          <Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
            {gone ? 'Close dialog' : 'Cancel'}
          </Button>
          <Button type="submit" disabled={!ready}>
            Link spool
          </Button>
        </div>
      </DialogFooter>
    </form>
  )
}
