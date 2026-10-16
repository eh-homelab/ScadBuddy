import { clampOption, PRINT_OPTIONS } from '../../lib/printOptions'

/**
 * Options → Quantity's own bounds, which are the API's (`PrintRunRequest.copies`): the
 * two controls are one value, so they bound it alike (#1046).
 */
const QUANTITY = PRINT_OPTIONS.find((spec) => spec.name === 'quantity')!

type Props = {
  /** `null` until the user sets it, so a remembered quantity is not overridden (#124). */
  value: number | null
  /** #145 — what an unset box queues. */
  remembered: number | null
  onChange: (next: number | null) => void
}

export function CopiesField({ value, remembered, onChange }: Props) {
  return (
    <div className="flex items-center gap-3">
      <label htmlFor="print-copies" className="text-[13px] text-ink">
        Copies
      </label>
      <input
        id="print-copies"
        type="number"
        min={QUANTITY.min}
        max={QUANTITY.max}
        step={1}
        value={value ?? ''}
        placeholder={String(remembered ?? 1)}
        // The attributes are advisory; this is what keeps 2.5 or 5000 from being queued.
        onChange={(event) =>
          onChange(event.target.value === '' ? null : clampOption(event.target.value, QUANTITY))
        }
        className="sb-field sb-num w-20 text-right"
      />
      {value === null && remembered !== null && (
        <span className="text-[12px] text-muted" data-testid="remembered-copies">
          <span className="sb-num">{remembered}</span> remembered — leave blank to use it
        </span>
      )}
    </div>
  )
}
