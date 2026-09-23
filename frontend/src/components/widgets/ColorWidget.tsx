import type { Param } from '../../api/types'
import { normalizeHex } from '../../lib/format'
import { Field } from './Field'

export function ColorWidget({
  param,
  value,
  extruder,
  onChange,
}: {
  param: Param
  value: string
  extruder?: number
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const hex = normalizeHex(value)

  return (
    <Field
      id={id}
      label={param.caption ?? param.name}
      name={param.name}
      readout={extruder ? <span className="text-muted">extruder {extruder}</span> : undefined}
    >
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="color"
          value={hex.toLowerCase()}
          aria-label={param.caption ?? param.name}
          onChange={(event) => onChange(normalizeHex(event.target.value))}
          className="size-8 shrink-0 cursor-pointer rounded-[4px] border border-line bg-surface-2 p-0.5"
        />
        <input
          type="text"
          value={hex}
          aria-label={`${param.caption ?? param.name} hex`}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value.toUpperCase())}
          onBlur={(event) => onChange(normalizeHex(event.target.value))}
          className="sb-field sb-num uppercase"
        />
      </div>
    </Field>
  )
}
