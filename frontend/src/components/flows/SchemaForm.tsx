import { useEffect, useRef, type FormEvent, type ReactNode } from 'react'
import type { AnswerProblem } from '../../api/types'
import { inputFields, noErrors, undrawnFields } from '../../lib/flowInputs'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'
import { useInputs } from './inputsForm'

/** Each problem by the field it is shown under, and the rest, which are shown above the button. */
function placed(names: ReadonlySet<string>, problems: readonly AnswerProblem[]) {
  const fields = noErrors()
  const rest: string[] = []
  for (const { field, message } of problems) {
    const [name = '', item] = field.split('/')
    if (!names.has(name)) {
      rest.push(field ? `${field}: ${message}` : message)
      continue
    }
    const said = item === undefined ? message : `Item ${Number(item) + 1}: ${message}`
    fields[name] = fields[name] ? `${fields[name]}; ${said}` : said
  }
  return { fields, rest }
}

/** Why the server refused the form. */
export function FormError({ error }: { error?: string }) {
  return error ? (
    <p role="alert" className="text-[13px] whitespace-pre-wrap text-destructive">
      {error}
    </p>
  ) : null
}

/**
 * #7364 — a form drawn from a JSON schema (`lib/flowInputs.ts`): a flow's inputs, and with
 * `answer` a prompt's answer. `problems` are what the server said of the values sent;
 * `error` is why it refused them as a whole. What the person typed stays through both.
 */
export function SchemaForm({
  schema,
  submitLabel,
  busyLabel = submitLabel,
  busy,
  problems,
  error,
  answer = false,
  actions = (submit) => <div>{submit}</div>,
  onSubmit,
}: {
  schema: unknown
  submitLabel: string
  busyLabel?: string
  busy: boolean
  problems?: readonly AnswerProblem[]
  error?: string
  answer?: boolean
  /** Where the submit button goes. */
  actions?: (submit: ReactNode) => ReactNode
  onSubmit: (values: Record<string, unknown>) => void
}) {
  const form = useRef<HTMLFormElement>(null)
  const fields = inputFields(schema)
  const said = placed(new Set(fields.map((field) => field.name)), problems ?? [])
  const { parse, rendered, errors } = useInputs(fields, { answer, problems: said.fields })
  const undrawn = answer ? undrawnFields(schema) : []

  // An answer that does not fit moves to its first bad field, whoever found it.
  useEffect(() => {
    if (answer) form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
  }, [answer, errors, problems])

  function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    const inputs = form.current?.querySelectorAll<HTMLInputElement>('input[data-field]') ?? []
    const unread = answer ? Array.from(inputs).filter((input) => input.validity.badInput) : []
    const values = parse(unread.map((input) => input.dataset['field']!))
    if (values) onSubmit(values)
  }

  return (
    <form ref={form} noValidate onSubmit={submit} className={cn('flex flex-col gap-4', answer && 'max-w-lg')}>
      {rendered}
      {undrawn.map((label, i) => (
        <div key={i} className="flex flex-col gap-1.5 text-sm">
          <p className="leading-none font-medium break-words">{label}</p>
          <p className="text-[12px] text-muted-foreground">This field cannot be answered here.</p>
        </div>
      ))}
      <FormError error={[...said.rest, ...(error ? [error] : [])].join('\n')} />
      {actions(
        <Button type="submit" size={answer ? 'sm' : 'default'} disabled={busy} aria-busy={busy}>
          {busy && <Spinner />}
          {busy ? busyLabel : submitLabel}
        </Button>,
      )}
    </form>
  )
}
