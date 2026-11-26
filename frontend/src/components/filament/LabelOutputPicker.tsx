import type { LabelOutput } from '../../api/filament'
import { Label as FieldLabel } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The dpi a PNG can have (R-7216-6, Bambuddy's set). */
const DPIS: LabelOutput['dpi'][] = [203, 300, 600]

/**
 * #7216 — the labels file's format, PDF or PNG, and a PNG's dpi. The dpi shows for PNG only.
 * A change gives the whole value, its `fields` kept.
 */
export function LabelOutputPicker({ value, onChange }: { value: LabelOutput; onChange(next: LabelOutput): void }) {
  return (
    <span className="flex items-center gap-3">
      <RadioGroup
        value={value.format}
        onValueChange={(format) => onChange({ ...value, format: format === 'png' ? 'png' : 'pdf' })}
        aria-label="Format"
        className="flex w-auto items-center gap-4"
      >
        <span className="flex items-center gap-1.5">
          <RadioGroupItem value="pdf" id="output-pdf" />
          <FieldLabel htmlFor="output-pdf" className="text-[13px]">
            PDF
          </FieldLabel>
        </span>
        <span className="flex items-center gap-1.5">
          <RadioGroupItem value="png" id="output-png" />
          <FieldLabel htmlFor="output-png" className="text-[13px]">
            PNG
          </FieldLabel>
        </span>
      </RadioGroup>
      {value.format === 'png' && (
        <Select value={String(value.dpi)} onValueChange={(dpi) => onChange({ ...value, dpi: Number(dpi) as LabelOutput['dpi'] })}>
          <SelectTrigger aria-label="Resolution" className="w-auto">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DPIS.map((dpi) => (
              <SelectItem key={dpi} value={String(dpi)}>
                {dpi} dpi
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </span>
  )
}
