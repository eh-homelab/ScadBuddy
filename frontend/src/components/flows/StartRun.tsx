import { useId, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { FlowGrantSpec } from '../../api/types'
import { inputFields, type InputField } from '../../lib/flowInputs'
import { flowRunPath } from '../../lib/flows'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Spinner } from '../ui/spinner'
import { GrantEditor } from './GrantEditor'
import { useInputs } from './inputsForm'

type Schema = Record<string, unknown> | null
type Mode = 'inputs' | 'grant'

/**
 * #2483 — Start run, on the Flows tab and the flow page. A flow with an inputs schema
 * asks for its inputs first, in a form drawn from the schema at its defaults; one
 * without starts at once, as before. The backend checks the inputs again and fills in
 * the defaults; a refusal is shown beside the form. Starting goes to the run.
 *
 * #2481 — with `withGrant`, also "Start with a grant…": the grant editor, under the same
 * inputs form when the flow has one. The grant is approved as sent, so the button and
 * the editor are the user's own (`USER_ONLY`), never an assistant's.
 *
 * `schema` is the version's (`FlowDefinition.inputs_schema`); left out (the Flows tab,
 * whose list carries no schema), the version is read when a button is pressed.
 */
export function StartRun({
  flowId,
  name,
  schema,
  disabled = false,
  size = 'default',
  variant = 'default',
  withGrant = false,
}: {
  flowId: string
  name?: string
  schema?: Schema
  disabled?: boolean
  size?: 'sm' | 'default'
  variant?: 'default' | 'outline'
  withGrant?: boolean
}) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [form, setForm] = useState<{ mode: Mode; name: string; fields: InputField[] } | null>(null)

  async function start(options: { inputs?: Record<string, unknown>; grant?: FlowGrantSpec }) {
    setBusy(true)
    setError(undefined)
    try {
      const run = await api.startFlowRun(flowId, undefined, options)
      void navigate(flowRunPath(run.id))
    } catch (caught) {
      setError(refusal(caught))
      setBusy(false)
    }
  }

  async function press(mode: Mode) {
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
    if (mode === 'inputs' && fields.length === 0) return void start({})
    setForm({ mode, name: found.name, fields })
  }

  const close = () => {
    if (busy) return
    setForm(null)
    setError(undefined)
  }

  return (
    <>
      <Button
        variant={variant}
        size={size}
        onClick={() => void press('inputs')}
        disabled={disabled || busy}
        aria-busy={busy && !form}
      >
        {busy && !form && <Spinner />}
        Start run
      </Button>
      {withGrant && (
        <Button variant="outline" size={size} onClick={() => void press('grant')} disabled={disabled || busy} {...USER_ONLY}>
          Start with a grant…
        </Button>
      )}
      {error && !form && (
        <p role="alert" className="text-[12.5px] text-destructive">
          {error}
        </p>
      )}
      {form?.mode === 'inputs' && (
        <InputsDialog
          name={form.name}
          fields={form.fields}
          busy={busy}
          error={error}
          onStart={(inputs) => void start({ inputs })}
          onClose={close}
        />
      )}
      {form?.mode === 'grant' && (
        <GrantDialog
          fields={form.fields}
          busy={busy}
          error={error}
          onStart={(options) => void start(options)}
          onClose={close}
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

function Refusal({ error }: { error?: string }) {
  return error ? (
    <p role="alert" className="text-[13px] whitespace-pre-wrap text-destructive">
      {error}
    </p>
  ) : null
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
  const { parse, rendered } = useInputs(fields)

  function submit(event: FormEvent) {
    event.preventDefault()
    const inputs = parse()
    if (inputs) onStart(inputs)
  }

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{`Start ${name}`}</DialogTitle>
          <DialogDescription>
            This flow takes inputs. Each starts at its default; text you clear is sent empty, and a number or a choice
            left blank takes its default.
          </DialogDescription>
        </DialogHeader>
        <form id={formId} noValidate onSubmit={submit} className="flex flex-col gap-4">
          {rendered}
          <Refusal error={error} />
        </form>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={busy} aria-busy={busy}>
            {busy && <Spinner />}
            Start run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** #2481 with #2483: the run's inputs, when it takes any, then its grant; one press starts it with both. */
function GrantDialog({
  fields,
  busy,
  error,
  onStart,
  onClose,
}: {
  fields: InputField[]
  busy: boolean
  error?: string
  onStart: (options: { inputs?: Record<string, unknown>; grant: FlowGrantSpec }) => void
  onClose: () => void
}) {
  const { parse, rendered } = useInputs(fields)
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Start with a grant</DialogTitle>
          <DialogDescription>What the run&apos;s outward calls may do without asking you.</DialogDescription>
        </DialogHeader>
        {fields.length > 0 && (
          <section aria-label="Inputs" className="flex flex-col gap-4">
            <h3 className="text-[13px] font-medium">Inputs</h3>
            {rendered}
          </section>
        )}
        <GrantEditor
          submitLabel="Start run"
          busy={busy}
          onSubmit={(grant) => {
            if (fields.length === 0) return onStart({ grant })
            const inputs = parse()
            if (inputs) onStart({ inputs, grant })
          }}
          onCancel={onClose}
        />
        <Refusal error={error} />
      </DialogContent>
    </Dialog>
  )
}
