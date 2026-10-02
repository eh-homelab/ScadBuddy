import type { OutputPlate } from '../../api/types'

type Props = {
  plates: OutputPlate[]
  value: number | 'all'
  onChange: (next: number | 'all') => void
  /** #313 — the plate's image, from whichever source is being printed. */
  thumbnailUrl: (index: number) => string
}

/**
 * #83 — which plate of a multi-plate 3MF to print, or all of them. #929: a plate is
 * labelled by what it holds; its number is secondary, and the label only when unnamed.
 */
export function PlatesToPrint({ plates, value, onChange, thumbnailUrl }: Props) {
  return (
    <fieldset data-testid="plate-choice">
      <legend className="text-[13px]">Plates to print</legend>
      <div className="mt-1.5 flex flex-wrap gap-2">
        {plates.map((entry) => {
          const number = `Plate ${entry.index}`
          return (
            <label
              key={entry.index}
              className={`flex cursor-pointer items-center gap-2 rounded-[6px] border p-2 text-[13px] ${
                value === entry.index
                  ? 'border-accent bg-accent/8'
                  : 'border-line bg-surface-2'
              }`}
            >
              <input
                type="radio"
                name="print-plate"
                checked={value === entry.index}
                onChange={() => onChange(entry.index)}
                className="accent-[var(--sb-accent)]"
              />
              {entry.has_thumbnail && (
                <img
                  src={thumbnailUrl(entry.index)}
                  alt={entry.name ?? number}
                  className="h-12 w-12 rounded-[4px] object-contain"
                />
              )}
              {entry.name ? (
                <span className="flex flex-col leading-tight">
                  {entry.name}
                  <span className="sb-num text-[11px] text-faint">{number}</span>
                </span>
              ) : (
                <span>
                  Plate <span className="sb-num">{entry.index}</span>
                </span>
              )}
            </label>
          )
        })}
        <label
          className={`flex cursor-pointer items-center gap-2 rounded-[6px] border p-2 text-[13px] ${
            value === 'all' ? 'border-accent bg-accent/8' : 'border-line bg-surface-2'
          }`}
        >
          <input
            type="radio"
            name="print-plate"
            checked={value === 'all'}
            onChange={() => onChange('all')}
            className="accent-[var(--sb-accent)]"
          />
          All plates
        </label>
      </div>
      {value === 'all' && (
        <p className="mt-1.5 text-[12px] text-faint">One queue item per plate.</p>
      )}
    </fieldset>
  )
}
