import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import { sourceFilePath } from '../lib/deeplink'
import { MAX_SOURCE_FILES, newSourceFileProblem, sourceFileName } from '../lib/sourceFiles'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  /** The file the page has open. */
  current: string
  /** The model is one the user may write: "New file" is offered. */
  canEdit: boolean
  /** Shown at the right end of the row (the open file's own actions). */
  children?: ReactNode
}

/**
 * #1290 — a model's `.scad` files above its source editor: `model.scad` first, then
 * the files beside it that it can `include` or `use`. Each opens its own editor page,
 * so leaving one with unsaved edits asks first (lib/useLeaveGuard.ts). The list
 * follows the model's changes, so a file the agent adds shows up here.
 */
export function SourceFileTabs({ slug, current, canEdit, children }: Props) {
  // The model's revision is read before the list, so New file can send it as `base`:
  // PUT both creates and replaces, and a file of the same name added since (by the
  // agent, or another tab) is then a 409 rather than overwritten.
  const files = useAsync(
    async () => {
      const version = (await api.getModel(slug)).version ?? undefined
      return { list: await api.listSourceFiles(slug), version }
    },
    [slug],
    [`model:${slug}`],
  )
  const navigate = useNavigate()
  const [adding, setAdding] = useState(false)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const names = (files.data?.list ?? []).map((file) => file.name)
  // Nothing to switch between and nothing to add: no row at all.
  if (!files.data || (names.length < 2 && !canEdit && !children)) return null

  const name = sourceFileName(typed)
  const problem = name === '' ? null : newSourceFileProblem(name, names)
  const full = names.length >= MAX_SOURCE_FILES

  const close = () => {
    if (busy) return
    setAdding(false)
    setTyped('')
    setError(null)
  }

  async function create(event?: FormEvent) {
    event?.preventDefault()
    if (busy || name === '' || problem !== null) return
    setBusy(true)
    setError(null)
    try {
      await api.writeSourceFile(
        slug,
        name,
        `// ${name}: bring it into model.scad with include <${name}> or use <${name}>.\n`,
        files.data?.version,
      )
      files.refresh()
      setAdding(false)
      setTyped('')
      void navigate(sourceFilePath(slug, name))
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409 && caught.problem['current']) {
        // The model moved on since the list was read: nothing was written. Re-read it,
        // so a file of this name added meanwhile shows up and the name is refused.
        files.refresh()
        setError('This model was changed elsewhere since its files were listed, so nothing was created. Check the name and try again.')
      } else {
        setError(caught instanceof ApiError ? caught.detail : 'Could not create the file. Try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center gap-1 border-t border-line px-3 py-1 text-[12px]">
      <nav aria-label="Source files" className="flex min-w-0 items-center gap-1 overflow-x-auto">
        {names.map((file) => (
          <Link
            key={file}
            to={sourceFilePath(slug, file)}
            aria-current={file === current ? 'page' : undefined}
            className={`shrink-0 rounded-[6px] px-2 py-0.5 font-mono ${
              file === current ? 'bg-surface-2 text-ink' : 'text-muted hover:bg-surface-2 hover:text-ink'
            }`}
          >
            {file}
          </Link>
        ))}
      </nav>
      {canEdit && (
        <button
          type="button"
          onClick={() => setAdding(true)}
          disabled={full}
          title={full ? `A model holds at most ${MAX_SOURCE_FILES} .scad files.` : undefined}
          className="shrink-0 rounded-[6px] px-2 py-0.5 text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-50"
        >
          New file
        </button>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-2">{children}</div>

      <Dialog
        open={adding}
        title="New source file"
        description="A .scad file beside model.scad, which it can include or use. It is created now, as one revision."
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void create()} disabled={busy || name === '' || problem !== null}>
              {busy && <Spinner />}
              Create
            </Button>
          </>
        }
      >
        <form onSubmit={(event) => void create(event)}>
          <label className="flex flex-col gap-1 text-[13px] text-muted">
            File name
            <input
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              placeholder="parts.scad"
              aria-invalid={problem !== null}
              aria-describedby="new-source-file-hint"
              className="h-8 w-full rounded-[6px] border border-line bg-surface-2 px-2 font-mono text-[13px] outline-none focus:border-line-strong"
            />
          </label>
          <p id="new-source-file-hint" className={`mt-1 text-[12px] ${problem ? 'text-warn' : 'text-faint'}`}>
            {problem ?? (name !== '' && name !== typed.trim() ? `Saved as ${name}.` : '')}
          </p>
        </form>
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </div>
  )
}
