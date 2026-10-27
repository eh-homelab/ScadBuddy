import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { DiscoveredPrinter, PrinterRow, PrinterSyncEntry, PrinterSyncResult, PrintPath } from '../../api/types'
import { PRINTER_TOPICS as TOPICS, registryNews, syncSummary } from '../../lib/printerRegistry'
import { useAsync } from '../../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { ChoiceSelect, LabeledInput, LoadingLines } from './form'
import { Spinner } from '../ui/spinner'
import { VirtualPrintersPanel } from './VirtualPrinters'

/**
 * Settings → Printers (#3823, plan 2026-10-16, Task 10): the printer registry, one row per
 * physical printer, keyed by its serial (`backend/scadbuddy/api/printers.py`). A row links
 * a Bambuddy printer (picked from `/printers/discover`; one whose serial Bambuddy
 * withholds can only be linked from a row created by serial) and a LAN connection (host
 * and the printer's access code, tested on the printer before it is stored). The
 * code is sent once and never shown again: the row says only whether one is stored. The
 * path says how a print reaches it. Every change applies at once, and the list follows
 * the `printers` topic, so another tab's change shows here.
 *
 * #4439, #4747: "Import from Bambuddy" runs the Bambuddy sync (`POST /printers/sync-bambuddy`)
 * and shows what it did. A linked row shows Bambuddy's own values beside ScadBuddy's, a
 * difference marked; a Bambuddy virtual printer's row says what it forwards to, and is not a
 * printer to print on, so it has no path and no links of its own to make here.
 */

const PATHS: { value: PrintPath; label: string }[] = [
  { value: 'auto', label: 'Automatic' },
  { value: 'native', label: 'Over the LAN' },
  { value: 'bambuddy', label: 'Through Bambuddy' },
]

function problem(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

export function PrintersPanel({ bambuddyConfigured = false }: { bambuddyConfigured?: boolean }) {
  const printers = useAsync(() => api.listPrinters(), [], TOPICS, registryNews)
  // Bambuddy's list is a convenience here: unreadable, nothing is offered to link.
  const discovered = useAsync(
    () => api.discoverPrinters().catch(() => [] as DiscoveredPrinter[]),
    [],
    TOPICS,
    registryNews,
  )
  const rows = printers.data

  const show = (next: PrinterRow[]) => printers.setData(next, { supersede: true })
  const replace = (next: PrinterRow) => {
    show((rows ?? []).map((row) => (row.id === next.id ? next : row)))
    discovered.refresh()
  }

  if (printers.error) {
    return (
      <p role="alert" className="text-xs text-warn">
        {problem(printers.error, 'The printers could not be read.')}
      </p>
    )
  }
  if (!rows) return <LoadingLines label="Loading the printers" />
  const free = (discovered.data ?? []).filter((printer) => !printer.linked_to)
  const named = new Map(rows.map((row) => [row.id, row.name]))
  return (
    <div className="space-y-3">
      <ImportFromBambuddy
        configured={bambuddyConfigured}
        onDone={() => {
          printers.refresh()
          discovered.refresh()
        }}
      />
      {rows.length === 0 && <p className="text-xs text-muted-foreground">No printers yet.</p>}
      <ul className="space-y-3" aria-label="Printers">
        {rows.map((row) => (
          <PrinterItem
            key={row.id}
            row={row}
            targetName={row.forwards_to ? (named.get(row.forwards_to) ?? null) : null}
            free={row.bambuddy_printer_id == null && row.kind === 'physical' ? free : []}
            onChanged={replace}
            onGone={() => show(rows.filter((entry) => entry.id !== row.id))}
          />
        ))}
      </ul>
      <AddPrinter onAdded={(row) => show([...rows, row])} />
      {/* #4747: only with a Bambuddy, whose virtual printers these are. */}
      {bambuddyConfigured && <VirtualPrintersPanel rows={rows} onChanged={() => printers.refresh()} />}
    </div>
  )
}

function PrinterItem({
  row,
  targetName,
  free,
  onChanged,
  onGone,
}: {
  row: PrinterRow
  targetName: string | null
  free: DiscoveredPrinter[]
  onChanged: (row: PrinterRow) => void
  onGone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function act(work: () => Promise<PrinterRow | void>, fallback: string) {
    setBusy(true)
    setError(null)
    try {
      const next = await work()
      if (next) onChanged(next)
      else onGone()
    } catch (cause) {
      setError(problem(cause, fallback))
    } finally {
      setBusy(false)
    }
  }

  const bambuddy = row.bambuddy_printer_id
  const virtual = row.kind === 'bambuddy_virtual'
  return (
    <li aria-label={`Printer ${row.name}`}>
      <Card size="sm" className="py-0">
        <CardContent className="space-y-3 py-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-sm font-medium">{row.name}</p>
              <p className="text-xs text-muted-foreground">
                {/* Plain text, never a control's name: the agent's page read skips it (#4375). */}
                <span className="sb-num">{row.serial}</span>
                {row.model ? ` · ${row.model}` : ''}
              </p>
            </div>
            {!virtual && (
            <ChoiceSelect
              aria-label={`How ${row.name} prints`}
              size="sm"
              className="w-auto min-w-44"
              value={row.preferred_path}
              disabled={busy}
              choices={PATHS}
              onValueChange={(next) =>
                void act(() => api.patchPrinter(row.id, { preferred_path: next as PrintPath }), 'Could not save the path.')
              }
            />
            )}
          </div>

          {virtual ? <VirtualLine row={row} targetName={targetName} /> : <BambuddyLine row={row} />}

          <div className="flex flex-wrap items-center gap-2 text-xs">
            {virtual ? null : bambuddy != null ? (
              <>
                <Badge variant="outline">Bambuddy printer {bambuddy}</Badge>
                <Button
                  size="xs"
                  variant="ghost"
                  disabled={busy}
                  aria-label={`Unlink Bambuddy from ${row.name}`}
                  onClick={() => void act(() => api.unlinkPrinterBambuddy(row.id), 'Could not unlink Bambuddy.')}
                >
                  Unlink
                </Button>
              </>
            ) : (
              <span className="text-muted-foreground">Not linked to Bambuddy</span>
            )}
            {row.lan_host ? (
              <>
                <Badge variant="outline">LAN {row.lan_host}</Badge>
                <Button
                  size="xs"
                  variant="ghost"
                  disabled={busy}
                  aria-label={`Unlink the LAN from ${row.name}`}
                  onClick={() => void act(() => api.unlinkPrinterLan(row.id), 'Could not unlink the LAN.')}
                  // It deletes the stored access code: the user's to do, as the agent's tool is outward.
                  {...USER_ONLY}
                >
                  Unlink
                </Button>
              </>
            ) : (
              <span className="text-muted-foreground">No LAN link</span>
            )}
          </div>

          {free.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-medium">Link a Bambuddy printer</p>
              <ul className="space-y-1">
                {free.map((printer) => (
                  <li key={printer.bambuddy_printer_id} className="flex flex-wrap items-center gap-2 text-xs">
                    <Button
                      size="xs"
                      variant="outline"
                      disabled={busy || !printer.linkable}
                      aria-label={`Link Bambuddy printer ${printer.name}`}
                      onClick={() =>
                        void act(
                          () => api.linkPrinterBambuddy(row.id, printer.bambuddy_printer_id),
                          'Could not link that Bambuddy printer.',
                        )
                      }
                    >
                      {printer.name}
                      {printer.model ? ` (${printer.model})` : ''}
                    </Button>
                    {printer.reason && <span className="text-muted-foreground">{printer.reason}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {row.lan_sync_error && (
            <p className="text-xs text-warn" aria-label={`Bambuddy sync note for ${row.name}`}>
              {row.lan_sync_error}
            </p>
          )}

          {!row.lan_host && !virtual && <LanForm row={row} onLinked={onChanged} />}

          <div className="flex items-center gap-2">
            <Button
              size="xs"
              variant="ghost"
              disabled={busy}
              aria-label={`Forget ${row.name}`}
              onClick={() => void act(() => api.deletePrinter(row.id), 'Could not forget the printer.')}
              {...USER_ONLY}
            >
              Forget
            </Button>
            {busy && <Spinner />}
          </div>
          {error && (
            <p role="alert" className="text-xs text-warn">
              {error}
            </p>
          )}
        </CardContent>
      </Card>
    </li>
  )
}

/** One value of Bambuddy's beside ScadBuddy's; marked when they differ. */
function Compared({ label, ours, theirs }: { label: string; ours?: string | null; theirs?: string | null }) {
  if (theirs == null || theirs === '') return null
  const differs = ours != null && ours !== '' && ours !== theirs
  return (
    <span className={differs ? 'text-warn' : undefined} data-differs={differs || undefined}>
      {label} {theirs}
      {differs ? ` (ScadBuddy: ${ours})` : ''}
    </span>
  )
}

/** #4747 item 1: what Bambuddy reports for a linked printer, as of the last sync. */
function BambuddyLine({ row }: { row: PrinterRow }) {
  const view = row.bambuddy_view
  if (!view || row.bambuddy_printer_id == null) return null
  const parts = [
    <Compared key="name" label="name" ours={row.name} theirs={view.name} />,
    <Compared key="model" label="model" ours={row.model} theirs={view.model} />,
    <Compared key="ip" label="IP" ours={row.lan_host} theirs={view.ip_address} />,
    <span key="active">{view.is_active === false ? 'inactive' : 'active'}</span>,
    view.nozzle_count != null ? <span key="nozzles">{view.nozzle_count} nozzles</span> : null,
  ].filter(Boolean)
  return (
    <p className="flex flex-wrap gap-x-2 text-xs text-muted-foreground" aria-label={`Bambuddy's view of ${row.name}`}>
      <span className="font-medium">In Bambuddy:</span>
      {parts}
    </p>
  )
}

/** #4747 item 3: a Bambuddy virtual printer, what it forwards to, and its mode. */
function VirtualLine({ row, targetName }: { row: PrinterRow; targetName: string | null }) {
  const view = row.bambuddy_view
  const model = view?.model ?? row.model ?? 'printer'
  const target = view?.target_printer_id == null ? `any ${model}` : (targetName ?? `Bambuddy printer ${view.target_printer_id}`)
  const mode = row.vp_mode ?? view?.mode
  return (
    <div className="space-y-1 text-xs">
      <p>
        <Badge variant="outline">Bambuddy virtual printer</Badge> → {target}
        {mode ? ` (${mode})` : ''}
      </p>
      {view && (
        <p className="flex flex-wrap gap-x-2 text-muted-foreground">
          <span>{view.enabled ? 'enabled' : 'disabled'}</span>
          <span>{view.running ? 'running' : 'stopped'}</span>
          {view.bind_ip ? <span>bind IP {view.bind_ip}</span> : null}
          {view.remote_interface_ip ? <span>interface {view.remote_interface_ip}</span> : null}
          <span>Not a printer to print on: print on the printer it forwards to.</span>
        </p>
      )}
    </div>
  )
}

const OUTCOMES: Record<PrinterSyncEntry['action'], string> = {
  created: 'added',
  linked: 'linked',
  already_linked: 'already linked',
  skipped: 'skipped',
  conflict: 'conflict',
  missing: 'no longer in Bambuddy',
}

/**
 * #4439 item 2: "Import from Bambuddy", the same sync the server runs on its own Schedule.
 * User-only, as the agent's tool is outward: it stores access codes Bambuddy gives.
 */
function ImportFromBambuddy({ configured, onDone }: { configured: boolean; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<PrinterSyncResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.syncPrintersFromBambuddy())
      onDone()
    } catch (cause) {
      setError(problem(cause, 'Could not import the printers from Bambuddy.'))
    } finally {
      setBusy(false)
    }
  }

  const notable = (result?.entries ?? []).filter(
    (entry) =>
      entry.reason ||
      entry.lan_reason ||
      entry.target_missing ||
      entry.action === 'conflict' ||
      entry.action === 'skipped',
  )
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={busy || !configured}
          aria-busy={busy}
          onClick={() => void run()}
          {...USER_ONLY}
        >
          {busy && <Spinner />}
          Import from Bambuddy
        </Button>
        {!configured && (
          <span className="text-xs text-muted-foreground">Set up the Bambuddy connection first, under Connection.</span>
        )}
      </div>
      {result && (
        <div role="status" className="space-y-1 text-xs">
          <p>{syncSummary(result)}.</p>
          {result.virtual_printers_error && (
            <p className="text-warn">Virtual printers not read: {result.virtual_printers_error}</p>
          )}
          {notable.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-4">
              {notable.map((entry, index) => (
                <li key={`${entry.kind}-${entry.bambuddy_printer_id ?? entry.bambuddy_vp_id ?? index}`}>
                  {entry.name}: {OUTCOMES[entry.action]}
                  {entry.reason ? ` (${entry.reason})` : ''}
                  {entry.lan_reason ? `. LAN: ${entry.lan_reason}` : ''}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/**
 * `ACCESS_CODE_MAX_LENGTH` in backend/scadbuddy/printers/models.py: Printers.test.tsx
 * compares this with the API spec's `maxLength`, so one changed alone fails it. The
 * browser counts UTF-16 units and the backend code points, so a code with characters
 * outside the BMP is cut a little short here, never long.
 */
const ACCESS_CODE_MAX_LENGTH = 128

/** The LAN link: the code goes once, to `PUT /lan`, which asks the printer first. User-only. */
function LanForm({ row, onLinked }: { row: PrinterRow; onLinked: (row: PrinterRow) => void }) {
  const [host, setHost] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const linked = await api.linkPrinterLan(row.id, { host: host.trim(), access_code: code })
      setCode('')
      setHost('')
      onLinked(linked)
    } catch (cause) {
      setCode('')
      setError(problem(cause, 'The printer could not be linked.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      aria-label={`LAN link for ${row.name}`}
      className="grid gap-2 sm:grid-cols-[1fr_10rem_auto] sm:items-end"
      onSubmit={(event) => void submit(event)}
      {...USER_ONLY}
    >
      <LabeledInput
        label="Host or IP address"
        value={host}
        onChange={(event) => setHost(event.target.value)}
        autoComplete="off"
        maxLength={253}
        required
      />
      <LabeledInput
        label="Access code"
        type="password"
        // Any format (#4434): the printer, asked by the link's probe, decides. A long paste
        // is cut here, at the backend's bound (see ACCESS_CODE_MAX_LENGTH).
        maxLength={ACCESS_CODE_MAX_LENGTH}
        value={code}
        onChange={(event) => setCode(event.target.value)}
        // Not "off", which Chrome ignores on a password: it would save one printer's code and
        // autofill it into the next printer's form, sending it to another host.
        autoComplete="new-password"
        required
      />
      <Button type="submit" size="sm" variant="outline" disabled={busy} aria-busy={busy}>
        {busy && <Spinner />}
        Test and link
      </Button>
      {error && (
        <p role="alert" className="text-xs text-warn sm:col-span-3">
          {error}
        </p>
      )}
    </form>
  )
}

function AddPrinter({ onAdded }: { onAdded: (row: PrinterRow) => void }) {
  const [serial, setSerial] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onAdded(await api.createPrinter({ serial: serial.trim().toUpperCase(), name: name.trim(), preferred_path: 'auto' }))
      setSerial('')
      setName('')
    } catch (cause) {
      setError(problem(cause, 'The printer could not be added.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      aria-label="Add a printer"
      className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
      onSubmit={(event) => void submit(event)}
    >
      {/* The backend's bounds (`printers/models.py` PrinterCreate). */}
      <LabeledInput
        label="Serial"
        value={serial}
        onChange={(event) => setSerial(event.target.value)}
        minLength={8}
        maxLength={32}
        pattern="[0-9A-Za-z]+"
        required
      />
      <LabeledInput
        label="Name"
        value={name}
        onChange={(event) => setName(event.target.value)}
        maxLength={80}
        required
      />
      <Button type="submit" size="sm" variant="outline" disabled={busy} aria-busy={busy}>
        {busy && <Spinner />}
        Add printer
      </Button>
      {error && (
        <p role="alert" className="text-xs text-warn sm:col-span-3">
          {error}
        </p>
      )}
    </form>
  )
}
