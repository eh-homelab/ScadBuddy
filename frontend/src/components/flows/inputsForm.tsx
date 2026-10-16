import { useId, useState } from 'react'
import { initialValues, toInputs, type InputField, type InputValues } from '../../lib/flowInputs'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

/** The form's values and errors, and the inputs once they parse. */
export function useInputs(fields: InputField[]) {
  const [values, setValues] = useState<InputValues>(() => initialValues(fields))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const parse = (): Record<string, unknown> | null => {
    const parsed = toInputs(fields, values)
    setErrors(parsed.errors)
    return Object.keys(parsed.errors).length === 0 ? parsed.inputs : null
  }
  const rendered = fields.map((field) => (
    <Field
      key={field.name}
      field={field}
      value={values[field.name] ?? ''}
      error={errors[field.name]}
      onChange={(next) => setValues((now) => ({ ...now, [field.name]: next }))}
    />
  ))
  return { parse, rendered }
}

/** An optional choice left unset: a select item cannot have the empty value. */
const NONE = '\u0000none'

function Field({
  field,
  value,
  error,
  onChange,
}: {
  field: InputField
  value: string | boolean
  error?: string
  onChange: (next: string | boolean) => void
}) {
  const id = useId()
  const help = `${id}-help`
  const described = [field.description || field.type === 'array' ? help : '', error ? `${id}-error` : '']
    .filter(Boolean)
    .join(' ')
  const common = {
    id,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': described || undefined,
    'aria-required': field.required && field.default === undefined ? true : undefined,
  }
  const label = (
    <>
      {field.label}
      {field.required && field.default === undefined && <span className="text-destructive"> *</span>}
      {field.label !== field.name && (
        <code className="ml-1.5 font-mono text-[11px] font-normal text-muted-foreground">{field.name}</code>
      )}
    </>
  )
  const hint = (field.description || field.type === 'array') && (
    <p id={help} className="text-[12px] text-muted-foreground">
      {field.description}
      {field.type === 'array' && `${field.description ? ' ' : ''}One per line.`}
    </p>
  )
  const problem = error && (
    <p id={`${id}-error`} className="text-[12px] text-destructive">
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
    control = <Input {...common} type="text" value={String(value)} onChange={(event) => onChange(event.target.value)} />
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
