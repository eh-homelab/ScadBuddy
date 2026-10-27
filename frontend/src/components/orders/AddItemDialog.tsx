import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { ModelSummary, OrderItemCreate, Output } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { QUANTITY_HINT, parseQuantity } from './quantity'
import { RoughEstimate } from './RoughEstimate'
import { refusalText } from './refusal'

/** The radio value of "the current version with its default settings". */
const CURRENT = 'current'
/** How many templates the search lists at once. */
const SHOWN = 8

function matches(model: ModelSummary, query: string): boolean {
  const q = query.trim().toLocaleLowerCase()
  if (!q) return true
  return [model.name, model.slug, ...(model.tags ?? [])].some((text) => text.toLocaleLowerCase().includes(q))
}

/**
 * The body `POST /orders/{id}/items` takes for `choice` (spec 2026-10-18 §3.3). The
 * current version names the template's own revision, `version`, which is a full commit of
 * the models repository (`ModelRecord.version`), never `HEAD` (Ruling 1); its inputs are
 * no parameters at the template's `inputs_version`, so every parameter takes its default
 * at that commit. An output is named by its id alone: the backend takes its commit,
 * inputs, libraries and colours (`orders/reference.py` `resolve_reference`).
 */
function itemBody(current: Pinned | null, choice: string, quantity: number, title: string): OrderItemCreate {
  const named = title.trim() ? { title: title.trim() } : {}
  if (choice === CURRENT && current) {
    return { slug: current.slug, commit: current.version, inputs: { params: {}, v: current.inputsVersion }, quantity, ...named }
  }
  return { output_id: choice, quantity, ...named }
}

/** A template's current version as `itemBody` pins it: both its commit and its inputs version known. */
interface Pinned {
  slug: string
  version: string
  inputsVersion: number
}

function pinnedOf(model: ModelSummary | null): Pinned | null {
  return model && typeof model.version === 'string' && model.version && typeof model.inputs_version === 'number'
    ? { slug: model.slug, version: model.version, inputsVersion: model.inputs_version }
    : null
}

/**
 * "Add item" on the order page (spec 2026-10-18 §3.3, §9): pick a template from the
 * catalogue, then what to make of it, either its current version with default settings or
 * one of its saved outputs (exact settings and colours), how many, and an optional title.
 * The order page shows it only while the order is `draft` or `quoted`
 * (`Lifecycle._open_for_items`).
 */
export function AddItemDialog({ open, orderId, onAdded, onClose }: { open: boolean; orderId: string; onAdded: () => void; onClose: () => void }) {
  const models = useAsync(() => (open ? api.listModels() : Promise.resolve([] as ModelSummary[])), [open])
  const [query, setQuery] = useState('')
  const [slug, setSlug] = useState<string | null>(null)
  const model = (models.data ?? []).find((m) => m.slug === slug) ?? null
  const outputs = useAsync(() => (slug ? api.listOutputs(slug) : Promise.resolve([] as Output[])), [slug])
  // What the person picked; empty until they pick, for this template.
  const [picked, setPicked] = useState('')
  const [quantity, setQuantity] = useState('1')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const found = useMemo(() => (models.data ?? []).filter((m) => matches(m, query)), [models.data, query])
  const saved = useMemo(() => [...(outputs.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)), [outputs.data])
  const pinned = pinnedOf(model)
  // Unpicked, the current version when the template has one to pin, else its newest output.
  const choice = picked || (pinned ? CURRENT : (saved[0]?.id ?? ''))

  useEffect(() => {
    if (!open) {
      setQuery('')
      setSlug(null)
      setPicked('')
      setQuantity('1')
      setTitle('')
      setError(null)
      setBusy(false)
    }
  }, [open])
  /** Another template, or none, starting from its own default pick in the same render. */
  function pickTemplate(next: string | null) {
    setSlug(next)
    setPicked('')
  }

  async function add() {
    if (!model) {
      setError('Pick a template.')
      return
    }
    if (!choice || (choice === CURRENT && !pinned)) {
      setError('Pick what to order.')
      return
    }
    const count = parseQuantity(quantity)
    if (count === null) {
      setError(QUANTITY_HINT)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.addOrderItem(orderId, itemBody(pinned, choice, count, title))
      onAdded()
      onClose()
    } catch (err) {
      setError(refusalText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add item</DialogTitle>
          <DialogDescription>A template becomes a line item of this order, pinned to one revision and its settings.</DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-3">
          {model ? (
            <div className="flex items-center justify-between gap-2 rounded-md border px-3 py-2" data-testid="add-item-template">
              <span className="min-w-0 truncate text-sm font-medium">{model.name}</span>
              <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => pickTemplate(null)}>
                Change
              </Button>
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              <Label htmlFor="add-item-search">Template</Label>
              <Input id="add-item-search" type="search" placeholder="Search the catalogue" autoComplete="off" value={query} onChange={(e) => setQuery(e.target.value)} />
              {models.loading && !models.data ? (
                <p className="text-[13px] text-muted-foreground">Loading templates…</p>
              ) : found.length === 0 ? (
                <p className="text-[13px] text-muted-foreground">No template matches.</p>
              ) : (
                <ul aria-label="Search results" className="flex max-h-56 flex-col overflow-y-auto rounded-md border">
                  {found.slice(0, SHOWN).map((m) => (
                    <li key={m.slug}>
                      <button
                        type="button"
                        className="flex w-full flex-col items-start px-3 py-1.5 text-left text-sm hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                        onClick={() => {
                          pickTemplate(m.slug)
                          setError(null)
                        }}
                      >
                        <span className="font-medium">{m.name}</span>
                        <span className="text-xs text-muted-foreground">{m.slug}</span>
                      </button>
                    </li>
                  ))}
                  {found.length > SHOWN && <li className="px-3 py-1.5 text-xs text-muted-foreground">{found.length - SHOWN} more; narrow the search.</li>}
                </ul>
              )}
            </div>
          )}

          {model && (
            <div className="flex flex-col gap-1">
              <span className="text-sm font-medium" id="add-item-what">
                What to order
              </span>
              <RadioGroup aria-labelledby="add-item-what" value={choice} onValueChange={setPicked} className="max-h-60 gap-1 overflow-y-auto">
                <label className="flex items-start gap-2 rounded-md px-1 py-1 text-sm has-[:disabled]:opacity-60">
                  <RadioGroupItem value={CURRENT} disabled={!pinned} aria-label="Current version, default settings" className="mt-0.5" />
                  <span className="flex flex-col">
                    <span>Current version, default settings</span>
                    <span className="text-xs text-muted-foreground">
                      {pinned ? `revision ${pinned.version.slice(0, 7)}; colours are chosen when it is quoted` : 'This template has no history to pin a revision in; order one of its outputs.'}
                    </span>
                  </span>
                </label>
                {saved.map((output) => (
                  <label key={output.id} className="flex items-start gap-2 rounded-md px-1 py-1 text-sm">
                    <RadioGroupItem value={output.id} aria-label={output.name || 'Untitled output'} className="mt-0.5" />
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate">{output.name || 'Untitled output'}</span>
                      <span className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                        saved {timeAgo(output.created_at)}
                        {output.model_version ? ` · revision ${output.model_version.slice(0, 7)}` : ''}
                        {(output.colors ?? []).map((colour, slot) => (
                          <span key={slot} aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: colour }} />
                        ))}
                      </span>
                    </span>
                  </label>
                ))}
              </RadioGroup>
              {outputs.loading && !outputs.data && <p className="text-xs text-muted-foreground">Loading saved outputs…</p>}
              {outputs.data && saved.length === 0 && <p className="text-xs text-muted-foreground">No saved outputs of this template.</p>}
              {outputs.error && <p className="text-xs text-warn">Could not list its outputs: {outputs.error.message}</p>}
            </div>
          )}

          {model && (
            <div className="flex flex-wrap gap-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="add-item-quantity">Quantity</Label>
                <Input id="add-item-quantity" inputMode="numeric" className="w-24" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
              </div>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <Label htmlFor="add-item-title">Title (optional)</Label>
                <Input id="add-item-title" maxLength={200} placeholder={model.name} value={title} onChange={(e) => setTitle(e.target.value)} />
              </div>
            </div>
          )}

          {model && choice && choice !== CURRENT && <RoughEstimate outputId={choice} idPrefix="add-item-estimate" />}

          {(error ?? models.error?.message) && (
            <p role="alert" className="text-[13px] text-warn">
              {error ?? `Could not load the catalogue: ${models.error?.message}`}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" disabled={busy || !model || !choice} onClick={() => void add()}>
            Add item
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
