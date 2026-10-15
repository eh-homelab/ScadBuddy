import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import type { Upstream } from '../api/types'
import { modelPath } from '../lib/deeplink'
import { Spinner } from './ui/Spinner'
import { CopyPlusIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

interface Props {
  slug: string
  name: string
  /** "Duplicate to edit" on a built-in's source, which lands on the copy's source. */
  label?: string
  /** The page of the new template to open once it exists; its customizer by default. */
  landOn?: 'source'
  /** A primary button, where duplicating is the page's main action. */
  primary?: boolean
}

/**
 * #159 — copies any template, built-in or mine, to a new template of mine under a
 * name the user picks, then opens it. A taken or unusable name is the server's to
 * refuse (409, 422), and is shown in the dialog so it can be changed.
 */
export function DuplicateModelButton({
  slug,
  name,
  label = 'Duplicate',
  landOn,
  primary = false,
}: Props) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [copyName, setCopyName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function show() {
    setCopyName(`${name} copy`)
    setError(null)
    setOpen(true)
  }

  function close() {
    if (busy) return
    setOpen(false)
    setError(null)
  }

  async function duplicate(event?: FormEvent) {
    event?.preventDefault()
    const chosen = copyName.trim()
    if (!chosen || busy) return
    setBusy(true)
    setError(null)
    try {
      const copy = await api.duplicateModel(slug, chosen)
      setBusy(false)
      setOpen(false)
      await navigate(modelPath(copy.slug, landOn))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setBusy(false)
    }
  }

  return (
    <>
      {primary ? (
        <Button size="sm" onClick={show}>
          <CopyPlusIcon /> {label}
        </Button>
      ) : (
        <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={show}>
          {label}
        </Button>
      )}
      <Dialog open={open} onOpenChange={(next) => !next && close()}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{`Duplicate ${name}`}</DialogTitle>
            <DialogDescription>The copy is a template of yours to edit. It remembers the one it came from.</DialogDescription>
          </DialogHeader>
          <form id={`duplicate-${slug}`} className="grid gap-2" onSubmit={(event) => void duplicate(event)}>
            <Label htmlFor={`duplicate-${slug}-name`}>Name</Label>
            <Input id={`duplicate-${slug}-name`} value={copyName} onChange={(event) => setCopyName(event.target.value)} />
          </form>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" form={`duplicate-${slug}`} disabled={busy || !copyName.trim()}>
              {busy && <Spinner />}
              Duplicate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** "Duplicated from <template>", linked; nothing for a template with no upstream. */
export function DuplicatedFrom({
  upstream,
  name,
  className = '',
}: {
  upstream: Upstream | null | undefined
  /** The upstream's display name, when the caller has it; its id otherwise. */
  name?: string
  className?: string
}) {
  if (!upstream) return null
  return (
    <span data-testid="duplicated-from" className={cn('text-xs text-muted-foreground', className)}>
      Duplicated from{' '}
      <Link to={modelPath(upstream.id)} className="text-foreground/80 underline underline-offset-2 hover:text-foreground">
        {name ?? upstream.id}
      </Link>
    </span>
  )
}
