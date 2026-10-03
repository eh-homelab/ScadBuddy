import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { recheckAiAvailability, useAiAvailability } from '../../agent/chat/availability'
import { USER_ONLY } from '../../agent/dom'
import type {
  AiConnectionTest,
  AiCredentialEntry,
  AiCredentialKind,
  AiCredentialList,
} from '../../api/aiCredential'
import { AI_NOT_ROUTED, api, ApiError } from '../../api/client'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

const KIND_LABEL: Record<AiCredentialKind, string> = {
  anthropic_api_key: 'Anthropic API key',
  gateway: 'Gateway',
}

const KIND_OPTION: Record<AiCredentialKind, string> = {
  anthropic_api_key: 'Anthropic API',
  gateway: 'Gateway (base URL and token)',
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

/** The time, with the date when it is not today (a provider's limit can last days). */
function clock(iso: string, now = new Date()): string {
  const at = new Date(iso)
  return at.toDateString() === now.toDateString()
    ? at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : at.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

/** How a credential is named in messages: "Anthropic API key ••••abcd", no secret. */
function nameOf(entry: AiCredentialEntry): string {
  return `${KIND_LABEL[entry.kind]}${entry.last4 ? ` ••••${entry.last4}` : ''}`
}

type Busy = { id: string; action: 'add' | 'up' | 'down' | 'test' | 'reset' | 'replace' | 'delete' } | null

/** How long after a cooldown ends the list is read again, so the agent sees it ended too. */
const REFRESH_SLACK_MS = 500
/** setTimeout's ceiling (about 24.8 days); a later cooldown is read again then. */
const MAX_TIMER_MS = 2 ** 31 - 1
/** The back-off for a cooldown the agent still reports after it ended by this clock. */
const OVERDUE_FIRST_MS = 1000
const OVERDUE_MAX_MS = 30_000

/** What deleting `entry` does to the assistant, for the confirmation. */
function afterDelete(entry: AiCredentialEntry, credentials: AiCredentialEntry[]): string {
  const live = (c: AiCredentialEntry) => c.usable && c.status === 'active'
  const inUse = credentials.find(live)
  if (inUse && inUse.id !== entry.id) return `The assistant keeps using ${nameOf(inUse)}.`
  const next = credentials.find((c) => c.id !== entry.id && live(c))
  if (next) return `The assistant falls back to ${nameOf(next)}.`
  return 'No other credential is usable now, so the assistant stops working until one is.'
}

/**
 * #1000, #1093 — Settings → "Claude credentials": the agent service's credentials in the
 * order it tries them (`/api/v1/ai/credentials/entries`, agent `routes/credentials.ts`). A
 * query uses the first usable one and falls back to the next when a credential is refused
 * (disabled until reset or given a new key) or rate limited (usable again on its own at
 * `cooldown_until`). Reads give the kind, the gateway's base URL, the last four characters
 * and the status; the secret is never returned, so every key field starts empty and a save
 * sends what was typed, once.
 *
 * Shown whenever the agent answers the read, not only when the assistant is available: no
 * usable credential is the most common reason it is not. Hidden when the agent or its
 * database is not there, like the other agent settings. Every action is user-only: a
 * credential write is outward (AI design spec §8.1), and the agent guards it to the UI's
 * own origin over HTTPS.
 */
export function AiCredentialSection() {
  const list = useAsync(() => api.listAiCredentials(), [])
  const ai = useAiAvailability()
  const [kind, setKind] = useState<AiCredentialKind>('anthropic_api_key')
  const [baseUrl, setBaseUrl] = useState('')
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState<Busy>(null)
  const [replacing, setReplacing] = useState<string | null>(null)
  const [replacement, setReplacement] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<AiCredentialEntry | null>(null)
  const [tests, setTests] = useState<Record<string, AiConnectionTest>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // A read that failed is tried again when the agent's state changes (it may have just come up).
  const { error: loadError, reload } = list
  const seenState = useRef(ai.state)
  useEffect(() => {
    const before = seenState.current
    if (before === ai.state) return
    seenState.current = ai.state
    // Leaving `checking` is the first answer for this tab, not a change in the agent.
    if (loadError && before !== 'checking') reload()
  }, [ai.state, loadError, reload])

  // The agent works a cooldown out at read time, so read again just after the earliest one ends.
  // This browser's clock may run ahead of the database's, which decides: a cooldown that has
  // passed here but still comes back is read again after 1 s, 2 s, 4 s… up to 30 s.
  const { data: listed, setData } = list
  const overdue = useRef({ until: Number.NaN, tries: 0 })
  // Bumped when a timed re-read fails, so the effect schedules the next one.
  const [missed, setMissed] = useState(0)
  useEffect(() => {
    const ends = (listed?.credentials ?? [])
      .filter((c) => c.status === 'cooling_down' && c.cooldown_until)
      .map((c) => Date.parse(c.cooldown_until!))
      .filter((t) => Number.isFinite(t))
    if (ends.length === 0) return
    const earliest = Math.min(...ends)
    // `missed` counts as overdue: the read failed, so back off whatever the clock says.
    const left = earliest - Date.now()
    let wait: number
    if (left > 0 && missed === 0) {
      overdue.current = { until: Number.NaN, tries: 0 }
      wait = Math.min(left + REFRESH_SLACK_MS, MAX_TIMER_MS)
    } else {
      const tries = overdue.current.until === earliest ? overdue.current.tries + 1 : 0
      overdue.current = { until: earliest, tries }
      wait = Math.min(OVERDUE_FIRST_MS * 2 ** tries, OVERDUE_MAX_MS)
    }
    const timer = setTimeout(() => {
      api.listAiCredentials().then(
        (next) => {
          setMissed(0)
          setData(next)
        },
        () => setMissed((n) => n + 1),
      )
    }, wait)
    return () => clearTimeout(timer)
  }, [listed, setData, missed])

  if (notDeployed(list.error)) return null
  const current = list.data
  if (!current) {
    return (
      <Frame>
        {list.error ? (
          <div role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-warn">
            {describeError(list.error, list.error.message)}
            <Button size="sm" onClick={list.reload}>
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
  const credentials = current.credentials
  const adding = busy?.action === 'add'
  const canAdd =
    busy === null && current.can_save && secret.trim() !== '' && (kind !== 'gateway' || baseUrl.trim() !== '')

  /** Runs one action, then shows the list as the agent now has it; true when it worked. */
  async function act<T extends AiCredentialList | AiCredentialEntry>(
    next: Busy,
    run: () => Promise<T>,
    done: string | null | ((answer: T) => string),
    fallback: string,
  ): Promise<{ ok: true } | { ok: false; error: unknown }> {
    setBusy(next)
    setError(null)
    setNotice(null)
    try {
      const answer = await run()
      if ('credentials' in answer) list.setData(answer)
      else list.refresh()
      if (next && (next.action === 'replace' || next.action === 'reset' || next.action === 'delete')) forgetTest(next.id)
      if (done) setNotice(typeof done === 'function' ? done(answer) : done)
      void recheckAiAvailability({ force: true })
      return { ok: true }
    } catch (caught) {
      setError(describeError(caught, fallback))
      refreshIfStale(caught)
      return { ok: false, error: caught }
    } finally {
      setBusy(null)
    }
  }

  /** A stale order, a credential deleted elsewhere, or a full list: show what the agent has now. */
  function refreshIfStale(caught: unknown) {
    if (caught instanceof ApiError && (caught.status === 409 || caught.status === 404)) list.refresh()
  }

  function forgetTest(id: string) {
    setTests(({ [id]: _, ...rest }) => rest)
  }

  async function add(event: FormEvent) {
    event.preventDefault()
    setBusy({ id: '', action: 'add' })
    setError(null)
    setNotice(null)
    try {
      await api.createAiCredential({
        kind,
        ...(kind === 'gateway' ? { base_url: baseUrl.trim() } : {}),
        secret: secret.trim(),
      })
      setSecret('')
      setBaseUrl('')
      setNotice(
        credentials.length === 0 ? 'Saved. Use Test to check it works.' : 'Added last; it is tried after the others.',
      )
      list.refresh()
      void recheckAiAvailability({ force: true })
    } catch (caught) {
      setError(describeError(caught, 'Could not save the credential'))
      refreshIfStale(caught)
    } finally {
      setBusy(null)
    }
  }

  function move(index: number, by: -1 | 1) {
    const ids = credentials.map((entry) => entry.id)
    const [moved] = ids.splice(index, 1)
    ids.splice(index + by, 0, moved!)
    void act(
      { id: moved!, action: by < 0 ? 'up' : 'down' },
      () => api.reorderAiCredentials(ids),
      null,
      'Could not change the order',
    )
  }

  async function test(entry: AiCredentialEntry) {
    setBusy({ id: entry.id, action: 'test' })
    setError(null)
    setNotice(null)
    forgetTest(entry.id)
    try {
      const result = await api.testAiCredential(entry.id)
      setTests((all) => ({ ...all, [entry.id]: result }))
    } catch (caught) {
      const wait = retryAfter(caught)
      setError(
        wait === undefined
          ? describeError(caught, 'Could not test the credential')
          : `${describeError(caught, 'A test ran moments ago')} (wait ${wait} s).`,
      )
      refreshIfStale(caught)
    } finally {
      setBusy(null)
    }
  }

  async function replace(event: FormEvent, entry: AiCredentialEntry) {
    event.preventDefault()
    const position = credentials.findIndex((c) => c.id === entry.id) + 1
    const saved = await act(
      { id: entry.id, action: 'replace' },
      () => api.saveAiCredential(entry.id, { kind: entry.kind, base_url: entry.base_url, secret: replacement.trim() }),
      (answer) => `Saved a new key for credential ${position} (${nameOf(answer)}).`,
      'Could not save the key',
    )
    if (saved.ok) {
      setReplacing(null)
      setReplacement('')
    }
  }

  async function remove(entry: AiCredentialEntry) {
    const deleted = await act(
      { id: entry.id, action: 'delete' },
      () => api.deleteAiCredential(entry.id),
      credentials.length === 1
        ? 'Deleted. The assistant is off until a credential is saved.'
        : `Deleted ${nameOf(entry)}.`,
      'Could not delete the credential',
    )
    // Gone already (deleted elsewhere): what the user asked for has happened, and the list is re-read.
    const gone = !deleted.ok && deleted.error instanceof ApiError && deleted.error.status === 404
    if (deleted.ok || gone) setConfirmDelete(null)
  }

  const isBusy = (id: string, action?: NonNullable<Busy>['action']) =>
    busy !== null && busy.id === id && (action === undefined || busy.action === action)

  return (
    <Frame>
      {credentials.length === 0 ? (
        <p className="text-warn">No credential saved. The assistant stays off until one is.</p>
      ) : (
        <>
          {!current.usable_now && (
            <p role="status" data-testid="ai-credentials-none-usable" className="text-warn">
              {current.recovers_at
                ? `No credential is usable now. The first rate-limited one is usable again at ${clock(current.recovers_at)}.`
                : 'No credential is usable now: each one needs a reset or a new key.'}
            </p>
          )}
          <ol className="flex flex-col divide-y divide-line rounded-[6px] border border-line" aria-label="Claude credentials, tried in this order">
            {credentials.map((entry, index) => (
              <li key={entry.id} className="flex flex-col gap-2 px-3 py-2.5" data-testid="ai-credential">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="text-muted">{index + 1}.</span>
                  <span className="font-medium">{KIND_LABEL[entry.kind]}</span>
                  {entry.base_url && <span className="text-muted">at {entry.base_url}</span>}
                  {entry.last4 && (
                    <span className="font-mono text-muted" aria-label={`ending in ${entry.last4}`}>
                      ••••{entry.last4}
                    </span>
                  )}
                  <StatusBadge entry={entry} />
                  {entry.last_used_at && (
                    <span className="text-[12px] text-muted" title={entry.last_used_at}>
                      used {timeAgo(entry.last_used_at)}
                    </span>
                  )}
                </div>
                {entry.status !== 'active' && entry.last_error && (
                  <p className="text-[12px] text-muted">{entry.last_error}</p>
                )}
                {!entry.usable && (
                  <p className="text-[12px] text-warn">
                    The agent cannot decrypt this key (it was saved under another encryption key, or in an old
                    format). Replace it.
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-1.5">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => move(index, -1)}
                    disabled={index === 0 || busy !== null}
                    aria-label={`Move ${nameOf(entry)} up`}
                  >
                    {isBusy(entry.id, 'up') ? <Spinner /> : 'Up'}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => move(index, 1)}
                    disabled={index === credentials.length - 1 || busy !== null}
                    aria-label={`Move ${nameOf(entry)} down`}
                  >
                    {isBusy(entry.id, 'down') ? <Spinner /> : 'Down'}
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => void test(entry)}
                    disabled={busy !== null || !entry.usable}
                    aria-busy={isBusy(entry.id, 'test')}
                    aria-label={`Test ${nameOf(entry)}`}
                  >
                    {isBusy(entry.id, 'test') && <Spinner />}
                    Test
                  </Button>
                  {entry.status !== 'active' && entry.usable && (
                    <Button
                      size="sm"
                      onClick={() =>
                        void act(
                          { id: entry.id, action: 'reset' },
                          () => api.resetAiCredential(entry.id),
                          `${nameOf(entry)} is active again.`,
                          'Could not reset the credential',
                        )
                      }
                      disabled={busy !== null}
                      aria-label={`Reset ${nameOf(entry)}`}
                    >
                      {isBusy(entry.id, 'reset') && <Spinner />}
                      Reset
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setReplacing(replacing === entry.id ? null : entry.id)
                      setReplacement('')
                    }}
                    disabled={busy !== null || !current.can_save}
                    aria-expanded={replacing === entry.id}
                    aria-label={`Replace the key of ${nameOf(entry)}`}
                  >
                    Replace key
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => setConfirmDelete(entry)}
                    disabled={busy !== null}
                    aria-label={`Delete ${nameOf(entry)}`}
                  >
                    Delete
                  </Button>
                </div>
                {replacing === entry.id && (
                  <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => void replace(event, entry)}>
                    <label className="flex flex-col gap-1">
                      {entry.kind === 'gateway' ? 'New gateway token' : 'New API key'}
                      <input
                        type="password"
                        required
                        value={replacement}
                        onChange={(event) => setReplacement(event.target.value)}
                        className="sb-field w-72"
                        autoComplete="new-password"
                        spellCheck={false}
                      />
                    </label>
                    <Button
                      type="submit"
                      variant="primary"
                      size="sm"
                      disabled={replacement.trim() === '' || busy !== null}
                      aria-busy={isBusy(entry.id, 'replace')}
                    >
                      {isBusy(entry.id, 'replace') && <Spinner />}
                      Save key
                    </Button>
                  </form>
                )}
                {tests[entry.id] && (
                  <p
                    role="status"
                    data-testid="ai-credential-test"
                    className={`text-[12px] ${tests[entry.id]!.ok ? 'text-ok' : 'text-warn'}`}
                  >
                    {tests[entry.id]!.ok
                      ? `Works${tests[entry.id]!.model ? ` (${tests[entry.id]!.model})` : ''}, ${(tests[entry.id]!.duration_ms / 1000).toFixed(1)} s.`
                      : `Failed: ${tests[entry.id]!.detail}`}
                  </p>
                )}
              </li>
            ))}
          </ol>
          <p className="text-[12px] text-muted">
            The assistant uses the first usable credential. A refused one is disabled until you reset it or give it a
            new key; a rate-limited one is skipped until its limit resets. Test sends one short prompt, so it spends a
            few tokens, at most once per 10 seconds.
          </p>
        </>
      )}
      {!current.can_save && (
        <p className="text-[12px] text-warn">
          Saving is not possible: {current.cannot_save_reason ?? 'the agent cannot store secrets'}.
        </p>
      )}

      <form className="flex flex-col gap-3" onSubmit={(event) => void add(event)}>
        <fieldset className="flex flex-wrap gap-4">
          <legend className="mb-1 text-[12px] text-muted">{credentials.length === 0 ? 'Save' : 'Add another'}</legend>
          {(Object.keys(KIND_OPTION) as AiCredentialKind[]).map((option) => (
            <label key={option} className="flex items-center gap-1.5">
              <input
                type="radio"
                name="ai-credential-kind"
                value={option}
                checked={kind === option}
                onChange={() => {
                  // A secret typed for one kind must not be sent as another.
                  setKind(option)
                  setSecret('')
                  setBaseUrl('')
                }}
              />
              {KIND_OPTION[option]}
            </label>
          ))}
        </fieldset>
        {kind === 'gateway' && (
          <label className="flex flex-col gap-1">
            Base URL
            <input
              type="url"
              required
              placeholder="https://gateway.example/anthropic"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              className="sb-field max-w-md"
              autoComplete="off"
            />
          </label>
        )}
        <label className="flex flex-col gap-1">
          {kind === 'gateway' ? 'Gateway token' : 'Anthropic API key'}
          <input
            type="password"
            required
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder={kind === 'gateway' ? 'token' : 'sk-ant-…'}
            className="sb-field max-w-md"
            autoComplete="new-password"
            spellCheck={false}
            aria-describedby="ai-credential-help"
          />
        </label>
        <p id="ai-credential-help" className="text-[12px] text-muted">
          Sent once to ScadBuddy&rsquo;s agent service, which stores it encrypted. It is never shown again; only its
          last four characters are.
        </p>
        <div>
          <Button type="submit" variant="primary" size="sm" disabled={!canAdd} aria-busy={adding}>
            {adding && <Spinner />}
            {credentials.length === 0 ? 'Save' : 'Add'}
          </Button>
        </div>
      </form>

      {notice && (
        <p role="status" className="text-[12px] text-ok">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
      <Dialog
        open={confirmDelete !== null}
        title={`Delete ${confirmDelete ? nameOf(confirmDelete) : 'the credential'}?`}
        onClose={() => {
          if (busy?.action === 'delete') return
          setConfirmDelete(null)
          setError(null)
        }}
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setConfirmDelete(null)
                setError(null)
              }}
              disabled={busy !== null}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => confirmDelete && void remove(confirmDelete)}
              disabled={busy !== null}
              {...USER_ONLY}
            >
              {busy?.action === 'delete' ? <Spinner /> : 'Delete credential'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">{confirmDelete && afterDelete(confirmDelete, credentials)}</p>
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </Frame>
  )
}

function StatusBadge({ entry }: { entry: AiCredentialEntry }) {
  const base = 'rounded-[4px] border px-1 py-px text-[12px]'
  if (entry.status === 'cooling_down') {
    return (
      <span className={`${base} border-warn/50 text-warn`} title={entry.cooldown_until ?? undefined}>
        {entry.cooldown_until ? `Rate limited until ${clock(entry.cooldown_until)}` : 'Rate limited'}
      </span>
    )
  }
  if (entry.status === 'disabled') {
    return <span className={`${base} border-warn/50 text-warn`}>Disabled</span>
  }
  return <span className={`${base} border-line text-ok`}>Active</span>
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <section
      className="mt-4 rounded-[6px] border border-line bg-surface"
      aria-labelledby="ai-credential-heading"
      {...USER_ONLY}
    >
      <h2 id="ai-credential-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        Claude credentials
      </h2>
      <div className="flex flex-col gap-3 p-4 text-[13px]">{children}</div>
    </section>
  )
}
