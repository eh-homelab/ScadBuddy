import { PlusIcon, Undo2Icon, XIcon } from 'lucide-react'
import { useId } from 'react'
import type { EffectiveKValue, TypeKValue, TypeKValueInput } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cmpDecimals } from '@/lib/decimal'
import { keyOf, type KPrinter } from './kValueRows'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The nozzle types Bambu Studio's printers report (`nozzle_type`). */
const NOZZLE_TYPES = ['stainless_steel', 'hardened_steel', 'tungsten_carbide', 'brass'] as const
/** The K value as typed: the API sends and takes it as a decimal string. */
const text = (value: TypeKValueInput['k_value']) => String(value)
/** Who set a saved row, said beside its name; a person's row says nothing (R-6714-19). */
const SOURCE_TEXT: Record<Exclude<TypeKValue['source'], 'person' | undefined>, string> = {
  profile: 'From linked Bambuddy spools',
  printer_calibration: 'Printer calibration',
  profile_fetch: 'From 3DFP',
}

interface Props {
  rows: TypeKValueInput[]
  onChange: (rows: TypeKValueInput[]) => void
  printers: KPrinter[]
  /**
   * A spool's effective K values. A value the spool does not override shows the type's,
   * dimmed, with "From type" and an Override button; an own row has "Reset to type".
   */
  inherited?: EffectiveKValue[]
  /**
   * The keys the spool's type itself holds a K value for (`kValueRows` `keyOf`), from the
   * type's own rows: `inherited` cannot say, because a row the spool overrides is shown
   * there as the spool's. An own row with a key in it has "Reset to type"; any other has Remove. With `inherited` and no `typeKeys` (the type is loading, or failed to load) an own row has no action.
   */
  typeKeys?: ReadonlySet<string>
  /**
   * The saved rows, by key (`keyOf`), which say who set each one: a printer calibration
   * may be replaced by a newer one, a person's row never is (R-6714-19). A row edited and
   * not yet saved says nothing, since its save makes it the person's (R-6714-22).
   */
  saved?: ReadonlyMap<string, TypeKValue>
}

/**
 * Whether two K values are one decimal, as the server's `k_identity` compares them
 * (`Decimal(...).normalize()`): `0.020` is `0.02`, but `0.02000000000000000001`, which a
 * float rounds to `0.02`, is not (#6857). Text that is not a plain decimal is no match.
 */
const sameK = (a: TypeKValueInput['k_value'], b: TypeKValue['k_value']) => {
  try {
    return cmpDecimals(String(a).trim(), String(b)) === 0
  } catch {
    return false
  }
}

/** Whether a draft row still holds what its saved row does (`0.020` is `0.02`). */
const unchanged = (row: TypeKValueInput, was: TypeKValue) =>
  sameK(row.k_value, was.k_value) &&
  (row.nozzle_type ?? null) === (was.nozzle_type ?? null) &&
  (row.name ?? null) === (was.name ?? null) &&
  (row.cali_idx ?? null) === (was.cali_idx ?? null) &&
  (row.setting_id ?? null) === (was.setting_id ?? null)


/**
 * #6262 — pressure advance (K) values by printer, extruder and nozzle, as Bambu Studio's
 * flow dynamics calibration stores one per filament, nozzle and extruder. The calibration
 * index and slicer setting id the printer reported stay on the row; a person edits the
 * rest.
 */
export function KValueTable({ rows, onChange, printers, inherited, typeKeys, saved }: Props) {
  const listId = useId()
  const set = (at: number, patch: Partial<TypeKValueInput>) => onChange(rows.map((row, i) => (i === at ? { ...row, ...patch } : row)))
  const known = new Set(printers.map((printer) => printer.id))
  const own = new Set(rows.map(keyOf))
  const fromType = (inherited ?? []).filter((row) => !row.overridden && !own.has(keyOf(row)))
  const printerName = (id: string) => printers.find((printer) => printer.id === id)?.name ?? `Printer ${id.slice(0, 8)}`
  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto rounded-md border">
        <table aria-label="K values" className="w-full text-left text-[13px]">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th scope="col" className="px-2 py-1.5 font-medium">Printer</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Extruder</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Nozzle (mm)</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Nozzle type</th>
              <th scope="col" className="px-2 py-1.5 text-right font-medium">K</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Name</th>
              <th scope="col" className="w-px px-2 py-1.5"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, at) => (
              <tr key={at} aria-label={`K value ${at + 1}`} className="border-t align-top">
                <td className="px-2 py-1">
                  <Select value={row.printer_id || undefined} onValueChange={(printer_id) => set(at, { printer_id })}>
                    <SelectTrigger aria-label="Printer" size="sm" className="w-44">
                      <SelectValue placeholder="Choose a printer" />
                    </SelectTrigger>
                    <SelectContent>
                      {printers.map((printer) => (
                        <SelectItem key={printer.id} value={printer.id}>
                          {printer.name}
                        </SelectItem>
                      ))}
                      {row.printer_id && !known.has(row.printer_id) && <SelectItem value={row.printer_id}>Printer {row.printer_id.slice(0, 8)}</SelectItem>}
                    </SelectContent>
                  </Select>
                </td>
                <td className="px-2 py-1">
                  <Input
                    aria-label="Extruder"
                    inputMode="numeric"
                    className="h-7 w-14 tabular-nums"
                    value={String(row.extruder)}
                    onChange={(e) => set(at, { extruder: e.target.value === '' ? 0 : Number.parseInt(e.target.value, 10) || 0 })}
                  />
                </td>
                <td className="px-2 py-1">
                  <Input aria-label="Nozzle" inputMode="decimal" className="h-7 w-20 tabular-nums" value={row.nozzle_diameter} onChange={(e) => set(at, { nozzle_diameter: e.target.value })} />
                </td>
                <td className="px-2 py-1">
                  <Input aria-label="Nozzle type" list={listId} className="h-7 w-36" value={row.nozzle_type ?? ''} onChange={(e) => set(at, { nozzle_type: e.target.value === '' ? null : e.target.value })} />
                </td>
                <td className="px-2 py-1">
                  <Input aria-label="K value" inputMode="decimal" className="h-7 w-20 text-right tabular-nums" value={text(row.k_value)} onChange={(e) => set(at, { k_value: e.target.value })} />
                </td>
                <td className="px-2 py-1">
                  <Input aria-label="K value name" className="h-7 min-w-36" value={row.name ?? ''} onChange={(e) => set(at, { name: e.target.value === '' ? null : e.target.value })} />
                  {(() => {
                    const was = saved?.get(keyOf(row))
                    const source = was && unchanged(row, was) ? was.source : undefined
                    return source && source !== 'person' ? <p className="mt-0.5 text-[12px] text-muted-foreground">{SOURCE_TEXT[source]}</p> : null
                  })()}
                </td>
                <td className="px-2 py-1 whitespace-nowrap">
                  {inherited && !typeKeys ? null : inherited && typeKeys?.has(keyOf(row)) ? (
                    <Button type="button" size="xs" variant="ghost" aria-label={`Reset K value ${at + 1} to type`} onClick={() => onChange(rows.filter((_, i) => i !== at))}>
                      <Undo2Icon /> Reset to type
                    </Button>
                  ) : (
                    <Button type="button" size="icon-xs" variant="ghost" aria-label={`Remove K value ${at + 1}`} onClick={() => onChange(rows.filter((_, i) => i !== at))}>
                      <XIcon />
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {fromType.map((row) => (
              <tr key={keyOf(row)} aria-label={`${printerName(row.printer_id)} ${row.nozzle_diameter} from type`} className="border-t opacity-60">
                <td className="px-2 py-1.5">{printerName(row.printer_id)}</td>
                <td className="px-2 py-1.5 tabular-nums">{row.extruder}</td>
                <td className="px-2 py-1.5 tabular-nums">{row.nozzle_diameter}</td>
                <td className="px-2 py-1.5">{row.nozzle_type}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{text(row.k_value)}</td>
                <td className="px-2 py-1.5">
                  {row.name} <span className="ml-1 text-[12px] text-muted-foreground">From type</span>
                </td>
                <td className="px-2 py-1 whitespace-nowrap">
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    aria-label={`Override ${printerName(row.printer_id)} ${row.nozzle_diameter}`}
                    onClick={() =>
                      onChange([
                        ...rows,
                        {
                          printer_id: row.printer_id,
                          extruder: row.extruder,
                          nozzle_diameter: row.nozzle_diameter,
                          nozzle_type: row.nozzle_type,
                          k_value: text(row.k_value),
                          name: row.name,
                          cali_idx: row.cali_idx,
                          setting_id: row.setting_id,
                        },
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
                <td colSpan={7} className="px-2 py-3 text-muted-foreground">
                  No K values. Add one for each printer, extruder and nozzle you calibrated this type on.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <datalist id={listId}>
        {NOZZLE_TYPES.map((one) => (
          <option key={one} value={one} />
        ))}
      </datalist>
      <div>
        <Button
          type="button"
          size="xs"
          variant="outline"
          disabled={printers.length === 0}
          onClick={() => onChange([...rows, { printer_id: printers[0]!.id, extruder: 0, nozzle_diameter: '0.4', nozzle_type: null, k_value: '', name: null }])}
        >
          <PlusIcon /> Add K value
        </Button>
      </div>
    </div>
  )
}
