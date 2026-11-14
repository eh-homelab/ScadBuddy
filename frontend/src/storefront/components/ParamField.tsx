import { useId, useState } from 'react'
import { ToggleGroup, ToggleGroupItem } from '../../components/ui/toggle-group'
import type { Filament, ParamValue, PublicParam } from '../api/client'
import { colourGone, filamentName, filamentOf, parseNumber, textLength, valueProblem, valueText } from '../params'

interface Props {
  param: PublicParam
  /** The visitor's value; undefined while the shop's own stands. */
  value: ParamValue | undefined
  filaments: readonly Filament[]
  onChange: (next: ParamValue | undefined) => void
}

const LABEL = 'mb-1.5 block p-0 font-semibold'
const HINT = 'mt-1.5 text-sm text-muted-foreground'
const BAD = 'mt-1.5 text-sm text-destructive'
const INPUT =
  'min-h-12 w-full rounded-[10px] border-[1.5px] border-input bg-background px-3.5 py-2.5 aria-invalid:border-destructive'

/**
 * One parameter a listing offers. A field the visitor has not touched says the shop's
 * choice stands, and sends nothing (spec §5.4: the CRUD side fills it in); only a text
 * field shows the design's default, as its placeholder (#6137). Labels are the listing's
 * own or the template's caption without its extruder note (`shown_label`), and choices
 * are the shop's values, a switch's as Yes or No (`valueText`); all are shown as text. Styled
 * with the theme's tokens only, so the back office's preview (StorefrontPage) shows the
 * same form in its own colours.
 */
export function ParamField({ param, value, filaments, onChange }: Props) {
  const id = useId()
  const problem = valueProblem(param, value)
  const hintId = `${id}-hint`

  if (param.kind === 'choice') {
    const gone = colourGone(param)
    return (
      <fieldset className="m-0 min-w-0 border-0 p-0" aria-describedby={hintId}>
        <legend className={LABEL}>{param.label}</legend>
        <ToggleGroup
          type="single"
          spacing={2}
          className="w-full flex-wrap"
          aria-label={param.label}
          value={value === undefined ? '' : String(param.choices.findIndex((choice) => choice === value))}
          // Choosing the one picked again empties the group: back to the shop's choice.
          onValueChange={(picked) => onChange(picked === '' ? undefined : param.choices[Number(picked)])}
        >
          {param.choices.map((choice, index) => {
            const filament = param.colour && typeof choice === 'string' ? filamentOf(filaments, choice) : undefined
            const name = filament ? filamentName(filament) : valueText(choice)
            return (
              <ToggleGroupItem
                key={String(choice)}
                value={String(index)}
                aria-label={name}
                variant="outline"
                className="h-auto min-h-11 max-w-full rounded-full px-3.5 py-1.5 text-left whitespace-normal [overflow-wrap:anywhere] data-[state=on]:border-primary data-[state=on]:ring-1 data-[state=on]:ring-primary"
              >
                {param.colour && typeof choice === 'string' && (
                  <span className="size-5 flex-none rounded-full border border-foreground/25" style={{ background: choice }} />
                )}
                <span>{name}</span>
              </ToggleGroupItem>
            )
          })}
        </ToggleGroup>
        <p id={hintId} className={gone ? BAD : HINT}>
          {gone
            ? 'No colour is available for this right now.'
            : value === undefined
              ? "The shop's choice, unless you pick one."
              : 'Tap it again to go back to the shop’s choice.'}
        </p>
      </fieldset>
    )
  }

  const fieldId = `${id}-input`
  return (
    <div className="min-w-0">
      <label className={LABEL} htmlFor={fieldId}>
        {param.label}
      </label>
      {param.kind === 'number' ? (
        <NumberInput id={fieldId} hintId={hintId} param={param} value={value} onChange={onChange} />
      ) : (
        <input
          id={fieldId}
          className={INPUT}
          type="text"
          value={typeof value === 'string' ? value : ''}
          placeholder={param.kind === 'text' ? (param.placeholder ?? undefined) : undefined}
          aria-describedby={hintId}
          aria-invalid={problem ? true : undefined}
          onChange={(event) => onChange(event.target.value === '' ? undefined : event.target.value)}
        />
      )}
      <p id={hintId} className={problem ? BAD : HINT}>
        {problem ??
          (param.kind === 'number'
            ? `From ${param.min} to ${param.max}${param.step ? `, in steps of ${param.step}` : ''}.`
            : `${typeof value === 'string' ? textLength(value) : 0} of ${param.max_length} characters.`)}
      </p>
    </div>
  )
}

/**
 * A number as typed: the text stays the visitor's while it is not yet a number ("1." or
 * "-"), which the range check refuses. A listing that allows a negative value gets the
 * text keyboard: the iPhone's decimal pad has no minus key.
 */
function NumberInput({
  id,
  hintId,
  param,
  value,
  onChange,
}: {
  id: string
  hintId: string
  param: Extract<PublicParam, { kind: 'number' }>
  value: ParamValue | undefined
  onChange: (next: ParamValue | undefined) => void
}) {
  const [text, setText] = useState(value === undefined ? '' : String(value))
  const problem = valueProblem(param, value)
  return (
    <input
      id={id}
      className={INPUT}
      type="text"
      inputMode={param.min < 0 ? 'text' : 'decimal'}
      value={text}
      placeholder={`${param.min} to ${param.max}`}
      aria-describedby={hintId}
      aria-invalid={problem ? true : undefined}
      onChange={(event) => {
        const next = event.target.value
        setText(next)
        onChange(next.trim() === '' ? undefined : parseNumber(next))
      }}
    />
  )
}
