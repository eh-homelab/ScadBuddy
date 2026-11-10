import { useEffect, useId, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { StoreError, errorText, store } from '../api/client'
import { Turnstile } from '../components/Turnstile'
import { forgetTurnstileSiteKey, useTurnstileSiteKey } from '../turnstile'

/** The code a mailed link carries: `/account/sign-in#code=<code>`. */
function linkedCode(hash: string): string {
  return new URLSearchParams(hash.replace(/^#/, '')).get('code') ?? ''
}

/**
 * Sign in (spec §5.2): the address and the code mailed to it. A mailed link fills the
 * code from the address's `#code=`, and the page then drops it from the address, so the
 * code is not kept in the history. The bot check (`sign_in`) runs while it is on. A code
 * is used once: the edge sends it once, so a send is never retried with the same code.
 */
export function SignInPage() {
  const id = useId()
  const location = useLocation()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [code, setCode] = useState(() => linkedCode(location.hash))
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [widget, setWidget] = useState(0)
  const siteKey = useTurnstileSiteKey(widget)
  const needsCheck = siteKey !== null && token === null

  useEffect(() => {
    if (location.hash) navigate(location.pathname, { replace: true })
  }, [location.hash, location.pathname, navigate])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (needsCheck) return
    if (!email.trim() || !code.trim()) {
      setError('Enter your email address and the code we sent you.')
      return
    }
    setSending(true)
    setError(null)
    try {
      await store.signIn(email.trim(), code.trim(), token)
      navigate('/account')
    } catch (caught) {
      if (caught instanceof StoreError && caught.slug === 'bot_check_failed') forgetTurnstileSiteKey()
      const wait = caught instanceof StoreError && caught.status === 429 && caught.retryAfter !== null
      setError(wait ? `${errorText(caught)} Try again in ${(caught as StoreError).retryAfter} seconds.` : errorText(caught))
    } finally {
      setSending(false)
      // A token passes one check only.
      setWidget((n) => n + 1)
    }
  }

  return (
    <section>
      <h1 className="sf-h1">Sign in</h1>
      <p className="sf-lead">Enter your email address and the code we emailed you.</p>
      <form className="sf-panel sf-form" onSubmit={submit} noValidate>
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
        {siteKey && <Turnstile siteKey={siteKey} action="sign_in" onToken={setToken} resetKey={widget} />}
        {error && (
          <p className="sf-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="sf-button" disabled={sending || needsCheck}>
          {sending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </section>
  )
}
