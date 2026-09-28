import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import type { CustomizerSchema, ParamPreset } from '../api/types'
import { sameValues, type ParamValues } from '../lib/params'
import { applyPreset, presetParams } from '../lib/presets'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  schema: CustomizerSchema
  values: ParamValues
  /** Replaces every value on screen, as Reset to defaults does. */
  onApply: (values: ParamValues) => void
}

interface Selection {
  preset: ParamPreset
  /** The values the preset put on screen, so an edit since shows as a change to it. */
  applied: ParamValues
}

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

/**
 * Named parameter sets for this template: the ones it ships with and the ones saved on
 * it, built-ins included. Picking one puts its values on screen over the defaults, and
 * from there only the value that differs this time — a name, a colour — needs changing.
 * Saving stores only what differs from the defaults.
 */
export function PresetPicker({ slug, schema, values, onApply }: Props) {
  const presetsState = useAsync(() => api.listPresets(slug), [slug])
  const presets = presetsState.data ?? []
  const shipped = presets.filter((preset) => preset.origin === 'template')
  const saved = presets.filter((preset) => preset.origin === 'mine')

  const [selection, setSelection] = useState<Selection | null>(null)
  const [skipped, setSkipped] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  /** Which dialog asks for a name: a save of the values on screen, or a copy of a preset. */
  const [naming, setNaming] = useState<'save' | 'duplicate' | null>(null)
  const [name, setName] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const selected = selection?.preset
  const modified = selection !== null && !sameValues(values, selection.applied)
  const editable = selected?.origin === 'mine'

  function pick(id: string) {
    setError(null)
    const preset = presets.find((candidate) => candidate.id === id)
    if (!preset) {
      setSelection(null)
      setSkipped([])
      return
    }
    const applied = applyPreset(schema, preset)
    setSelection({ preset, applied: applied.values })
    setSkipped(applied.skipped)
    onApply(applied.values)
  }

  function openSaveAs() {
    setName(selected && modified ? `${selected.name} (variant)` : '')
    setNameError(null)
    setNaming('save')
  }

  function openDuplicate() {
    if (!selected) return
    setName(`${selected.name} copy`)
    setNameError(null)
    setNaming('duplicate')
  }

  function closeNaming() {
    if (!busy) setNaming(null)
  }

  async function saveAs(event?: FormEvent) {
    event?.preventDefault()
    const chosen = name.trim()
    if (!chosen || busy) return
    setBusy(true)
    setNameError(null)
    try {
      const created = await api.createPreset(slug, {
        name: chosen,
        params: presetParams(schema, values),
      })
      presetsState.setData([...presets, created])
      setSelection({ preset: created, applied: values })
      setSkipped([])
      setNaming(null)
    } catch (caught) {
      setNameError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  /**
   * Copies the selected preset -- the way to change one the template ships, which is
   * read-only. The copy holds the original's values, not what is on screen, and the
   * screen is left alone: an edit made since picking the original shows as a change
   * to the copy, which Update then saves into it.
   */
  async function duplicate(event?: FormEvent) {
    event?.preventDefault()
    const chosen = name.trim()
    if (!selected || !chosen || busy) return
    setBusy(true)
    setNameError(null)
    try {
      const copy = await api.duplicatePreset(slug, selected.id, { name: chosen })
      presetsState.setData([...presets, copy])
      setSelection({ preset: copy, applied: applyPreset(schema, copy).values })
      setNaming(null)
    } catch (caught) {
      setNameError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  async function update() {
    if (!selected || !editable || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.updatePreset(slug, selected.id, {
        params: presetParams(schema, values),
      })
      presetsState.setData(presets.map((preset) => (preset.id === updated.id ? updated : preset)))
      setSelection({ preset: updated, applied: values })
      setSkipped([])
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (!selected || !editable || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.deletePreset(slug, selected.id)
      presetsState.setData(presets.filter((preset) => preset.id !== selected.id))
      setSelection(null)
      setSkipped([])
      setConfirmingDelete(false)
    } catch (caught) {
      setError(message(caught))
      setConfirmingDelete(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-testid="preset-picker" className="flex flex-col gap-1.5 border-b border-line px-3 py-2">
      <div className="flex items-center gap-2">
        <label htmlFor="preset-select" className="shrink-0 text-[12px] text-muted">
          Preset
        </label>
        <select
          id="preset-select"
          value={selected?.id ?? ''}
          onChange={(event) => pick(event.target.value)}
          disabled={presetsState.loading && !presetsState.data}
          className="sb-field min-w-0 flex-1 cursor-pointer"
        >
          <option value="">
            {presets.length === 0 ? 'No presets yet' : 'Choose a preset…'}
          </option>
          {shipped.length > 0 && (
            <optgroup label="From the template">
              {shipped.map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.name}
                </option>
              ))}
            </optgroup>
          )}
          {saved.length > 0 && (
            <optgroup label="Saved">
              {saved.map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.name}
                </option>
              ))}
            </optgroup>
          )}
        </select>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-1">
        {modified && (
          <span data-testid="preset-modified" className="mr-auto text-[12px] text-faint">
            Changed from {selected?.name}
          </span>
        )}
        {editable && modified && (
          <Button size="sm" onClick={() => void update()} disabled={busy}>
            Update
          </Button>
        )}
        {selected && (
          <Button
            size="sm"
            onClick={openDuplicate}
            disabled={busy}
            aria-label={`Duplicate preset ${selected.name}`}
          >
            Duplicate
          </Button>
        )}
        <Button size="sm" onClick={openSaveAs} disabled={busy}>
          Save as preset…
        </Button>
        {editable && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setConfirmingDelete(true)}
            disabled={busy}
            aria-label={`Delete preset ${selected?.name ?? ''}`}
          >
            Delete
          </Button>
        )}
      </div>

      {skipped.length > 0 && (
        <p role="status" className="text-[12px] text-warn">
          Skipped {skipped.length === 1 ? 'a value' : `${skipped.length} values`} this template no
          longer has: {skipped.join(', ')}
        </p>
      )}
      {(error ?? presetsState.error) && (
        <p role="alert" className="text-[12px] text-warn">
          {error ?? `Could not load presets: ${presetsState.error?.message}`}
        </p>
      )}

      <Dialog
        open={naming !== null}
        title={naming === 'duplicate' ? `Duplicate ${selected?.name ?? 'preset'}` : 'Save as preset'}
        description={
          naming === 'duplicate'
            ? `The copy is a saved preset of yours to change, with ${selected?.name ?? 'the original'}'s values. The original stays as it is.`
            : "Saves the values that differ from the template's defaults, under a name to pick them by next time."
        }
        onClose={closeNaming}
        footer={
          <>
            <Button variant="ghost" onClick={closeNaming} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void (naming === 'duplicate' ? duplicate() : saveAs())}
              disabled={busy || !name.trim()}
            >
              {busy && <Spinner />}
              {naming === 'duplicate' ? 'Duplicate' : 'Save'}
            </Button>
          </>
        }
      >
        <form
          onSubmit={(event) => void (naming === 'duplicate' ? duplicate(event) : saveAs(event))}
        >
          <label className="flex flex-col gap-1 text-[13px] text-muted">
            Preset name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={80}
              autoFocus
              className="h-8 w-full rounded-[6px] border border-line bg-surface-2 px-2 text-[13px] outline-none focus:border-line-strong"
            />
          </label>
        </form>
        {nameError && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {nameError}
          </p>
        )}
      </Dialog>

      <Dialog
        open={confirmingDelete}
        title={`Delete preset ${selected?.name ?? ''}`}
        description="The values on screen stay as they are."
        onClose={() => setConfirmingDelete(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmingDelete(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void remove()} disabled={busy} {...USER_ONLY}>
              {busy && <Spinner />}
              Delete preset
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">This cannot be undone.</p>
      </Dialog>
    </div>
  )
}
