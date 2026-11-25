import { useId, useState } from 'react'
import type { PropertyValue } from './api'
import { definedKeys, propertiesOf, requiredOf, typeOf, type FieldErrors, type Sub, type Values } from './definedProperties'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const NONE = '__none__'

/** The defined keys' values of a row (#7703), as fields of its own form. */
export function DefinedFields({ schema, values, onChange, errors }: { schema: Record<string, unknown>; values: Values; onChange: (values: Values) => void; errors?: FieldErrors }) {
  const keys = definedKeys(schema)
  if (keys.length === 0) return null
  const required = new Set(requiredOf(schema))
  const subs = propertiesOf(schema)
  return (
    <fieldset aria-label="Defined properties" className="grid gap-3 sm:col-span-2 sm:grid-cols-2">
      {keys.map((key) => (
        <Field
          key={key}
          name={key}
          sub={subs[key]!}
          required={required.has(key)}
          value={values[key]}
          problem={errors?.missing.includes(key) ? 'Required' : errors?.invalid.find((one) => one.key === key)?.reason}
          onChange={(next) => onChange({ ...values, [key]: next })}
        />
      ))}
    </fieldset>
  )
}

function Field({ name, sub, required, value, problem, onChange }: { name: string; sub: Sub; required: boolean; value: PropertyValue | undefined; problem?: string; onChange: (value: PropertyValue | undefined) => void }) {
  const id = useId()
  const hint = `${id}-problem`
  const label = sub.title ?? name
  const type = typeOf(sub)
  const aria = { 'aria-label': label, 'aria-invalid': problem ? true : undefined, 'aria-describedby': problem ? hint : undefined, 'aria-required': required || undefined }
  const caption = (
    <>
      {label}
      {required && (
        <span aria-hidden className="text-warn">
          {' '}*
        </span>
      )}
    </>
  )
  const note = problem && (
    <span id={hint} className="text-xs text-warn">
      {problem}
    </span>
  )
  if (type === 'boolean')
    return (
      <div data-testid="defined-field" data-key={name} className="flex flex-col gap-1">
        <label className="flex items-center gap-2 self-start pt-5">
          <Checkbox checked={value === true} onCheckedChange={(on) => onChange(on === true)} {...aria} />
          {caption}
        </label>
        {note}
      </div>
    )
  if (sub.enum && sub.enum.length > 0)
    return (
      <div data-testid="defined-field" data-key={name} className="flex flex-col gap-1">
        <span>{caption}</span>
        <Select value={value === undefined ? NONE : String(value)} onValueChange={(next) => onChange(next === NONE ? undefined : (sub.enum!.find((one) => String(one) === next) as PropertyValue))}>
          <SelectTrigger className="w-full text-[13px]" {...aria}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>None</SelectItem>
            {sub.enum.map((one) => (
              <SelectItem key={String(one)} value={String(one)}>
                {String(one)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {note}
      </div>
    )
  if (type === 'array') {
    const items = Array.isArray(value) && value.length > 0 ? value : ['']
    return (
      <div data-testid="defined-field" data-key={name} role="group" aria-label={label} className="flex flex-col gap-1">
        <span>{caption}</span>
        {items.map((item, at) => (
          <Input key={at} value={item} onChange={(event) => onChange(items.map((one, i) => (i === at ? event.target.value : one)))} {...(at === 0 ? aria : {})} aria-label={`${label} item ${at + 1}`} />
        ))}
        <Button type="button" size="xs" variant="outline" className="self-start" onClick={() => onChange([...items, ''])}>
          Add {label} item
        </Button>
        {note}
      </div>
    )
  }
  return (
    <label data-testid="defined-field" data-key={name} className="flex flex-col gap-1">
      {caption}
      {type === 'number' || type === 'integer' ? (
        <NumberInput value={typeof value === 'number' ? value : undefined} onChange={onChange} aria={aria} integer={type === 'integer'} />
      ) : (
        <Input value={typeof value === 'string' ? value : ''} onChange={(event) => onChange(event.target.value)} {...aria} />
      )}
      {note}
    </label>
  )
}

/** A number field that keeps what is typed ("1.") while it is not a number yet. */
function NumberInput({ value, onChange, aria, integer }: { value: number | undefined; onChange: (value: number | undefined) => void; aria: Record<string, unknown>; integer: boolean }) {
  const [text, setText] = useState(value === undefined ? '' : String(value))
  return (
    <Input
      type="number"
      step={integer ? 1 : 'any'}
      value={text}
      onChange={(event) => {
        setText(event.target.value)
        const parsed = event.target.value.trim() === '' ? undefined : Number(event.target.value)
        onChange(parsed === undefined || Number.isNaN(parsed) ? undefined : parsed)
      }}
      {...aria}
    />
  )
}
