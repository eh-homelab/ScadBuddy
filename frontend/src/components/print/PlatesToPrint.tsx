import type { OutputPlate } from '../../api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { plateLabel } from '../../lib/plate'
import { plateChosen, platesSummary, togglePlate, type PlateChoice } from '../../lib/plateChoice'
import { cn } from '@/lib/utils'

type Props = {
  plates: OutputPlate[]
  value: PlateChoice
  onChange: (next: PlateChoice) => void
  /** #313 — the plate's image, from whichever source is being printed. */
  thumbnailUrl: (index: number) => string
  /** #2169 — the plate the dialog's preview pane shows; a plate ticked or unticked is shown there. */
  previewed?: number
  onPreview?: (index: number) => void
}

/**
 * #83, #3652 — which plates of a multi-plate 3MF to print: any set of them. Each plate is
 * one card and one click target: a click anywhere on it, image or name, ticks or unticks
 * it and shows it in the preview. Chosen is the tick and the card's border; previewed is
 * said in words; focus is the ring, so the three never look alike. #929: a plate is
 * labelled by what it holds; its number is secondary, and the label only when unnamed.
 */
export function PlatesToPrint({ plates, value, onChange, thumbnailUrl, previewed, onPreview }: Props) {
  const offered = plates.map((entry) => entry.index)
  const chosen = value === 'all' ? plates.length : value.length
  return (
    <div role="group" aria-labelledby="plates-to-print" data-testid="plate-choice" className="min-w-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span id="plates-to-print" className="text-sm font-medium">
          Plates to print
        </span>
        <div className="flex gap-1">
          <Button
            type="button"
            size="xs"
            variant="ghost"
            disabled={value === 'all'}
            onClick={() => onChange('all')}
            data-testid="plates-all"
          >
            All
          </Button>
          <Button
            type="button"
            size="xs"
            variant="ghost"
            disabled={chosen === 0}
            onClick={() => onChange([])}
            data-testid="plates-none"
          >
            None
          </Button>
        </div>
      </div>
      <div className="mt-1.5 grid grid-cols-[repeat(auto-fill,minmax(8.5rem,1fr))] gap-2">
        {plates.map((entry) => {
          const checked = plateChosen(value, entry.index)
          const shown = previewed === entry.index
          const label = plateLabel(plates, entry.index)
          return (
            <label
              key={entry.index}
              data-testid={`plate-card-${entry.index}`}
              data-checked={checked || undefined}
              className={cn(
                'flex min-w-0 cursor-pointer flex-col gap-1.5 rounded-lg border bg-card p-2 text-sm transition-colors',
                'hover:bg-muted has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50',
                checked ? 'border-primary' : 'border-border',
              )}
            >
              <span className="flex min-h-5 items-center justify-between gap-2">
                <Checkbox
                  checked={checked}
                  onCheckedChange={() => {
                    onChange(togglePlate(value, entry.index, offered))
                    onPreview?.(entry.index)
                  }}
                  aria-label={label}
                  data-testid={`plate-${entry.index}`}
                />
                {shown && onPreview && (
                  <Badge variant="secondary" data-testid={`plate-previewed-${entry.index}`}>
                    Previewing
                  </Badge>
                )}
              </span>
              {entry.has_thumbnail && (
                <img
                  src={thumbnailUrl(entry.index)}
                  alt=""
                  className={cn('aspect-square w-full rounded-md bg-muted object-contain', !checked && 'opacity-60')}
                />
              )}
              <span className="flex min-w-0 flex-col gap-0.5 leading-tight">
                <span className="truncate font-medium">{entry.name || `Plate ${entry.index}`}</span>
                {entry.name && <span className="sb-num text-xs text-muted-foreground">Plate {entry.index}</span>}
              </span>
            </label>
          )
        })}
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground" aria-live="polite" data-testid="plates-summary">
        {platesSummary(value, plates)}
        {chosen > 1 && '. One queue item per plate.'}
        {chosen === 0 && '. Choose at least one to print.'}
      </p>
    </div>
  )
}
