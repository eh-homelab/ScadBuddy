import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import { sourceFilePath } from '../lib/deeplink'
import { MAX_SOURCE_FILES, newSourceFileProblem, sourceFileName } from '../lib/sourceFiles'
import { useAsync } from '../lib/useAsync'
import { Hint } from './Hint'
import { Spinner } from './ui/spinner'
import { Button, buttonVariants } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

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
    <div className="flex items-center gap-1 border-t px-3 py-1 text-xs">
      {/* Links, as each file is a page of its own; drawn as line tabs (#3672). */}
      <nav aria-label="Source files" className="flex min-w-0 items-center gap-1 overflow-x-auto">
        {names.map((file) => (
          <Link
            key={file}
            to={sourceFilePath(slug, file)}
            aria-current={file === current ? 'page' : undefined}
            className={cn(
              buttonVariants({ variant: 'ghost', size: 'xs' }),
              'shrink-0 font-mono',
              file === current ? 'bg-muted text-foreground' : 'text-muted-foreground',
            )}
          >
            {file}
          </Link>
        ))}
      </nav>
      {canEdit && (
        <Hint content={full ? `A model holds at most ${MAX_SOURCE_FILES} .scad files.` : undefined} wrap={full}>
          <Button type="button" variant="ghost" size="xs" onClick={() => setAdding(true)} disabled={full} className="shrink-0">
            New file
          </Button>
        </Hint>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-2">{children}</div>

      <Dialog open={adding} onOpenChange={(open) => !open && close()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New source file</DialogTitle>
            <DialogDescription>
              A .scad file beside model.scad, which it can include or use. It is created now, as one revision.
            </DialogDescription>
          </DialogHeader>
          <form id="new-source-file-form" onSubmit={(event) => void create(event)} className="flex flex-col gap-2">
            <Label htmlFor="new-source-file-name">File name</Label>
            <Input
              id="new-source-file-name"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              placeholder="parts.scad"
              aria-invalid={problem !== null}
              aria-describedby="new-source-file-hint"
              className="font-mono"
            />
            <p id="new-source-file-hint" className={`text-xs ${problem ? 'text-destructive' : 'text-muted-foreground'}`}>
              {problem ?? (name !== '' && name !== typed.trim() ? `Saved as ${name}.` : '')}
            </p>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </form>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" form="new-source-file-form" disabled={busy || name === '' || problem !== null}>
              {busy && <Spinner />}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
