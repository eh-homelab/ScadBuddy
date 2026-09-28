import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api } from '../api/client'
import type { PrintOptions, PrintOptionsState, PrintRunResult } from '../api/types'
import { openExternal } from '../lib/embed'
import { resolveOptions } from '../lib/printOptions'
import { sourceApi, type PrintSource } from '../lib/printSource'
import { useFilamentPlan } from '../lib/useFilamentPlan'
import { usePrintChoices } from '../lib/usePrintChoices'
import { usePrintProgress } from '../lib/usePrintProgress'
import { useRunPrint } from '../lib/useRunPrint'
import { FilamentPicker } from './FilamentPicker'
import { AdvancedSwitch } from './print/AdvancedSwitch'
import { CopiesField } from './print/CopiesField'
import { NozzleStep } from './print/NozzleStep'
import { PlatesToPrint } from './print/PlatesToPrint'
import { PlateStep } from './print/PlateStep'
import { PresetOverrides } from './print/PresetOverrides'
import { QualityStep } from './print/QualityStep'
import { QueuedPanel } from './print/QueuedPanel'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'
import { ProjectPicker } from './ProjectPicker'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/**
 * The print dialog, spool-first (docs/superpowers/specs/2026-09-27-spool-first-print-design.md).
 *
 * You choose what you actually think about — the spools from the inventory, the nozzle
 * size, a quality tier and the plate — and ScadBuddy derives every Bambu preset from
 * those choices server-side, then slices and queues through Bambuddy. There are no
 * slicer pipelines here any more; they stay in Bambuddy untouched.
 *
 * - One read, the source's choices read (`/print/outputs/{id}/…` or
 *   `/print/library/{file_id}/…`, #313), opens the dialog: printers, installed
 *   nozzles, tiers and processes per size, plate types with the one last printed on,
 *   the filament step, and what this model last printed with (#78).
 * - One write, the source's run (`/print/outputs/{id}/…` or `/print/library/{file_id}/…`,
 *   #313), prints. A 422 is the resolver refusing a
 *   combination (an unpicked slot, no process, no preset for a spool at this size); its
 *   `detail` is shown above Print and the dialog stays open.
 * - Simple mode offers the tiers; Advanced adds the full process list, per-side flow and
 *   a per-slot filament preset override.
 *
 * Around that it keeps what the send bar's print already had: the options disclosure
 * (#88), the project (#79), copies with the remembered quantity (#124/#145), which plate
 * of a multi-plate 3MF (#83), and following the run to completion (#89).
 */

interface Props {
  open: boolean
  /** #313 — an output ScadBuddy rendered, or a file in Bambuddy's library. */
  source: PrintSource | undefined
  onClose: () => void
  onRan: (result: PrintRunResult) => void
  /** #81 — the model of the printer in view, so the preview can draw its plate. */
  onPrinterModel?: (model: string | null) => void
}

export function PrintPicker({ open, source, onClose, onRan, onPrinterModel }: Props) {
  /**
   * The model, for its print-options scope — the same slug its choices are remembered
   * under (`sourceApi`). A library file has none.
   */
  const slug = source?.kind === 'output' ? source.output.slug : undefined
  // A library run polls nothing and attaches nothing: its progress is Bambuddy's queue (#313).
  const outputId = source?.kind === 'output' ? source.output.id : undefined
  const picker = usePrintChoices(open, source)
  const { choices, loading, loadError, printers, printerId, printer, selection, size } = picker
  const { nozzles, tier, processName, bedType, overrides, plate } = selection
  const { filaments, plan, setPlan, planChanged, filamentError } = useFilamentPlan(
    source,
    choices,
    plate,
    size,
  )

  // null until the user sets it, so a remembered quantity is not overridden by the
  // box's own starting value (#124).
  const [copies, setCopies] = useState<number | null>(null)
  /** #145 — the remembered options, so the box can say what an unset Copies queues. */
  const [remembered, setRemembered] = useState<PrintOptionsState | null>(null)
  /** #88 — this print's overrides, all but `quantity`, which is `copies`. */
  const [options, setOptions] = useState<PrintOptions>({})
  /** #79 — the Bambuddy project this print is filed under. */
  const [projectId, setProjectId] = useState<number | null>(null)

  const runPrint = useRunPrint({
    source,
    choices,
    printerId,
    selection,
    plan,
    planChanged,
    copies,
    projectId,
    options,
    onRan,
  })
  const { run, running, runError, refused, result } = runPrint

  /**
   * #89 — follow only the print this dialog just started, so opening the dialog on an
   * output printed last week does not start polling a run nobody is watching.
   */
  const { progress, polling } = usePrintProgress(outputId, open && result !== null)

  /**
   * #79 — file the finished print under its project, once the progress read (#89) has
   * the queue entries. Best effort, once per settled print, guarded by a ref so the
   * guard itself does not re-render and re-run the effect.
   */
  const attached = useRef<string | null>(null)
  useEffect(() => {
    if (!outputId || projectId === null || !progress?.settled) return
    const entries = (progress.copies_detail ?? [])
      .map((copy) => copy.queue_entry_id)
      .filter((id): id is number => typeof id === 'number')
    if (entries.length === 0) return
    const key = `${outputId}:${projectId}:${entries.join(',')}`
    if (attached.current === key) return
    attached.current = key
    void api
      .attachToProject(outputId, { project_id: projectId, queue_item_ids: entries })
      .catch(() => {
        attached.current = null
      })
  }, [outputId, projectId, progress])

  // One output's options do not survive a change of output — the other half of
  // usePrintChoices' reset on the same `sourceKey`.
  const { sourceKey } = picker
  useEffect(() => {
    setOptions({})
  }, [sourceKey])

  /** #81 — the chosen printer's model, reported once the choices have landed. */
  const printerModel = choices ? (printer?.model ?? null) : undefined
  useEffect(() => {
    if (printerModel !== undefined) onPrinterModel?.(printerModel)
  }, [printerModel, onPrinterModel])

  useEffect(() => {
    if (!open) return
    let live = true
    api
      .getPrintOptions()
      .then((view) => live && setRemembered(view))
      // Nothing to show is the pre-#145 behavior; the run still resolves it server-side.
      .catch(() => live && setRemembered(null))
    return () => {
      live = false
    }
  }, [open, slug])

  const rememberedCopies =
    resolveOptions(
      remembered?.global_options,
      printerId === null ? undefined : remembered?.printers?.[String(printerId)],
      slug === undefined ? undefined : remembered?.models?.[slug],
    ).quantity ?? null
  const effectiveCopies = copies ?? rememberedCopies ?? 1

  function close() {
    setProjectId(null)
    setOptions({})
    runPrint.reset()
    picker.reset()
    onClose()
  }

  return (
    <Dialog
      open={open}
      title="Print"
      description={
        result
          ? undefined
          : 'Choose the spools, nozzles, quality and plate. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
      }
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            <Button variant="primary" onClick={() => openExternal(result.bambuddy_url)}>
              Open in queue
            </Button>
          </>
        ) : (
          <>
            <Button onClick={close} disabled={running}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void run()}
              disabled={running || loading || !choices || refused}
              data-testid="run-print"
              {...USER_ONLY}
            >
              {running && <Spinner />}
              Print
            </Button>
          </>
        )
      }
    >
      {result ? (
        <QueuedPanel
          result={result}
          printerName={printer?.name ?? null}
          progress={progress}
          polling={polling}
        />
      ) : (
        <>
          {loading && !choices && (
            <p className="flex items-center gap-2 text-[13px] text-muted">
              <Spinner /> Reading the printer and the inventory
            </p>
          )}

          {loadError && (
            <p role="alert" className="text-[13px] text-warn">
              {loadError}
            </p>
          )}

          {choices && (
            <div className="space-y-3">
              <AdvancedSwitch value={picker.advanced} onToggle={picker.toggleAdvanced} />
              {printers.length > 1 && (
                <div>
                  <label htmlFor="print-printer" className="block text-[13px]">
                    Printer
                  </label>
                  <select
                    id="print-printer"
                    value={printerId === null ? '' : String(printerId)}
                    onChange={(event) =>
                      picker.askPrinter(
                        event.target.value === '' ? null : Number(event.target.value),
                      )
                    }
                    className="sb-field mt-1.5 cursor-pointer"
                  >
                    {printerId === null && <option value="">Choose a printer</option>}
                    {printers.map((entry) => (
                      <option key={entry.id} value={entry.id}>
                        {entry.name}
                        {entry.model ? ` (${entry.model})` : ''}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {filamentError && (
                <p className="text-[12px] text-warn" data-testid="filaments-unavailable">
                  ScadBuddy could not read the filament inventory: {filamentError}
                </p>
              )}
              {filaments && (
                <FilamentPicker
                  options={filaments}
                  plan={plan}
                  onChange={setPlan}
                  copies={effectiveCopies}
                  nozzleSize={size}
                />
              )}

              {picker.advanced && filaments && (filaments.slots ?? []).length > 0 && (
                <PresetOverrides
                  size={size}
                  slots={filaments.slots ?? []}
                  presets={choices.filament_presets?.[size] ?? []}
                  overrides={overrides}
                  onChange={picker.setOverride}
                />
              )}

              <NozzleStep
                sizes={choices.nozzle_sizes ?? []}
                installed={choices.installed ?? []}
                advanced={picker.advanced}
                value={nozzles}
                onChange={picker.changeNozzles}
              />
              <QualityStep
                size={size}
                tiers={choices.tiers?.[size] ?? []}
                processes={choices.processes?.[size] ?? []}
                advanced={picker.advanced}
                tier={tier}
                processName={processName}
                onChange={picker.changeQuality}
              />
              {bedType !== null && (
                <PlateStep
                  bedTypes={
                    (choices.bed_types ?? []).includes(bedType)
                      ? (choices.bed_types ?? [])
                      : [bedType, ...(choices.bed_types ?? [])]
                  }
                  value={bedType}
                  lastBedType={choices.last_bed_type ?? null}
                  printerName={printer?.name ?? null}
                  onChange={picker.setBedType}
                />
              )}

              {picker.plates.length > 1 && source && (
                <PlatesToPrint
                  plates={picker.plates}
                  value={plate}
                  onChange={picker.setPlate}
                  thumbnailUrl={sourceApi(source).plateThumbnailUrl}
                />
              )}

              <PrintOptionsDisclosure
                slug={slug}
                printerId={printerId}
                value={copies === null ? options : { ...options, quantity: copies }}
                onChange={({ quantity, ...rest }) => {
                  setCopies(quantity ?? null)
                  setOptions(rest)
                }}
              />

              {/* #79 — a send to a project uploads into that project's folder. */}
              <ProjectPicker value={projectId} onChange={setProjectId} onLoaded={setProjectId} />

              <CopiesField value={copies} remembered={rememberedCopies} onChange={setCopies} />
            </div>
          )}

          {runError && (
            <p role="alert" className="mt-3 text-[13px] text-warn">
              {runError}
            </p>
          )}
        </>
      )}
    </Dialog>
  )
}
