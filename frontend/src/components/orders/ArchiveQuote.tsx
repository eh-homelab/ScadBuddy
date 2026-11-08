import { useState } from 'react'
import { api } from '../../api/client'
import type { OrderItem } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { filamentLabel } from './FilamentsEditor'
import { refusalText } from './refusal'

/**
 * "Quote from the print" (#4777, order operations spec 2026-11-07 §3.2): an `archive`
 * quote of an item taken from a past print, priced from what that print used (every plate
 * archive of its run, the slicer's grams and the actual time) at the price book in force,
 * with no re-slice. Each colour slot takes a price-book filament, as a slice quote's do; an
 * item with no slots yet takes one filament for every slot the print used.
 */
export function ArchiveQuote({
  orderId,
  itemId,
  colours,
  onQuoted,
  onCancel,
}: {
  orderId: string
  itemId: string
  colours: OrderItem['colours']
  onQuoted: () => void
  onCancel: () => void
}) {
  const book = useAsync(() => api.priceBook(), [])
  const filaments = book.data?.filaments ?? []
  const [value, setValue] = useState<Record<string, string>>(() =>
    Object.fromEntries(colours.flatMap((c) => (c.filament_id ? [[String(c.slot), c.filament_id]] : []))),
  )
  const [every, setEvery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ready = colours.length > 0 ? colours.every((c) => value[String(c.slot)]) : every !== ''

  async function quote() {
    // An item with no slots yet names one filament, which the backend gives every slot
    // the print used (`ArchiveQuoteRequest.filament_id`).
    const body = colours.length > 0 ? { filaments: value } : { filament_id: every }
    setBusy(true)
    setError(null)
    try {
      await api.quoteItemFromArchive(orderId, itemId, body)
      onQuoted()
    } catch (err) {
      setError(refusalText(err))
      setBusy(false)
    }
  }

  const picker = (id: string, label: string, current: string, onChange: (next: string) => void) => (
    <Select value={current} onValueChange={onChange}>
      <SelectTrigger id={id} className="w-64" aria-label={label}>
        <SelectValue placeholder="Choose a filament" />
      </SelectTrigger>
      <SelectContent>
        {filaments.map((filament) => (
          <SelectItem key={filament.id} value={filament.id}>
            {filamentLabel(filament)} · {filament.cost_per_kg}/kg
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )

  return (
    <fieldset className="flex flex-col gap-2 rounded-md border p-2" aria-label="Quote from the print">
      <legend className="px-1 text-sm font-medium">Quote from the print</legend>
      {book.data && filaments.length === 0 && <p className="text-xs text-warn">The price book has no filaments yet: add them in Settings, Orders.</p>}
      {colours.length > 0 ? (
        colours.map((colour) => {
          const id = `archive-filament-${itemId}-${colour.slot}`
          return (
            <div key={colour.slot} className="flex flex-wrap items-center gap-2">
              <Label htmlFor={id} className="w-20 shrink-0">
                <span aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: colour.colour }} />
                Slot {colour.slot + 1}
              </Label>
              {picker(id, `Filament for slot ${colour.slot + 1}`, value[String(colour.slot)] ?? '', (next) => setValue({ ...value, [String(colour.slot)]: next }))}
            </div>
          )
        })
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Label htmlFor={`archive-filament-${itemId}`}>Every slot the print used</Label>
          {picker(`archive-filament-${itemId}`, 'Filament for every slot', every, setEvery)}
        </div>
      )}
      <div className="flex gap-2">
        <Button type="button" size="sm" disabled={busy || !ready} onClick={() => void quote()}>
          Quote
        </Button>
        <Button type="button" size="sm" variant="outline" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </fieldset>
  )
}
