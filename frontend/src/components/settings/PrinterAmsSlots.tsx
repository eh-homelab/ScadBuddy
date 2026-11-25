import { ChevronRightIcon, PencilIcon, RotateCwIcon } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api/client'
import { listSpools } from '../../api/filament'
import type { AmsUnitView, AmsView, PrinterRow, SlotCommandSent, SlotConfigureBody, SlotPresetBody, SlotView } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { cn } from '@/lib/utils'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { ChoiceSelect, LabeledInput, LoadingLines } from './form'
import { Spinner } from '../ui/spinner'

/**
 * #7153 — a printer's AMS slots, controlled through Bambuddy (`backend/scadbuddy/api/printer_slots.py`,
 * plan 2026-11-23-ams-slot-control). Closed until opened: the read asks Bambuddy's live status
 * (R-7153-9). Every write that reaches the printer asks first, in a dialog naming the printer and the
 * slot (R-7153-10), and the route refuses it while the printer prints (R-7153-3), so the buttons are
 * off then too. A unit's label is Bambuddy's and names ScadBuddy's place of the unit (R-7153-8).
 */

type Action = SlotCommandSent['action']

const ASKS: Record<Action, { title: string; does: string; verb: string }> = {
  configure: { title: 'Configure', does: 'sets the slot’s material, colour, preset and temperatures on the printer', verb: 'Configure' },
  load: { title: 'Load', does: 'feeds this slot’s filament to the hotend', verb: 'Load' },
  unload: { title: 'Unload', does: 'pulls the filament back out of the hotend', verb: 'Unload' },
  refresh: { title: 'Read the tag again', does: 'makes the AMS read the spool’s RFID tag again', verb: 'Read the tag' },
  reset: { title: 'Reset', does: 'clears the slot’s filament setting and its saved preset', verb: 'Reset' },
}

const NOZZLES = [
  { value: '', label: 'As the printer reports' },
  ...['0.2', '0.4', '0.6', '0.8'].map((value) => ({ value, label: `${value} mm` })),
]

type PresetSource = NonNullable<SlotPresetBody['preset_source']>

const PRESET_SOURCES: { value: PresetSource; label: string }[] = [
  { value: 'cloud', label: 'Bambu cloud' },
  { value: 'local', label: 'Local' },
  { value: 'builtin', label: 'Built-in' },
  { value: 'orca_cloud', label: 'Orca cloud' },
]

function problem(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

export function PrinterAmsSlots({ row }: { row: PrinterRow }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <Button size="xs" variant="ghost" aria-expanded={open} className="-ml-2">
          <ChevronRightIcon className={open ? 'rotate-90 transition-transform' : 'transition-transform'} />
          AMS slots
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>{open && <AmsBody row={row} />}</CollapsibleContent>
    </Collapsible>
  )
}

interface Pending {
  action: Action
  unit: AmsUnitView
  slot: SlotView
  body?: SlotConfigureBody
  presetSource?: PresetSource
}

function AmsBody({ row }: { row: PrinterRow }) {
  const ams = useAsync(() => api.printerAms(row.id), [row.id])
  const [picked, setPicked] = useState<{ ams: number; tray: number } | null>(null)
  const [configuring, setConfiguring] = useState(false)
  const [asking, setAsking] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; warn: boolean } | null>(null)
  const view = ams.data

  async function send(pending: Pending) {
    setBusy(true)
    setMessage(null)
    const { unit, slot } = pending
    try {
      if (pending.action === 'configure' && pending.body) {
        const sent = await api.configureSlot(row.id, unit.ams_id, slot.tray_id, pending.body)
        let text = `Configured ${slot.label}: ${sent.material} ${sent.preset_name}.`
        let warn = false
        try {
          // The saved preset follows what the slot was sent: a spool's type preset too, and none clears it.
          // A spool's preset carries its source from the backend; the form's choice is for an id typed by hand.
          if (sent.preset_id) {
            await api.setSlotPreset(row.id, unit.ams_id, slot.tray_id, {
              preset_id: sent.preset_id,
              preset_name: sent.preset_name,
              preset_source: sent.preset_source ?? pending.presetSource ?? 'cloud',
              tray_info_idx: sent.preset_id,
            })
          } else if (slot.saved_preset) {
            await api.clearSlotPreset(row.id, unit.ams_id, slot.tray_id)
          }
        } catch (cause) {
          warn = true
          text += ` The slot’s saved preset was not changed: ${problem(cause, 'Bambuddy refused it.')}`
        }
        setMessage({ text, warn })
        setConfiguring(false)
      } else {
        const sent = await api.slotCommand(row.id, unit.ams_id, slot.tray_id, pending.action)
        setMessage({ text: `${ASKS[pending.action].title}: sent to ${slot.label}. ${sent.message}`.trim(), warn: false })
      }
      ams.refresh()
    } catch (cause) {
      setMessage({ text: problem(cause, `Could not ${ASKS[pending.action].verb.toLowerCase()} ${slot.label}.`), warn: true })
    } finally {
      setBusy(false)
    }
  }

  async function clearPreset(unit: AmsUnitView, slot: SlotView) {
    setBusy(true)
    setMessage(null)
    try {
      await api.clearSlotPreset(row.id, unit.ams_id, slot.tray_id)
      setMessage({ text: `Cleared the saved preset of ${slot.label}.`, warn: false })
      ams.refresh()
    } catch (cause) {
      setMessage({ text: problem(cause, 'Could not clear the saved preset.'), warn: true })
    } finally {
      setBusy(false)
    }
  }

  const unit = view?.units.find((u) => u.ams_id === picked?.ams)
  const slot = unit?.slots.find((s) => s.tray_id === picked?.tray)
  const locked = busy || !view?.connected || view.printing || view.ams_busy

  return (
    <div className="mt-2 space-y-3" role="region" aria-label={`AMS slots of ${row.name}`}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {view && <StateLine view={view} />}
        <Button size="xs" variant="ghost" disabled={ams.loading} aria-label="Read the AMS again" onClick={() => ams.refresh()}>
          <RotateCwIcon />
        </Button>
        {(busy || ams.loading) && <Spinner />}
      </div>

      {ams.error ? (
        <p role="alert" className="text-xs text-warn">
          {problem(ams.error, 'The AMS could not be read.')}
        </p>
      ) : !view ? (
        <LoadingLines label="Reading the AMS from Bambuddy" />
      ) : (
        view.units.map((u) => (
          <UnitBlock
            key={u.ams_id}
            row={row}
            unit={u}
            picked={picked?.ams === u.ams_id ? picked.tray : null}
            onPick={(tray) => {
              setPicked({ ams: u.ams_id, tray })
              setConfiguring(false)
            }}
            onLabelled={(text, warn) => {
              setMessage({ text, warn })
              ams.refresh()
            }}
          />
        ))
      )}

      {unit && slot && (
        <div role="group" aria-label={`Actions for ${slot.label}`} className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-xs font-medium">{slot.label}</span>
          <Button size="xs" variant="outline" disabled={locked || configuring} onClick={() => setConfiguring(true)}>
            Configure…
          </Button>
          {slot.loadable && (
            <Button
              size="xs"
              variant="outline"
              disabled={locked || !slot.filled || slot.loaded}
              onClick={() => setAsking({ action: 'load', unit, slot })}
            >
              Load
            </Button>
          )}
          {slot.loadable && (
            <Button size="xs" variant="outline" disabled={locked} onClick={() => setAsking({ action: 'unload', unit, slot })}>
              Unload
            </Button>
          )}
          {slot.refreshable && (
            <Button
              size="xs"
              variant="outline"
              disabled={locked || view == null || (view.tray_now != null && view.tray_now !== 255)}
              onClick={() => setAsking({ action: 'refresh', unit, slot })}
            >
              Read the tag again
            </Button>
          )}
          <Button size="xs" variant="outline" disabled={locked} onClick={() => setAsking({ action: 'reset', unit, slot })}>
            Reset
          </Button>
          {slot.saved_preset && (
            <Button size="xs" variant="ghost" disabled={busy} onClick={() => void clearPreset(unit, slot)}>
              Clear saved preset
            </Button>
          )}
        </div>
      )}

      {unit && slot && configuring && (
        <ConfigureForm
          slot={slot}
          onCancel={() => setConfiguring(false)}
          onSubmit={(body, presetSource) => setAsking({ action: 'configure', unit, slot, body, presetSource })}
        />
      )}

      {message && (
        <p role="status" className={cn('text-xs', message.warn ? 'text-warn' : 'text-muted-foreground')}>
          {message.text}
        </p>
      )}

      <AlertDialog open={asking !== null} onOpenChange={(next) => !next && setAsking(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {asking ? `${ASKS[asking.action].title} ${asking.slot.label} on ${row.name}?` : ''}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {asking
                ? `This ${ASKS[asking.action].does}. It goes to the printer through Bambuddy now, and the printer may move.`
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const pending = asking
                setAsking(null)
                if (pending) void send(pending)
              }}
            >
              {asking ? ASKS[asking.action].verb : 'Send'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function StateLine({ view }: { view: AmsView }) {
  if (!view.connected) return <span className="text-warn">Bambuddy is not connected to {view.name}. Slot changes wait until it is.</span>
  if (view.printing)
    return <span className="text-warn">{view.name} is printing. Slot changes wait until the print has ended.</span>
  if (view.ams_busy) return <span className="text-warn">The AMS is busy. Slot changes wait until it is idle.</span>
  return <span className="text-muted-foreground">{view.name} is idle. Pick a slot to change it.</span>
}

/** One AMS unit as the physical unit lays it out: its slots side by side, each with the spool's colour as its band. */
function UnitBlock({
  row,
  unit,
  picked,
  onPick,
  onLabelled,
}: {
  row: PrinterRow
  unit: AmsUnitView
  picked: number | null
  onPick: (tray: number) => void
  onLabelled: (text: string, warn: boolean) => void
}) {
  const [renaming, setRenaming] = useState(false)
  const [label, setLabel] = useState(unit.label ?? '')
  const [saving, setSaving] = useState(false)
  const external = unit.ams_id === 255

  async function save(clear: boolean) {
    setSaving(true)
    try {
      const saved = clear ? await api.clearAmsLabel(row.id, unit.ams_id) : await api.setAmsLabel(row.id, unit.ams_id, label.trim())
      const place =
        saved.place_problem === 'no_ams_kind'
          ? ' Its place was not named: no location kind is marked AMS.'
          : saved.place_problem === 'no_printer'
            ? ' Its place was not named: the printer was deleted meanwhile.'
            : ''
      onLabelled(clear ? `Cleared the label of ${unit.unit}.${place}` : `Labelled ${unit.unit} “${label.trim()}”.${place}`, place !== '')
      setRenaming(false)
    } catch (cause) {
      onLabelled(problem(cause, 'Could not save the label.'), true)
    } finally {
      setSaving(false)
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (label.trim()) void save(false)
  }

  return (
    <section aria-label={unit.unit} className="space-y-1.5">
      <header className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h4 className="text-[13px] font-semibold">{unit.label ?? unit.unit}</h4>
        {unit.label && <span className="text-xs text-muted-foreground">{unit.unit}</span>}
        {unit.place_name && unit.place_name !== unit.label && (
          <span className="text-xs text-muted-foreground">Place: {unit.place_name}</span>
        )}
        {!external && !renaming && (
          <Button size="icon-xs" variant="ghost" aria-label={`Rename ${unit.unit}`} onClick={() => { setLabel(unit.label ?? ''); setRenaming(true) }}>
            <PencilIcon />
          </Button>
        )}
      </header>
      {renaming && (
        <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
          <LabeledInput
            label={`Label for ${unit.unit}`}
            value={label}
            maxLength={100}
            onChange={(event) => setLabel(event.target.value)}
            className="w-56"
          />
          <Button size="xs" type="submit" disabled={saving || !label.trim()}>
            Save label
          </Button>
          {unit.label && (
            <Button size="xs" type="button" variant="ghost" disabled={saving} onClick={() => void save(true)}>
              Clear label
            </Button>
          )}
          <Button size="xs" type="button" variant="ghost" disabled={saving} onClick={() => setRenaming(false)}>
            Cancel
          </Button>
        </form>
      )}
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
        {unit.slots.map((slot) => (
          <button
            key={slot.tray_id}
            type="button"
            aria-pressed={picked === slot.tray_id}
            aria-label={slot.label}
            onClick={() => onPick(slot.tray_id)}
            className={cn(
              'group overflow-hidden rounded-md border text-left text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring',
              picked === slot.tray_id ? 'border-foreground' : 'hover:border-foreground/40',
            )}
          >
            <span
              aria-hidden
              className={cn('block h-3 border-b', !slot.filled && 'bg-[repeating-linear-gradient(135deg,transparent_0_4px,var(--color-border)_4px_5px)]')}
              style={slot.filled && slot.colour ? { backgroundColor: slot.colour.slice(0, 7) } : undefined}
            />
            <span className="block space-y-0.5 px-2 py-1.5">
              <span className="flex items-center justify-between gap-1">
                <span className="font-medium">{slot.filled ? slot.material : 'Empty'}</span>
                <span className="tabular-nums text-muted-foreground">{slot.tray_id + 1}</span>
              </span>
              <span className="block truncate text-muted-foreground">{slot.filled ? slot.preset_name ?? '—' : ' '}</span>
              {slot.loaded && <span className="block font-medium">Feeding the hotend</span>}
              {slot.saved_preset && (
                <span className="block truncate text-muted-foreground" title={slot.saved_preset.preset_name}>
                  Saved: {slot.saved_preset.preset_name}
                </span>
              )}
            </span>
          </button>
        ))}
      </div>
    </section>
  )
}

function ConfigureForm({
  slot,
  onCancel,
  onSubmit,
}: {
  slot: SlotView
  onCancel: () => void
  onSubmit: (body: SlotConfigureBody, presetSource?: PresetSource) => void
}) {
  const spools = useAsync(() => listSpools(), [])
  const [spool, setSpool] = useState('')
  const [material, setMaterial] = useState(slot.material ?? '')
  const [colour, setColour] = useState((slot.colour ?? '#FFFFFFFF').slice(0, 7))
  const [presetId, setPresetId] = useState(slot.preset_id ?? '')
  const [presetName, setPresetName] = useState(slot.preset_name ?? '')
  const [low, setLow] = useState(slot.nozzle_temp_min?.toString() ?? '')
  const [high, setHigh] = useState(slot.nozzle_temp_max?.toString() ?? '')
  const [nozzle, setNozzle] = useState('')
  const [presetSource, setPresetSource] = useState<PresetSource>('cloud')
  const fromSpool = spool !== ''

  function submit(event: FormEvent) {
    event.preventDefault()
    const body: SlotConfigureBody = fromSpool
      ? { spool_id: spool }
      : {
          material: material.trim(),
          colour,
          preset_id: presetId.trim() || null,
          preset_name: presetName.trim() || null,
          nozzle_temp_min: low ? Number(low) : null,
          nozzle_temp_max: high ? Number(high) : null,
        }
    if (nozzle) body.nozzle_diameter = nozzle as SlotConfigureBody['nozzle_diameter']
    onSubmit(body, fromSpool ? undefined : presetSource)
  }

  const spoolChoices = [
    { value: '', label: 'None: set the values by hand' },
    ...(spools.data ?? []).map((s) => ({
      value: s.id,
      label: `#${s.number} ${[s.names?.brand, s.names?.material, s.colour_name].filter(Boolean).join(' ')}`,
    })),
  ]

  return (
    <form onSubmit={submit} aria-label={`Configure ${slot.label}`} className="grid gap-2 rounded-md border p-3 sm:grid-cols-2">
      <div className="grid gap-1.5 sm:col-span-2">
        <span className="text-xs font-normal">From a spool</span>
        <ChoiceSelect aria-label="From a spool" size="sm" value={spool} onValueChange={setSpool} choices={spoolChoices} />
        {fromSpool && (
          <p className="text-xs text-muted-foreground">
            The slot takes the spool’s material, colour and temperatures, and its type’s preset and K value for the slot’s
            nozzle.
          </p>
        )}
      </div>
      {!fromSpool && (
        <>
          <LabeledInput label="Material" value={material} required maxLength={32} onChange={(e) => setMaterial(e.target.value)} />
          <LabeledInput label="Colour" type="color" value={colour} onChange={(e) => setColour(e.target.value.toUpperCase())} />
          <LabeledInput label="Preset id" value={presetId} maxLength={64} placeholder="GFA00" onChange={(e) => setPresetId(e.target.value)} />
          <LabeledInput label="Preset name" value={presetName} maxLength={64} onChange={(e) => setPresetName(e.target.value)} />
          <LabeledInput label="Nozzle min (°C)" type="number" min={0} max={500} required value={low} onChange={(e) => setLow(e.target.value)} />
          <LabeledInput label="Nozzle max (°C)" type="number" min={0} max={500} required value={high} onChange={(e) => setHigh(e.target.value)} />
        </>
      )}
      {!fromSpool && (
        <div className="grid gap-1.5">
          <span className="text-xs font-normal">Preset source</span>
          <ChoiceSelect
            aria-label="Preset source"
            size="sm"
            value={presetSource}
            onValueChange={(value) => setPresetSource(value as PresetSource)}
            choices={PRESET_SOURCES}
          />
        </div>
      )}
      <div className="grid gap-1.5">
        <span className="text-xs font-normal">Nozzle</span>
        <ChoiceSelect aria-label="Nozzle" size="sm" value={nozzle} onValueChange={setNozzle} choices={NOZZLES} />
      </div>
      <div className="flex items-end gap-2 sm:col-span-2">
        <Button size="xs" type="submit">
          Configure {slot.label}…
        </Button>
        <Button size="xs" type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
