import type {
  FilamentOptions,
  NozzleChoice,
  NozzlePlan,
  PlannedSlot,
  PrintRunRequest,
  SlicePreview,
  SpoolOption,
  UnknownTray,
} from '../api/types'

/**
 * #2166, #2169, #2164 — the maintainer's H2C as it was on 2026-10-10: two 0.4 High Flow
 * nozzles, the Filament Track Switch fitted, and a two-colour library file in Mistletoe
 * Green and Inland Black PLA. The black is in AMS-D slot 4 with no RFID tag, so
 * Bambuddy has no spool for that tray: the dialog asks which spool it is.
 */
export const H2C_FILE = 2182
export const H2C_FILENAME = 'Mistletoe ornament.3mf'

const GREEN = '#3F8E43'
const BLACK = '#1A1A1A'
/** AMS-D (3) slot 4 (tray 3), offered under `-(ams * 16 + tray + 1)`. */
export const UNKNOWN_TRAY_SPOOL = -(3 * 16 + 3 + 1)

const green: SpoolOption = {
  spool_id: 41,
  extruder: 0,
  side: 'R',
  material: 'PLA',
  subtype: 'Matte',
  brand: 'Bambu Lab',
  color_name: 'Mistletoe Green',
  colour: GREEN,
  slicer_filament: 'GFA01',
  slicer_filament_name: 'Bambu PLA Matte @BBL H2C',
  remaining_g: 742,
  label_weight_g: 1000,
  storage_location: null,
  loaded: { printer_id: 1, printer_name: '3DP-31B-598', ams_id: 0, tray_id: 0 },
}

const inlandBlack: SpoolOption = {
  spool_id: 16,
  material: 'PLA',
  subtype: 'Basic',
  brand: 'Inland',
  color_name: 'Black',
  colour: BLACK,
  slicer_filament: 'GFL99',
  slicer_filament_name: 'Inland PLA @BBL H2C',
  remaining_g: 515,
  label_weight_g: 1000,
  storage_location: 'Shelf B',
  loaded: null,
}

const emptyBlack: SpoolOption = { ...inlandBlack, spool_id: 18, remaining_g: 0, storage_location: 'Shelf C' }

const trayItself: SpoolOption = {
  spool_id: UNKNOWN_TRAY_SPOOL,
  extruder: 1,
  side: 'L',
  material: 'PLA',
  color_name: "what's in AMS-D slot 4",
  colour: '#27272C',
  remaining_g: null,
  storage_location: null,
  loaded: { printer_id: 1, printer_name: '3DP-31B-598', ams_id: 3, tray_id: 3 },
  tray_only: true,
}

const unknownTray: UnknownTray = {
  ams_id: 3,
  tray_id: 3,
  label: 'AMS-D slot 4',
  material: 'PLA',
  colour: '#27272C',
  colour_word: 'black',
  fingerprint: 'PLA|#27272C|11',
  spool_id: UNKNOWN_TRAY_SPOOL,
  candidates: [16, 18],
}

let assigned = false

export function resetH2c(): void {
  assigned = false
}

/** "Yes": spool 16 is in AMS-D slot 4 from now on. */
export function assignH2cTray(spoolId: number): void {
  if (spoolId === inlandBlack.spool_id) assigned = true
}

export function h2cFilaments(printerId: number | null): FilamentOptions {
  const black: SpoolOption = assigned
    ? { ...inlandBlack, extruder: 1, side: 'L', loaded: { printer_id: 1, printer_name: '3DP-31B-598', ams_id: 3, tray_id: 3 } }
    : inlandBlack
  return {
    library_file_id: H2C_FILE,
    printer_id: printerId,
    printer_name: '3DP-31B-598',
    track_switch: true,
    slots: [
      { slot_id: 1, material: 'PLA', colour: GREEN, used_grams: null, colour_matches: [41] },
      { slot_id: 2, material: 'PLA', colour: BLACK, used_grams: null, colour_matches: [16, 18] },
    ],
    spools: assigned ? [green, black, emptyBlack] : [green, black, emptyBlack, trayItself],
    suggested: [
      { slot_id: 1, spool_id: 41 },
      { slot_id: 2, spool_id: 16 },
    ],
    warnings: [],
    trays: assigned ? [] : [unknownTray],
  }
}

/** The default flow on each side: what is mounted, High Flow on both. */
export const H2C_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'high_flow' },
  { size: '0.4', flow: 'high_flow' },
]

const NAMES: Record<number, { name: string; colour: string }> = {
  41: { name: 'Mistletoe Green', colour: GREEN },
  16: { name: 'Inland Black', colour: BLACK },
  18: { name: 'Inland Black', colour: BLACK },
  [UNKNOWN_TRAY_SPOOL]: { name: "what's in AMS-D slot 4", colour: '#27272C' },
}

/** As `nozzle_plan.plan_nozzles` plans PLA with PLA: one filament per nozzle. */
export function h2cPlan(request: PrintRunRequest): NozzlePlan {
  const sides = request.choices?.sides ?? {}
  const nozzles = request.choices?.nozzles ?? H2C_NOZZLES
  // Sides chosen by hand first; the rest go to the side with fewer, the right on a tie.
  const load = { L: 0, R: 0 }
  for (const side of Object.values(sides)) load[side] += 1
  const slots: PlannedSlot[] = (request.filament_plan?.slots ?? []).map((choice) => {
    const named = NAMES[choice.spool_id] ?? { name: `spool #${choice.spool_id}`, colour: '#888888' }
    const byHand = sides[String(choice.slot_id)]
    let side = byHand
    if (side === undefined) {
      side = load.R <= load.L ? 'R' : 'L'
      load[side] += 1
    }
    return {
      slot_id: choice.slot_id,
      side,
      name: named.name,
      colour: named.colour,
      material: 'PLA',
      by_hand: byHand !== undefined,
    }
  })
  const flow = (side: 0 | 1) => nozzles[side]?.flow ?? 'standard'
  const words = { standard: 'Standard', high_flow: 'High Flow' } as const
  return {
    slots,
    size: nozzles[0]?.size ?? '0.4',
    left_flow: flow(0),
    right_flow: flow(1),
    track_switch: true,
    summary: `${slots.map((slot) => `${slot.name} → ${slot.side === 'L' ? 'left' : 'right'}`).join(' · ')} · ${nozzles[0]?.size ?? '0.4'} ${words[flow(0)]}`,
  }
}

/** What a background slice of `request` comes to: one change per swap of nozzle. */
export function h2cSlice(jobId: number, request: PrintRunRequest, done: boolean): SlicePreview {
  if (!done) return { job_id: jobId, status: 'running', slots: [] }
  const plan = h2cPlan(request)
  const oneSide = new Set(plan.slots?.map((slot) => slot.side)).size === 1
  const highFlow = plan.left_flow === 'high_flow'
  return {
    job_id: jobId,
    status: 'completed',
    print_time_seconds: highFlow ? 2843 : 3961,
    filament_used_g: oneSide ? 21.7 : 15.5,
    slots: (plan.slots ?? []).map((slot) => ({
      slot_id: slot.slot_id,
      grams: slot.slot_id === 1 ? 12.4 : 3.1,
      side: slot.side,
    })),
    filament_changes: oneSide ? 24 : 1,
  }
}
