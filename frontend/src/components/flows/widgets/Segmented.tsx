import { useRef, type KeyboardEvent, type ReactNode } from 'react'
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
  const group = useRef<HTMLDivElement>(null)
  // One tab stop: the chosen radio, else the first.
  const stop = Math.max(0, options.indexOf(value))

  function onKeyDown(event: KeyboardEvent) {
    const at = options.indexOf(
      group.current?.querySelector<HTMLElement>('[role="radio"]:focus')?.textContent ?? '',
    )
    const to =
      event.key === 'ArrowRight' || event.key === 'ArrowDown'
        ? (at + 1) % options.length
        : event.key === 'ArrowLeft' || event.key === 'ArrowUp'
          ? (at - 1 + options.length) % options.length
          : event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? options.length - 1
              : -1
    if (to < 0 || at < 0) return
    event.preventDefault()
    onChange(options[to]!)
    group.current?.querySelectorAll<HTMLElement>('[role="radio"]')[to]?.focus()
  }

  return (
    <div
      ref={group}
      onKeyDown={onKeyDown}
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-invalid={invalid ? true : undefined}
      aria-describedby={describedBy}
      className="flex flex-wrap gap-2"
    >
      {options.map((option, i) => {
        const chosen = option === value
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={chosen}
            tabIndex={i === stop ? 0 : -1}
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
