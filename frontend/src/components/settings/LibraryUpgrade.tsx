import { useEffect, useId, useRef, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { LibraryCheck, LibraryUser, ModelSummary } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

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
      <p role="alert" className="text-[13px] text-warn">
        {libraries.error.message}
      </p>
    )
  }
  if (libraries.loading) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading libraries
      </p>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor={selectId} className="block text-[13px]">
            Library
          </label>
          <select
            id={selectId}
            value={library}
            onChange={(event) => choose(event.target.value)}
            disabled={busy}
            className="sb-field mt-1.5 w-48"
          >
            <option value="">Choose a library</option>
            {names.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={refId} className="block text-[13px]">
            Candidate ref
          </label>
          <input
            id={refId}
            value={ref}
            placeholder="A tag or branch"
            onChange={(event) => setRef(event.target.value)}
            disabled={!library}
            className="sb-field sb-num mt-1.5 w-40"
          />
        </div>
      </div>
      {models.error && (
        <p role="alert" className="text-[13px] text-warn">
          Could not read the models, so rows show slugs: {models.error.message}
        </p>
      )}
      {/* Keyed by library: another library's checks and ticks never carry over. */}
      {library && (
        <LibraryUsers key={library} library={library} candidate={ref.trim()} models={bySlug} onBusy={setBusy} />
      )}
    </div>
  )
}

function LibraryUsers({
  library,
  candidate,
  models,
  onBusy,
}: {
  library: string
  candidate: string
  models: ReadonlyMap<string, ModelSummary>
  onBusy: (busy: boolean) => void
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

  // Leaving (another library, or the page) tells the server to stop: the check permit is app-wide.
  useEffect(
    () => () => {
      inflight.current?.abort()
      onBusy(false)
    },
    [onBusy],
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
      <p role="alert" className="text-[13px] text-warn">
        {users.error.message}
      </p>
    )
  }
  if (users.loading || !users.data) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading the models that pin {library}
      </p>
    )
  }
  const rows = users.data
  if (rows.length === 0) {
    return <p className="text-[13px] text-muted">No model pins {library}.</p>
  }
  const count = rows.filter((row) => ticked.has(row.slug)).length

  return (
    <div className="space-y-3">
      <ul aria-label={`Models that pin ${library}`} className="divide-y divide-line rounded-[6px] border border-line">
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
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          onClick={() => void move(rows)}
          disabled={busy || count === 0 || !candidate}
          aria-busy={moving}
        >
          {moving && <Spinner />}
          {count === 1 ? 'Move 1 model' : `Move ${count} models`}
          {candidate && ` to ${candidate}`}
        </Button>
        <p className="text-[12px] text-muted">Each model is re-pinned from its own URL, as its own revision.</p>
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
            <input
              type="checkbox"
              checked={ticked}
              onChange={(event) => onTick(event.target.checked)}
              disabled={moving}
              aria-label={`Move ${name}`}
              className="mt-1"
            />
          )}
          <div className="min-w-0">
            <span className="text-[13px] font-medium">{name}</span>
            {model && model.name !== user.slug && (
              <span className="sb-num ml-2 text-[11px] text-faint">{user.slug}</span>
            )}
            {invalid ? (
              <p className="mt-0.5 text-[12px] text-warn">
                Invalid: this model's entry cannot be read as a pin, so it cannot be moved. Fix it from the model's
                Libraries.
              </p>
            ) : (
              <p className="mt-0.5 text-[12px] text-muted">
                Pinned to <span className="sb-num">{ref}</span> at <span className="sb-num">{short(commit ?? '')}</span>
              </p>
            )}
            {builtin && (
              <p className="mt-0.5 text-[12px] text-muted">Built-in: it can be checked, but not re-pinned.</p>
            )}
          </div>
        </div>
        {!invalid && (
          <Button
            size="sm"
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
        <p className="mt-2 text-[12px] text-muted" data-testid="library-check-stale">
          Checked at <span className="sb-num">{check.ref}</span>, not{' '}
          {candidate ? <span className="sb-num">{candidate}</span> : 'the candidate'}; check again.
        </p>
      )}
      {move?.running && (
        <p className="mt-2 flex items-center gap-2 text-[12px] text-muted">
          <Spinner /> Re-pinning
        </p>
      )}
      {moved && (
        <p className="mt-2 text-[12px] text-ok" data-testid="library-moved">
          Moved to <span className="sb-num">{moved.ref}</span> at <span className="sb-num">{short(moved.commit)}</span>
        </p>
      )}
      {move && !move.running && move.error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
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
      <p role="alert" className="mt-2 text-[12px] text-warn">
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
    <div className="mt-2 text-[12px]" data-testid="library-check">
      {!result.checked ? (
        <p className="text-muted">Not checked at {at}: no OpenSCAD was available to ask.</p>
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
            <li key={index} className={diagnostic.severity === 'error' ? 'text-warn' : 'text-muted'}>
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
      {log.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-muted">OpenSCAD log</summary>
          <pre className="sb-num mt-1 max-h-40 overflow-auto rounded-[6px] bg-surface-2 p-2 text-[11px] whitespace-pre-wrap">
            {log.join('\n')}
          </pre>
        </details>
      )}
    </div>
  )
}
