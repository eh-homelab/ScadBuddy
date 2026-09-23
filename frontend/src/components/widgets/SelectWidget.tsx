import type { Param, ParamValue } from '../../api/types'
import { Field } from './Field'

export function SelectWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: ParamValue
  onChange: (next: ParamValue) => void
}) {
  const id = `p-${param.name}`
  const options = param.options ?? []

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name}>
      <select
        id={id}
        value={String(value)}
        onChange={(event) => {
          const picked = options.find((option) => String(option.value) === event.target.value)
          onChange(picked ? picked.value : event.target.value)
        }}
        className="sb-field cursor-pointer"
      >
        {options.map((option) => (
          <option key={String(option.value)} value={String(option.value)}>
            {option.name}
          </option>
        ))}
      </select>
    </Field>
  )
}
