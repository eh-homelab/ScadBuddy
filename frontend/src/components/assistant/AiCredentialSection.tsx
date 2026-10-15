import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
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
import { ArrowDownIcon, ArrowUpIcon, ChevronDownIcon } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '../ui/Spinner'
import { Tip } from './Tip'

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

/** The label of a stored credential's replacement secret. */
const REPLACE_LABEL: Record<AiCredentialKind, string> = {
  anthropic_api_key: 'New API key',
  claude_oauth_token: 'New OAuth token',
  gateway: 'New gateway token',
}

/** The secret field's label and placeholder for each kind. */
const SECRET_FIELD: Record<AiCredentialKind, { label: string; placeholder: string }> = {
  anthropic_api_key: { label: 'Anthropic API key', placeholder: 'sk-ant-api03-…' },
  // What `claude setup-token` prints.
  claude_oauth_token: { label: 'OAuth token', placeholder: 'sk-ant-oat01-…' },
  gateway: { label: 'Gateway token', placeholder: 'token' },
}

/** The Anthropic secret prefixes, and the kind each one is (the placeholders in `SECRET_FIELD`). */
const PREFIXES: readonly { prefix: string; kind: AiCredentialKind; called: string }[] = [
  { prefix: 'sk-ant-api03-', kind: 'anthropic_api_key', called: 'an Anthropic API key' },
  { prefix: 'sk-ant-oat01-', kind: 'claude_oauth_token', called: 'a Claude Code OAuth token' },
]

/** The other Anthropic kind the secret's prefix says it is, if any. A gateway token can look like anything. */
function looksLike(kind: AiCredentialKind, secret: string): (typeof PREFIXES)[number] | null {
  if (kind === 'gateway') return null
  const typed = secret.trim()
  const match = PREFIXES.find((p) => typed.startsWith(p.prefix))
  return match && match.kind !== kind ? match : null
}

/**
 * A warning, not a refusal, when the secret has the other Anthropic kind's prefix: saved
 * as the wrong kind it is sent in the wrong header, refused, and the credential disabled.
 */
function kindMismatch(kind: AiCredentialKind, secret: string): string | null {
  const other = looksLike(kind, secret)
  if (!other) return null
  return `This looks like ${other.called}. Choose “${KIND_OPTION[other.kind]}” to save it as one.`
}

/** The same warning for a replacement, which cannot change the credential's kind. */
function replaceMismatch(kind: AiCredentialKind, secret: string): string | null {
  const other = looksLike(kind, secret)
  if (!other) return null
  return `This looks like ${other.called}, and a replacement keeps the credential's kind. Add it as a new credential instead, then delete this one.`
}

/**
 * Why no credential is usable when none recovers by itself: a refused one needs a reset or a
 * new key, one the agent cannot decrypt (agent `opensWith`) needs its key saved again, and
 * with no key-encryption key mounted nothing can be decrypted or saved.
 */
function noneUsable(credentials: readonly AiCredentialEntry[], canSave: boolean): string {
  const locked = credentials.some((c) => !c.usable)
  const refused = credentials.some((c) => c.usable && c.status === 'disabled')
  if (locked && !canSave) {
    return 'No credential is usable now: the agent cannot decrypt the stored keys, or save new ones, until its key-encryption key is mounted.'
  }
  if (locked && refused) {
    return 'No credential is usable now: a refused one needs a reset or a new key, and one the agent cannot decrypt needs its key saved again.'
  }
  if (locked) return 'No credential is usable now: the agent cannot decrypt them, so each key needs saving again.'
  return 'No credential is usable now: each one needs a reset or a new key.'
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

/**
 * How a credential is named in labels and messages, no secret: "credential 2 (Anthropic API key
 * ••••abcd)". The position keeps two of one kind apart, even with no last four (a short secret).
 */
function nameOf(entry: AiCredentialEntry, list: readonly AiCredentialEntry[]): string {
  const at = list.findIndex((c) => c.id === entry.id)
  const what = `${KIND_LABEL[entry.kind]}${entry.last4 ? ` ••••${entry.last4}` : ''}`
  return at < 0 ? what : `credential ${at + 1} (${what})`
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
  // Named by position in the list as it will be once the delete lands.
  const left = credentials.filter((c) => c.id !== entry.id)
  const inUse = credentials.find(live)
  if (inUse && inUse.id !== entry.id) return `The assistant keeps using ${nameOf(inUse, left)}.`
  const next = left.find(live)
  if (next) return `The assistant falls back to ${nameOf(next, left)}.`
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
  /** The Delete that asked for the confirm: focus goes back to it when the dialog closes. */
  const deleteButton = useRef<HTMLElement | null>(null)
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
  const { data: listed, refresh } = list
  const overdue = useRef({ until: Number.NaN, tries: 0 })
  // Bumped when a re-read fails, so the effect schedules the next one.
  const [missed, setMissed] = useState(0)
  // The last re-read failed: what is on screen may be out of date.
  const [stale, setStale] = useState(false)
  // Every re-read goes through here with the same handlers. Whichever read turns out the
  // newest (the timer's, or an action's that overtook it) either changes `listed` or bumps
  // `missed`, so the timer below is always scheduled again. An action that answers with the
  // list sets it directly and clears `missed` (`act`).
  const reread = useCallback(
    () =>
      refresh(
        () => {
          setMissed(0)
          setStale(false)
          return true
        },
        () => {
          setMissed((n) => n + 1)
          setStale(true)
        },
      ),
    [refresh],
  )
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
    const timer = setTimeout(reread, wait)
    return () => clearTimeout(timer)
  }, [listed, reread, missed])

  if (notDeployed(list.error)) return null
  const current = list.data
  if (!current) {
    return (
      <Frame>
        {list.error ? (
          <div role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-warn">
            {describeError(list.error, list.error.message)}
            <Button size="sm" variant="outline" onClick={list.reload}>
              Retry
            </Button>
          </div>
        ) : (
          <div role="status" className="flex flex-col gap-2">
            <span className="sr-only">Loading</span>
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-8 w-2/3" />
          </div>
        )}
      </Frame>
    )
  }
  const credentials = current.credentials
  const adding = busy?.action === 'add'
  const canAdd =
    busy === null && current.can_save && secret.trim() !== '' && (kind !== 'gateway' || baseUrl.trim() !== '')

  /**
   * What every action shares: the one busy state, the last message cleared, and on failure the
   * error shown and a stale list read again.
   */
  async function attempt<T>(
    next: NonNullable<Busy>,
    work: () => Promise<T>,
    failure: (caught: unknown) => string,
  ): Promise<{ ok: true; value: T } | { ok: false; error: unknown }> {
    setBusy(next)
    setError(null)
    setNotice(null)
    try {
      return { ok: true, value: await work() }
    } catch (caught) {
      setError(failure(caught))
      refreshIfStale(caught)
      return { ok: false, error: caught }
    } finally {
      setBusy(null)
    }
  }

  /** Runs one row action, then shows the list as the agent now has it. */
  async function act<T extends AiCredentialList | AiCredentialEntry>(
    next: NonNullable<Busy>,
    run: () => Promise<T>,
    done: string | null | ((answer: T) => string),
    fallback: string,
  ): Promise<{ ok: true } | { ok: false; error: unknown }> {
    const result = await attempt(next, run, (caught) => rowError(caught, next.id, fallback))
    if (!result.ok) return result
    const answer = result.value
    if ('credentials' in answer) {
      list.setData(answer, { supersede: true })
      setStale(false)
      setMissed(0)
    } else reread()
    if (next.action === 'replace' || next.action === 'reset' || next.action === 'delete') forgetTest(next.id)
    if (done) setNotice(typeof done === 'function' ? done(answer) : done)
    void recheckAiAvailability({ force: true })
    return { ok: true }
  }

  /**
   * A row action's error. The agent's 404 names the credential by its id, which the page never
   * shows, so it is said with the credential's name instead.
   */
  function rowError(caught: unknown, id: string | undefined, fallback: string): string {
    const entry = credentials.find((c) => c.id === id)
    if (entry && caught instanceof ApiError && caught.status === 404) {
      return `${nameOf(entry, credentials)} was deleted elsewhere; the list has been read again.`
    }
    return describeError(caught, fallback)
  }

  /** A stale order, a credential deleted elsewhere, or a full list: show what the agent has now. */
  function refreshIfStale(caught: unknown) {
    if (caught instanceof ApiError && (caught.status === 409 || caught.status === 404)) reread()
  }

  function forgetTest(id: string) {
    setTests(({ [id]: _, ...rest }) => rest)
  }

  async function add(event: FormEvent) {
    event.preventDefault()
    const created = await attempt(
      { id: '', action: 'add' },
      () =>
        api.createAiCredential({
          kind,
          ...(kind === 'gateway' ? { base_url: baseUrl.trim() } : {}),
          secret: secret.trim(),
        }),
      // No row yet, so no name to give a 404.
      (caught) => describeError(caught, 'Could not save the credential'),
    )
    if (!created.ok) return
    setSecret('')
    setBaseUrl('')
    setNotice(
      credentials.length === 0 ? 'Saved. Use Test to check it works.' : 'Added last; it is tried after the others.',
    )
    reread()
    void recheckAiAvailability({ force: true })
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
    // Before the test, not after: an earlier verdict must not show beside the one running.
    forgetTest(entry.id)
    const tested = await attempt({ id: entry.id, action: 'test' }, () => api.testAiCredential(entry.id), (caught) => {
      const wait = retryAfter(caught)
      return wait === undefined
        ? rowError(caught, entry.id, 'Could not test the credential')
        : `${describeError(caught, 'A test ran moments ago')} (wait ${wait} s).`
    })
    if (tested.ok) setTests((all) => ({ ...all, [entry.id]: tested.value }))
  }

  async function replace(event: FormEvent, entry: AiCredentialEntry) {
    event.preventDefault()
    const saved = await act(
      { id: entry.id, action: 'replace' },
      () => api.saveAiCredential(entry.id, { kind: entry.kind, base_url: entry.base_url, secret: replacement.trim() }),
      (answer) => `Saved a new key for ${nameOf(answer, credentials)}.`,
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
        : `Deleted ${nameOf(entry, credentials)}.`,
      'Could not delete the credential',
    )
    // Gone already (deleted elsewhere): what the user asked for has happened, and the list is re-read.
    const gone = !deleted.ok && deleted.error instanceof ApiError && deleted.error.status === 404
    if (gone) {
      setError(null)
      setNotice('Already deleted elsewhere.')
    }
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
                : noneUsable(credentials, current.can_save)}
            </p>
          )}
          <ol className="flex flex-col divide-y divide-border rounded-lg border border-border" aria-label="Claude credentials, tried in this order">
            {credentials.map((entry, index) => (
              <Collapsible
                asChild
                key={entry.id}
                open={replacing === entry.id}
                onOpenChange={(next) => {
                  setReplacing(next ? entry.id : null)
                  setReplacement('')
                }}
              >
              <li className="flex flex-col gap-2 px-3 py-2.5" data-testid="ai-credential">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-muted-foreground">{index + 1}.</span>
                  <span className="font-medium">{KIND_LABEL[entry.kind]}</span>
                  {entry.base_url && <span className="text-muted-foreground">at {entry.base_url}</span>}
                  {entry.last4 && (
                    <span className="font-mono text-muted-foreground" data-testid="ai-credential-last4">
                      <span aria-hidden="true">••••</span>
                      <span className="sr-only">ending in </span>
                      {entry.last4}
                    </span>
                  )}
                  <StatusBadge entry={entry} />
                  {entry.last_used_at && (
                    <Tip content={entry.last_used_at}>
                      <span className="text-xs text-muted-foreground">used {timeAgo(entry.last_used_at)}</span>
                    </Tip>
                  )}
                </div>
                {entry.status !== 'active' && entry.last_error && (
                  <p className="text-xs text-muted-foreground">{entry.last_error}</p>
                )}
                {!entry.usable && (
                  <p className="text-xs text-warn">
                    {current.can_save
                      ? 'The agent cannot decrypt this key (it was saved under another encryption key, or in an old format). Replace it.'
                      : 'The agent cannot decrypt this key: no key-encryption key is mounted. Mount it, then replace the key if it still cannot be decrypted.'}
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-1.5">
                  <Tip wrap={index === 0 || busy !== null} content="Try it earlier">
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      onClick={() => move(index, -1)}
                      disabled={index === 0 || busy !== null}
                      aria-label={`Move ${nameOf(entry, credentials)} up`}
                    >
                      {isBusy(entry.id, 'up') ? <Spinner /> : <ArrowUpIcon />}
                    </Button>
                  </Tip>
                  <Tip wrap={index === credentials.length - 1 || busy !== null} content="Try it later">
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      onClick={() => move(index, 1)}
                      disabled={index === credentials.length - 1 || busy !== null}
                      aria-label={`Move ${nameOf(entry, credentials)} down`}
                    >
                      {isBusy(entry.id, 'down') ? <Spinner /> : <ArrowDownIcon />}
                    </Button>
                  </Tip>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => void test(entry)}
                    disabled={busy !== null || !entry.usable}
                    aria-busy={isBusy(entry.id, 'test')}
                    aria-label={`Test ${nameOf(entry, credentials)}`}
                  >
                    {isBusy(entry.id, 'test') && <Spinner />}
                    Test
                  </Button>
                  {entry.status !== 'active' && entry.usable && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        void act(
                          { id: entry.id, action: 'reset' },
                          () => api.resetAiCredential(entry.id),
                          `${nameOf(entry, credentials)} is active again.`,
                          'Could not reset the credential',
                        )
                      }
                      disabled={busy !== null}
                      aria-label={`Reset ${nameOf(entry, credentials)}`}
                    >
                      {isBusy(entry.id, 'reset') && <Spinner />}
                      Reset
                    </Button>
                  )}
                  <CollapsibleTrigger asChild>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy !== null || !current.can_save}
                      aria-label={`Replace the key of ${nameOf(entry, credentials)}`}
                    >
                      Replace key
                      <ChevronDownIcon
                        data-icon="inline-end"
                        className={`transition-transform motion-reduce:transition-none ${replacing === entry.id ? 'rotate-180' : ''}`}
                      />
                    </Button>
                  </CollapsibleTrigger>
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={(event) => {
                      // An earlier action's error is not this delete's.
                      setError(null)
                      deleteButton.current = event.currentTarget
                      setConfirmDelete(entry)
                    }}
                    disabled={busy !== null}
                    aria-label={`Delete ${nameOf(entry, credentials)}`}
                  >
                    Delete
                  </Button>
                </div>
                <CollapsibleContent asChild>
                  <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => void replace(event, entry)}>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`ai-credential-replace-${entry.id}`} className="text-[13px] font-normal">
                        {REPLACE_LABEL[entry.kind]}
                      </Label>
                      <Input
                        id={`ai-credential-replace-${entry.id}`}
                        type="password"
                        required
                        value={replacement}
                        onChange={(event) => setReplacement(event.target.value)}
                        className="w-72 max-w-full"
                        autoComplete="new-password"
                        spellCheck={false}
                      />
                    </div>
                    {replaceMismatch(entry.kind, replacement) && (
                      <p data-testid="ai-credential-replace-kind-warning" className="basis-full text-xs text-warn">
                        {replaceMismatch(entry.kind, replacement)}
                      </p>
                    )}
                    <Button
                      type="submit"
                      disabled={replacement.trim() === '' || busy !== null}
                      aria-busy={isBusy(entry.id, 'replace')}
                    >
                      {isBusy(entry.id, 'replace') && <Spinner />}
                      Save key
                    </Button>
                  </form>
                </CollapsibleContent>
                {tests[entry.id] && (
                  <p
                    role="status"
                    data-testid="ai-credential-test"
                    className={`text-xs ${tests[entry.id]!.ok ? 'text-ok' : 'text-warn'}`}
                  >
                    {tests[entry.id]!.ok
                      ? `Works${tests[entry.id]!.model ? ` (${tests[entry.id]!.model})` : ''}, ${(tests[entry.id]!.duration_ms / 1000).toFixed(1)} s.`
                      : `Failed: ${tests[entry.id]!.detail}`}
                  </p>
                )}
              </li>
              </Collapsible>
            ))}
          </ol>
          <p className="text-xs text-muted-foreground">
            The assistant uses the first usable credential. A refused one is disabled until you reset it or give it a
            new key; a rate-limited one is skipped until its limit resets. Test sends one short prompt, so it spends a
            few tokens, at most once per 10 seconds.
          </p>
        </>
      )}
      {!current.can_save && (
        <p className="text-xs text-warn">
          Saving is not possible: {current.cannot_save_reason ?? 'the agent cannot store secrets'}.
        </p>
      )}

      <form className="flex flex-col gap-3" onSubmit={(event) => void add(event)}>
        <fieldset disabled={!current.can_save}>
          <legend className="mb-2 text-xs text-muted-foreground">{credentials.length === 0 ? 'Save' : 'Add another'}</legend>
          <RadioGroup
            name="ai-credential-kind"
            value={kind}
            disabled={!current.can_save}
            onValueChange={(option) => {
              // A secret typed for one kind must not be sent as another.
              setKind(option as AiCredentialKind)
              setSecret('')
              setBaseUrl('')
            }}
            className="flex flex-wrap gap-4"
          >
            {(Object.keys(KIND_OPTION) as AiCredentialKind[]).map((option) => (
              <div key={option} className="flex items-center gap-1.5">
                {/* 24 px each way: Settings' tap-target size (#1035, settings-tap-targets.spec.ts). */}
                <RadioGroupItem id={`ai-credential-kind-${option}`} value={option} className="size-6" />
                <Label htmlFor={`ai-credential-kind-${option}`} className="text-[13px] font-normal">
                  {KIND_OPTION[option]}
                </Label>
              </div>
            ))}
          </RadioGroup>
        </fieldset>
        {kind === 'gateway' && (
          <div className="flex flex-col gap-1">
            <Label htmlFor="ai-credential-base-url" className="text-[13px] font-normal">
              Base URL
            </Label>
            <Input
              id="ai-credential-base-url"
              type="url"
              required
              placeholder="https://gateway.example/anthropic"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              disabled={!current.can_save}
              className="max-w-md"
              autoComplete="off"
            />
          </div>
        )}
        <div className="flex flex-col gap-1">
          <Label htmlFor="ai-credential-secret" className="text-[13px] font-normal">
            {SECRET_FIELD[kind].label}
          </Label>
          <Input
            id="ai-credential-secret"
            type="password"
            required
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            disabled={!current.can_save}
            placeholder={SECRET_FIELD[kind].placeholder}
            className="max-w-md"
            autoComplete="new-password"
            spellCheck={false}
            aria-describedby="ai-credential-help"
          />
        </div>
        {kindMismatch(kind, secret) && (
          <p data-testid="ai-credential-kind-warning" className="text-xs text-warn">
            {kindMismatch(kind, secret)}
          </p>
        )}
        {kind === 'claude_oauth_token' && (
          <p className="text-xs text-muted-foreground">
            Run <code>claude setup-token</code> on a machine signed in to your Claude subscription, and paste the
            token it prints. Its use is subject to Anthropic&rsquo;s terms for your plan.
          </p>
        )}
        <p id="ai-credential-help" className="text-xs text-muted-foreground">
          Sent once to ScadBuddy&rsquo;s agent service, which stores it encrypted. It is never shown again; only its
          last four characters are.
        </p>
        <div>
          <Button type="submit" disabled={!canAdd} aria-busy={adding}>
            {adding && <Spinner />}
            {credentials.length === 0 ? 'Save' : 'Add'}
          </Button>
        </div>
      </form>

      {notice && (
        <p role="status" className="text-xs text-ok">
          {notice}
        </p>
      )}
      {stale && (
        <div
          role="alert"
          data-testid="ai-credentials-stale"
          className="flex flex-wrap items-center gap-2 text-xs text-warn"
        >
          Could not read the credentials again, so this list may be out of date.
          <Button size="sm" variant="outline" onClick={reread}>
            Retry
          </Button>
        </div>
      )}
      {error && confirmDelete === null && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
      <AlertDialog
        open={confirmDelete !== null}
        onOpenChange={(next) => {
          if (next || busy?.action === 'delete') return
          setConfirmDelete(null)
          setError(null)
        }}
      >
        <AlertDialogContent
          data-modal=""
          onCloseAutoFocus={(event) => {
            // Back to the Delete that opened it, while its credential is still listed.
            event.preventDefault()
            const back = deleteButton.current
            if (back?.isConnected) back.focus()
          }}
          {...USER_ONLY}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {confirmDelete ? nameOf(confirmDelete, credentials) : 'the credential'}?</AlertDialogTitle>
            <AlertDialogDescription>{confirmDelete && afterDelete(confirmDelete, credentials)}</AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-[13px] text-warn">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy !== null}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={busy !== null}
              aria-busy={busy?.action === 'delete'}
              onClick={(event) => {
                // Open until the agent answers: a refusal is shown here.
                event.preventDefault()
                if (confirmDelete) void remove(confirmDelete)
              }}
              {...USER_ONLY}
            >
              {busy?.action === 'delete' && <Spinner />}
              Delete credential
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Frame>
  )
}

function StatusBadge({ entry }: { entry: AiCredentialEntry }) {
  const warn = 'border-warn/50 text-warn'
  // Stored status and decryptability are separate (agent `entryView`): a key no mounted
  // key-encryption key opens is skipped whatever its status says.
  if (!entry.usable) {
    return (
      <Badge variant="outline" className={warn}>
        Cannot decrypt
      </Badge>
    )
  }
  if (entry.status === 'cooling_down') {
    return (
      <Tip content={entry.cooldown_until ?? undefined}>
        <Badge variant="outline" className={warn}>
          {entry.cooldown_until ? `Rate limited until ${clock(entry.cooldown_until)}` : 'Rate limited'}
        </Badge>
      </Tip>
    )
  }
  if (entry.status === 'disabled') {
    return (
      <Badge variant="outline" className={warn}>
        Disabled
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="border-ok/50 text-ok">
      Active
    </Badge>
  )
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <Card role="region" className="mt-4" aria-labelledby="ai-credential-heading" {...USER_ONLY}>
      <CardHeader className="border-b">
        <h2 id="ai-credential-heading" className="font-heading text-sm font-medium">
          Claude credentials
        </h2>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-[13px]">{children}</CardContent>
    </Card>
  )
}
