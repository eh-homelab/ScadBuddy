import { useState } from 'react'
import { Link } from 'react-router'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type {
  BambuddyTargets,
  ModelPrintChoices,
  PrintOptions,
  ProjectChoices,
  RememberedChoices,
} from '../../api/types'
import { ALGORITHM_LABELS } from '../../components/print/rackLabels'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { LoadingLines } from '../../components/settings/form'
import { Spinner } from '../../components/ui/spinner'
import { libraryFileOfScope } from '../../lib/printSource'
import { useAsync } from '../../lib/useAsync'

type Row = {
  key: string
  kind: string
  subject: string
  /** Where the subject is opened: a library file's place in the Library (#2349). */
  href?: string
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

/**
 * A model's id as it is, a library file's scope as the file (#2349): its name, linked to
 * it in the Library, or "deleted file N" once Bambuddy no longer has it. Until the names
 * have answered, or when they cannot be read, the file's id.
 */
function scopeName(scope: string, names: ReadonlyMap<number, string | null> | undefined): { subject: string; href?: string } {
  const fileId = libraryFileOfScope(scope)
  if (fileId === null) return { subject: scope }
  const name = names?.get(fileId)
  if (name === undefined) return { subject: `Library file ${fileId}` }
  if (name === null) return { subject: `deleted file ${fileId}` }
  return { subject: name, href: `/library?file=${fileId}` }
}

/** The most ids `GET /print/library/names` takes at once (the route's `NAMES_MAX`). */
export const LIBRARY_NAMES_PER_REQUEST = 100

/** The library files the remembered choices and options are kept for, each once, in order. */
function libraryFilesOf(remembered: RememberedChoices | undefined): number[] {
  const scopes = [...Object.keys(remembered?.model_print_choices ?? {}), ...Object.keys(remembered?.model_print_options ?? {})]
  return [...new Set(scopes.map(libraryFileOfScope).filter((id): id is number => id !== null))]
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
  const fileIds = libraryFilesOf(state.data)
  // One lookup for every library-file row's name (#2349), in requests of at most the
  // route's 100 ids; none when there are no such rows.
  const names = useAsync(async () => {
    const batches: number[][] = []
    for (let at = 0; at < fileIds.length; at += LIBRARY_NAMES_PER_REQUEST) {
      batches.push(fileIds.slice(at, at + LIBRARY_NAMES_PER_REQUEST))
    }
    return (await Promise.all(batches.map((batch) => api.libraryFileNames(batch)))).flat()
  }, [fileIds.join(',')])
  // A Forget changes the ids, and so asks again: the rows keep the last names meanwhile,
  // rather than every one blinking back to its id.
  const [lastNames, setLastNames] = useState(names.data)
  if (names.data !== undefined && names.data !== lastNames) setLastNames(names.data)
  const shownNames = names.error ? undefined : (names.data ?? lastNames)
  const fileNames: ReadonlyMap<number, string | null> | undefined = shownNames
    ? new Map(shownNames.map((file) => [file.id, file.name ?? null]))
    : undefined
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const printerName = (id: number | string) =>
    targets?.printers?.find((printer) => String(printer.id) === String(id))?.name ?? `Printer ${id}`

  const projectName = (id: string) =>
    projects?.projects?.find((project) => String(project.id) === id)?.name ?? `Project ${id}`

  const remembered: RememberedChoices | undefined = state.data
  const rows: Row[] = []
  if (remembered) {
    for (const [scope, choices] of Object.entries(remembered.model_print_choices ?? {})) {
      // #1754: a library file's are in the same map, under `library:<file id>`.
      const fileId = libraryFileOfScope(scope)
      rows.push({
        key: `choices:${scope}`,
        kind: 'Printer and spools',
        ...scopeName(scope, fileNames),
        value: describeChoices(choices, printerName),
        forget: () => (fileId === null ? api.putModelChoices(scope, {}) : api.putLibraryChoices(fileId, {})),
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
    for (const [printerId, algorithm] of Object.entries(remembered.printer_rack_algorithms ?? {})) {
      rows.push({
        key: `rack:${printerId}`,
        kind: 'Rack nozzle',
        subject: printerName(printerId),
        value: ALGORITHM_LABELS[algorithm],
        forget: () => api.putPrinterRackAlgorithm(Number(printerId), null),
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
    for (const [scope, entry] of Object.entries(remembered.model_projects ?? {})) {
      // #1660: `<model id>` with no preset, `<model id>/<preset id>` with one. The
      // server names a preset by its name now (one read for every model, not one
      // `listPresets` each), and drops an entry for a preset its model no longer lists.
      const slash = scope.indexOf('/')
      const modelId = slash < 0 ? scope : scope.slice(0, slash)
      const presetId = slash < 0 ? null : scope.slice(slash + 1)
      rows.push({
        key: `model-project:${scope}`,
        kind: 'Project',
        subject:
          presetId === null
            ? modelId
            : `${modelId} · ${entry.preset_name ?? presetId}`,
        value: projectName(String(entry.project_id)),
        forget: () => api.putModelProject(modelId, { preset_id: presetId, project_id: null }),
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
    for (const [scope, options] of Object.entries(remembered.model_print_options ?? {})) {
      rows.push({
        key: `options:model:${scope}`,
        kind: 'Print options',
        ...scopeName(scope, fileNames),
        value: describeOptions(options),
        forget: () => api.putPrintOptions({ scope: 'model', key: scope, options: {} }),
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
    return <LoadingLines label="Loading" lines={['w-full', 'w-full', 'w-2/3']} />
  }
  if (state.error) {
    return (
      <p role="alert" className="text-sm text-warn">
        {state.error instanceof ApiError ? state.error.detail : 'Could not read the remembered choices.'}
      </p>
    )
  }

  return (
    <div className="space-y-3">
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing is remembered yet.</p>
      ) : (
        <Card size="sm" className="overflow-x-auto py-0">
          <table className="w-full text-left text-sm" aria-label="Remembered choices">
            <thead className="border-b text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">What</th>
                <th className="px-3 py-2 font-medium">For</th>
                <th className="px-3 py-2 font-medium">Remembered</th>
                <th className="px-3 py-2 font-medium">
                  <span className="sr-only">Forget</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((row) => (
                <tr key={row.key} className="align-top transition-colors hover:bg-muted/50">
                  <td className="px-3 py-2 text-muted-foreground">{row.kind}</td>
                  <td className="sb-num px-3 py-2">
                    {row.href ? (
                      <Link to={row.href} className="underline decoration-border underline-offset-2 hover:text-primary">
                        {row.subject}
                      </Link>
                    ) : (
                      row.subject
                    )}
                  </td>
                  <td className="px-3 py-2">{row.value}</td>
                  <td className="px-3 py-1.5 text-right">
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
        </Card>
      )}
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
      {rows.length > 0 && (
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size="sm" variant="destructive" disabled={busy !== null} aria-busy={busy === 'all'} {...USER_ONLY}>
              {busy === 'all' && <Spinner />}
              Forget all
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Forget all {rows.length}?</AlertDialogTitle>
              <AlertDialogDescription>
                The print dialog then opens on its own defaults for every model and printer.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Keep them</AlertDialogCancel>
              <AlertDialogAction
                variant="destructive"
                onClick={() => void run('all', () => api.forgetAllRemembered())}
                {...USER_ONLY}
              >
                Yes, forget all
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  )
}
