import type { Param } from '../../api/types'
import { Field } from './Field'

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
      <input
        id={id}
        type="number"
        value={value}
        min={param.min ?? undefined}
        max={param.max ?? undefined}
        step={step}
        onChange={(event) => {
          const next = Number(event.target.value)
          onChange(param.type === 'integer' ? Math.round(next) : next)
        }}
        className="sb-field sb-num"
      />
    </Field>
  )
}
