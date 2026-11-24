import { ScaleIcon } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { ApiError } from '../../../api/client'
import { addSpoolEvent, listCatalog, type Spool } from '../../../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/** Whole grams with a space between thousands: `1 243`. */
const grams = (value: number) => Math.round(value).toLocaleString('en-US').replace(/,/g, ' ')

/**
 * #7273 (G5) — the spool on the scale. With its insert type's empty weight known, the person
 * types the gross weight and the field works out the filament; without it, they type the
 * filament's own weight. "Save weight" books a person's weighing of the net grams.
 */
export function WeightField({ spool }: { spool: Spool }) {
  const id = useId()
  const insertId = spool.effective?.insert_type_id ?? spool.insert_type_id
  const [empty, setEmpty] = useState<number | null | undefined>(insertId ? undefined : null)
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!insertId) return
    let live = true
    listCatalog('insert-types').then(
      (rows) => live && setEmpty(rows.find((row) => row.id === insertId)?.empty_weight_g ?? null),
      () => live && setEmpty(null),
    )
    return () => {
      live = false
    }
  }, [insertId])

  if (empty === undefined) return <p className="text-muted-foreground">Loading…</p>

  const value = text.trim() === '' ? null : Number(text)
  const net = value === null || !Number.isFinite(value) ? null : empty === null ? value : value - empty

  async function save() {
    setSaved(false)
    if (net === null) {
      setError('Type the weight first.')
      return
    }
    if (net < 0) {
      setError('The weight is less than the empty spool.')
      return
    }
    setError(null)
    setSaving(true)
    try {
      await addSpoolEvent(spool.id, { kind: 'weighed', source: 'person', grams: Math.round(net) })
      setSaved(true)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'ScadBuddy could not save the weight. Try again.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      <label htmlFor={id} className="font-medium">
        {empty === null ? 'Filament weight (g)' : 'Gross weight (g)'}
      </label>
      <div className="flex gap-2">
        <Input
          id={id}
          inputMode="decimal"
          autoComplete="off"
          value={text}
          onChange={(event) => {
            setText(event.target.value)
            setSaved(false)
            setError(null)
          }}
          className="h-11 text-base tabular-nums"
        />
        <Button type="submit" variant="outline" className="h-11" disabled={saving}>
          <ScaleIcon /> Save weight
        </Button>
      </div>
      {empty !== null && (
        <p className="tabular-nums text-muted-foreground">
          {value !== null && net !== null
            ? `${grams(value)} g − ${grams(empty)} g empty spool = ${grams(net)} g of filament`
            : `The empty spool weighs ${grams(empty)} g. Weigh the spool with its filament.`}
        </p>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      {saved && <p aria-live="polite">Saved</p>}
    </form>
  )
}
