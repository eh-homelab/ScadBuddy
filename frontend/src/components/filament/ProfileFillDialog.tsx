import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import { fillTypeFromProfile, type FillField, type TypeFill } from '../../api/filament'
import { FILL_LABELS, sourceText } from './profileFill'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')

/**
 * #6714 — "Fill from slicer profile" on the type page: reads the profile's values that differ
 * from the type's (`refresh`), shows each beside the type's own, and writes the rows a person
 * keeps (`apply`), with the K values of its Bambuddy spools when the type has none. Nothing is
 * written until Apply.
 */
export function ProfileFillDialog({ typeId, open, onClose, onApplied }: { typeId: string; open: boolean; onClose: () => void; onApplied: (done: TypeFill) => void }) {
  const [diff, setDiff] = useState<TypeFill | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [kept, setKept] = useState<ReadonlySet<string>>(new Set())
  const [withK, setWithK] = useState(true)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    let live = true
    setDiff(null)
    setError(null)
    fillTypeFromProfile(typeId, { refresh: true }).then(
      (found) => {
        if (!live) return
        setDiff(found)
        setKept(new Set(Object.keys(found.filled)))
        setWithK(found.k_values.length > 0)
      },
      (cause: unknown) => live && setError(message(cause)),
    )
    return () => {
      live = false
    }
  }, [open, typeId])

  const rows = diff ? (Object.entries(diff.filled) as [FillField, number | string][]) : []
  const count = rows.filter(([field]) => kept.has(field)).length + (withK && diff && diff.k_values.length > 0 ? 1 : 0)

  async function apply() {
    if (!diff) return
    setBusy(true)
    setError(null)
    try {
      const apply = Object.fromEntries(rows.filter(([field]) => kept.has(field)))
      const done = await fillTypeFromProfile(typeId, { apply, k_values: withK && diff.k_values.length > 0 })
      onApplied(done)
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  const toggle = (field: string, on: boolean) =>
    setKept((was) => {
      const next = new Set(was)
      if (on) next.add(field)
      else next.delete(field)
      return next
    })

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Fill from slicer profile</DialogTitle>
          <DialogDescription>{diff?.source ? `From ${sourceText(diff.source)}.` : 'The values in the preset this type slices with.'}</DialogDescription>
        </DialogHeader>
        {!diff && !error && <p className="text-[13px] text-muted-foreground">Reading the profile…</p>}
        {error && (
          <p role="alert" className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
            {error}
          </p>
        )}
        {diff?.reason && <p className="text-[13px]">The profile could not be read: {diff.reason}.</p>}
        {diff && rows.length > 0 && (
          <table aria-label="Changes from the profile" className="w-full text-left text-[13px]">
            <thead className="text-muted-foreground">
              <tr>
                <th scope="col" className="w-7 py-1.5">
                  <span className="sr-only">Apply</span>
                </th>
                <th scope="col" className="py-1.5 font-medium">Field</th>
                <th scope="col" className="py-1.5 text-right font-medium">Now</th>
                <th scope="col" className="py-1.5 text-right font-medium">Profile</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([field, value]) => {
                const now = diff.previous[field]
                return (
                  <tr key={field} className="border-t">
                    <td className="py-1.5">
                      <Checkbox checked={kept.has(field)} aria-label={`Apply ${FILL_LABELS[field]}`} onCheckedChange={(on) => toggle(field, on === true)} />
                    </td>
                    <th scope="row" className="py-1.5 font-normal">
                      {FILL_LABELS[field]}
                    </th>
                    <td className="py-1.5 text-right tabular-nums text-muted-foreground">{now == null ? 'Empty' : String(now)}</td>
                    <td className="py-1.5 text-right font-medium tabular-nums">{String(value)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        {diff && !diff.reason && rows.length === 0 && <p className="text-[13px] text-muted-foreground">The type already has every value the profile gives.</p>}
        {diff && Object.keys(diff.skipped).length > 0 && (
          <ul className="text-[12px] text-muted-foreground">
            {Object.entries(diff.skipped).map(([field, why]) => (
              <li key={field}>Not offered: {why}.</li>
            ))}
          </ul>
        )}
        {diff && diff.k_values.length > 0 && (
          <label className="flex items-center gap-2 text-[13px]">
            <Checkbox checked={withK} onCheckedChange={(on) => setWithK(on === true)} />
            Add {diff.k_values.length} {diff.k_values.length === 1 ? 'K value' : 'K values'} from its Bambuddy spools
          </label>
        )}
        {diff?.k_reason && <p className="text-[12px] text-muted-foreground">K values were not read: {diff.k_reason}.</p>}
        {diff?.cost_hint && <p className="text-[12px] text-muted-foreground">The profile prices it at {diff.cost_hint} per kg. The cost is never filled from it.</p>}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          {diff && (rows.length > 0 || diff.k_values.length > 0) && (
            <Button type="button" disabled={busy || count === 0} onClick={() => void apply()}>
              Apply {count} {count === 1 ? 'change' : 'changes'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
