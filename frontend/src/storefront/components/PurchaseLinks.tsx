import { useId } from 'react'
import type { PurchaseLink } from '../api/client'
import { LINK_PROBLEM, MAX_LINK_CHARS, MAX_LINK_NOTE, MAX_PURCHASE_LINKS, checkedLink } from '../models'

/**
 * "Things to buy for this project" (#6133, spec 2026-11-13 §10.1): up to five links, each
 * with a note, such as "the 6 mm magnets, 20 of them". The shop buys each by hand and adds
 * what it paid to the order. A link is text only: the page never opens it.
 */
export function PurchaseLinks({
  links,
  onChange,
}: {
  links: readonly PurchaseLink[]
  onChange: (links: PurchaseLink[]) => void
}) {
  const id = useId()
  const set = (index: number, change: Partial<PurchaseLink>) =>
    onChange(links.map((link, i) => (i === index ? { ...link, ...change } : link)))
  return (
    <fieldset className="sf-field sf-buy">
      <legend className="sf-label">Things to buy for this project</legend>
      <p className="sf-hint">
        Links to parts or paid files we should buy for you, such as magnets or screws. We add what we pay to your order
        before you pay.
      </p>
      {links.length > 0 && (
        <ol className="sf-buy-list">
          {links.map((link, index) => {
            const bad = link.url.trim() !== '' && checkedLink(link.url) === null
            return (
              <li key={index} className="sf-buy-row">
                <label className="sf-label" htmlFor={`${id}-url-${index}`}>{`Link to buy ${index + 1}`}</label>
                <input
                  id={`${id}-url-${index}`}
                  className="sf-input"
                  type="url"
                  inputMode="url"
                  maxLength={MAX_LINK_CHARS}
                  value={link.url}
                  aria-invalid={bad}
                  onChange={(event) => set(index, { url: event.target.value })}
                />
                {bad && <p className="sf-hint sf-hint-bad">{LINK_PROBLEM}</p>}
                <label className="sf-label" htmlFor={`${id}-note-${index}`}>{`Note for link ${index + 1}`}</label>
                <input
                  id={`${id}-note-${index}`}
                  className="sf-input"
                  maxLength={MAX_LINK_NOTE}
                  value={link.note}
                  placeholder="Which one, and how many"
                  onChange={(event) => set(index, { note: event.target.value })}
                />
                <button
                  type="button"
                  className="sf-link"
                  aria-label={`Remove link to buy ${index + 1}`}
                  onClick={() => onChange(links.filter((_, i) => i !== index))}
                >
                  Remove
                </button>
              </li>
            )
          })}
        </ol>
      )}
      {links.length < MAX_PURCHASE_LINKS && (
        <button type="button" className="sf-link" onClick={() => onChange([...links, { url: '', note: '' }])}>
          Add a thing to buy
        </button>
      )}
    </fieldset>
  )
}
