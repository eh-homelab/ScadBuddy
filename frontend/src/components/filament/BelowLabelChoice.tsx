import type { InsertWeightCheck } from '../../api/filament'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'

/** How a new spool's reading under its label weight is read (#7683, spec §5.3). */
export type Resolution = NonNullable<InsertWeightCheck['default']>

const grams = (value: number) => `${Number(value.toFixed(1))} g`

function text(one: InsertWeightCheck['resolutions'][number], shortfall: number): string {
  switch (one.kind) {
    case 'tared':
      return `The scale was tared on the empty spool: ${grams(one.filament_g)} of filament`
    case 'short':
      return `The spool is short: ${grams(one.filament_g)} of filament, ${grams(shortfall)} under its label`
    case 'label_wrong':
      return `The label weight is wrong: ${grams(one.filament_g)} of filament, and the label becomes ${grams(one.new_label_weight_g ?? one.filament_g)}`
  }
}

/**
 * The choice in the weigh form and on the phone when a new spool reads under its label
 * weight: the shortfall and one radio per reading the check offers, with its numbers.
 */
export function BelowLabelChoice({ check, value, onChange, disabled }: { check: InsertWeightCheck; value: Resolution; onChange: (next: Resolution) => void; disabled?: boolean }) {
  const shortfall = check.shortfall_g ?? 0
  const legend = `The reading is ${grams(shortfall)} under the label weight. What is up?`
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-dashed bg-muted/40 px-3 py-2.5">
      <legend className="px-1 text-[13px] font-medium">{legend}</legend>
      <RadioGroup aria-label={legend} value={value} disabled={disabled} onValueChange={(next) => onChange(next as Resolution)} className="gap-1.5">
        {check.resolutions.map((one) => {
          const id = `below-label-${one.kind}`
          return (
            <label key={one.kind} htmlFor={id} className="flex cursor-pointer items-start gap-2 text-[13px]">
              <RadioGroupItem id={id} value={one.kind} className="mt-0.5" />
              <span>{text(one, shortfall)}</span>
            </label>
          )
        })}
      </RadioGroup>
    </fieldset>
  )
}
