import { ChevronRightIcon, PencilIcon, PlusIcon, RotateCwIcon, Trash2Icon } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { api, ApiError, mayHaveRun } from '../../api/client'
import type { KProfile, KProfileRemove, KProfileSet, KProfileWriteResult, PrinterRow } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { ChoiceSelect, LabeledInput, LoadingLines } from './form'
import { Spinner } from '../ui/spinner'
import { Said, stopped, usePrinterCommand } from './usePrinterCommand'

/**
 * #7154, #7276 — a printer's K-profiles (pressure advance), read from and written to the
 * printer over ScadBuddy's own LAN link (`backend/scadbuddy/api/printer_kprofiles.py`), shown
 * on a printer whose row is `lan_linked`. Closed until opened: each read asks the printer over
 * MQTT (R-7154-9). A write goes once under one key (R-7154-5, `printerCommand`, which follows a
 * slow command's 202), says it was sent while the printer has not answered, and says when the
 * printer did not confirm it (R-7154-7). A press that repeats a command the page stopped
 * following sends its key again (P55, `usePrinterCommand`); closing the panel or changing the
 * nozzle stops every follow. Notes are ScadBuddy's own, under the keys Bambuddy's K-Profiles
 * page used, so the notes carried over from it still match (R-7154-8).
 */

export const NOZZLES = ['0.2', '0.4', '0.6', '0.8'] as const
const STANDARD = 'HS00'
const HIGH_FLOW = 'HH00'
const FLOWS = [
  { value: STANDARD, label: 'Standard' },
  { value: HIGH_FLOW, label: 'High flow' },
]

function problem(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

/** Bambuddy's flow type from `nozzle_id`; a printer that reports none is standard (#1748 there). */
export function flowOf(nozzleId: string): string {
  return /^([A-Z]{2}\d{2})/.exec(nozzleId)?.[1] === HIGH_FLOW ? HIGH_FLOW : STANDARD
}

/** K as Bambu Studio shows it: truncated to three places. */
export function shownK(value: string): string {
  const k = Number.parseFloat(value)
  return Number.isFinite(k) ? (Math.trunc(k * 1000) / 1000).toFixed(3) : value
}

/** K as the printer holds it, for editing: every place, the trailing zeros dropped (`0.034500` is `0.0345`). */
export function editedK(value: string): string {
  return /^\d+\.\d*$/.test(value) ? value.replace(/\.?0+$/, '') || '0' : value
}

/**
 * The key a note on `profile` goes under when it has none yet: its `setting_id`, else its slot
 * key, the first keys Bambuddy's page reads (`noteKeys`), so no later note there hides it.
 */
function noteKeyFor(profile: KProfile): string {
  return profile.setting_id || `slot_${profile.slot_id}_${profile.filament_id}_${profile.extruder_id}`
}

/** The keys Bambuddy's page stores a profile's note under, in the order it reads them. */
export function noteKeys(profile: KProfile): string[] {
  return [
    ...(profile.setting_id ? [profile.setting_id] : []),
    `slot_${profile.slot_id}_${profile.filament_id}_${profile.extruder_id}`,
    `name_${profile.name}_${profile.filament_id}`,
  ]
}

function noteOf(notes: Record<string, string>, profile: KProfile): { key: string; note: string } | undefined {
  for (const key of noteKeys(profile)) {
    const note = notes[key]
    if (note) return { key, note }
  }
  return undefined
}

/** What a write's answer says to the person: never "saved" when the printer did not confirm it. */
function said(result: KProfileWriteResult, done: string): { text: string; warn: boolean } {
  return result.confirmed === false
    ? { text: 'Sent; the printer did not confirm it. Check the list before you send it again.', warn: true }
    : { text: done, warn: false }
}

/** #7276 — a write the printer has not answered yet (its route's 202): it was sent, and runs. */
const WAITING = { text: 'Sent to the printer. Waiting for it to answer.', warn: false }

export function PrinterKProfiles({ row }: { row: PrinterRow }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <Button size="xs" variant="ghost" aria-expanded={open} className="-ml-2">
          <ChevronRightIcon className={open ? 'rotate-90 transition-transform' : 'transition-transform'} />
          K-profiles
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>{open && <KProfilesBody row={row} />}</CollapsibleContent>
    </Collapsible>
  )
}

type Editing = { kind: 'add' } | { kind: 'edit'; profile: KProfile }

type Send = ReturnType<typeof usePrinterCommand>['send']

function KProfilesBody({ row }: { row: PrinterRow }) {
  const [nozzle, setNozzle] = useState<string>('0.4')
  const commands = usePrinterCommand(row.id)
  const table = useAsync(() => commands.track((signal) => api.listKProfiles(row.id, nozzle, { signal })), [row.id, nozzle])
  const notes = useAsync(() => api.kProfileNotes(row.id), [row.id])
  const [editing, setEditing] = useState<Editing | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; warn: boolean } | null>(null)
  const profiles = table.data?.profiles ?? []
  // P54: the count is the printer's own report's (`extruders`); a list that names none is two
  // when a profile is on extruder 1.
  const extruders = table.data?.extruders ?? (profiles.some((profile) => profile.extruder_id === 1) ? 2 : 1)
  const dual = extruders > 1
  const noted = notes.data?.notes ?? {}
  const rowKey = (profile: KProfile) => `${profile.slot_id}-${profile.extruder_id}-${profile.filament_id}`

  const refresh = () => {
    table.refresh()
    notes.refresh()
  }

  async function remove(profile: KProfile) {
    setBusy(true)
    setMessage(null)
    const body: KProfileRemove = {
      slot_id: profile.slot_id,
      extruder_id: profile.extruder_id,
      nozzle_id: profile.nozzle_id,
      nozzle_diameter: profile.nozzle_diameter as KProfileSet['nozzle_diameter'],
      filament_id: profile.filament_id,
      setting_id: profile.setting_id ?? null,
    }
    try {
      const result = await commands.send('kprofiles', 'delete', body, (options) =>
        api.deleteKProfile(row.id, body, { ...options, onWaiting: () => setMessage(WAITING) }),
      )
      setMessage(said(result, `Deleted ${profile.name}.`))
      setDeleting(null)
      refresh()
    } catch (cause) {
      if (stopped(cause)) {
        // The follow is over: leave the status empty rather than a stale "Waiting…".
        setMessage(null)
        return
      }
      // A delete that may have reached the printer closes its confirm, so it is not sent again.
      if (mayHaveRun(cause)) setDeleting(null)
      setMessage({ text: problem(cause, 'Could not delete the K-profile.'), warn: true })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 space-y-2" aria-label={`K-profiles of ${row.name}`} role="region">
      <div className="flex flex-wrap items-center gap-2">
        <ChoiceSelect
          aria-label="Nozzle diameter"
          size="sm"
          className="w-auto min-w-28"
          value={nozzle}
          choices={NOZZLES.map((value) => ({ value, label: `${value} mm` }))}
          onValueChange={(next) => {
            // The old nozzle's read and any write's follow stop; a write already sent still runs.
            commands.stop()
            setNozzle(next)
            setEditing(null)
            setDeleting(null)
          }}
        />
        <Button size="xs" variant="outline" disabled={busy || editing !== null} onClick={() => setEditing({ kind: 'add' })}>
          <PlusIcon /> Add a K-profile
        </Button>
        <Button size="xs" variant="ghost" disabled={table.loading} aria-label="Read the K-profiles again" onClick={refresh}>
          <RotateCwIcon />
        </Button>
        {(busy || table.loading) && <Spinner />}
      </div>

      {table.error ? (
        <p role="alert" className="text-xs text-warn">
          {problem(table.error, 'The K-profiles could not be read.')}
        </p>
      ) : !table.data ? (
        <LoadingLines label="Reading the K-profiles from the printer" />
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <table aria-label={`K-profiles at ${nozzle} mm`} className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className="px-2 py-1.5 font-medium">Name</th>
                <th scope="col" className="px-2 py-1.5 font-medium">Filament</th>
                {dual && <th scope="col" className="px-2 py-1.5 font-medium">Extruder</th>}
                <th scope="col" className="px-2 py-1.5 font-medium">Flow</th>
                <th scope="col" className="px-2 py-1.5 text-right font-medium">K</th>
                <th scope="col" className="px-2 py-1.5 font-medium">Note</th>
                <th scope="col" className="w-px px-2 py-1.5"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {profiles.map((profile) => {
                const key = rowKey(profile)
                const note = noteOf(noted, profile)?.note
                return (
                  <tr key={key} aria-label={`K-profile ${profile.name}`} className="border-t align-top">
                    <td className="px-2 py-1.5">{profile.name}</td>
                    <td className="px-2 py-1.5 tabular-nums">{profile.filament_id}</td>
                    {dual && <td className="px-2 py-1.5 tabular-nums">{profile.extruder_id}</td>}
                    <td className="px-2 py-1.5">
                      <Badge variant="outline">{flowOf(profile.nozzle_id) === HIGH_FLOW ? 'High flow' : 'Standard'}</Badge>
                    </td>
                    <td className="px-2 py-1.5 text-right font-medium tabular-nums" title={profile.k_value}>
                      {shownK(profile.k_value)}
                    </td>
                    <td className="max-w-56 px-2 py-1.5 text-muted-foreground">{note}</td>
                    <td className="px-2 py-1 whitespace-nowrap">
                      {deleting === key ? (
                        <span className="flex items-center gap-1">
                          <Button size="xs" variant="destructive" disabled={busy} onClick={() => void remove(profile)}>
                            Delete {profile.name}
                          </Button>
                          <Button size="xs" variant="ghost" disabled={busy} onClick={() => setDeleting(null)}>
                            Keep
                          </Button>
                        </span>
                      ) : (
                        <span className="flex items-center gap-1">
                          <Button
                            size="icon-xs"
                            variant="ghost"
                            aria-label={`Edit ${profile.name}`}
                            disabled={busy || editing !== null}
                            onClick={() => setEditing({ kind: 'edit', profile })}
                          >
                            <PencilIcon />
                          </Button>
                          <Button
                            size="icon-xs"
                            variant="ghost"
                            aria-label={`Delete ${profile.name}`}
                            disabled={busy || editing !== null}
                            onClick={() => setDeleting(key)}
                          >
                            <Trash2Icon />
                          </Button>
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
              {profiles.length === 0 && (
                <tr className="border-t">
                  <td colSpan={dual ? 7 : 6} className="px-2 py-3 text-muted-foreground">
                    The printer holds no K-profile for a {nozzle} mm nozzle. Add one, or run flow dynamics calibration on the
                    printer.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {notes.error && (
        <p className="text-xs text-muted-foreground">{problem(notes.error, 'The notes could not be read.')}</p>
      )}

      {editing && (
        <KProfileForm
          row={row}
          nozzle={nozzle}
          dual={dual}
          editing={editing}
          send={commands.send}
          note={editing.kind === 'edit' ? noteOf(noted, editing.profile) : undefined}
          onCancel={() => setEditing(null)}
          onWaiting={() => setMessage(WAITING)}
          onDone={(next) => {
            setMessage(next)
            setEditing(null)
            refresh()
          }}
        />
      )}

      <Said message={message} />
    </div>
  )
}

type Extruders = '0' | '1' | 'both'

function KProfileForm({
  row,
  nozzle,
  dual,
  editing,
  send,
  note,
  onCancel,
  onWaiting,
  onDone,
}: {
  row: PrinterRow
  nozzle: string
  dual: boolean
  editing: Editing
  send: Send
  note: { key: string; note: string } | undefined
  onCancel: () => void
  onWaiting: () => void
  onDone: (message: { text: string; warn: boolean }) => void
}) {
  const was = editing.kind === 'edit' ? editing.profile : undefined
  const [name, setName] = useState(was?.name ?? '')
  const [filament, setFilament] = useState(was?.filament_id ?? '')
  const [k, setK] = useState(was ? editedK(was.k_value) : '0.020')
  const [flow, setFlow] = useState(was ? flowOf(was.nozzle_id) : STANDARD)
  const [extruders, setExtruders] = useState<Extruders>(was ? (String(was.extruder_id) as Extruders) : dual ? 'both' : '0')
  const [text, setText] = useState(note?.note ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const diameter = (was?.nozzle_diameter ?? nozzle) as KProfileSet['nozzle_diameter']
    const body = (extruder: number): KProfileSet => ({
      slot_id: was?.slot_id ?? 0,
      extruder_id: extruder,
      // An edit keeps the id the printer reported, as Bambuddy's page does; one it reported
      // empty, and a new profile, get the flow type and the nozzle (`HS00-0.4`).
      nozzle_id: was?.nozzle_id || `${flow}-${diameter}`,
      nozzle_diameter: diameter,
      filament_id: filament.trim(),
      name: name.trim(),
      k_value: k.trim(),
      setting_id: was?.setting_id || null,
      ...(was ? { n_coef: was.n_coef, ams_id: was.ams_id, tray_id: was.tray_id } : {}),
    })
    const noteChanged = text !== (note?.note ?? '')
    // A profile write runs once and on a single-nozzle printer is a delete and an add, so an
    // edit that changes only the note sends only the note.
    const profileChanged = !was || name.trim() !== was.name || Number(k.trim()) !== Number(was.k_value)
    let result: KProfileWriteResult | undefined
    try {
      if (profileChanged) {
        if (extruders === 'both' && !was) {
          const profiles = [body(0), body(1)]
          result = await send('kprofiles', 'batch', profiles, (options) =>
            api.setKProfilesBatch(row.id, profiles, { ...options, onWaiting }),
          )
        } else {
          const one = body(was ? was.extruder_id : Number(extruders))
          result = await send('kprofiles', 'set', one, (options) => api.setKProfile(row.id, one, { ...options, onWaiting }))
        }
      }
    } catch (cause) {
      if (stopped(cause)) return
      // A write that may have reached the printer closes the form, so it is not sent again.
      if (mayHaveRun(cause)) {
        onDone({ text: problem(cause, 'The printer has not answered. Do not send it again.'), warn: true })
        return
      }
      setError(problem(cause, 'Could not save the K-profile.'))
      setBusy(false)
      return
    }
    const saved = was ? `Saved ${name.trim()}` : `Added ${name.trim()}`
    if (noteChanged) {
      // The key the note was read under; else, for a new profile, the one Bambuddy's page
      // gives it (its slot and setting are not known until the printer answers).
      const key = note?.key ?? (was ? noteKeyFor(was) : `name_${name.trim()}_${filament.trim()}`)
      try {
        await api.setKProfileNote(row.id, key, text)
      } catch (cause) {
        // The profile write is done and must not be sent again: close, and say what failed.
        onDone({ text: `${saved}, but not its note: ${problem(cause, 'the note could not be saved')}.`, warn: true })
        return
      }
    }
    if (!result) {
      onDone({ text: noteChanged ? `Saved the note on ${was?.name}.` : 'Nothing changed.', warn: false })
      return
    }
    onDone(said(result, `${saved}.`))
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label={was ? `Edit ${was.name}` : 'Add a K-profile'}
      className="space-y-3 rounded-md border p-3"
    >
      <p className="text-sm font-medium">{was ? `Edit ${was.name}` : `New K-profile for a ${nozzle} mm nozzle`}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <LabeledInput label="Name" required maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
        <LabeledInput
          label="Filament id"
          required
          pattern="[A-Za-z0-9_.+\-]{1,64}"
          placeholder="GFA00"
          disabled={was !== undefined}
          value={filament}
          onChange={(e) => setFilament(e.target.value)}
        />
        <LabeledInput
          label="K value"
          required
          inputMode="decimal"
          pattern="\d{1}(\.\d{1,6})?"
          value={k}
          onChange={(e) => setK(e.target.value)}
        />
        <div className="grid gap-1.5">
          <Label className="font-normal">Flow</Label>
          <ChoiceSelect aria-label="Flow" disabled={was !== undefined} value={flow} choices={FLOWS} onValueChange={setFlow} />
        </div>
        {dual && (
          <div className="grid gap-1.5">
            <Label className="font-normal">Extruder</Label>
            <ChoiceSelect
              aria-label="Extruder"
              disabled={was !== undefined}
              value={extruders}
              choices={[
                { value: '0', label: 'Extruder 0' },
                { value: '1', label: 'Extruder 1' },
                ...(was ? [] : [{ value: 'both', label: 'Both extruders' }]),
              ]}
              onValueChange={(next) => setExtruders(next as Extruders)}
            />
          </div>
        )}
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`kp-note-${row.id}`} className="font-normal">
          Note
        </Label>
        <Textarea id={`kp-note-${row.id}`} maxLength={2000} rows={2} value={text} onChange={(e) => setText(e.target.value)} />
        <p className="text-xs text-muted-foreground">Kept in ScadBuddy, not on the printer. Clear it to delete it.</p>
      </div>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          {was ? 'Save K-profile' : 'Add K-profile'}
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
        {busy && <Spinner />}
      </div>
    </form>
  )
}
