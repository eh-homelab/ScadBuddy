import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, LibraryListing, ModelSummary, Output } from '../api/types'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

type Props<T> = {
  /** Already in the arrange: shown ticked and not offered again. */
  chosen: (item: T) => boolean
  onAdd: (items: T[]) => void
  onCancel: () => void
}

function failure(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

/** One tick per item, and Add for the ticked ones. */
function PickList<T>({
  items,
  label,
  note,
  keyOf,
  chosen,
  onAdd,
  onCancel,
  what,
}: Props<T> & {
  items: T[]
  label: (item: T) => string
  note?: (item: T) => string | null
  keyOf: (item: T) => string
  what: string
}) {
  const [ticked, setTicked] = useState<string[]>([])
  const picked = items.filter((item) => ticked.includes(keyOf(item)))
  return (
    <>
      {items.length === 0 ? (
        <p className="text-[12px] text-muted">No {what} here.</p>
      ) : (
        <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto">
          {items.map((item) => {
            const key = keyOf(item)
            const already = chosen(item)
            const extra = note?.(item)
            return (
              <li key={key} className="text-[13px]">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={already || ticked.includes(key)}
                    disabled={already}
                    onChange={() =>
                      setTicked((now) => (now.includes(key) ? now.filter((k) => k !== key) : [...now, key]))
                    }
                  />
                  <span className="min-w-0 truncate">{label(item)}</span>
                  {extra && <span className="text-[11px] text-faint">{extra}</span>}
                </label>
              </li>
            )
          })}
        </ul>
      )}
      <div className="flex justify-end gap-2">
        <Button size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" variant="primary" disabled={picked.length === 0} onClick={() => onAdd(picked)}>
          Add ({picked.length})
        </Button>
      </div>
    </>
  )
}

/** #1864 — Arrange's "Add files": printable files of one library folder at a time. */
export function AddFiles(props: Props<LibraryEntry>) {
  const [folderId, setFolderId] = useState<number | null>(null)
  const [listing, setListing] = useState<LibraryListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setListing(null)
    setError(null)
    api
      .listLibrary({ folderId, all: false })
      .then((next) => live && setListing(next))
      .catch((cause: unknown) => live && setError(failure(cause, 'Could not read the Bambuddy library.')))
    return () => {
      live = false
    }
  }, [folderId])
  return (
    <fieldset className="flex flex-col gap-2 rounded-[6px] border border-line p-2">
      <legend className="px-1 text-[12px] text-muted">Add files from the library</legend>
      <select
        aria-label="Library folder"
        value={folderId ?? ''}
        onChange={(event) => setFolderId(event.target.value === '' ? null : Number(event.target.value))}
        className="sb-field"
      >
        <option value="">Top level</option>
        {(listing?.folders ?? []).map((folder) => (
          <option key={folder.id} value={folder.id}>
            {`${'  '.repeat(folder.depth ?? 0)}${folder.name}`}
          </option>
        ))}
      </select>
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
      {!listing && !error && (
        <p className="flex items-center gap-2 text-[12px] text-muted">
          <Spinner /> Reading the library
        </p>
      )}
      {listing && (
        <PickList
          {...props}
          key={folderId ?? 'top'}
          items={(listing.files ?? []).filter((file) => file.printable)}
          keyOf={(file) => String(file.id)}
          label={(file) => file.filename}
          note={(file) => (file.output_id ? null : 'not made by ScadBuddy')}
          what="printable files"
        />
      )}
    </fieldset>
  )
}

/** #1864 — Arrange's "Add from a model": a template, then any of its saved outputs. */
export function AddFromModel(props: Props<Output>) {
  const [models, setModels] = useState<ModelSummary[] | null>(null)
  const [slug, setSlug] = useState('')
  const [outputs, setOutputs] = useState<Output[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    api
      .listModels()
      .then((next) => live && setModels(next))
      .catch((cause: unknown) => live && setError(failure(cause, 'Could not read the models.')))
    return () => {
      live = false
    }
  }, [])
  useEffect(() => {
    if (!slug) return
    let live = true
    setOutputs(null)
    setError(null)
    api
      .listOutputs(slug)
      .then((next) => live && setOutputs(next))
      .catch((cause: unknown) => live && setError(failure(cause, 'Could not read the outputs.')))
    return () => {
      live = false
    }
  }, [slug])
  return (
    <fieldset className="flex flex-col gap-2 rounded-[6px] border border-line p-2">
      <legend className="px-1 text-[12px] text-muted">Add outputs of a model</legend>
      <select aria-label="Model" value={slug} onChange={(event) => setSlug(event.target.value)} className="sb-field">
        <option value="" disabled>
          {models ? 'Choose a model' : 'Reading the models…'}
        </option>
        {(models ?? []).map((model) => (
          <option key={model.slug} value={model.slug}>
            {model.name ?? model.slug}
          </option>
        ))}
      </select>
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
      {slug && !outputs && !error && (
        <p className="flex items-center gap-2 text-[12px] text-muted">
          <Spinner /> Reading its outputs
        </p>
      )}
      {slug && outputs && (
        <PickList
          {...props}
          key={slug}
          items={outputs}
          keyOf={(output) => output.id}
          label={(output) => output.name ?? output.id.slice(0, 8)}
          what="saved outputs"
        />
      )}
      {!slug && (
        <div className="flex justify-end">
          <Button size="sm" onClick={props.onCancel}>
            Cancel
          </Button>
        </div>
      )}
    </fieldset>
  )
}
