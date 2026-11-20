import { PlusIcon, Trash2Icon } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { Link } from 'react-router'
import type { Spool, SpoolType } from '../../../api/filament'
import type { InvoiceLine, SupplierProduct } from '../../../api/filamentPurchasing'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { NEW_TAB } from '../../../lib/embed'
import { AppLink } from '../../AppLink'
import { SpoolLink } from '../SpoolLink'
import { RgbaSwatch } from '../RgbaSwatch'
import { TD, TH } from '../formClasses'
import { blankLine, fillable, type LineDraft, type LineErrors } from './invoiceLines'
import { SpoolMatchPicker } from './SpoolMatchPicker'

const NONE = '__none__'
const PROPOSED = '__proposed__'

interface Shared {
  /** A money amount in the invoice's currency, with its own digits. */
  money: (amount: string) => string
  types: SpoolType[]
  spools: Spool[]
}

interface EditableProps extends Shared {
  rows: LineDraft[]
  /** The server's line for each row, by the row's key: its cost, tax and basis are shown read-only. */
  computed: ReadonlyMap<string, InvoiceLine>
  errors: LineErrors
  /** Lines are edited and not saved yet: the server's figures beside them are out of date. */
  pending?: boolean
  products: SupplierProduct[]
  onChange: (rows: LineDraft[]) => void
  /** A field was left, or a choice made: save the lines soon. */
  onCommit: () => void
}

const field = 'h-8 px-2 text-[13px]'

/** What the lines of a draft are: typed in, saved as they are left, the server's figures beside them. */
function Editable({ rows, computed, errors, pending = false, products, types, spools, money, onChange, onCommit }: EditableProps) {
  const change = (at: number, patch: Partial<LineDraft>) => onChange(rows.map((row, i) => (i === at ? { ...row, ...patch } : row)))
  const typeOf = (id: string) => types.find((type) => type.id === id)
  const table = useRef<HTMLTableElement>(null)
  const addButton = useRef<HTMLButtonElement>(null)
  // A line added by the button: its description takes focus once the row is there.
  const focusLine = useRef<number | null>(null)
  useEffect(() => {
    if (focusLine.current === null) return
    table.current?.querySelector<HTMLInputElement>(`input[aria-label="Description, line ${focusLine.current}"]`)?.focus()
    focusLine.current = null
  }, [rows.length])
  const stale = pending ? 'opacity-50' : ''
  const taken = (at: number) => new Set(rows.flatMap((row, i) => (i === at ? [] : row.updateSpoolIds)))

  function pickType(at: number, value: string) {
    const row = rows[at]!
    const typeId = value === NONE || value === PROPOSED ? '' : value
    const keep = value === PROPOSED ? row.proposedType : null
    const stays = row.updateSpoolIds.filter((id) => {
      const spool = spools.find((one) => one.id === id)
      return spool && (spool.type_id === null || spool.type_id === typeId)
    })
    change(at, { typeId, proposedType: keep, updateSpoolIds: stays })
    onCommit()
  }

  function leaveSku(at: number) {
    const row = rows[at]!
    const sku = row.sku.trim().toLowerCase()
    const product = row.productId === '' && sku ? products.find((one) => !one.archived_at && one.sku.toLowerCase() === sku) : undefined
    if (product) change(at, { productId: product.id, ...(row.typeId === '' && !row.proposedType ? { typeId: product.type_id } : {}) })
    onCommit()
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="relative overflow-x-auto rounded-md border">
        <table ref={table} aria-label="Invoice lines" className="w-full min-w-[46rem] text-left text-[13px]">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th scope="col" className={TH}>Item</th>
              <th scope="col" className={TH}>Quantity</th>
              <th scope="col" className={TH}>Unit price</th>
              <th scope="col" className={TH}>Line tax</th>
              <th scope="col" className={TH}>Type</th>
              <th scope="col" className={`${TH} text-right`}>Cost</th>
              <th scope="col" className={TH}>
                <span className="sr-only">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, at) => {
              const n = at + 1
              const problems = errors[row.key] ?? {}
              const server = computed.get(row.key)
              const type = typeOf(row.typeId)
              const candidates = fillable(spools, row.typeId, taken(at))
              const ticked = row.updateSpoolIds.map((id) => spools.find((one) => one.id === id)).filter((one): one is Spool => !!one)
              const quantity = Number(row.quantity) >= 1 ? Number(row.quantity) : 1
              return (
                <tr key={row.key} data-line={n} className="border-t align-top">
                  <td className={`${TD} min-w-52`}>
                    <div className="flex flex-col gap-1">
                      <Input className={field} aria-label={`Description, line ${n}`} aria-invalid={problems.description ? true : undefined} value={row.description} onChange={(e) => change(at, { description: e.target.value })} onBlur={onCommit} />
                      {problems.description && <p className="text-[12px] text-warn">{problems.description}</p>}
                      <div className="flex gap-1">
                        <Input className={field} aria-label={`SKU, line ${n}`} placeholder="SKU" value={row.sku} onChange={(e) => change(at, { sku: e.target.value })} onBlur={() => leaveSku(at)} />
                        <div className="relative flex-1">
                          <Input className={`${field} ${type ? 'pl-7' : ''}`} aria-label={`Colour code, line ${n}`} placeholder="Colour code" value={row.colourCode} onChange={(e) => change(at, { colourCode: e.target.value })} onBlur={onCommit} />
                          {type && <RgbaSwatch rgba={type.rgba ?? '#FFFFFFFF'} size="sm" className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2" />}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td className={TD}>
                    <Input
                      className={`${field} w-16`}
                      inputMode="numeric"
                      aria-label={`Quantity, line ${n}`}
                      aria-invalid={problems.quantity ? true : undefined}
                      value={row.quantity}
                      onChange={(e) => change(at, { quantity: e.target.value })}
                      onBlur={() => {
                        // Fewer spools than it fills is refused at confirm: drop the extra ticks now.
                        if (/^\d+$/.test(row.quantity) && row.updateSpoolIds.length > Number(row.quantity)) change(at, { updateSpoolIds: row.updateSpoolIds.slice(0, Number(row.quantity)) })
                        onCommit()
                      }}
                    />
                    {problems.quantity && <p className="mt-1 max-w-24 text-[12px] text-warn">{problems.quantity}</p>}
                  </td>
                  <td className={TD}>
                    <Input className={`${field} w-24`} inputMode="decimal" aria-label={`Unit price, line ${n}`} aria-invalid={problems.unitPrice ? true : undefined} value={row.unitPrice} onChange={(e) => change(at, { unitPrice: e.target.value })} onBlur={onCommit} />
                    {problems.unitPrice && <p className="mt-1 max-w-28 text-[12px] text-warn">{problems.unitPrice}</p>}
                  </td>
                  <td className={TD}>
                    <Input className={`${field} w-20`} inputMode="decimal" aria-label={`Line tax, line ${n}`} placeholder="Receipt-wide" aria-invalid={problems.lineTax ? true : undefined} value={row.lineTax} onChange={(e) => change(at, { lineTax: e.target.value })} onBlur={onCommit} />
                    {problems.lineTax && <p className="mt-1 max-w-28 text-[12px] text-warn">{problems.lineTax}</p>}
                    {server && (
                      <p className={`mt-1 text-[12px] text-muted-foreground ${stale}`} title={pending ? 'Worked out again when the lines are saved' : undefined}>
                        {money(server.line_tax)}
                        {server.tax_basis !== 'none' && server.tax_basis !== 'line' && <span className="block">{server.tax_basis}</span>}
                      </p>
                    )}
                  </td>
                  <td className={`${TD} min-w-48`}>
                    <div className="flex flex-col gap-1">
                      <Select value={row.typeId || (row.proposedType ? PROPOSED : NONE)} onValueChange={(next) => pickType(at, next)}>
                        <SelectTrigger size="sm" aria-label={`Type, line ${n}`} className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={NONE}>No type</SelectItem>
                          {row.proposedType && <SelectItem value={PROPOSED}>New type {row.proposedType.code}</SelectItem>}
                          {types
                            .filter((one) => !one.archived_at || one.id === row.typeId)
                            .map((one) => (
                              <SelectItem key={one.id} value={one.id}>
                                {one.code}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                      <AppLink href="/filament/types/new" {...NEW_TAB} className="w-fit text-[12px] underline underline-offset-4">
                        New type
                      </AppLink>
                      <Select
                        value={row.productId || NONE}
                        onValueChange={(next) => {
                          change(at, { productId: next === NONE ? '' : next })
                          onCommit()
                        }}
                      >
                        <SelectTrigger size="sm" aria-label={`Supplier product, line ${n}`} className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={NONE}>Product by SKU</SelectItem>
                          {products
                            .filter((one) => !one.archived_at || one.id === row.productId)
                            .map((one) => (
                              <SelectItem key={one.id} value={one.id}>
                                {one.sku}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                      <SpoolMatchPicker
                        line={n}
                        candidates={[...ticked.filter((one) => !candidates.some((c) => c.id === one.id)), ...candidates]}
                        quantity={quantity}
                        value={row.updateSpoolIds}
                        money={money}
                        onChange={(next) => {
                          change(at, { updateSpoolIds: next })
                          onCommit()
                        }}
                      />
                      {/* #6700: the line's cost replaces a filled spool's own at confirm. */}
                      {ticked.some((one) => one.cost_per_kg !== null) && (
                        <ul className="flex flex-col text-[12px] text-muted-foreground">
                          {ticked
                            .filter((one) => one.cost_per_kg !== null)
                            .map((one) => (
                              <li key={one.id}>
                                Spool #{one.number} replaces {money(one.cost_per_kg!)}/kg
                              </li>
                            ))}
                        </ul>
                      )}
                    </div>
                  </td>
                  <td className={`${TD} text-right tabular-nums ${stale}`} title={pending ? 'Worked out again when the lines are saved' : undefined}>
                    {server ? (
                      <>
                        {money(server.allocated_cost)}
                        {server.unit_cost && <span className="block text-[12px] text-muted-foreground">{money(server.unit_cost)} each</span>}
                      </>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className={`${TD} text-right`}>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove line ${n}`}
                      onClick={() => {
                        onChange(rows.filter((_, i) => i !== at))
                        onCommit()
                        // The row and its button are gone: focus the button that adds one.
                        addButton.current?.focus()
                      }}
                    >
                      <Trash2Icon aria-hidden />
                    </Button>
                  </td>
                </tr>
              )
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className={`${TD} text-muted-foreground`}>
                  No lines yet. Add one for each product on the receipt.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          ref={addButton}
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            focusLine.current = rows.length + 1
            onChange([...rows, blankLine()])
          }}
        >
          <PlusIcon aria-hidden />
          Add line
        </Button>
        <p className="text-[12px] text-muted-foreground">Leave every line tax empty to spread the receipt&apos;s tax over the lines by price.</p>
      </div>
    </div>
  )
}

/** A confirmed invoice's lines: what was bought, what it cost, and the spools it made. */
function Frozen({ lines, types, spools, money }: Shared & { lines: InvoiceLine[] }) {
  const spoolNumber = new Map(spools.map((spool) => [spool.id, spool.number]))
  return (
    <div className="relative overflow-x-auto rounded-md border">
      <table aria-label="Invoice lines" className="w-full min-w-[40rem] text-left text-[13px]">
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className={TH}>Item</th>
            <th scope="col" className={`${TH} text-right`}>Quantity</th>
            <th scope="col" className={`${TH} text-right`}>Unit price</th>
            <th scope="col" className={TH}>Type</th>
            <th scope="col" className={`${TH} text-right`}>Cost</th>
            <th scope="col" className={TH}>Spools</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line, at) => {
            const type = types.find((one) => one.id === line.type_id)
            return (
              <tr key={line.id} data-line={at + 1} className="border-t align-top">
                <td className={TD}>
                  {line.description}
                  {(line.sku || line.colour_code) && <span className="block text-[12px] text-muted-foreground">{[line.sku, line.colour_code].filter(Boolean).join(' · ')}</span>}
                </td>
                <td className={`${TD} text-right tabular-nums`}>{line.quantity}</td>
                <td className={`${TD} text-right tabular-nums`}>{money(line.unit_price)}</td>
                <td className={TD}>
                  {type ? (
                    <Link to={`/filament/types/${type.id}`} className="inline-flex items-center gap-2 underline-offset-4 hover:underline">
                      <RgbaSwatch rgba={type.rgba ?? '#FFFFFFFF'} size="sm" />
                      {type.code}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className={`${TD} text-right tabular-nums`}>
                  {money(line.allocated_cost)}
                  <span className="block text-[12px] text-muted-foreground">
                    tax {money(line.line_tax)}
                    {line.tax_basis !== 'none' && line.tax_basis !== 'line' ? `, ${line.tax_basis}` : ''}
                  </span>
                </td>
                <td className={TD}>
                  <ul className="flex flex-wrap gap-x-2 gap-y-1">
                    {line.spool_ids.map((id) => (
                      <li key={id}>
                        <SpoolLink spool={{ id }} className="underline-offset-4 hover:underline">
                          Spool #{spoolNumber.get(id) ?? '?'}
                        </SpoolLink>
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function InvoiceLinesTable(props: ({ editable: true } & EditableProps) | ({ editable: false; lines: InvoiceLine[] } & Shared)) {
  return props.editable ? <Editable {...props} /> : <Frozen {...props} />
}
