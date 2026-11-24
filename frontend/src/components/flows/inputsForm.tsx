import { useId, useState } from 'react'
import type { AnswerProblem } from '../../api/types'
import { defaultText, initialValues, itemLines, noErrors, toInputs, type InputField, type InputValues } from '../../lib/flowInputs'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

interface InputsOptions {
  /** #7364 — a prompt's answer, not a run's inputs: see `toInputs`. */
  answer?: boolean
  /** What the server said of the values sent; what this form says of a field comes first. */
  problems?: readonly AnswerProblem[]
}

/**
 * Each problem by the field it is shown under, and the rest, which have no field. The server
 * counts an array's items; the person sees lines, so an item is named by its line in `sent`.
 */
function placed(fields: InputField[], sent: InputValues, problems: readonly AnswerProblem[]) {
  const names = new Set(fields.map((field) => field.name))
  const said = noErrors()
  const rest: string[] = []
  for (const { field, message } of problems) {
    const [name = '', item] = field.split('/')
    if (!names.has(name)) {
      rest.push(field ? `${field}: ${message}` : message)
      continue
    }
    const line = item && (itemLines(String(sent[name] ?? ''))[Number(item)]?.line ?? Number(item) + 1)
    const text = line ? `Line ${line}: ${message}` : message
    said[name] = said[name] ? `${said[name]}; ${text}` : text
  }
  return { said, rest }
}

/**
 * The form's values and errors, and the inputs once they parse. `parse` takes the fields
 * the browser could not read (a date with no time), which are never sent as blank. `rest`
 * are the problems that are no field's.
 */
export function useInputs(fields: InputField[], { answer = false, problems = [] }: InputsOptions = {}) {
  const [values, setValues] = useState<InputValues>(() => initialValues(fields))
  const [sent, setSent] = useState(values)
  const [errors, setErrors] = useState<Record<string, string>>(noErrors)
  const parse = (unread: readonly string[] = []): Record<string, unknown> | null => {
    const parsed = toInputs(fields, values, { answer, unread })
    setErrors(parsed.errors)
    if (Object.keys(parsed.errors).length > 0) return null
    setSent(values)
    return parsed.inputs
  }
  const { said, rest } = placed(fields, sent, problems)
  const rendered = fields.map((field) => (
    <Field
      key={field.name}
      field={field}
      value={values[field.name] ?? ''}
      error={errors[field.name] ?? said[field.name]}
      answer={answer}
      onChange={(next) => setValues((now) => ({ ...now, [field.name]: next }))}
    />
  ))
  return { parse, rendered, errors, rest }
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
  const becomes = answer && !required && typeof value === 'string' && value.trim() === '' ? defaultText(field) : ''
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
    <span className="min-w-0 break-words">
      {field.label}
      {required && <span className="text-destructive"> *</span>}
      {field.label !== field.name && (
        <code className="ml-1.5 font-mono text-[11px] font-normal text-muted-foreground">{field.name}</code>
      )}
    </span>
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
        <div className="flex w-fit max-w-full items-center gap-2">
          <Checkbox {...common} checked={value === true} onCheckedChange={(next) => onChange(next === true)} />
          <Label htmlFor={id} className="min-w-0">
            {label}
          </Label>
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
