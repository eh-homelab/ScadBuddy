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
import { ParamField } from '../components/ParamField'
import { useDraftChanged } from '../draftCount'
import { listingUnavailable, slotFilaments, valueProblem } from '../params'

// three.js is most of the viewer: its own chunk, fetched once there is a render to show.
const Preview = lazy(async () => ({ default: (await import('../../components/Preview')).Preview }))

/** The most of one design a visitor may order (`AddListingItem.quantity`, `le=100`). */
const MAX_QUANTITY = 100
/** How long between two reads of a render in flight. */
const POLL_MS = 700

function wait(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** The fixed text of a render refused for its limit (429), with the edge's `Retry-After`. */
function limitedText(error: StoreError): string {
  return error.retryAfter === null
    ? 'You have made a lot of previews. Try again in a minute.'
    : `You have made a lot of previews. Try again in ${error.retryAfter} seconds.`
}

/**
 * `/l/:id`: one listing to customize (spec §5.4). The visitor changes the parameters the
 * listing offers, renders a preview, reads the rough price, and adds it to the order. Only
 * the values the visitor set are sent; every locked value and every default comes from the
 * listing's published snapshot on the shop's side, never from this page.
 */
export function CustomizePage({ id }: { id: string }) {
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

  const unavailable = listingUnavailable(listing)
  const blocked = unavailable || problems.length > 0
  const current = JSON.stringify(values)
  const input = (): RenderInput => ({
    listing_id: listing.id,
    listing_updated_at: listing.updated_at,
    values,
    // The colours of the render on screen, when it is of these values.
    colours: job?.status === 'done' && jobValues === current ? slotFilaments(job.colors, listing.colours) : {},
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

  const render = async () => {
    const body: RenderInput = { ...input(), colours: {} }
    setRendering(true)
    setRenderError(null)
    setEstimate(undefined)
    try {
      let next = await store.startRender(body, renderKey.current.for(JSON.stringify(body)))
      renderKey.current.done()
      setJob(next)
      setJobValues(JSON.stringify(body.values))
      while (alive.current && (next.status === 'pending' || next.status === 'running')) {
        await wait(POLL_MS)
        next = await store.render(next.id)
        if (alive.current) setJob(next)
      }
      if (alive.current && next.status === 'done') setEstimate(await store.estimate(next.id))
      else if (alive.current && next.status !== 'done') setRenderError(next.error ?? 'This preview could not be made. Change something and try again.')
    } catch (caught) {
      if (!alive.current) return
      setRenderError(caught instanceof StoreError && caught.status === 429 ? limitedText(caught) : errorText(caught))
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
      setAddError(errorText(caught))
    } finally {
      setAdding(false)
    }
  }

  const image = listing.media.find((media) => media.kind === 'image')
  return (
    <article className="sf-customize">
      <div className="sf-stage">
        {job ? (
          <Suspense fallback={<div className="sf-stage-empty">Loading the viewer…</div>}>
            <Preview job={job} rendering={rendering} />
          </Suspense>
        ) : image ? (
          <img className="sf-stage-image" src={image.url} alt={image.caption} />
        ) : (
          <div className="sf-stage-empty">Press Render to see your design in 3D.</div>
        )}
      </div>
      <div className="sf-controls">
        <Link className="sf-back" to="/">
          All designs
        </Link>
        <h1 className="sf-h1 sf-layered">{listing.title}</h1>
        {listing.description && <p className="sf-description">{listing.description}</p>}
        {unavailable && (
          <p className="sf-error" role="status">
            This design cannot be ordered right now.
          </p>
        )}
        {Object.keys(listing.params).length === 0 && <p className="sf-hint">This design is made as shown.</p>}
        <div className="sf-fields">
          {Object.entries(listing.params).map(([name, param]) => (
            <ParamField key={name} param={param} value={values[name]} filaments={listing.colours} onChange={(next) => setValue(name, next)} />
          ))}
        </div>
        <div className="sf-actions">
          <button type="button" className="sf-button sf-button-quiet" disabled={blocked || rendering} onClick={() => void render()}>
            {rendering ? 'Rendering…' : 'Render'}
          </button>
        </div>
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
    </article>
  )
}
