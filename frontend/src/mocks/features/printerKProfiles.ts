import { HttpResponse, http } from 'msw'
import type { KProfile, KProfileRemove, KProfileSet, KProfileWriteResult } from '../../api/types'

/**
 * #7154 — a printer's K-profiles through Bambuddy (`backend/scadbuddy/api/printer_kprofiles.py`).
 * Printer 1's row (`printers.ts`, the dual-nozzle H2C linked to Bambuddy printer 1) holds two
 * profiles at 0.4 mm and one note; any other printer is not linked, a 409, as the route
 * answers. `setKProfileAdminOnly(true)` makes every write Bambuddy's admin-only scope problem;
 * `setKProfileUnconfirmed(true)` makes the printer leave every write unconfirmed.
 */

export const H2C = '00000000-0000-4000-8000-000000000001'
const SCOPE_PROBLEM = 'https://scadbuddy.dev/problems/bambuddy-scope'

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
  adminOnly: false,
  unconfirmed: false,
  nextSlot: 3,
}

export function reset(): void {
  state.profiles = seedProfiles()
  state.notes = { PFUS1: 'Calibrated on the textured plate' }
  state.adminOnly = false
  state.unconfirmed = false
  state.nextSlot = 3
}

export function setKProfileAdminOnly(on: boolean): void {
  state.adminOnly = on
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

const notLinked = () =>
  HttpResponse.json(
    { detail: 'This printer is not linked to a Bambuddy printer, so ScadBuddy cannot reach its K-profiles' },
    { status: 409 },
  )

const adminOnly = () =>
  HttpResponse.json(
    {
      type: SCOPE_PROBLEM,
      title: 'Bambuddy API key scope',
      status: 409,
      detail:
        "Bambuddy refused the API key when asked to set a K-profile on printer 1. No Bambuddy API key can change a printer's K-profiles or their notes while Bambuddy's auth is on: turn its auth off, or make the change in Bambuddy",
      required_scope: 'K-profiles (admin only)',
    },
    { status: 409 },
  )

const answer = (message: string): KProfileWriteResult => ({
  success: true,
  confirmed: !state.unconfirmed,
  message: state.unconfirmed ? 'K-profile sent; the printer did not confirm it' : message,
})

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
    if (params.id !== H2C) return notLinked()
    const nozzle = new URL(request.url).searchParams.get('nozzle_diameter') ?? '0.4'
    return HttpResponse.json({
      profiles: state.profiles.filter((profile) => profile.nozzle_diameter === nozzle),
      nozzle_diameter: nozzle,
    })
  }),
  http.get('/api/v1/printers/:id/kprofiles/notes', ({ params }) =>
    params.id === H2C ? HttpResponse.json({ notes: state.notes }) : notLinked(),
  ),
  http.post('/api/v1/printers/:id/kprofiles', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly()
    const row = (await request.json()) as KProfileSet
    const slot = row.slot_id ?? 0
    if (slot === 0) {
      add(row)
      return HttpResponse.json(answer('K-profile added successfully'))
    }
    state.profiles = state.profiles.map((profile) =>
      profile.slot_id === slot && profile.extruder_id === (row.extruder_id ?? 0)
        ? { ...profile, name: row.name, k_value: Number.parseFloat(row.k_value).toFixed(6) }
        : profile,
    )
    return HttpResponse.json(answer('K-profile updated successfully'))
  }),
  http.post('/api/v1/printers/:id/kprofiles/batch', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly()
    const { profiles } = (await request.json()) as { profiles: KProfileSet[] }
    profiles.forEach(add)
    return HttpResponse.json(answer(`Added ${profiles.length} K-profiles`))
  }),
  http.post('/api/v1/printers/:id/kprofiles/delete', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly()
    const row = (await request.json()) as KProfileRemove
    state.profiles = state.profiles.filter(
      (profile) => !(profile.slot_id === row.slot_id && profile.extruder_id === (row.extruder_id ?? 0)),
    )
    return HttpResponse.json(answer('K-profile deleted successfully'))
  }),
  http.put('/api/v1/printers/:id/kprofiles/notes', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly()
    const { setting_id: key, note } = (await request.json()) as { setting_id: string; note: string }
    if (note.trim()) state.notes[key] = note
    else delete state.notes[key]
    return HttpResponse.json({ success: true, confirmed: null, message: note.trim() ? 'Note saved' : 'Note deleted' })
  }),
  http.delete('/api/v1/printers/:id/kprofiles/notes/:key', ({ params }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly()
    delete state.notes[String(params.key)]
    return HttpResponse.json({ success: true, confirmed: null, message: 'Note deleted' })
  }),
]
