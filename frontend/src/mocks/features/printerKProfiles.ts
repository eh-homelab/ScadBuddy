import { HttpResponse, http } from 'msw'
import type { KProfile, KProfileNotes, KProfileRemove, KProfileSet, KProfileTable, KProfileWriteResult } from '../../api/types'
import { commandAnswer, joined, keyRequired, lanRefusal } from './printerCommands'
import { printersState } from './printers'

/**
 * #7154, #7276 — a printer's K-profiles over ScadBuddy's LAN link
 * (`backend/scadbuddy/api/printer_kprofiles.py`). The printer holds two profiles at 0.4 mm, and
 * ScadBuddy one note. A printer `lan_linked` is false on is refused 409 `not_lan_linked`, as the
 * route answers (`printerCommands.ts` `lanRefusal`), so a test links printer 1's row first. A
 * write without an `Idempotency-Key` is a 428. `setKProfileUnconfirmed(true)` makes the printer
 * leave every write unconfirmed; `printerCommands.ts` `setPrinterCommandSlow` makes every command
 * a 202 to follow. The list answers `extruders`, 2 as the H2C reports (`setKProfileExtruders`).
 * The notes are ScadBuddy's own and need no LAN link.
 */

export const H2C = '00000000-0000-4000-8000-000000000001'
const FEATURE = 'K-profiles'

function seedProfiles(): KProfile[] {
  return [
    {
      slot_id: 1,
      extruder_id: 0,
      nozzle_id: 'HS00-0.4',
      nozzle_diameter: '0.4',
      filament_id: 'GFA00',
      name: 'Bambu PLA Basic',
      k_value: '0.020000',
      setting_id: 'PFUS1',
      n_coef: '0.000000',
      ams_id: 0,
      tray_id: -1,
    },
    {
      slot_id: 2,
      extruder_id: 1,
      nozzle_id: 'HH00-0.4',
      nozzle_diameter: '0.4',
      filament_id: 'GFG02',
      name: 'High Flow_Bambu PETG HF',
      k_value: '0.034500',
      setting_id: null,
      n_coef: '0.000000',
      ams_id: 0,
      tray_id: -1,
    },
  ]
}

const state = {
  profiles: seedProfiles(),
  notes: { PFUS1: 'Calibrated on the textured plate' } as Record<string, string>,
  unconfirmed: false,
  nextSlot: 3,
  extruders: 2 as number | null,
}

export function reset(): void {
  state.profiles = seedProfiles()
  state.notes = { PFUS1: 'Calibrated on the textured plate' }
  state.unconfirmed = false
  state.nextSlot = 3
  state.extruders = 2
}

/** The list's `extruders`, as the route reads it from the printer's last report; `null` for a report that names none. */
export function setKProfileExtruders(count: number | null): void {
  state.extruders = count
}

export function setKProfileUnconfirmed(on: boolean): void {
  state.unconfirmed = on
}

export function mockKProfiles(): KProfile[] {
  return state.profiles
}

export function mockKProfileNotes(): Record<string, string> {
  return state.notes
}

/** A note is ScadBuddy's own: the printer needs only a row (`_notes_owner`), no LAN link. */
const noPrinter = (id: string) =>
  printersState().some((row) => row.id === id)
    ? undefined
    : HttpResponse.json({ title: `no printer ${id}`, status: 404, detail: `no printer ${id}` }, { status: 404 })

const answer = (message: string) =>
  ({
    success: true,
    confirmed: !state.unconfirmed,
    message: state.unconfirmed ? 'K-profile sent; the printer did not confirm it' : message,
  }) satisfies KProfileWriteResult

function add(row: KProfileSet): void {
  const slot = state.nextSlot++
  state.profiles.push({
    slot_id: slot,
    extruder_id: row.extruder_id ?? 0,
    nozzle_id: row.nozzle_id,
    nozzle_diameter: row.nozzle_diameter,
    filament_id: row.filament_id,
    name: row.name,
    k_value: Number.parseFloat(row.k_value).toFixed(6),
    setting_id: null,
    n_coef: '0.000000',
    ams_id: 0,
    tray_id: -1,
  })
}

export const handlers = [
  http.get('/api/v1/printers/:id/kprofiles', ({ params, request }) => {
    const refused = lanRefusal(String(params.id), FEATURE)
    if (refused) return refused
    const nozzle = new URL(request.url).searchParams.get('nozzle_diameter') ?? '0.4'
    return commandAnswer(String(params.id), request, {
      profiles: state.profiles.filter((profile) => profile.nozzle_diameter === nozzle),
      nozzle_diameter: nozzle,
      extruders: state.extruders,
    } satisfies KProfileTable)
  }),
  http.get('/api/v1/printers/:id/kprofiles/notes', ({ params }) =>
    noPrinter(String(params.id)) ?? HttpResponse.json({ notes: state.notes } satisfies KProfileNotes),
  ),
  http.post('/api/v1/printers/:id/kprofiles', async ({ params, request }) => {
    const refused = lanRefusal(String(params.id), FEATURE) ?? keyRequired(request) ?? joined(String(params.id), request)
    if (refused) return refused
    const row = (await request.json()) as KProfileSet
    const slot = row.slot_id ?? 0
    if (slot === 0) {
      add(row)
      return commandAnswer(String(params.id), request, answer('K-profile added successfully'))
    }
    state.profiles = state.profiles.map((profile) =>
      profile.slot_id === slot && profile.extruder_id === (row.extruder_id ?? 0)
        ? { ...profile, name: row.name, k_value: Number.parseFloat(row.k_value).toFixed(6) }
        : profile,
    )
    return commandAnswer(String(params.id), request, answer('K-profile updated successfully'))
  }),
  http.post('/api/v1/printers/:id/kprofiles/batch', async ({ params, request }) => {
    const refused = lanRefusal(String(params.id), FEATURE) ?? keyRequired(request) ?? joined(String(params.id), request)
    if (refused) return refused
    const { profiles } = (await request.json()) as { profiles: KProfileSet[] }
    profiles.forEach(add)
    return commandAnswer(String(params.id), request, answer(`Added ${profiles.length} K-profiles`))
  }),
  http.post('/api/v1/printers/:id/kprofiles/delete', async ({ params, request }) => {
    const refused = lanRefusal(String(params.id), FEATURE) ?? keyRequired(request) ?? joined(String(params.id), request)
    if (refused) return refused
    const row = (await request.json()) as KProfileRemove
    state.profiles = state.profiles.filter(
      (profile) => !(profile.slot_id === row.slot_id && profile.extruder_id === (row.extruder_id ?? 0)),
    )
    return commandAnswer(String(params.id), request, answer('K-profile deleted successfully'))
  }),
  http.put('/api/v1/printers/:id/kprofiles/notes', async ({ params, request }) => {
    const refused = noPrinter(String(params.id))
    if (refused) return refused
    const { setting_id: key, note } = (await request.json()) as { setting_id: string; note: string }
    if (note.trim()) state.notes[key] = note
    else delete state.notes[key]
    return HttpResponse.json({ success: true, message: '' } satisfies KProfileWriteResult)
  }),
  http.delete('/api/v1/printers/:id/kprofiles/notes/:key', ({ params }) => {
    const refused = noPrinter(String(params.id))
    if (refused) return refused
    delete state.notes[String(params.key)]
    return HttpResponse.json({ success: true, message: '' } satisfies KProfileWriteResult)
  }),
]
