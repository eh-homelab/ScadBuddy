import { useEffect, useState, type FormEvent } from 'react'
import type { PropertyValue } from './api'
import { DefinedFields } from './DefinedFields'
import { fieldErrorsOf, propertiesPatch, refusalText, requiredOf, type FieldErrors } from './definedProperties'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

type Props = {
  /** The kind's schema; only its required keys are asked. */
  schema: Record<string, unknown>
  title: string
  action: string
  open: boolean
  onCancel: () => void
  /** Makes the row with these properties; a 422 `missing_required_properties` marks the fields and keeps the dialog open. */
  onSubmit: (properties: Record<string, PropertyValue>) => Promise<void>
}

/** #7703 — a one-click create asks first for the kind's required keys, the way its own form would. */
export function RequiredKeysDialog({ schema, title, action, open, onCancel, onSubmit }: Props) {
  const keys = requiredOf(schema)
  const asked = { ...schema, properties: Object.fromEntries(keys.map((key) => [key, (schema.properties as Record<string, unknown> | undefined)?.[key] ?? { type: 'string' }])), required: keys }
  const [values, setValues] = useState<Record<string, PropertyValue | undefined>>({})
  const [errors, setErrors] = useState<FieldErrors | undefined>()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Each opening starts empty: nothing typed or refused for the last row carries over.
  useEffect(() => {
    if (!open) return
    setValues({})
    setErrors(undefined)
    setError(null)
  }, [open])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setErrors(undefined)
    try {
      await onSubmit(propertiesPatch(asked, {}, values) as Record<string, PropertyValue>)
      setValues({})
    } catch (cause) {
      // A refusal may come wrapped in an error whose own message adds to it (#7952).
      const wrapped = !fieldErrorsOf(cause) && cause instanceof Error && fieldErrorsOf(cause.cause) !== null
      const refused = fieldErrorsOf(wrapped ? (cause as Error).cause : cause)
      setErrors(refused ?? undefined)
      if (refused) setError(wrapped ? `${refusalText(refused)} ${(cause as Error).message}` : refusalText(refused))
      else setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <form noValidate onSubmit={(event) => void submit(event)} className="flex flex-col gap-3 text-[13px]">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>Its kind requires these keys, so the row is made with them.</DialogDescription>
          </DialogHeader>
          <DefinedFields schema={asked} values={values} onChange={setValues} errors={errors} />
          {error && (
            <p role="alert" className="text-warn">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {action}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
