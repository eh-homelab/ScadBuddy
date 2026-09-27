import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import type { Upstream } from '../api/types'
import { modelPath } from '../lib/deeplink'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

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
        <Button size="sm" variant="primary" onClick={show}>
          {label}
        </Button>
      ) : (
        <button
          type="button"
          onClick={show}
          className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
        >
          {label}
        </button>
      )}
      <Dialog
        open={open}
        title={`Duplicate ${name}`}
        description="The copy is a template of yours to edit. It remembers the one it came from."
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void duplicate()}
              disabled={busy || !copyName.trim()}
            >
              {busy && <Spinner />}
              Duplicate
            </Button>
          </>
        }
      >
        <form onSubmit={(event) => void duplicate(event)}>
          <label className="flex flex-col gap-1 text-[13px] text-muted">
            Name
            <input
              value={copyName}
              onChange={(event) => setCopyName(event.target.value)}
              className="h-8 w-full rounded-[6px] border border-line bg-surface-2 px-2 text-[13px] outline-none focus:border-line-strong"
            />
          </label>
        </form>
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
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
    <span data-testid="duplicated-from" className={`text-[12px] text-faint ${className}`}>
      Duplicated from{' '}
      <Link
        to={modelPath(upstream.id)}
        className="text-muted underline decoration-line-strong underline-offset-2 hover:text-ink"
      >
        {name ?? upstream.id}
      </Link>
    </span>
  )
}
