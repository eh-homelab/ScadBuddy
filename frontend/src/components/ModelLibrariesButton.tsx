import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { ApiError, api } from '../api/client'
import type { LibraryEntry } from '../api/types'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  name: string
}

/**
 * #93 — which pinned libraries this model renders with. Only these go on its
 * OPENSCADPATH, so a `use <BOSL2/std.scad>` resolves once BOSL2 is ticked here.
 */
export function ModelLibrariesButton({ slug, name }: Props) {
  const [declared, setDeclared] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [available, setAvailable] = useState<LibraryEntry[] | null>(null)
  const [chosen, setChosen] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .getModel(slug)
      .then((model) => {
        if (live) setDeclared(model.libraries ?? [])
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [slug])

  async function show() {
    setOpen(true)
    setChosen(declared)
    setError(null)
    try {
      setAvailable((await api.listLibraries()).filter((entry) => entry.pin))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setAvailable([])
    }
  }

  function close() {
    if (saving) return
    setOpen(false)
    setAvailable(null)
  }

  function toggle(library: string) {
    setChosen((current) =>
      current.includes(library) ? current.filter((n) => n !== library) : [...current, library],
    )
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const model = await api.updateModel(slug, { libraries: chosen })
      setDeclared(model.libraries ?? [])
      setOpen(false)
      setAvailable(null)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void show()}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        Libraries
        {declared.length > 0 && <span className="sb-num ml-1.5 text-faint">{declared.length}</span>}
      </button>
      <Dialog
        open={open}
        title={`Libraries for ${name}`}
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={saving}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void save()}
              disabled={saving || available === null || available.length === 0}
            >
              {saving ? <Spinner /> : 'Save'}
            </Button>
          </>
        }
      >
        {available === null ? (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading libraries
          </p>
        ) : available.length === 0 ? (
          <p className="text-[13px] text-muted">
            No library has been added yet. Add one on the{' '}
            <Link to="/libraries" className="text-accent underline">
              Libraries page
            </Link>
            .
          </p>
        ) : (
          <ul className="space-y-2">
            {available.map((entry) => (
              <li key={entry.name}>
                <label className="flex cursor-pointer items-center gap-2 text-[13px]">
                  <input
                    type="checkbox"
                    checked={chosen.includes(entry.name)}
                    onChange={() => toggle(entry.name)}
                  />
                  {entry.name}
                  <span className="sb-num text-[12px] text-faint">{entry.pin?.ref}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </>
  )
}
