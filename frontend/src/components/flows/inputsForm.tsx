import { useId, useState } from 'react'
import { initialValues, toInputs, type InputField, type InputValues } from '../../lib/flowInputs'

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

const CONTROL =
  'w-full rounded-[6px] border border-line bg-surface px-2 py-1.5 text-[13px] text-ink aria-[invalid=true]:border-warn'

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
      {field.required && field.default === undefined && <span className="text-warn"> *</span>}
      {field.label !== field.name && <code className="ml-1.5 font-mono text-[11px] text-faint">{field.name}</code>}
    </>
  )
  const hint = (field.description || field.type === 'array') && (
    <p id={help} className="text-[12px] text-muted">
      {field.description}
      {field.type === 'array' && `${field.description ? ' ' : ''}One per line.`}
    </p>
  )
  const problem = error && (
    <p id={`${id}-error`} className="text-[12px] text-warn">
      {error}
    </p>
  )

  if (field.type === 'boolean') {
    return (
      <div className="flex flex-col gap-0.5">
        <label htmlFor={id} className="flex w-fit items-center gap-2 text-[13px]">
          <input {...common} type="checkbox" checked={value === true} onChange={(event) => onChange(event.target.checked)} />
          {label}
        </label>
        {hint}
        {problem}
      </div>
    )
  }

  let control
  if (field.enum) {
    control = (
      <select {...common} className={CONTROL} value={String(value)} onChange={(event) => onChange(event.target.value)}>
        {/* With no default, nothing is chosen until the person chooses: never the first option shown chosen. */}
        {field.default === undefined &&
          (field.required ? (
            <option value="" disabled>
              Choose…
            </option>
          ) : (
            <option value="">(none)</option>
          ))}
        {field.enum.map((option) => (
          <option key={String(option)} value={String(option)}>
            {String(option)}
          </option>
        ))}
      </select>
    )
  } else if (field.type === 'array') {
    control = (
      <textarea
        {...common}
        rows={3}
        className={`${CONTROL} font-mono`}
        value={String(value)}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  } else if (field.type === 'integer' || field.type === 'number') {
    control = (
      <input
        {...common}
        type="number"
        inputMode={field.type === 'integer' ? 'numeric' : 'decimal'}
        step={field.type === 'integer' ? 1 : 'any'}
        min={field.minimum}
        max={field.maximum}
        className={CONTROL}
        value={String(value)}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  } else {
    control = (
      <input {...common} type="text" className={CONTROL} value={String(value)} onChange={(event) => onChange(event.target.value)} />
    )
  }
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[13px] font-medium">
        {label}
      </label>
      {control}
      {hint}
      {problem}
    </div>
  )
}
