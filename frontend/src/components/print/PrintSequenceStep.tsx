import type { PrintSequence } from '../../api/types'

type Props = {
  /** `null` leaves the sequence to the template's `print_settings` and the process. */
  value: PrintSequence | null
  onChange: (next: PrintSequence | null) => void
}

const DEFAULT = ''

const CHOICES: { value: PrintSequence; label: string }[] = [
  { value: 'by layer', label: 'By layer' },
  { value: 'by object', label: 'By object' },
]

/**
 * #1862 — Bambu's print sequence for this print (#907): every object a layer at a time,
 * or each object finished before the next starts. Per print, never remembered.
 */
export function PrintSequenceStep({ value, onChange }: Props) {
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Print sequence</legend>
      <div className="mt-1.5 flex flex-col gap-1">
        <label htmlFor="print-sequence-select" className="text-[12px] text-muted">
          Print sequence
        </label>
        <select
          id="print-sequence-select"
          value={value ?? DEFAULT}
          onChange={(event) =>
            onChange(event.target.value === DEFAULT ? null : (event.target.value as PrintSequence))
          }
          className="sb-field"
        >
          <option value={DEFAULT}>Default (template or process)</option>
          {CHOICES.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      </div>
      {value === 'by object' && (
        <p className="mt-1.5 text-[12px] text-muted">
          Each object is finished before the next starts, so the toolhead has to clear the
          ones already printed: the objects need room between them.
        </p>
      )}
    </fieldset>
  )
}
