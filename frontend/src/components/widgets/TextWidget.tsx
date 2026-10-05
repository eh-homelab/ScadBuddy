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
        // An edit past the limit is refused whole, as maxlength refuses it, rather than
        // trimmed from the end, which would drop the name's last letters wherever the caret is.
        onChange={(event) => {
          const next = event.target.value
          if (maxLength && textLength(next) > maxLength && textLength(next) > length) return
          onChange(next)
        }}
        className="sb-field"
      />
    </Field>
  )
}
