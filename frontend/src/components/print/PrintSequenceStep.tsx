import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StepCard } from './StepCard'
import type { PrintSequence } from '../../api/types'

type Props = {
  /** `null` leaves the sequence to the template's `print_settings` and the process. */
  value: PrintSequence | null
  onChange: (next: PrintSequence | null) => void
  /**
   * #3222 — the template's own sequence (its `print_settings`), which `null` prints with;
   * `null` when it sets none, or the file is not a template's.
   */
  inherited?: PrintSequence | null
  /**
   * #956 — how many objects the plate holds, from the check; `null` while unknown. `0` is
   * a file that lists none for the plate (#3222), as unknown as `null`.
   */
  objects?: number | null
}

/** The select's value for no override: Radix's select takes no empty value. */
const DEFAULT = 'default'

const CHOICES: { value: PrintSequence; label: string }[] = [
  { value: 'by layer', label: 'By layer' },
  { value: 'by object', label: 'By object' },
]

/**
 * #1862 — Bambu's print sequence for this print (#907): every object a layer at a time,
 * or each object finished before the next starts. Per print, never remembered.
 */
export function PrintSequenceStep({ value, onChange, inherited = null, objects = null }: Props) {
  const count = objects !== null && objects > 0 ? objects : null
  return (
    <StepCard title="Print sequence">
      <div className="grid gap-1.5">
        <Label htmlFor="print-sequence-select" className="text-xs font-normal text-muted-foreground">
          Print sequence
        </Label>
        <Select
          value={value ?? DEFAULT}
          onValueChange={(next) => onChange(next === DEFAULT ? null : (next as PrintSequence))}
        >
          <SelectTrigger id="print-sequence-select" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={DEFAULT}>Default (template or process)</SelectItem>
            {CHOICES.map((choice) => (
              <SelectItem key={choice.value} value={choice.value}>
                {choice.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {(value ?? inherited) === 'by object' && (
        <p className="text-xs text-muted-foreground">
          {count === 1
            ? 'This plate is one object, so by object prints it as by layer would.'
            : `Each object is finished before the next starts, so the toolhead has to clear the ones already printed: the objects need room between them.${
                count !== null ? ` This plate has ${count} objects.` : ''
              }`}
        </p>
      )}
    </StepCard>
  )
}
