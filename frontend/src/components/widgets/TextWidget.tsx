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
  const maxLength = param.max_length ?? undefined
  const readout = maxLength ? (
    <span className={value.length >= maxLength ? 'text-accent' : undefined}>
      {value.length}/{maxLength}
    </span>
  ) : undefined

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name} readout={readout}>
      <input
        id={id}
        type="text"
        value={value}
        maxLength={maxLength}
        onChange={(event) => onChange(event.target.value)}
        className="sb-field"
      />
    </Field>
  )
}
