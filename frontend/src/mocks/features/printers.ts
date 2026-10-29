import { HttpResponse, http } from 'msw'
import type {
  DiscoveredPrinter,
  PrinterRow,
  PrinterSyncEntry,
  PrinterSyncResult,
  VirtualPrinter,
  VirtualPrinterCreate,
  VirtualPrinterDiagnostic,
} from '../../api/types'
import { emitRealtime } from '../realtime'

/**
 * #3823 (plan 2026-10-16, Task 10) — the printer registry (`backend/scadbuddy/api/printers.py`):
 * one row per physical printer, keyed by serial, with its Bambuddy and LAN links. The
 * access code is taken and never returned (`has_access_code`). A LAN probe of
 * `UNREACHABLE` answers the route's one 502, as a printer that does not answer would.
 */

export const UNREACHABLE = '10.0.0.99'
export const PROBE_FAILED =
  'the printer did not answer at that address, or did not take the access code: check both'
const NO_SERIAL = 'Bambuddy did not report a serial: link it from a printer you created by serial'
export const LAN_WITHHELD =
  'Bambuddy withholds the access code from API keys while its auth is on: link the LAN in Settings > Printers'
const STAMP = '2026-10-18T12:00:00Z'

function row(fields: Partial<PrinterRow> & Pick<PrinterRow, 'id' | 'serial' | 'name'>): PrinterRow {
  return {
    kind: 'physical',
    model: 'H2C',
    bambuddy_printer_id: null,
    bambuddy_unlinked_id: null,
    bambuddy_active: true,
    lan_host: null,
    lan_cert_sha256: null,
    has_access_code: false,
    preferred_path: 'auto',
    lan_move_pending: false,
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
      // What Bambuddy reported at the last sync (#4747 item 1): its name matches, and its IP
      // is one this row has no LAN link to.
      bambuddy_view: {
        seen_at: STAMP,
        name: '3DP-31B-598',
        model: 'H2C',
        ip_address: '10.0.0.10',
        is_active: true,
        nozzle_count: 2,
      },
    }),
    row({ id: '00000000-0000-4000-8000-0000000000aa', serial: '01P00A0000000AA', name: 'Garage' }),
  ]
}

/**
 * Bambuddy's printers, named as `printersNow.ts` and `fixtures.targets` name them: 1 linked,
 * 2 free to link to `Garage` (the same serial), 3 with its serial withheld.
 */
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
      name: '3DP-77A-114',
      model: 'H2C',
      serial: '01P00A0000000AA',
      linked_to: null,
      linkable: true,
      reason: null,
    },
    { bambuddy_printer_id: 3, name: 'Spare', model: 'A1', serial: null, linked_to: null, linkable: false, reason: NO_SERIAL },
  ]
}

/**
 * #4747 item 4 — Bambuddy's virtual printers (`/printers/virtual`): 1 is prod's queue-mode
 * H2C forwarding to printer 1's row. `auth_enabled` is off, as prod runs; set it on with
 * `setVirtualAuth(true)` and every write answers Bambuddy's admin-only scope problem.
 */
export function initialVirtualPrinters(): VirtualPrinter[] {
  return [
    {
      id: 1,
      name: 'H2C Virtual',
      enabled: true,
      mode: 'queue',
      model: 'O1C',
      model_name: 'H2C',
      access_code_set: true,
      serial: '09400A391800001',
      target_printer_id: 1,
      target: '00000000-0000-4000-8000-000000000001',
      printer_id: null,
      auto_dispatch: true,
      queue_force_color_match: false,
      save_ams_mapping: false,
      gcode_injection: false,
      tailscale_disabled: true,
      bind_ip: '192.168.10.9',
      remote_interface_ip: null,
      status: { running: true, pending_files: 0 },
    },
  ]
}

export const VP_MODELS: Record<string, string> = { O1C: 'H2C', C12: 'P1S' }

const state = {
  printers: initialPrinters(),
  discovered: initialDiscovered(),
  seq: 0,
  virtual: initialVirtualPrinters(),
  virtualAuth: false,
  /** Every VP write's body as sent, for a test that checks what left the page. */
  virtualWrites: [] as unknown[],
}

export function reset(): void {
  state.printers = initialPrinters()
  state.discovered = initialDiscovered()
  state.seq = 0
  state.virtual = initialVirtualPrinters()
  state.virtualAuth = false
  state.virtualWrites = []
}

export function setVirtualAuth(on: boolean): void {
  state.virtualAuth = on
}

/** The virtual printers as they are now, for a test that sets one up. */
export function virtualState(): VirtualPrinter[] {
  return state.virtual
}

export function virtualWrites(): unknown[] {
  return state.virtualWrites
}

const ADMIN_ONLY = () =>
  HttpResponse.json(
    {
      type: 'https://scadbuddy.dev/problems/bambuddy/scope',
      title: 'Bambuddy API key scope',
      status: 409,
      detail:
        "Bambuddy refused the API key when asked to change the virtual printer. No Bambuddy API key can change its settings while Bambuddy's auth is on: turn its auth off, or make the change in Bambuddy",
      required_scope: 'Settings (admin only)',
    },
    { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
  )

function bambuddyTarget(target: string | null | undefined): number | null {
  return target ? (state.printers.find((row) => row.id === target)?.bambuddy_printer_id ?? null) : null
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

/** The id the mocked sync gives Bambuddy's virtual printer 1's row. */
export const VIRTUAL_ID = '00000000-0000-4000-8000-0000000000f1'

/**
 * #4439, #4747 — `POST /printers/sync-bambuddy` as the backend runs it over `initialDiscovered`:
 * 1 already linked, 2 linked to `Garage` by its serial (the code withheld, auth on), 3 skipped
 * (no serial), and Bambuddy's virtual printer 1 imported, forwarding to printer 1's row. A dry
 * run plans the same and writes nothing; a second run finds everything already linked.
 */
function sync(dryRun: boolean): PrinterSyncResult {
  const entries: PrinterSyncEntry[] = []
  for (const printer of state.discovered) {
    const linked = state.printers.find((row) => row.bambuddy_printer_id === printer.bambuddy_printer_id)
    const holder = state.printers.find((row) => row.serial === printer.serial)
    const base = { kind: 'physical' as const, name: printer.name, bambuddy_printer_id: printer.bambuddy_printer_id }
    if (linked) {
      entries.push({ ...base, printer_id: linked.id, action: 'already_linked', lan: linked.lan_host ? 'kept' : 'none', changed: false, target_missing: false })
    } else if (!printer.serial) {
      entries.push({ ...base, action: 'skipped', reason: NO_SERIAL, changed: false, target_missing: false })
    } else if (holder) {
      entries.push({
        ...base,
        printer_id: holder.id,
        action: 'linked',
        lan: dryRun ? 'would_try' : 'withheld',
        lan_reason: dryRun ? null : LAN_WITHHELD,
        changed: !dryRun,
        target_missing: false,
      })
      if (!dryRun) update(holder.id, { bambuddy_printer_id: printer.bambuddy_printer_id, lan_sync_error: LAN_WITHHELD })
    }
  }
  const target = state.printers.find((row) => row.bambuddy_printer_id === 1)
  const virtual = state.printers.find((row) => row.bambuddy_vp_id === 1)
  entries.push({
    kind: 'bambuddy_virtual',
    name: 'H2C Virtual',
    bambuddy_vp_id: 1,
    printer_id: virtual?.id ?? (dryRun ? null : VIRTUAL_ID),
    action: virtual ? 'already_linked' : 'created',
    forwards_to: target?.id ?? null,
    changed: !virtual && !dryRun,
    target_missing: false,
  })
  if (!virtual && !dryRun) {
    state.printers = [
      ...state.printers,
      row({
        id: VIRTUAL_ID,
        serial: '09400A391800001',
        name: 'H2C Virtual',
        kind: 'bambuddy_virtual',
        bambuddy_vp_id: 1,
        vp_mode: 'queue',
        forwards_to: target?.id ?? null,
        preferred_path: 'bambuddy',
        bambuddy_view: {
          seen_at: STAMP,
          name: 'H2C Virtual',
          model: 'H2C',
          model_code: 'O1C',
          mode: 'queue',
          enabled: true,
          target_printer_id: 1,
          bind_ip: '0.0.0.0',
          remote_interface_ip: '',
          access_code_set: true,
          running: true,
          pending_files: 0,
        },
      }),
    ]
    changed(VIRTUAL_ID)
  }
  const count = (action: PrinterSyncEntry['action']) => entries.filter((entry) => entry.action === action).length
  return {
    dry_run: dryRun,
    entries,
    virtual_printers_error: null,
    created: count('created'),
    linked: count('linked'),
    already_linked: count('already_linked'),
    skipped: count('skipped'),
    conflicts: count('conflict'),
    lan_linked: entries.filter((entry) => entry.lan === 'linked' || entry.lan === 'relinked').length,
    lan_failed: entries.filter((entry) => entry.lan === 'failed' || entry.lan === 'withheld').length,
    target_missing: entries.filter((entry) => entry.target_missing).length,
  }
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
  http.post('/api/v1/printers/sync-bambuddy', ({ request }) =>
    HttpResponse.json(sync(new URL(request.url).searchParams.get('dry_run') === 'true')),
  ),
  http.get('/api/v1/printers/virtual', () =>
    HttpResponse.json({ printers: state.virtual, models: VP_MODELS, auth_enabled: state.virtualAuth }),
  ),
  http.get('/api/v1/printers/virtual/:vp/diagnostic', ({ params }) => {
    const vp = state.virtual.find((entry) => entry.id === Number(params.vp))
    if (!vp) return HttpResponse.json({ detail: 'no such virtual printer' }, { status: 404 })
    const result: VirtualPrinterDiagnostic = {
      vp_id: vp.id,
      vp_name: vp.name,
      mode: vp.mode,
      overall: 'warnings',
      checks: [
        { id: 'enabled', status: vp.enabled ? 'pass' : 'fail', params: {} },
        { id: 'port_mqtt', status: 'pass', params: { port: 8883 } },
        { id: 'certificate', status: 'warn', params: {} },
      ],
    }
    return HttpResponse.json(result)
  }),
  http.post('/api/v1/printers/virtual', async ({ request }) => {
    if (state.virtualAuth) return ADMIN_ONLY()
    const body = (await request.json()) as VirtualPrinterCreate
    state.virtualWrites.push(body)
    const { access_code, target, ...rest } = body
    const created = {
      ...initialVirtualPrinters()[0],
      ...rest,
      id: Math.max(0, ...state.virtual.map((vp) => vp.id)) + 1,
      model_name: body.model ? (VP_MODELS[body.model] ?? null) : null,
      target: target ?? null,
      target_printer_id: bambuddyTarget(target),
      access_code_set: Boolean(access_code) || Boolean(target),
      serial: null,
      status: { running: Boolean(body.enabled), pending_files: 0 },
    } as VirtualPrinter
    state.virtual = [...state.virtual, created]
    return HttpResponse.json(created)
  }),
  http.put('/api/v1/printers/virtual/:vp', async ({ params, request }) => {
    if (state.virtualAuth) return ADMIN_ONLY()
    const body = (await request.json()) as Partial<VirtualPrinterCreate>
    state.virtualWrites.push(body)
    const id = Number(params.vp)
    const { access_code, ...rest } = body
    let next: VirtualPrinter | undefined
    state.virtual = state.virtual.map((vp) => {
      if (vp.id !== id) return vp
      next = {
        ...vp,
        ...rest,
        ...('target' in body ? { target: body.target ?? null, target_printer_id: bambuddyTarget(body.target) } : {}),
        access_code_set: vp.access_code_set || Boolean(access_code),
      } as VirtualPrinter
      return next
    })
    return next ? HttpResponse.json(next) : HttpResponse.json({ detail: 'no such virtual printer' }, { status: 404 })
  }),
  http.delete('/api/v1/printers/virtual/:vp', ({ params }) => {
    if (state.virtualAuth) return ADMIN_ONLY()
    const id = Number(params.vp)
    state.virtual = state.virtual.filter((vp) => vp.id !== id)
    return HttpResponse.json({ deleted: id })
  }),
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
    // As `LanLink` (backend/scadbuddy/printers/models.py): any format, no control character.
    // eslint-disable-next-line no-control-regex
    if (body.access_code.length > 128 || !/^[^\x00-\x1f\x7f]+$/.test(body.access_code)) {
      return HttpResponse.json(
        { detail: 'access_code: 1 to 128 characters, none of them a control character' },
        { status: 422 },
      )
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
