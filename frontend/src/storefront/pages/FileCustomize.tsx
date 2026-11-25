import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ToggleGroup, ToggleGroupItem } from '../../components/ui/toggle-group'
import { ActionKey, StoreError, errorText, store, type Listing, type ParamValue, type StoreEstimate, type StoreRender } from '../api/client'
import { Estimate } from '../components/Estimate'
import { ListingView } from '../components/ListingView'
import { clearChoices, loadChoices, saveChoices } from '../customizeState'
import { useDraftChanged } from '../draftCount'
import { partFilaments, partsView, slotColours } from '../fileParts'
import { keepOffered, listingUnavailable } from '../params'
import type { PollTiming } from '../poll'
import { changedListing, hasNumber, inTime, passing, priceLimitText, refusalText } from '../refusals'
import { Preview } from '../viewer'

/** The most of one design a visitor may order (`AddListingItem.quantity`, `le=100`). */
const MAX_QUANTITY = 100

/** The longest a price read waits on a `Retry-After`: it is read at least this often. */
const RETRY_CAP_MS = 30_000

/**
 * The wait past which a `last` price is over the visitor's limit (#7523: the shop answers the
 * limit's wait with it). The shop's own wait for a slice that runs is 2 s
 * (`slice_price.py` `PENDING_RETRY`), so a `last` price of one never reaches this; the limit's
 * shorter waits are read again as a slice's are.
 */
const LIMIT_WAIT_MS = RETRY_CAP_MS

/** The same, once the page's one automatic read is spent: well over a running slice's 2 s. */
const SPENT_WAIT_MS = 5_000

/** Refusals that pass only with time: the visitor reads why. */
const LIMITS = new Set(['render_limited', 'rate_limited'])

/** The wait of a limit that names none, as `inTime` words it. */
const LIMIT_SECONDS = 60

/** No firm price: the shop prices the design by hand. */
const BY_HAND: StoreEstimate = { source: 'none', price_minor: null, currency: null, minor_exponent: null, retry_after: null }

/** The price while the shop works it out and no price is known. */
const WORKING: StoreEstimate = { ...BY_HAND, source: 'pending' }

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
 *
 * #7523: over a limit the page reads the price once more on its own, after the wait the shop
 * asks for (at most `timing.limitMs`). After that, and when a read fails or runs out of time,
 * the visitor reads it again with "Try again", once any wait is over.
 */
export function FileCustomize({
  initial,
  timing,
  wait,
  now,
  debounceMs,
  onSourceChange,
}: {
  initial: Listing
  timing: PollTiming
  wait: (ms: number) => Promise<void>
  now: () => number
  /** How long the page waits after the last change before it reads the price. */
  debounceMs: number
  /** The listing read again is a design: its page is the template's. */
  onSourceChange: () => void
}) {
  const [listing, setListing] = useState(initial)
  const view = useMemo(() => partsView(listing), [listing])
  const [values, setValues] = useState<Record<string, ParamValue>>(() => {
    const saved = loadChoices(initial.id)
    return saved ? keepOffered(partsView(initial), saved.values) : {}
  })
  const [quantity, setQuantity] = useState(() => Math.min(loadChoices(initial.id)?.quantity ?? 1, MAX_QUANTITY))
  const [plate, setPlate] = useState(0)
  /** The price on screen and the colours (`current`) it is the price of. `heldFor` is the
   * colours whose read ended with no price of their own (refused, failed or too slow): the
   * price stays on screen for them as the last one, not as updating, since no update will
   * come, and never as firm. */
  const [priced, setPriced] = useState<{ key: string; estimate: StoreEstimate; heldFor?: string }>()
  /** The colours whose price the shop is working out (it answered `pending`). */
  const [pendingFor, setPendingFor] = useState<string>()
  /** Why there is no price of the colours on screen; `limit` is the slug of a limit that is why. */
  const [priceError, setPriceError] = useState<{ text: string; limit?: string | null } | null>(null)
  /** "Try again", once the page reads no more on its own: `seconds` to wait first, and whether the refusal says them. */
  const [again, setAgain] = useState<{ seconds: number; told?: boolean }>()
  /** How often the visitor pressed "Try again". */
  const [attempt, setAttempt] = useState(0)
  /** The next read is the visitor's own: it does not wait out the debounce. */
  const pressed = useRef(false)
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
  const orderable = !listingUnavailable(view) && chosen !== null
  const blocked = !orderable || stale
  const previews = listing.previews ?? []
  /** Whether the price on screen is the firm price of the colours on screen. */
  const firm = !blocked && priced !== undefined && priced.key === current && priced.estimate.source === 'slice' && pendingFor !== current
  /** No read follows: the last one ended with no price, or none is made while blocked. */
  const held = !firm && (blocked || priced?.heldFor === current)
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
    // A read is due: what an earlier one ended with says nothing about it.
    setPriceError(null)
    setAgain(undefined)
    const resumed = () =>
      setPriced((p) => {
        if (!p || (p.key === key && !hasNumber(p.estimate))) return undefined
        return p.heldFor === undefined ? p : { key: p.key, estimate: p.estimate }
      })
    resumed()
    // The key is the body: a change that keeps it (a pick of the colour on screen, a reload
    // that keeps the colours) reads nothing again and leaves a read that runs alone.
    const [listingUpdatedAt, colours] = JSON.parse(key) as [string, Record<string, string>]
    const body = { listing_updated_at: listingUpdatedAt, colours }
    const read = async () => {
      let began = now()
      let delay = timing.firstMs
      /** Whether the page's one automatic read after a limit's wait is spent. */
      let spent = false
      const waitsFor = (seconds: number) => !spent && seconds * 1000 <= timing.limitMs
      /** Waits out a limit, then reads on with a time limit of its own; false when these colours are gone. */
      const late = async (seconds: number) => {
        spent = true
        await wait(seconds * 1000)
        if (!live) return false
        setPriceError(null)
        resumed()
        began = now()
        delay = timing.firstMs
        return true
      }
      /** Offers "Try again", once the limit's wait is over. */
      const offer = async (seconds: number, limit?: StoreError) => {
        setAgain({ seconds, told: limit !== undefined })
        await wait(seconds * 1000)
        if (!live) return
        setAgain({ seconds: 0 })
        if (limit) setPriceError({ text: priceLimitText(limit, null), limit: limit.slug })
      }
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
          // `last` (#6710 §7.5) is the listing's last price while the shop works out the
          // colours' own: it shows at once, marked as updating, and the page reads again.
          const working = answer.source === 'pending' || (answer.source === 'last' && answer.retry_after != null)
          if (!working) {
            setPendingFor(undefined)
            setPriced({ key, estimate: answer })
            return
          }
          if (answer.source === 'last') {
            const seconds = answer.retry_after ?? 0
            if (seconds * 1000 > (spent ? SPENT_WAIT_MS : LIMIT_WAIT_MS)) {
              setPendingFor(undefined)
              setPriced({ key, estimate: answer, heldFor: key })
              if (!waitsFor(seconds)) return await offer(seconds)
              if (await late(seconds)) continue
              return
            }
            setPriced({ key, estimate: answer })
          }
          setPendingFor(key)
          if (await pause(answer.retry_after)) continue
          if (!live) return
          // Past the time limit the last price stays; with none, the shop prices it by hand.
          setPendingFor(undefined)
          setPriced((p) => (p && hasNumber(p.estimate) ? { ...p, heldFor: key } : { key, estimate: BY_HAND }))
          setAgain({ seconds: 0 })
          return
        } catch (caught) {
          if (!live) return
          const limited = passing(caught) && caught.slug !== null && LIMITS.has(caught.slug)
          if (passing(caught) && !limited && (await pause(caught.retryAfter))) continue
          if (!live) return
          setPendingFor(undefined)
          setPriced((p) => {
            if (!p) return p
            // Always show a price: a limit keeps any price on screen as the last one, and any
            // other end keeps the listing's `last` price these colours were answered.
            if (hasNumber(p.estimate) && (limited || p.key === key)) return { ...p, heldFor: key }
            // The shop refuses these colours: a price of others says nothing about them.
            return p.key !== key ? undefined : p
          })
          if (limited) {
            const seconds = caught.retryAfter ?? LIMIT_SECONDS
            const waits = waitsFor(seconds)
            setPriceError({ text: priceLimitText(caught, waits ? 'page' : 'visitor'), limit: caught.slug })
            if (!waits) return await offer(seconds, caught)
            if (await late(seconds)) continue
            return
          }
          if (changedListing(caught)) setStale(true)
          setPriceError({ text: refusalText(caught, shown.current) })
          // A refusal of these colours stands; a read that failed may pass.
          if (!(caught instanceof StoreError) || passing(caught)) setAgain({ seconds: 0 })
          return
        }
      }
    }
    const timer = setTimeout(
      () => {
        started.current = true
        void read()
      },
      started.current && !pressed.current ? debounceMs : 0,
    )
    pressed.current = false
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [blocked, current, attempt, listing.id, timing, wait, now, debounceMs])

  const tryAgain = () => {
    pressed.current = true
    setAttempt((n) => n + 1)
  }

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
      if (fresh.source !== 'library_file') {
        onSourceChange()
        return
      }
      setListing(fresh)
      setValues(keepOffered(partsView(fresh), values))
      setStale(false)
      setPriceError(null)
      setAddError(null)
    } catch (caught) {
      if (alive.current) setPriceError({ text: errorText(caught) })
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
        {/* What a limit's refusal says of the next read holds only while a read can follow. */}
        {priceError && !(blocked && priceError.limit) && (
          <p className="sf-error" role="alert">
            {priceError.text}
          </p>
        )}
        {priceError?.limit === 'render_limited' && !blocked && !(priced && hasNumber(priced.estimate)) && (
          <p className="sf-hint">You can still add it to your order, and we will send you the price before you pay.</p>
        )}
        {priced && hasNumber(priced.estimate) ? (
          // The owner's rule: always show a price. The last one stays, marked as updating,
          // until the colours on screen are priced, or held when their read ended with none.
          <Estimate estimate={priced.estimate} updating={!firm && !held} held={held} orderable={orderable} />
        ) : priced && priced.key === current ? (
          <Estimate estimate={priced.estimate} orderable={orderable} />
        ) : (
          // From the change on, through the debounce, until the colours are priced.
          !blocked && !priceError && <Estimate estimate={WORKING} />
        )}
        {again && !blocked && (
          <button type="button" className="sf-button sf-button-quiet sf-price-again" disabled={again.seconds > 0} onClick={tryAgain}>
            {again.seconds > 0 && !again.told ? `Try again ${inTime(again.seconds)}` : 'Try again'}
          </button>
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
