import type { Param } from '../../api/types'
import { Field } from './Field'

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
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={`${param.caption ?? param.name} value`}
          onChange={(event) => onChange(Number(event.target.value))}
          className="sb-field sb-num w-[4.5rem] shrink-0 text-right"
        />
      </div>
    </Field>
  )
}
