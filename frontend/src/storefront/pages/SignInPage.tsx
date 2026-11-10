import { useEffect, useId, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { StoreError, errorText, store } from '../api/client'
import { Turnstile } from '../components/Turnstile'
import { forgetTurnstileSiteKey, useTurnstileSiteKey } from '../turnstile'

/** The code a mailed link carries: `/account/sign-in#code=<code>`. */
function linkedCode(hash: string): string {
  return new URLSearchParams(hash.replace(/^#/, '')).get('code') ?? ''
}

/** What the shop said, with the wait it asked for after too many tries. */
function failure(caught: unknown): string {
  if (caught instanceof StoreError && caught.slug === 'bot_check_failed') forgetTurnstileSiteKey()
  const wait = caught instanceof StoreError && caught.status === 429 && caught.retryAfter !== null
  return wait ? `${errorText(caught)} Try again in ${(caught as StoreError).retryAfter} seconds.` : errorText(caught)
}

type Step = 'ask' | 'code'

/**
 * Sign in (spec §5.2) in two steps: the address, to which the shop mails a code (the bot
 * check `sign_up`), then the address and that code (`sign_in`). The shop answers the
 * first step alike for every address, so the page says the same whatever the address. A
 * mailed link opens on the second step with the code filled from the address's `#code=`,
 * and the page then drops it from the address, so the code is not kept in the history. A
 * code is used once: the edge sends it once, so a send is never retried with the same code.
 */
export function SignInPage() {
  const id = useId()
  const location = useLocation()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [code, setCode] = useState(() => linkedCode(location.hash))
  const [step, setStep] = useState<Step>(() => (linkedCode(location.hash) ? 'code' : 'ask'))
  const [mailedTo, setMailedTo] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [widget, setWidget] = useState(0)
  const siteKey = useTurnstileSiteKey(widget)
  const needsCheck = siteKey !== null && token === null

  useEffect(() => {
    if (location.hash) navigate(location.pathname, { replace: true })
  }, [location.hash, location.pathname, navigate])

  const goTo = (next: Step) => {
    setStep(next)
    setError(null)
    setToken(null)
    setWidget((n) => n + 1)
  }

  const send = async (work: () => Promise<void>) => {
    setSending(true)
    setError(null)
    try {
      await work()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setSending(false)
      // A token passes one check only.
      setToken(null)
      setWidget((n) => n + 1)
    }
  }

  const askCode = (event: FormEvent) => {
    event.preventDefault()
    if (needsCheck) return
    const address = email.trim()
    if (!address) {
      setError('Enter your email address.')
      return
    }
    void send(async () => {
      await store.requestCode(address, token)
      setEmail(address)
      setCode('')
      setMailedTo(address)
      setStep('code')
    })
  }

  const signIn = (event: FormEvent) => {
    event.preventDefault()
    if (needsCheck) return
    if (!email.trim() || !code.trim()) {
      setError('Enter your email address and the code we sent you.')
      return
    }
    void send(async () => {
      await store.signIn(email.trim(), code.trim(), token)
      navigate('/account')
    })
  }

  const emailField = (
    <div className="sf-field">
      <label className="sf-label" htmlFor={`${id}-email`}>
        Email
      </label>
      <input
        id={`${id}-email`}
        className="sf-input"
        type="email"
        autoComplete="email"
        maxLength={320}
        value={email}
        onChange={(event) => setEmail(event.target.value)}
      />
    </div>
  )
  const check = siteKey && (
    <Turnstile key={step} siteKey={siteKey} action={step === 'ask' ? 'sign_up' : 'sign_in'} onToken={setToken} resetKey={widget} />
  )
  const alert = error && (
    <p className="sf-error" role="alert">
      {error}
    </p>
  )

  if (step === 'ask') {
    return (
      <section>
        <h1 className="sf-h1">Sign in</h1>
        <p className="sf-lead">Enter your email address. We will email you a code to sign in with.</p>
        <form className="sf-panel sf-form" onSubmit={askCode} noValidate>
          {emailField}
          {check}
          {alert}
          <button type="submit" className="sf-button" disabled={sending || needsCheck}>
            {sending ? 'Sending…' : 'Send me a code'}
          </button>
          <button type="button" className="sf-link" onClick={() => goTo('code')}>
            I have a code
          </button>
        </form>
      </section>
    )
  }

  return (
    <section>
      <h1 className="sf-h1">Sign in</h1>
      {mailedTo ? (
        <p className="sf-lead" role="status">
          {`We emailed a code to ${mailedTo}. It can take a few minutes to arrive.`}
        </p>
      ) : (
        <p className="sf-lead">Enter your email address and the code we emailed you.</p>
      )}
      <form className="sf-panel sf-form" onSubmit={signIn} noValidate>
        {emailField}
        <div className="sf-field">
          <label className="sf-label" htmlFor={`${id}-code`}>
            Code
          </label>
          <input
            id={`${id}-code`}
            className="sf-input"
            autoComplete="one-time-code"
            autoCapitalize="characters"
            spellCheck={false}
            maxLength={32}
            placeholder="XXXX-XXXX-XXXX-XXXX"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
        </div>
        {check}
        {alert}
        <button type="submit" className="sf-button" disabled={sending || needsCheck}>
          {sending ? 'Signing in…' : 'Sign in'}
        </button>
        <button type="button" className="sf-link" onClick={() => goTo('ask')}>
          Send me a new code
        </button>
      </form>
    </section>
  )
}
