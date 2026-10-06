import { useId, useRef, useState, type FormEvent } from 'react'
import { Markdown } from '../agent/chat/Markdown'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import type { CustomizerSchema, ParamPreset, ParamValue } from '../api/types'
import { defaultValues, sameValues, type ParamValues } from '../lib/params'
import {
  applyPreset,
  parsePresetTags,
  presetDescriptionProblem,
  presetInputs,
  presetTagsProblem,
} from '../lib/presets'
import { splitInputs, type InputsExtra } from '../lib/inputs'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  schema: CustomizerSchema
  values: ParamValues
  /** The UI state saved with a preset (spec 2026-09-27 §4.3). */
  extra: InputsExtra
  /** Replaces every value on screen, and the UI state, as Reset to defaults does. */
  onApply: (values: ParamValues, extra: InputsExtra) => void
}

interface Selection {
  preset: ParamPreset
  /** The values the preset put on screen, so an edit since shows as a change to it. */
  applied: ParamValues
}

/** A value the selected preset stores for a parameter this template no longer has. */
interface Skipped {
  name: string
  value: ParamValue
}

function describeSkipped(skipped: readonly Skipped[]): string {
  return skipped.map(({ name, value }) => `${name} = ${JSON.stringify(value)}`).join(', ')
}

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

const FIELD =
  'w-full rounded-[6px] border border-line bg-surface-2 px-2 text-[13px] outline-none focus:border-line-strong'

/**
 * Named parameter sets for this template: the ones it ships with and the ones saved on
 * it, built-ins included. Picking one puts its values on screen over the defaults, and
 * from there only the value that differs this time — a name, a colour — needs changing.
 * Saving stores only what differs from the defaults.
 */
export function PresetPicker({ slug, schema, values, extra, onApply }: Props) {
  const presetsState = useAsync(() => api.listPresets(slug), [slug])
  const presets = presetsState.data ?? []
  const shipped = presets.filter((preset) => preset.origin === 'template')
  const saved = presets.filter((preset) => preset.origin === 'mine')

  const [selection, setSelection] = useState<Selection | null>(null)
  const [skipped, setSkipped] = useState<Skipped[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  /**
   * Which dialog is open: a save of the values on screen, a copy of a preset, or the
   * details (name, description, tags) of a saved one. Only a copy takes a name alone:
   * it keeps the original's details.
   */
  const [naming, setNaming] = useState<'save' | 'duplicate' | 'details' | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tagsText, setTagsText] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  /**
   * The field `nameError` is about, marked invalid and described by it: the description
   * or the tags when they are refused here, else the name, which only the server judges.
   */
  const [invalidField, setInvalidField] = useState<'name' | 'description' | 'tags' | null>(null)
  const descriptionInput = useRef<HTMLTextAreaElement>(null)
  const tagsInput = useRef<HTMLInputElement>(null)
  const dialogErrorId = useId()
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  /** #359 — the preset a pick would apply over unsaved edits, while it asks. */
  const [pending, setPending] = useState<ParamPreset | null>(null)
  /** #358 — an Update would drop the skipped values for good, so it asks first. */
  const [confirmingUpdate, setConfirmingUpdate] = useState(false)

  const selected = selection?.preset
  const modified = selection !== null && !sameValues(values, selection.applied)
  const editable = selected?.origin === 'mine'
  // Edits a pick would lose: values that are neither the selected preset's nor, with
  // none selected, the defaults.
  const unsaved = !sameValues(values, selection ? selection.applied : defaultValues(schema))

  /**
   * #359 — a pick replaces every value on screen, so with unsaved edits it asks first.
   * The select stays on the current preset meanwhile, so arrowing through the list
   * stops at the first preset instead of applying each one in turn.
   */
  function choose(id: string) {
    // While it asks, a further change (a key held down on the select) waits its turn.
    if (pending) return
    const preset = presets.find((candidate) => candidate.id === id)
    if (preset && unsaved) {
      setPending(preset)
      return
    }
    pick(preset)
  }

  /** Applies `preset`, or with none clears the selection and leaves the values alone. */
  function pick(preset: ParamPreset | undefined) {
    setError(null)
    if (!preset) {
      setSelection(null)
      setSkipped([])
      return
    }
    const applied = applyPreset(schema, preset)
    setSelection({ preset, applied: applied.values })
    const stored = splitInputs(preset.inputs, preset.params).params
    setSkipped(applied.skipped.map((name) => ({ name, value: stored[name] as ParamValue })))
    onApply(applied.values, applied.extra)
  }

  function openSaveAs() {
    setName(selected && modified ? `${selected.name} (variant)` : '')
    setDescription('')
    setTagsText('')
    setNameError(null)
    setInvalidField(null)
    setNaming('save')
  }

  function openDuplicate() {
    if (!selected) return
    setName(`${selected.name} copy`)
    setNameError(null)
    setInvalidField(null)
    setNaming('duplicate')
  }

  function openDetails() {
    if (!selected) return
    setName(selected.name)
    setDescription(selected.description)
    setTagsText(selected.tags.join(', '))
    setNameError(null)
    setInvalidField(null)
    setNaming('details')
  }

  /**
   * The details as typed, or null after saying why they cannot be sent. Given the
   * preset's current tags, the tags are left out when their text is as the dialog
   * opened it, so an edit of the name alone never rewrites them.
   */
  function details(current?: readonly string[]): { description: string; tags?: string[] } | null {
    const trimmed = description.trim()
    const descriptionProblem = presetDescriptionProblem(trimmed)
    if (descriptionProblem) {
      setNameError(descriptionProblem)
      setInvalidField('description')
      descriptionInput.current?.focus()
      return null
    }
    if (current && tagsText === current.join(', ')) return { description: trimmed }
    const tags = parsePresetTags(tagsText)
    const problem = presetTagsProblem(tags)
    if (problem) {
      setNameError(problem)
      setInvalidField('tags')
      tagsInput.current?.focus()
      return null
    }
    return { description: trimmed, tags }
  }

  /** The server's refusal, shown against the name: the field it alone judges. */
  function refused(caught: unknown) {
    setNameError(message(caught))
    setInvalidField('name')
  }

  function closeNaming() {
    if (!busy) setNaming(null)
  }

  async function saveAs(event?: FormEvent) {
    event?.preventDefault()
    const chosen = name.trim()
    if (!chosen || busy) return
    setNameError(null)
    const described = details()
    if (!described) return
    setBusy(true)
    try {
      const created = await api.createPreset(slug, {
        name: chosen,
        inputs: presetInputs(schema, values, extra),
        ...described,
      })
      presetsState.setData([...presets, created])
      setSelection({ preset: created, applied: values })
      setSkipped([])
      setNaming(null)
    } catch (caught) {
      refused(caught)
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
      refused(caught)
    } finally {
      setBusy(false)
    }
  }

  /**
   * Renames the selected saved preset and sets its description and tags. Its values
   * stay as they were, and so does the screen: an edit made since picking it still
   * shows as a change, for Update to save.
   */
  async function saveDetails(event?: FormEvent) {
    event?.preventDefault()
    const chosen = name.trim()
    if (!selected || selected.origin !== 'mine' || !chosen || busy) return
    setNameError(null)
    const described = details(selected.tags)
    if (!described) return
    setBusy(true)
    try {
      const updated = await api.updatePreset(slug, selected.id, { name: chosen, ...described })
      presetsState.setData(presets.map((preset) => (preset.id === updated.id ? updated : preset)))
      setSelection((current) => (current ? { ...current, preset: updated } : current))
      setNaming(null)
    } catch (caught) {
      refused(caught)
    } finally {
      setBusy(false)
    }
  }

  function submitNaming(event?: FormEvent) {
    if (naming === 'duplicate') void duplicate(event)
    else if (naming === 'details') void saveDetails(event)
    else void saveAs(event)
  }

  async function update(confirmed = false) {
    if (!selected || !editable || busy) return
    // The server refuses a parameter the template does not have, so the stored values
    // cannot be kept: Update replaces them, and says so before it does.
    if (skipped.length > 0 && !confirmed) {
      setConfirmingUpdate(true)
      return
    }
    setConfirmingUpdate(false)
    setBusy(true)
    setError(null)
    try {
      const updated = await api.updatePreset(slug, selected.id, {
        inputs: presetInputs(schema, values, extra),
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
          onChange={(event) => choose(event.target.value)}
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
        {/* #351 — always in the page, so "Changed from …" is announced as it appears. */}
        <span role="status" className="mr-auto text-[12px] text-faint">
          {modified && <span data-testid="preset-modified">Changed from {selected?.name}</span>}
        </span>
        {editable && modified && (
          <Button
            size="sm"
            onClick={() => void update()}
            disabled={busy}
            aria-label={`Update preset ${selected?.name ?? ''}`}
          >
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
            onClick={openDetails}
            disabled={busy}
            aria-label={`Edit details of preset ${selected?.name ?? ''}`}
          >
            Edit details
          </Button>
        )}
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

      {selected && (selected.description || selected.tags.length > 0) && (
        <div data-testid="preset-details" className="flex flex-col gap-1 text-[12px] text-muted">
          {selected.tags.length > 0 && (
            <ul aria-label="Preset tags" className="flex flex-wrap gap-1">
              {selected.tags.map((tag) => (
                <li
                  key={tag}
                  className="rounded-full border border-line bg-surface-2 px-2 py-px text-[11px]"
                >
                  {tag}
                </li>
              ))}
            </ul>
          )}
          {selected.description && <Markdown text={selected.description} />}
        </div>
      )}

      {skipped.length > 0 && (
        <p role="status" className="text-[12px] text-warn">
          Skipped {skipped.length === 1 ? 'a value' : `${skipped.length} values`} this template no
          longer has: {describeSkipped(skipped)}
        </p>
      )}
      {(error ?? presetsState.error) && (
        <p role="alert" className="text-[12px] text-warn">
          {error ?? `Could not load presets: ${presetsState.error?.message}`}
        </p>
      )}

      <Dialog
        open={naming !== null}
        title={
          naming === 'duplicate'
            ? `Duplicate ${selected?.name ?? 'preset'}`
            : naming === 'details'
              ? `Edit details of ${selected?.name ?? 'preset'}`
              : 'Save as preset'
        }
        description={
          naming === 'duplicate'
            ? `The copy is a saved preset of yours to change, with ${selected?.name ?? 'the original'}'s values, description and tags. The original stays as it is.`
            : naming === 'details'
              ? 'Its values stay as they are: Update saves the ones on screen.'
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
              onClick={() => submitNaming()}
              disabled={busy || !name.trim()}
            >
              {busy && <Spinner />}
              {naming === 'duplicate' ? 'Duplicate' : 'Save'}
            </Button>
          </>
        }
      >
        <form onSubmit={submitNaming} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1 text-[13px] text-muted">
            Preset name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={80}
              autoFocus
              aria-invalid={invalidField === 'name' || undefined}
              aria-describedby={invalidField === 'name' && nameError ? dialogErrorId : undefined}
              className={`h-8 ${FIELD}`}
            />
          </label>
          {naming !== 'duplicate' && (
            <>
              <label className="flex flex-col gap-1 text-[13px] text-muted">
                Description (optional, Markdown)
                <textarea
                  ref={descriptionInput}
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  rows={3}
                  aria-invalid={invalidField === 'description' || undefined}
                  aria-describedby={
                    invalidField === 'description' && nameError ? dialogErrorId : undefined
                  }
                  className={`py-1.5 ${FIELD}`}
                />
              </label>
              <label className="flex flex-col gap-1 text-[13px] text-muted">
                Tags (optional, comma-separated)
                <input
                  ref={tagsInput}
                  value={tagsText}
                  onChange={(event) => setTagsText(event.target.value)}
                  aria-invalid={invalidField === 'tags' || undefined}
                  aria-describedby={invalidField === 'tags' && nameError ? dialogErrorId : undefined}
                  placeholder="gift, small"
                  className={`h-8 ${FIELD}`}
                />
              </label>
            </>
          )}
        </form>
        {nameError && (
          <p id={dialogErrorId} role="alert" className="mt-3 text-[13px] text-warn">
            {nameError}
          </p>
        )}
      </Dialog>

      <Dialog
        open={pending !== null}
        title={`Apply preset ${pending?.name ?? ''}?`}
        description={
          selected
            ? `You changed ${selected.name} since you picked it.`
            : 'The values on screen have changes no preset holds.'
        }
        onClose={() => setPending(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              // The preset as asked about, not looked up again: it may have changed since.
              // Not USER_ONLY: replacing the values on screen stays in the page, so the
              // assistant may confirm a pick it made (spec §8.1).
              onClick={() => {
                pick(pending ?? undefined)
                setPending(null)
              }}
            >
              Replace my changes
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          Applying {pending?.name} replaces them. To keep them, cancel and{' '}
          {editable ? `update ${selected?.name} or ` : ''}save them as a preset first.
        </p>
      </Dialog>

      <Dialog
        open={confirmingUpdate}
        title={`Update preset ${selected?.name ?? ''}`}
        description="This template no longer has every parameter the preset sets."
        onClose={() => setConfirmingUpdate(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmingUpdate(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void update(true)} disabled={busy} {...USER_ONLY}>
              {busy && <Spinner />}
              Update and drop them
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          {selected?.name} also sets {describeSkipped(skipped)}, which this template no longer has.
          Updating drops them; note them first to re-enter them on the new parameters.
        </p>
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
