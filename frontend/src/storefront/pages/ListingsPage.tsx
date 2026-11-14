import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { errorText, store, type ListingCard } from '../api/client'

/** How long the page waits after the last key before it searches. */
const SEARCH_DEBOUNCE_MS = 300
/** The longest search the edge takes (`LISTINGS_MAX_QUERY`): a longer `?q=` is cut. */
const MAX_QUERY = 100

/**
 * `/`: the shop's published listings, a page of cards at a time (#6154), and "Ask for a
 * design" when the shop takes requests. The search is in the address (`?q=`), so going
 * back to the list keeps it.
 */
export function ListingsPage({ debounceMs = SEARCH_DEBOUNCE_MS }: { debounceMs?: number }) {
  const [params, setParams] = useSearchParams()
  const query = (params.get('q') ?? '').trim().slice(0, MAX_QUERY)
  const [typed, setTyped] = useState(query)
  /** The search this page last put in the address: any other came from outside it. */
  const wrote = useRef(query)
  const [loading, setLoading] = useState(true)
  const [cards, setCards] = useState<ListingCard[]>()
  const [next, setNext] = useState<string | null>(null)
  const [requestsEnabled, setRequestsEnabled] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [more, setMore] = useState(false)
  /** Counts the searches this page started: a page of an older one is dropped. */
  const searches = useRef(0)

  useEffect(() => {
    let live = true
    searches.current += 1
    setNext(null)
    setMore(false)
    // A search from outside the box (the shop's mark, Back): the box shows it.
    if (query !== wrote.current) {
      wrote.current = query
      setTyped(query)
    }
    setError(null)
    setLoading(true)
    store.listings({ q: query }).then(
      (answer) => {
        if (!live) return
        setCards(answer.listings)
        setNext(answer.next_cursor)
        setRequestsEnabled(answer.requests_enabled)
        setLoading(false)
      },
      (caught: unknown) => {
        if (!live) return
        setError(errorText(caught))
        setLoading(false)
      },
    )
    return () => {
      live = false
    }
  }, [query])

  useEffect(() => {
    if (typed.trim() === query) return
    const timer = setTimeout(() => {
      wrote.current = typed.trim()
      setParams(typed.trim() ? { q: typed.trim() } : {}, { replace: true })
    }, debounceMs)
    return () => clearTimeout(timer)
  }, [typed, query, setParams, debounceMs])

  const showMore = async () => {
    if (!next || loading) return
    const search = searches.current
    setMore(true)
    try {
      const answer = await store.listings({ cursor: next, q: query })
      if (searches.current !== search) return
      // A card the shop moved between two pages comes once.
      setCards((before) => {
        const seen = new Set((before ?? []).map((card) => card.id))
        return [...(before ?? []), ...answer.listings.filter((card) => !seen.has(card.id))]
      })
      setNext(answer.next_cursor)
    } catch (caught) {
      if (searches.current === search) setError(errorText(caught))
    } finally {
      if (searches.current === search) setMore(false)
    }
  }

  return (
    <>
      <section className="sf-hero">
        <h1 className="sf-layered">Printed to order, made to your measure.</h1>
        <p className="sf-lead">Pick a design, change it to suit you, and see it in 3D before we print it.</p>
      </section>
      <form className="sf-search" role="search" onSubmit={(event) => event.preventDefault()}>
        <label className="sf-label" htmlFor="sf-search">
          Search designs
        </label>
        <input
          id="sf-search"
          className="sf-input"
          type="search"
          value={typed}
          maxLength={MAX_QUERY}
          placeholder="Keychain, sign, name…"
          onChange={(event) => setTyped(event.target.value)}
        />
      </form>
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      {loading && !error && <p className="sf-hint">{cards ? 'Searching…' : 'Loading the designs…'}</p>}
      {cards && cards.length === 0 && !loading && (
        <p className="sf-lead">{query ? `No design matches “${query}”.` : 'There is nothing for sale yet.'}</p>
      )}
      {cards && cards.length > 0 && (
        <div className="sf-grid">
          {cards.map((card) => (
            <Card key={card.id} card={card} />
          ))}
        </div>
      )}
      {next && !loading && (
        <div className="sf-more">
          <button type="button" className="sf-button sf-button-quiet" disabled={more} onClick={() => void showMore()}>
            {more ? 'Loading…' : 'Show more designs'}
          </button>
        </div>
      )}
      {requestsEnabled && (
        <section className="sf-ask">
          <h2 className="sf-h2">Have something else in mind?</h2>
          <p>Tell us what you need and send a photo or a sketch. We reply with a design and a price.</p>
          <Link className="sf-button" to="/request">
            Ask for a design
          </Link>
        </section>
      )}
    </>
  )
}

function Card({ card }: { card: ListingCard }) {
  return (
    <article className="sf-card">
      <div className="sf-card-media">
        {card.image ? (
          <img src={card.image.url} alt={card.image.caption} loading="lazy" />
        ) : (
          <span className="sf-card-blank" aria-hidden="true" />
        )}
      </div>
      <div className="sf-card-body">
        <h2 className="sf-card-title">{card.title}</h2>
        {card.description && <p className="sf-card-text">{card.description}</p>}
        {!card.available && <p className="sf-tag-bad">Not available right now</p>}
        <Link className="sf-card-link" to={`/l/${encodeURIComponent(card.id)}`} aria-label={`Customize ${card.title}`}>
          Customize
        </Link>
      </div>
    </article>
  )
}
