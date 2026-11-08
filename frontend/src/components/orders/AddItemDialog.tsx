import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { ArchiveOutcome, LibrarySearchHit, ModelSummary, Order, OrderItemCreate, Output, RevisionCreate } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { addItem, addItems, addRevision, afterAcceptanceWarning, reviseWarning, type AddMode } from './afterAcceptance'
import { QUANTITY_HINT, parseQuantity } from './quantity'
import { RoughEstimate } from './RoughEstimate'
import { refusalText } from './refusal'
import { LibraryFilePicker } from './LibraryFilePicker'
import { RecentRendersPicker, type RenderPick } from './RecentRendersPicker'
import { PastPrintsPicker } from './PastPrintsPicker'

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
  if (choice === CURRENT) {
    // `add()` refuses this first; never sent as an output id.
    if (!current) throw new Error('This template has no version to pin.')
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

/** A revision's body for `choice` (#5280): the current version names its commit and inputs, an output its id. */
function revisionBody(current: Pinned | null, choice: string, note: string): RevisionCreate {
  if (choice === CURRENT) {
    if (!current) throw new Error('This template has no version to pin.')
    return { note, reference: { slug: current.slug, commit: current.version, inputs: { params: {}, v: current.inputsVersion } } }
  }
  return { note, output_id: choice }
}

/**
 * The Add item dialog, opened by the items card's "Add product" (spec 2026-10-18 §3.3, §9; #4955): pick a template from the
 * catalogue, then what to make of it, either its current version with default settings or
 * one of its saved outputs (exact settings and colours), how many, and an optional title.
 * The order page shows it while the order takes items: `draft` or `quoted`, or, with
 * `mode` `after_acceptance` (#5349), `accepted`, `in_production` or `ready`, where the add
 * asks the person to confirm that it changes an accepted order first. It also takes a "Design request" (#5280): a title and the
 * customer's words, whose design a revision supplies later, and a "Library file" (#5544): a
 * file of the library, found as the Library page's search finds it, which the item prints, and "Recent renders"
 * (#4777): saved outputs of every model, several picked at once, each with a quantity, added in one request, and "Past prints"
 * (#4777): a Bambuddy archive, which the backend makes an item of what printed it. With `revise` it is the
 * design picker of an item's new revision instead (`slug`: the item's template, if any); with
 * `mode` `after_acceptance` that revision changes an accepted item (#5350), after a confirm.
 */
export function AddItemDialog({
  open,
  orderId,
  onAdded,
  onClose,
  revise,
  mode = 'open',
  order,
}: {
  open: boolean
  orderId: string
  onAdded: () => void
  onClose: () => void
  revise?: { itemId: string; slug: string | null }
  /** #5349 — `after_acceptance` adds (or, with `revise`, revises, #5350) on that route, after a confirm. */
  mode?: AddMode
  /** The order the add changes: its number and status, for the confirm's text. */
  order?: Pick<Order, 'number' | 'status'>
}) {
  const models = useAsync(() => (open ? api.listModels() : Promise.resolve([] as ModelSummary[])), [open])
  const [query, setQuery] = useState('')
  const [slug, setSlug] = useState<string | null>(revise?.slug ?? null)
  // What the item is made from: a template, a design request (#5280), a library file (#5544)
  // recent renders of any model or a past print (#4777).
  const [source, setSource] = useState<'template' | 'request' | 'library' | 'recent' | 'past'>('template')
  const [archive, setArchive] = useState<ArchiveOutcome | null>(null)
  const [renders, setRenders] = useState<Map<string, RenderPick>>(new Map())
  const request = source === 'request'
  const [file, setFile] = useState<LibrarySearchHit | null>(null)
  const [note, setNote] = useState('')
  const model = (models.data ?? []).find((m) => m.slug === slug) ?? null
  const outputs = useAsync(() => (slug ? api.listOutputs(slug) : Promise.resolve([] as Output[])), [slug])
  // What the person picked; empty until they pick, for this template.
  const [picked, setPicked] = useState('')
  const [quantity, setQuantity] = useState('1')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // #5349, #5350: a change to an accepted order waits for a second click on the confirm.
  const [confirming, setConfirming] = useState(false)
  const confirm = mode === 'after_acceptance'
  // Each source has its own title: one typed for another source is not sent unseen.
  const pickSource = (next: typeof source) => {
    if (next === source) return
    setSource(next)
    setTitle('')
  }

  const found = useMemo(() => (models.data ?? []).filter((m) => matches(m, query)), [models.data, query])
  const saved = useMemo(() => [...(outputs.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)), [outputs.data])
  const pinned = pinnedOf(model)
  // Unpicked, the current version when the template has one to pin, else its newest output.
  const choice = picked || (pinned ? CURRENT : (saved[0]?.id ?? ''))

  useEffect(() => {
    if (!open) {
      setQuery('')
      setSlug(revise?.slug ?? null)
      setSource('template')
      setFile(null)
      setArchive(null)
      setRenders(new Map())
      setNote('')
      setPicked('')
      setQuantity('1')
      setTitle('')
      setError(null)
      setBusy(false)
      setConfirming(false)
    }
  }, [open, revise?.slug])
  // A new pick asks again: the confirm names what was picked when it was armed.
  useEffect(() => setConfirming(false), [source, file, archive, renders, slug, choice, quantity, title, note])
  /** Another template, or none, starting from its own default pick in the same render. */
  function pickTemplate(next: string | null) {
    setSlug(next)
    setPicked('')
  }

  /** Asks first on an accepted order; true when the add may go now. */
  function confirmed(): boolean {
    if (!confirm || confirming) return true
    setConfirming(true)
    return false
  }

  async function add() {
    if (source === 'recent') {
      if (renders.size === 0) {
        setError('Pick at least one render.')
        return
      }
      const items = []
      for (const { output, quantity: typed } of renders.values()) {
        const count = parseQuantity(typed)
        if (count === null) {
          setError(`${output.name || 'Untitled output'}: ${QUANTITY_HINT}`)
          return
        }
        items.push({ output_id: output.id, quantity: count })
      }
      if (!confirmed()) return
      setBusy(true)
      setError(null)
      try {
        await addItems(orderId, items, mode)
        onAdded()
        onClose()
      } catch (err) {
        setError(refusalText(err))
        setConfirming(false)
        setBusy(false)
      }
      return
    }
    if (source === 'past') {
      if (!archive) {
        setError('Pick a past print.')
        return
      }
      const count = parseQuantity(quantity)
      if (count === null) {
        setError(QUANTITY_HINT)
        return
      }
      if (!confirmed()) return
      setBusy(true)
      setError(null)
      try {
        await addItem(orderId, { archive_id: archive.id, quantity: count, ...(title.trim() ? { title: title.trim() } : {}) }, mode)
        onAdded()
        onClose()
      } catch (err) {
        setError(refusalText(err))
        setConfirming(false)
        setBusy(false)
      }
      return
    }
    if (source === 'library') {
      if (!file) {
        setError('Pick a library file.')
        return
      }
      const count = parseQuantity(quantity)
      if (count === null) {
        setError(QUANTITY_HINT)
        return
      }
      if (!confirmed()) return
      setBusy(true)
      setError(null)
      try {
        await addItem(orderId, { library_file_id: file.id, quantity: count, ...(title.trim() ? { title: title.trim() } : {}) }, mode)
        onAdded()
        onClose()
      } catch (err) {
        setError(refusalText(err))
        setConfirming(false)
        setBusy(false)
      }
      return
    }
    if (request) {
      if (!title.trim()) {
        setError('Type a title for the request.')
        return
      }
      const count = parseQuantity(quantity)
      if (count === null) {
        setError(QUANTITY_HINT)
        return
      }
      if (!confirmed()) return
      setBusy(true)
      setError(null)
      try {
        await addItem(orderId, { kind: 'request', title: title.trim(), quantity: count, notes: note }, mode)
        onAdded()
        onClose()
      } catch (err) {
        setError(refusalText(err))
        setBusy(false)
      }
      return
    }
    if (!model) {
      setError('Pick a template.')
      return
    }
    if (!choice || (choice === CURRENT && !pinned)) {
      setError('Pick what to order.')
      return
    }
    const count = revise ? 1 : parseQuantity(quantity)
    if (count === null) {
      setError(QUANTITY_HINT)
      return
    }
    if (!confirmed()) return
    setBusy(true)
    setError(null)
    try {
      if (revise) await addRevision(orderId, revise.itemId, revisionBody(pinned, choice, note), mode)
      else await addItem(orderId, itemBody(pinned, choice, count, title), mode)
      onAdded()
      onClose()
    } catch (err) {
      setError(refusalText(err))
      // A failed save asks again before the next try.
      setConfirming(false)
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className={source === 'recent' || source === 'past' ? 'sm:max-w-2xl' : 'sm:max-w-lg'}>
        <DialogHeader>
          <DialogTitle>{revise ? 'New revision' : 'Add item'}</DialogTitle>
          <DialogDescription>
            {revise
              ? "Pick the design this revision shows: a template's current version or one of its saved outputs."
              : request
                ? "A request for a design: the customer's words now, its designs as revisions later."
                : source === 'library'
                  ? 'A file of the library becomes a line item of this order. The item prints that file.'
                  : source === 'recent'
                    ? 'Saved renders of any model become line items of this order, each with its exact settings and colours.'
                    : source === 'past'
                      ? 'A past print becomes a line item: of the render or library file that printed it, else of a copy of its file in the library.'
                      : 'A template becomes a line item of this order, pinned to one revision and its settings.'}
          </DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-3">
          {!revise && (
            <div className="flex gap-2" role="group" aria-label="Kind of item">
              <Button type="button" size="sm" variant={source === 'template' ? 'secondary' : 'outline'} aria-pressed={source === 'template'} onClick={() => pickSource('template')}>
                From a template
              </Button>
              <Button type="button" size="sm" variant={source === 'recent' ? 'secondary' : 'outline'} aria-pressed={source === 'recent'} onClick={() => pickSource('recent')}>
                Recent renders
              </Button>
              <Button type="button" size="sm" variant={source === 'past' ? 'secondary' : 'outline'} aria-pressed={source === 'past'} onClick={() => pickSource('past')}>
                Past prints
              </Button>
              <Button type="button" size="sm" variant={source === 'library' ? 'secondary' : 'outline'} aria-pressed={source === 'library'} onClick={() => pickSource('library')}>
                Library file
              </Button>
              <Button type="button" size="sm" variant={request ? 'secondary' : 'outline'} aria-pressed={request} onClick={() => pickSource('request')}>
                Design request
              </Button>
            </div>
          )}
          {request && (
            <>
              <div className="flex flex-col gap-1">
                <Label htmlFor="add-request-title">Title</Label>
                <Input id="add-request-title" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="add-request-text">What the customer asked for</Label>
                <Textarea id="add-request-text" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="add-request-quantity">Quantity</Label>
                <Input id="add-request-quantity" inputMode="numeric" className="w-24" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
              </div>
            </>
          )}
          {source === 'recent' && (
            <RecentRendersPicker open={open} models={models.data ?? []} picks={renders} disabled={busy} onPicks={(next) => {
              setRenders(next)
              setError(null)
            }} />
          )}
          {source === 'past' && (
            <>
              <PastPrintsPicker open={open} picked={archive} disabled={busy} onPick={(next) => {
                setArchive(next)
                setError(null)
              }} />
              {archive && (
                <div className="flex flex-wrap gap-3">
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="add-past-quantity">Quantity</Label>
                    <Input id="add-past-quantity" inputMode="numeric" className="w-24" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
                  </div>
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <Label htmlFor="add-past-title">Title (optional)</Label>
                    <Input id="add-past-title" maxLength={200} placeholder={archive.print_name ?? undefined} value={title} onChange={(e) => setTitle(e.target.value)} />
                  </div>
                </div>
              )}
            </>
          )}
          {source === 'library' && (
            <>
              <LibraryFilePicker open={open} picked={file} disabled={busy} onPick={(next) => {
                setFile(next)
                setError(null)
              }} />
              {file && (
                <div className="flex flex-wrap gap-3">
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="add-file-quantity">Quantity</Label>
                    <Input id="add-file-quantity" inputMode="numeric" className="w-24" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
                  </div>
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <Label htmlFor="add-file-title">Title (optional)</Label>
                    <Input id="add-file-title" maxLength={200} placeholder={file.filename} value={title} onChange={(e) => setTitle(e.target.value)} />
                  </div>
                </div>
              )}
            </>
          )}
          {source === 'template' && (model ? (
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
          ))}

          {source === 'template' && model && (
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

          {source === 'template' && model && revise && (
            <div className="flex flex-col gap-1">
              <Label htmlFor="add-revision-note">Note</Label>
              <Textarea id="add-revision-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
          )}

          {source === 'template' && model && !revise && (
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

          {source === 'template' && model && choice && choice !== CURRENT && !revise && <RoughEstimate outputId={choice} idPrefix="add-item-estimate" />}

          {confirm && confirming && (
            <p role="alert" aria-label="Confirm the change" className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px]">
              {(revise ? reviseWarning : afterAcceptanceWarning)(order ?? { number: 'This order', status: 'accepted' })}
            </p>
          )}
          {(error ?? models.error?.message) && (
            <p role="alert" className="text-[13px] text-warn">
              {error ?? `Could not load the catalogue: ${models.error?.message}`}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={confirming ? () => setConfirming(false) : onClose}>
            {confirming ? 'Back' : 'Cancel'}
          </Button>
          <Button
            type="button"
            disabled={busy || (request ? !title.trim() : source === 'library' ? !file : source === 'past' ? !archive : source === 'recent' ? renders.size === 0 : !model || !choice)}
            onClick={() => void add()}
          >
            {confirming
              ? 'Change the accepted order'
              : revise
                ? 'Add revision'
                : source === 'recent' && renders.size > 0
                  ? `Add ${renders.size} item${renders.size === 1 ? '' : 's'}`
                  : 'Add item'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
