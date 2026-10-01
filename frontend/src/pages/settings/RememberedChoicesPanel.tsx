import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { BambuddyTargets, ModelPrintChoices, PrintOptions, ProjectChoices, RememberedChoices } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { Spinner } from '../../components/ui/Spinner'
import { useAsync } from '../../lib/useAsync'

type Row = {
  key: string
  kind: string
  subject: string
  value: string
  forget: () => Promise<unknown>
}

function describeChoices(choices: ModelPrintChoices, printerName: (id: number) => string): string {
  const parts: string[] = []
  if (choices.printer_id !== null && choices.printer_id !== undefined) parts.push(printerName(choices.printer_id))
  if (choices.tier) parts.push(`${choices.tier} quality`)
  if (choices.process_name) parts.push(choices.process_name)
  const spools = choices.filament_plan?.length ?? 0
  if (spools) parts.push(`${spools} ${spools === 1 ? 'spool' : 'spools'}`)
  if (choices.nozzles?.length) parts.push(`nozzles ${choices.nozzles.map((n) => n.size).join(' / ')} mm`)
  return parts.join(' · ') || 'Choices remembered'
}

function describeOptions(options: PrintOptions): string {
  return (
    Object.entries(options)
      .filter(([, value]) => value !== null && value !== undefined)
      .map(([name, value]) => `${name.replaceAll('_', ' ')}: ${typeof value === 'object' ? JSON.stringify(value) : String(value)}`)
      .join(', ') || 'Options remembered'
  )
}

function isEmptyOptions(options: PrintOptions | undefined): boolean {
  return !options || Object.values(options).every((value) => value === null || value === undefined)
}

/**
 * #322 — what the print dialog remembers, each with Forget. Every Forget is
 * that entry's own one-key route, so the page never posts a whole map back.
 */
export function RememberedChoicesPanel({
  targets,
  projects,
}: {
  targets: BambuddyTargets | null | undefined
  projects?: ProjectChoices | null
}) {
  const state = useAsync(() => api.getRemembered(), [], ['settings'])
  const [busy, setBusy] = useState<string | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const printerName = (id: number | string) =>
    targets?.printers?.find((printer) => String(printer.id) === String(id))?.name ?? `Printer ${id}`

  const projectName = (id: string) =>
    projects?.projects?.find((project) => String(project.id) === id)?.name ?? `Project ${id}`

  const remembered: RememberedChoices | undefined = state.data
  const rows: Row[] = []
  if (remembered) {
    for (const [slug, choices] of Object.entries(remembered.model_print_choices ?? {})) {
      rows.push({
        key: `choices:${slug}`,
        kind: 'Printer and spools',
        subject: slug,
        value: describeChoices(choices, printerName),
        forget: () => api.putModelChoices(slug, {}),
      })
    }
    for (const [printerId, bed] of Object.entries(remembered.printer_bed_types ?? {})) {
      rows.push({
        key: `bed:${printerId}`,
        kind: 'Plate',
        subject: printerName(printerId),
        value: bed,
        forget: () => api.putPrinterBedType(Number(printerId), null),
      })
    }
    for (const [projectId, target] of Object.entries(remembered.project_print_targets ?? {})) {
      rows.push({
        key: `project:${projectId}`,
        kind: 'Project printer and nozzle',
        subject: projectName(projectId),
        value: [printerName(target.printer_id), target.nozzle_diameter ? `${target.nozzle_diameter} mm` : null]
          .filter(Boolean)
          .join(' · '),
        forget: () => api.forgetRememberedProject(projectId),
      })
    }
    if (!isEmptyOptions(remembered.print_options)) {
      rows.push({
        key: 'options:global',
        kind: 'Print options',
        subject: 'Every print',
        value: describeOptions(remembered.print_options ?? {}),
        forget: () => api.putPrintOptions({ scope: 'global', options: {} }),
      })
    }
    for (const [printerId, options] of Object.entries(remembered.printer_print_options ?? {})) {
      rows.push({
        key: `options:printer:${printerId}`,
        kind: 'Print options',
        subject: printerName(printerId),
        value: describeOptions(options),
        forget: () => api.putPrintOptions({ scope: 'printer', key: printerId, options: {} }),
      })
    }
    for (const [slug, options] of Object.entries(remembered.model_print_options ?? {})) {
      rows.push({
        key: `options:model:${slug}`,
        kind: 'Print options',
        subject: slug,
        value: describeOptions(options),
        forget: () => api.putPrintOptions({ scope: 'model', key: slug, options: {} }),
      })
    }
  }

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key)
    setError(null)
    try {
      await action()
      state.refresh()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not forget that choice.')
    } finally {
      setBusy(null)
    }
  }

  if (state.loading) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading
      </p>
    )
  }
  if (state.error) {
    return (
      <p role="alert" className="text-[13px] text-warn">
        {state.error instanceof ApiError ? state.error.detail : 'Could not read the remembered choices.'}
      </p>
    )
  }

  return (
    <div className="space-y-3">
      {rows.length === 0 ? (
        <p className="text-[13px] text-muted">Nothing is remembered yet.</p>
      ) : (
        <table className="w-full text-left text-[13px]" aria-label="Remembered choices">
          <thead className="text-[12px] text-muted">
            <tr>
              <th className="py-1 pr-3 font-normal">What</th>
              <th className="py-1 pr-3 font-normal">For</th>
              <th className="py-1 pr-3 font-normal">Remembered</th>
              <th className="py-1 font-normal">
                <span className="sr-only">Forget</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className="border-t border-line align-top">
                <td className="py-1.5 pr-3 text-muted">{row.kind}</td>
                <td className="sb-num py-1.5 pr-3">{row.subject}</td>
                <td className="py-1.5 pr-3">{row.value}</td>
                <td className="py-1.5 text-right">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => void run(row.key, row.forget)}
                    disabled={busy !== null}
                    aria-label={`Forget ${row.kind.toLowerCase()} for ${row.subject}`}
                    {...USER_ONLY}
                  >
                    {busy === row.key && <Spinner />}
                    Forget
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
      {rows.length > 0 && (
        <div className="flex items-center gap-2">
          {confirmAll ? (
            <>
              <span className="text-[12px] text-muted">Forget all {rows.length}?</span>
              <Button
                size="sm"
                variant="danger"
                onClick={() => {
                  setConfirmAll(false)
                  void run('all', () => api.forgetAllRemembered())
                }}
                {...USER_ONLY}
              >
                Yes, forget all
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmAll(false)}>
                Keep them
              </Button>
            </>
          ) : (
            <Button size="sm" variant="danger" onClick={() => setConfirmAll(true)} disabled={busy !== null} {...USER_ONLY}>
              {busy === 'all' && <Spinner />}
              Forget all
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
