import type { ReactNode } from 'react'
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
    <div
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-invalid={invalid ? true : undefined}
      aria-describedby={describedBy}
      className="flex flex-wrap gap-2"
    >
      {options.map((option) => {
        const chosen = option === value
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={chosen}
            onClick={() => onChange(chosen && clearable ? '' : option)}
            className={cn(
              'min-h-11 min-w-16 flex-1 rounded-md border px-4 text-sm font-medium break-words outline-none focus-visible:ring-3 focus-visible:ring-ring/50',
              chosen ? 'border-primary bg-primary text-primary-foreground' : 'bg-background hover:bg-muted',
              invalid && !chosen && 'border-destructive',
            )}
          >
            {option}
          </button>
        )
      })}
    </div>
  )
}
