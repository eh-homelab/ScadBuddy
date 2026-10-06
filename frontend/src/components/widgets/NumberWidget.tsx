import type { Param } from '../../api/types'
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

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name}>
      <NumberInput
        id={id}
        value={value}
        min={param.min ?? undefined}
        max={param.max ?? undefined}
        step={step}
        onCommit={(next) => onChange(param.type === 'integer' ? Math.round(next) : next)}
        className="sb-field sb-num"
      />
    </Field>
  )
}
