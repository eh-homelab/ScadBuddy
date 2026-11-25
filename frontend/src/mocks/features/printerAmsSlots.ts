import { HttpResponse, http } from 'msw'
import type {
  AmsLabelSaved,
  AmsUnitView,
  AmsView,
  SavedSlotPreset,
  SlotCommandSent,
  SlotConfigureBody,
  SlotConfigured,
  SlotPresetBody,
  SlotView,
} from '../../api/types'
import { answersAtOnce, commandAnswer, joined, lanRefusal } from './printerCommands'
import { printersState } from './printers'

/**
 * #7153, #7276 — a printer's AMS slots over ScadBuddy's LAN link
 * (`backend/scadbuddy/api/printer_slots.py`). The printer's last report has AMS-A with three
 * spools and an empty slot, and an empty external holder. A printer `lan_linked` is false on is
 * refused 409 `not_lan_linked` (`printerCommands.ts` `lanRefusal`), so a test links printer 1's
 * row first. `setAmsPrinting(true)` makes the printer print, so every printer write is the route's
 * 409 `printer_busy`; `setAmsUnconfirmed(true)` makes the printer leave every write
 * unacknowledged; `printerCommands.ts` `setPrinterCommandSlow` makes every write a 202 to follow.
 * The saved presets and the labels are ScadBuddy's own and need only the printer's row. A
 * configure from a spool sends its type's preset's filament id, `GFL99`.
 */

export const H2C = '00000000-0000-4000-8000-000000000001'
const FEATURE = 'AMS'
const SENT = 'sent'
const UNCONFIRMED = 'sent; the printer did not acknowledge it in time'

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
  unconfirmed: false,
  sent: [] as string[],
  presets: [] as SlotPresetBody[],
  trayNow: undefined as number | null | undefined,
}

export function reset(): void {
  state.units = seedUnits()
  state.printing = false
  state.unconfirmed = false
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

/** Marks only AMS-A's tray `tray` loaded, or none with `null`. */
export function setAmsLoaded(tray: number | null): void {
  state.units.forEach((u) => u.slots.forEach((s) => (s.loaded = u.ams_id === 0 && s.tray_id === tray)))
}

/** Every printer write answers that the printer did not acknowledge it in time. */
export function setAmsUnconfirmed(on: boolean): void {
  state.unconfirmed = on
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

/** The backend's `ams_slots.py` `GENERIC_FILAMENT_IDS`, as Bambuddy's configure route maps them. */
const GENERIC_FILAMENT_IDS: Record<string, string> = {
  PLA: 'GFL99',
  PETG: 'GFG99',
  ABS: 'GFB99',
  ASA: 'GFB98',
  PC: 'GFC99',
  PA: 'GFN99',
  NYLON: 'GFN99',
  TPU: 'GFU99',
  PVA: 'GFS99',
  HIPS: 'GFS98',
  'PLA-CF': 'GFL98',
  'PETG-CF': 'GFG98',
  'PA-CF': 'GFN98',
  'PETG HF': 'GFG96',
  'PLA SILK': 'GFL96',
  'PLA HIGH SPEED': 'GFL95',
  PCTG: 'GFG97',
  PE: 'GFP99',
  PP: 'GFP97',
}

/** `ams_slots.py` `generic_filament_id`: the whole name, then its first word; "" when none is known. */
function genericFilamentId(material: string): string {
  const name = material.toUpperCase().trim()
  return GENERIC_FILAMENT_IDS[name] ?? GENERIC_FILAMENT_IDS[name.split('-')[0]!.split(' ')[0]!] ?? ''
}

const problem = (status: number, detail: string, code?: string) =>
  HttpResponse.json({ title: detail, status, detail, ...(code ? { code } : {}) }, { status })

/** A saved preset or a label is ScadBuddy's own: the printer needs only a row (`_owner`). */
const noPrinter = (id: string) =>
  printersState().some((row) => row.id === id) ? undefined : problem(404, `no printer ${id}`)

function find(ams: number, tray: number): SlotView | undefined {
  return state.units.find((u) => u.ams_id === ams)?.slots.find((s) => s.tray_id === tray)
}

export const handlers = [
  http.get('/api/v1/printers/:id/ams', ({ params }) => lanRefusal(String(params.id), FEATURE) ?? HttpResponse.json(view())),
  http.post('/api/v1/printers/:id/trays/:ams/:tray/:action', async ({ params, request }) => {
    const id = String(params.id)
    const refused = lanRefusal(id, FEATURE) ?? joined(id, request)
    if (refused) return refused
    const action = String(params.action) as SlotCommandSent['action']
    const ams = Number(params.ams)
    const tray = Number(params.tray)
    const label = ams === 255 ? `External slot ${tray + 1}` : `AMS-A slot ${tray + 1}`
    if (state.printing) {
      return problem(409, `H2C is printing (RUNNING); ${action} the slot once the print has ended`, 'printer_busy')
    }
    const target = find(ams, tray)
    if (!target) return problem(409, `H2C reports no ${label}`, 'no_slot')
    const message = state.unconfirmed ? UNCONFIRMED : SENT
    if (action === 'configure') {
      const body = (await request.json()) as SlotConfigureBody
      const colour = (body.colour ?? '#808080').toUpperCase()
      const fromSpool = body.spool_id != null
      Object.assign(target, {
        filled: true,
        material: body.material ?? 'PLA',
        colour: colour.length === 7 ? `${colour}FF` : colour,
        // With no preset, the material's generic filament id, "" for one it has none for
        // (`ams_slots.py` `configure_values`).
        preset_id: body.preset_id || genericFilamentId(body.material ?? 'PLA'),
        preset_name: body.preset_name ?? (fromSpool ? 'Shop PLA' : (body.material ?? 'PLA')),
        nozzle_temp_min: body.nozzle_temp_min ?? 190,
        nozzle_temp_max: body.nozzle_temp_max ?? 230,
      })
      state.sent.push(`${action} ${ams}/${tray}`)
      return commandAnswer(id, request, {
        ams_id: ams,
        tray_id: tray,
        spool_id: body.spool_id ?? null,
        material: target.material!,
        colour: target.colour!,
        preset_id: target.preset_id!,
        preset_name: target.preset_name!,
        preset_source: null,
        nozzle_temp_min: target.nozzle_temp_min!,
        nozzle_temp_max: target.nozzle_temp_max!,
        nozzle_diameter: body.nozzle_diameter ?? '0.4',
        cali_idx: null,
        k_value: null,
        message,
      } satisfies SlotConfigured)
    }
    if (action === 'load') {
      if (!target.filled) return problem(409, `H2C's ${label} holds no spool to load`, 'tray_empty')
      if (target.loaded) return problem(409, `H2C is already loaded from ${label}`, 'already_loaded')
      state.units.forEach((u) => u.slots.forEach((s) => (s.loaded = false)))
      target.loaded = true
    }
    if (action === 'unload') {
      if (!target.loaded) return problem(409, `H2C is not loaded from ${label}`, 'not_loaded')
      target.loaded = false
    }
    // As `ams_slots.any_loaded`: a slot shows `loaded`, or `tray_now` names a slot.
    const trayNow = view().tray_now
    const anyLoaded = (trayNow != null && trayNow !== 255) || state.units.some((u) => u.slots.some((s) => s.loaded))
    if (action === 'refresh' && anyLoaded) {
      return problem(409, 'H2C has filament loaded; unload it before the AMS can read a tag again', 'filament_loaded')
    }
    if (action === 'reset') {
      Object.assign(target, { material: null, colour: null, preset_id: null, preset_name: null })
      // As the route: only a reset the printer acknowledged in this answer clears the saved preset (P43).
      if (message === SENT && answersAtOnce()) target.saved_preset = null
    }
    state.sent.push(`${action} ${ams}/${tray}`)
    return commandAnswer(id, request, { ams_id: ams, tray_id: tray, action, message } satisfies SlotCommandSent)
  }),
  http.put('/api/v1/printers/:id/trays/:ams/:tray/preset', async ({ params, request }) => {
    const refused = noPrinter(String(params.id))
    if (refused) return refused
    const body = (await request.json()) as SlotPresetBody
    state.presets.push(body)
    const target = find(Number(params.ams), Number(params.tray))
    const saved = {
      preset_id: body.preset_id,
      preset_name: body.preset_name,
      tray_info_idx: body.tray_info_idx ?? null,
    } satisfies SavedSlotPreset
    if (target) target.saved_preset = saved
    return HttpResponse.json(saved)
  }),
  http.delete('/api/v1/printers/:id/trays/:ams/:tray/preset', ({ params }) => {
    const refused = noPrinter(String(params.id))
    if (refused) return refused
    const target = find(Number(params.ams), Number(params.tray))
    if (target) target.saved_preset = null
    return new HttpResponse(null, { status: 204 })
  }),
  http.put('/api/v1/printers/:id/ams/:ams/label', async ({ params, request }) => {
    const refused = noPrinter(String(params.id))
    if (refused) return refused
    const { label } = (await request.json()) as { label: string }
    const unit = state.units.find((u) => u.ams_id === Number(params.ams))
    if (unit) Object.assign(unit, { label, place_name: label })
    return HttpResponse.json({ ams_id: Number(params.ams), label, place: null, place_problem: null } satisfies AmsLabelSaved)
  }),
  http.delete('/api/v1/printers/:id/ams/:ams/label', ({ params }) => {
    const refused = noPrinter(String(params.id))
    if (refused) return refused
    const unit = state.units.find((u) => u.ams_id === Number(params.ams))
    if (unit) Object.assign(unit, { label: null, place_name: `H2C ${unit.unit}` })
    return HttpResponse.json({ ams_id: Number(params.ams), label: null, place: null, place_problem: null } satisfies AmsLabelSaved)
  }),
]
