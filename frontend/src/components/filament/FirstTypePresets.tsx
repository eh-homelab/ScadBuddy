import type { EffectivePreset } from '../../api/filament'
import { keyOf } from './presetRows'

const DROPPED = 'Dropped when the type is saved'

/**
 * #6262 — the presets of a spool that has no type yet and has chosen its first, shown
 * read-only until the save: the backend's first type drops the rows its migration made
 * (`migrated`), keeps the ones a person saved, and the type's presets show for the keys
 * that are left (`presets.drop_migrated`). The note names exactly the rows that go.
 */
export function FirstTypePresets({ own, typeRows }: { own: EffectivePreset[]; typeRows: EffectivePreset[] }) {
  const dropped = own.filter((row) => row.migrated).map((row) => `${row.printer_model} ${row.nozzle_diameter}`)
  const rows = [
    ...own.map((row) => ({ row, state: row.migrated ? DROPPED : 'Kept' })),
    ...typeRows.filter((row) => !own.some((mine) => keyOf(mine) === keyOf(row) && !mine.migrated)).map((row) => ({ row, state: 'From type' })),
  ]
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[13px] text-muted-foreground">
        {dropped.length > 0
          ? `Saving the type drops ${dropped.join(', ')}, which this spool carried over from before it had a type. Presets you saved yourself stay.`
          : 'Presets you saved yourself stay when the type is saved.'}
      </p>
      <div className="overflow-x-auto rounded-md border">
        <table aria-label="Slicer presets" className="w-full text-left text-[13px]">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th scope="col" className="px-2 py-1.5 font-medium">Printer model</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Nozzle (mm)</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Slicer filament</th>
              <th scope="col" className="px-2 py-1.5 font-medium">After saving</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ row, state }) => (
              <tr
                key={`${state}-${keyOf(row)}`}
                aria-label={`${row.printer_model} ${row.nozzle_diameter} ${state}`}
                className={state === DROPPED ? 'border-t text-muted-foreground [&>td:not(:last-child)]:line-through' : 'border-t'}
              >
                <td className="px-2 py-1.5">{row.printer_model}</td>
                <td className="px-2 py-1.5 tabular-nums">{row.nozzle_diameter}</td>
                <td className="px-2 py-1.5 font-mono">{row.slicer_filament}</td>
                <td className="px-2 py-1.5">{state}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
