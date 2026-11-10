import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import {
  ActionKey,
  StoreError,
  errorText,
  store,
  type Listing,
  type ParamValue,
  type RenderInput,
  type StoreEstimate,
  type StoreRender,
} from '../api/client'
import { Estimate } from '../components/Estimate'
import { ListingView } from '../components/ListingView'
import { useDraftChanged } from '../draftCount'
import { keepOffered, listingUnavailable, slotFilaments, valueProblem } from '../params'
import { POLL, type PollTiming } from '../poll'

// three.js is most of the viewer: its own chunk, fetched once there is a render to show.
const Preview = lazy(async () => ({ default: (await import('../../components/Preview')).Preview }))

/** The most of one design a visitor may order (`AddListingItem.quantity`, `le=100`). */
const MAX_QUANTITY = 100

/** The longest a poll waits on a `Retry-After`: the limit is checked at least this often. */
const RETRY_CAP_MS = 30_000

const SLOW = 'This preview is taking longer than usual. Try Render again in a few minutes.'

function wait(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function terminal(render: StoreRender): boolean {
  return render.status !== 'pending' && render.status !== 'running'
}

/** A poll that failed for a reason that passes: the network, a limit, a busy shop. */
function passing(error: unknown): error is StoreError {
  return error instanceof StoreError && (error.status === 0 || error.status === 429 || error.status >= 500)
}

function inSeconds(error: StoreError): string {
  return error.retryAfter === null ? 'in a minute' : `in ${error.retryAfter} seconds`
}

/** What a visitor reads when a render or an add is refused. */
function refusalText(error: unknown): string {
  if (!(error instanceof StoreError)) return errorText(error)
  if (error.slug === 'render_limited') return `You have made a lot of previews. Try again ${inSeconds(error)}.`
  if (error.slug === 'render_queue_full') return `The shop is busy making other previews. Try again ${inSeconds(error)}.`
  if (error.slug === 'rate_limited') return `Your connection has sent a lot of requests. Try again ${inSeconds(error)}.`
  return errorText(error)
}

function changedListing(error: unknown): boolean {
  return error instanceof StoreError && error.slug === 'listing_changed'
}

/**
 * `/l/:id`: one listing to customize (spec §5.4). The visitor changes the parameters the
 * listing offers (`ListingView`, the same form the shop previews), renders a preview,
 * reads the rough price, and adds it to the order. Only the values the visitor set are
 * sent; every locked value and every default comes from the listing's published snapshot
 * on the shop's side, never from this page.
 */
export function CustomizePage({ id, timing = POLL }: { id: string; timing?: PollTiming }) {
  const [listing, setListing] = useState<Listing>()
  const [loadError, setLoadError] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, ParamValue>>({})
  const [job, setJob] = useState<StoreRender>()
  /** The values the job on screen was rendered with, as `JSON.stringify` wrote them. */
  const [jobValues, setJobValues] = useState<string | null>(null)
  const [rendering, setRendering] = useState(false)
  const [renderError, setRenderError] = useState<string | null>(null)
  const [estimate, setEstimate] = useState<StoreEstimate>()
  const [quantity, setQuantity] = useState(1)
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [added, setAdded] = useState(false)
  /** The shop republished the listing since it was read (409 `listing_changed`). */
  const [stale, setStale] = useState(false)
  const [reloading, setReloading] = useState(false)
  const renderKey = useRef(new ActionKey())
  const addKey = useRef(new ActionKey())
  const alive = useRef(true)
  const draftChanged = useDraftChanged()

  useEffect(() => {
    alive.current = true
    let live = true
    store.listing(id).then(
      (answer) => live && setListing(answer),
      (caught: unknown) => live && setLoadError(errorText(caught)),
    )
    return () => {
      live = false
      alive.current = false
    }
  }, [id])

  const problems = useMemo(
    () => (listing ? Object.entries(listing.params).filter(([name, param]) => valueProblem(param, values[name]) !== null) : []),
    [listing, values],
  )

  if (loadError) {
    return (
      <section>
        <p className="sf-error" role="alert">
          {loadError}
        </p>
        <Link to="/">Back to the designs</Link>
      </section>
    )
  }
  if (!listing) return <p className="sf-hint">Loading the design…</p>

  const blocked = listingUnavailable(listing) || problems.length > 0 || stale
  const current = JSON.stringify(values)
  const shownIsCurrent = job?.status === 'done' && jobValues === current
  const input = (): RenderInput => ({
    listing_id: listing.id,
    listing_updated_at: listing.updated_at,
    values,
    // The colours of the render on screen, when it is of these values.
    colours: shownIsCurrent ? slotFilaments(job.colors, listing.colours) : {},
  })

  const setValue = (name: string, next: ParamValue | undefined) => {
    setValues((before) => {
      const copy = { ...before }
      if (next === undefined) delete copy[name]
      else copy[name] = next
      return copy
    })
    setAdded(false)
  }

  /** Reads the render again until it ends, backing off; a poll that fails for a passing reason waits and reads again. */
  const follow = async (first: StoreRender): Promise<StoreRender | null> => {
    let next = first
    let delay = timing.firstMs
    const started = Date.now()
    const timedOut = () => {
      if (Date.now() - started < timing.limitMs) return false
      setRenderError(SLOW)
      return true
    }
    while (!terminal(next)) {
      if (timedOut()) return null
      // Never past the time limit, however long the edge asked to wait. A wait cut to the
      // time left ends at the limit: a timer can fire a millisecond before Date.now()
      // shows it (#5875), so that wait stops the reads without asking the clock.
      const left = started + timing.limitMs - Date.now()
      await wait(Math.min(delay, Math.max(0, left)))
      if (!alive.current) return null
      if (delay >= left) {
        setRenderError(SLOW)
        return null
      }
      if (timedOut()) return null
      try {
        next = await store.render(next.id)
        if (!alive.current) return null
        setJob(next)
        delay = Math.min(delay * 2, timing.maxMs)
      } catch (caught) {
        if (!passing(caught)) throw caught
        // The render runs on: wait as long as the edge asks (at most RETRY_CAP_MS), then
        // read it again.
        delay = Math.max(delay, Math.min((caught.retryAfter ?? 0) * 1000, RETRY_CAP_MS))
      }
    }
    return next
  }

  const render = async () => {
    const body: RenderInput = { ...input(), colours: {} }
    setRendering(true)
    setRenderError(null)
    setEstimate(undefined)
    try {
      const first = await store.startRender(body, renderKey.current.for(JSON.stringify(body)))
      renderKey.current.done()
      setJob(first)
      setJobValues(JSON.stringify(body.values))
      const done = await follow(first)
      if (!alive.current || done === null) return
      if (done.status === 'done') setEstimate(await store.estimate(done.id))
      else setRenderError(done.error ?? 'This preview could not be made. Change something and try again.')
    } catch (caught) {
      if (!alive.current) return
      if (changedListing(caught)) setStale(true)
      setRenderError(refusalText(caught))
    } finally {
      if (alive.current) setRendering(false)
    }
  }

  const add = async () => {
    const body = { ...input(), quantity }
    setAdding(true)
    setAddError(null)
    try {
      const draft = await store.addItem(body, addKey.current.for(JSON.stringify(body)))
      addKey.current.done()
      draftChanged(draft)
      setAdded(true)
    } catch (caught) {
      if (changedListing(caught)) setStale(true)
      setAddError(refusalText(caught))
    } finally {
      setAdding(false)
    }
  }

  const reload = async () => {
    setReloading(true)
    try {
      const latest = await store.listing(listing.id)
      if (!alive.current) return
      setListing(latest)
      setValues((before) => keepOffered(latest, before))
      setStale(false)
      setRenderError(null)
      setAddError(null)
    } catch (caught) {
      if (alive.current) setRenderError(errorText(caught))
    } finally {
      if (alive.current) setReloading(false)
    }
  }

  return (
    <div className="sf-customize">
      <div className="sf-stage">
        {job ? (
          <Suspense fallback={<div className="sf-stage-empty">Loading the viewer…</div>}>
            <Preview job={job} unit="mm" rendering={rendering} />
          </Suspense>
        ) : (
          <div className="sf-stage-empty">Press Render to see your design in 3D.</div>
        )}
      </div>
      <div className="sf-controls">
        <Link className="sf-back" to="/">
          All designs
        </Link>
        <ListingView listing={listing} values={values} onChange={setValue} />
        {stale && (
          <div className="sf-panel sf-stale" role="status">
            <p>The shop has changed this design since you opened it.</p>
            <button type="button" className="sf-button" disabled={reloading} onClick={() => void reload()}>
              {reloading ? 'Loading…' : 'Load the latest version'}
            </button>
          </div>
        )}
        <div className="sf-actions">
          <button type="button" className="sf-button sf-button-quiet" disabled={blocked || rendering} onClick={() => void render()}>
            {rendering ? 'Rendering…' : 'Render'}
          </button>
        </div>
        {job && !rendering && jobValues !== current && (
          <p className="sf-hint" role="status">
            Preview shows earlier choices. Render again to see your changes.
          </p>
        )}
        {renderError && (
          <p className="sf-error" role="alert">
            {renderError}
          </p>
        )}
        {estimate && jobValues === current && <Estimate estimate={estimate} />}
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
