import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ToggleGroup, ToggleGroupItem } from '../../components/ui/toggle-group'
import { ActionKey, errorText, store, type Listing, type ParamValue, type StoreEstimate, type StoreRender } from '../api/client'
import { Estimate } from '../components/Estimate'
import { ListingView } from '../components/ListingView'
import { clearChoices, loadChoices, saveChoices } from '../customizeState'
import { useDraftChanged } from '../draftCount'
import { partFilaments, partsView, slotColours } from '../fileParts'
import { keepOffered, listingUnavailable } from '../params'
import type { PollTiming } from '../poll'
import { changedListing, hasNumber, passing, refusalText } from '../refusals'
import { Preview } from '../viewer'

/** The most of one design a visitor may order (`AddListingItem.quantity`, `le=100`). */
const MAX_QUANTITY = 100

/** The longest a price read waits on a `Retry-After`: it is read at least this often. */
const RETRY_CAP_MS = 30_000

/** Refusals a read again cannot pass: the visitor reads why, and the page stops reading. */
const LIMITS = new Set(['render_limited', 'rate_limited'])

/** A held plate preview as the viewer reads a done render: its mesh, and nothing else. */
function plateJob(url: string): StoreRender {
  return { id: url, status: 'done', stage: null, plates: [], bbox_mm: null, colors: null, preview_url: url, error: null }
}

/**
 * #6710 §6.3 — the Customize page of a listing of a library file. Its geometry is fixed, so
 * nothing renders: the preview is the plate's GLB that publish held, each part drawn in the
 * colour picked for its slot (`Preview` `slotColours`), and a colour change shows at once.
 * Each part (an offered slot, "Part 1", "Part 2", …) has a colour picker (`ListingView` over
 * `fileParts.ts` `partsView`); the price is the shop's slice of the file in those colours
 * (`store.price`, the instant read: firm when the shop has it, else worked out while the
 * last known price stays on screen, marked as updating), and "Add to order" sends the
 * colours with no values. The page never shows a material, the file's own colours or its
 * name.
 */
export function FileCustomize({
  initial,
  timing,
  wait,
  now,
  debounceMs,
}: {
  initial: Listing
  timing: PollTiming
  wait: (ms: number) => Promise<void>
  now: () => number
  /** How long the page waits after the last change before it reads the price. */
  debounceMs: number
}) {
  const [listing, setListing] = useState(initial)
  const view = useMemo(() => partsView(listing), [listing])
  const [values, setValues] = useState<Record<string, ParamValue>>(() => {
    const saved = loadChoices(initial.id)
    return saved ? keepOffered(partsView(initial), saved.values) : {}
  })
  const [quantity, setQuantity] = useState(() => Math.min(loadChoices(initial.id)?.quantity ?? 1, MAX_QUANTITY))
  const [plate, setPlate] = useState(0)
  /** The price on screen and the colours (`current`) it is the price of. */
  const [priced, setPriced] = useState<{ key: string; estimate: StoreEstimate }>()
  /** The colours whose price the shop is working out (it answered `pending`). */
  const [pendingFor, setPendingFor] = useState<string>()
  const [priceError, setPriceError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [added, setAdded] = useState(false)
  const addKey = useRef(new ActionKey())
  /** Whether the page read a price yet: the first read does not wait out the debounce. */
  const started = useRef(false)
  const alive = useRef(true)
  const draftChanged = useDraftChanged()

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const chosen = useMemo(() => partFilaments(view, values), [view, values])
  const colours = useMemo(() => (chosen ? slotColours(listing, chosen) : undefined), [listing, chosen])
  const current = JSON.stringify([listing.updated_at, chosen])
  const blocked = listingUnavailable(view) || chosen === null || stale
  const previews = listing.previews ?? []
  const shownPlate = Math.min(plate, Math.max(0, previews.length - 1))

  useEffect(() => {
    saveChoices(listing.id, { values, quantity })
  }, [listing.id, values, quantity])

  /** The listing as the page shows it, for a price read's refusal text: the read follows
   * the price key (`current`), never a new `view` or `chosen` of the same key. */
  const shown = useRef(view)
  useEffect(() => {
    shown.current = view
  }, [view])

  useEffect(() => {
    if (blocked) return
    let live = true
    const key = current
    // The key is the body: a change that keeps it (a pick of the colour on screen, a reload
    // that keeps the colours) reads nothing again and leaves a read that runs alone.
    const [listingUpdatedAt, colours] = JSON.parse(key) as [string, Record<string, string>]
    const body = { listing_updated_at: listingUpdatedAt, colours }
    const read = async () => {
      setPriceError(null)
      const began = now()
      let delay = timing.firstMs
      const pause = async (seconds: number | null | undefined) => {
        const left = began + timing.limitMs - now()
        if (left <= 0) return false
        await wait(Math.min(Math.max(delay, Math.min((seconds ?? 0) * 1000, RETRY_CAP_MS)), left))
        delay = Math.min(delay * 2, timing.maxMs)
        return live
      }
      for (;;) {
        try {
          const answer = await store.price(listing.id, body)
          if (!live) return
          if (answer.source !== 'pending') {
            setPendingFor(undefined)
            setPriced({ key, estimate: answer })
            return
          }
          setPendingFor(key)
          if (await pause(answer.retry_after)) continue
          if (!live) return
          // Past the time limit the shop prices it by hand.
          setPendingFor(undefined)
          setPriced({ key, estimate: { ...answer, source: 'none', retry_after: null } })
          return
        } catch (caught) {
          if (!live) return
          const limited = passing(caught) && caught.slug !== null && LIMITS.has(caught.slug)
          if (passing(caught) && !limited && (await pause(caught.retryAfter))) continue
          if (!live) return
          setPendingFor(undefined)
          // The colours on screen have no price now: no other price stays as if it were updating.
          setPriced((p) => (p && p.key !== key ? undefined : p))
          if (changedListing(caught)) setStale(true)
          setPriceError(refusalText(caught, shown.current))
          return
        }
      }
    }
    const timer = setTimeout(
      () => {
        started.current = true
        void read()
      },
      started.current ? debounceMs : 0,
    )
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [blocked, current, listing.id, timing, wait, now, debounceMs])

  const setValue = (name: string, next: ParamValue | undefined) => {
    setValues((before) => {
      const copy = { ...before }
      if (next === undefined) delete copy[name]
      else copy[name] = next
      return copy
    })
    setAdded(false)
  }

  const add = async () => {
    if (chosen === null) return
    const body = { listing_id: listing.id, listing_updated_at: listing.updated_at, values: {}, colours: chosen, quantity }
    setAdding(true)
    setAddError(null)
    try {
      const draft = await store.addItem(body, addKey.current.for(JSON.stringify(body)))
      addKey.current.done()
      draftChanged(draft)
      clearChoices(listing.id)
      setAdded(true)
    } catch (caught) {
      if (changedListing(caught)) setStale(true)
      setAddError(refusalText(caught, view))
    } finally {
      setAdding(false)
    }
  }

  const reload = async () => {
    setReloading(true)
    try {
      const fresh = await store.listing(listing.id)
      if (!alive.current) return
      setListing(fresh)
      setValues(keepOffered(partsView(fresh), values))
      setStale(false)
      setPriceError(null)
      setAddError(null)
    } catch (caught) {
      if (alive.current) setPriceError(errorText(caught))
    } finally {
      if (alive.current) setReloading(false)
    }
  }

  const plates =
    previews.length > 1 ? (
      <ToggleGroup
        type="single"
        aria-label="Plate"
        value={String(shownPlate)}
        // Tapping the plate on screen empties the group: it stays on that plate.
        onValueChange={(next) => next !== '' && setPlate(Number(next))}
        className="rounded-lg border bg-background/85 p-0.5 shadow-xs backdrop-blur-sm"
      >
        {previews.map((url, index) => (
          <ToggleGroupItem key={url} value={String(index)} className="h-9 px-3 text-sm">
            {`Plate ${index + 1}`}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    ) : undefined

  return (
    <div className="sf-customize">
      <div className="sf-stage">
        {previews.length > 0 && colours ? (
          <Suspense fallback={<div className="sf-stage-empty">Loading the viewer…</div>}>
            <Preview job={plateJob(previews[shownPlate]!)} unit="mm" rendering={false} slotColours={colours} leading={plates} />
          </Suspense>
        ) : (
          <div className="sf-stage-empty">
            {previews.length === 0 ? 'This design has no 3D preview. Its pictures are beside the colours.' : 'This design cannot be ordered right now.'}
          </div>
        )}
      </div>
      <div className="sf-controls">
        <Link className="sf-back" to="/">
          All designs
        </Link>
        <ListingView listing={view} values={values} onChange={setValue} />
        {stale && (
          <div className="sf-panel sf-stale" role="status">
            <p>The shop has changed this design since you opened it.</p>
            <button type="button" className="sf-button" disabled={reloading} onClick={() => void reload()}>
              {reloading ? 'Loading…' : 'Load the latest version'}
            </button>
          </div>
        )}
        {priceError && (
          <p className="sf-error" role="alert">
            {priceError}
          </p>
        )}
        {priced && hasNumber(priced.estimate) ? (
          // The owner's rule: always show a price. The last one stays, marked as updating,
          // until the colours on screen are priced.
          <Estimate estimate={priced.estimate} updating={priced.key !== current || priced.estimate.source !== 'slice' || pendingFor === current} />
        ) : priced && priced.key === current ? (
          <Estimate estimate={priced.estimate} />
        ) : (
          pendingFor === current && <Estimate estimate={{ source: 'pending', price_minor: null, currency: null, minor_exponent: null, retry_after: null }} />
        )}
        <div className="sf-order">
          <p className="sf-label" id="sf-quantity">
            How many
          </p>
          <div className="sf-qty" role="group" aria-labelledby="sf-quantity">
            <button type="button" aria-label="One fewer" disabled={quantity <= 1} onClick={() => setQuantity((n) => Math.max(1, n - 1))}>
              −
            </button>
            <output aria-live="polite">{quantity}</output>
            <button type="button" aria-label="One more" disabled={quantity >= MAX_QUANTITY} onClick={() => setQuantity((n) => Math.min(MAX_QUANTITY, n + 1))}>
              +
            </button>
          </div>
          <button type="button" className="sf-button" disabled={blocked || adding} onClick={() => void add()}>
            {adding ? 'Adding…' : 'Add to order'}
          </button>
        </div>
        {addError && (
          <p className="sf-error" role="alert">
            {addError}
          </p>
        )}
        {added && (
          <p className="sf-done" role="status">
            <span>Added to your order.</span> <Link to="/draft">See your order</Link>
          </p>
        )}
      </div>
    </div>
  )
}
