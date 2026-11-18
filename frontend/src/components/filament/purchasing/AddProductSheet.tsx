import { useState, type FormEvent } from 'react'
import { createProduct } from '../../../api/filamentPurchasing'
import { RecordDrawer } from '../../records/RecordDrawer'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { message } from './priceFormat'
import { webUrlError } from './webUrl'

interface Option {
  id: string
  label: string
}

/**
 * #6266 — the drawer that adds a supplier product. A product is one supplier's pack of one
 * spool type: the page knows one of them (`fixed`) and the person picks the other (`pick`).
 */
export function AddProductSheet({
  title,
  fixed,
  pick,
  onClose,
  onAdded,
}: {
  title: string
  fixed: { supplierId: string } | { typeId: string }
  pick: { label: 'Type' | 'Supplier'; placeholder: string; refusal: string; options: Option[] }
  onClose: () => void
  onAdded: () => void
}) {
  const [chosen, setChosen] = useState('')
  const [sku, setSku] = useState('')
  const [pack, setPack] = useState('1')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const form = `add-product-${pick.label.toLowerCase()}`

  async function submit(event: FormEvent) {
    event.preventDefault()
    const size = /^\d+$/.test(pack.trim()) ? Number(pack) : 0
    const refused = !chosen ? pick.refusal : !sku.trim() ? 'Enter the supplier’s SKU.' : size < 1 ? 'The pack size is a whole number of spools, at least 1.' : webUrlError(url, 'The product page')
    if (refused) {
      setError(refused)
      return
    }
    setBusy(true)
    setError(null)
    const [supplierId, typeId] = 'supplierId' in fixed ? [fixed.supplierId, chosen] : [chosen, fixed.typeId]
    try {
      await createProduct(supplierId, { type_id: typeId, sku: sku.trim(), pack_size: size, ...(url.trim() ? { product_url: url.trim() } : {}) })
      onAdded()
    } catch (cause) {
      setError(message(cause, 'The product could not be added. Try again.'))
      setBusy(false)
    }
  }

  return (
    <RecordDrawer
      title={title}
      onClose={onClose}
      footer={
        <Button type="submit" form={form} disabled={busy}>
          Add product
        </Button>
      }
    >
      <form id={form} className="flex flex-col gap-3" onSubmit={(event) => void submit(event)} noValidate>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${form}-pick`} className="text-[13px]">{pick.label}</Label>
          <Select value={chosen} onValueChange={setChosen}>
            <SelectTrigger id={`${form}-pick`} aria-label={pick.label} className="w-full text-[13px]">
              <SelectValue placeholder={pick.placeholder} />
            </SelectTrigger>
            <SelectContent>
              {pick.options.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor={`${form}-sku`} className="text-[13px]">SKU</Label>
            <Input id={`${form}-sku`} value={sku} onChange={(event) => setSku(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor={`${form}-pack`} className="text-[13px]">Pack size</Label>
            <Input id={`${form}-pack`} inputMode="numeric" value={pack} onChange={(event) => setPack(event.target.value)} />
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${form}-url`} className="text-[13px]">Product page</Label>
          <Input id={`${form}-url`} inputMode="url" placeholder="https://" value={url} onChange={(event) => setUrl(event.target.value)} />
        </div>
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
      </form>
    </RecordDrawer>
  )
}
