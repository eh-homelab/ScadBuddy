import { useEffect, useId, useRef, type FormEvent, type ReactNode } from 'react'
import type { AnswerProblem } from '../../api/types'
import { inputFields, undrawnFields } from '../../lib/flowInputs'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'
import { useInputs } from './inputsForm'

/** Why the server refused the form. */
export function FormError({ error }: { error?: string }) {
  return error ? (
    <p role="alert" className="min-w-0 text-[13px] break-words whitespace-pre-wrap text-destructive">
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
  taskId,
  expect,
  actions,
  onSubmit,
}: {
  schema: unknown
  submitLabel: string
  busyLabel?: string
  busy: boolean
  problems?: readonly AnswerProblem[]
  error?: string
  answer?: boolean
  /** #8062 — the task a photo or file uploads to. */
  taskId?: string
  /** #8062 — the weight the task expects. */
  expect?: { weight_g: number; tolerance_g?: number }
  /** Puts the submit button elsewhere, after the form: a dialog's footer. */
  actions?: (submit: ReactNode) => ReactNode
  onSubmit: (values: Record<string, unknown>) => void
}) {
  const id = useId()
  const form = useRef<HTMLFormElement>(null)
  const fields = inputFields(schema)
  const { parse, rendered, errors, rest } = useInputs(fields, { answer, problems, taskId, expect })
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

  const button = (
    <Button type="submit" form={id} size={answer ? 'sm' : 'default'} disabled={busy} aria-busy={busy}>
      {busy && <Spinner />}
      {busy ? busyLabel : submitLabel}
    </Button>
  )
  return (
    <>
      <form ref={form} id={id} noValidate onSubmit={submit} className={cn('flex min-w-0 flex-col gap-4', answer && 'max-w-lg')}>
        {rendered}
        {undrawn.map((label, i) => (
          <div key={i} className="flex flex-col gap-1.5 text-sm">
            <p className="leading-none font-medium break-words">{label}</p>
            <p className="text-[12px] text-muted-foreground">This field cannot be answered here.</p>
          </div>
        ))}
        <FormError error={[...rest, ...(error ? [error] : [])].join('\n')} />
        {!actions && <div>{button}</div>}
      </form>
      {actions?.(button)}
    </>
  )
}
