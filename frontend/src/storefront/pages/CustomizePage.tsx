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
import { clearChoices, loadChoices, saveChoices } from '../customizeState'
import { useDraftChanged } from '../draftCount'
import { filamentLabels, keepOffered, keepPicks, listingMaterials, listingUnavailable, oneFilamentPerColour, settleMaterial, startMaterial, valueProblem } from '../params'
import { POLL, type PollTiming } from '../poll'

// three.js is most of the viewer: its own chunk, fetched once there is a render to show.
const Preview = lazy(async () => ({ default: (await import('../../components/Preview')).Preview }))

/** The most of one design a visitor may order (`AddListingItem.quantity`, `le=100`). */
const MAX_QUANTITY = 100

/** The longest a poll waits on a `Retry-After`: the limit is checked at least this often. */
const RETRY_CAP_MS = 30_000

const SLOW = 'This preview is taking longer than usual. Change something, or come back in a few minutes.'

/** How long the page waits after the last change before it renders (#6151). */
const DEBOUNCE_MS = 900

/** The longest the page waits on a `Retry-After` before its one retry of a refused render. */
const RETRY_RENDER_CAP_MS = 60_000

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Whether a render has ended. A done one ends once it names its preview: until the shop
 * has its files at hand it names none, and the page reads it again (#6120).
 */
function terminal(render: StoreRender): boolean {
  if (render.status === 'done') return render.preview_url !== null
  return render.status !== 'pending' && render.status !== 'running'
}

/** What the viewer is shown of a render: a done one with no preview yet as still running. */
function onScreen(render: StoreRender): StoreRender {
  return render.status === 'done' && render.preview_url === null ? { ...render, status: 'running' } : render
}

/** A poll that failed for a reason that passes: the network, a limit, a busy shop. */
function passing(error: unknown): error is StoreError {
  return error instanceof StoreError && (error.status === 0 || error.status === 429 || error.status >= 500)
}

function inSeconds(error: StoreError): string {
  if (error.retryAfter === null) return 'in a minute'
  return error.retryAfter === 1 ? 'in 1 second' : `in ${error.retryAfter} seconds`
}

/** What a visitor reads when a render or an add is refused. */
function refusalText(error: unknown): string {
  if (!(error instanceof StoreError)) return errorText(error)
  if (error.slug === 'render_limited') return `You have made a lot of previews. Try again ${inSeconds(error)}.`
  if (error.slug === 'render_queue_full') return `The shop is busy making other previews. Try again ${inSeconds(error)}.`
  if (error.slug === 'rate_limited') return `Your connection has sent a lot of requests. Try again ${inSeconds(error)}.`
  // #6152: the page gives one colour one filament (`oneFilamentPerColour`); this is the shop's own check.
  if (error.slug === 'pick_colour_clash') return 'Two of your colours are one colour of two filaments. A colour prints from one filament: pick the same one for both.'
  return errorText(error)
}

/** A render the shop refused for now, that one retry after `Retry-After` may get. */
function retryable(error: unknown): error is StoreError {
  return error instanceof StoreError && (error.slug === 'render_limited' || error.slug === 'render_queue_full')
}

function changedListing(error: unknown): boolean {
  return error instanceof StoreError && error.slug === 'listing_changed'
}

/** A done render and the choices it was made of (`choicesKey`). */
interface Shown {
  job: StoreRender
  key: string
}

/**
 * The choices a render is of: the listing as read, the visitor's values and picks. A new
 * pick of the same colour asks again: the shop reuses the render and prices the pick.
 */
function choicesKey(listing: Listing, values: Record<string, ParamValue>, picks: Record<string, string>): string {
  return JSON.stringify([listing.updated_at, values, picks])
}

/** The material `material` names, if `listing` still offers it for a customer to pick. */
function offeredMaterial(listing: Listing, material: string | null | undefined): string | null {
  return listingMaterials(listing).find((each) => each === material) ?? null
}

/**
 * `/l/:id`: one listing to customize (spec §5.4). The visitor changes the parameters the
 * listing offers (`ListingView`, the same form the shop previews), sees the preview and
 * the rough price, and adds it to the order. Only the values the visitor set are sent;
 * every locked value and every default comes from the listing's published snapshot on the
 * shop's side, never from this page.
 *
 * #6151: the page renders on its own, at once on load and `debounceMs` after the last
 * change, and follows only the latest render. The visitor's choices are kept per listing
 * (`customizeState.ts`) until they are added to the order, so leaving the page loses
 * nothing; coming back sends the same values, and the shop reuses the render it made.
 */
export function CustomizePage({
  id,
  timing = POLL,
  wait = sleep,
  now = Date.now,
  debounceMs = DEBOUNCE_MS,
}: {
  id: string
  timing?: PollTiming
  /** How a poll waits; a test passes one that fires early, as a real timer can (#5902). */
  wait?: (ms: number) => Promise<void>
  /** The clock the time limit is read on; a test passes one that moves only when `wait` does. */
  now?: () => number
  /** How long the page waits after the last change before it renders. */
  debounceMs?: number
}) {
  const [listing, setListing] = useState<Listing>()
  const [loadError, setLoadError] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, ParamValue>>({})
  /** The filament picked for each colour parameter, by id (#6152): the shop prices it and fills the slots from it. */
  const [picks, setPicks] = useState<Record<string, string>>({})
  /** With `one_material`, the material shown (`startMaterial` until the visitor picks one); else null. */
  const [material, setMaterial] = useState<string | null>(null)
  const [materialNote, setMaterialNote] = useState<string | null>(null)
  /** #6152: which other colours a pick moved to its filament (`oneFilamentPerColour`). */
  const [pickNote, setPickNote] = useState<string | null>(null)
  /** The latest render while it runs; the viewer shows its progress until one is done. */
  const [job, setJob] = useState<StoreRender>()
  /** The last done render: the viewer keeps it on screen while a newer one runs. */
  const [shown, setShown] = useState<Shown>()
  const [rendering, setRendering] = useState(false)
  const [renderError, setRenderError] = useState<string | null>(null)
  /**
   * The rough price of the render `job` names, for the choices `key` (`choicesKey`). #6152:
   * a pick of another filament of the same colour reuses the render, so the job alone does
   * not tell the price of one pick from that of the other.
   */
  const [estimate, setEstimate] = useState<{ job: string; key: string; estimate: StoreEstimate }>()
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
  /** Counts the renders this page started: only the latest may change what it shows. */
  const latest = useRef(0)
  /** Whether the page rendered yet: the first render does not wait out the debounce. */
  const started = useRef(false)
  const renderNow = useRef<(key: string, retry: boolean) => Promise<void>>(async () => undefined)
  /** The choices on screen, and whether the page refuses them: what a delayed retry checks. */
  const choicesNow = useRef({ key: '', blocked: true })
  const draftChanged = useDraftChanged()

  useEffect(() => {
    alive.current = true
    let live = true
    store.listing(id).then(
      (answer) => {
        if (!live) return
        const saved = loadChoices(id)
        setListing(answer)
        const kept = saved ? keepOffered(answer, saved.values) : {}
        const shownMaterial = answer.one_material ? (offeredMaterial(answer, saved?.material) ?? startMaterial(answer)) : null
        const settled = settleMaterial(answer, kept, saved ? keepPicks(answer, kept, saved.picks ?? {}) : {}, shownMaterial)
        setValues(settled.values)
        setPicks(settled.picks)
        setMaterial(shownMaterial)
        if (saved) setQuantity(Math.min(saved.quantity, MAX_QUANTITY))
      },
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
  const blocked = !listing || listingUnavailable(listing) || problems.length > 0 || stale
  /** Only the picks the values still match: the shop refuses any other. */
  const sentPicks = useMemo(() => (listing ? keepPicks(listing, values, picks) : {}), [listing, values, picks])
  const current = listing ? choicesKey(listing, values, sentPicks) : ''

  useEffect(() => {
    if (listing) saveChoices(listing.id, { values, quantity, picks, material })
  }, [listing, values, quantity, picks, material])

  useEffect(() => {
    choicesNow.current = { key: current, blocked }
  }, [current, blocked])

  useEffect(() => {
    if (blocked || shown?.key === current) return
    const timer = setTimeout(
      () => {
        started.current = true
        void renderNow.current(current, false)
      },
      started.current ? debounceMs : 0,
    )
    return () => clearTimeout(timer)
  }, [blocked, current, shown, debounceMs])

  const shownIsCurrent = shown?.key === current
  /**
   * #6152: the shop maps picks to the slots of the done render of these choices, and
   * answers 409 `render_pending` to an add with picks until that render is done.
   */
  const waitsForPreview = !shownIsCurrent && Object.keys(sentPicks).length > 0
  const input = (on: Listing): RenderInput => ({
    listing_id: on.id,
    listing_updated_at: on.updated_at,
    values,
    // #6152: the picks, never a guess of the slots. The shop maps them to the render's
    // slots by colour.
    picks: sentPicks,
    colours: {},
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

  const setPick = (name: string, next: string | undefined) => {
    if (!listing) return
    setMaterialNote(null)
    setPickNote(null)
    if (next === undefined && listing.one_material) {
      // #6152: "Use the shop's choice" on a `one_material` listing. With no value, the shop
      // would use the listing's default, which can be of another material (422
      // `listing_mixed_material`): the colour goes to the nearest of the shown material.
      const without = <T,>(record: Record<string, T>) => Object.fromEntries(Object.entries(record).filter(([key]) => key !== name))
      const settled = settleMaterial(listing, without(values), without(picks), material)
      setValues(settled.values)
      setPicks(settled.picks)
      return
    }
    const copy = { ...picks }
    if (next === undefined) delete copy[name]
    else copy[name] = next
    // #6152: a colour prints from one filament. A pick of a colour another parameter
    // already picked moves that parameter to the new filament, and the page says so. Only
    // the picks the page sends count (`keepPicks`); `values` does not yet hold this one.
    const sent = next === undefined ? {} : { ...keepPicks(listing, values, copy), [name]: next }
    const one = oneFilamentPerColour(listing, sent, name)
    setPicks({ ...copy, ...one.picks })
    const filament = listing.colours.find((each) => each.id === next)
    if (filament && one.moved.length > 0) {
      const names = one.moved.map((moved) => listing.params[moved]?.label ?? moved)
      const shown = `${filamentLabels(listing.colours).get(filament.id) ?? filament.colour} ${filament.material}`
      setPickNote(`${names.join(' and ')} ${names.length === 1 ? 'uses' : 'use'} ${shown} too. One colour prints from one filament.`)
    }
  }

  /**
   * #6152: a `one_material` listing's material. Every colour moves to the nearest colour
   * (RGB distance) of the new material (`settleMaterial`), and the page says so when one the
   * visitor set changed.
   */
  const chooseMaterial = (next: string) => {
    if (!listing || next === material) return
    const { values: nextValues, picks: nextPicks, moved } = settleMaterial(listing, values, picks, next)
    setMaterial(next)
    setValues(nextValues)
    setPicks(nextPicks)
    setPickNote(null)
    setMaterialNote(moved ? `Your colours changed to the nearest ${next} colours.` : null)
    setAdded(false)
  }

  /** Reads the render again until it ends, backing off; a poll that fails for a passing reason waits and reads again. */
  const follow = async (first: StoreRender, live: () => boolean): Promise<StoreRender | null> => {
    let next = first
    let delay = timing.firstMs
    const began = now()
    const timedOut = () => {
      if (now() - began < timing.limitMs) return false
      setRenderError(SLOW)
      return true
    }
    while (!terminal(next)) {
      if (timedOut()) return null
      // Never past the time limit, however long the edge asked to wait. A wait cut to the
      // time left ends at the limit: a timer can fire a millisecond before Date.now()
      // shows it (#5875), so that wait stops the reads without asking the clock.
      const left = began + timing.limitMs - now()
      await wait(Math.min(delay, Math.max(0, left)))
      if (!live()) return null
      if (delay >= left) {
        setRenderError(SLOW)
        return null
      }
      if (timedOut()) return null
      try {
        next = await store.render(next.id)
        if (!live()) return null
        setJob(onScreen(next))
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

  /**
   * The rough price of a done render. While the shop works it out it answers 429
   * `estimate_pending` with a `Retry-After` (#6120); that, and any other passing failure (a
   * 5xx, the network), waits and reads again, backing off, until the time limit.
   */
  const estimateOf = async (job: string, live: () => boolean): Promise<StoreEstimate | null> => {
    const began = now()
    let delay = timing.firstMs
    for (;;) {
      try {
        return await store.estimate(job)
      } catch (caught) {
        if (!passing(caught)) throw caught
        const left = began + timing.limitMs - now()
        if (left <= 0) throw caught
        await wait(Math.min(Math.max(delay, Math.min((caught.retryAfter ?? 0) * 1000, RETRY_CAP_MS)), left))
        if (!live()) return null
        delay = Math.min(delay * 2, timing.maxMs)
      }
    }
  }

  /** Renders the values on screen (`key`), and shows the render once it is done, unless a newer one started. */
  const render = async (key: string, retry: boolean) => {
    if (!listing) return
    const body: RenderInput = { ...input(listing), colours: {} }
    const mine = ++latest.current
    const live = () => alive.current && latest.current === mine
    setRendering(true)
    setRenderError(null)
    // #6152: the old price is hidden until the new one is read (spec 2026-11-13 §5.3).
    setEstimate(undefined)
    try {
      const first = await store.startRender(body, renderKey.current.for(JSON.stringify(body)))
      renderKey.current.done()
      if (!live()) return
      setJob(onScreen(first))
      const done = await follow(first, live)
      if (!live() || done === null) return
      if (done.status !== 'done') {
        setRenderError(done.error ?? 'This preview could not be made. Change something and try again.')
        return
      }
      setShown({ job: done, key })
      const rough = await estimateOf(done.id, live)
      if (rough && live()) setEstimate({ job: done.id, key, estimate: rough })
    } catch (caught) {
      if (!live()) return
      if (changedListing(caught)) setStale(true)
      setRenderError(refusalText(caught))
      if (retryable(caught) && !retry) {
        const after = Math.min((caught.retryAfter ?? 60) * 1000, RETRY_RENDER_CAP_MS)
        setTimeout(() => {
          // Only the same choices, and only while the page still takes them: a change in
          // the meantime renders through the debounce as any other does.
          if (live() && choicesNow.current.key === key && !choicesNow.current.blocked) void renderNow.current(key, true)
        }, after)
      }
    } finally {
      if (live()) setRendering(false)
    }
  }
  useEffect(() => {
    renderNow.current = render
  })

  const add = async () => {
    if (!listing) return
    const body = { ...input(listing), quantity }
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
      setAddError(refusalText(caught))
    } finally {
      setAdding(false)
    }
  }

  const reload = async () => {
    if (!listing) return
    setReloading(true)
    try {
      const fresh = await store.listing(listing.id)
      if (!alive.current) return
      setListing(fresh)
      const kept = keepOffered(fresh, values)
      const shownMaterial = fresh.one_material ? (offeredMaterial(fresh, material) ?? startMaterial(fresh)) : null
      const settled = settleMaterial(fresh, kept, keepPicks(fresh, kept, picks), shownMaterial)
      setValues(settled.values)
      setPicks(settled.picks)
      setMaterial(shownMaterial)
      setStale(false)
      setRenderError(null)
      setAddError(null)
    } catch (caught) {
      if (alive.current) setRenderError(errorText(caught))
    } finally {
      if (alive.current) setReloading(false)
    }
  }

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

  return (
    <div className="sf-customize">
      <div className="sf-stage">
        {shown || job ? (
          <Suspense fallback={<div className="sf-stage-empty">Loading the viewer…</div>}>
            <Preview job={shown?.job ?? job} unit="mm" rendering={rendering} />
          </Suspense>
        ) : (
          <div className="sf-stage-empty">
            {blocked
              ? 'Your design shows here once your choices are complete.'
              : renderError && !rendering
                ? 'No preview yet.'
                : 'Making your preview…'}
          </div>
        )}
      </div>
      <div className="sf-controls">
        <Link className="sf-back" to="/">
          All designs
        </Link>
        <ListingView
          listing={listing}
          values={values}
          onChange={setValue}
          picks={picks}
          onPick={setPick}
          material={material}
          onMaterial={chooseMaterial}
          materialNote={materialNote}
        />
        {pickNote && (
          <p className="sf-hint" role="status">
            {pickNote}
          </p>
        )}
        {stale && (
          <div className="sf-panel sf-stale" role="status">
            <p>The shop has changed this design since you opened it.</p>
            <button type="button" className="sf-button" disabled={reloading} onClick={() => void reload()}>
              {reloading ? 'Loading…' : 'Load the latest version'}
            </button>
          </div>
        )}
        {rendering && (
          <p className="sf-hint" role="status">
            {shown ? 'Updating your preview and price…' : 'Making your preview and price…'}
          </p>
        )}
        {renderError && (
          <div className="sf-retry">
            <p className="sf-error" role="alert">
              {renderError}
            </p>
            {!rendering && !blocked && (
              <button type="button" className="sf-button sf-button-quiet" onClick={() => void renderNow.current(current, false)}>
                Try again
              </button>
            )}
          </div>
        )}
        {estimate && shownIsCurrent && estimate.key === current && estimate.job === shown.job.id && <Estimate estimate={estimate.estimate} />}
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
          <button type="button" className="sf-button" disabled={blocked || adding || waitsForPreview} onClick={() => void add()}>
            {adding ? 'Adding…' : 'Add to order'}
          </button>
        </div>
        {waitsForPreview && !blocked && (
          <p className="sf-hint">
            {/* #6152: a failed or refused preview never shows by itself; say to try again. */}
            {renderError && !rendering ? 'You can add it once its preview shows. Its preview could not be made: try again.' : 'You can add it once its preview shows.'}
          </p>
        )}
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
