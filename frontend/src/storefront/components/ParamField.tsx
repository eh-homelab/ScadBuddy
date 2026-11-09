import { useId, useState } from 'react'
import { ToggleGroup, ToggleGroupItem } from '../../components/ui/toggle-group'
import type { Filament, ParamValue, PublicParam } from '../api/client'
import { colourGone, filamentName, filamentOf, textLength, valueProblem } from '../params'

interface Props {
  param: PublicParam
  /** The visitor's value; undefined while the shop's own stands. */
  value: ParamValue | undefined
  filaments: readonly Filament[]
  onChange: (next: ParamValue | undefined) => void
}

/**
 * One parameter a listing offers. The public view has no default to show, so a field the
 * visitor has not touched says the shop's choice stands, and sends nothing (spec §5.4: the
 * CRUD side fills it in). Labels and choices are the shop's text, shown as text.
 */
export function ParamField({ param, value, filaments, onChange }: Props) {
  const id = useId()
  const problem = valueProblem(param, value)
  const hintId = `${id}-hint`

  if (param.kind === 'choice') {
    const gone = colourGone(param)
    return (
      <fieldset className="sf-field" aria-describedby={hintId}>
        <legend className="sf-label">{param.label}</legend>
        <ToggleGroup
          type="single"
          className="sf-choices"
          aria-label={param.label}
          value={value === undefined ? '' : String(param.choices.findIndex((choice) => choice === value))}
          // Choosing the one picked again empties the group: back to the shop's choice.
          onValueChange={(picked) => onChange(picked === '' ? undefined : param.choices[Number(picked)])}
        >
          {param.choices.map((choice, index) => {
            const filament = param.colour && typeof choice === 'string' ? filamentOf(filaments, choice) : undefined
            const name = filament ? filamentName(filament) : String(choice)
            return (
              <ToggleGroupItem key={String(choice)} value={String(index)} aria-label={name} className="sf-choice">
                {param.colour && typeof choice === 'string' && <span className="sf-swatch" style={{ background: choice }} />}
                <span>{name}</span>
              </ToggleGroupItem>
            )
          })}
        </ToggleGroup>
        <p id={hintId} className={gone ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
          {gone ? 'No colour is available for this right now.' : value === undefined ? "The shop's choice, unless you pick one." : 'Tap it again to go back to the shop’s choice.'}
        </p>
      </fieldset>
    )
  }

  const fieldId = `${id}-input`
  return (
    <div className="sf-field">
      <label className="sf-label" htmlFor={fieldId}>
        {param.label}
      </label>
      {param.kind === 'number' ? (
        <NumberInput id={fieldId} hintId={hintId} min={param.min} max={param.max} step={param.step ?? undefined} value={value} onChange={onChange} />
      ) : (
        <input
          id={fieldId}
          className="sf-input"
          type="text"
          value={typeof value === 'string' ? value : ''}
          placeholder="The shop's text"
          aria-describedby={hintId}
          aria-invalid={problem ? true : undefined}
          onChange={(event) => onChange(event.target.value === '' ? undefined : event.target.value)}
        />
      )}
      <p id={hintId} className={problem ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
        {problem ??
          (param.kind === 'number'
            ? `From ${param.min} to ${param.max}${param.step ? `, in steps of ${param.step}` : ''}.`
            : `${typeof value === 'string' ? textLength(value) : 0} of ${param.max_length} characters.`)}
      </p>
    </div>
  )
}

/** A number as typed: the text stays the visitor's while it is not yet a number ("1." or "-"). */
function NumberInput({
  id,
  hintId,
  min,
  max,
  step,
  value,
  onChange,
}: {
  id: string
  hintId: string
  min: number
  max: number
  step: number | undefined
  value: ParamValue | undefined
  onChange: (next: ParamValue | undefined) => void
}) {
  const [text, setText] = useState(value === undefined ? '' : String(value))
  const problem = valueProblem({ kind: 'number', min, max, step, label: '', group: '', colour: false }, value)
  return (
    <input
      id={id}
      className="sf-input"
      type="text"
      inputMode="decimal"
      value={text}
      placeholder={`${min} to ${max}`}
      aria-describedby={hintId}
      aria-invalid={problem ? true : undefined}
      onChange={(event) => {
        const next = event.target.value
        setText(next)
        const trimmed = next.trim()
        // Not a number yet is NaN, which the range check refuses until it is one.
        onChange(trimmed === '' ? undefined : Number(trimmed))
      }}
    />
  )
}
