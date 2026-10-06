import type { Param } from '../../api/types'
import { rangeProblem } from '../../lib/params'
import { Field } from './Field'
import { NumberInput } from './NumberInput'

export function NumberWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: number
  onChange: (next: number) => void
}) {
  const id = `p-${param.name}`
  const step = param.type === 'integer' ? 1 : (param.step ?? 0.1)
  const error = rangeProblem(param, value)

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name} error={error}>
      <NumberInput
        id={id}
        value={value}
        min={param.min ?? undefined}
        max={param.max ?? undefined}
        step={step}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        onCommit={(next) => onChange(param.type === 'integer' ? Math.round(next) : next)}
        className="sb-field sb-num"
      />
    </Field>
  )
}
