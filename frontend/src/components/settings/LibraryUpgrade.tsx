import { useEffect, useId, useRef, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { InstalledLibrary, LibraryCheck, LibraryUser, ModelSummary } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { ChevronRightIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { ChoiceSelect, LoadingLines } from './form'
import { Spinner } from '../ui/spinner'

/**
 * Settings → Libraries (#169): move a library to another ref across the models that pin
 * it. Pick a library and a candidate ref; the models pinning it are listed
 * (`GET /libraries/{name}/users`). Each can be checked against the candidate
 * (`POST /models/{slug}/libraries/{name}/check`), and the ticked ones are re-pinned from
 * the URL each already pins (`PATCH`), one request and one commit per model.
 *
 * A check holds the server's checkout gate for the clone and the parse check, which can
 * run up to the render timeout, so checks run only when asked, one at a time, and never
 * while re-pins run: a burst of them would keep a library removal waiting. The permit is
 * app-wide, so a check whose rows unmount is aborted, and the library cannot be switched
 * while a check or a re-pin runs.
 */

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

function short(commit: string): string {
  return commit.slice(0, 7)
}

/** `ref` is the candidate the check was asked for: a result for another ref is stale. */
type CheckState = { ref: string } & ({ running: true } | { running: false; result?: LibraryCheck; error?: string })
type MoveState =
  | { running: true }
  | { running: false; moved?: { ref: string; commit: string }; error?: string }

export function LibraryUpgrade() {
  const libraries = useAsync(
    () => Promise.all([api.listLibraries(), api.listInstalledLibraries()]),
    [],
    ['libraries'],
  )
  // Once per mount, not per library: the names and built-in flags every row shows.
  const models = useAsync(() => api.listModels(), [], ['models'])
  const bySlug: ReadonlyMap<string, ModelSummary> = new Map(
    (models.data ?? []).map((model) => [model.slug, model]),
  )
  const [library, setLibrary] = useState('')
  const [ref, setRef] = useState('')
  const [busy, setBusy] = useState(false)
  // A running move re-pins every row to the ref it started with, so the field is held
  // until it ends. A check leaves it open: its result is marked stale on a change.
  const [moving, setMoving] = useState(false)
  // A removal's re-list that failed: the removed checkout may still be listed, so say so,
  // until the list is read again (#3839): what was on screen when it failed. A later read
  // that lands, a realtime refresh or the next removal's, replaces that and the alert goes.
  const [relistFailedOn, setRelistFailedOn] = useState<typeof libraries.data | null>(null)
  const shownLibraries = useRef(libraries.data)
  useEffect(() => {
    shownLibraries.current = libraries.data
  })
  const relistFailed = relistFailedOn !== null && relistFailedOn === libraries.data
  const selectId = useId()
  const refId = useId()

  const [catalogue = [], installed = []] = libraries.data ?? []
  const names = [...new Set([...catalogue.map((entry) => entry.name), ...installed.map((entry) => entry.name)])].sort(
    (a, b) => a.localeCompare(b),
  )

  function choose(name: string) {
    setLibrary(name)
    setRef(catalogue.find((entry) => entry.name === name)?.ref ?? '')
  }

  if (libraries.error) {
    return (
      <p role="alert" className="text-sm text-warn">
        {libraries.error.message}
      </p>
    )
  }
  if (libraries.loading) {
    return <LoadingLines label="Loading libraries" lines={['h-8 w-48', 'h-8 w-40']} />
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor={selectId} className="font-normal">
            Library
          </Label>
          <ChoiceSelect
            id={selectId}
            value={library}
            onValueChange={choose}
            disabled={busy}
            choices={[{ value: '', label: 'Choose a library' }, ...names.map((name) => ({ value: name, label: name }))]}
            className="w-48"
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor={refId} className="font-normal">
            Candidate ref
          </Label>
          <Input
            id={refId}
            value={ref}
            placeholder="A tag or branch"
            onChange={(event) => setRef(event.target.value)}
            disabled={!library || moving}
            className="sb-num w-40"
          />
        </div>
      </div>
      {models.error && (
        <p role="alert" className="text-sm text-warn">
          Could not read the models, so rows show slugs: {models.error.message}
        </p>
      )}
      {relistFailed && (
        <p role="alert" className="text-[13px] text-warn">
          Could not re-list the checkouts, so the list may still show one that was removed.
        </p>
      )}
      {/* A refresh, not a reload: the panel keeps its library, checks and ticks on screen. */}
      <UnusedCheckouts
        installed={installed}
        onRemoved={() => {
          setRelistFailedOn(null)
          // Best effort: a read started after this one (a realtime refresh) supersedes it,
          // and only that read's own failure is reported, not this one's (#3839). Should it
          // fail too, the list shown is still the one from before the removal.
          libraries.refresh(undefined, () => setRelistFailedOn(shownLibraries.current))
        }}
      />
      {/* Keyed by library: another library's checks and ticks never carry over. */}
      {library && (
        <LibraryUsers
          key={library}
          library={library}
          candidate={ref.trim()}
          models={bySlug}
          onBusy={setBusy}
          onMoving={setMoving}
        />
      )}
    </div>
  )
}

function LibraryUsers({
  library,
  candidate,
  models,
  onBusy,
  onMoving,
}: {
  library: string
  candidate: string
  models: ReadonlyMap<string, ModelSummary>
  onBusy: (busy: boolean) => void
  onMoving: (moving: boolean) => void
}) {
  const users = useAsync(() => api.listLibraryUsers(library), [library], ['libraries'])
  const [checks, setChecks] = useState<Record<string, CheckState>>({})
  const [moves, setMoves] = useState<Record<string, MoveState>>({})
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set())
  const [moving, setMoving] = useState(false)

  const checking = Object.values(checks).some((state) => state.running)
  const busy = checking || moving
  const inflight = useRef<AbortController | null>(null)

  useEffect(() => {
    onBusy(busy)
  }, [busy, onBusy])

  useEffect(() => {
    onMoving(moving)
  }, [moving, onMoving])

  // Leaving (another library, or the page) tells the server to stop: the check permit is app-wide.
  useEffect(
    () => () => {
      inflight.current?.abort()
      onBusy(false)
      onMoving(false)
    },
    [onBusy, onMoving],
  )

  async function check(slug: string) {
    if (busy || !candidate) return
    const ref = candidate
    const controller = new AbortController()
    inflight.current = controller
    const { signal } = controller
    setChecks((all) => ({ ...all, [slug]: { ref, running: true } }))
    try {
      const result = await api.checkModelLibrary(slug, library, { ref }, signal)
      if (!signal.aborted) setChecks((all) => ({ ...all, [slug]: { ref, running: false, result } }))
    } catch (caught) {
      // An abort is this component's own doing, not a failed check.
      if (!signal.aborted) setChecks((all) => ({ ...all, [slug]: { ref, running: false, error: message(caught) } }))
    } finally {
      if (inflight.current === controller) inflight.current = null
    }
  }

  async function move(rows: readonly LibraryUser[]) {
    if (busy || !candidate) return
    setMoving(true)
    // The ticks as of this click; the checkboxes are disabled until the move is done.
    const chosen = rows.filter((row) => ticked.has(row.slug))
    // One at a time, in list order: each re-pin clones under the same gate a check holds.
    for (const { slug } of chosen) {
      setMoves((all) => ({ ...all, [slug]: { running: true } }))
      try {
        const model = await api.repinModelLibrary(slug, library, { ref: candidate })
        const pin = model.libraries?.find((entry) => entry.name === library)
        if (!pin) {
          // Stays ticked: the user can try again.
          setMoves((all) => ({
            ...all,
            [slug]: {
              running: false,
              error: `the re-pin answered, but the model it returned does not pin ${library}; reload to see where it stands`,
            },
          }))
          continue
        }
        setMoves((all) => ({ ...all, [slug]: { running: false, moved: { ref: pin.ref, commit: pin.commit } } }))
        setTicked((all) => {
          const next = new Set(all)
          next.delete(slug)
          return next
        })
      } catch (caught) {
        setMoves((all) => ({ ...all, [slug]: { running: false, error: message(caught) } }))
      }
    }
    setMoving(false)
  }

  function tick(slug: string, on: boolean) {
    setTicked((all) => {
      const next = new Set(all)
      if (on) next.add(slug)
      else next.delete(slug)
      return next
    })
  }

  if (users.error) {
    return (
      <p role="alert" className="text-sm text-warn">
        {users.error.message}
      </p>
    )
  }
  if (users.loading || !users.data) {
    return <LoadingLines label={`Loading the models that pin ${library}`} lines={['h-12 w-full', 'h-12 w-full']} />
  }
  const rows = users.data
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No model pins {library}.</p>
  }
  const count = rows.filter((row) => ticked.has(row.slug)).length

  return (
    <div className="space-y-3">
      <Card size="sm" className="py-0">
      <ul aria-label={`Models that pin ${library}`} className="divide-y">
        {rows.map((user) => (
          <UserRow
            key={user.slug}
            user={user}
            model={models.get(user.slug)}
            candidate={candidate}
            check={checks[user.slug]}
            move={moves[user.slug]}
            ticked={ticked.has(user.slug)}
            busy={busy}
            moving={moving}
            onCheck={() => void check(user.slug)}
            onTick={(on) => tick(user.slug, on)}
          />
        ))}
      </ul>
      </Card>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          onClick={() => void move(rows)}
          disabled={busy || count === 0 || !candidate}
          aria-busy={moving}
        >
          {moving && <Spinner />}
          {count === 1 ? 'Move 1 model' : `Move ${count} models`}
          {candidate && ` to ${candidate}`}
        </Button>
        <p className="text-xs text-muted-foreground">Each model is re-pinned from its own URL, as its own revision.</p>
      </div>
    </div>
  )
}

function UserRow({
  user,
  model,
  candidate,
  check,
  move,
  ticked,
  busy,
  moving,
  onCheck,
  onTick,
}: {
  user: LibraryUser
  model: ModelSummary | undefined
  candidate: string
  check: CheckState | undefined
  move: MoveState | undefined
  ticked: boolean
  busy: boolean
  /** Ticks stay open during a check; a move reads them once, as it starts. */
  moving: boolean
  onCheck: () => void
  onTick: (on: boolean) => void
}) {
  // A slug's prefix says so before the model list has answered.
  const builtin = model ? model.origin === 'builtin' : user.slug.startsWith('builtin:')
  const moved = move && !move.running ? move.moved : undefined
  const ref = moved?.ref ?? user.ref
  const commit = moved?.commit ?? user.commit
  const invalid = user.ref === null || user.commit === null
  const name = model?.name ?? user.slug

  return (
    <li aria-label={name} className="px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          {!builtin && !invalid && (
            <Checkbox
              checked={ticked}
              onCheckedChange={(next) => onTick(next === true)}
              disabled={moving}
              aria-label={`Move ${name}`}
              className="mt-0.5"
            />
          )}
          <div className="min-w-0">
            <span className="text-sm font-medium">{name}</span>
            {model && model.name !== user.slug && (
              <span className="sb-num ml-2 text-[11px] text-faint">{user.slug}</span>
            )}
            {invalid ? (
              <p className="mt-0.5 text-xs text-warn">
                Invalid: this model's entry cannot be read as a pin, so it cannot be moved. Fix it from the model's
                Libraries.
              </p>
            ) : (
              <p className="mt-0.5 text-xs text-muted-foreground">
                Pinned to <span className="sb-num">{ref}</span> at <span className="sb-num">{short(commit ?? '')}</span>
              </p>
            )}
            {builtin && (
              <p className="mt-0.5 text-xs text-muted-foreground">Built-in: it can be checked, but not re-pinned.</p>
            )}
          </div>
        </div>
        {!invalid && (
          <Button
            size="sm"
            variant="outline"
            onClick={onCheck}
            disabled={busy || !candidate}
            aria-busy={check?.running === true}
          >
            {check?.running && <Spinner />}
            Check
          </Button>
        )}
      </div>
      {check && !check.running && check.ref === candidate && <CheckResult check={check} />}
      {check && !check.running && check.ref !== candidate && (
        <p className="mt-2 text-xs text-muted-foreground" data-testid="library-check-stale">
          Checked at <span className="sb-num">{check.ref}</span>, not{' '}
          {candidate ? <span className="sb-num">{candidate}</span> : 'the candidate'}; check again.
        </p>
      )}
      {move?.running && (
        <p className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner /> Re-pinning
        </p>
      )}
      {moved && (
        <p className="mt-2 text-xs text-ok" data-testid="library-moved">
          Moved to <span className="sb-num">{moved.ref}</span> at <span className="sb-num">{short(moved.commit)}</span>
        </p>
      )}
      {move && !move.running && move.error && (
        <p role="alert" className="mt-2 text-xs text-warn">
          Not moved: {move.error}
        </p>
      )}
    </li>
  )
}

function CheckResult({ check }: { check: { result?: LibraryCheck; error?: string } }) {
  const { result, error } = check
  if (error !== undefined) {
    return (
      <p role="alert" className="mt-2 text-xs text-warn">
        Check failed: {error}
      </p>
    )
  }
  if (!result) return null
  const at = (
    <>
      <span className="sb-num">{result.ref}</span> (<span className="sb-num">{short(result.commit)}</span>)
    </>
  )
  const diagnostics = result.diagnostics ?? []
  const log = result.log_tail ?? []
  return (
    <div className="mt-2 text-xs" data-testid="library-check">
      {!result.checked ? (
        <p className="text-muted-foreground">Not checked at {at}: no OpenSCAD was available to ask.</p>
      ) : result.ok ? (
        <p className="text-ok">
          Parses at {at}
          {result.parameters == null ? '' : ` — ${result.parameters} parameters`}.
        </p>
      ) : (
        <p className="text-warn">
          {result.timed_out ? 'Timed out' : 'Does not parse'} at {at}.
        </p>
      )}
      {diagnostics.length > 0 && (
        <ul aria-label="Diagnostics" className="mt-1 space-y-0.5">
          {diagnostics.map((diagnostic, index) => (
            <li key={index} className={diagnostic.severity === 'error' ? 'text-warn' : 'text-muted-foreground'}>
              {diagnostic.file && (
                <span className="sb-num">
                  {diagnostic.file}
                  {diagnostic.line != null && `:${diagnostic.line}`}{' '}
                </span>
              )}
              {diagnostic.message}
            </li>
          ))}
        </ul>
      )}
      {log.length > 0 && <LogFold log={log} />}
    </div>
  )
}

/** The OpenSCAD log of a check, folded until asked for. */
function LogFold({ log }: { log: readonly string[] }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mt-1">
      <CollapsibleTrigger className="inline-flex min-h-6 items-center gap-1 text-muted-foreground hover:text-foreground">
        <ChevronRightIcon aria-hidden="true" className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
        OpenSCAD log
      </CollapsibleTrigger>
      <CollapsibleContent forceMount hidden={!open}>
        <pre className="sb-num mt-1 max-h-40 overflow-auto rounded-md bg-muted p-2 text-[11px] whitespace-pre-wrap">
          {log.join('\n')}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  )
}

/**
 * #2770 — checkouts on the volume that no model's live pin reads (`used_by` empty): an
 * unpin keeps its checkout, so they build up. Each can be removed
 * (`DELETE /libraries/{name}?commit=`); the route still refuses one a pin or a running
 * render reads, and a later render of an older revision that pinned it clones it again.
 */
function UnusedCheckouts({ installed, onRemoved }: { installed: readonly InstalledLibrary[]; onRemoved: () => void }) {
  const unused = installed.filter((row) => (row.used_by ?? []).length === 0)
  const [removing, setRemoving] = useState<string | null>(null)
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({})
  const headingId = useId()
  if (unused.length === 0) return null

  async function remove(row: InstalledLibrary) {
    const key = `${row.name}@${row.commit}`
    setRemoving(key)
    setErrors(({ [key]: _, ...rest }) => rest)
    try {
      // Each press is a new Idempotency-Key, a press after a failure included: under the
      // failed attempt's key the server would answer that attempt's refusal again, and the
      // delete is safe to repeat. A "Try again" for an answer that never came would pass
      // that attempt's key instead (`command()`'s `key`).
      await api.removeLibraryCheckout(row.name, row.commit)
      // The rows are re-listed in place: another row's refusal may no longer hold.
      setErrors({})
      onRemoved()
    } catch (caught) {
      setErrors((current) => ({ ...current, [key]: message(caught) }))
    } finally {
      setRemoving(null)
    }
  }

  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <h3 id={headingId} className="text-sm font-medium">
        Checkouts no model uses
      </h3>
      <p className="text-xs text-muted-foreground">
        Unpinning a library keeps its checkout on the volume. Remove the ones nothing pins any more.
      </p>
      <ul aria-labelledby={headingId} className="space-y-1">
        {unused.map((row) => {
          const key = `${row.name}@${row.commit}`
          const label = `${row.name} ${short(row.commit)}`
          return (
            <li key={key} aria-label={label} className="flex flex-wrap items-center gap-3 text-sm">
              <span>
                {row.name} <span className="sb-num text-muted-foreground">{short(row.commit)}</span>
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void remove(row)}
                disabled={removing !== null}
                aria-busy={removing === key}
                aria-label={`Remove ${label}`}
              >
                {removing === key ? 'Removing' : 'Remove'}
              </Button>
              {errors[key] && (
                <span role="alert" className="text-warn">
                  {errors[key]}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
