import { Fragment, useId, useState, type KeyboardEvent } from 'react'
import { MoreHorizontalIcon, PencilIcon } from 'lucide-react'
import { api } from '../../api/client'
import type { Order, OrderAttachment, OrderCosts, OrderItemDetail, OrderStatus, Quote, Shipment } from '../../api/types'
import { formatMoney, toMajor } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { CELL, DataTable, ROW, type Column } from '../records/DataTable'
import { ItemPicture } from './ItemPicture'
import { ItemStatusBadge } from './OrderStatusBadge'
import { byCreated, itemPrice, parseUnitPrice, pricingQuote, UNIT_PRICE_HINT, unitPrice } from './price'
import { parseQuantity, QUANTITY_HINT } from './quantity'
import { refusalText } from './refusal'
import { CUSTOM_ITEM_TITLE, itemAnchor } from './itemLink'
import { addItem, afterAcceptanceWarning, isCart, TAKES_ITEMS, type AddMode } from './afterAcceptance'
import { ItemsBulk } from './ItemsBulk'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import type { SliceQuoteRun, SliceQuoteRuns } from './useSliceQuotes'
import { elapsed, useNow } from './useNow'
import { When } from './When'
import { UnfulfilDialog } from './Unfulfil'
import { UNFULFIL_OFF as UNFULFIL_OFF_TEXT, UNFULFIL_SETTING } from './unfulfilRules'
import { itemActions, itemEdits, itemSource, type ItemAction, type ItemSection } from './itemActions'
import { MarginBadge } from './OrderCosts'
import { Link } from 'react-router'

/**
 * An order's line items (§3.3), as one card (#4955, spec 2026-11-07 §4.1): its header adds
 * a product or a custom item; a row is the picture, title with its source chip, status,
 * quantity, unit price, line total, margin badge and a ⋯ menu; a row opens the item. The
 * quantity, unit price and title are typed in place (`itemEdits`): Enter saves, Escape puts
 * back, Tab moves along the row. A typed unit price is recorded and accepted as the drawer's
 * "Record and accept" does. Past `quoted` an accepted item's quantity and price are pinned and
 * shown with the reason; an item revised after acceptance (#5350) takes a new price; the
 * title stays editable while the item is open. The ⋯ menu holds the actions allowed now
 * (`itemActions.ts`), a refused one with its reason; Unfulfil (#4952) confirms first.
 */
export function OrderItemsTable({
  orderId,
  orderStatus,
  order,
  items,
  attachments,
  currency,
  exponent,
  onOpen,
  onChanged,
  addTo = null,
  selected = null,
  quoting,
  shipments = [],
  unfulfilAllowed = true,
  onAdd,
  onSection,
  onAct,
  costs,
  busy = false,
  onWriting,
}: {
  orderId: string
  /** The order: the ⋯ menu's rules read its status and hold. */
  order: Order
  /** The order's status: a unit price is typed only while it is draft or quoted. */
  orderStatus: OrderStatus
  items: OrderItemDetail[]
  /** The order's files: an item's picture is its delivered photo once one is marked (#4251). */
  attachments: OrderAttachment[]
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  onOpen: (item: OrderItemDetail) => void
  /** After a price is recorded: the page reloads the order. */
  onChanged: () => void
  /** The page offers Add product (`afterAcceptance.ts` `addMode`), on the route `mode` names: the
   * empty table points at it, and the custom item row adds on that route. After acceptance
   * (#5541) the row asks the person to confirm first, naming the order by `number`. */
  addTo?: { mode: AddMode; number: string } | null
  /** The item open in the page's drawer (#4693). */
  selected?: string | null
  /** The page's slice quotes: a running one shows on its row (#4693). */
  quoting?: SliceQuoteRuns
  /** The order's shipments: Unfulfil is refused for an item one carries. */
  shipments?: Shipment[]
  /** #4953 — `orders_allow_unfulfil`: off, Unfulfil is disabled with a link to the setting. */
  unfulfilAllowed?: boolean
  /** The card header's Add product: the page opens Add item. */
  onAdd?: () => void
  /** A ⋯ action that is done in the drawer: the page opens the item at that section. */
  onSection?: (item: OrderItemDetail, section: ItemSection) => void
  /** A ⋯ action that writes (delete, cancel, reopen): the page runs it and shows a refusal. */
  onAct: (action: () => Promise<unknown>) => void
  /** #5326 — the order's money view: a row shows its item's loss or thin-margin badge. */
  costs?: OrderCosts
  /** A page write is running: the header's adds and the ⋯ menu's writes wait for it. */
  busy?: boolean
  /** A bulk write starts or ends: the page holds its other writes (`busy`) meanwhile. */
  onWriting?: (writing: boolean) => void
}) {
  const canAdd = addTo !== null
  // #4955 step 3 — the rows picked for a bulk action; none on a visitor's cart or an ended order.
  const selectable = !isCart(order) && order.status !== 'closed' && order.status !== 'cancelled'
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set())
  // An order that stops taking a selection (cancelled, closed) drops it, so a reopen starts
  // with nothing picked. Reset while rendering, as React's "adjusting state" pattern does.
  const [selectableWas, setSelectableWas] = useState(selectable)
  if (selectableWas !== selectable) {
    setSelectableWas(selectable)
    if (!selectable) setPicked(new Set())
  }
  const chosen = selectable ? items.filter((item) => picked.has(item.id)) : []
  const pick = (ids: string[], on: boolean) => {
    const next = new Set(picked)
    for (const id of ids) {
      if (on) next.add(id)
      else next.delete(id)
    }
    setPicked(next)
  }
  const columns: Column<OrderItemDetail>[] = []
  if (selectable && items.length > 0) {
    const all = chosen.length === items.length
    columns.push({
      key: 'select',
      header: (
        <Checkbox
          aria-label="Select all items"
          checked={all ? true : chosen.length > 0 ? 'indeterminate' : false}
          onCheckedChange={() => pick(items.map((item) => item.id), !all)}
        />
      ),
      className: 'w-8',
      cell: (item) => <Checkbox aria-label={`Select ${item.title}`} checked={picked.has(item.id)} onCheckedChange={(next) => pick([item.id], next === true)} />,
    })
  }
  columns.push(
    {
      key: 'picture',
      header: <span className="sr-only">Picture</span>,
      className: 'w-14',
      cell: (item) => <ItemPicture orderId={orderId} item={item} attachments={attachments} />,
    },
    {
      key: 'title',
      header: 'Item',
      className: 'break-words',
      cell: (item, open) => (
        <span className="flex flex-col items-start gap-0.5">
          <TitleEditor orderId={orderId} item={item} editable={itemEdits(order, item).title} open={open} onChanged={onChanged} pageBusy={busy} />
          {/* #5544 — a library file item names its file, by its name when it was added. */}
          {item.library_file && (
            <span className="text-[11px] break-all text-muted-foreground" data-testid="item-library-file">
              Library file{item.library_file.library === 'native' ? '' : ' in Bambuddy'}: {item.library_file.filename}
            </span>
          )}
          {/* #4777 — an item taken from a past print names the Bambuddy archives it came from. */}
          {(item.origin_archive_ids ?? []).length > 0 && (
            <span className="text-[11px] text-muted-foreground" data-testid="item-past-print">
              From past print {(item.origin_archive_ids ?? []).map((id) => `#${id}`).join(', ')}
            </span>
          )}
          {item.sku && <span className="font-mono text-[11px] text-muted-foreground">SKU {item.sku}</span>}
          <span className="rounded-sm border px-1 text-[10px] text-muted-foreground" data-testid={`item-source-${item.id}`}>
            {itemSource(item)}
          </span>
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      cell: (item) => <ItemStatus item={item} run={quoting?.get(item.id)} />,
    },
    {
      key: 'quantity',
      header: 'Qty',
      className: 'sb-num',
      cell: (item) =>
        itemEdits(order, item).quantity ? <QuantityEditor orderId={orderId} item={item} onChanged={onChanged} pageBusy={busy} /> : item.quantity,
    },
    {
      key: 'unit',
      header: 'Unit price',
      className: 'sb-num whitespace-nowrap',
      cell: (item) => {
        const edits = itemEdits(order, item)
        return (
          <span className="flex flex-col items-end gap-1">
            {edits.price ? (
              <UnitPriceEditor orderId={orderId} item={item} currency={currency} exponent={exponent} onChanged={onChanged} />
            ) : (
              <UnitPrice item={item} currency={currency} exponent={exponent} />
            )}
            {edits.locked && (
              <span className="max-w-56 text-right text-[11px] whitespace-normal text-muted-foreground" data-testid={`item-locked-${item.id}`}>
                {edits.locked}
              </span>
            )}
          </span>
        )
      },
    },
    {
      key: 'total',
      header: 'Line total',
      className: 'sb-num whitespace-nowrap',
      cell: (item) => {
        const price = itemPrice(item)
        if (price === null) return <span className="text-muted-foreground">Not priced</span>
        // A cancelled item is in no sum: its price shows struck through, so the column
        // adds up to the subtotal.
        if (item.status === 'cancelled') {
          return (
            <span className="flex flex-col items-end text-muted-foreground">
              <s>{formatMoney(price.amount, currency, exponent)}</s>
              <span className="text-[11px]">not counted</span>
            </span>
          )
        }
        const accepted = item.quotes.find((quote) => quote.accepted)
        return (
          <span className="flex flex-col items-end">
            {/* A draft's total counts before it is accepted; its unit price says so. */}
            <span className={price.accepted ? undefined : 'text-muted-foreground'}>{formatMoney(price.amount, currency, exponent)}</span>
            {/* #4250 — a price read off a slice names the file it was read from. */}
            {accepted?.source === 'slice' && accepted.sliced_sha256 && (
              <span className="font-mono text-[11px] text-muted-foreground" title={accepted.sliced_sha256}>
                slice {accepted.sliced_sha256.slice(0, 8)}
              </span>
            )}
          </span>
        )
      },
    },
  )
  const margins = new Map((costs?.items ?? []).map((cost) => [cost.item_id, cost.margin_state]))
  if (margins.size > 0) {
    columns.push({
      key: 'margin',
      header: <span className="sr-only">Margin</span>,
      cell: (item) => <MarginBadge state={margins.get(item.id) ?? null} />,
    })
  }
  columns.push({
    key: 'actions',
    header: <span className="sr-only">Actions</span>,
    className: 'w-10 text-right',
    cell: (item) => (
      <ItemMenu
        orderId={orderId}
        order={order}
        item={item}
        actions={itemActions(order, item, { shipments, unfulfilAllowed })}
        onOpen={() => onOpen(item)}
        onSection={(section) => (onSection ? onSection(item, section) : onOpen(item))}
        onAct={onAct}
        onChanged={onChanged}
        busy={busy}
      />
    ),
  })
  return (
    <DataTable
      label="Line items"
      columns={columns}
      rows={items}
      rowKey={(item) => item.id}
      rowTestId={(item) => `order-item-${item.id}`}
      rowId={(item) => itemAnchor(item.id)}
      selected={selected}
      onOpen={onOpen}
      heading="Items"
      toolbar={
        <>
          {chosen.length > 0 && (
            <ItemsBulk
              order={order}
              selected={chosen}
              shipments={shipments}
              unfulfilAllowed={unfulfilAllowed}
              busy={busy}
              onWriting={onWriting ?? (() => undefined)}
              onClear={() => setPicked(new Set())}
              onChanged={onChanged}
            />
          )}
          {canAdd && !isCart(order) && (
          <>
            <Button type="button" variant="outline" size="sm" disabled={busy} onClick={onAdd}>
              Add product
            </Button>
            {/* #4954 — the entry row under the items takes it (#5541: after acceptance too). */}
            <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => document.getElementById(CUSTOM_ITEM_TITLE)?.focus()}>
              Add custom item
            </Button>
          </>
          )}
        </>
      }
      empty={canAdd ? 'No items yet. Add one with Add product above, or type a custom item below.' : 'No items.'}
      footer={
        addTo ? (
          <CustomItemRow
            orderId={orderId}
            order={{ number: addTo.number, status: orderStatus }}
            mode={addTo.mode}
            currency={currency}
            exponent={exponent}
            leading={columns.findIndex((column) => column.key === 'picture')}
            trailing={columns.length - 1 - columns.findIndex((column) => column.key === 'total')}
            onChanged={onChanged}
          />
        ) : undefined
      }
    />
  )
}

/**
 * #5554 — an item's status, with when its quotes were made (the newest, and the accepted one
 * when it is another) and its slice quote's run: started and running for, or started, ended
 * and failed.
 */
function ItemStatus({ item, run }: { item: OrderItemDetail; run: SliceQuoteRun | undefined }) {
  const running = run && !run.error ? run : undefined
  const now = useNow(running !== undefined)
  const newest = [...item.quotes].sort(byCreated).at(-1)
  const accepted = item.quotes.find((quote) => quote.accepted)
  return (
    <span className="flex flex-col items-start gap-0.5">
      <ItemStatusBadge status={item.status} />
      {newest && (
        <span className="text-[11px] text-muted-foreground" data-testid={`item-quoted-${item.id}`}>
          {accepted?.id === newest.id ? 'Accepted quote' : 'Newest quote'} <When at={newest.created_at} />
        </span>
      )}
      {accepted && accepted.id !== newest?.id && (
        <span className="text-[11px] text-muted-foreground" data-testid={`item-accepted-${item.id}`}>
          Accepted quote <When at={accepted.created_at} />
        </span>
      )}
      {running && (
        <span className="text-[11px] text-muted-foreground" data-testid={`item-quoting-${item.id}`}>
          <span>Quoting…</span> since <When at={running.startedAt} /> ({elapsed(running.startedAt, now)})
        </span>
      )}
      {run?.error && (
        <span className="text-[11px] text-warn" data-testid={`item-quote-failed-${item.id}`} title={run.error}>
          <span>Quote failed</span> <When at={run.finishedAt ?? run.startedAt} />
        </span>
      )}
    </span>
  )
}

/** The actions that write at once, or after a confirm: off while a page write runs. */
const WRITES: ReadonlySet<ItemAction['key']> = new Set(['unfulfil', 'reopen', 'duplicate', 'delete', 'cancel'])

/** The actions done in the drawer, at the section they open it at. */
const SECTIONS: Partial<Record<ItemAction['key'], ItemSection>> = { quote: 'slice', print: 'print', fulfil: 'fulfil' }

/**
 * #4955 — a row's ⋯ menu: the actions allowed on the item now (`itemActions`), each refused
 * one disabled with its reason under it. Named without the item's title, as the unit price's
 * Accept is. Unfulfil confirms first; Unfulfil turned off links to its setting.
 */
function ItemMenu({
  orderId,
  order,
  item,
  actions,
  onOpen,
  onSection,
  onAct,
  onChanged,
  busy,
}: {
  orderId: string
  order: Order
  item: OrderItemDetail
  actions: ItemAction[]
  onOpen: () => void
  onSection: (section: ItemSection) => void
  onAct: (action: () => Promise<unknown>) => void
  onChanged: () => void
  /** A page write is running: the writes here wait for it. */
  busy: boolean
}) {
  const [confirming, setConfirming] = useState(false)
  // Delete and Cancel ask first: in the menu they sit one slip from Open details.
  const [removing, setRemoving] = useState<'delete' | 'cancel' | null>(null)
  const [duplicating, setDuplicating] = useState(false)
  function run(action: ItemAction) {
    const section = SECTIONS[action.key]
    if (section) return onSection(section)
    if (action.key === 'open') return onOpen()
    // The menu closes first: a dialog opened in the same tick would be closed with it.
    if (action.key === 'unfulfil') return setTimeout(() => setConfirming(true), 0)
    if (action.key === 'reopen') return onAct(() => api.reopenOrderItem(orderId, item.id))
    if (action.key === 'delete' || action.key === 'cancel') {
      const key = action.key
      return setTimeout(() => setRemoving(key), 0)
    }
    if (action.key === 'duplicate') {
      // After acceptance a copy changes what the customer gets: asked first, as Add item does.
      if (TAKES_ITEMS.has(order.status)) return setTimeout(() => setDuplicating(true), 0)
      return onAct(() => api.duplicateOrderItem(orderId, item.id, {}))
    }
  }
  const off = actions.some((action) => action.key === 'unfulfil' && action.blocked === UNFULFIL_OFF_TEXT)
  return (
    <>
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Row actions">
            <MoreHorizontalIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-auto min-w-44">
          {actions.map((action) => (
            <Fragment key={action.key}>
              <DropdownMenuItem
                variant={action.key === 'delete' || action.key === 'cancel' || action.key === 'unfulfil' ? 'destructive' : 'default'}
                disabled={action.blocked !== null || (busy && WRITES.has(action.key))}
                onSelect={() => run(action)}
              >
                {action.label}
              </DropdownMenuItem>
              {action.blocked && <DropdownMenuLabel className="max-w-56 text-xs font-normal whitespace-normal text-muted-foreground">{action.blocked}</DropdownMenuLabel>}
            </Fragment>
          ))}
          {off && (
            <DropdownMenuItem asChild>
              <Link to={UNFULFIL_SETTING}>Open Settings → Orders</Link>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <UnfulfilDialog orderId={orderId} item={item} open={confirming} onOpenChange={setConfirming} onChanged={onChanged} />
      <AlertDialog open={removing !== null} onOpenChange={(open) => !open && setRemoving(null)}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{`${removing === 'delete' ? 'Delete' : 'Cancel'} ${item.title}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              {removing === 'delete'
                ? 'The draft item and its quotes are deleted.'
                : 'The item stays on the order, in no sum, and can be reopened.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() => {
                const what = removing
                setRemoving(null)
                onAct(() => (what === 'delete' ? api.deleteOrderItem(orderId, item.id) : api.cancelOrderItem(orderId, item.id)))
              }}
            >
              {removing === 'delete' ? 'Delete item' : 'Cancel item'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog open={duplicating} onOpenChange={setDuplicating}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{`Duplicate ${item.title}?`}</AlertDialogTitle>
            <AlertDialogDescription>{afterAcceptanceWarning(order)}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              onClick={() => {
                setDuplicating(false)
                onAct(() => api.duplicateOrderItem(orderId, item.id, { after_acceptance: true }))
              }}
            >
              Duplicate
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

/**
 * #4954 — the empty row under the items: a custom item's title, quantity and unit price,
 * and optional SKU, notes and "Requires printing". Enter in any field saves it, priced
 * and accepted as it is saved (`kind: "custom"`); the row then clears for the next one.
 * On an accepted, in-production or ready order (`mode` `after_acceptance`, #5541) the first
 * save asks the person to confirm, as Add item does; the second adds the item on that route,
 * its price `quoted`, not accepted. A change to the row asks again.
 */
function CustomItemRow({
  orderId,
  order,
  mode,
  currency,
  exponent,
  leading,
  trailing,
  onChanged,
}: {
  orderId: string
  order: { number: string; status: OrderStatus }
  mode: AddMode
  currency: string
  exponent: number
  /** The table's columns before the picture (the bulk select), and after the line total
   * (margin, actions): empty cells. */
  leading: number
  trailing: number
  onChanged: () => void
}) {
  const errorId = useId()
  const blank = { title: '', quantity: '1', price: '', sku: '', notes: '', printing: false }
  const [draft, setDraft] = useState(blank)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const set = (fields: Partial<typeof blank>) => {
    setDraft({ ...draft, ...fields })
    setError(null)
    setConfirming(false)
  }

  async function submit() {
    const title = draft.title.trim()
    const quantity = parseQuantity(draft.quantity)
    const price = parseUnitPrice(draft.price, exponent)
    if (!title) return setError('Type a title for the custom item.')
    if (quantity === null) return setError(QUANTITY_HINT)
    if (price === null) return setError(UNIT_PRICE_HINT)
    if (mode === 'after_acceptance' && !confirming) return setConfirming(true)
    setBusy(true)
    setError(null)
    try {
      await addItem(orderId, {
        kind: 'custom',
        title,
        quantity,
        unit_price_minor: price,
        notes: draft.notes.trim(),
        ...(draft.sku.trim() ? { sku: draft.sku.trim() } : {}),
        requires_printing: draft.printing,
      }, mode)
      setDraft(blank)
      setConfirming(false)
      onChanged()
      document.getElementById(CUSTOM_ITEM_TITLE)?.focus()
    } catch (err) {
      setError(refusalText(err))
      // A failed add asks again before the next try.
      setConfirming(false)
    } finally {
      setBusy(false)
    }
  }

  // A row of a table holds no form, so Enter in any field saves the row.
  function enter(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== 'Enter') return
    event.preventDefault()
    if (!busy) void submit()
  }

  const described = error ? errorId : undefined
  return (
    <tr className={ROW} data-testid="custom-item-row">
      {Array.from({ length: leading }, (_, index) => (
        <td key={index} className={CELL} />
      ))}
      <td className={CELL}>
        <span className="flex size-12 items-center justify-center rounded-md border border-dashed text-[10px] text-muted-foreground">Custom</span>
      </td>
      <td className={CELL}>
        <span className="flex flex-col gap-1">
          <Input
            id={CUSTOM_ITEM_TITLE}
            aria-label="Custom item title"
            aria-describedby={described}
            className="h-7"
            placeholder="Custom item: design work, a fee…"
            value={draft.title}
            onKeyDown={enter}
            onChange={(event) => set({ title: event.target.value })}
          />
          <span className="flex gap-1">
            <Input aria-label="Custom item SKU" className="h-7 w-24" placeholder="SKU" value={draft.sku} onKeyDown={enter} onChange={(event) => set({ sku: event.target.value })} />
            <Input aria-label="Custom item notes" className="h-7" placeholder="Notes" value={draft.notes} onKeyDown={enter} onChange={(event) => set({ notes: event.target.value })} />
          </span>
          {confirming && (
            <p role="alert" aria-label="Confirm the change" className="rounded-md border border-warn/40 bg-warn/10 px-2 py-1 text-[11px] whitespace-normal">
              {afterAcceptanceWarning(order)}
            </p>
          )}
          {error && (
            <p id={errorId} role="alert" className="text-[11px] whitespace-normal text-warn">
              {error}
            </p>
          )}
        </span>
      </td>
      <td className={CELL}>
        <label className="flex items-center gap-1 text-[11px] whitespace-nowrap text-muted-foreground">
          <Checkbox checked={draft.printing} onKeyDown={enter} onCheckedChange={(next) => set({ printing: next === true })} />
          Requires printing
        </label>
      </td>
      <td className={`${CELL} sb-num`}>
        <Input aria-label="Custom item quantity" aria-describedby={described} inputMode="numeric" className="h-7 w-14 text-right" value={draft.quantity} onKeyDown={enter} onChange={(event) => set({ quantity: event.target.value })} />
      </td>
      <td className={`${CELL} sb-num`}>
        <Input aria-label="Custom item unit price" aria-describedby={described} inputMode="decimal" className="h-7 w-24 text-right" placeholder={currency} value={draft.price} onKeyDown={enter} onChange={(event) => set({ price: event.target.value })} />
      </td>
      <td className={`${CELL} sb-num`}>
        <Button type="button" size="xs" aria-label={confirming ? 'Confirm custom item' : 'Save custom item'} disabled={busy} onClick={() => void submit()}>
          {confirming ? 'Confirm' : 'Add'}
        </Button>
      </td>
      {Array.from({ length: trailing }, (_, index) => (
        <td key={index} className={CELL} />
      ))}
    </tr>
  )
}

/**
 * #4955 step 2 — the item's title, a link that opens the item, and while the item is open an
 * Edit title button that types it in place: Enter saves, Escape puts it back. The button is
 * named without the title, so a `{ name: <title> }` lookup finds only the link.
 */
function TitleEditor({
  orderId,
  item,
  editable,
  open,
  onChanged,
  pageBusy,
}: {
  orderId: string
  item: OrderItemDetail
  editable: boolean
  open: (() => void) | undefined
  onChanged: () => void
  /** A page write is running: Enter waits for it, as the ⋯ menu's writes do. */
  pageBusy: boolean
}) {
  const [typed, setTyped] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const errorId = useId()

  function close() {
    setTyped(null)
    setError(null)
  }

  async function save(text: string) {
    const title = text.trim()
    if (!title) return setError('Type a title for the item.')
    if (title === item.title) return close()
    setBusy(true)
    try {
      await api.updateOrderItem(orderId, item.id, { title })
      close()
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  if (typed === null) {
    return (
      <span className="flex items-start gap-1">
        <Button variant="link" className="h-auto p-0 text-left font-medium whitespace-normal text-foreground" onClick={open}>
          {item.title}
        </Button>
        {editable && (
          <Button type="button" variant="ghost" size="icon-xs" aria-label="Edit title" onClick={() => setTyped(item.title)}>
            <PencilIcon />
          </Button>
        )}
      </span>
    )
  }
  return (
    <span className="flex w-full flex-col gap-1">
      <Input
        autoFocus
        aria-label={`Title for ${item.title}`}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        className="h-7"
        value={typed}
        readOnly={busy}
        onChange={(event) => {
          setTyped(event.target.value)
          setError(null)
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            if (!busy && !pageBusy) void save(typed)
          } else if (event.key === 'Escape') {
            event.preventDefault()
            close()
          }
        }}
      />
      {error && (
        <p id={errorId} role="alert" className="text-[11px] whitespace-normal text-warn">
          {error}
        </p>
      )}
    </span>
  )
}

/**
 * #4955 step 2 — the quantity, typed in place while the item is open (`itemEdits`): Enter
 * saves it (a model item's accepted quote is dropped, as the drawer's Save item does; a custom
 * item is priced again at once), Escape puts back the quantity it has. A refusal shows under it.
 */
function QuantityEditor({ orderId, item, onChanged, pageBusy }: { orderId: string; item: OrderItemDetail; onChanged: () => void; pageBusy: boolean }) {
  const [typed, setTyped] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const errorId = useId()
  const text = typed ?? String(item.quantity)

  async function save() {
    const quantity = parseQuantity(text)
    if (quantity === null) return setError(QUANTITY_HINT)
    if (quantity === item.quantity) return setTyped(null)
    setBusy(true)
    setError(null)
    try {
      await api.updateOrderItem(orderId, item.id, { quantity })
      setTyped(null)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <span className="flex flex-col items-end gap-1">
      <Input
        aria-label={`Quantity for ${item.title}`}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        inputMode="numeric"
        className="h-7 w-16 text-right"
        value={text}
        // Read-only while it saves, not disabled: the input keeps its focus, so Tab goes on.
        readOnly={busy}
        onChange={(event) => {
          setTyped(event.target.value)
          setError(null)
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            if (!busy && !pageBusy) void save()
          } else if (event.key === 'Escape') {
            event.preventDefault()
            setTyped(null)
            setError(null)
          }
        }}
      />
      {error && (
        <p id={errorId} role="alert" className="max-w-40 text-right text-[11px] whitespace-normal text-warn">
          {error}
        </p>
      )}
    </span>
  )
}

/** A draft's price counts before it is accepted, and says so. */
function NotAccepted() {
  return <span className="text-[11px] text-muted-foreground">not accepted</span>
}

/** The unit price, shown only: the accepted quote's, else the newest quote's. */
function UnitPrice({ item, currency, exponent }: { item: OrderItemDetail; currency: string; exponent: number }) {
  const price = unitPrice(item)
  if (price === null) return <span className="text-muted-foreground">—</span>
  // A cancelled item's price is in no sum, accepted or not: the line total says so.
  const cancelled = item.status === 'cancelled'
  return (
    <span className={cancelled ? 'flex flex-col items-end text-muted-foreground' : 'flex flex-col items-end'}>
      {formatMoney(price.minor, currency, exponent)}
      {!price.accepted && !cancelled && <NotAccepted />}
    </span>
  )
}

/**
 * The quote the row's unit price reads: `pricingQuote`'s, except for an item revised after
 * acceptance (#5350), whose old accepted quote prices a design replaced since. It reads the
 * newest quote of its current design not yet accepted, or none.
 */
function editorQuote(item: OrderItemDetail): { quote: Quote; accepted: boolean } | null {
  if (!item.revised_after_acceptance) return pricingQuote(item)
  const pending = item.quotes.filter((quote) => !quote.accepted && quote.revision_id === item.current_revision_id).sort(byCreated).at(-1)
  return pending ? { quote: pending, accepted: false } : null
}

/**
 * The unit price, typed: Enter (or Accept) accepts the item's pending quote when the
 * price is that quote's and it is for the item's quantity (a slice quote keeps its sliced
 * file and cost), else records a `manual` quote at it and accepts that. Escape puts back
 * the price the item has. A refusal shows under it.
 */
function UnitPriceEditor({
  orderId,
  item,
  currency,
  exponent,
  onChanged,
}: {
  orderId: string
  item: OrderItemDetail
  currency: string
  exponent: number
  onChanged: () => void
}) {
  const priced = editorQuote(item)
  const price = priced ? { minor: priced.quote.unit_price_minor, accepted: priced.accepted } : null
  const current = price ? toMajor(price.minor, exponent) : ''
  // What was typed; null shows the item's price, so a reload brings in the new one.
  const [typed, setTyped] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const errorId = useId()
  const text = typed ?? current
  // The same amount however it is typed (12.5 is 12.50), so an accepted price stays put.
  const unchanged = price !== null && parseUnitPrice(text, exponent) === price.minor
  // Nothing to accept: the accepted quote prices the item at its quantity now.
  const settled = priced !== null && priced.accepted && priced.quote.quantity === item.quantity

  async function submit() {
    const minor = parseUnitPrice(text, exponent)
    if (minor === null) {
      setError(UNIT_PRICE_HINT)
      return
    }
    setBusy(true)
    setError(null)
    // The pending quote is accepted as it is only while it still prices this item: at the
    // typed price and at the item's quantity (a quantity change leaves it `quote_stale`).
    const pending = editorQuote(item)
    const reuse = pending && !pending.accepted && pending.quote.quantity === item.quantity && pending.quote.unit_price_minor === minor ? pending.quote : null
    let recorded = false
    try {
      if (item.kind === 'custom') {
        // #4954 — a custom item's price is its own field, accepted as it is saved.
        await api.updateOrderItem(orderId, item.id, { unit_price_minor: minor })
        setTyped(null)
        onChanged()
        return
      }
      const quote = reuse ?? (await api.quoteOrderItem(orderId, item.id, { unit_price_minor: minor }))
      recorded = reuse === null
      await api.acceptOrderQuote(orderId, item.id, quote.id)
      setTyped(null)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
      // A refused accept after a recorded quote: reload, so the row shows it and a retry
      // accepts that price rather than recording a second one. The typed text and the
      // refusal stay.
      if (recorded) onChanged()
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="flex flex-col items-end gap-1"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <span className="flex items-center gap-1">
        <Input
          aria-label={`Unit price for ${item.title}`}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          inputMode="decimal"
          className="h-7 w-24 text-right"
          placeholder={currency}
          value={text}
          disabled={busy}
          onChange={(event) => {
            setTyped(event.target.value)
            setError(null)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              setTyped(null)
              setError(null)
            }
          }}
        />
        {/* Named without the item's title: the input beside it names the item, and a
            button named after it would match every `{ name: <title> }` lookup of the row's
            title link (Playwright matches names by substring). */}
        <Button type="submit" size="xs" aria-label="Accept unit price" disabled={busy || (unchanged && settled)}>
          Accept
        </Button>
      </span>
      {((price && !price.accepted) || item.revised_after_acceptance) && <NotAccepted />}
      {error && (
        <p id={errorId} role="alert" className="max-w-56 text-right text-[11px] whitespace-normal text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
