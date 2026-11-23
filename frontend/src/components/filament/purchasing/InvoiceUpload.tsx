import { FileUpIcon } from 'lucide-react'
import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../../api/client'
import { filamentLinks } from '../../../lib/filamentLinks'
import { createInvoice, listSuppliers, uploadInvoiceFile, type Supplier } from '../../../api/filamentPurchasing'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { acceptableInvoiceFile } from './invoiceLines'
import { lastSupplier, rememberSupplier } from './invoicesQuery'
import { today } from './priceFormat'
import { useMoney } from './useMoney'

const ACCEPT = 'application/pdf,image/*'

/**
 * #6266 — adds an invoice from its file: drop it, pick it or paste a photo. The file makes
 * a draft (today's date, the shop's currency, zeros) and is uploaded to it, then `onCreated`
 * opens the draft to type the receipt in. The client checks the type to save a trip; the
 * server's own sniff is the truth. A failed upload keeps the draft and says so.
 */
export function InvoiceUpload({ onCreated }: { onCreated: (invoiceId: string) => void }) {
  const money = useMoney()
  const [suppliers, setSuppliers] = useState<Supplier[]>([])
  const [supplierId, setSupplierId] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [over, setOver] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [kept, setKept] = useState<string | null>(null)
  const picker = useRef<HTMLInputElement>(null)
  const working = useRef(false)

  useEffect(() => {
    let live = true
    listSuppliers().then(
      (rows) => {
        if (!live) return
        setSuppliers(rows)
        const last = lastSupplier()
        setSupplierId((current) => current || (rows.some((row) => row.id === last) ? last! : ''))
      },
      () => live && setError('The suppliers could not be loaded. Reload the page.'),
    )
    return () => {
      live = false
    }
  }, [])

  // The zone is aria-disabled only while a draft is being made or the currency is unread, never for a missing supplier:
  // the picker sits inside the zone, so a disabled zone would lock it. `add` asks for the supplier instead.

  async function add(file: File | undefined) {
    if (!file || working.current) return
    setError(null)
    setKept(null)
    if (!acceptableInvoiceFile(file)) {
      setError('Choose a PDF or a photo.')
      return
    }
    if (!supplierId) {
      setError('Choose the supplier first.')
      return
    }
    if (money.currency === null) {
      setError("ScadBuddy is still reading the shop's currency. Try again in a moment.")
      return
    }
    working.current = true
    setBusy(true)
    let draftId: string | null = null
    try {
      rememberSupplier(supplierId)
      const draft = await createInvoice({ supplier_id: supplierId, invoice_date: today(), currency: money.currency, subtotal: '0', shipping: '0', tax: '0', total: '0' })
      draftId = draft.id
      await uploadInvoiceFile(draft.id, file)
      onCreated(draft.id)
    } catch (cause) {
      const detail = cause instanceof ApiError ? cause.detail : null
      if (draftId) {
        setKept(draftId)
        setError(`The draft was saved; the file was not.${detail ? ` ${detail}` : ''}`)
      } else {
        setError(detail ?? 'The invoice could not be added. Try again.')
      }
    } finally {
      working.current = false
      setBusy(false)
    }
  }

  function paste(event: ClipboardEvent) {
    const file = event.clipboardData?.files?.[0]
    if (!file) return
    event.preventDefault()
    void add(file)
  }

  function drop(event: DragEvent) {
    event.preventDefault()
    setOver(false)
    void add(event.dataTransfer.files[0])
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        role="group"
        aria-label="Add an invoice file"
        aria-disabled={busy || money.currency === null}
        tabIndex={0}
        onPaste={paste}
        onDragOver={(event) => {
          event.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
        className={`flex flex-col items-start gap-3 rounded-md border border-dashed p-3 text-[13px] transition-colors focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none sm:flex-row sm:items-center ${over ? 'border-primary bg-primary/5' : ''}`}
      >
        <FileUpIcon aria-hidden className="size-5 shrink-0 text-muted-foreground" />
        <p className="text-muted-foreground sm:flex-1">Drop a PDF or a photo of the invoice here, paste a photo, or choose a file. It makes a draft to type the receipt into.</p>
        <Select
          value={supplierId}
          onValueChange={(next) => {
            setSupplierId(next)
            rememberSupplier(next)
          }}
        >
          <SelectTrigger aria-label="Supplier" className="w-full sm:w-48">
            <SelectValue placeholder="Supplier…" />
          </SelectTrigger>
          <SelectContent>
            {suppliers.map((supplier) => (
              <SelectItem key={supplier.id} value={supplier.id}>
                {supplier.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => picker.current?.click()}>
          {busy ? 'Adding…' : 'Choose a file'}
        </Button>
        <input
          ref={picker}
          type="file"
          accept={ACCEPT}
          aria-label="Choose a file"
          className="sr-only"
          tabIndex={-1}
          onChange={(event) => {
            void add(event.target.files?.[0])
            event.target.value = ''
          }}
        />
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
          {kept && (
            <>
              {' '}
              <Link to={filamentLinks.invoice(kept)} className="underline underline-offset-4">
                Open the draft
              </Link>
            </>
          )}
        </p>
      )}
    </div>
  )
}
