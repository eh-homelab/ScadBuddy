import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { listTypes, type SpoolType } from '../../api/filament'
import { addShopping, clearShopping, listShopping, patchShopping, removeShopping, type ShoppingItem, type ShoppingItemCreate } from '../../api/filamentPurchasing'
import { AppLink } from '../../components/AppLink'
import { TD, TH } from '../../components/filament/formClasses'
import { spoolPath } from '../../components/filament/spoolsQuery'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { message } from '../../components/filament/purchasing/priceFormat'

/** Remembered per browser: the last quantity of spools added. */
export const QUANTITY_KEY = 'scadbuddy.filament.shopping.quantity'

const NO_TYPE = '__none__'
const LINK = 'underline underline-offset-2 hover:text-foreground'
const QUANTITY = /^\d+$/


function rememberedQuantity(): string {
  try {
    const value = window.localStorage.getItem(QUANTITY_KEY)
    return value && QUANTITY.test(value) ? value : '1'
  } catch {
    return '1'
  }
}

function remember(quantity: string) {
  try {
    window.localStorage.setItem(QUANTITY_KEY, quantity)
  } catch {
    // The choice is a convenience: the page works without it.
  }
}

/** Pending items first, each group in the order the server sent (newest first). */
const pendingFirst = (items: ShoppingItem[]) => [...items.filter((item) => item.status !== 'purchased'), ...items.filter((item) => item.status === 'purchased')]

/** What an item is called on a button: its colour and material. */
const nameOf = (item: ShoppingItem) => [item.colour_name, item.material].filter(Boolean).join(' ')

type Bulk = 'purchased' | 'all'

/**
 * #6266 — the Shopping list tab: what to buy next. An item is added from a type or typed by
 * hand, marked purchased (it moves below the pending ones), or removed. "Clear purchased"
 * and "Clear all" ask first. The last quantity used is remembered. #6868: a spool that runs
 * out or is archived adds its type (Settings → Filament, `filament_shopping_auto_add_empty`);
 * such an item carries an "Auto" mark, and its note links to that spool.
 */
export function ShoppingListTab() {
  const [items, setItems] = useState<ShoppingItem[] | null>(null)
  const [types, setTypes] = useState<SpoolType[]>([])
  const [error, setError] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<Bulk | null>(null)
  const [typeId, setTypeId] = useState('')
  const [quantity, setQuantity] = useState(rememberedQuantity)
  const [text, setText] = useState({ material: '', subtype: '', brand: '', colour_name: '' })
  const [note, setNote] = useState('')
  const latest = useRef(0)

  const load = useCallback(async () => {
    const mine = ++latest.current
    try {
      const [rows, all] = await Promise.all([listShopping(), listTypes({ includeArchived: true })])
      if (mine !== latest.current) return
      setItems(rows)
      setTypes(all)
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause, 'The shopping list could not be loaded. Try again.'))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const typeById = useMemo(() => new Map(types.map((type) => [type.id, type])), [types])
  const shown = useMemo(() => pendingFirst(items ?? []), [items])
  const hasPurchased = shown.some((item) => item.status === 'purchased')

  async function run(action: () => Promise<unknown>, fallback: string) {
    setError(null)
    setBusy(true)
    try {
      await action()
      await load()
    } catch (cause) {
      setError(message(cause, fallback))
    } finally {
      setBusy(false)
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault()
    const count = quantity.trim()
    if (!QUANTITY.test(count) || Number(count) < 1 || Number(count) > 1000) {
      setFormError('Enter a whole number of spools, from 1 to 1000.')
      return
    }
    const body: ShoppingItemCreate = { quantity_spools: Number(count) }
    if (typeId) body.type_id = typeId
    else {
      if (!text.material.trim()) {
        setFormError('Choose a type, or enter the material.')
        return
      }
      body.material = text.material.trim()
      if (text.subtype.trim()) body.subtype = text.subtype.trim()
      if (text.brand.trim()) body.brand = text.brand.trim()
      if (text.colour_name.trim()) body.colour_name = text.colour_name.trim()
    }
    if (note.trim()) body.note = note.trim()
    setFormError(null)
    setBusy(true)
    try {
      await addShopping(body)
      remember(String(Number(count)))
      setText({ material: '', subtype: '', brand: '', colour_name: '' })
      setNote('')
      setTypeId('')
      await load()
    } catch (cause) {
      setFormError(message(cause, 'The item could not be added. Try again.'))
    } finally {
      setBusy(false)
    }
  }

  const bulk = {
    purchased: { title: 'Clear purchased items?', body: 'The purchased items leave the list. Pending items stay.', action: 'Clear purchased', run: () => clearShopping({ purchasedOnly: true }) },
    all: { title: 'Clear the whole list?', body: 'Every item leaves the list, pending ones too. This cannot be undone.', action: 'Clear all', run: () => clearShopping() },
  } as const

  return (
    <div className="flex flex-col gap-4">
      <form className="flex flex-col gap-3 rounded-md border p-3" onSubmit={(event) => void add(event)} noValidate aria-label="Add to the shopping list">
        <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_6rem_minmax(0,2fr)]">
          <div className="flex min-w-0 flex-col gap-1">
            <Label className="text-[13px]">Type</Label>
            <Select
              value={typeId || NO_TYPE}
              onValueChange={(next) => {
                setTypeId(next === NO_TYPE ? '' : next)
                setFormError(null)
              }}
            >
              <SelectTrigger aria-label="Type" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_TYPE}>Another product (type it below)</SelectItem>
                {types
                  .filter((type) => !type.archived_at)
                  .map((type) => (
                    <SelectItem key={type.id} value={type.id}>
                      {type.code}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="shopping-quantity" className="text-[13px]">
              Spools
            </Label>
            <Input
              id="shopping-quantity"
              inputMode="numeric"
              value={quantity}
              onChange={(event) => {
                setQuantity(event.target.value)
                setFormError(null)
              }}
            />
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <Label htmlFor="shopping-note" className="text-[13px]">
              Note
            </Label>
            <Input id="shopping-note" value={note} onChange={(event) => setNote(event.target.value)} />
          </div>
        </div>
        {!typeId && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {(
              [
                ['material', 'Material'],
                ['subtype', 'Subtype'],
                ['brand', 'Brand'],
                ['colour_name', 'Colour name'],
              ] as const
            ).map(([key, label]) => (
              <div key={key} className="flex min-w-0 flex-col gap-1">
                <Label htmlFor={`shopping-${key}`} className="text-[13px]">
                  {label}
                </Label>
                <Input
                  id={`shopping-${key}`}
                  value={text[key]}
                  onChange={(event) => {
                    setText({ ...text, [key]: event.target.value })
                    setFormError(null)
                  }}
                />
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" size="sm" disabled={busy}>
            Add to list
          </Button>
          {formError && (
            <p role="alert" className="text-[13px] text-warn">
              {formError}
            </p>
          )}
        </div>
      </form>

      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold tracking-tight">To buy</h2>
        <div className="ml-auto flex gap-2">
          <Button type="button" variant="outline" size="sm" disabled={busy || !hasPurchased} onClick={() => setConfirm('purchased')}>
            Clear purchased
          </Button>
          <Button type="button" variant="outline" size="sm" disabled={busy || shown.length === 0} onClick={() => setConfirm('all')}>
            Clear all
          </Button>
        </div>
      </div>
      <p className="-mt-2 text-[13px] text-muted-foreground">
        A spool that runs out adds its type here, unless the type is already on the list. Turn it off in{' '}
        <AppLink href="/settings/printing#filament" className={LINK}>
          Settings
        </AppLink>
        .
      </p>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!items && !error && <p className="text-[13px] text-muted-foreground">Loading the shopping list…</p>}
      {items && shown.length === 0 && <p className="text-[13px] text-muted-foreground">Nothing to buy. Add what you need next.</p>}
      {items && shown.length > 0 && (
        <div className="relative overflow-x-auto rounded-md border">
          <table aria-label="Shopping list" className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className="w-7 px-1 py-1.5">
                  <span className="sr-only">Colour chip</span>
                </th>
                <th scope="col" className={TH}>Material</th>
                <th scope="col" className={TH}>Subtype</th>
                <th scope="col" className={TH}>Brand</th>
                <th scope="col" className={TH}>Colour</th>
                <th scope="col" className={`${TH} text-right`}>Spools</th>
                <th scope="col" className={TH}>Note</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((item) => {
                const bought = item.status === 'purchased'
                const type = item.type_id ? typeById.get(item.type_id) : undefined
                return (
                  <tr key={item.id} data-item={item.id} className={`border-t ${bought ? 'text-muted-foreground' : ''}`}>
                    <td className="px-1 py-1.5">{type && <RgbaSwatch rgba={type.rgba} size="md" />}</td>
                    <td className={TD}>{item.material}</td>
                    <td className={TD}>{item.subtype}</td>
                    <td className={TD}>{item.brand}</td>
                    <td className={TD}>{item.colour_name}</td>
                    <td className={`${TD} text-right tabular-nums`}>{item.quantity_spools}</td>
                    <td className={TD}>
                      {item.empty_spool_id && (
                        <span title="Added when a spool ran out or was archived" className="mr-1.5 inline-block rounded-sm border px-1 text-[11px] leading-4 text-muted-foreground">
                          Auto
                        </span>
                      )}
                      {item.empty_spool_id && item.note ? (
                        <AppLink href={spoolPath({ id: item.empty_spool_id })} className={LINK}>
                          {item.note}
                        </AppLink>
                      ) : (
                        item.note
                      )}
                    </td>
                    <td className={TD}>{bought ? 'Purchased' : 'Pending'}</td>
                    <td className={`${TD} whitespace-nowrap text-right`}>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        aria-label={`Mark ${nameOf(item)} ${bought ? 'pending' : 'purchased'}`}
                        onClick={() => void run(() => patchShopping(item.id, { status: bought ? 'pending' : 'purchased' }), 'The item could not be changed. Try again.')}
                      >
                        {bought ? 'Mark pending' : 'Mark purchased'}
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        aria-label={`Remove ${nameOf(item)}`}
                        onClick={() => void run(() => removeShopping(item.id), 'The item could not be removed. Try again.')}
                      >
                        Remove
                      </Button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <AlertDialogContent>
          {confirm && (
            <>
              <AlertDialogHeader>
                <AlertDialogTitle>{bulk[confirm].title}</AlertDialogTitle>
                <AlertDialogDescription>{bulk[confirm].body}</AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep them</AlertDialogCancel>
                <AlertDialogAction variant="destructive" onClick={() => void run(bulk[confirm].run, 'The list could not be cleared. Try again.')}>
                  {bulk[confirm].action}
                </AlertDialogAction>
              </AlertDialogFooter>
            </>
          )}
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
