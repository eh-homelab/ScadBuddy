import { HttpResponse, http } from 'msw'
import type { DiscoveredPrinter, PrinterRow } from '../../api/types'
import { emitRealtime } from '../realtime'

/**
 * #3823 (plan 2026-10-16, Task 10) — the printer registry (`backend/scadbuddy/api/printers.py`):
 * one row per physical printer, keyed by serial, with its Bambuddy and LAN links. The
 * access code is taken and never returned (`has_access_code`). A LAN probe of
 * `UNREACHABLE` answers the route's one 502, as a printer that does not answer would.
 */

export const UNREACHABLE = '10.0.0.99'
export const PROBE_FAILED = 'the printer did not answer at that address'
const NO_SERIAL = 'Bambuddy did not report a serial: link it from a printer you created by serial'
const STAMP = '2026-10-18T12:00:00Z'

function row(fields: Partial<PrinterRow> & Pick<PrinterRow, 'id' | 'serial' | 'name'>): PrinterRow {
  return {
    model: 'H2C',
    bambuddy_printer_id: null,
    bambuddy_unlinked_id: null,
    bambuddy_active: true,
    lan_host: null,
    lan_cert_sha256: null,
    has_access_code: false,
    preferred_path: 'auto',
    created_at: STAMP,
    updated_at: STAMP,
    ...fields,
  }
}

/** The H2C in Bambuddy as printer 1 (`printersNow.ts`'s first card), and one by serial only. */
export function initialPrinters(): PrinterRow[] {
  return [
    row({
      id: '00000000-0000-4000-8000-000000000001',
      serial: '01P00A000000001',
      name: '3DP-31B-598',
      bambuddy_printer_id: 1,
      preferred_path: 'bambuddy',
    }),
    row({ id: '00000000-0000-4000-8000-0000000000aa', serial: '01P00A0000000AA', name: 'Garage', model: 'P1S' }),
  ]
}

/** Bambuddy's printers (`printersNow.ts`'s): 1 linked, 2 free to link to `Garage`, 3 with its serial withheld. */
export function initialDiscovered(): DiscoveredPrinter[] {
  return [
    {
      bambuddy_printer_id: 1,
      name: '3DP-31B-598',
      model: 'H2C',
      serial: '01P00A000000001',
      linked_to: '00000000-0000-4000-8000-000000000001',
      linkable: true,
      reason: null,
    },
    {
      bambuddy_printer_id: 2,
      name: 'Workshop',
      model: 'P1S',
      serial: '01P00A0000000AA',
      linked_to: null,
      linkable: true,
      reason: null,
    },
    { bambuddy_printer_id: 3, name: 'Spare', model: 'A1', serial: null, linked_to: null, linkable: false, reason: NO_SERIAL },
  ]
}

const state = { printers: initialPrinters(), discovered: initialDiscovered(), seq: 0 }

export function reset(): void {
  state.printers = initialPrinters()
  state.discovered = initialDiscovered()
  state.seq = 0
}

/** The rows as they are now, for a test that looks behind the page. */
export function printersState(): PrinterRow[] {
  return state.printers
}

function changed(id: string): void {
  emitRealtime('printer.changed', ['printers', `printer:${id}`], { printer_id: id })
}

function found(id: string): PrinterRow | undefined {
  return state.printers.find((printer) => printer.id === id)
}

function update(id: string, fields: Partial<PrinterRow>): PrinterRow {
  const next = { ...found(id)!, ...fields, updated_at: STAMP }
  state.printers = state.printers.map((printer) => (printer.id === id ? next : printer))
  changed(id)
  return next
}

const notFound = (id: string) => HttpResponse.json({ detail: `no printer ${id}` }, { status: 404 })

export const handlers = [
  http.get('/api/v1/printers', () => HttpResponse.json(state.printers)),
  http.get('/api/v1/printers/discover', () =>
    HttpResponse.json(
      state.discovered.map((printer) => ({
        ...printer,
        linked_to: state.printers.find((row) => row.bambuddy_printer_id === printer.bambuddy_printer_id)?.id ?? null,
      })),
    ),
  ),
  http.post('/api/v1/printers', async ({ request }) => {
    const body = (await request.json()) as { serial: string; name: string; model?: string | null }
    const holder = state.printers.find((printer) => printer.serial === body.serial)
    if (holder) return HttpResponse.json({ detail: `printer ${holder.id} already has this serial` }, { status: 409 })
    const created = row({
      id: `00000000-0000-4000-9000-${String(++state.seq).padStart(12, '0')}`,
      serial: body.serial,
      name: body.name,
      model: body.model ?? null,
    })
    state.printers = [...state.printers, created]
    changed(created.id)
    return HttpResponse.json(created, { status: 201 })
  }),
  http.get('/api/v1/printers/:id', ({ params }) => {
    const printer = found(String(params.id))
    return printer ? HttpResponse.json(printer) : notFound(String(params.id))
  }),
  http.patch('/api/v1/printers/:id', async ({ params, request }) => {
    const id = String(params.id)
    if (!found(id)) return notFound(id)
    const body = (await request.json()) as Partial<Pick<PrinterRow, 'name' | 'preferred_path'>>
    return HttpResponse.json(update(id, body))
  }),
  http.delete('/api/v1/printers/:id', ({ params }) => {
    const id = String(params.id)
    const printer = found(id)
    if (!printer) return notFound(id)
    if (printer.bambuddy_printer_id !== null && printer.bambuddy_printer_id !== undefined) {
      return HttpResponse.json(
        { detail: `printer ${id} is linked to Bambuddy printer ${printer.bambuddy_printer_id}; unlink it first` },
        { status: 409 },
      )
    }
    state.printers = state.printers.filter((entry) => entry.id !== id)
    changed(id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.put('/api/v1/printers/:id/bambuddy', async ({ params, request }) => {
    const id = String(params.id)
    if (!found(id)) return notFound(id)
    const { bambuddy_printer_id } = (await request.json()) as { bambuddy_printer_id: number }
    const holder = state.printers.find((printer) => printer.bambuddy_printer_id === bambuddy_printer_id)
    if (holder && holder.id !== id) {
      return HttpResponse.json(
        { detail: `printer ${holder.id} is already linked to Bambuddy printer ${bambuddy_printer_id}; unlink it first` },
        { status: 409 },
      )
    }
    return HttpResponse.json(update(id, { bambuddy_printer_id, bambuddy_unlinked_id: null }))
  }),
  http.delete('/api/v1/printers/:id/bambuddy', ({ params }) => {
    const id = String(params.id)
    const printer = found(id)
    if (!printer) return notFound(id)
    return HttpResponse.json(
      update(id, { bambuddy_printer_id: null, bambuddy_unlinked_id: printer.bambuddy_printer_id ?? null }),
    )
  }),
  http.put('/api/v1/printers/:id/lan', async ({ params, request }) => {
    const id = String(params.id)
    const printer = found(id)
    if (!printer) return notFound(id)
    const body = (await request.json()) as { host: string; access_code: string }
    if (!/^\d{8}$/.test(body.access_code)) {
      return HttpResponse.json({ detail: 'access_code: an 8-digit code' }, { status: 422 })
    }
    if (body.host === UNREACHABLE) return HttpResponse.json({ detail: PROBE_FAILED }, { status: 502 })
    return HttpResponse.json(
      update(id, { lan_host: body.host, lan_cert_sha256: 'ab'.repeat(32), has_access_code: true }),
    )
  }),
  http.delete('/api/v1/printers/:id/lan', ({ params }) => {
    const id = String(params.id)
    if (!found(id)) return notFound(id)
    return HttpResponse.json(update(id, { lan_host: null, lan_cert_sha256: null, has_access_code: false }))
  }),
]
