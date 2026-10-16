import type { Param } from '../../api/types'
import { paramLabel, rangeProblem } from '../../lib/params'
import { Field } from './Field'
import { NumberInput } from './NumberInput'
import { Slider } from '@/components/ui/slider'

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
  const error = rangeProblem(param, value)

  return (
    <Field
      id={id}
      label={paramLabel(param)}
      name={param.name}
      readout={<span className="text-foreground">{value}</span>}
      error={error}
    >
      <div className="flex items-center gap-3">
        <Slider
          min={min}
          max={max}
          step={step}
          // Clamped for the thumb only: an off-range value from a preset stays in the box.
          value={[Math.min(max, Math.max(min, value))]}
          thumbLabel={paramLabel(param)}
          onValueChange={([next]) => {
            if (next !== undefined && next !== value) onChange(next)
          }}
          className="flex-1 cursor-pointer py-2"
        />
        <NumberInput
          // The label's target: the box takes typing, the slider only arrows and drags.
          id={id}
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={`${paramLabel(param)} value`}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          onCommit={onChange}
          // Padding, border and the spin buttons take the 2.5 rem; never under 4.5 rem.
          style={{ '--sb-box-chars': boxChars(min, max, step, value) } as React.CSSProperties}
          className="sb-num w-[max(4.5rem,calc(var(--sb-box-chars)*1ch+2.5rem))] shrink-0 text-right"
        />
      </div>
    </Field>
  )
}
