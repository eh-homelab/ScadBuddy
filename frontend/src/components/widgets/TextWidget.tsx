import type { Param } from '../../api/types'
import { Field } from './Field'

export function TextWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: string
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const readout = param.maxLength ? (
    <span className={value.length >= param.maxLength ? 'text-accent' : undefined}>
      {value.length}/{param.maxLength}
    </span>
  ) : undefined

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name} readout={readout}>
      <input
        id={id}
        type="text"
        value={value}
        maxLength={param.maxLength}
        onChange={(event) => onChange(event.target.value)}
        className="sb-field"
      />
    </Field>
  )
}
