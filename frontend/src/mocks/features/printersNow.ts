import { HttpResponse, http } from 'msw'
import type { PrinterNow, PrintersNow } from '../../api/types'

/**
 * #977 — what each printer is doing now: the H2C paused on the 2026-10-01 spaghetti
 * detection, a print ScadBuddy linked, and an idle printer.
 */
export const PRINTERS_NOW: PrinterNow[] = [
  {
    printer_id: 1,
    name: '3DP-31B-598',
    activity: 'paused',
    state: 'PAUSE',
    job_name: 'Bracket',
    progress: 32,
    layer: 9,
    total_layers: 90,
    remaining_minutes: 81,
    hms: [
      { code: '0C008042', description: 'Spaghetti detected: check the print.', severity: 2 },
      { code: '0700220000020025', description: null, severity: 2 },
    ],
    awaiting_plate_clear: false,
    archive_id: 90,
    linked: false,
    bambuddy_url: 'http://bambuddy.local/archives',
    error: null,
  },
  {
    printer_id: 2,
    name: 'Workshop',
    activity: 'printing',
    state: 'RUNNING',
    job_name: 'Luna keychain',
    progress: 66,
    layer: 40,
    total_layers: 79,
    remaining_minutes: 54,
    hms: [],
    awaiting_plate_clear: false,
    archive_id: 35,
    linked: true,
    bambuddy_url: null,
    error: null,
  },
  {
    printer_id: 3,
    name: 'Spare',
    activity: 'idle',
    state: 'IDLE',
    job_name: null,
    progress: null,
    layer: null,
    total_layers: null,
    remaining_minutes: null,
    hms: [],
    awaiting_plate_clear: false,
    archive_id: null,
    linked: false,
    bambuddy_url: null,
    error: null,
  },
]

const state: { printers: PrinterNow[] } = { printers: PRINTERS_NOW }

export function reset(): void {
  state.printers = PRINTERS_NOW
}

/** Replace what the printers report, for a test. */
export function setPrintersNow(printers: PrinterNow[]): void {
  state.printers = printers
}

export const handlers = [
  http.get('/api/v1/print/printers/now', () =>
    HttpResponse.json({ printers: state.printers } satisfies PrintersNow),
  ),
]
