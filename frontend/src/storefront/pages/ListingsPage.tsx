import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { errorText, store, type Listing, type Listings } from '../api/client'
import { listingUnavailable } from '../params'

/** `/`: the shop's published listings, and "Ask for a design" when the shop takes requests. */
export function ListingsPage() {
  const [listings, setListings] = useState<Listings>()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    store.listings().then(
      (answer) => live && setListings(answer),
      (caught: unknown) => live && setError(errorText(caught)),
    )
    return () => {
      live = false
    }
  }, [])

  return (
    <>
      <section className="sf-hero">
        <h1 className="sf-layered">Printed to order, made to your measure.</h1>
        <p className="sf-lead">Pick a design, change it to suit you, and see it in 3D before we print it.</p>
      </section>
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      {!listings && !error && <p className="sf-hint">Loading the designs…</p>}
      {listings && listings.listings.length === 0 && <p className="sf-lead">There is nothing for sale yet.</p>}
      {listings && listings.listings.length > 0 && (
        <div className="sf-grid">
          {listings.listings.map((listing) => (
            <ListingCard key={listing.id} listing={listing} />
          ))}
        </div>
      )}
      {listings?.requests_enabled && (
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

function ListingCard({ listing }: { listing: Listing }) {
  const unavailable = listingUnavailable(listing)
  const image = listing.media.find((media) => media.kind === 'image')
  return (
    <article className="sf-card">
      <div className="sf-card-media">
        {image ? <img src={image.url} alt={image.caption} loading="lazy" /> : <span className="sf-card-blank" aria-hidden="true" />}
      </div>
      <div className="sf-card-body">
        <h2 className="sf-card-title">{listing.title}</h2>
        {listing.description && <p className="sf-card-text">{listing.description}</p>}
        {unavailable && <p className="sf-tag-bad">Not available right now</p>}
        <Link className="sf-card-link" to={`/l/${encodeURIComponent(listing.id)}`} aria-label={`Customize ${listing.title}`}>
          Customize
        </Link>
      </div>
    </article>
  )
}
