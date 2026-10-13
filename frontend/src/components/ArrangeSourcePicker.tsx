import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, LibraryListing, ModelSummary, Output } from '../api/types'
import { needsBackfill } from '../lib/arrange'
import { formatBbox, timeAgo } from '../lib/format'
import { useDisplayUnit } from '../lib/units'
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
  keyOf,
  chosen,
  onAdd,
  onCancel,
  what,
}: Props<T> & {
  items: T[]
  label: (item: T) => string
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
          what="printable files"
        />
      )}
    </fieldset>
  )
}

/** #2285 — models by name, one alphabetical run whatever case a name starts with. */
function byName(models: ModelSummary[]): ModelSummary[] {
  const name = (model: ModelSummary) => model.name ?? model.slug
  return [...models].sort((a, b) => name(a).localeCompare(name(b), undefined, { sensitivity: 'base' }))
}

/** #1864 — Arrange's "Add from a model": a template, then any of its saved outputs. */
export function AddFromModel(props: Props<Output>) {
  const [models, setModels] = useState<ModelSummary[] | null>(null)
  const [slug, setSlug] = useState('')
  /** #2285 — narrows the model list, which runs to ~100 entries. */
  const [search, setSearch] = useState('')
  const unit = useDisplayUnit()
  const wanted = search.trim().toLowerCase()
  const shown = byName(models ?? []).filter(
    (model) => !wanted || (model.name ?? model.slug).toLowerCase().includes(wanted) || model.slug === slug,
  )
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
      <input
        type="search"
        aria-label="Find a model"
        placeholder="Find a model"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        className="sb-field"
      />
      <select aria-label="Model" value={slug} onChange={(event) => setSlug(event.target.value)} className="sb-field">
        <option value="" disabled>
          {models ? (shown.length > 0 ? 'Choose a model' : 'No model matches') : 'Reading the models…'}
        </option>
        {shown.map((model) => (
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
          // #2285 — when and how big, so two outputs of one name can be told apart.
          label={(output) =>
            [
              output.name ?? output.id.slice(0, 8),
              timeAgo(output.created_at),
              ...(output.bbox_mm ? [formatBbox(output.bbox_mm, unit)] : []),
              ...(needsBackfill(output) ? ['needs a re-render'] : []),
            ].join(' · ')
          }
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
