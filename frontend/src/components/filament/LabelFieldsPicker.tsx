import { Checkbox } from '@/components/ui/checkbox'
import { Label as FieldLabel } from '@/components/ui/label'
import { ALL_FIELDS, type LabelField } from './labelSheet'

/** What each field is called on the page. */
const NAMES: Record<LabelField, string> = {
  number: 'Number',
  material: 'Material',
  colour: 'Colour',
  hex: 'Colour code',
  brand: 'Brand',
  location: 'Location',
  temps: 'Temperatures',
  weight: 'Weight',
  note: 'Note',
  added: 'Date added',
  qr: 'QR code',
}

/**
 * #7216 — the fields a spool label prints (R-7216-9): a checkbox for each, in the order the lines
 * print. A change gives the fields in `ALL_FIELDS` order. A label prints at least one field, so the
 * last box that is checked is disabled.
 */
export function LabelFieldsPicker({ value, onChange }: { value: readonly LabelField[]; onChange(next: LabelField[]): void }) {
  const toggle = (field: LabelField, on: boolean) => onChange(ALL_FIELDS.filter((one) => (one === field ? on : value.includes(one))))
  return (
    <fieldset className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-border px-3 py-2">
      <legend className="px-1 text-[13px] font-medium">Spool label prints</legend>
      {ALL_FIELDS.map((field) => {
        const checked = value.includes(field)
        const id = `label-field-${field}`
        return (
          <span key={field} className="flex items-center gap-1.5">
            <Checkbox id={id} checked={checked} disabled={checked && value.length === 1} onCheckedChange={(on) => toggle(field, on === true)} />
            <FieldLabel htmlFor={id} className="text-[13px] font-normal">
              {NAMES[field]}
            </FieldLabel>
          </span>
        )
      })}
    </fieldset>
  )
}
