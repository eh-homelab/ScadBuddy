import { PlusIcon, Undo2Icon, XIcon } from 'lucide-react'
import { useId } from 'react'
import type { EffectivePreset, PresetInput } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { keyOf } from './presetRows'

/** What a nozzle field may hold on the way to a diameter: digits and one point. */
const TYPING = /^\d*\.?\d*$/

interface Props {
  /** The presets this owner holds itself: a type's, or a spool's overrides. */
  rows: PresetInput[]
  onChange: (rows: PresetInput[]) => void
  /** Printer models to suggest. */
  models?: readonly string[]
  /**
   * A spool's effective presets. A row the spool does not override shows the type's value,
   * dimmed, with "From type" and an Override button; an overridden row has "Reset to type".
   */
  inherited?: EffectivePreset[]
  /**
   * The keys the spool's type itself holds a preset for (`presetRows` `keyOf`), from the
   * type's own rows: `inherited` cannot say, because a row the spool overrides is shown
   * there as the spool's. An own row with a key in it has "Reset to type"; any other has Remove. With `inherited` and no `typeKeys` (the type is loading, or failed to load) an own row has no action.
   */
  typeKeys?: ReadonlySet<string>
}

/**
 * #6262 — slicer filament presets by printer model and nozzle, as Bambu Studio keys a
 * filament preset to the printers and nozzles it is compatible with. Shared by the type
 * page and the spool page (`inherited`).
 */
export function PresetTable({ rows, onChange, models = [], inherited, typeKeys }: Props) {
  const listId = useId()
  const set = (at: number, patch: Partial<PresetInput>) => onChange(rows.map((row, i) => (i === at ? { ...row, ...patch } : row)))
  const own = new Set(rows.map(keyOf))
  const fromType = (inherited ?? []).filter((row) => !row.overridden && !own.has(keyOf(row)))

  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto rounded-md border">
        <table aria-label="Slicer presets" className="w-full text-left text-[13px]">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th scope="col" className="px-2 py-1.5 font-medium">Printer model</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Nozzle (mm)</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Slicer filament</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Preset name</th>
              <th scope="col" className="w-px px-2 py-1.5"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, at) => {
              const nozzle = row.nozzle_diameter.trim()
              const bad = nozzle !== '' && !TYPING.test(nozzle)
              const errorId = `${listId}-nozzle-${at}`
              return (
                <tr key={at} aria-label={row.printer_model ? `Preset ${row.printer_model} ${nozzle}` : 'New preset'} className="border-t align-top">
                  <td className="px-2 py-1">
                    <Input aria-label="Printer model" list={listId} className="h-7 w-28" value={row.printer_model} onChange={(e) => set(at, { printer_model: e.target.value })} />
                  </td>
                  <td className="px-2 py-1">
                    <Input
                      aria-label="Nozzle"
                      inputMode="decimal"
                      className="h-7 w-20 tabular-nums"
                      aria-invalid={bad || undefined}
                      aria-describedby={bad ? errorId : undefined}
                      value={row.nozzle_diameter}
                      onChange={(e) => set(at, { nozzle_diameter: e.target.value })}
                    />
                    {bad && (
                      <p id={errorId} role="alert" className="mt-1 max-w-48 text-[12px] text-warn">
                        A nozzle is a diameter in mm, such as 0.4. &quot;{nozzle}&quot; is not a nozzle.
                      </p>
                    )}
                  </td>
                  <td className="px-2 py-1">
                    <Input aria-label="Slicer filament" className="h-7 w-28 font-mono" value={row.slicer_filament} onChange={(e) => set(at, { slicer_filament: e.target.value })} />
                  </td>
                  <td className="px-2 py-1">
                    <Input
                      aria-label="Preset name"
                      className="h-7 min-w-48"
                      value={row.slicer_filament_name ?? ''}
                      onChange={(e) => set(at, { slicer_filament_name: e.target.value === '' ? null : e.target.value })}
                    />
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap">
                    {inherited && !typeKeys ? null : inherited && typeKeys?.has(keyOf(row)) ? (
                      <Button type="button" size="xs" variant="ghost" aria-label={`Reset preset ${at + 1} to type`} onClick={() => onChange(rows.filter((_, i) => i !== at))}>
                        <Undo2Icon /> Reset to type
                      </Button>
                    ) : (
                      <Button type="button" size="icon-xs" variant="ghost" aria-label={`Remove preset ${row.printer_model} ${nozzle}`} onClick={() => onChange(rows.filter((_, i) => i !== at))}>
                        <XIcon />
                      </Button>
                    )}
                  </td>
                </tr>
              )
            })}
            {fromType.map((row) => (
              <tr key={keyOf(row)} aria-label={`${row.printer_model} ${row.nozzle_diameter} from type`} className={cn('border-t opacity-60')}>
                <td className="px-2 py-1.5">{row.printer_model}</td>
                <td className="px-2 py-1.5 tabular-nums">{row.nozzle_diameter}</td>
                <td className="px-2 py-1.5 font-mono">{row.slicer_filament}</td>
                <td className="px-2 py-1.5">
                  {row.slicer_filament_name} <span className="ml-1 text-[12px] text-muted-foreground">From type</span>
                </td>
                <td className="px-2 py-1 whitespace-nowrap">
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    onClick={() =>
                      onChange([
                        ...rows,
                        { printer_model: row.printer_model, nozzle_diameter: row.nozzle_diameter, slicer_filament: row.slicer_filament, slicer_filament_name: row.slicer_filament_name ?? null },
                      ])
                    }
                  >
                    Override
                  </Button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && fromType.length === 0 && (
              <tr className="border-t">
                <td colSpan={5} className="px-2 py-3 text-muted-foreground">
                  No presets. Add one for each printer model and nozzle the slicer needs a filament for.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <datalist id={listId}>
        {models.map((model) => (
          <option key={model} value={model} />
        ))}
      </datalist>
      <div>
        <Button
          type="button"
          size="xs"
          variant="outline"
          onClick={() => onChange([...rows, { printer_model: '', nozzle_diameter: '0.4', slicer_filament: '', slicer_filament_name: null }])}
        >
          <PlusIcon /> Add preset
        </Button>
      </div>
    </div>
  )
}
