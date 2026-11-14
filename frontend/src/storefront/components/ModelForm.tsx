import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { Checkbox } from '../../components/ui/checkbox'
import { RadioGroup, RadioGroupItem } from '../../components/ui/radio-group'
import {
  ActionKey,
  StoreError,
  UploadError,
  errorText,
  putFile,
  store,
  type Filament,
  type PurchaseLink,
  type UploadSlot,
} from '../api/client'
import { useDraftChanged } from '../draftCount'
import {
  LINK_PROBLEM,
  MAX_COLOURS,
  MAX_LINK_CHARS,
  MAX_NOTE,
  MAX_PRINT_LINKS,
  MAX_QUANTITY,
  checkedLink,
  modelFilesProblem,
  sizeText,
} from '../models'
import { forgetTurnstileSiteKey, useTurnstileSiteKey } from '../turnstile'
import { purchaseLinksProblem, sentPurchaseLinks } from '../models'
import { PurchaseLinks } from './PurchaseLinks'
import { Turnstile } from './Turnstile'

/** One file of the form, and how far its upload went. */
interface Upload {
  file: File
  slot: UploadSlot | null
  /** 0 to 1, while it uploads or once it is up. */
  progress: number
  done: boolean
  error: string | null
}

function expired(slot: UploadSlot): boolean {
  return Date.parse(slot.expires_at) <= Date.now()
}

/**
 * "Print my own model" (#6133, spec 2026-11-13 §4.1, §4.2): any mix of STL and 3MF files and
 * links, one quantity, the colours, a note, and up to five things to buy. On "Add to my
 * order" the page asks the shop for an upload slot per file (one call, which takes the bot
 * check), `PUT`s each file straight to the store with a progress bar, and then sends the
 * request naming the slots. A failed upload can be tried again on the same URL until it
 * expires; an expired one gets a new slot at the next send. Each send keeps one
 * `Idempotency-Key` while its content stays the same, so a send again after a lost answer is
 * the same request to the shop.
 */
export function ModelForm() {
  const id = useId()
  const [uploads, setUploads] = useState<Upload[]>([])
  const [links, setLinks] = useState<string[]>([])
  const [linkText, setLinkText] = useState('')
  const [linkError, setLinkError] = useState<string | null>(null)
  const [quantity, setQuantity] = useState('1')
  const [shopChoice, setShopChoice] = useState(true)
  const [filaments, setFilaments] = useState<Filament[]>([])
  const [chosen, setChosen] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [buy, setBuy] = useState<PurchaseLink[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const slotKey = useRef(new ActionKey())
  const sendKey = useRef(new ActionKey())
  const draftChanged = useDraftChanged()
  const [token, setToken] = useState<string | null>(null)
  const [widget, setWidget] = useState(0)
  const siteKey = useTurnstileSiteKey(widget)
  const needsCheck = siteKey !== null && token === null

  useEffect(() => {
    let live = true
    store.modelColours().then(
      (answer) => live && setFilaments(answer.filaments),
      () => undefined,
    )
    return () => {
      live = false
    }
  }, [])

  const filesProblem = modelFilesProblem(uploads.map((u) => u.file))
  const count = Number(quantity)
  const quantityProblem =
    Number.isInteger(count) && count >= 1 && count <= MAX_QUANTITY ? null : `Ask for 1 to ${MAX_QUANTITY}.`
  const buyProblem = purchaseLinksProblem(buy)
  const coloursProblem = !shopChoice && chosen.length === 0 ? 'Pick a colour, or let the shop choose.' : null
  const blocked = filesProblem ?? quantityProblem ?? buyProblem ?? coloursProblem

  const change = (index: number, patch: Partial<Upload>) =>
    setUploads((now) => now.map((u, i) => (i === index ? { ...u, ...patch } : u)))

  const addLink = () => {
    const link = checkedLink(linkText)
    if (link === null) {
      setLinkError(LINK_PROBLEM)
      return
    }
    if (links.length >= MAX_PRINT_LINKS) {
      setLinkError(`Add at most ${MAX_PRINT_LINKS} links.`)
      return
    }
    setLinks([...links, link])
    setLinkText('')
    setLinkError(null)
  }

  /** One file's PUT on its slot's URL; true once it is up. */
  const upload = async (index: number, item: Upload, slot: UploadSlot): Promise<boolean> => {
    change(index, { error: null, progress: 0 })
    try {
      await putFile(slot, item.file, (progress) => change(index, { progress }))
      change(index, { done: true, progress: 1 })
      return true
    } catch (caught) {
      change(index, { error: caught instanceof UploadError ? caught.message : errorText(caught), progress: 0 })
      return false
    }
  }

  const retry = async (index: number) => {
    const item = uploads[index]
    if (!item?.slot) return
    if (expired(item.slot)) {
      // The URL can no longer start: the next send asks for a new slot.
      change(index, { slot: null, error: null, progress: 0 })
      return
    }
    await upload(index, item, item.slot)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (blocked || sending) return
    if (uploads.length === 0 && links.length === 0) {
      setError('Add a model file or a link to a model.')
      return
    }
    setSending(true)
    setError(null)
    let checked = false
    try {
      let current = uploads.map((u) => (u.slot && !u.done && expired(u.slot) ? { ...u, slot: null } : u))
      const missing = current.filter((u) => u.slot === null)
      if (missing.length > 0) {
        if (needsCheck) return
        const what = JSON.stringify(missing.map((u) => [u.file.name, u.file.size, u.file.lastModified]))
        checked = true
        const answer = await store.uploadSlots(
          missing.map((u) => u.file),
          slotKey.current.for(what),
          token,
        )
        slotKey.current.done()
        let next = 0
        current = current.map((u) => (u.slot === null ? { ...u, slot: answer.uploads[next++] ?? null } : u))
        setUploads(current)
      }
      const results = await Promise.all(
        current.map((u, index) => (u.done || u.slot === null ? Promise.resolve(u.done) : upload(index, u, u.slot))),
      )
      if (!results.every(Boolean)) {
        setError('A file did not upload. Try it again, then add your models.')
        return
      }
      if (current.length === 0 && needsCheck) return
      const body = {
        uploads: current.map((u) => u.slot!.id),
        links: [
          ...links.map((url) => ({ url, purpose: 'print' as const, note: '' })),
          ...sentPurchaseLinks(buy).map((link) => ({ ...link, purpose: 'purchase' as const })),
        ],
        quantity: count,
        colours: { shop_choice: shopChoice, filaments: shopChoice ? [] : chosen },
        note: note.trim(),
      }
      checked = checked || current.length === 0
      const draft = await store.addModels(body, sendKey.current.for(JSON.stringify(body)), current.length === 0 ? token : null)
      sendKey.current.done()
      draftChanged(draft)
      setSent(true)
      setUploads([])
      setLinks([])
      setNote('')
      setBuy([])
      setQuantity('1')
    } catch (caught) {
      if (caught instanceof StoreError && caught.slug === 'bot_check_failed') forgetTurnstileSiteKey()
      const wait = caught instanceof StoreError && caught.status === 429 && caught.retryAfter !== null
      setError(wait ? `${errorText(caught)} Try again in ${(caught as StoreError).retryAfter} seconds.` : errorText(caught))
    } finally {
      setSending(false)
      // A token passes one check only: after a send that used it, draw a new widget.
      if (checked) {
        setToken(null)
        setWidget((n) => n + 1)
      }
    }
  }

  if (sent) {
    return (
      <div className="sf-panel" role="status">
        <p className="sf-lead">Your models are in your order.</p>
        <p className="sf-hint">We check each file now. Your order shows when a file is ready.</p>
        <p className="sf-actions">
          <Link className="sf-button" to="/draft">
            See your order
          </Link>
          <button type="button" className="sf-link" onClick={() => setSent(false)}>
            Add more models
          </button>
        </p>
      </div>
    )
  }

  return (
    <form className="sf-panel sf-form" onSubmit={submit} noValidate>
      <fieldset className="sf-field">
        <legend className="sf-h2">Your models</legend>
        <label className="sf-label" htmlFor={`${id}-files`}>
          Model files
        </label>
        <input
          id={`${id}-files`}
          className="sf-file"
          type="file"
          multiple
          accept=".stl,.3mf"
          aria-describedby={`${id}-files-hint`}
          disabled={sending}
          onChange={(event) => {
            const picked = Array.from(event.target.files ?? [])
            setUploads((now) => [...now, ...picked.map((file) => ({ file, slot: null, progress: 0, done: false, error: null }))])
            event.target.value = ''
          }}
        />
        <p id={`${id}-files-hint`} className={filesProblem ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
          {filesProblem ?? 'STL or 3MF, up to 20 files, 256 MB each.'}
        </p>
        {uploads.length > 0 && (
          <ul className="sf-uploads">
            {uploads.map((item, index) => (
              <li key={`${item.file.name}-${item.file.size}-${item.file.lastModified}-${index}`} className="sf-upload">
                <div className="sf-upload-head">
                  <span className="sf-upload-name">{item.file.name}</span>
                  <span className="sf-item-qty">{sizeText(item.file.size)}</span>
                </div>
                {item.slot && !item.error && (
                  <div
                    className="sf-progress"
                    role="progressbar"
                    aria-label={`Upload of ${item.file.name}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(item.progress * 100)}
                  >
                    <span className="sf-progress-fill" style={{ width: `${Math.round(item.progress * 100)}%` }} />
                  </div>
                )}
                {item.done && <p className="sf-hint">Uploaded</p>}
                {item.error && (
                  <p className="sf-hint sf-hint-bad">
                    {item.error}{' '}
                    <button
                      type="button"
                      className="sf-link"
                      aria-label={`Try ${item.file.name} again`}
                      disabled={sending}
                      onClick={() => void retry(index)}
                    >
                      Try again
                    </button>
                  </p>
                )}
                {!item.slot && !sending && (
                  <button
                    type="button"
                    className="sf-link"
                    aria-label={`Remove ${item.file.name}`}
                    onClick={() => setUploads((now) => now.filter((_, i) => i !== index))}
                  >
                    Remove
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        <label className="sf-label" htmlFor={`${id}-link`}>
          Link to a model
        </label>
        <div className="sf-inline">
          <input
            id={`${id}-link`}
            className="sf-input"
            type="url"
            inputMode="url"
            maxLength={MAX_LINK_CHARS}
            placeholder="https://makerworld.com/…"
            value={linkText}
            aria-invalid={linkError !== null}
            onChange={(event) => setLinkText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                addLink()
              }
            }}
          />
          <button type="button" className="sf-button sf-button-quiet" onClick={addLink}>
            Add link
          </button>
        </div>
        <p className={linkError ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
          {linkError ?? 'MakerWorld, Printables, Thingiverse or any other page with the model.'}
        </p>
        {links.length > 0 && (
          <ul className="sf-uploads">
            {links.map((link, index) => (
              <li key={`${link}-${index}`} className="sf-upload">
                <span className="sf-upload-name">{link}</span>
                <button
                  type="button"
                  className="sf-link"
                  aria-label={`Remove link ${index + 1}`}
                  onClick={() => setLinks(links.filter((_, i) => i !== index))}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </fieldset>
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-qty`}>
          How many
        </label>
        <input
          id={`${id}-qty`}
          className="sf-input sf-input-short"
          type="number"
          inputMode="numeric"
          min={1}
          max={MAX_QUANTITY}
          value={quantity}
          aria-invalid={quantityProblem !== null}
          onChange={(event) => setQuantity(event.target.value)}
        />
        <p className={quantityProblem ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
          {quantityProblem ?? 'Of each model. Say in the note if you need different numbers.'}
        </p>
      </div>
      <fieldset className="sf-field">
        <legend className="sf-label">Colours</legend>
        <RadioGroup
          name={`${id}-colours`}
          value={shopChoice ? 'shop' : 'choose'}
          onValueChange={(value) => setShopChoice(value === 'shop')}
        >
          <label className="sf-choice">
            <RadioGroupItem value="shop" />
            Let the shop choose
          </label>
          <label className="sf-choice">
            <RadioGroupItem value="choose" disabled={filaments.length === 0} />
            Choose colours
          </label>
        </RadioGroup>
        {!shopChoice && (
          <>
            <p className={coloursProblem ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
              {coloursProblem ?? `Pick up to ${MAX_COLOURS}.`}
            </p>
            <ul className="sf-colours">
              {filaments.map((filament) => {
                const on = chosen.includes(filament.id)
                const name = `${filament.colour_name ?? filament.colour} ${filament.material}`
                return (
                  <li key={filament.id}>
                    <label className="sf-choice">
                      <Checkbox
                        checked={on}
                        disabled={!on && chosen.length >= MAX_COLOURS}
                        onCheckedChange={() =>
                          setChosen(on ? chosen.filter((c) => c !== filament.id) : [...chosen, filament.id])
                        }
                        aria-label={name}
                      />
                      <span className="sf-swatch" style={{ background: filament.colour }} aria-hidden="true" />
                      {name}
                    </label>
                  </li>
                )
              })}
            </ul>
          </>
        )}
      </fieldset>
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-note`}>
          Anything else
        </label>
        <textarea
          id={`${id}-note`}
          className="sf-input sf-textarea"
          maxLength={MAX_NOTE}
          rows={4}
          placeholder="Sizes, finish, which parts go together."
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </div>
      <PurchaseLinks links={buy} onChange={setBuy} />
      {siteKey && <Turnstile siteKey={siteKey} action="model" onToken={setToken} resetKey={widget} />}
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="sf-button" disabled={sending || blocked !== null || needsCheck}>
        {sending ? 'Sending…' : 'Add to my order'}
      </button>
    </form>
  )
}
