import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { NEW_CUSTOMER, type CustomerPickerState } from './useCustomerPicker'

/**
 * Who an order is for (spec 2026-10-18 §3.1, §3.2): one of the customers that are not
 * archived, or "New customer…", which asks for a name and, if known, an email and a
 * phone; the dialog makes that customer when it submits (`useCustomerPicker`'s `resolve`).
 */
export function CustomerPicker({
  picker,
  id,
  label,
  loading = false,
  disabled = false,
}: {
  picker: CustomerPickerState
  /** The select's id; the new customer's fields take it as their prefix. */
  id: string
  /** The select's accessible name. */
  label: string
  loading?: boolean
  disabled?: boolean
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor={id}>Customer</Label>
        <Select value={picker.value} onValueChange={picker.setValue} disabled={disabled}>
          <SelectTrigger id={id} aria-label={label}>
            <SelectValue placeholder={loading ? 'Loading…' : 'Pick a customer'} />
          </SelectTrigger>
          <SelectContent position="popper">
            {picker.options.map((c) => (
              <SelectItem key={c.id} value={c.id}>
                {c.display_name}
              </SelectItem>
            ))}
            <SelectItem value={NEW_CUSTOMER}>New customer…</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {picker.value === NEW_CUSTOMER && (
        <fieldset className="flex flex-col gap-2 rounded-lg border p-3" aria-label="New customer">
          <div className="flex flex-col gap-1">
            <Label htmlFor={`${id}-name`}>Customer name</Label>
            <Input id={`${id}-name`} required maxLength={200} disabled={disabled} value={picker.draft.name} onChange={(e) => picker.setDraft({ name: e.target.value })} />
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${id}-email`}>Email</Label>
              <Input id={`${id}-email`} type="email" placeholder="Optional" maxLength={500} disabled={disabled} value={picker.draft.email} onChange={(e) => picker.setDraft({ email: e.target.value })} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${id}-phone`}>Phone</Label>
              <Input id={`${id}-phone`} type="tel" placeholder="Optional" maxLength={500} disabled={disabled} value={picker.draft.phone} onChange={(e) => picker.setDraft({ phone: e.target.value })} />
            </div>
          </div>
        </fieldset>
      )}
    </div>
  )
}
