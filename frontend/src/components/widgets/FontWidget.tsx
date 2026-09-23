import { useId } from 'react'
import type { FontFamily, Param } from '../../api/types'
import { Field } from './Field'

/**
 * OpenSCAD font values look like `Liberation Sans:style=Bold`. The container's
 * installed fonts come from `GET /fonts`; the input stays free-text because a
 * model can name a font the server has not enumerated.
 */
export function FontWidget({
  param,
  value,
  fonts,
  onChange,
}: {
  param: Param
  value: string
  fonts: FontFamily[]
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const listId = useId()

  const choices = fonts.flatMap((font) =>
    font.styles.length > 0
      ? font.styles.map((style) => `${font.family}:style=${style}`)
      : [font.family],
  )

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name}>
      <input
        id={id}
        type="text"
        list={listId}
        value={value}
        spellCheck={false}
        placeholder="Family:style=Bold"
        onChange={(event) => onChange(event.target.value)}
        className="sb-field sb-num"
      />
      <datalist id={listId} data-testid="font-options">
        {choices.map((choice) => (
          <option key={choice} value={choice} />
        ))}
      </datalist>
    </Field>
  )
}
