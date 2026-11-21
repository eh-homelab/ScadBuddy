import { useId } from 'react'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import type { SupplierDraft } from './supplierDraft'

/** The supplier's four fields, in the add sheet and on the supplier page. */
export function SupplierFields({ draft, onChange }: { draft: SupplierDraft; onChange: (next: SupplierDraft) => void }) {
  const id = useId()
  const set = (key: keyof SupplierDraft, value: string) => onChange({ ...draft, [key]: value })
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-name`} className="text-[13px]">Name</Label>
        <Input id={`${id}-name`} value={draft.name} onChange={(event) => set('name', event.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-website`} className="text-[13px]">Website</Label>
        <Input id={`${id}-website`} inputMode="url" placeholder="https://" value={draft.website} onChange={(event) => set('website', event.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-customer`} className="text-[13px]">Customer number</Label>
        <Input id={`${id}-customer`} value={draft.customer_number} onChange={(event) => set('customer_number', event.target.value)} />
      </div>
      <div className="flex flex-col gap-1 sm:col-span-2">
        <Label htmlFor={`${id}-note`} className="text-[13px]">Note</Label>
        <Textarea id={`${id}-note`} rows={3} value={draft.note} onChange={(event) => set('note', event.target.value)} />
      </div>
    </div>
  )
}
