import type { InsertWeightOffer as Offer } from '../../api/filament'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

/** Where an insert's empty weight came from (R-7280-3), in words. */
const BASIS_TEXT: Record<Offer['current_basis'], string> = {
  catalog: 'from the catalog',
  person: 'set by a person',
  placeholder: 'a placeholder',
  derived: 'from new spools',
  weighed_empty: 'weighed empty',
}

const grams = (value: number) => `${Number(value.toFixed(1))} g`

/**
 * The offer in the weigh form: the numbers it rests on, and a checkbox that saves the new
 * empty weight for the insert before the weighing is recorded (R-7280-4, R-7280-6). Over a
 * weight a person set, it says that weight is replaced (#7450-3).
 */
export function InsertWeightOffer({ offer, checked, onCheckedChange, disabled }: { offer: Offer; checked: boolean; onCheckedChange: (next: boolean) => void; disabled?: boolean }) {
  const rows: [string, string][] = [
    ['Scale reading', grams(offer.reading_g)],
    ['Label weight', grams(offer.label_weight_g)],
    ['This empty spool', grams(offer.derived_g)],
    ['Now', `${grams(offer.current_empty_weight_g)}, ${offer.placeholder ? BASIS_TEXT.placeholder : BASIS_TEXT[offer.current_basis]}`],
    ['New weight', grams(offer.new_empty_weight_g)],
    ['Readings used', String(offer.sample_count)],
  ]
  const id = `insert-weight-${offer.insert_type_id}`
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-dashed bg-muted/40 px-3 py-2.5">
      <legend className="px-1 text-[13px] font-medium">Empty spool weight from this new spool</legend>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[12px]">
        {rows.map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <Label htmlFor={id} className="flex items-start gap-2 text-[13px] font-normal">
        <Checkbox id={id} className="mt-0.5" checked={checked} disabled={disabled} onCheckedChange={(next) => onCheckedChange(next === true)} />
        <span>
          Save {grams(offer.new_empty_weight_g)} as the empty weight of {offer.insert_name}
        </span>
      </Label>
      {offer.current_basis === 'person' && !offer.placeholder && <p className="text-[12px] text-warn">This replaces the {grams(offer.current_empty_weight_g)} a person set.</p>}
      {/* Weighing an empty spool to set the weight is #7300, not built yet (#7451-1). */}
      <p className="text-[12px] text-muted-foreground">Makers overfill by about 10-30 g, so this estimate comes out slightly heavy. To correct it, edit the insert's empty weight in the catalog.</p>
      {/* #7451-3: the weighing books `reading - mean`, so the spool can end off its label weight and read `not_full` later. */}
      {offer.sample_count > 1 && <p className="text-[12px] text-muted-foreground">The new weight is a mean, so this spool's grams left can end a few grams off its label weight. It then gets no new offer.</p>}
    </fieldset>
  )
}
