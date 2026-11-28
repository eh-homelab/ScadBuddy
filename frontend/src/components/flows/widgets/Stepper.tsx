import { Minus, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/**
 * #8062 — a bounded number with − and + buttons that stop at the bounds. The number stays
 * a field too, so a person can type it and the form reads it as any number input.
 */
export function Stepper({
  id,
  field,
  value,
  min,
  max,
  integer,
  common,
  onChange,
}: {
  id: string
  field: string
  value: string
  min: number
  max: number
  integer: boolean
  common: Record<string, unknown>
  onChange: (next: string) => void
}) {
  const number = value.trim() === '' ? NaN : Number(value)
  const set = (next: number) => onChange(String(Math.min(max, Math.max(min, next))))
  const less = Number.isNaN(number) ? min : number - 1
  const more = Number.isNaN(number) ? min : number + 1
  return (
    <div className="flex items-center gap-2">
      <Button type="button" variant="outline" className="size-11 shrink-0" aria-label="Less" disabled={!Number.isNaN(number) && number <= min} onClick={() => set(less)}>
        <Minus />
      </Button>
      <Input
        {...common}
        id={id}
        data-field={field}
        type="number"
        inputMode={integer ? 'numeric' : 'decimal'}
        step={integer ? 1 : 'any'}
        min={min}
        max={max}
        value={value}
        className="h-11 w-24 text-center text-base"
        onChange={(event) => onChange(event.target.value)}
      />
      <Button type="button" variant="outline" className="size-11 shrink-0" aria-label="More" disabled={!Number.isNaN(number) && number >= max} onClick={() => set(more)}>
        <Plus />
      </Button>
    </div>
  )
}
