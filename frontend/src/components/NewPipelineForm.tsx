import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { PipelineCreate, PipelineView, PresetChoice, PresetRef } from '../api/types'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/**
 * Builds a Bambuddy slicer pipeline out of Bambuddy's own presets (#86).
 *
 * ScadBuddy owns no slicing settings, so there is nothing here but preset choices: the
 * **nozzle diameter lives in the process preset's name** ("… H2C 0.2 nozzle"), which is
 * why this form picks a process preset rather than offering a diameter of its own.
 *
 * `SlicerPipelineCreate` carries no target fields, so the new pipeline cannot be aimed at
 * a printer from here — Bambuddy targets it and the created row reports what it chose.
 */

function refKey(ref: PresetRef | undefined): string {
  return ref ? `${ref.source}:${ref.id}` : ''
}

function findRef(choices: PresetChoice[], key: string): PresetRef | undefined {
  return choices.find((choice) => refKey(choice.ref) === key)?.ref
}

interface Props {
  /** One filament preset per colour, in the plate's slot order. */
  colors: string[]
  onCreated: (pipeline: PipelineView) => void
  onCancel: () => void
}

export function NewPipelineForm({ colors, onCreated, onCancel }: Props) {
  const [printerKey, setPrinterKey] = useState('')
  const [processKey, setProcessKey] = useState('')
  const [filamentKeys, setFilamentKeys] = useState<string[]>(() => colors.map(() => ''))
  const [bedType, setBedType] = useState('')
  const [name, setName] = useState('')

  const [printers, setPrinters] = useState<PresetChoice[]>([])
  const [processes, setProcesses] = useState<PresetChoice[]>([])
  const [filaments, setFilaments] = useState<PresetChoice[]>([])
  const [bedTypes, setBedTypes] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Printer presets and bed types first; the other two tiers are thousands of rows on a
  // real Bambuddy, so the server only sends them once a printer preset is named.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .getPrintPresets()
      .then((options) => {
        if (cancelled) return
        setPrinters(options.printer ?? [])
        setBedTypes(options.bed_types ?? [])
      })
      .catch((cause: unknown) =>
        setError(cause instanceof ApiError ? cause.detail : 'Could not list the presets.'),
      )
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const chosen = findRef(printers, printerKey)
    if (!chosen) {
      setProcesses([])
      setFilaments([])
      return
    }
    let cancelled = false
    api
      .getPrintPresets(chosen)
      .then((options) => {
        if (cancelled) return
        setProcesses(options.process ?? [])
        setFilaments(options.filament ?? [])
      })
      .catch((cause: unknown) =>
        setError(
          cause instanceof ApiError ? cause.detail : 'Could not list the compatible presets.',
        ),
      )
    return () => {
      cancelled = true
    }
  }, [printerKey, printers])

  const processName = processes.find((choice) => refKey(choice.ref) === processKey)?.name
  const printerName = printers.find((choice) => refKey(choice.ref) === printerKey)?.name
  const suggested = [printerName, processName].filter(Boolean).join(' · ')
  const chosenFilaments = filamentKeys.map((key) => findRef(filaments, key))
  const complete =
    Boolean(findRef(printers, printerKey)) &&
    Boolean(findRef(processes, processKey)) &&
    chosenFilaments.every(Boolean) &&
    chosenFilaments.length > 0

  async function create() {
    const printerPreset = findRef(printers, printerKey)
    const processPreset = findRef(processes, processKey)
    if (!printerPreset || !processPreset) return
    const body: PipelineCreate = {
      name: name.trim() || suggested || 'ScadBuddy pipeline',
      printer_preset: printerPreset,
      process_preset: processPreset,
      // Bambuddy rejects an empty list (minItems: 1) — one preset per slot, in order.
      filament_presets: chosenFilaments.filter((ref): ref is PresetRef => Boolean(ref)),
      bed_type: bedType || null,
    }
    setSaving(true)
    setError(null)
    try {
      onCreated(await api.createPipeline(body))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not create the pipeline.')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading presets
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <div>
        <label htmlFor="pipeline-printer-preset" className="block text-[13px]">
          Printer preset
        </label>
        <select
          id="pipeline-printer-preset"
          value={printerKey}
          onChange={(event) => {
            setPrinterKey(event.target.value)
            setProcessKey('')
            setFilamentKeys(colors.map(() => ''))
          }}
          className="sb-field mt-1.5 cursor-pointer"
        >
          <option value="">Choose a printer preset</option>
          {printers.map((choice) => (
            <option key={refKey(choice.ref)} value={refKey(choice.ref)}>
              {choice.name}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label htmlFor="pipeline-process-preset" className="block text-[13px]">
          Process preset
        </label>
        <select
          id="pipeline-process-preset"
          value={processKey}
          disabled={processes.length === 0}
          onChange={(event) => setProcessKey(event.target.value)}
          className="sb-field mt-1.5 cursor-pointer"
        >
          <option value="">
            {printerKey ? 'Choose a process preset' : 'Choose a printer preset first'}
          </option>
          {processes.map((choice) => (
            <option key={refKey(choice.ref)} value={refKey(choice.ref)}>
              {choice.name}
            </option>
          ))}
        </select>
        <p className="mt-1.5 text-[12px] text-muted">
          The nozzle diameter is part of this preset&rsquo;s name &mdash; Bambuddy keeps it
          there, so ScadBuddy does not ask for it separately.
        </p>
      </div>

      <fieldset>
        <legend className="text-[13px]">Filament per colour</legend>
        <ul className="mt-1.5 space-y-2">
          {colors.map((colour, slot) => (
            <li key={`${colour}-${slot}`} className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="size-4 shrink-0 rounded-[3px] border border-line"
                style={{ backgroundColor: colour }}
              />
              <label htmlFor={`pipeline-filament-${slot}`} className="sr-only">
                {`Filament for slot ${slot + 1}`}
              </label>
              <select
                id={`pipeline-filament-${slot}`}
                value={filamentKeys[slot] ?? ''}
                disabled={filaments.length === 0}
                onChange={(event) =>
                  setFilamentKeys((current) =>
                    current.map((key, index) => (index === slot ? event.target.value : key)),
                  )
                }
                className="sb-field cursor-pointer"
              >
                <option value="">
                  {printerKey ? 'Choose a filament preset' : 'Choose a printer preset first'}
                </option>
                {filaments.map((choice) => (
                  <option key={refKey(choice.ref)} value={refKey(choice.ref)}>
                    {choice.name}
                    {choice.filament_type ? ` (${choice.filament_type})` : ''}
                  </option>
                ))}
              </select>
            </li>
          ))}
        </ul>
        <p className="mt-1.5 text-[12px] text-muted">
          One per slot, in the plate&rsquo;s order. Which AMS tray each slot prints from is a
          separate choice &mdash; see #87.
        </p>
      </fieldset>

      <div>
        <label htmlFor="pipeline-bed-type" className="block text-[13px]">
          Bed type
        </label>
        <select
          id="pipeline-bed-type"
          value={bedType}
          onChange={(event) => setBedType(event.target.value)}
          className="sb-field mt-1.5 cursor-pointer"
        >
          <option value="">Inherit from the process preset</option>
          {bedTypes.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label htmlFor="pipeline-name" className="block text-[13px]">
          Name
        </label>
        <input
          id="pipeline-name"
          type="text"
          value={name}
          placeholder={suggested || 'ScadBuddy pipeline'}
          onChange={(event) => setName(event.target.value)}
          className="sb-field mt-1.5"
        />
      </div>

      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}

      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          onClick={() => void create()}
          disabled={!complete || saving}
          aria-busy={saving}
        >
          {saving && <Spinner />}
          Create pipeline
        </Button>
        <Button onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
