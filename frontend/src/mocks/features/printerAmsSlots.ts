import { HttpResponse, http } from 'msw'
import type { AmsUnitView, AmsView, SlotConfigureBody, SlotPresetBody, SlotView } from '../../api/types'

/**
 * #7153 — a printer's AMS slots through Bambuddy (`backend/scadbuddy/api/printer_slots.py`).
 * Printer 1's row (`printers.ts`, the H2C linked to Bambuddy printer 1) has AMS-A with three
 * spools and an empty slot, and an empty external holder; any other printer is not linked, a 409,
 * as the route answers. `setAmsPrinting(true)` makes the printer print, so every printer write is
 * the route's 409 `printer_busy`; `setAmsAdminOnly(true)` makes a label or saved-preset write
 * Bambuddy's admin-only scope problem. A configure from a spool sends its type's ScadBuddy preset
 * (`sb-<id>`) by its mirror's Bambuddy local preset id, `141`, so the route names its source `local`.
 */

export const H2C = '00000000-0000-4000-8000-000000000001'
const SCOPE_PROBLEM = 'https://scadbuddy.dev/problems/bambuddy-scope'

function slot(tray_id: number, values: Partial<SlotView>): SlotView {
  return {
    tray_id,
    label: `AMS-A slot ${tray_id + 1}`,
    filled: true,
    loaded: false,
    material: 'PLA',
    colour: '#FFFFFFFF',
    preset_id: 'GFA00',
    preset_name: 'Bambu PLA Basic',
    nozzle_temp_min: 190,
    nozzle_temp_max: 230,
    saved_preset: null,
    loadable: true,
    refreshable: true,
    ...values,
  }
}

function seedUnits(): AmsUnitView[] {
  return [
    {
      ams_id: 0,
      unit: 'AMS-A',
      label: null,
      place_id: '00000000-0000-4000-8000-00000000a0a0',
      place_name: 'H2C AMS-A',
      slots: [
        slot(0, { colour: '#000000FF', loaded: true }),
        slot(1, {
          material: 'PETG',
          colour: '#00AE42FF',
          preset_id: 'GFG00',
          preset_name: 'Bambu PETG Basic',
          saved_preset: { preset_id: 'GFSG00', preset_name: 'Bambu PETG Basic @BBL H2C', tray_info_idx: 'GFG00' },
        }),
        slot(2, { colour: '#E83100FF', preset_name: 'Bambu PLA Matte' }),
        slot(3, {
          filled: false,
          material: null,
          colour: null,
          preset_id: null,
          preset_name: null,
          nozzle_temp_min: null,
          nozzle_temp_max: null,
        }),
      ],
    },
    {
      ams_id: 255,
      unit: 'External',
      label: null,
      place_id: null,
      place_name: null,
      slots: [0, 1].map((tray_id) => ({
        ...slot(tray_id, {
          filled: false,
          material: null,
          colour: null,
          preset_id: null,
          preset_name: null,
          nozzle_temp_min: null,
          nozzle_temp_max: null,
          refreshable: false,
        }),
        label: `External slot ${tray_id + 1}`,
      })),
    },
  ]
}

const state = {
  units: seedUnits(),
  printing: false,
  adminOnly: false,
  sent: [] as string[],
  presets: [] as SlotPresetBody[],
  trayNow: undefined as number | null | undefined,
}

export function reset(): void {
  state.units = seedUnits()
  state.printing = false
  state.adminOnly = false
  state.sent = []
  state.presets = []
  state.trayNow = undefined
}

export function setAmsPrinting(on: boolean): void {
  state.printing = on
}

/** Overrides the view's `tray_now` (`null` is a status that did not say); `undefined` derives it. */
export function setAmsTrayNow(value: number | null | undefined): void {
  state.trayNow = value
}

export function setAmsAdminOnly(on: boolean): void {
  state.adminOnly = on
}

/** The saved-preset bodies the mock was sent, in order. */
export function amsPresetsSaved(): SlotPresetBody[] {
  return state.presets
}

/** What the mock was asked to do, in order (`load 0/1`, `configure 0/3`, …). */
export function amsSent(): string[] {
  return state.sent
}

function view(): AmsView {
  return {
    printer_id: H2C,
    name: 'H2C',
    connected: true,
    state: state.printing ? 'RUNNING' : 'IDLE',
    printing: state.printing,
    ams_busy: false,
    tray_now: state.trayNow !== undefined ? state.trayNow : state.units[0]!.slots.find((s) => s.loaded) ? 0 : 255,
    units: state.units,
  }
}

const adminOnly = (what: string) =>
  HttpResponse.json(
    {
      type: SCOPE_PROBLEM,
      title: 'Bambuddy API key scope',
      status: 409,
      detail: `Bambuddy refused the API key when asked to ${what} of printer 1. No Bambuddy API key can change a printer's slot presets or AMS labels while Bambuddy's auth is on: turn its auth off, or make the change in Bambuddy`,
      required_scope: 'Printers (admin only)',
    },
    { status: 409 },
  )

const notLinked = () =>
  HttpResponse.json(
    { detail: 'Garage is not linked to a Bambuddy printer, so ScadBuddy cannot reach its AMS' },
    { status: 409 },
  )

const busy = (action: string) =>
  HttpResponse.json(
    { status: 409, detail: `H2C is printing (RUNNING); ${action} the slot once the print has ended`, code: 'printer_busy' },
    { status: 409 },
  )

function find(ams: number, tray: number): SlotView | undefined {
  return state.units.find((u) => u.ams_id === ams)?.slots.find((s) => s.tray_id === tray)
}

export const handlers = [
  http.get('/api/v1/printers/:id/ams', ({ params }) => (params.id === H2C ? HttpResponse.json(view()) : notLinked())),
  http.post('/api/v1/printers/:id/trays/:ams/:tray/:action', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    const action = String(params.action)
    const ams = Number(params.ams)
    const tray = Number(params.tray)
    if (state.printing) return busy(action)
    const target = find(ams, tray)
    if (!target) return HttpResponse.json({ detail: 'no such slot', code: 'no_slot' }, { status: 409 })
    state.sent.push(`${action} ${ams}/${tray}`)
    if (action === 'configure') {
      const body = (await request.json()) as SlotConfigureBody
      const colour = (body.colour ?? '#808080').toUpperCase()
      const fromSpool = body.spool_id != null
      Object.assign(target, {
        filled: true,
        material: body.material ?? 'PLA',
        colour: colour.length === 7 ? `${colour}FF` : colour,
        preset_id: body.preset_id ?? (fromSpool ? '141' : ''),
        preset_name: body.preset_name ?? (fromSpool ? 'Shop PLA' : (body.material ?? 'PLA')),
        nozzle_temp_min: body.nozzle_temp_min ?? 190,
        nozzle_temp_max: body.nozzle_temp_max ?? 230,
      })
      return HttpResponse.json({
        ams_id: ams,
        tray_id: tray,
        spool_id: body.spool_id ?? null,
        material: target.material,
        colour: target.colour,
        preset_id: target.preset_id,
        preset_name: target.preset_name,
        preset_source: fromSpool && body.preset_id == null ? 'local' : null,
        nozzle_temp_min: target.nozzle_temp_min,
        nozzle_temp_max: target.nozzle_temp_max,
        nozzle_diameter: body.nozzle_diameter ?? '0.4',
        cali_idx: null,
        k_value: null,
      })
    }
    if (action === 'load') {
      if (!target.filled) return HttpResponse.json({ detail: 'holds no spool', code: 'tray_empty' }, { status: 409 })
      state.units.forEach((u) => u.slots.forEach((s) => (s.loaded = false)))
      target.loaded = true
    }
    if (action === 'unload') {
      if (!target.loaded) return HttpResponse.json({ detail: 'Bambuddy reported a conflict: No hotend is loaded from that slot' }, { status: 409 })
      target.loaded = false
    }
    if (action === 'reset') {
      Object.assign(target, { material: null, colour: null, preset_id: null, preset_name: null, saved_preset: null })
    }
    return HttpResponse.json({ ams_id: ams, tray_id: tray, action, message: `Sent ${action}` })
  }),
  http.put('/api/v1/printers/:id/trays/:ams/:tray/preset', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly('save the preset of a slot')
    const body = (await request.json()) as SlotPresetBody
    state.presets.push(body)
    const target = find(Number(params.ams), Number(params.tray))
    const saved = { preset_id: body.preset_id, preset_name: body.preset_name, tray_info_idx: body.tray_info_idx ?? null }
    if (target) target.saved_preset = saved
    return HttpResponse.json(saved)
  }),
  http.delete('/api/v1/printers/:id/trays/:ams/:tray/preset', ({ params }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly('clear the preset of a slot')
    const target = find(Number(params.ams), Number(params.tray))
    if (target) target.saved_preset = null
    return new HttpResponse(null, { status: 204 })
  }),
  http.put('/api/v1/printers/:id/ams/:ams/label', async ({ params, request }) => {
    if (params.id !== H2C) return notLinked()
    if (state.adminOnly) return adminOnly('label AMS 0')
    const { label } = (await request.json()) as { label: string }
    const unit = state.units.find((u) => u.ams_id === Number(params.ams))
    if (unit) Object.assign(unit, { label, place_name: label })
    return HttpResponse.json({ ams_id: Number(params.ams), label, place: null, place_problem: null })
  }),
  http.delete('/api/v1/printers/:id/ams/:ams/label', ({ params }) => {
    if (params.id !== H2C) return notLinked()
    const unit = state.units.find((u) => u.ams_id === Number(params.ams))
    if (unit) Object.assign(unit, { label: null, place_name: `H2C ${unit.unit}` })
    return HttpResponse.json({ ams_id: Number(params.ams), label: null, place: null, place_problem: null })
  }),
]
