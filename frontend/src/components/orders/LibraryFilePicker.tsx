import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { LibrarySearch, LibrarySearchHit } from '../../api/types'
import { useDebounced } from '../../lib/useDebounced'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** How long the search waits after the last key before it asks. */
const SEARCH_DEBOUNCE_MS = 250
/** How many files the list shows at once. */
const SHOWN = 8

/**
 * #5544 — the Add Item dialog's "Library file" source: the library searched as the Library
 * page and the agent's `search_library` search it (`GET /print/library/search`, every
 * folder, unsliced 3MFs), then one file picked. The order item prints that file.
 */
export function LibraryFilePicker({
  open,
  picked,
  disabled,
  onPick,
}: {
  open: boolean
  picked: LibrarySearchHit | null
  disabled: boolean
  onPick: (file: LibrarySearchHit | null) => void
}) {
  const [query, setQuery] = useState('')
  const asked = useDebounced(query.trim(), SEARCH_DEBOUNCE_MS)
  const [found, setFound] = useState<{ query: string; result: LibrarySearch } | null>(null)
  // Keyed to its query, as `found` is: a failed search's message shows only while that query is asked.
  const [error, setError] = useState<{ query: string; message: string } | null>(null)

  useEffect(() => {
    if (!open) setQuery('')
  }, [open])

  useEffect(() => {
    if (!open || picked || !asked) return
    const controller = new AbortController()
    api
      .searchLibrary({ q: asked, all: false }, controller.signal)
      .then((result) => {
        setFound({ query: asked, result })
        setError(null)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError({ query: asked, message: err instanceof Error ? err.message : String(err) })
      })
    return () => controller.abort()
  }, [open, picked, asked])

  if (picked) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border px-3 py-2" data-testid="add-item-library-file">
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">{picked.filename}</span>
          {picked.folder_path && picked.folder_path.length > 0 && (
            <span className="truncate text-xs text-muted-foreground">{picked.folder_path.join(' / ')}</span>
          )}
        </span>
        <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={() => onPick(null)}>
          Change
        </Button>
      </div>
    )
  }

  const current = found !== null && found.query === asked ? found.result : null
  const files = current?.files ?? []
  const failed = error !== null && error.query === asked ? error.message : null
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor="add-item-library-search">Library file</Label>
      <Input
        id="add-item-library-search"
        type="search"
        placeholder="Search the library"
        autoComplete="off"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {failed !== null ? (
        <p role="alert" className="text-[13px] text-warn">
          Could not search the library: {failed}
        </p>
      ) : !asked ? (
        <p className="text-[13px] text-muted-foreground">Type part of a file or folder name.</p>
      ) : current === null ? (
        <p className="text-[13px] text-muted-foreground">Searching…</p>
      ) : files.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No library file matches.</p>
      ) : (
        <ul aria-label="Library files" className="flex max-h-56 flex-col overflow-y-auto rounded-md border">
          {files.slice(0, SHOWN).map((file) => (
            <li key={file.id}>
              <button
                type="button"
                className="flex w-full flex-col items-start px-3 py-1.5 text-left text-sm hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                onClick={() => onPick(file)}
              >
                <span className="font-medium break-all">{file.filename}</span>
                <span className="text-xs text-muted-foreground">
                  {file.folder_path && file.folder_path.length > 0 ? file.folder_path.join(' / ') : 'Top level'}
                </span>
              </button>
            </li>
          ))}
          {(current?.total ?? files.length) > SHOWN && (
            <li className="px-3 py-1.5 text-xs text-muted-foreground">{(current?.total ?? files.length) - SHOWN} more; narrow the search.</li>
          )}
        </ul>
      )}
    </div>
  )
}
