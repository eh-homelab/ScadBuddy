import { useId, useRef, useState, type FormEvent } from 'react'
import { RadioGroup, RadioGroupItem } from '../../components/ui/radio-group'
import { formatMoney } from '../../lib/money'
import { ActionKey, StoreError, errorText, store, type Draft, type SubmitInput, type SubmittedOrder } from '../api/client'
import { forgetTurnstileSiteKey, useTurnstileSiteKey } from '../turnstile'
import { Turnstile } from './Turnstile'

interface Props {
  draft: Draft
  onSubmitted: (order: SubmittedOrder) => void
}

const EMPTY_ADDRESS = { recipient: '', street: '', street2: '', locality: '', region: '', postalCode: '', country: '' }

/**
 * Submit (spec 2026-11-02 §5.5): a name, an email, one of the shop's shipping options and,
 * when that option ships, the address. The shop mails a link to the order to the email.
 * One `Idempotency-Key` per submit: sent again after a failure, the same form is the
 * same submit to the shop, which makes one order and sends one mail. The bot check
 * (§5.8) shows its widget while it is on, and each send takes a new token.
 */
export function SubmitForm({ draft, onSubmitted }: Props) {
  const id = useId()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [optionId, setOptionId] = useState<string | null>(null)
  const [address, setAddress] = useState(EMPTY_ADDRESS)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const key = useRef(new ActionKey())
  const [token, setToken] = useState<string | null>(null)
  const [widget, setWidget] = useState(0)
  const siteKey = useTurnstileSiteKey(widget)
  const needsCheck = siteKey !== null && token === null
  const options = draft.shipping_options ?? []
  const chosen = options.find((option) => option.id === optionId) ?? null
  const ships = chosen?.ships ?? false

  const field = (part: keyof typeof EMPTY_ADDRESS) => ({
    id: `${id}-${part}`,
    className: 'sf-input',
    value: address[part],
    onChange: (event: { target: { value: string } }) => setAddress((a) => ({ ...a, [part]: event.target.value })),
  })

  const problem = (): string | null => {
    if (!name.trim()) return 'Tell us your name.'
    if (!email.trim()) return 'Give your email address: we send the link to your order there.'
    if (options.length > 0 && chosen === null) return 'Choose how your order reaches you.'
    if (ships && (!address.recipient.trim() || !address.street.trim() || !address.locality.trim()))
      return 'Give the address to ship to.'
    if (ships && address.country.trim() && !/^[A-Za-z]{2}$/.test(address.country.trim()))
      return 'Give the country as two letters, such as US.'
    return null
  }

  const input = (): SubmitInput => {
    const sent: SubmitInput = { draft_id: draft.id, name: name.trim(), email: email.trim() }
    if (chosen) sent.shipping_option_id = chosen.id
    if (ships) {
      const optional = {
        locality: address.locality.trim(),
        region: address.region.trim(),
        postal_code: address.postalCode.trim(),
        country: address.country.trim().toUpperCase(),
      }
      sent.address = {
        recipient: address.recipient.trim(),
        lines: [address.street, address.street2].map((line) => line.trim()).filter(Boolean),
        ...Object.fromEntries(Object.entries(optional).filter(([, value]) => value)),
      }
    }
    return sent
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (sending || needsCheck) return
    const wrong = problem()
    if (wrong) {
      setError(wrong)
      return
    }
    const body = input()
    setSending(true)
    setError(null)
    try {
      const order = await store.submit(body, key.current.for(JSON.stringify(body)), token)
      key.current.done()
      onSubmitted(order)
    } catch (caught) {
      if (caught instanceof StoreError && caught.slug === 'bot_check_failed') forgetTurnstileSiteKey()
      setError(errorText(caught))
    } finally {
      setSending(false)
      setWidget((n) => n + 1)
    }
  }

  return (
    <form className="sf-panel sf-form" onSubmit={submit} noValidate>
      <h2 className="sf-item-title">Send your order</h2>
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-name`}>
          Your name
        </label>
        <input
          id={`${id}-name`}
          className="sf-input"
          autoComplete="name"
          maxLength={200}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-email`}>
          Email
        </label>
        <input
          id={`${id}-email`}
          className="sf-input"
          type="email"
          autoComplete="email"
          maxLength={254}
          aria-describedby={`${id}-email-hint`}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <p id={`${id}-email-hint`} className="sf-hint">
          We send the link to your order here.
        </p>
      </div>
      {options.length > 0 && (
        <fieldset className="sf-field">
          <legend className="sf-label">How should your order reach you?</legend>
          <RadioGroup name={`${id}-shipping`} value={optionId ?? ''} onValueChange={setOptionId}>
            {options.map((option) => (
              <label key={option.id} className="sf-choice">
                <RadioGroupItem value={option.id} />
                {` ${option.label} — ${formatMoney(option.amount_minor, draft.currency, draft.minor_exponent)}`}
              </label>
            ))}
          </RadioGroup>
        </fieldset>
      )}
      {ships && (
        <fieldset className="sf-field">
          <legend className="sf-label">Where should we ship it?</legend>
          <label className="sf-label" htmlFor={`${id}-recipient`}>
            Recipient
          </label>
          <input {...field('recipient')} autoComplete="shipping name" maxLength={200} />
          <label className="sf-label" htmlFor={`${id}-street`}>
            Street address
          </label>
          <input {...field('street')} autoComplete="shipping address-line1" maxLength={200} />
          <label className="sf-label" htmlFor={`${id}-street2`}>
            Address line 2 (optional)
          </label>
          <input {...field('street2')} autoComplete="shipping address-line2" maxLength={200} />
          <label className="sf-label" htmlFor={`${id}-locality`}>
            Town or city
          </label>
          <input {...field('locality')} autoComplete="shipping address-level2" maxLength={200} />
          <label className="sf-label" htmlFor={`${id}-region`}>
            State or region (optional)
          </label>
          <input {...field('region')} autoComplete="shipping address-level1" maxLength={200} />
          <label className="sf-label" htmlFor={`${id}-postalCode`}>
            Postal code
          </label>
          <input {...field('postalCode')} autoComplete="shipping postal-code" maxLength={40} />
          <label className="sf-label" htmlFor={`${id}-country`}>
            Country code (optional, such as US)
          </label>
          <input {...field('country')} autoComplete="shipping country" maxLength={2} />
        </fieldset>
      )}
      {siteKey && <Turnstile siteKey={siteKey} action="submit" onToken={setToken} resetKey={widget} />}
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="sf-button" disabled={sending || needsCheck}>
        {sending ? 'Sending…' : 'Send my order'}
      </button>
    </form>
  )
}
