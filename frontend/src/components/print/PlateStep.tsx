import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StepCard } from './StepCard'
type Props = {
  bedTypes: string[]
  /** Bambu Studio's label for each plate it names (#2366); others show as they are. */
  labels?: Record<string, string>
  value: string
  lastBedType: string | null
  printerName: string | null
  onChange: (next: string) => void
}

/** One object for every render, so `labels` stays stable if a memo or effect reads it (#2859). */
const NO_LABELS: Record<string, string> = {}

/**
 * Fix round 1 — `printerName` is nullable (an eligible printer is not always chosen
 * yet), and reading it straight into the sentence rendered "The 's last print used…".
 * Falls back the way `backend/scadbuddy/bambuddy/hardware.py`'s `plate_warning` does
 * (`printer_name or "printer"`), so the two stay worded the same.
 */
export function PlateStep({ bedTypes, labels = NO_LABELS, value, lastBedType, printerName, onChange }: Props) {
  return (
    <StepCard title="Plate">
      <div className="grid gap-1.5">
        <Label htmlFor="plate-select" className="text-xs font-normal text-muted-foreground">
          Plate
        </Label>
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger id="plate-select" aria-label="Plate" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {bedTypes.map((type) => (
              <SelectItem key={type} value={type}>
                {labels[type] ?? type}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {lastBedType && <p className="text-xs text-muted-foreground">Last print used: {lastBedType}</p>}
      {lastBedType && lastBedType !== value && (
        <p role="status" className="text-xs text-muted-foreground">
          The {printerName ?? 'printer'}&apos;s last print used {lastBedType}. Swap to {value}{' '}
          before this starts.
        </p>
      )}
    </StepCard>
  )
}
