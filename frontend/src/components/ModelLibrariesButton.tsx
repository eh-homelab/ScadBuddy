import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { useLatest } from '../agent/useAgentHandlers'
import { ApiError, api } from '../api/client'
import { useSubscription, type RealtimeSignal } from '../lib/realtime'
import type { CatalogueLibrary, LibraryPinRequest, ModelLibrary, ModelSummary } from '../api/types'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  name: string
  /** Called once the pins have changed — the model now renders differently. */
  onSaved?: () => void
}

/** Runs one pin/unpin and hands back the model record it returns. */
type Apply = (action: () => Promise<ModelSummary>) => Promise<void>

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

/**
 * #93 — the third-party libraries this model renders with. Each is an upstream git
 * repository cloned at a tag or branch, its commit pinned in this model alone: only
 * these go on its OPENSCADPATH, so a `use <BOSL2/std.scad>` resolves once BOSL2 is
 * pinned here, and another model can pin another BOSL2.
 */
export function ModelLibrariesButton({ slug, name, onSaved }: Props) {
  const [pins, setPins] = useState<ModelLibrary[]>([])
  const [open, setOpen] = useState(false)
  // null until this dialog has read the model: its pins decide what the catalogue offers.
  const [catalogue, setCatalogue] = useState<CatalogueLibrary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(0)
  // Pins changed while the dialog was open. The schema is re-read on close rather than
  // after each change: re-reading it re-mounts the page, which would close the dialog
  // under the user between two pins.
  const dirty = useRef(false)
  const openRef = useRef(false)
  // Bumped by every read as it starts and every pin/unpin as it lands: only the
  // latest of them may set what the dialog shows. A read overtaken by a newer read,
  // or by a pin, would otherwise land late and put back what it replaced.
  const generation = useRef(0)

  useEffect(() => {
    let live = true
    const started = ++generation.current
    api
      .getModel(slug)
      .then((model) => {
        if (live && generation.current === started) setPins(model.libraries ?? [])
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [slug])

  async function show() {
    setOpen(true)
    openRef.current = true
    dirty.current = false
    setCatalogue(null)
    setError(null)
    const started = ++generation.current
    try {
      const [model, libraries] = await Promise.all([api.getModel(slug), api.listLibraries()])
      if (generation.current !== started) return
      setPins(model.libraries ?? [])
      setCatalogue(libraries)
    } catch (caught) {
      if (generation.current === started) setError(message(caught))
    }
  }

  // #269 — while open, pins and the catalogue follow changes made elsewhere (a clone
  // finishing, another tab or an agent pinning). Not mid-action: that answer is newer.
  const shown = useLatest(catalogue)
  const refreshOpen = async () => {
    if (!openRef.current || running > 0) return
    // This read supersedes the opening one when that has not answered yet, so it then
    // owes the dialog its answer or its error.
    const first = shown.current === null
    const started = ++generation.current
    try {
      const [model, libraries] = await Promise.all([api.getModel(slug), api.listLibraries()])
      if (generation.current !== started) return
      setPins(model.libraries ?? [])
      setCatalogue(libraries)
    } catch (caught) {
      // Otherwise the dialog keeps what it shows; the next change or reopen reads again.
      if (first && generation.current === started) setError(message(caught))
    }
  }
  // `libraries` alone: a pin change emits `library.changed` there as well as on
  // `model:<slug>` (backend `api/libraries.py`), and nothing else on that topic
  // touches what this dialog shows.
  useSubscription(open ? 'libraries' : undefined, (signal: RealtimeSignal) => {
    if (signal !== 'resync') void refreshOpen()
  })

  function close() {
    if (running > 0) return
    setOpen(false)
    openRef.current = false
    setCatalogue(null)
    if (dirty.current) {
      dirty.current = false
      onSaved?.()
    }
  }

  const apply: Apply = async (action) => {
    setRunning((n) => n + 1)
    try {
      const model = await action()
      generation.current += 1
      setPins(model.libraries ?? [])
      if (openRef.current) dirty.current = true
      else onSaved?.()
    } finally {
      setRunning((n) => n - 1)
    }
  }

  const pinned = new Set(pins.map((pin) => pin.name))
  const offered = catalogue?.filter((entry) => !pinned.has(entry.name)) ?? []

  return (
    <>
      <button
        type="button"
        onClick={() => void show()}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        Libraries
        {pins.length > 0 && <span className="sb-num ml-1.5 text-faint">{pins.length}</span>}
      </button>
      <Dialog
        open={open}
        title={`Libraries for ${name}`}
        description="Adding a library clones it at a tag or branch and pins the commit in this model's history. The model renders with only the libraries pinned here."
        onClose={close}
        footer={
          <Button variant="ghost" onClick={close} disabled={running > 0}>
            Done
          </Button>
        }
      >
        {error ? (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        ) : catalogue === null ? (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading libraries
          </p>
        ) : (
          <div className="space-y-5">
            <section>
              <h3 className="text-[13px] font-medium">Pinned</h3>
              {pins.length === 0 ? (
                <p className="mt-1 text-[12px] text-muted">
                  None yet. Add one from the catalogue or by URL.
                </p>
              ) : (
                <ul
                  aria-label="Pinned libraries"
                  className="mt-2 divide-y divide-line rounded-[6px] border border-line"
                >
                  {pins.map((pin) => (
                    <PinnedRow key={pin.name} slug={slug} pin={pin} apply={apply} />
                  ))}
                </ul>
              )}
            </section>

            {offered.length > 0 && (
              <section>
                <h3 className="text-[13px] font-medium">Add from catalogue</h3>
                <ul
                  aria-label="Catalogue"
                  className="mt-2 divide-y divide-line rounded-[6px] border border-line"
                >
                  {offered.map((entry) => (
                    <CatalogueRow key={entry.name} slug={slug} entry={entry} apply={apply} />
                  ))}
                </ul>
              </section>
            )}

            <AddByUrl slug={slug} apply={apply} />
          </div>
        )}
      </Dialog>
    </>
  )
}

/** Busy and error state for one row's actions, each row failing on its own. */
function useAction(apply: Apply) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function run(label: string, action: () => Promise<ModelSummary>): Promise<boolean> {
    setBusy(label)
    setError(null)
    try {
      await apply(action)
      return true
    } catch (caught) {
      setError(message(caught))
      return false
    } finally {
      setBusy(null)
    }
  }

  return { busy, error, run }
}

function RowError({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="mt-2 text-[12px] text-warn">
      {error}
    </p>
  ) : null
}

function RefInput({ value, onChange }: { value: string; onChange: (ref: string) => void }) {
  const id = useId()
  return (
    <>
      <label htmlFor={id} className="sr-only">
        Ref
      </label>
      <input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="sb-field sb-num h-7 w-28 text-[12px]"
      />
    </>
  )
}

function PinnedRow({ slug, pin, apply }: { slug: string; pin: ModelLibrary; apply: Apply }) {
  const [ref, setRef] = useState(pin.ref)
  const { busy, error, run } = useAction(apply)

  return (
    <li aria-label={pin.name} className="px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <span className="text-[13px] font-medium">{pin.name}</span>
          <p className="mt-0.5 truncate text-[12px] text-muted">
            Pinned to <span className="sb-num">{pin.ref}</span> at{' '}
            <span className="sb-num">{pin.commit.slice(0, 7)}</span>
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <RefInput value={ref} onChange={setRef} />
          <Button
            size="sm"
            onClick={() =>
              void run('update', () => api.pinModelLibrary(slug, pin.name, { url: pin.url, ref }))
            }
            disabled={busy !== null || !ref}
            aria-busy={busy === 'update'}
          >
            {busy === 'update' && <Spinner />}
            Update
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void run('remove', () => api.unpinModelLibrary(slug, pin.name))}
            disabled={busy !== null}
            aria-busy={busy === 'remove'}
          >
            {busy === 'remove' && <Spinner />}
            Remove
          </Button>
        </div>
      </div>
      <RowError error={error} />
    </li>
  )
}

function CatalogueRow({
  slug,
  entry,
  apply,
}: {
  slug: string
  entry: CatalogueLibrary
  apply: Apply
}) {
  const [ref, setRef] = useState(entry.ref)
  const { busy, error, run } = useAction(apply)

  return (
    <li aria-label={entry.name} className="px-3 py-2.5">
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
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <RefInput value={ref} onChange={setRef} />
          <Button
            size="sm"
            onClick={() =>
              void run('add', () => api.pinModelLibrary(slug, entry.name, { url: entry.url, ref }))
            }
            disabled={busy !== null || !ref}
            aria-busy={busy !== null}
          >
            {busy && <Spinner />}
            Add
          </Button>
        </div>
      </div>
      <RowError error={error} />
    </li>
  )
}

interface Draft extends LibraryPinRequest {
  name: string
}

function AddByUrl({ slug, apply }: { slug: string; apply: Apply }) {
  const [draft, setDraft] = useState<Draft>({ name: '', url: '', ref: '' })
  const { busy, error, run } = useAction(apply)
  const id = useId()

  async function submit(event: FormEvent) {
    event.preventDefault()
    const { name, url, ref } = draft
    if (await run('add', () => api.pinModelLibrary(slug, name, { url, ref }))) {
      setDraft({ name: '', url: '', ref: '' })
    }
  }

  const field = (key: keyof Draft, label: string, placeholder: string) => (
    <div>
      <label htmlFor={`${id}-${key}`} className="block text-[13px]">
        {label}
      </label>
      <input
        id={`${id}-${key}`}
        value={draft[key] ?? ''}
        placeholder={placeholder}
        onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
        className="sb-field sb-num mt-1.5"
      />
    </div>
  )

  return (
    <section className="rounded-[6px] border border-line">
      <h3 className="border-b border-line px-3 py-2 text-[13px] font-medium">Add by URL</h3>
      <form aria-label="Add by URL" onSubmit={(event) => void submit(event)} className="space-y-3 p-3">
        {field('name', 'Name', 'The directory a model uses, e.g. threads')}
        {field('url', 'Git URL', 'https://github.com/owner/repo.git')}
        {field('ref', 'Ref', 'A tag or branch')}
        <div className="flex items-center gap-2">
          <Button
            type="submit"
            variant="primary"
            disabled={busy !== null || !draft.name || !draft.url || !draft.ref}
            aria-busy={busy !== null}
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
