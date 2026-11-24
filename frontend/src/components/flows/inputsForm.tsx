import { useId, useState } from 'react'
import { initialValues, noErrors, toInputs, type InputField, type InputValues } from '../../lib/flowInputs'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

interface InputsOptions {
  /** #7364 — a prompt's answer, not a run's inputs: see `toInputs`. */
  answer?: boolean
  /** What the server said of each field; what this form says of it comes first. */
  problems?: Record<string, string>
}

/**
 * The form's values and errors, and the inputs once they parse. `parse` takes the fields
 * the browser could not read (a date with no time), which are never sent as blank.
 */
export function useInputs(fields: InputField[], { answer = false, problems }: InputsOptions = {}) {
  const [values, setValues] = useState<InputValues>(() => initialValues(fields))
  const [errors, setErrors] = useState<Record<string, string>>(noErrors)
  const parse = (unread: readonly string[] = []): Record<string, unknown> | null => {
    const parsed = toInputs(fields, values, { answer })
    for (const name of unread) parsed.errors[name] = 'This is not complete'
    setErrors(parsed.errors)
    return Object.keys(parsed.errors).length === 0 ? parsed.inputs : null
  }
  const rendered = fields.map((field) => (
    <Field
      key={field.name}
      field={field}
      value={values[field.name] ?? ''}
      error={errors[field.name] ?? problems?.[field.name]}
      answer={answer}
      onChange={(next) => setValues((now) => ({ ...now, [field.name]: next }))}
    />
  ))
  return { parse, rendered, errors }
}

/** How an input of each string format is drawn. */
const TYPED = {
  text: { type: 'text' },
  email: { type: 'email', autoComplete: 'email' },
  date: { type: 'date', className: 'w-fit' },
  'date-time': { type: 'datetime-local', className: 'w-fit' },
} as const

/** An optional choice left unset: a select item cannot have the empty value. */
const NONE = '\u0000none'

function Field({
  field,
  value,
  error,
  answer,
  onChange,
}: {
  field: InputField
  value: string | boolean
  error?: string
  answer: boolean
  onChange: (next: string | boolean) => void
}) {
  const id = useId()
  const help = `${id}-help`
  const required = field.required && (answer || field.default === undefined)
  // What a blank field left out of an answer becomes.
  const blank = answer && !required && typeof value === 'string' && value.trim() === '' ? (field.default ?? '') : ''
  const becomes = Array.isArray(blank) ? blank.join(', ') : String(blank)
  const lines = [
    field.description,
    field.type === 'array' && 'One per line.',
    becomes && `Left empty, this is ${becomes}.`,
  ].filter(Boolean)
  const described = [lines.length > 0 ? help : '', error ? `${id}-error` : '']
    .filter(Boolean)
    .join(' ')
  const common = {
    id,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': described || undefined,
    'aria-required': required ? true : undefined,
  }
  const label = (
    <>
      {field.label}
      {required && <span className="text-destructive"> *</span>}
      {field.label !== field.name && (
        <code className="ml-1.5 font-mono text-[11px] font-normal text-muted-foreground">{field.name}</code>
      )}
    </>
  )
  const hint = lines.length > 0 && (
    <p id={help} className="text-[12px] break-words text-muted-foreground">
      {lines.join(' ')}
    </p>
  )
  const problem = error && (
    <p id={`${id}-error`} className="text-[12px] text-destructive first-letter:uppercase">
      {error}
    </p>
  )

  if (field.type === 'boolean') {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex w-fit items-center gap-2">
          <Checkbox {...common} checked={value === true} onCheckedChange={(next) => onChange(next === true)} />
          <Label htmlFor={id}>{label}</Label>
        </div>
        {hint}
        {problem}
      </div>
    )
  }

  let control
  if (field.enum) {
    // With no default, nothing is chosen until the person chooses: never the first option shown chosen.
    const optional = field.default === undefined && !field.required
    control = (
      <Select
        value={value === '' && optional ? NONE : String(value)}
        onValueChange={(next) => onChange(next === NONE ? '' : next)}
      >
        <SelectTrigger {...common} className="w-full">
          <SelectValue placeholder="Choose…" />
        </SelectTrigger>
        <SelectContent>
          {optional && <SelectItem value={NONE}>(none)</SelectItem>}
          {field.enum.map((option) => (
            <SelectItem key={String(option)} value={String(option)}>
              {String(option)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  } else if (field.type === 'array') {
    control = (
      <Textarea {...common} rows={3} className="font-mono" value={String(value)} onChange={(event) => onChange(event.target.value)} />
    )
  } else if (field.type === 'integer' || field.type === 'number') {
    control = (
      <Input
        {...common}
        data-field={field.name}
        type="number"
        inputMode={field.type === 'integer' ? 'numeric' : 'decimal'}
        step={field.type === 'integer' ? 1 : 'any'}
        min={field.minimum}
        max={field.maximum}
        value={String(value)}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  } else {
    control = (
      <Input
        {...common}
        data-field={field.name}
        {...TYPED[field.format ?? 'text']}
        value={String(value)}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {control}
      {hint}
      {problem}
    </div>
  )
}
