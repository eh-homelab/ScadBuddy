import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Markdown } from '../agent/chat/Markdown'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import type { CustomizerSchema, ParamPreset, ParamValue } from '../api/types'
import { defaultValues, sameValues, type ParamValues } from '../lib/params'
import {
  isJsonObject,
  joinInputs,
  NO_EXTRA,
  sameJson,
  splitInputs,
  type InputsExtra,
  type JsonObject,
} from '../lib/inputs'
import {
  applyPreset,
  parsePresetTags,
  presetDescriptionProblem,
  presetInputs,
  presetTagsProblem,
} from '../lib/presets'
import { useSubscription } from '../lib/realtime'
import { useAsync } from '../lib/useAsync'
import { useFillable } from '../agent/useFillable'
import { Hint } from './Hint'
import { Spinner } from './ui/spinner'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { MoreHorizontalIcon } from 'lucide-react'

interface Props {
  slug: string
  schema: CustomizerSchema
  values: ParamValues
  /** The UI state saved with a preset (spec 2026-09-27 §4.3). */
  extra: InputsExtra
  /** Replaces every value on screen, and the UI state, as Reset to defaults does. */
  onApply: (values: ParamValues, extra: InputsExtra) => void
  /**
   * Brings a preset's stored inputs up to the template's INPUTS_VERSION (spec §8.2)
   * before they are cut to the schema, so a renamed parameter is carried forward.
   * Null means they could not be, and the caller has said so; nothing is applied.
   */
  migrate?: (inputs: JsonObject) => JsonObject | null | Promise<JsonObject | null>
  /**
   * #355 — the page customizes an old revision. A preset belongs to the template and
   * is checked against its current revision, which refuses this one's own parameters,
   * so the values on screen can be neither saved nor updated into a preset.
   */
  pinned?: boolean
  /**
   * #350 — changes whenever the page resets the values to the defaults, which leaves
   * them no preset's: the selection is cleared, so Update cannot empty the preset.
   */
  resetKey?: number
  /**
   * #1457, #1484 — a preset the page put on screen or saved itself (a template UI's
   * `presets.load` and `presets.save`), with the values and UI state it holds. A new
   * object each time: the picker shows it as selected, as if picked here, without
   * applying it again. Read on mount too, so it survives the remount a save causes.
   */
  selected?: SelectedElsewhere | null
  /** #1660 — told the preset selected now, or null for none, whenever that changes. */
  onSelected?: (preset: ParamPreset | null) => void
}

export interface SelectedElsewhere {
  preset: ParamPreset
  values: ParamValues
  extra: InputsExtra
}

interface Selection {
  preset: ParamPreset
  /** The values the preset put on screen, so an edit since shows as a change to it. */
  applied: ParamValues
  /** The UI state it put there, which a pick replaces too (#1484). */
  extra: InputsExtra
}

/** A value the selected preset stores for a parameter this template no longer has. */
interface Skipped {
  name: string
  value: ParamValue
}

function describeSkipped(skipped: readonly Skipped[]): string {
  return skipped.map(({ name, value }) => `${name} = ${JSON.stringify(value)}`).join(', ')
}

/** What ``preset`` stores for parameters ``schema`` no longer has. */
function skippedOf(schema: CustomizerSchema, preset: ParamPreset): Skipped[] {
  const stored = splitInputs(preset.inputs, preset.params).params
  return applyPreset(schema, preset).skipped.map((name) => ({ name, value: stored[name] as ParamValue }))
}

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

/** #357 — what a preset deleted in another tab, or by the assistant, leaves to say. */
const DELETED_ELSEWHERE = 'That preset was deleted elsewhere.'

function gone(caught: unknown): boolean {
  return caught instanceof ApiError && caught.status === 404
}

/** The server's longest preset name (`library/presets.py`), in code points as it counts. */
const MAX_NAME = 80

/** `base` with `suffix`, the base cut short so the whole fits the name limit (#350). */
function prefill(base: string, suffix: string): string {
  return [...base].slice(0, MAX_NAME - [...suffix].length).join('') + suffix
}

/** The select's "no preset" item: Radix refuses an item whose value is "". */
const NONE = '\u0000none'

/** The user's own input, which ends a pick's settling (#1775, #2132). */
const USER_EVENTS = ['pointerdown', 'keydown', 'wheel', 'input'] as const
/** How long after its last write a just-picked preset's template UI is still answering it. */
const SETTLE_MS = 1000

/**
 * Named parameter sets for this template: the ones it ships with and the ones saved on
 * it, built-ins included. Picking one puts its values on screen over the defaults, and
 * from there only the value that differs this time — a name, a colour — needs changing.
 * Saving stores only what differs from the defaults.
 */
export function PresetPicker({
  slug,
  schema,
  values,
  extra,
  onApply,
  migrate,
  pinned = false,
  resetKey,
  selected: elsewhere = null,
  onSelected,
}: Props) {
  const presetsState = useAsync(() => api.listPresets(slug), [slug])
  const presets = presetsState.data ?? []
  const shipped = presets.filter((preset) => preset.origin === 'template')
  const saved = presets.filter((preset) => preset.origin === 'mine')

  const [selection, setSelection] = useState<Selection | null>(() =>
    elsewhere ? { preset: elsewhere.preset, applied: elsewhere.values, extra: elsewhere.extra } : null,
  )
  const [skipped, setSkipped] = useState<Skipped[]>(() => (elsewhere ? skippedOf(schema, elsewhere.preset) : []))
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
  const nameInput = useRef<HTMLInputElement>(null)
  const descriptionInput = useRef<HTMLTextAreaElement>(null)
  const tagsInput = useRef<HTMLInputElement>(null)
  const dialogErrorId = useId()
  const fieldIds = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  /** #359 — the preset a pick would apply over unsaved edits, while it asks. */
  const [pending, setPending] = useState<ParamPreset | null>(null)
  /** #358 — an Update would drop the skipped values for good, so it asks first. */
  const [confirmingUpdate, setConfirmingUpdate] = useState(false)

  /**
   * #357 — the presets changed elsewhere (another tab, the assistant): read them again.
   * The selected one follows its new details; gone, it is let go, with the values it
   * put on screen kept. A resync means changes may have been missed, so it reads too.
   */
  useSubscription(`model:${slug}`, (signal) => {
    if (signal !== 'resync' && signal.kind !== 'presets.changed') return
    presetsState.refresh((list) => {
      setSelection((current) => {
        if (!current) return current
        const now = list.find((preset) => preset.id === current.preset.id)
        if (now) return now === current.preset ? current : { ...current, preset: now }
        setSkipped([])
        setError(DELETED_ELSEWHERE)
        return null
      })
      return true
    })
  })

  /** #357 — a write found the selected preset gone: drop it and say why. */
  function lost(id: string) {
    presetsState.setData(presets.filter((preset) => preset.id !== id), { supersede: true })
    setSelection(null)
    setSkipped([])
    setNaming(null)
    setError(DELETED_ELSEWHERE)
  }

  // #350 — a reset puts the defaults on screen: no preset's values any more.
  const [seenReset, setSeenReset] = useState(resetKey)
  if (resetKey !== seenReset) {
    setSeenReset(resetKey)
    setSelection(null)
    setSkipped([])
    setError(null)
  }

  /**
   * #1775 — a preset just put on screen, which the template UI may still be answering:
   * one that seeds its own state when its inputs change (a tab, or state a preset saved
   * before it had any lacks) would make every pick read as changed. Until the user
   * presses a key or a pointer, turns the wheel or types (#2132), or a value moves off
   * the preset's, the UI state it writes is taken as the preset's. A UI that answers does
   * so at once, so settling also ends `SETTLE_MS` after the last write it took (#2132): a
   * write from a timer or a fetch that lands later is a change, not the preset's.
   */
  const [settling, setSettling] = useState(elsewhere !== null)

  // #1457 — a preset the page applied or saved is the selection, as a pick here is.
  const [seenElsewhere, setSeenElsewhere] = useState(elsewhere)
  if (elsewhere !== seenElsewhere) {
    setSeenElsewhere(elsewhere)
    if (elsewhere) {
      setSelection({ preset: elsewhere.preset, applied: elsewhere.values, extra: elsewhere.extra })
      setSkipped(skippedOf(schema, elsewhere.preset))
      setError(null)
      setSettling(true)
    }
  }

  if (settling) {
    if (!selection || !sameValues(values, selection.applied)) setSettling(false)
    else if (!sameJson(extra, selection.extra)) setSelection({ ...selection, extra })
  }

  useEffect(() => {
    if (!settling) return
    const settled = () => setSettling(false)
    for (const kind of USER_EVENTS) document.addEventListener(kind, settled, true)
    return () => {
      for (const kind of USER_EVENTS) document.removeEventListener(kind, settled, true)
    }
  }, [settling])

  // Restarted by every write taken as the preset's (`extra` is a new object each time).
  useEffect(() => {
    if (!settling) return
    const timer = setTimeout(() => setSettling(false), SETTLE_MS)
    return () => clearTimeout(timer)
  }, [settling, extra])

  const selected = selection?.preset
  useEffect(() => {
    onSelected?.(selected ?? null)
  }, [selected, onSelected])
  const modified =
    selection !== null && (!sameValues(values, selection.applied) || !sameJson(extra, selection.extra))
  const editable = selected?.origin === 'mine'
  // Edits a pick would lose: values or UI state that are not the selected preset's or,
  // with none selected, values that are not the defaults. The UI state has no default
  // to compare with there: a template UI may seed its own (#1484).
  const unsaved = selection ? modified : !sameValues(values, defaultValues(schema))

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
    void pick(preset)
  }

  /** The latest pick: a migration answering after another pick is dropped. */
  const picking = useRef(0)

  /** Applies `preset`, or with none clears the selection and leaves the values alone. */
  async function pick(preset: ParamPreset | undefined) {
    setError(null)
    const turn = ++picking.current
    if (!preset) {
      setSelection(null)
      setSkipped([])
      return
    }
    const stored = isJsonObject(preset.inputs)
      ? (preset.inputs as JsonObject)
      : joinInputs(preset.params ?? {}, NO_EXTRA)
    // Awaited only when it is a promise: a pick needing no migration applies at once, as
    // before (#1445: the assistant's confirm reads the values right after its click).
    const answer = migrate ? migrate(stored) : stored
    const migrated = answer instanceof Promise ? await answer : answer
    if (migrated === null || turn !== picking.current) return
    const applied = applyPreset(schema, { ...preset, inputs: migrated })
    setSelection({ preset, applied: applied.values, extra: applied.extra })
    const storedParams = splitInputs(migrated, preset.params).params
    setSkipped(applied.skipped.map((name) => ({ name, value: storedParams[name] as ParamValue })))
    setSettling(true)
    onApply(applied.values, applied.extra)
  }

  function openSaveAs() {
    setName(selected && modified ? prefill(selected.name, ' (variant)') : '')
    setDescription('')
    setTagsText('')
    setNameError(null)
    setInvalidField(null)
    setNaming('save')
  }

  function openDuplicate() {
    if (!selected) return
    setName(prefill(selected.name, ' copy'))
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

  /** An edit answers whatever the error said, so it goes (#350). */
  function edited() {
    setNameError(null)
    setInvalidField(null)
  }

  function closeNaming() {
    if (!busy) setNaming(null)
  }

  async function saveAs(event?: FormEvent) {
    event?.preventDefault()
    const chosen = name.trim()
    if (!chosen || busy || pinned) return
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
      setSelection({ preset: created, applied: values, extra })
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
      const applied = applyPreset(schema, copy)
      setSelection({ preset: copy, applied: applied.values, extra: applied.extra })
      setNaming(null)
    } catch (caught) {
      if (gone(caught)) lost(selected.id)
      else refused(caught)
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
      if (gone(caught)) lost(selected.id)
      else refused(caught)
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
    if (!selected || !editable || busy || pinned) return
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
      setSelection({ preset: updated, applied: values, extra })
      setSkipped([])
    } catch (caught) {
      if (gone(caught)) lost(selected.id)
      else setError(message(caught))
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
      // Deleted already: what was asked for is done, and the list says so.
      if (gone(caught)) lost(selected.id)
      else setError(message(caught))
      setConfirmingDelete(false)
    } finally {
      setBusy(false)
    }
  }

  // #1445 — the assistant's fill picks a preset as it did on the native select: by id or name.
  useFillable(trigger, {
    // "No preset" as the list offers it, so fill can clear the pick with "" as before.
    options: () => [
      ...(selected ? [{ value: '', label: 'No preset' }] : []),
      ...presets.map((preset) => ({ value: preset.id, label: preset.name })),
    ],
    value: () => selected?.id ?? '',
    set: choose,
  })
  const nameId = `${fieldIds}-name`
  const descriptionId = `${fieldIds}-description`
  const tagsId = `${fieldIds}-tags`
  const namingTitle =
    naming === 'duplicate'
      ? `Duplicate ${selected?.name ?? 'preset'}`
      : naming === 'details'
        ? `Edit details of ${selected?.name ?? 'preset'}`
        : 'Save as preset'
  const namingDescription =
    naming === 'duplicate'
      ? `The copy is a saved preset of yours to change, with ${selected?.name ?? 'the original'}'s values, description and tags. The original stays as it is.`
      : naming === 'details'
        ? 'Its values stay as they are: Update saves the ones on screen.'
        : "Saves the values that differ from the template's defaults, under a name to pick them by next time."

  return (
    <div data-testid="preset-picker" className="flex flex-col gap-2 border-b px-3 py-2.5">
      <div className="flex items-center gap-2">
        <Label htmlFor="preset-select" className="shrink-0 font-normal text-muted-foreground">
          Preset
        </Label>
        <Select
          value={selected?.id ?? ''}
          onValueChange={(id) => choose(id === NONE ? '' : id)}
          disabled={presetsState.loading && !presetsState.data}
        >
          <SelectTrigger ref={trigger} id="preset-select" className="min-w-0 flex-1">
            <SelectValue placeholder={presets.length === 0 ? 'No presets yet' : 'Choose a preset…'} />
          </SelectTrigger>
          <SelectContent position="popper">
            {selected && (
              <>
                <SelectItem value={NONE}>No preset</SelectItem>
                <SelectSeparator />
              </>
            )}
            {shipped.length > 0 && (
              <SelectGroup>
                <SelectLabel>From the template</SelectLabel>
                {shipped.map((preset) => (
                  <SelectItem key={preset.id} value={preset.id}>
                    {preset.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            )}
            {saved.length > 0 && (
              <SelectGroup>
                <SelectLabel>Saved</SelectLabel>
                {saved.map((preset) => (
                  <SelectItem key={preset.id} value={preset.id}>
                    {preset.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            )}
          </SelectContent>
        </Select>
      </div>

      {/* #352 — its own line above the buttons: beside them it wrapped word by word
          into a narrow column, and a long name pushed it out of the panel.
          #351 — always in the page, so "Changed from …" is announced as it appears;
          out of the flow while empty, so it adds no gap. */}
      <Hint content={modified ? `Changed from ${selected?.name ?? ''}` : undefined}>
        <p
          role="status"
          data-testid={modified ? 'preset-modified' : undefined}
          className={modified ? 'min-w-0 truncate text-xs text-muted-foreground' : 'sr-only'}
        >
          {modified && <>Changed from {selected?.name}</>}
        </p>
      </Hint>
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        {editable && modified && !pinned && (
          <Button
            type="button"
            size="sm"
            onClick={() => void update()}
            disabled={busy}
            aria-label={`Update preset ${selected?.name ?? ''}`}
          >
            Update
          </Button>
        )}
        <Button type="button" variant="outline" size="sm" onClick={openSaveAs} disabled={busy || pinned}>
          Save as preset…
        </Button>
        {selected && (
          // #3672 — the preset's own actions, in one menu beside Save as. Each dialog opens
          // once the menu has closed and put the focus back on its button.
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="outline" size="icon-sm" disabled={busy} aria-label={`More for preset ${selected.name}`}>
                <MoreHorizontalIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-auto min-w-44">
              <DropdownMenuItem onSelect={() => setTimeout(openDuplicate, 0)} aria-label={`Duplicate preset ${selected.name}`}>
                Duplicate
              </DropdownMenuItem>
              {editable && (
                <DropdownMenuItem onSelect={() => setTimeout(openDetails, 0)} aria-label={`Edit details of preset ${selected.name}`}>
                  Edit details
                </DropdownMenuItem>
              )}
              {editable && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    onSelect={() => setTimeout(() => setConfirmingDelete(true), 0)}
                    aria-label={`Delete preset ${selected.name}`}
                  >
                    Delete
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {pinned && (
        <p data-testid="preset-pinned" className="text-xs text-muted-foreground">
          Presets are saved against the current version.
        </p>
      )}

      {selected && (selected.description || selected.tags.length > 0) && (
        <div data-testid="preset-details" className="flex flex-col gap-1.5 text-xs text-muted-foreground">
          {selected.tags.length > 0 && (
            <ul aria-label="Preset tags" className="flex flex-wrap gap-1">
              {selected.tags.map((tag) => (
                <li key={tag}>
                  <Badge variant="secondary">{tag}</Badge>
                </li>
              ))}
            </ul>
          )}
          {selected.description && (
            // #1074 — a preset the template ships is one of its files: its relative images
            // are the template's too. A saved one of mine has no folder to resolve them in.
            // No revision in the base: these are the template's current presets, checked
            // against its current files (#355), so its current files are the right ones.
            // Presets listed per revision would pass the viewed one here (#3246).
            <Markdown text={selected.description} base={selected.origin === 'template' ? { slug } : undefined} />
          )}
        </div>
      )}

      {skipped.length > 0 && (
        <p role="status" className="text-xs text-destructive">
          Skipped {skipped.length === 1 ? 'a value' : `${skipped.length} values`} this template no
          longer has: {describeSkipped(skipped)}
        </p>
      )}
      {(error ?? presetsState.error) && (
        <p role="alert" className="text-xs text-destructive">
          {error ?? `Could not load presets: ${presetsState.error?.message}`}
        </p>
      )}

      <Dialog open={naming !== null} onOpenChange={(open) => !open && closeNaming()}>
        <DialogContent
          // #350 — the name field takes the focus with its prefill selected, so typing
          // replaces it rather than appending to it.
          onOpenAutoFocus={(event) => {
            event.preventDefault()
            nameInput.current?.focus()
            nameInput.current?.select()
          }}
        >
          <DialogHeader>
            <DialogTitle>{namingTitle}</DialogTitle>
            <DialogDescription>{namingDescription}</DialogDescription>
          </DialogHeader>
          <form id={`${fieldIds}-form`} onSubmit={submitNaming} className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor={nameId}>Preset name</Label>
              <Input
                id={nameId}
                ref={nameInput}
                value={name}
                onChange={(event) => {
                  setName(event.target.value)
                  edited()
                }}
                maxLength={MAX_NAME}
                aria-invalid={invalidField === 'name' || undefined}
                aria-describedby={invalidField === 'name' && nameError ? dialogErrorId : undefined}
              />
            </div>
            {naming !== 'duplicate' && (
              <>
                <div className="flex flex-col gap-2">
                  <Label htmlFor={descriptionId}>Description (optional, Markdown)</Label>
                  <Textarea
                    id={descriptionId}
                    ref={descriptionInput}
                    value={description}
                    onChange={(event) => {
                      setDescription(event.target.value)
                      edited()
                    }}
                    rows={3}
                    aria-invalid={invalidField === 'description' || undefined}
                    aria-describedby={invalidField === 'description' && nameError ? dialogErrorId : undefined}
                  />
                </div>
                <div className="flex flex-col gap-2">
                  <Label htmlFor={tagsId}>Tags (optional, comma-separated)</Label>
                  <Input
                    id={tagsId}
                    ref={tagsInput}
                    value={tagsText}
                    onChange={(event) => {
                      setTagsText(event.target.value)
                      edited()
                    }}
                    aria-invalid={invalidField === 'tags' || undefined}
                    aria-describedby={invalidField === 'tags' && nameError ? dialogErrorId : undefined}
                    placeholder="gift, small"
                  />
                </div>
              </>
            )}
            {nameError && (
              <p id={dialogErrorId} role="alert" className="text-sm text-destructive">
                {nameError}
              </p>
            )}
          </form>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={closeNaming} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" form={`${fieldIds}-form`} disabled={busy || !name.trim()}>
              {busy && <Spinner />}
              {naming === 'duplicate' ? 'Duplicate' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent
          // Opened from the select's list, which is gone by now: back to the select (#1457).
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            trigger.current?.focus()
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>{`Apply preset ${pending?.name ?? ''}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              {selected
                ? `You changed ${selected.name} since you picked it.`
                : 'The values on screen have changes no preset holds.'}{' '}
              Applying {pending?.name} replaces them.
              {!pinned && (
                <>
                  {' '}To keep them, cancel and {editable ? `update ${selected?.name} or ` : ''}save
                  them as a preset first.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              // The preset as asked about, not looked up again: it may have changed since.
              // Not USER_ONLY: replacing the values on screen stays in the page, so the
              // assistant may confirm a pick it made (spec §8.1).
              onClick={() => {
                void pick(pending ?? undefined)
                setPending(null)
              }}
            >
              Replace my changes
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={confirmingUpdate} onOpenChange={(open) => !open && setConfirmingUpdate(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{`Update preset ${selected?.name ?? ''}`}</AlertDialogTitle>
            <AlertDialogDescription>
              This template no longer has every parameter the preset sets. {selected?.name} also sets{' '}
              {describeSkipped(skipped)}, which this template no longer has. Updating drops them; note them
              first to re-enter them on the new parameters.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={() => void update(true)} disabled={busy} {...USER_ONLY}>
              {busy && <Spinner />}
              Update and drop them
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={confirmingDelete} onOpenChange={(open) => !open && setConfirmingDelete(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{`Delete preset ${selected?.name ?? ''}`}</AlertDialogTitle>
            <AlertDialogDescription>The values on screen stay as they are. This cannot be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={() => void remove()} disabled={busy} {...USER_ONLY}>
              {busy && <Spinner />}
              Delete preset
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
