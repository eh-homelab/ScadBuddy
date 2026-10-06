import type { Param } from '../../api/types'
import { Field } from './Field'
import { NumberInput } from './NumberInput'

/**
 * #367 — the characters the number box must show: the range's ends at the step's
 * decimals, or the value itself when it has more (an off-step value from a preset).
 * A fixed 4.5 rem cut "-0.06" to "-0.0" behind Chromium's spin buttons.
 */
function boxChars(min: number, max: number, step: number, value: number): number {
  const decimals = String(step).split('.')[1]?.length ?? 0
  return Math.max(...[min, max].map((end) => end.toFixed(decimals).length), String(value).length)
}

export function SliderWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: number
  onChange: (next: number) => void
}) {
  const id = `p-${param.name}`
  const min = param.min ?? 0
  const max = param.max ?? 100
  const step = param.step ?? 1

  return (
    <Field
      id={id}
      label={param.caption ?? param.name}
      name={param.name}
      readout={<span className="text-ink">{value}</span>}
    >
      <div className="flex items-center gap-2.5">
        <input
          id={id}
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={param.caption ?? param.name}
          onChange={(event) => onChange(Number(event.target.value))}
          className="h-1.5 w-full flex-1 cursor-pointer appearance-none rounded-full bg-surface-3 accent-[var(--sb-accent)]"
        />
        <NumberInput
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={`${param.caption ?? param.name} value`}
          onCommit={onChange}
          // Padding, border and the spin buttons take the 2.5 rem; never under 4.5 rem.
          style={{ '--sb-box-chars': boxChars(min, max, step, value) } as React.CSSProperties}
          className="sb-field sb-num w-[max(4.5rem,calc(var(--sb-box-chars)*1ch+2.5rem))] shrink-0 text-right"
        />
      </div>
    </Field>
  )
}
