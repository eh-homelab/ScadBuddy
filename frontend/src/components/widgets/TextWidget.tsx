import type { Param } from '../../api/types'
import { textLength } from '../../lib/params'
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
  const length = textLength(value)
  const readout = maxLength ? (
    <span className={length >= maxLength ? 'text-accent' : undefined}>
      {length}/{maxLength}
    </span>
  ) : undefined

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name} readout={readout}>
      <input
        id={id}
        type="text"
        value={value}
        // Not the maxLength attribute: it counts UTF-16 units, so an emoji takes two (#920).
        onChange={(event) =>
          onChange(maxLength ? Array.from(event.target.value).slice(0, maxLength).join('') : event.target.value)
        }
        className="sb-field"
      />
    </Field>
  )
}
