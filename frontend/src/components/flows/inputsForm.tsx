import { useId, useState } from 'react'
import type { AnswerProblem } from '../../api/types'
import { defaultText, expectedText, initialValues, itemLines, noErrors, toInputs, type InputField, type InputValues } from '../../lib/flowInputs'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { LibraryField } from './widgets/LibraryField'
import { ScanField } from './widgets/ScanField'
import { Segmented } from './widgets/Segmented'
import { Stepper } from './widgets/Stepper'
import { UploadField } from './widgets/UploadField'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

interface InputsOptions {
  /** #7364 — a prompt's answer, not a run's inputs: see `toInputs`. */
  answer?: boolean
  /** What the server said of the values sent; what this form says of a field comes first. */
  problems?: readonly AnswerProblem[]
  /** #5701 — what each field starts at, by name, in place of its schema's default. */
  values?: Readonly<Record<string, unknown>>
  /** #5701 — false leaves each field's own name out of its label. Default true. */
  names?: boolean
  /** #5701 — called at each edit. */
  onChange?: () => void
  /** #8062 — the task a photo or file is uploaded to; with none, such a field is plain text. */
  taskId?: string
  /** #8062 — the weight the task's context expects, shown beside a weight field. */
  expect?: { weight_g: number; tolerance_g?: number }
}

/** A string enum of up to this many options is drawn as buttons, more as a select (spec §6.3). */
const SEGMENTED_MAX = 5

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
export function useInputs(fields: InputField[], { answer = false, problems = [], values: starts, names = true, onChange, taskId, expect }: InputsOptions = {}) {
  const [values, setValues] = useState<InputValues>(() =>
    initialValues(starts ? fields.map((field) => ({ ...field, default: starts[field.name] })) : fields),
  )
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
      names={names}
      taskId={taskId}
      expect={expect}
      onChange={(next) => {
        onChange?.()
        setValues((now) => ({ ...now, [field.name]: next }))
      }}
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
  names,
  taskId,
  expect,
  onChange,
}: {
  taskId?: string
  expect?: InputsOptions['expect']
  field: InputField
  value: string | boolean
  error?: string
  answer: boolean
  names: boolean
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
      {names && field.label !== field.name && (
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
    <p id={`${id}-error`} className="min-w-0 text-[12px] break-words text-destructive first-letter:uppercase">
      {error}
    </p>
  )

  if (field.type === 'boolean' && field.widget === 'confirm') {
    return (
      <div className="flex flex-col gap-1.5">
        <Button
          {...common}
          type="button"
          size="lg"
          className="h-14 w-full text-base"
          onClick={(event) => {
            const form = event.currentTarget.form
            onChange(true)
            // Submits once the form holds the `true`.
            setTimeout(() => form?.requestSubmit())
          }}
        >
          {field.label}
        </Button>
        {hint}
        {problem}
      </div>
    )
  }

  if (field.type === 'boolean') {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex w-fit max-w-full items-center gap-3">
          <Switch {...common} checked={value === true} onCheckedChange={(next) => onChange(next)} />
          <Label htmlFor={id} className="min-w-0">
            {label}
          </Label>
        </div>
        {hint}
        {problem}
      </div>
    )
  }

  const optional = field.default === undefined && !field.required
  const plain = field.type === 'string' && !field.enum
  const scanKinds = field.kind === 'location' || field.kind === 'spool' ? [field.kind] : undefined
  if (field.widget === 'entity' && field.kind !== 'library_file' && !scanKinds) warnOnce(`${field.name}: unknown entity kind ${JSON.stringify(field.kind)}`)

  // The library picker has a label of its own.
  if (plain && field.widget === 'entity' && field.kind === 'library_file') {
    return (
      <div className="flex flex-col gap-1.5">
        <LibraryField label={field.label} onPick={(file) => onChange(file ? String(file.id) : '')} />
        {hint}
        {problem}
      </div>
    )
  }

  let control
  if (plain && (field.widget === 'scan' || scanKinds)) {
    control = <ScanField common={common} id={id} expects={scanKinds ? { kinds: scanKinds } : field.expects} value={String(value)} onChange={onChange} />
  } else if (plain && taskId && (field.widget === 'photo' || field.widget === 'file')) {
    control = (
      <UploadField
        common={common}
        id={id}
        taskId={taskId}
        accept={field.widget === 'photo' ? 'image/*' : undefined}
        capture={field.widget === 'photo'}
        onChange={onChange}
      />
    )
  } else if (field.widget === 'weight' && (field.type === 'number' || field.type === 'integer')) {
    control = (
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <Input
            {...common}
            data-field={field.name}
            type="number"
            inputMode="decimal"
            step="any"
            min={field.minimum}
            className="h-11 w-36 text-base"
            value={String(value)}
            onChange={(event) => onChange(event.target.value)}
          />
          <span className="text-sm text-muted-foreground">g</span>
        </div>
        {expect && <p className="text-[13px] font-medium">{expectedText(expect)}</p>}
      </div>
    )
  } else if (field.enum && field.type === 'string' && field.enum.length <= SEGMENTED_MAX) {
    control = (
      <Segmented
        labelledBy={`${id}-label`}
        options={field.enum.map(String)}
        value={String(value)}
        clearable={optional}
        invalid={Boolean(error)}
        describedBy={described || undefined}
        onChange={onChange}
      />
    )
  } else if (field.enum) {
    // With no default, nothing is chosen until the person chooses: never the first option shown chosen.
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
  } else if (field.type !== 'string' && field.minimum !== undefined && field.maximum !== undefined) {
    control = (
      <Stepper
        common={common}
        id={id}
        field={field.name}
        value={String(value)}
        min={field.minimum}
        max={field.maximum}
        integer={field.type === 'integer'}
        onChange={onChange}
      />
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
      <Label id={`${id}-label`} htmlFor={field.enum && field.type === 'string' && field.enum.length <= SEGMENTED_MAX ? undefined : id}>
        {label}
      </Label>
      {control}
      {hint}
      {problem}
    </div>
  )
}

const warned = new Set<string>()
function warnOnce(note: string) {
  if (warned.has(note)) return
  warned.add(note)
  console.warn(`${note}; a text field is drawn.`)
}
