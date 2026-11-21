import { ChevronDownIcon, ChevronLeftIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listSpools, listTypes, type Spool, type SpoolType } from '../../api/filament'
import {
  confirmInvoice,
  deleteInvoice,
  getInvoice,
  patchInvoice,
  listProducts,
  listSuppliers,
  putInvoiceLines,
  type InvoiceDetail,
  type InvoiceLine,
  type InvoicePatch,
  type Supplier,
  type SupplierProduct,
} from '../../api/filamentPurchasing'
import { InvoiceFile } from '../../components/filament/purchasing/InvoiceFile'
import { InvoiceImport } from '../../components/filament/purchasing/InvoiceImport'
import { InvoiceHeaderForm } from '../../components/filament/purchasing/InvoiceHeaderForm'
import { InvoiceLinesTable } from '../../components/filament/purchasing/InvoiceLinesTable'
import { bodyOfLines, confirmCounts, draftOfLine, hasType, lineErrors, taxBasisOf, type LineDraft, type LineErrors } from '../../components/filament/purchasing/invoiceLines'
import { InvoiceTotals } from '../../components/filament/purchasing/InvoiceTotals'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { addDecimals, formatCost } from '../../lib/decimal'
import { useLatest } from '../../lib/useLatest'
import { useLeaveGuard } from '../../lib/useLeaveGuard'
import { message } from '../../components/filament/purchasing/priceFormat'

/** How long after a field is left the lines are saved, so a tab through three fields is one save. */
const SAVE_DELAY_MS = 400

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-7xl px-4 py-6">
        <Link to="/filament/invoices" className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Invoices
        </Link>
        {children}
      </div>
    </div>
  )
}

/**
 * #6266 — one invoice at `/filament/invoices/:id`: its file beside the receipt typed in. A
 * draft is edited (the header saves on blur, the lines as they are left, debounced) until
 * it reconciles and every line has a type; "Confirm" then makes its spools. A confirmed
 * invoice is frozen but for its note and its file, and links its spools.
 */
export function InvoicePage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const money = useMoney()
  const [invoice, setInvoice] = useState<InvoiceDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [rows, setRows] = useState<LineDraft[]>([])
  const [errors, setErrors] = useState<LineErrors>({})
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  // The last lines save failed: the lines stay unsaved until a retry or a later edit saves them.
  const [failed, setFailed] = useState(false)
  // The keys of the rows the invoice's lines answer, by position: a row keeps its server figures (cost, tax) by key.
  const [savedKeys, setSavedKeys] = useState<string[]>([])
  const [types, setTypes] = useState<SpoolType[]>([])
  const [products, setProducts] = useState<SupplierProduct[]>([])
  const [spools, setSpools] = useState<Spool[]>([])
  const [suppliers, setSuppliers] = useState<Supplier[]>([])
  const [asking, setAsking] = useState<'confirm' | 'delete' | null>(null)
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  // On a phone the file starts folded, so the form leads; from `lg` it stands beside the form.
  const [fileOpen, setFileOpen] = useState(() => typeof matchMedia !== 'function' || matchMedia('(min-width: 1024px)').matches)

  const rowsRef = useRef<LineDraft[]>([])
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const version = useRef(0)
  const running = useRef(false)
  const again = useRef(false)
  // The page the answers belong to, and the newest save (header or lines) sent for it: an
  // answer that is not the newest would bring back a header the newer save has changed.
  const here = useLatest(id)
  const sent = useRef(0)
  // Saves in flight, and whether the newest one to finish failed: the server then holds what
  // the page cannot know, so the invoice is read again once nothing is in flight.
  const inflight = useRef(0)
  // Edits no save has taken yet: a field left with nothing changed sends nothing.
  const unsent = useRef(false)
  const stale = useRef(false)
  // The row key each line id of the invoice answers.
  const keyOfLine = useRef(new Map<string, string>())
  // Header saves in flight (state, so Confirm waits for them).
  const [headerSaves, setHeaderSaves] = useState(0)
  const heading = useRef<HTMLHeadingElement>(null)
  const focusHeading = useRef(false)

  const guard = useLeaveGuard(dirty)

  useEffect(() => {
    let live = true
    setInvoice(null)
    setLoadError(null)
    setFailed(false)
    setSaveError(null)
    sent.current += 1
    getInvoice(id).then(
      (found) => {
        if (!live) return
        const next = found.lines.map(draftOfLine)
        rowsRef.current = next
        version.current = 0
        setSavedKeys(next.map((row) => row.key))
        remember(found, next.map((row) => row.key))
        unsent.current = false
        setRows(next)
        setErrors({})
        setDirty(false)
        setInvoice(found)
      },
      (cause) => live && setLoadError(cause instanceof ApiError && cause.status === 404 ? 'That invoice is gone. Pick another from the Invoices tab.' : message(cause, 'The invoice could not be loaded. Try again.')),
    )
    return () => {
      live = false
      if (timer.current) clearTimeout(timer.current)
    }
  }, [id])

  const supplierId = invoice?.supplier_id
  useEffect(() => {
    if (!supplierId) return
    let live = true
    listProducts(supplierId, { includeArchived: true }).then(
      (rows) => live && setProducts(rows),
      () => undefined,
    )
    return () => {
      live = false
    }
  }, [supplierId])

  const readSpools = useCallback(() => {
    listSpools({ includeArchived: true }).then(setSpools, () => undefined)
  }, [])
  useEffect(() => {
    let live = true
    listTypes({ includeArchived: true }).then(
      (rows) => live && setTypes(rows),
      () => undefined,
    )
    listSuppliers({ includeArchived: true }).then(
      (rows) => live && setSuppliers(rows),
      () => undefined,
    )
    readSpools()
    return () => {
      live = false
    }
  }, [readSpools])

  function edit(next: LineDraft[]) {
    rowsRef.current = next
    version.current += 1
    unsent.current = true
    setRows(next)
    setDirty(true)
  }

  /** Remember which row each line of `found` answers (by line id), for the rows' server figures. */
  function remember(found: InvoiceDetail, keys: string[]) {
    keyOfLine.current = new Map(found.lines.map((line, at) => [line.id, keys[at]!]))
  }

  /**
   * The invoice as the server holds it, unless a save was sent since. With a save in flight
   * the read could be answered before that save commits: it waits for the save (`settle`).
   */
  function refetch(page: string) {
    if (inflight.current > 0) {
      stale.current = true
      return
    }
    const seq = ++sent.current
    getInvoice(page).then(
      (found) => {
        if (here.current !== page || seq !== sent.current) return
        setInvoice(found)
        // By line id, not position: rows swapped, added or removed in unsent edits keep their own figures.
        const keys = found.lines.map((line) => keyOfLine.current.get(line.id))
        if (keys.every((key): key is string => key !== undefined)) {
          remember(found, keys)
          setSavedKeys(keys)
        } else {
          keyOfLine.current = new Map()
          setSavedKeys([])
        }
      },
      () => undefined,
    )
  }

  /** A save is over: when it was the last in flight after a failure, read the invoice again. */
  function settle(page: string) {
    inflight.current -= 1
    if (inflight.current === 0 && stale.current) {
      stale.current = false
      if (here.current === page) refetch(page)
    }
  }

  async function saveOnce() {
    const at = version.current
    const sending = rowsRef.current
    if (!unsent.current || Object.keys(lineErrors(sending)).length > 0) return
    unsent.current = false
    const page = id
    const seq = ++sent.current
    inflight.current += 1
    setSaving(true)
    setSaveError(null)
    setFailed(false)
    try {
      const saved = await putInvoiceLines(page, bodyOfLines(sending))
      if (here.current !== page) return
      // An older answer is dropped: the newer save's answer is the truth.
      // The line ids are learnt from every lines answer: a header save sent after this one
      // carries no row keys, so a later refetch needs this map to place the server figures.
      remember(saved, sending.map((row) => row.key))
      if (seq === sent.current) {
        setInvoice(saved)
        setSavedKeys(sending.map((row) => row.key))
      }
      // An edit made while this was in flight is saved by the commit that follows it.
      if (version.current === at) setDirty(false)
    } catch (cause) {
      if (here.current !== page) return
      unsent.current = true
      if (seq === sent.current) stale.current = true
      setFailed(true)
      setSaveError(message(cause, 'The lines could not be saved. Try again.'))
    } finally {
      settle(page)
      if (here.current === page) setSaving(false)
    }
  }

  /** One header change (the form sends them): its answer replaces the invoice only if no later save was sent. */
  async function saveHeader(patch: InvoicePatch) {
    const page = id
    // A move drops the old supplier's products from the lines, here before any save can send them.
    const before = rowsRef.current
    if (patch.supplier_id && before.some((row) => row.productId)) {
      const next = before.map((row) => ({ ...row, productId: '' }))
      rowsRef.current = next
      setRows(next)
    }
    const seq = ++sent.current
    inflight.current += 1
    setHeaderSaves((n) => n + 1)
    try {
      const saved = await patchInvoice(page, patch)
      if (here.current !== page) return
      if (seq === sent.current) setInvoice(saved)
    } catch (cause) {
      if (here.current === page) {
        if (seq === sent.current) stale.current = true
        // Refused: the lines keep the products they had. A row whose product was picked
        // while the request was out keeps that pick.
        if (patch.supplier_id && before !== rowsRef.current) {
          const next = rowsRef.current.map((row) => (row.productId === '' ? { ...row, productId: before.find((one) => one.key === row.key)?.productId ?? '' } : row))
          rowsRef.current = next
          setRows(next)
        }
      }
      throw cause
    } finally {
      settle(page)
      setHeaderSaves((n) => n - 1)
    }
  }

  async function flush() {
    timer.current = null
    if (running.current) {
      again.current = true
      return
    }
    running.current = true
    try {
      do {
        again.current = false
        await saveOnce()
      } while (again.current)
    } finally {
      running.current = false
    }
  }

  /** A field was left: say what is wrong now, and save soon if nothing is. */
  function commit() {
    const found = lineErrors(rowsRef.current)
    setErrors(found)
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    if (Object.keys(found).length === 0) timer.current = setTimeout(() => void flush(), SAVE_DELAY_MS)
  }

  async function confirm() {
    setBusy(true)
    setRefusal(null)
    const page = id
    try {
      const made = await confirmInvoice(page)
      if (here.current !== page) return
      setInvoice(made)
      setDirty(false)
      focusHeading.current = true
      setAsking(null)
      readSpools()
    } catch (cause) {
      if (here.current === page) setRefusal(message(cause, 'The invoice could not be confirmed. Try again.'))
    } finally {
      if (here.current === page) setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    setRefusal(null)
    const page = id
    try {
      await deleteInvoice(page)
      if (here.current !== page) return
      setDirty(false)
      navigate('/filament/invoices')
    } catch (cause) {
      if (here.current !== page) return
      setRefusal(message(cause, 'The draft could not be deleted. Try again.'))
      setBusy(false)
    }
  }

  const reload = () => refetch(id)

  if (loadError) {
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">Invoice</h1>
        <p role="alert" className="mt-2 text-[13px] text-warn">
          {loadError}
        </p>
      </Shell>
    )
  }
  if (!invoice) {
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">Loading the invoice…</h1>
      </Shell>
    )
  }

  const draft = invoice.status === 'draft'
  const computed = new Map<string, InvoiceLine>()
  if (savedKeys.length === invoice.lines.length) savedKeys.forEach((key, at) => computed.set(key, invoice.lines[at]!))
  const format = (amount: string) => formatCost(amount, invoice.currency, invoice.minor_exponent)
  const lineSum = invoice.lines.map((line) => line.line_total).reduce(addDecimals, '0')

  const firstBad = rows.findIndex((row) => errors[row.key])
  const untyped = rows.findIndex((row) => !hasType(row))
  const reason = !draft
    ? null
    : rows.length === 0
      ? 'Add at least one line'
      : firstBad >= 0
        ? `Fix line ${firstBad + 1} first`
        : untyped >= 0
          ? `Line ${untyped + 1} has no type`
          : failed
            ? 'The lines are not saved'
            : dirty
              ? 'Saving the lines…'
              : headerSaves > 0
                ? 'Saving the header…'
                : money.currency === null
                ? "Reading the shop's currency…"
                : invoice.currency !== money.currency
                  ? `The invoice is in ${invoice.currency} but the shop's currency is ${money.currency}. Change the currency above.`
                  : invoice.reconciles !== true
                    ? `The total is ${format(invoice.difference ?? '0')} off the lines and shipping. Fix a price, the tax or the shipping.`
                    : null
  const counts = confirmCounts(rows)

  return (
    <Shell>
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <h1 ref={heading} tabIndex={-1} className="truncate text-lg font-semibold tracking-tight outline-none">
            Invoice {invoice.number ?? '(no number)'}
          </h1>
          <Badge variant={draft ? 'outline' : 'secondary'}>{draft ? 'Draft' : 'Confirmed'}</Badge>
        </div>
        {draft && (
          <>
            <InvoiceImport invoice={invoice} onChanged={reload} />
            <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => setAsking('delete')}>
              Delete draft
            </Button>
            <Button type="button" size="sm" disabled={reason !== null || busy} aria-describedby="confirm-reason" onClick={() => setAsking('confirm')}>
              Confirm
            </Button>
          </>
        )}
      </header>
      <div className="mb-4 min-h-5 text-[13px]" aria-live="polite">
        {draft && reason && (
          <p id="confirm-reason" className="flex items-center gap-2 text-muted-foreground">
            {reason}
            {failed && !saving && (
              <Button type="button" variant="outline" size="xs" onClick={() => void flush()}>
                Retry
              </Button>
            )}
          </p>
        )}
        {draft && !reason && saving && <span className="text-muted-foreground">Saving…</span>}
      </div>
      {saveError && (
        <p role="alert" className="mb-3 text-[13px] text-warn">
          {saveError}
        </p>
      )}
      {refusal && asking === null && (
        <p role="alert" className="mb-3 text-[13px] text-warn">
          {refusal}
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Collapsible open={fileOpen} onOpenChange={setFileOpen} className="min-w-0 lg:sticky lg:top-4 lg:self-start">
          <CollapsibleTrigger asChild>
            <Button type="button" variant="outline" size="sm" className="mb-2 w-full justify-between lg:hidden">
              {fileOpen ? 'Hide the file' : 'Show the file'}
              <ChevronDownIcon aria-hidden className={fileOpen ? 'rotate-180' : ''} />
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent forceMount className="lg:data-[state=closed]:block">
            <InvoiceFile invoiceId={invoice.id} file={invoice.file} onChanged={reload} />
          </CollapsibleContent>
        </Collapsible>
        <div className="flex min-w-0 flex-col gap-6">
          <InvoiceHeaderForm invoice={invoice} suppliers={suppliers} editable={draft} onSave={saveHeader} />
          <section aria-labelledby="invoice-lines-heading" className="flex min-w-0 flex-col gap-2">
            <h2 id="invoice-lines-heading" className="text-sm font-semibold tracking-tight">
              Lines
            </h2>
            {draft ? (
              <InvoiceLinesTable editable pending={dirty} rows={rows} computed={computed} errors={errors} products={products} types={types} spools={spools} money={format} onChange={edit} onCommit={commit} />
            ) : (
              <InvoiceLinesTable editable={false} lines={invoice.lines} types={types} spools={spools} money={format} />
            )}
          </section>
          <InvoiceTotals
            currency={invoice.currency}
            exponent={invoice.minor_exponent}
            subtotal={invoice.subtotal}
            tax={invoice.tax}
            taxBasis={taxBasisOf(invoice.lines)}
            shipping={invoice.shipping}
            lineSum={lineSum}
            total={invoice.total}
            difference={invoice.difference ?? '0'}
            reconciles={invoice.reconciles === true}
          />
        </div>
      </div>

      <AlertDialog open={asking === 'confirm'} onOpenChange={(open) => !open && !busy && setAsking(null)}>
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            // The Confirm button is gone once the invoice is confirmed: focus goes to the heading.
            if (!focusHeading.current) return
            focusHeading.current = false
            event.preventDefault()
            heading.current?.focus()
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Confirm this invoice?</AlertDialogTitle>
            <AlertDialogDescription>
              {plural(counts.made, 'spool', 'spools')} will be made, {counts.filled} filled, {plural(counts.prices, 'price point', 'price points')}. After this the lines are frozen; only the note and the file can change.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {refusal && (
            <p role="alert" className="text-[13px] text-warn">
              {refusal}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              onClick={(event) => {
                event.preventDefault()
                void confirm()
              }}
            >
              Make the spools
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={asking === 'delete'} onOpenChange={(open) => !open && !busy && setAsking(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this draft?</AlertDialogTitle>
            <AlertDialogDescription>The draft and its lines are removed. Its file goes with it. No spool is touched.</AlertDialogDescription>
          </AlertDialogHeader>
          {refusal && (
            <p role="alert" className="text-[13px] text-warn">
              {refusal}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Keep it</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={busy}
              onClick={(event) => {
                event.preventDefault()
                void remove()
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={guard.pending !== null} onOpenChange={(next) => !next && guard.stay()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
            <AlertDialogDescription>Some line changes have not been saved, and will be lost.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Stay</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={guard.leave}>
              Leave without saving
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Shell>
  )
}
