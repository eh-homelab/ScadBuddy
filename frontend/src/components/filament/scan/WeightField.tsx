import { ScaleIcon } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { ApiError } from '../../../api/client'
import { addSpoolEvent, getSpool, insertWeightOffer, listCatalog, type Spool } from '../../../api/filament'
import { BelowLabelChoice, type Resolution } from '../BelowLabelChoice'
import { DECIMAL } from '../spoolWeight'
import { useInsertWeightCheck } from '../useInsertWeightCheck'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/** Whole grams with a space between thousands: `1 243`. */
const grams = (value: number) => Math.round(value).toLocaleString('en-US').replace(/,/g, ' ')

/**
 * #7273 (G5) — the spool on the scale. With its insert type's empty weight known, the person
 * types the gross weight and the field works out the filament; without it, they type the
 * filament's own weight. "Save weight" books a person's weighing of the net grams.
 */
export function WeightField({ spool, onSaved }: { spool: Spool; onSaved?: (spool: Spool) => void }) {
  const id = useId()
  const insertId = spool.effective?.insert_type_id ?? spool.insert_type_id
  const [empty, setEmpty] = useState<number | null | undefined>(insertId ? undefined : null)
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)
  // #7683: a new spool read under its label weight: the person says what is up first.
  const [choice, setChoice] = useState<{ reading: number; resolution: Resolution } | null>(null)

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

  const typed = text.trim()
  const value = DECIMAL.test(typed) ? Number(typed) : null
  const { check, pending } = useInsertWeightCheck(empty != null ? spool.id : undefined, value !== null && value > 0 ? value : null)
  const belowLabel = check?.reason === 'below_label' ? check : null
  const resolution: Resolution | null = belowLabel ? (choice?.reading === value ? choice.resolution : (belowLabel.default ?? 'tared')) : null
  const chosen = belowLabel?.resolutions.find((one) => one.kind === resolution) ?? null
  const net = chosen ? chosen.filament_g : value === null ? null : empty === null || empty === undefined ? value : value - empty

  async function save() {
    setSaved(false)
    if (typed === '') {
      setError('Type the weight first.')
      return
    }
    if (value === null) {
      setError('Enter the weight in grams, such as 1243.')
      return
    }
    setError(null)
    setSaving(true)
    try {
      // A save sent before the hook's check answered reads the check itself (#7683). When
      // that check asks what is up and the person has not seen the question yet, nothing
      // is booked: the hook's answer renders the choice, and the next Save takes it.
      const below = pending && empty !== null ? await insertWeightOffer(spool.id, value).then((one) => (one.reason === 'below_label' ? one : null), () => null) : belowLabel
      if (pending && below && choice?.reading !== value) return
      const picked: Resolution | null = below ? (choice?.reading === value ? choice.resolution : (below.default ?? 'tared')) : null
      const filament = below ? (below.resolutions.find((one) => one.kind === picked)?.filament_g ?? value) : empty == null ? value : value - empty
      if (filament < 0) {
        setError('The weight is less than the empty spool.')
        return
      }
      // #7682: with an empty weight the reading goes alone and the server takes the insert off
      // it, so the event keeps its breakdown and a decimal reading stays the insert's.
      if (below && picked) await addSpoolEvent(spool.id, { kind: 'weighed', source: 'person', reading_g: value, resolution: picked })
      else await addSpoolEvent(spool.id, { kind: 'weighed', source: 'person', ...(empty != null ? { reading_g: value } : { grams: Math.round(filament) }) })
      setSaved(true)
      setChoice(null)
      // The card shows the grams left: the spool read again carries the new ones.
      getSpool(spool.id).then(
        (fresh) => onSaved?.(fresh),
        () => {},
      )
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'ScadBuddy could not save the weight. Try again.')
    } finally {
      setSaving(false)
    }
  }

  if (empty === undefined) return <p className="text-muted-foreground">Loading…</p>

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
      {belowLabel && resolution && <BelowLabelChoice check={belowLabel} value={resolution} disabled={saving} onChange={(next) => value !== null && setChoice({ reading: value, resolution: next })} />}
      {empty !== null && !belowLabel && (
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
      <p role="status" aria-label="Weight saved">
        {saved ? 'Saved' : ''}
      </p>
    </form>
  )
}
