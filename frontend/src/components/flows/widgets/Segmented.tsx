import type { ReactNode } from 'react'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { cn } from '@/lib/utils'

/**
 * #8062 — a few options as one row of buttons, one chosen. Wide buttons: the station page
 * reuses it on a shop-floor screen. With `clearable`, pressing the chosen one clears it.
 */
export function Segmented({
  labelledBy,
  options,
  value,
  clearable,
  invalid,
  describedBy,
  onChange,
}: {
  labelledBy: string
  options: readonly string[]
  value: string
  clearable?: boolean
  invalid?: boolean
  describedBy?: string
  onChange: (next: string) => void
}): ReactNode {
  return (
    <RadioGroup
      value={value}
      onValueChange={onChange}
      aria-labelledby={labelledBy}
      aria-invalid={invalid ? true : undefined}
      aria-describedby={describedBy}
      className="flex w-full flex-wrap gap-2"
    >
      {options.map((option) => {
        const chosen = option === value
        return (
          <label
            key={option}
            className={cn(
              'flex min-h-11 min-w-16 flex-1 cursor-pointer items-center justify-center rounded-md border px-4 text-sm font-medium break-words has-focus-visible:ring-3 has-focus-visible:ring-ring/50',
              chosen ? 'border-primary bg-primary text-primary-foreground' : 'bg-background hover:bg-muted',
              invalid && !chosen && 'border-destructive',
            )}
          >
            <RadioGroupItem
              value={option}
              aria-label={option}
              className="absolute size-px opacity-0 after:hidden"
              onClick={() => {
                if (chosen && clearable) onChange('')
              }}
            />
            {option}
          </label>
        )
      })}
    </RadioGroup>
  )
}
