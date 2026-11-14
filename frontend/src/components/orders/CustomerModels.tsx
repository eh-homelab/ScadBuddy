import { useState } from 'react'
import { DownloadIcon, ExternalLinkIcon } from 'lucide-react'
import { api } from '../../api/client'
import type { OrderDetail, OrderItemSource } from '../../api/types'
import { formatBytes } from '../../lib/format'
import { formatMoney, toMajor, toMinor } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { refusalText } from './refusal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'

/**
 * A customer's link, only when it is an `http` or `https` URL, checked again here (spec
 * 2026-11-13 §9.3): the page then offers "Open link". Any other text gets no anchor.
 */
export function safeLink(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : null
  } catch {
    return null
  }
}

const CHECK_WORDS: Record<NonNullable<OrderItemSource['check_status']>, string> = {
  checking: 'Checking',
  ready: 'Ready',
  refused: 'Refused',
  failed: 'Check failed',
}
const LINK_TYPES: Record<NonNullable<OrderItemSource['link_type']>, string> = {
  makerworld: 'MakerWorld',
  printables: 'Printables',
  thingiverse: 'Thingiverse',
  manual: 'Other link',
}
const STATUS_WORDS: Record<OrderItemSource['status'], string> = {
  open: 'Waiting for import',
  imported: 'Imported',
  manual: 'To process by hand',
  declined: 'Declined',
  pending: 'Purchase pending',
  recorded: 'Recorded',
  not_needed: 'Not needed',
}
const COST_KINDS = [
  { value: 'material', label: 'Parts' },
  { value: 'other', label: 'Paid model file' },
] as const

type Facts = { format?: string; plates?: { size_mm?: number[] }[]; triangles?: number }

/** The check's facts in one line: format, plates, triangles and plate 1's size in mm. */
export function factsLine(facts: OrderItemSource['facts']): string | null {
  if (!facts) return null
  const found = facts as Facts
  const parts: string[] = []
  if (found.format) parts.push(found.format.toUpperCase())
  if (found.plates) parts.push(`${found.plates.length} plate${found.plates.length === 1 ? '' : 's'}`)
  if (found.triangles != null) parts.push(`${found.triangles.toLocaleString('en-US')} triangles`)
  const size = found.plates?.[0]?.size_mm
  if (size && size.length === 3) parts.push(`${size.join(' × ')} mm`)
  return parts.join(', ') || null
}

/** "Open link" for a customer's link that is still http or https here, else nothing. */
function OpenLink({ url }: { url: string | null | undefined }) {
  const href = safeLink(url)
  if (!href) return null
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" className="inline-flex items-center gap-1 text-[13px] text-primary underline-offset-4 hover:underline">
      <ExternalLinkIcon className="size-3" aria-hidden />
      Open link
    </a>
  )
}

/** The link as escaped text: never an anchor, a preview or an unfurl. */
function LinkText({ url }: { url: string | null | undefined }) {
  return <span className="break-all font-mono text-xs">{url ?? ''}</span>
}

/**
 * #6133 (spec 2026-11-13 §11) — a storefront customer's own models on a request item: the
 * colours asked for, each file (its check, facts and preview; "Download" while staged;
 * "Check again" for a failed check), each link (its type, the link as text, "Open link",
 * "Decline"), a stuck import ("Retry import"), and each thing to buy ("Record cost",
 * "Change", "Not needed"). A customer's link is only ever text, or an anchor that opens in
 * a new tab with no referrer after its scheme is checked again.
 */
export function CustomerModels({ detail, itemId, onChanged }: { detail: OrderDetail; itemId: string; onChanged: () => void }) {
  const sources = (detail.sources ?? []).filter((s) => s.item_id === itemId)
  const colours = (detail.model_requests ?? []).find((m) => m.item_id === itemId)
  const book = useAsync(() => (colours && !colours.shop_choice ? api.priceBook(true) : Promise.resolve(null)), [colours?.shop_choice])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  if (sources.length === 0 && !colours) return null
  const { order } = detail
  const files = sources.filter((s) => s.kind === 'file')
  const links = sources.filter((s) => s.kind === 'link' && s.purpose === 'print')
  const purchases = sources.filter((s) => s.purpose === 'purchase')
  const names = new Map((book.data?.filaments ?? []).map((f) => [f.id, `${f.colour_name || f.colour} ${f.material}`]))

  async function run(id: string, action: () => Promise<unknown>): Promise<boolean> {
    setError(null)
    setBusy(id)
    try {
      await action()
      onChanged()
      return true
    } catch (caught) {
      setError(refusalText(caught))
      return false
    } finally {
      setBusy(null)
    }
  }

  async function download(source: OrderItemSource) {
    setError(null)
    setBusy(source.id)
    try {
      const { url } = await api.sourceDownload(order.id, source.id)
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch (caught) {
      setError(refusalText(caught))
    } finally {
      setBusy(null)
    }
  }

  const money = (minor: number) => formatMoney(minor, order.currency, order.minor_exponent)

  return (
    <section className="flex flex-col gap-3" aria-label="Customer's models" data-testid="customer-models">
      <h3 className="text-sm font-medium">Customer&apos;s models</h3>
      {colours && (
        <p className="text-[13px]">
          <span className="text-muted-foreground">Colours: </span>
          {colours.shop_choice ? 'Let the shop choose' : (colours.filaments ?? []).map((id) => names.get(id) ?? id).join(', ')}
        </p>
      )}
      {files.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Files">
          {files.map((file) => (
            <li key={file.id} data-testid={`source-${file.id}`} className="flex flex-wrap items-start gap-3 rounded-xl border p-3">
              {file.has_preview && (
                <img src={api.sourcePreviewUrl(order.id, file.id)} alt={`Preview of ${file.filename ?? 'the file'}`} className="size-20 rounded-md border bg-muted object-contain" loading="lazy" />
              )}
              <div className="flex min-w-0 flex-1 flex-col gap-1 text-[13px]">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="font-medium break-all">{file.filename}</span>
                  {file.size != null && <span className="text-muted-foreground">{formatBytes(file.size)}</span>}
                  {file.check_status && <span className="rounded-full border px-2 text-xs">{CHECK_WORDS[file.check_status]}</span>}
                  <SourceStatus source={file} />
                </div>
                {file.check_reason && <span className="text-muted-foreground">{file.check_reason}</span>}
                {factsLine(file.facts) && <span className="text-muted-foreground">{factsLine(file.facts)}</span>}
                <div className="flex flex-wrap gap-2">
                  {file.staged && (
                    <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void download(file)}>
                      <DownloadIcon aria-hidden />
                      Download
                    </Button>
                  )}
                  {file.check_status === 'failed' && file.staged && (
                    <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void run(file.id, () => api.checkSourceAgain(order.id, file.id))}>
                      {busy === file.id && <Spinner />}
                      Check again
                    </Button>
                  )}
                  {file.import_stuck && file.staged && (
                    <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void run(file.id, () => api.retrySourceImport(order.id, file.id))}>
                      Retry import
                    </Button>
                  )}
                  {(file.status === 'open' || file.status === 'manual') && <DeclineSource label="Decline file" disabled={busy !== null} onDecline={(reason) => run(file.id, () => api.declineSource(order.id, file.id, reason))} />}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {links.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Links">
          {links.map((link) => (
            <li key={link.id} data-testid={`source-${link.id}`} className="flex flex-col gap-1 rounded-xl border p-3 text-[13px]">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-medium">{link.link_type ? LINK_TYPES[link.link_type] : 'Link'}</span>
                <SourceStatus source={link} />
              </div>
              <LinkText url={link.url} />
              {link.note && <span className="text-muted-foreground">{link.note}</span>}
              <div className="flex flex-wrap items-center gap-2">
                <OpenLink url={link.url} />
                {(link.status === 'open' || link.status === 'manual') && <DeclineSource label="Decline link" disabled={busy !== null} onDecline={(reason) => run(link.id, () => api.declineSource(order.id, link.id, reason))} />}
              </div>
            </li>
          ))}
        </ul>
      )}
      {purchases.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Things to buy">
          {purchases.map((purchase) => (
            <li key={purchase.id} data-testid={`source-${purchase.id}`} className="flex flex-col gap-1 rounded-xl border p-3 text-[13px]">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-medium">Thing to buy</span>
                <span className="rounded-full border px-2 text-xs">{STATUS_WORDS[purchase.status]}</span>
                {purchase.status === 'recorded' && purchase.amount_minor != null && <span>{money(purchase.amount_minor)}</span>}
              </div>
              {purchase.note && <span>{purchase.note}</span>}
              <LinkText url={purchase.url} />
              {purchase.shop_note && <span className="text-muted-foreground">Note to the customer: {purchase.shop_note}</span>}
              <div className="flex flex-wrap items-center gap-2">
                <OpenLink url={purchase.url} />
                <PurchaseActions
                  purchase={purchase}
                  exponent={order.minor_exponent}
                  disabled={busy !== null}
                  onRecord={(body) => run(purchase.id, () => api.recordPurchase(order.id, purchase.id, body))}
                  onChange={(body) => run(purchase.id, () => api.updatePurchase(order.id, purchase.id, body))}
                  onDecline={(note) => run(purchase.id, () => api.declinePurchase(order.id, purchase.id, note))}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}

/** A print source's state; "Import stuck" with its reason in place of the plain words. */
function SourceStatus({ source }: { source: OrderItemSource }) {
  if (source.import_stuck) {
    return (
      <span className="rounded-full border border-warn/40 px-2 text-xs text-warn" title={source.status_reason ?? undefined}>
        Import stuck
      </span>
    )
  }
  if (source.status === 'open' && source.kind === 'file') return null
  return (
    <span className="text-muted-foreground">
      <span className="rounded-full border px-2 text-xs">{STATUS_WORDS[source.status]}</span>
      {source.status_reason && <span className="ml-1">{source.status_reason}</span>}
    </span>
  )
}

function DeclineSource({ label, disabled, onDecline }: { label: string; disabled: boolean; onDecline: (reason: string) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  if (!open) {
    return (
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setOpen(true)}>
        Decline
      </Button>
    )
  }
  return (
    <div className="flex w-full flex-wrap items-end gap-2">
      <div className="flex min-w-48 flex-1 flex-col gap-1">
        <Label htmlFor={`decline-${label}`} className="text-xs font-normal text-muted-foreground">
          Why the shop declines it
        </Label>
        <Input id={`decline-${label}`} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
      </div>
      <Button size="sm" disabled={disabled} onClick={() => void onDecline(reason).then((done) => done && setOpen(false))}>
        {label}
      </Button>
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setOpen(false)}>
        Back
      </Button>
    </div>
  )
}

type PurchaseBody = { amount_minor: number; cost_kind: 'material' | 'other'; note: string }

function PurchaseActions({
  purchase,
  exponent,
  disabled,
  onRecord,
  onChange,
  onDecline,
}: {
  purchase: OrderItemSource
  exponent: number
  disabled: boolean
  onRecord: (body: PurchaseBody) => Promise<boolean>
  onChange: (body: Partial<PurchaseBody>) => Promise<boolean>
  onDecline: (note: string) => Promise<boolean>
}) {
  const [mode, setMode] = useState<'record' | 'change' | 'decline' | null>(null)
  const [amount, setAmount] = useState('')
  const [kind, setKind] = useState<'material' | 'other'>('material')
  const [note, setNote] = useState('')
  const [hint, setHint] = useState<string | null>(null)
  const id = purchase.id

  function start(next: 'record' | 'change' | 'decline') {
    setHint(null)
    setNote('')
    setAmount(next === 'change' && purchase.amount_minor != null ? toMajor(purchase.amount_minor, exponent) : '')
    setKind(purchase.cost_kind ?? 'material')
    setMode(next)
  }

  function submit() {
    if (mode === 'decline') {
      void onDecline(note).then((done) => done && setMode(null))
      return
    }
    const minor = toMinor(amount, exponent)
    if (minor === null || minor < 1) {
      setHint('Type the amount the shop paid, such as 4.50.')
      return
    }
    const sent = mode === 'record' ? onRecord({ amount_minor: minor, cost_kind: kind, note }) : onChange({ amount_minor: minor, cost_kind: kind })
    void sent.then((done) => done && setMode(null))
  }

  if (mode === null) {
    return (
      <>
        {purchase.status === 'pending' && (
          <Button size="sm" variant="outline" disabled={disabled} onClick={() => start('record')}>
            Record cost
          </Button>
        )}
        {purchase.status === 'recorded' && (
          <Button size="sm" variant="outline" disabled={disabled} onClick={() => start('change')}>
            Change
          </Button>
        )}
        {purchase.status !== 'not_needed' && (
          <Button size="sm" variant="outline" disabled={disabled} onClick={() => start('decline')}>
            Not needed
          </Button>
        )}
      </>
    )
  }
  return (
    <div className="flex w-full flex-wrap items-end gap-2">
      {mode === 'decline' ? (
        <div className="flex min-w-48 flex-1 flex-col gap-1">
          <Label htmlFor={`purchase-note-${id}`} className="text-xs font-normal text-muted-foreground">
            A note to the customer
          </Label>
          <Input id={`purchase-note-${id}`} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-1">
            <Label htmlFor={`purchase-amount-${id}`} className="text-xs font-normal text-muted-foreground">
              What the shop paid
            </Label>
            <Input id={`purchase-amount-${id}`} className="w-28" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor={`purchase-kind-${id}`} className="text-xs font-normal text-muted-foreground">
              Cost kind
            </Label>
            <Select value={kind} onValueChange={(value) => setKind(value as 'material' | 'other')}>
              <SelectTrigger id={`purchase-kind-${id}`} size="sm" aria-label="Cost kind" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {COST_KINDS.map((k) => (
                  <SelectItem key={k.value} value={k.value}>
                    {k.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {mode === 'record' && (
            <div className="flex min-w-40 flex-1 flex-col gap-1">
              <Label htmlFor={`purchase-cost-note-${id}`} className="text-xs font-normal text-muted-foreground">
                The shop&apos;s note (the customer never reads it)
              </Label>
              <Input id={`purchase-cost-note-${id}`} value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
            </div>
          )}
        </>
      )}
      <Button size="sm" disabled={disabled} onClick={submit}>
        {mode === 'record' ? 'Record' : mode === 'change' ? 'Save' : 'Mark not needed'}
      </Button>
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setMode(null)}>
        Back
      </Button>
      {hint && (
        <p role="alert" className="w-full text-[13px] text-warn">
          {hint}
        </p>
      )}
    </div>
  )
}
