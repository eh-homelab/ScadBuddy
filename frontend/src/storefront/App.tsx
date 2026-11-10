import { useCallback, useEffect, useState } from 'react'
import { Link, Route, Routes, useParams } from 'react-router'
import { store, type Draft } from './api/client'
import { RequestForm } from './components/RequestForm'
import { DraftChanged } from './draftCount'
import { CustomizePage } from './pages/CustomizePage'
import { DraftPage } from './pages/DraftPage'
import { ListingsPage } from './pages/ListingsPage'
import { OrderPage } from './pages/OrderPage'

function CustomizeRoute() {
  const { id = '' } = useParams()
  // Keyed by the listing, so another listing starts with none of this one's values.
  return <CustomizePage key={id} id={id} />
}

function RequestRoute() {
  return (
    <section>
      <h1 className="sf-h1">Ask for a design</h1>
      <p className="sf-lead">Tell us what you need. We add your request to your order and reply with a design and a price.</p>
      <RequestForm />
    </section>
  )
}

function NotFound() {
  return (
    <section>
      <h1 className="sf-h1">There is no page here.</h1>
      <Link className="sf-button" to="/">
        Browse the designs
      </Link>
    </section>
  )
}

/** The public storefront page (#5282, spec 2026-11-02 §5): the shop's own host, no back office. */
export function App() {
  const [count, setCount] = useState<number | null>(null)
  const draftChanged = useCallback((draft: Draft | null) => setCount(draft?.items.length ?? 0), [])

  useEffect(() => {
    store.draft().then(draftChanged, () => undefined)
  }, [draftChanged])

  return (
    <DraftChanged.Provider value={draftChanged}>
      <div className="sf-page">
        <header className="sf-header">
          <Link className="sf-mark" to="/">
            <span className="sf-mark-layers" aria-hidden="true" />
            Print shop
          </Link>
          <nav aria-label="Shop">
            <Link className="sf-cart" to="/draft" aria-label={count ? `Your order, ${count} ${count === 1 ? 'item' : 'items'}` : 'Your order'}>
              Your order
              {count ? (
                <span className="sf-cart-count" aria-hidden="true">
                  {count}
                </span>
              ) : null}
            </Link>
          </nav>
        </header>
        <main className="sf-main">
          <Routes>
            <Route path="/" element={<ListingsPage />} />
            <Route path="/l/:id" element={<CustomizeRoute />} />
            <Route path="/draft" element={<DraftPage />} />
            <Route path="/request" element={<RequestRoute />} />
            <Route path="/o" element={<OrderPage />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </div>
    </DraftChanged.Provider>
  )
}
