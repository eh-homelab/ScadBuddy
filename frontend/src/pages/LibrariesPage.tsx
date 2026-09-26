import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, api } from '../api/client'
import type { LibraryAdd, LibraryEntry } from '../api/types'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { useAsync } from '../lib/useAsync'

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

/**
 * #93 — third-party OpenSCAD libraries. Each is the upstream git repository at a
 * commit, cloned onto the server; the commit is pinned in `libraries.lock` in the
 * models repository, so it is versioned with the models that declare it.
 */
export function LibrariesPage() {
  const loaded = useAsync(() => api.listLibraries(), [])
  const [entries, setEntries] = useState<LibraryEntry[]>([])

  useEffect(() => {
    if (loaded.data) setEntries(loaded.data)
  }, [loaded.data])

  function pinned(entry: LibraryEntry) {
    setEntries((current) =>
      current.some((row) => row.name === entry.name)
        ? current.map((row) => (row.name === entry.name ? entry : row))
        : [...current, entry],
    )
  }

  if (loaded.loading) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading libraries
      </p>
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-2xl px-4 py-6">
        <h1 className="text-lg font-semibold tracking-tight">Libraries</h1>
        <p className="mt-0.5 text-[13px] text-muted">
          OpenSCAD libraries a model can <code>use</code> or <code>include</code>. Adding one
          clones it at a tag or branch and pins the commit in the models history; a model
          renders with only the libraries it declares.
        </p>

        {loaded.error && (
          <p role="alert" className="mt-4 text-[13px] text-warn">
            {loaded.error.message}
          </p>
        )}

        <ul className="mt-5 divide-y divide-line rounded-[6px] border border-line bg-surface">
          {entries.map((entry) => (
            <LibraryRow key={entry.name} entry={entry} onPinned={pinned} />
          ))}
        </ul>

        <AddByUrl onPinned={pinned} />
      </div>
    </div>
  )
}

function LibraryRow({
  entry,
  onPinned,
}: {
  entry: LibraryEntry
  onPinned: (entry: LibraryEntry) => void
}) {
  const [ref, setRef] = useState(entry.pin?.ref ?? entry.ref)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const refId = `ref-${entry.name}`

  async function pin() {
    setBusy(true)
    setError(null)
    try {
      onPinned(await api.addLibrary({ name: entry.name, url: entry.url, ref }))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <li aria-label={entry.name} className="px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          {entry.homepage ? (
            <a
              href={entry.homepage}
              target="_blank"
              rel="noreferrer"
              className="text-[13px] font-medium hover:text-accent"
            >
              {entry.name}
            </a>
          ) : (
            <span className="text-[13px] font-medium">{entry.name}</span>
          )}
          {entry.licence && <span className="ml-2 text-[11px] text-faint">{entry.licence}</span>}
          <p className="mt-0.5 truncate text-[12px] text-muted">
            {entry.pin ? (
              <>
                Pinned to <span className="sb-num">{entry.pin.ref}</span> at{' '}
                <span className="sb-num">{entry.pin.commit.slice(0, 7)}</span>
              </>
            ) : (
              'Not added'
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <label htmlFor={refId} className="sr-only">
            Ref
          </label>
          <input
            id={refId}
            value={ref}
            onChange={(event) => setRef(event.target.value)}
            className="sb-field sb-num h-7 w-32 text-[12px]"
          />
          <Button size="sm" onClick={() => void pin()} disabled={busy || !ref} aria-busy={busy}>
            {busy && <Spinner />}
            {entry.pin ? 'Update' : 'Add'}
          </Button>
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
    </li>
  )
}

function AddByUrl({ onPinned }: { onPinned: (entry: LibraryEntry) => void }) {
  const [draft, setDraft] = useState<LibraryAdd>({ name: '', url: '', ref: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onPinned(await api.addLibrary(draft))
      setDraft({ name: '', url: '', ref: '' })
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  const field = (key: keyof LibraryAdd, label: string, placeholder: string) => (
    <div>
      <label htmlFor={`add-${key}`} className="block text-[13px]">
        {label}
      </label>
      <input
        id={`add-${key}`}
        value={draft[key] ?? ''}
        placeholder={placeholder}
        onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
        className="sb-field sb-num mt-1.5"
      />
    </div>
  )

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Add by URL</h2>
      <form aria-label="Add by URL" onSubmit={(event) => void submit(event)} className="space-y-4 p-4">
        {field('name', 'Name', 'The directory a model uses, e.g. threads')}
        {field('url', 'Git URL', 'https://github.com/owner/repo.git')}
        {field('ref', 'Ref', 'A tag or branch')}
        <div className="flex items-center gap-2">
          <Button
            type="submit"
            variant="primary"
            disabled={busy || !draft.name || !draft.url || !draft.ref}
            aria-busy={busy}
          >
            {busy && <Spinner />}
            Add
          </Button>
          {error && (
            <p role="alert" className="text-[12px] text-warn">
              {error}
            </p>
          )}
        </div>
      </form>
    </section>
  )
}
