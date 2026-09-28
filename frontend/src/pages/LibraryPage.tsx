import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, LibraryListing } from '../api/types'
import { PrintPicker } from '../components/PrintPicker'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'

/** #313 — remembered per viewer: whether the page lists every file type. */
const ADVANCED_KEY = 'scadbuddy.library.advanced'

function readAdvanced(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_KEY) === '1'
  } catch {
    return false
  }
}

/**
 * #313 — Bambuddy's library by folder. Each unsliced 3MF has Print, which opens the same
 * spool-first dialog an output uses; the file is sliced as it stands in Bambuddy.
 * Advanced also lists sliced files and STLs; a sliced file is printed from Bambuddy.
 */
export function LibraryPage() {
  const [folderId, setFolderId] = useState<number | null>(null)
  const [advanced, setAdvanced] = useState(readAdvanced)
  const [listing, setListing] = useState<LibraryListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [printing, setPrinting] = useState<LibraryEntry | null>(null)

  useEffect(() => {
    let live = true
    setError(null)
    setListing(null)
    api
      .listLibrary({ folderId, all: advanced })
      .then((next) => live && setListing(next))
      .catch((cause: unknown) => {
        if (!live) return
        setListing(null)
        setError(cause instanceof ApiError ? cause.detail : 'Could not read the Bambuddy library.')
      })
    return () => {
      live = false
    }
  }, [folderId, advanced])

  function toggleAdvanced() {
    const next = !advanced
    setAdvanced(next)
    try {
      window.localStorage.setItem(ADVANCED_KEY, next ? '1' : '0')
    } catch {
      // A browser that refuses storage still toggles for this visit.
    }
  }

  const folders = listing?.folders ?? []
  const files = listing?.files ?? []
  const hidden = listing?.hidden ?? 0

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 md:flex-row md:p-6">
        <nav aria-label="Library folders" className="w-full space-y-1 text-[13px] md:w-56 md:shrink-0">
          <button
            type="button"
            data-testid="library-folder-root"
            onClick={() => setFolderId(null)}
            className={`block w-full rounded-[6px] px-2 py-1 text-left ${folderId === null ? 'bg-accent/8 text-ink' : 'text-muted'}`}
          >
            Top level
          </button>
          {folders.map((folder) => (
            <button
              key={folder.id}
              type="button"
              data-testid={`library-folder-${folder.id}`}
              onClick={() => setFolderId(folder.id)}
              style={{ paddingLeft: `${8 + (folder.depth ?? 0) * 12}px` }}
              className={`block w-full rounded-[6px] py-1 pr-2 text-left ${folderId === folder.id ? 'bg-accent/8 text-ink' : 'text-muted'}`}
            >
              {folder.name}
              {folder.file_count ? <span className="sb-num ml-1 text-faint">{folder.file_count}</span> : null}
            </button>
          ))}
        </nav>

        <section className="min-w-0 flex-1 space-y-4">
          <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h1 className="text-[15px] text-ink">Library</h1>
            <span id="library-advanced" className="ml-auto text-[13px] text-ink">
              Advanced
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={advanced}
              aria-labelledby="library-advanced"
              aria-describedby="library-advanced-help"
              data-testid="library-advanced"
              onClick={toggleAdvanced}
              className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors ${
                advanced ? 'border-accent bg-accent' : 'border-line-strong bg-surface-3'
              }`}
            >
              <span
                className={`absolute top-[2px] size-3.5 rounded-full transition-[left] ${
                  advanced ? 'left-[18px] bg-accent-ink' : 'left-[2px] bg-muted'
                }`}
              />
            </button>
            <span id="library-advanced-help" className="text-[12px] text-faint">
              List every file, sliced files and STLs too.
            </span>
          </header>

          {error && (
            <p role="alert" className="text-[13px] text-warn">
              {error}
            </p>
          )}
          {!listing && !error && (
            <p className="flex items-center gap-2 text-[13px] text-muted">
              <Spinner /> Reading the Bambuddy library
            </p>
          )}
          {listing && files.length === 0 && (
            <p className="text-[13px] text-muted">No files here{hidden > 0 ? '' : ' yet'}.</p>
          )}
          {!advanced && hidden > 0 && (
            <p className="sb-num text-[12px] text-faint">{hidden} more under Advanced.</p>
          )}

          <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(160px,100%),1fr))] gap-3">
            {files.map((file) => (
              <li
                key={file.id}
                data-testid={`library-file-${file.id}`}
                className="flex flex-col gap-2 rounded-[8px] border border-line bg-surface-2 p-2"
              >
                {file.has_thumbnail ? (
                  <img
                    src={api.libraryThumbnailUrl(file.id)}
                    alt=""
                    loading="lazy"
                    className="aspect-square w-full rounded-[4px] object-contain"
                  />
                ) : (
                  <div className="aspect-square w-full rounded-[4px] bg-surface-3" aria-hidden />
                )}
                <p className="truncate text-[13px] text-ink" title={file.filename}>
                  {file.filename}
                </p>
                {file.printable ? (
                  <Button variant="primary" data-testid={`library-print-${file.id}`} onClick={() => setPrinting(file)}>
                    Print
                  </Button>
                ) : (
                  <p className="text-[12px] text-faint">
                    {file.file_type?.toLowerCase() === 'gcode.3mf'
                      ? 'Sliced already. Print it from Bambuddy.'
                      : 'ScadBuddy cannot print this file type.'}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </section>

        <PrintPicker
          open={printing !== null}
          source={printing ? { kind: 'library', file: printing } : undefined}
          onClose={() => setPrinting(null)}
          onRan={() => undefined}
        />
      </div>
    </div>
  )
}
