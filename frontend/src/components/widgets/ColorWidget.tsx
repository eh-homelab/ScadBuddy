import { useState } from 'react'
import type { Param } from '../../api/types'
import { colourHex, rangeProblem } from '../../lib/params'
import { Field } from './Field'
import { FilamentColourPicker } from './FilamentColourPicker'

/** A whole `#RRGGBB` as typed: taken at once, before the field loses focus. */
const WHOLE_HEX = /^#?[0-9a-f]{6}$/i

export function ColorWidget({
  param,
  value,
  extruder,
  onChange,
}: {
  param: Param
  value: string
  /** null: the latest render never used this colour (#938). */
  extruder?: number | null
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const colour = colourHex(value)
  const hex = colour ?? value
  // #2357 — what is being typed, until it is a whole colour or the field loses focus: a
  // partial or wrong value is never padded into a colour ("red" was sent as "#RREEDD");
  // a colour name becomes its hex.
  const [draft, setDraft] = useState<string | null>(null)
  const error = draft === null ? rangeProblem(param, value) : null

  function commit(text: string) {
    setDraft(null)
    onChange(colourHex(text) ?? text.trim())
  }

  return (
    <Field
      id={id}
      label={param.caption ?? param.name}
      name={param.name}
      error={error}
      readout={
        extruder === null ? (
          <span className="text-faint">not in this render</span>
        ) : extruder ? (
          <span className="text-muted">extruder {extruder}</span>
        ) : undefined
      }
    >
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="color"
          // The picker takes only #rrggbb; while the text is no colour it shows black.
          value={(colour ?? '#000000').toLowerCase()}
          aria-label={param.caption ?? param.name}
          onChange={(event) => commit(event.target.value)}
          className="size-8 shrink-0 cursor-pointer rounded-[4px] border border-line bg-surface-2 p-0.5"
        />
        <input
          type="text"
          value={draft ?? hex}
          aria-label={`${param.caption ?? param.name} hex`}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          spellCheck={false}
          onChange={(event) => {
            const text = event.target.value.toUpperCase()
            if (WHOLE_HEX.test(text.trim())) commit(text)
            else setDraft(text)
          }}
          onBlur={(event) => commit(event.target.value)}
          className="sb-field sb-num uppercase"
        />
      </div>
      <FilamentColourPicker label={param.caption ?? param.name} value={hex} onPick={onChange} />
    </Field>
  )
}
