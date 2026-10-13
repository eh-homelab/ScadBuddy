import { useId, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { api, ApiError } from '../../api/client'
import { initialValues, inputFields, toInputs, type InputField, type InputValues } from '../../lib/flowInputs'
import { flowRunPath } from '../../lib/flows'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

type Schema = Record<string, unknown> | null

/**
 * #2483 — Start run, on the Flows tab and the flow page. A flow with an inputs schema
 * asks for its inputs first, in a form drawn from the schema at its defaults; one
 * without starts at once, as before. The backend checks the inputs again and fills in
 * the defaults; a refusal is shown beside the form. Starting goes to the run.
 *
 * `schema` is the version's (`FlowDefinition.inputs_schema`); left out (the Flows tab,
 * whose list carries no schema), the version is read when the button is pressed.
 */
export function StartRun({
  flowId,
  name,
  schema,
  disabled = false,
  size = 'md',
  variant = 'primary',
}: {
  flowId: string
  name?: string
  schema?: Schema
  disabled?: boolean
  size?: 'sm' | 'md'
  variant?: 'primary' | 'default'
}) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [form, setForm] = useState<{ name: string; fields: InputField[] } | null>(null)

  async function start(inputs?: Record<string, unknown>) {
    setBusy(true)
    setError(undefined)
    try {
      const run = await api.startFlowRun(flowId, undefined, inputs ? { inputs } : {})
      void navigate(flowRunPath(run.id))
    } catch (caught) {
      setError(refusal(caught))
      setBusy(false)
    }
  }

  async function press() {
    let found = { name: name ?? '', schema }
    if (schema === undefined) {
      setBusy(true)
      setError(undefined)
      try {
        const flow = await api.getFlow(flowId)
        found = { name: flow.name, schema: flow.inputs_schema ?? null }
      } catch (caught) {
        setError(refusal(caught))
        setBusy(false)
        return
      }
      setBusy(false)
    }
    const fields = inputFields(found.schema)
    if (fields.length === 0) return void start()
    setForm({ name: found.name, fields })
  }

  return (
    <>
      <Button variant={variant} size={size} onClick={() => void press()} disabled={disabled || busy}>
        {busy && !form ? <Spinner /> : 'Start run'}
      </Button>
      {error && !form && (
        <p role="alert" className="text-[12.5px] text-warn">
          {error}
        </p>
      )}
      {form && (
        <InputsDialog
          name={form.name}
          fields={form.fields}
          busy={busy}
          error={error}
          onStart={(inputs) => void start(inputs)}
          onClose={() => {
            if (busy) return
            setForm(null)
            setError(undefined)
          }}
        />
      )}
    </>
  )
}

function refusal(caught: unknown): string {
  if (!(caught instanceof ApiError)) return String(caught)
  const problems = caught.problem['problems']
  // A 422 `flow-inputs` says which inputs do not fit; its detail says it too, so only one.
  return Array.isArray(problems) && problems.length > 0 ? problems.map(String).join('\n') : caught.detail
}

function InputsDialog({
  name,
  fields,
  busy,
  error,
  onStart,
  onClose,
}: {
  name: string
  fields: InputField[]
  busy: boolean
  error?: string
  onStart: (inputs: Record<string, unknown>) => void
  onClose: () => void
}) {
  const formId = useId()
  const [values, setValues] = useState<InputValues>(() => initialValues(fields))
  const [errors, setErrors] = useState<Record<string, string>>({})

  function submit(event: FormEvent) {
    event.preventDefault()
    const parsed = toInputs(fields, values)
    setErrors(parsed.errors)
    if (Object.keys(parsed.errors).length === 0) onStart(parsed.inputs)
  }

  return (
    <Dialog
      open
      title={`Start ${name}`}
      description="This flow takes inputs. Each starts at its default."
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form={formId} disabled={busy}>
            {busy ? <Spinner /> : 'Start run'}
          </Button>
        </>
      }
    >
      <form id={formId} noValidate onSubmit={submit} className="flex flex-col gap-3">
        {fields.map((field) => (
          <Field
            key={field.name}
            field={field}
            value={values[field.name] ?? ''}
            error={errors[field.name]}
            onChange={(next) => setValues((now) => ({ ...now, [field.name]: next }))}
          />
        ))}
        {error && (
          <p role="alert" className="whitespace-pre-wrap text-[13px] text-warn">
            {error}
          </p>
        )}
      </form>
    </Dialog>
  )
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
