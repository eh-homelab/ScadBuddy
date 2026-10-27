import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type {
  PrinterRow,
  VirtualPrinter,
  VirtualPrinterCreate,
  VirtualPrinterDiagnostic,
  VirtualPrinterUpdate,
  VirtualPrinters,
} from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { ChoiceSelect, LabeledInput, LoadingLines, SwitchField } from './form'
import { Spinner } from '../ui/spinner'

// #4747 item 4: Bambuddy's virtual printers, managed here. The routes are
// backend/scadbuddy/api/printers.py `/printers/virtual`; every write is a command and the
// registry follows it. A virtual printer's access code is write-only: typed here, sent once,
// never shown.

type Mode = VirtualPrinterCreate['mode']

const MODES: { value: Mode; label: string }[] = [
  { value: 'archive', label: 'Archive' },
  { value: 'review', label: 'Review' },
  { value: 'queue', label: 'Print queue' },
  { value: 'proxy', label: 'Proxy' },
]

/** Bambuddy's bound: "Access code must be exactly 8 characters". */
export const VP_ACCESS_CODE_LENGTH = 8

const ANY = ''
/**
 * The form's target for a VP whose Bambuddy target no registry row links (not imported
 * yet, or a sync that failed): kept as it is, never sent as "any printer" (review #4784).
 */
const UNREGISTERED = '\u0000bambuddy-target'

/** Bambuddy's target that no registry row links, or null. */
function unregisteredTarget(vp: VirtualPrinter | null): number | null {
  return vp && vp.target == null && vp.target_printer_id != null ? vp.target_printer_id : null
}

function problem(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.message : fallback
}

type Form = {
  name: string
  mode: Mode
  model: string
  target: string
  enabled: boolean
  auto_dispatch: boolean
  queue_force_color_match: boolean
  save_ams_mapping: boolean
  gcode_injection: boolean
  bind_ip: string
  remote_interface_ip: string
  access_code: string
}

function formOf(vp: VirtualPrinter | null, defaultModel: string): Form {
  return {
    name: vp?.name ?? '',
    mode: (vp?.mode as Mode | undefined) ?? 'queue',
    model: vp?.model ?? defaultModel,
    target: vp?.target ?? (unregisteredTarget(vp) != null ? UNREGISTERED : ANY),
    enabled: vp?.enabled ?? false,
    auto_dispatch: vp?.auto_dispatch ?? true,
    queue_force_color_match: vp?.queue_force_color_match ?? false,
    save_ams_mapping: vp?.save_ams_mapping ?? false,
    gcode_injection: vp?.gcode_injection ?? false,
    bind_ip: vp?.bind_ip ?? '',
    remote_interface_ip: vp?.remote_interface_ip ?? '',
    access_code: '',
  }
}

function bodyOf(form: Form): VirtualPrinterCreate {
  const body: VirtualPrinterCreate = {
    name: form.name.trim(),
    mode: form.mode,
    model: form.model || null,
    target: form.target === UNREGISTERED ? undefined : form.target || null,
    enabled: form.enabled,
    auto_dispatch: form.auto_dispatch,
    queue_force_color_match: form.queue_force_color_match,
    save_ams_mapping: form.save_ams_mapping,
    gcode_injection: form.gcode_injection,
    bind_ip: form.bind_ip.trim() || null,
    remote_interface_ip: form.remote_interface_ip.trim() || null,
  }
  // A targeted one uses its target's code; Bambuddy would overwrite any other.
  if (!form.target && form.access_code) body.access_code = form.access_code
  if (body.target === undefined) delete body.target
  return body
}

/**
 * An edit's PUT: only what the user changed, so a save never sends a field it did not
 * touch (the PUT changes only what is sent). An unregistered target is never sent.
 */
function changesOf(form: Form, vp: VirtualPrinter): VirtualPrinterUpdate {
  const next = bodyOf(form) as Record<string, unknown>
  const before = bodyOf(formOf(vp, vp.model ?? '')) as Record<string, unknown>
  const changes: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(next)) {
    if (key === 'access_code' || value !== before[key]) changes[key] = value
  }
  return changes as VirtualPrinterUpdate
}

export function VirtualPrintersPanel({ rows, onChanged }: { rows: PrinterRow[]; onChanged: () => void }) {
  const listed = useAsync(() => api.listVirtualPrinters(), [])
  const [adding, setAdding] = useState(false)
  const refresh = () => {
    listed.refresh()
    onChanged()
  }
  // A target is a registry printer linked to a physical Bambuddy printer.
  const targets = rows.filter((row) => row.kind === 'physical' && row.bambuddy_printer_id != null)
  const data = listed.data
  return (
    <section aria-label="Bambuddy virtual printers" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Bambuddy virtual printers</h3>
        {data && <AuthBadge enabled={data.auth_enabled ?? null} />}
      </div>
      {listed.error ? (
        <p role="alert" className="text-xs text-warn">
          {problem(listed.error, "Bambuddy's virtual printers could not be read.")}
        </p>
      ) : !data ? (
        <LoadingLines label="Loading the virtual printers" />
      ) : (
        <>
          {data.auth_enabled && (
            <p className="text-xs text-muted-foreground">
              Bambuddy's auth is on, so no API key can change a virtual printer. Change them in Bambuddy, or turn
              its auth off.
            </p>
          )}
          {data.printers.length === 0 && <p className="text-xs text-muted-foreground">No virtual printers.</p>}
          <ul className="space-y-3" aria-label="Virtual printers">
            {data.printers.map((vp) => (
              <VirtualPrinterItem key={vp.id} vp={vp} data={data} rows={rows} targets={targets} onChanged={refresh} />
            ))}
          </ul>
          {adding ? (
            <VirtualPrinterForm
              vp={null}
              data={data}
              targets={targets}
              onDone={() => {
                setAdding(false)
                refresh()
              }}
              onCancel={() => setAdding(false)}
            />
          ) : (
            <Button size="sm" variant="outline" onClick={() => setAdding(true)} {...USER_ONLY}>
              Add virtual printer
            </Button>
          )}
        </>
      )}
    </section>
  )
}

function AuthBadge({ enabled }: { enabled: boolean | null }) {
  return (
    <Badge variant="outline" aria-label="Bambuddy auth">
      Bambuddy auth: {enabled == null ? 'unknown' : enabled ? 'on' : 'off'}
    </Badge>
  )
}

function VirtualPrinterItem({
  vp,
  data,
  rows,
  targets: offered,
  onChanged,
}: {
  vp: VirtualPrinter
  data: VirtualPrinters
  rows: PrinterRow[]
  targets: PrinterRow[]
  onChanged: () => void
}) {
  // Its current target is offered and named even when it is no longer one a new VP may
  // take (review #4784): a row that is not physical, or no longer linked to Bambuddy.
  const current = vp.target && !offered.some((row) => row.id === vp.target) ? rows.filter((row) => row.id === vp.target) : []
  const targets = [...offered, ...current]
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [diagnostic, setDiagnostic] = useState<VirtualPrinterDiagnostic | null>(null)

  async function act(work: () => Promise<unknown>, fallback: string, after?: () => void) {
    setBusy(true)
    setError(null)
    try {
      await work()
      after?.()
    } catch (cause) {
      setError(problem(cause, fallback))
    } finally {
      setBusy(false)
    }
  }

  const unregistered = unregisteredTarget(vp)
  const target = vp.target
    ? (targets.find((row) => row.id === vp.target)?.name ?? 'a printer')
    : unregistered != null
      ? `Bambuddy printer ${unregistered} (not in the registry)`
      : null
  const model = vp.model_name ?? (vp.model ? data.models[vp.model] : null) ?? vp.model ?? 'its model'
  return (
    <li aria-label={`Virtual printer ${vp.name}`}>
      <Card size="sm" className="py-0">
        <CardContent className="space-y-3 py-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 space-y-1">
              <p className="text-sm font-medium">{vp.name}</p>
              <p className="text-xs text-muted-foreground">
                → {target ?? `any ${model}`} ({vp.mode})
              </p>
              <div className="flex flex-wrap gap-1">
                <Badge variant="outline">{vp.enabled ? 'Enabled' : 'Disabled'}</Badge>
                {vp.status?.running && <Badge variant="outline">Running</Badge>}
                {vp.target_printer_id == null && !vp.access_code_set && <Badge variant="outline">No access code</Badge>}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-1" {...USER_ONLY}>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  void act(async () => setDiagnostic(await api.diagnoseVirtualPrinter(vp.id)), 'Could not run the checks.')
                }
              >
                Check
              </Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing((open) => !open)}>
                {editing ? 'Close' : 'Edit'}
              </Button>
              {confirming ? (
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={busy}
                  aria-label={`Confirm deleting ${vp.name}`}
                  onClick={() =>
                    void act(() => api.deleteVirtualPrinter(vp.id), 'Could not delete the virtual printer.', onChanged)
                  }
                >
                  Delete in Bambuddy
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  aria-label={`Delete ${vp.name}`}
                  onClick={() => setConfirming(true)}
                >
                  Delete
                </Button>
              )}
              {busy && <Spinner />}
            </div>
          </div>
          {diagnostic && <Diagnostic result={diagnostic} />}
          {editing && (
            <VirtualPrinterForm
              vp={vp}
              data={data}
              targets={targets}
              onDone={() => {
                setEditing(false)
                setDiagnostic(null)
                onChanged()
              }}
              onCancel={() => setEditing(false)}
            />
          )}
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

const CHECK_TONE: Record<string, string> = { pass: '', warn: 'text-warn', fail: 'text-warn', skip: 'text-muted-foreground' }

function Diagnostic({ result }: { result: VirtualPrinterDiagnostic }) {
  return (
    <div role="status" aria-label={`Checks for ${result.vp_name}`} className="space-y-1 text-xs">
      <p>Bambuddy's checks: {result.overall}</p>
      <ul className="space-y-0.5 pl-4">
        {(result.checks ?? []).map((check) => (
          <li key={check.id} className={CHECK_TONE[check.status] ?? ''}>
            {check.id.replaceAll('_', ' ')}: {check.status}
          </li>
        ))}
      </ul>
    </div>
  )
}

function VirtualPrinterForm({
  vp,
  data,
  targets,
  onDone,
  onCancel,
}: {
  vp: VirtualPrinter | null
  data: VirtualPrinters
  targets: PrinterRow[]
  onDone: () => void
  onCancel: () => void
}) {
  const models = Object.entries(data.models)
  const [form, setForm] = useState<Form>(() => formOf(vp, models[0]?.[0] ?? ''))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((prev) => ({ ...prev, [key]: value }))

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const body = bodyOf(form)
      if (vp) await api.updateVirtualPrinter(vp.id, changesOf(form, vp))
      else await api.createVirtualPrinter(body)
      set('access_code', '')
      onDone()
    } catch (cause) {
      set('access_code', '')
      setError(problem(cause, 'Bambuddy did not take the change.'))
    } finally {
      setBusy(false)
    }
  }

  const label = vp ? `Edit ${vp.name}` : 'New virtual printer'
  return (
    <form aria-label={label} className="space-y-3" onSubmit={(event) => void submit(event)} {...USER_ONLY}>
      <div className="grid gap-2 sm:grid-cols-2">
        <LabeledInput
          label="Name"
          value={form.name}
          onChange={(event) => set('name', event.target.value)}
          maxLength={80}
          required
        />
        <div className="grid gap-1.5">
          <Label className="font-normal">Mode</Label>
          <ChoiceSelect
            aria-label="Mode"
            value={form.mode}
            onValueChange={(value) => set('mode', value as Mode)}
            choices={MODES}
          />
        </div>
        <div className="grid gap-1.5">
          <Label className="font-normal">Model</Label>
          <ChoiceSelect
            aria-label="Model"
            value={form.model}
            onValueChange={(value) => set('model', value)}
            choices={models.map(([code, name]) => ({ value: code, label: name }))}
          />
        </div>
        <div className="grid gap-1.5">
          <Label className="font-normal">Sends to</Label>
          <ChoiceSelect
            aria-label="Sends to"
            value={form.target}
            onValueChange={(value) => set('target', value)}
            choices={[
              { value: ANY, label: 'Any printer of its model' },
              ...(unregisteredTarget(vp) != null
                ? [{ value: UNREGISTERED, label: `Bambuddy printer ${unregisteredTarget(vp)} (not in the registry)` }]
                : []),
              ...targets.map((row) => ({ value: row.id, label: row.name })),
            ]}
          />
        </div>
        <LabeledInput
          label="Bind IP"
          value={form.bind_ip}
          onChange={(event) => set('bind_ip', event.target.value)}
          maxLength={253}
          autoComplete="off"
        />
        <LabeledInput
          label="Interface override"
          value={form.remote_interface_ip}
          onChange={(event) => set('remote_interface_ip', event.target.value)}
          maxLength={253}
          autoComplete="off"
        />
        {!form.target && (
          <LabeledInput
            label={vp?.access_code_set ? 'New access code (8 characters)' : 'Access code (8 characters)'}
            type="password"
            value={form.access_code}
            onChange={(event) => set('access_code', event.target.value)}
            minLength={VP_ACCESS_CODE_LENGTH}
            maxLength={VP_ACCESS_CODE_LENGTH}
            autoComplete="new-password"
          />
        )}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <SwitchField label="Enabled" checked={form.enabled} onCheckedChange={(on) => set('enabled', on)} />
        <SwitchField
          label="Start queued prints when the printer is free"
          checked={form.auto_dispatch}
          onCheckedChange={(on) => set('auto_dispatch', on)}
        />
        <SwitchField
          label="Queue only on matching filament colours"
          checked={form.queue_force_color_match}
          onCheckedChange={(on) => set('queue_force_color_match', on)}
        />
        <SwitchField
          label="Keep the slicer's AMS mapping"
          checked={form.save_ams_mapping}
          onCheckedChange={(on) => set('save_ams_mapping', on)}
        />
        <SwitchField
          label="G-code injection"
          checked={form.gcode_injection}
          onCheckedChange={(on) => set('gcode_injection', on)}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" variant="outline" disabled={busy} aria-busy={busy}>
          {busy && <Spinner />}
          {vp ? 'Save in Bambuddy' : 'Create in Bambuddy'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
