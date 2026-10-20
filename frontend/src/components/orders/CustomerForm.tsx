import { useState, type FormEvent } from 'react'
import type { Customer, CustomerContact, CustomerCreate } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

const KINDS: CustomerContact['kind'][] = ['phone', 'email', 'instagram', 'facebook', 'tiktok', 'etsy', 'website', 'other']

type ContactRow = { id?: string; kind: CustomerContact['kind']; value: string; label: string; primary: boolean }
type AddressRow = {
  id?: string
  label: string
  recipient: string
  lines: string
  locality: string
  region: string
  postal_code: string
  country: string
  phone: string
  default_shipping: boolean
  default_billing: boolean
}

/** What the form sends: a create's body, which a patch sends the same way (rows keep their ids, Ruling 11). */
export type CustomerFormValue = CustomerCreate

function fromCustomer(customer: Customer | undefined) {
  return {
    display_name: customer?.display_name ?? '',
    company: customer?.company ?? '',
    notes: customer?.notes ?? '',
    tags: customer?.tags.join(', ') ?? '',
    contacts: (customer?.contacts ?? []).map<ContactRow>((c) => ({ id: c.id, kind: c.kind, value: c.value, label: c.label ?? '', primary: c.primary })),
    addresses: (customer?.addresses ?? []).map<AddressRow>((a) => ({
      id: a.id,
      label: a.label ?? '',
      recipient: a.recipient,
      lines: (a.lines ?? []).join('\n'),
      locality: a.locality ?? '',
      region: a.region ?? '',
      postal_code: a.postal_code ?? '',
      country: a.country ?? '',
      phone: a.phone ?? '',
      default_shipping: a.default_shipping,
      default_billing: a.default_billing,
    })),
  }
}

/**
 * A customer's record (§3.1): name, company, notes, tags, and the contact points and
 * addresses as lists. An existing row keeps its id, so an open order's address pointer
 * survives the edit (Ruling 11); a removed row is deleted.
 */
export function CustomerForm({
  customer,
  submitLabel,
  busy,
  error,
  onSubmit,
}: {
  customer?: Customer
  submitLabel: string
  busy: boolean
  error: string | null
  onSubmit: (value: CustomerFormValue) => void
}) {
  const [form, setForm] = useState(() => fromCustomer(customer))
  const [problem, setProblem] = useState<string | null>(null)
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((was) => ({ ...was, [key]: value }))
  const contact = (index: number, patch: Partial<ContactRow>) =>
    set('contacts', form.contacts.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  const address = (index: number, patch: Partial<AddressRow>) =>
    set('addresses', form.addresses.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!form.display_name.trim()) {
      setProblem('A customer needs a name.')
      return
    }
    const seen = new Set<string>()
    for (const row of form.contacts) {
      if (row.primary && seen.has(row.kind)) {
        setProblem(`Only one ${row.kind} can be primary.`)
        return
      }
      if (row.primary) seen.add(row.kind)
    }
    setProblem(null)
    onSubmit({
      display_name: form.display_name.trim(),
      company: form.company.trim() || null,
      notes: form.notes,
      tags: form.tags.split(',').map((t) => t.trim()).filter(Boolean),
      contacts: form.contacts.map((row) => ({ ...(row.id ? { id: row.id } : {}), kind: row.kind, value: row.value.trim(), label: row.label.trim() || null, primary: row.primary })),
      addresses: form.addresses.map((row) => ({
        ...(row.id ? { id: row.id } : {}),
        label: row.label.trim() || null,
        recipient: row.recipient.trim(),
        lines: row.lines.split('\n').map((l) => l.trim()).filter(Boolean),
        locality: row.locality.trim() || null,
        region: row.region.trim() || null,
        postal_code: row.postal_code.trim() || null,
        country: row.country.trim().toUpperCase() || null,
        phone: row.phone.trim() || null,
        default_shipping: row.default_shipping,
        default_billing: row.default_billing,
      })),
    })
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={submit} aria-label="Customer">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor="customer-name">Name</Label>
          <Input id="customer-name" value={form.display_name} onChange={(e) => set('display_name', e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="customer-company">Company</Label>
          <Input id="customer-company" value={form.company} onChange={(e) => set('company', e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="customer-tags">Tags</Label>
          <Input id="customer-tags" placeholder="vip, wholesale" value={form.tags} onChange={(e) => set('tags', e.target.value)} />
        </div>
        <div className="flex flex-col gap-1 sm:col-span-2">
          <Label htmlFor="customer-notes">Notes</Label>
          <Textarea id="customer-notes" rows={2} value={form.notes} onChange={(e) => set('notes', e.target.value)} />
        </div>
      </div>

      <fieldset className="flex flex-col gap-2">
        <legend className="text-xs text-muted-foreground">Contact points</legend>
        {form.contacts.map((row, index) => (
          <div key={row.id ?? `new-${index}`} className="flex flex-wrap items-center gap-2">
            <Select value={row.kind} onValueChange={(kind) => contact(index, { kind: kind as CustomerContact['kind'] })}>
              <SelectTrigger aria-label={`Contact ${index + 1} kind`} className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {KINDS.map((k) => (
                  <SelectItem key={k} value={k}>
                    {k}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input aria-label={`Contact ${index + 1} value`} className="w-52" value={row.value} onChange={(e) => contact(index, { value: e.target.value })} />
            <Input aria-label={`Contact ${index + 1} label`} className="w-24" placeholder="label" value={row.label} onChange={(e) => contact(index, { label: e.target.value })} />
            <Label className="flex items-center gap-1 font-normal">
              <Checkbox checked={row.primary} onCheckedChange={(checked) => contact(index, { primary: checked === true })} aria-label={`Contact ${index + 1} primary`} />
              Primary
            </Label>
            <Button type="button" size="xs" variant="ghost" aria-label={`Remove contact ${index + 1}`} onClick={() => set('contacts', form.contacts.filter((_, i) => i !== index))}>
              Remove
            </Button>
          </div>
        ))}
        <div>
          <Button type="button" size="sm" variant="outline" onClick={() => set('contacts', [...form.contacts, { kind: 'email', value: '', label: '', primary: false }])}>
            Add contact
          </Button>
        </div>
      </fieldset>

      <fieldset className="flex flex-col gap-3">
        <legend className="text-xs text-muted-foreground">Addresses</legend>
        {form.addresses.map((row, index) => (
          <div key={row.id ?? `new-${index}`} className="grid gap-2 rounded-lg border p-3 sm:grid-cols-2">
            <Input aria-label={`Address ${index + 1} recipient`} placeholder="Recipient" value={row.recipient} onChange={(e) => address(index, { recipient: e.target.value })} />
            <Input aria-label={`Address ${index + 1} label`} placeholder="Label (home, work)" value={row.label} onChange={(e) => address(index, { label: e.target.value })} />
            <Textarea aria-label={`Address ${index + 1} lines`} placeholder="Street lines, one per line" rows={2} className="sm:col-span-2" value={row.lines} onChange={(e) => address(index, { lines: e.target.value })} />
            <Input aria-label={`Address ${index + 1} locality`} placeholder="City" value={row.locality} onChange={(e) => address(index, { locality: e.target.value })} />
            <Input aria-label={`Address ${index + 1} region`} placeholder="Region" value={row.region} onChange={(e) => address(index, { region: e.target.value })} />
            <Input aria-label={`Address ${index + 1} postal code`} placeholder="Postal code" value={row.postal_code} onChange={(e) => address(index, { postal_code: e.target.value })} />
            <Input aria-label={`Address ${index + 1} country`} placeholder="Country (ISO, e.g. GB)" maxLength={2} value={row.country} onChange={(e) => address(index, { country: e.target.value })} />
            <Input aria-label={`Address ${index + 1} phone`} placeholder="Phone" value={row.phone} onChange={(e) => address(index, { phone: e.target.value })} />
            <div className="flex flex-wrap items-center gap-3">
              <Label className="flex items-center gap-1 font-normal">
                <Checkbox checked={row.default_shipping} onCheckedChange={(c) => address(index, { default_shipping: c === true })} aria-label={`Address ${index + 1} default shipping`} />
                Ship here
              </Label>
              <Label className="flex items-center gap-1 font-normal">
                <Checkbox checked={row.default_billing} onCheckedChange={(c) => address(index, { default_billing: c === true })} aria-label={`Address ${index + 1} default billing`} />
                Bill here
              </Label>
              <Button type="button" size="xs" variant="ghost" aria-label={`Remove address ${index + 1}`} onClick={() => set('addresses', form.addresses.filter((_, i) => i !== index))}>
                Remove
              </Button>
            </div>
          </div>
        ))}
        <div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() =>
              set('addresses', [
                ...form.addresses,
                { label: '', recipient: form.display_name, lines: '', locality: '', region: '', postal_code: '', country: '', phone: '', default_shipping: form.addresses.length === 0, default_billing: false },
              ])
            }
          >
            Add address
          </Button>
        </div>
      </fieldset>

      {(problem ?? error) && (
        <p role="alert" className="text-[13px] text-warn">
          {problem ?? error}
        </p>
      )}
      <div>
        <Button type="submit" disabled={busy}>
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
