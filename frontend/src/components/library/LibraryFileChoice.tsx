import { useState } from 'react'
import { api } from '../../api/client'
import type { LibraryFileSuggestion, LibrarySearchHit } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { useDebounced } from '../../lib/useDebounced'
import { errorText } from '../prints/PrintSourceChoice'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** How many library files a search shows. */
const SEARCH_SHOWN = 8

/**
 * One library file, found by name (`GET /print/library/search`, as the Related dialog
 * searches): `suggested` files first, then the hits `keep` takes. Import print (#6394) keeps
 * ScadBuddy's own files that are no run's per-print copy; "Open a mesh as a model" (#6916)
 * keeps the files that hold a mesh.
 */
export function LibraryFileChoice({
  id,
  suggested = [],
  chosen,
  onChoose,
  keep,
  emptyHint,
  refreshKey = 0,
}: {
  /** The search field's id. */
  id: string
  suggested?: LibraryFileSuggestion[]
  chosen: LibraryFileSuggestion | null
  onChoose: (file: LibraryFileSuggestion) => void
  keep: (hit: LibrarySearchHit) => boolean
  /** What to say while nothing is chosen, suggested or searched for. */
  emptyHint: string
  /** A new value searches again: the library changed under the hits (#7335). */
  refreshKey?: number
}) {
  const [query, setQuery] = useState('')
  const q = useDebounced(query.trim(), 250)
  const found = useAsync(() => (q === '' ? Promise.resolve(null) : api.searchLibrary({ q, all: true })), [q, refreshKey])
  const hits = (found.data?.files ?? [])
    .filter(keep)
    .slice(0, SEARCH_SHOWN)
    .map((hit): LibraryFileSuggestion => ({ id: hit.id, filename: hit.filename, folder_id: hit.folder_id ?? null }))
  const choice = (file: LibraryFileSuggestion) => (
    <Button
      key={file.id}
      type="button"
      size="sm"
      variant={file.id === chosen?.id ? 'secondary' : 'outline'}
      aria-pressed={file.id === chosen?.id}
      className="max-w-full justify-start truncate"
      title={file.filename}
      onClick={() => onChoose(file)}
    >
      {file.filename}
    </Button>
  )
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {suggested.length > 0 && (
        <div className="flex flex-wrap gap-1" role="group" aria-label="Suggested library files">
          {suggested.map(choice)}
        </div>
      )}
      <Label htmlFor={id}>Find a file by name</Label>
      <Input id={id} type="search" value={query} maxLength={200} placeholder="benchy" onChange={(e) => setQuery(e.target.value)} />
      {q !== '' &&
        (found.error ? (
          <span className="text-xs text-warn">Could not search the library: {errorText(found.error)}</span>
        ) : !found.data ? (
          <span className="text-xs text-muted-foreground">Searching the library…</span>
        ) : hits.length === 0 ? (
          <span className="text-xs text-muted-foreground">No library file has that name.</span>
        ) : (
          <div className="flex flex-wrap gap-1" role="group" aria-label="Library files found">
            {hits.map(choice)}
          </div>
        ))}
      {chosen === null && suggested.length === 0 && q === '' && <span className="text-xs text-muted-foreground">{emptyHint}</span>}
    </div>
  )
}
