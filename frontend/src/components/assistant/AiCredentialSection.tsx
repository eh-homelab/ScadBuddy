import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { recheckAiAvailability, useAiAvailability } from '../../agent/chat/availability'
import { USER_ONLY } from '../../agent/dom'
import type { AiConnectionTest, AiCredentialKind, AiCredentialView } from '../../api/aiCredential'
import { AI_NOT_ROUTED, api, ApiError } from '../../api/client'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

const KIND_LABEL: Record<AiCredentialKind, string> = {
  anthropic_api_key: 'Anthropic API key',
  claude_oauth_token: 'Claude Code OAuth token',
  gateway: 'Gateway',
}

const KIND_OPTION: Record<AiCredentialKind, string> = {
  anthropic_api_key: 'Anthropic API',
  claude_oauth_token: 'Claude Code OAuth token',
  gateway: 'Gateway (base URL and token)',
}

/** The secret field's label and placeholder for each kind. */
const SECRET_FIELD: Record<AiCredentialKind, { label: string; placeholder: string }> = {
  anthropic_api_key: { label: 'Anthropic API key', placeholder: 'sk-ant-api03-…' },
  // What `claude setup-token` prints.
  claude_oauth_token: { label: 'OAuth token', placeholder: 'sk-ant-oat01-…' },
  gateway: { label: 'Gateway token', placeholder: 'token' },
}

function describeError(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

/**
 * The agent is not deployed here, so the section hides:
 * - nothing routes `/api/v1/ai/*` to it: the backend's SPA fallback answers the read with
 *   `index.html` (`AI_NOT_ROUTED`, as `availability.ts` NOT_ROUTED), or a proxy answers 404;
 * - it runs without its database (agent `routes/credentials.ts` NO_DATABASE_CODE).
 * Anything else, a malformed JSON body or the database still applying migrations, is shown
 * with a Retry.
 */
function notDeployed(cause: Error | undefined): boolean {
  if (!(cause instanceof ApiError)) return false
  if (cause.problem.type === AI_NOT_ROUTED || cause.status === 404) return true
  return cause.status === 503 && cause.problem.code === 'no_database'
}

/** The wait a 429 asked for, in seconds, when it gave one. */
function retryAfter(cause: unknown): number | undefined {
  if (!(cause instanceof ApiError) || cause.status !== 429) return undefined
  const seconds = cause.problem.retry_after
  return typeof seconds === 'number' ? seconds : undefined
}

/**
 * #1000 — Settings → "Claude credential": the agent service's one credential
 * (`/api/v1/ai/credentials`, agent `routes/credentials.ts`). Reads give the kind, the
 * gateway's base URL and the last four characters; the secret is never returned, so
 * the field below always starts empty and a save sends what was typed, once.
 *
 * Shown whenever the agent answers the read, not only when the assistant is
 * available: no credential is the most common reason it is not. Hidden when the agent
 * or its database is not there, like the other agent settings. Every action is
 * user-only: a credential write is outward (AI design spec §8.1), and the agent
 * guards it to the UI's own origin over HTTPS.
 */
export function AiCredentialSection() {
  const credential = useAsync(() => api.getAiCredential(), [])
  const ai = useAiAvailability()
  const [kind, setKind] = useState<AiCredentialKind | null>(null)
  const [baseUrl, setBaseUrl] = useState<string | null>(null)
  const [secret, setSecret] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [test, setTest] = useState<AiConnectionTest | null>(null)

  // A read that failed is tried again when the agent's state changes (it may have just come up).
  const { error: loadError, reload } = credential
  const seenState = useRef(ai.state)
  useEffect(() => {
    const before = seenState.current
    if (before === ai.state) return
    seenState.current = ai.state
    // Leaving `checking` is the first answer for this tab, not a change in the agent.
    if (loadError && before !== 'checking') reload()
  }, [ai.state, loadError, reload])

  if (notDeployed(credential.error)) return null
  const current = credential.data
  if (!current) {
    return (
      <Frame>
        {credential.error ? (
          <div role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-warn">
            {describeError(credential.error, credential.error.message)}
            <Button size="sm" onClick={credential.reload}>
              Retry
            </Button>
          </div>
        ) : (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading
          </p>
        )}
      </Frame>
    )
  }
  const kindValue = kind ?? current.kind ?? 'anthropic_api_key'
  const baseUrlValue = baseUrl ?? current.base_url ?? ''
  // Always with the secret: the agent keeps the stored one only for an unchanged kind and URL,
  // and that save would change nothing.
  const canSubmit =
    !saving && current.can_save && secret.trim() !== '' && (kindValue !== 'gateway' || baseUrlValue.trim() !== '')

  function applied(next: AiCredentialView, message: string) {
    credential.setData(next)
    setKind(null)
    setBaseUrl(null)
    setSecret('')
    setTest(null)
    setNotice(message)
    void recheckAiAvailability({ force: true })
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const next = await api.putAiCredential({
        kind: kindValue,
        ...(kindValue === 'gateway' ? { base_url: baseUrlValue.trim() } : {}),
        secret: secret.trim(),
      })
      applied(next, 'Saved. Use Test to check it works.')
    } catch (caught) {
      setError(describeError(caught, 'Could not save the credential'))
    } finally {
      setSaving(false)
    }
  }

  async function runTest() {
    setTesting(true)
    setError(null)
    setNotice(null)
    setTest(null)
    try {
      setTest(await api.testAiCredential())
    } catch (caught) {
      const wait = retryAfter(caught)
      setError(
        wait === undefined
          ? describeError(caught, 'Could not test the credential')
          : `${describeError(caught, 'A test ran moments ago')} (wait ${wait} s).`,
      )
    } finally {
      setTesting(false)
    }
  }

  async function remove() {
    setDeleting(true)
    setError(null)
    setNotice(null)
    try {
      const next = await api.deleteAiCredential()
      // Another credential (#1093) may have moved up into its place.
      applied(
        next,
        next.configured ? 'Deleted. The next credential is in use now.' : 'Deleted. The assistant is off until a credential is saved.',
      )
      setConfirmDelete(false)
    } catch (caught) {
      setConfirmDelete(false)
      setError(describeError(caught, 'Could not delete the credential'))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Frame>
      <div data-testid="ai-credential-current">
        {current.configured && current.kind ? (
          <p>
            {KIND_LABEL[current.kind]}
            {current.base_url && <span className="text-muted"> at {current.base_url}</span>}
            {current.last4 && (
              <span className="ml-2 font-mono text-muted" aria-label={`ending in ${current.last4}`}>
                ••••{current.last4}
              </span>
            )}
            {current.updated_at && (
              <span className="ml-2 text-[12px] text-muted" title={current.updated_at}>
                saved {timeAgo(current.updated_at)}
              </span>
            )}
          </p>
        ) : (
          <p className="text-warn">No credential saved. The assistant stays off until one is.</p>
        )}
        {current.configured && !current.usable && (
          <p className="mt-1 text-[12px] text-warn">
            The agent cannot decrypt the saved secret (it was saved under another key, or in an old format).
            Save it again.
          </p>
        )}
        {!current.can_save && (
          <p className="mt-1 text-[12px] text-warn">
            Saving is not possible: {current.cannot_save_reason ?? 'the agent cannot store secrets'}.
          </p>
        )}
      </div>

      <form className="flex flex-col gap-3" onSubmit={(event) => void save(event)}>
        <fieldset className="flex flex-wrap gap-4">
          <legend className="mb-1 text-[12px] text-muted">{current.configured ? 'Replace with' : 'Save'}</legend>
          {(Object.keys(KIND_OPTION) as AiCredentialKind[]).map((option) => (
            <label key={option} className="flex items-center gap-1.5">
              <input
                type="radio"
                name="ai-credential-kind"
                value={option}
                checked={kindValue === option}
                onChange={() => setKind(option)}
              />
              {KIND_OPTION[option]}
            </label>
          ))}
        </fieldset>
        {kindValue === 'gateway' && (
          <label className="flex flex-col gap-1">
            Base URL
            <input
              type="url"
              required
              placeholder="https://gateway.example/anthropic"
              value={baseUrlValue}
              onChange={(event) => setBaseUrl(event.target.value)}
              className="sb-field max-w-md"
              autoComplete="off"
            />
          </label>
        )}
        <label className="flex flex-col gap-1">
          {SECRET_FIELD[kindValue].label}
          <input
            type="password"
            required
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder={SECRET_FIELD[kindValue].placeholder}
            className="sb-field max-w-md"
            autoComplete="new-password"
            spellCheck={false}
            aria-describedby="ai-credential-help"
          />
        </label>
        <p id="ai-credential-help" className="text-[12px] text-muted">
          Sent once to ScadBuddy&rsquo;s agent service, which stores it encrypted. It is never shown again; only
          its last four characters are.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="primary" size="sm" disabled={!canSubmit} aria-busy={saving}>
            {saving && <Spinner />}
            Save
          </Button>
          {current.configured && (
            <>
              <Button
                size="sm"
                onClick={() => void runTest()}
                disabled={testing || !current.usable}
                aria-busy={testing}
                aria-describedby="ai-credential-test-help"
              >
                {testing && <Spinner />}
                Test
              </Button>
              <Button size="sm" variant="danger" onClick={() => setConfirmDelete(true)} disabled={deleting}>
                Delete
              </Button>
            </>
          )}
        </div>
        {current.configured && (
          <p id="ai-credential-test-help" className="text-[12px] text-muted">
            Test sends one short prompt with the saved credential, so it spends a few tokens. One test per 10
            seconds.
          </p>
        )}
      </form>

      {notice && (
        <p role="status" className="text-[12px] text-ok">
          {notice}
        </p>
      )}
      {test && (
        <p role="status" data-testid="ai-credential-test" className={`text-[12px] ${test.ok ? 'text-ok' : 'text-warn'}`}>
          {test.ok
            ? `Works${test.model ? ` (${test.model})` : ''}, ${(test.duration_ms / 1000).toFixed(1)} s.`
            : `Failed: ${test.detail}`}
        </p>
      )}
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
      <Dialog
        open={confirmDelete}
        title="Delete the Claude credential?"
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void remove()} disabled={deleting} {...USER_ONLY}>
              {deleting ? <Spinner /> : 'Delete credential'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">The assistant stops working until a new credential is saved.</p>
      </Dialog>
    </Frame>
  )
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <section
      className="mt-4 rounded-[6px] border border-line bg-surface"
      aria-labelledby="ai-credential-heading"
      {...USER_ONLY}
    >
      <h2 id="ai-credential-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        Claude credential
      </h2>
      <div className="flex flex-col gap-3 p-4 text-[13px]">{children}</div>
    </section>
  )
}
