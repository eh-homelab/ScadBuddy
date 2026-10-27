import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../api/client'
import { useLatest } from '../../lib/useLatest'
import type { Settings, SettingsUpdate } from '../../api/types'
import { UNCONFIRMED, useLoadedSettings, useSettingsCache, type Seeder } from './cache'
import { useReportDirty } from './dirty'
import type { FieldName, SectionId } from './fields'
import { editedSince, pendingFields, seedDraft, type Draft, type Seed } from './seed'
import { baseline, isProblem, isSecret, toPatchValue, type Secret } from './values'

/** A section's fields, or how to read them from the settings (the runtime ones vary). */
export type FieldsOf = readonly FieldName[] | ((settings: Settings) => readonly FieldName[])

export type FormContext = {
  value: (name: FieldName) => string
  changed: (name: FieldName) => boolean
  settings: Settings
}

export type BackendSettingsOptions = {
  /** What the form compares and sends for `name` in place of its raw value. */
  effective?: (name: FieldName, raw: string, form: Omit<FormContext, 'changed'>) => string
  /** More of the body a save sends, beside the section's changed fields. */
  alsoSend?: (form: FormContext) => Partial<SettingsUpdate>
  /** More of the body a reset of `name` sends, beside `reset: [name]`. */
  alsoReset?: (name: FieldName, settings: Settings) => Partial<SettingsUpdate>
  /** The fields whose drafts another section reads (`usePeerDraft`). */
  publish?: readonly FieldName[]
  onSaved?: (next: Settings) => void
  onReset?: (name: FieldName, next: Settings) => void
}

export type BackendSettings = {
  settings: Settings
  value: (name: FieldName) => string
  setField: (name: FieldName, next: string) => void
  changed: (name: FieldName) => boolean
  dirty: boolean
  errors: Partial<Record<FieldName, string>>
  sectionError: string | undefined
  saving: boolean
  savedAt: string | undefined
  clearing: readonly Secret[]
  clearSecret: (name: Secret) => void
  /** True when it saved, or had nothing to save. */
  save: () => Promise<boolean>
  discard: () => void
  reset: (name: FieldName) => Promise<void>
  resetting: FieldName | null
  resetError: string | null
}

/** The field names of a partial body, without `reset`. */
const namesOf = (body: Partial<SettingsUpdate>): FieldName[] =>
  Object.keys(body).filter((name): name is FieldName => name !== 'reset')

/**
 * #2171 — one Settings section's form over `PUT /settings`: its draft, seeded from the
 * shared settings cache, its own Save, Discard and per-field Reset.
 */
export function useBackendSettings(id: SectionId, fields: FieldsOf, options: BackendSettingsOptions = {}): BackendSettings {
  const cache = useSettingsCache()
  const settings = useLoadedSettings()
  const fieldsFor = (of: Settings): readonly FieldName[] => (typeof fields === 'function' ? fields(of) : fields)

  const [draft, setDraft] = useState<Draft>({})
  const [clearing, setClearing] = useState<Secret[]>([])
  const [saving, setSaving] = useState(false)
  /** Saves in flight: Test connection can send one beside another (#4847 review). */
  const inFlight = useRef(0)
  const [savedAt, setSavedAt] = useState<string | undefined>(undefined)
  const [errors, setErrors] = useState<Partial<Record<FieldName, string>>>({})
  const [sectionError, setSectionError] = useState<string | undefined>(undefined)
  const [resetting, setResetting] = useState<FieldName | null>(null)
  const [resetError, setResetError] = useState<string | null>(null)
  // #4826 — a lost save's or reset's note that nothing confirmed what is stored, shown once
  // after the failure it follows, where that failure is reported, until a read is taken.
  const [saveUnconfirmed, setSaveUnconfirmed] = useState(false)
  const [resetUnconfirmed, setResetUnconfirmed] = useState(false)

  // What the next settings objects seed the form with: all of it at first and after a
  // reload, the fields a save or reset wrote after it. Queued, since requests overlap.
  // Each seed carries the edit counts from when its request was sent, and leaves alone
  // any field typed into since (#767): the seed's effect can land after a keystroke, and
  // a save's answer can arrive after more typing.
  const edits = useRef(new Map<FieldName, number>())
  const seeds = useRef<Seed[]>([{ names: 'all', since: new Map() }])
  const [seeder] = useState<Seeder>(() => ({
    begin: () => new Map(edits.current),
    seed: (names, since) => {
      seeds.current.push({ names, since })
    },
    confirm: () => {
      setSaveUnconfirmed(false)
      setResetUnconfirmed(false)
    },
  }))
  const { register } = cache
  useEffect(() => register(seeder), [register, seeder])

  const fieldsRef = useLatest(fieldsFor)
  useEffect(() => {
    if (seeds.current.length === 0) return
    // Only this section's fields: a write names every field it wrote, in any section.
    const own = new Set(fieldsRef.current(settings))
    const since = new Map([...pendingFields(seeds.current, () => [...own])].filter(([name]) => own.has(name)))
    seeds.current = []
    const edited = editedSince(since, edits.current)
    const names = [...since.keys()].filter((name) => !edited.has(name))
    const values = Object.fromEntries(names.map((name) => [name, baseline(settings, name)]))
    // Checked again in the updater: it runs later, and a keystroke can land between.
    setDraft((current) => seedDraft(current, values, editedSince(since, edits.current)))
    setClearing((current) => current.filter((name) => !names.includes(name)))
  }, [settings, fieldsRef])

  const value = (name: FieldName): string => draft[name] ?? baseline(settings, name)
  const setField = (name: FieldName, next: string) => {
    edits.current.set(name, (edits.current.get(name) ?? 0) + 1)
    setDraft((current) => ({ ...current, [name]: next }))
    setErrors((current) => ({ ...current, [name]: undefined }))
    // A key typed after Remove key replaces the stored one rather than clearing it.
    if (isSecret(name) && next !== '') setClearing((current) => current.filter((secret) => secret !== name))
  }

  const effective = (name: FieldName): string =>
    options.effective ? options.effective(name, value(name), { value, settings }) : value(name)

  const changed = (name: FieldName): boolean => {
    if (isSecret(name)) return value(name) !== '' || clearing.includes(name)
    return effective(name) !== baseline(settings, name)
  }
  const dirty = fieldsFor(settings).some(changed)

  const publish = options.publish ?? []
  useReportDirty(id, dirty, Object.fromEntries(publish.map((name) => [name, value(name)])))

  /** The section's body, with what `alsoSend` adds; null when a field is refused here. */
  function patchFor(): { body: SettingsUpdate; also: FieldName[] } | null {
    const body: Record<string, unknown> = {}
    const problems: Partial<Record<FieldName, string>> = {}
    for (const name of fieldsFor(settings)) {
      if (!changed(name)) continue
      if (isSecret(name)) {
        // Omitted when untouched, so the stored key is left alone; "" clears it.
        body[name] = value(name)
        continue
      }
      const next = toPatchValue(name, effective(name), settings)
      if (isProblem(next)) problems[name] = next.problem
      else body[name] = next
    }
    if (Object.keys(problems).length > 0) {
      setErrors((current) => ({ ...current, ...problems }))
      return null
    }
    const also = options.alsoSend?.({ value, changed, settings }) ?? {}
    return { body: { ...body, ...also } as SettingsUpdate, also: namesOf(also) }
  }

  async function save(): Promise<boolean> {
    const patch = patchFor()
    if (patch === null) return false
    const { body, also } = patch
    if (Object.keys(body).length === 0) return true
    inFlight.current++
    setSaving(true)
    setSectionError(undefined)
    setSaveUnconfirmed(false)
    try {
      const next = await cache.write((answer) => [...fieldsFor(answer), ...also], () => api.putSettings(body), {
        written: namesOf(body),
        unconfirmed: () => setSaveUnconfirmed(true),
      })
      setSavedAt(new Date().toLocaleTimeString())
      options.onSaved?.(next)
      return true
    } catch (cause) {
      // A 422 names each refused field (`loc: ["body", <field>]`): shown beside it.
      const fieldErrors: Partial<Record<FieldName, string>> = {}
      const reported = cause instanceof ApiError ? (cause.problem as { errors?: unknown }).errors : undefined
      for (const entry of Array.isArray(reported) ? (reported as { loc?: string[]; msg?: string }[]) : []) {
        const name = entry.loc?.[1] as FieldName | undefined
        if (name && entry.msg) fieldErrors[name] = entry.msg.replace(/^Value error, /, '')
      }
      setErrors((current) => ({ ...current, ...fieldErrors }))
      if (Object.keys(fieldErrors).length === 0) {
        setSectionError(cause instanceof ApiError ? cause.detail : 'Could not save the settings.')
      }
      return false
    } finally {
      inFlight.current--
      setSaving(inFlight.current > 0)
    }
  }

  function discard() {
    const names = fieldsFor(settings)
    setDraft((current) => ({
      ...current,
      ...Object.fromEntries(names.map((name) => [name, baseline(settings, name)])),
    }))
    setClearing((current) => current.filter((name) => !names.includes(name)))
    setErrors((current) => ({ ...current, ...Object.fromEntries(names.map((name) => [name, undefined])) }))
    setSectionError(undefined)
    setSaveUnconfirmed(false)
  }

  async function reset(name: FieldName) {
    setResetting(name)
    setResetError(null)
    setResetUnconfirmed(false)
    try {
      const also = options.alsoReset?.(name, settings) ?? {}
      const written = [name, ...namesOf(also)]
      const next = await cache.write(written, () => api.putSettings({ ...also, reset: [name] }), {
        written,
        unconfirmed: () => setResetUnconfirmed(true),
      })
      options.onReset?.(name, next)
    } catch (cause) {
      setResetError(cause instanceof ApiError ? cause.detail : 'Could not reset the setting.')
    } finally {
      // A newer reset of another field may still be in flight: it keeps showing (#4847 review).
      setResetting((current) => (current === name ? null : current))
    }
  }

  const clearSecret = (name: Secret) => {
    setClearing((current) => [...current, name])
    setField(name, '')
  }

  return {
    settings,
    value,
    setField,
    changed,
    dirty,
    errors,
    sectionError: withNote(sectionError, saveUnconfirmed),
    saving,
    savedAt,
    clearing,
    clearSecret,
    save,
    discard,
    reset,
    resetting,
    resetError: withNote(resetError, resetUnconfirmed) ?? null,
  }
}

/** `message`, followed by the {@link UNCONFIRMED} note when `unconfirmed`. */
function withNote(message: string | null | undefined, unconfirmed: boolean): string | undefined {
  if (!unconfirmed) return message ?? undefined
  return message ? `${message} ${UNCONFIRMED}` : UNCONFIRMED
}
