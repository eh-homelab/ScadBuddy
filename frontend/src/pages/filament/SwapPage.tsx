import { ArrowDownIcon, ExternalLinkIcon, NfcIcon, PackageIcon } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../../api/client'
import { getSpool, moveSpool, staleOf, type LocationDetail, type Spool } from '../../api/filament'
import { problemCode } from '../../api/filamentPresets'
import { IdentifyPlace } from '../../components/filament/IdentifyPlace'
import { IdentifySpool } from '../../components/filament/IdentifySpool'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { WritePlaceTag } from '../../components/filament/WritePlaceTag'
import { WriteTag } from '../../components/filament/scan/WriteTag'
import { openExternal } from '../../lib/embed'
import { spoolCode } from '../../lib/homeReturns'
import { afterMove, isWaiting, moveFirst, nameTarget, reread, takeOutFirst, type Chain, type Held, type Next, type Waiting } from '../../lib/swapChain'
import { NfcSession, nfcSupport } from '../../lib/webNfc'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type Move = Extract<Next, { kind: 'move' }>
type Support = ReturnType<typeof nfcSupport>

/** Where the page is: the step the person answers, or the answer of the last move. */
type Phase =
  | { kind: 'spool' }
  /** `forId`: a waiting spool whose place the person names now, after its take-out did not go through. */
  | { kind: 'place'; forId?: string }
  | { kind: 'sending' }
  | { kind: 'stale'; next: Move; said: string }
  | { kind: 'full'; next: Move; occupant: Spool; cycle: boolean }
  | { kind: 'conflict'; next: Move; archives?: string }
  | { kind: 'failed'; next: Move; said: string; retry: boolean; archives?: string }

const EMPTY: Chain = { current: null, waiting: [] }
const grams = (value: number) => `${Number(value.toFixed(1))} g`
const reasonOf = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached.')
/** True when the request got no answer (the client's status 0): the same move may go through when sent again. */
const unanswered = (cause: unknown) => !(cause instanceof ApiError) || cause.status === 0
const colourOf = (spool: Spool) => spool.effective?.colour_name ?? spool.colour_name ?? spool.names?.material ?? 'No colour yet'

/** The entry of `id` in the chain, current or waiting. */
const entryOf = (chain: Chain, id: string): Held | null => (chain.current?.spool.id === id ? chain.current : (chain.waiting.find((one) => one.spool.id === id) ?? null))
/** The spool of `id` in the chain, current or waiting. */
const heldOf = (chain: Chain, id: string): Spool | null => entryOf(chain, id)?.spool ?? null

/** The chain without an archived spool: when it was current, the newest waiting spool is current. */
function without(chain: Chain, id: string): Chain {
  if (chain.current?.spool.id === id) return { current: chain.waiting.at(-1) ?? null, waiting: chain.waiting.slice(0, -1) }
  return { current: chain.current, waiting: chain.waiting.filter((one) => one.spool.id !== id) }
}

const HEADINGS: Record<Phase['kind'], string> = {
  spool: 'Which spool?',
  place: 'Where does it go?',
  sending: 'Moving…',
  stale: 'The spool moved meanwhile',
  full: 'The box is full',
  conflict: 'The move did not go through',
  failed: 'The move did not go through',
}

/**
 * #7168 — the swap wizard: the person names a spool, then its new place, and the page moves it;
 * then the next spool. Each step is a tag, a QR code or a pick by hand, never a default (R4).
 * Each move carries where the spool was read (`expected_location_id`); a stale one is shown and
 * waits for the person (D5). A full dry box asks: archive its spool, move that spool first (the
 * chain of `lib/swapChain.ts`), or choose another place. `nfcSupport()` is read once and passed
 * down: in a frame the page offers its own tab and uses no tag and no camera.
 */
export function SwapPage() {
  const [nfc] = useState<Support>(nfcSupport)
  const [session] = useState(() => (nfc === 'ok' ? new NfcSession() : null))
  const [chain, setChainState] = useState<Chain>(EMPTY)
  const chainRef = useRef(chain)
  const [phase, setPhase] = useState<Phase>({ kind: 'spool' })
  const [place, setPlace] = useState<LocationDetail | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [writing, setWriting] = useState<'spool' | 'place' | null>(null)
  const [log, setLog] = useState<{ key: number; text: string }[]>([])
  const logKey = useRef(0)
  // The names of the places this visit has seen, for the ticket and the stack.
  const [names, setNames] = useState<ReadonlyMap<string, string>>(new Map())
  const addName = (id: string, name: string) => setNames((known) => (known.get(id) === name ? known : new Map(known).set(id, name)))

  useEffect(() => () => session?.stop(), [session])

  // A new step moves focus to its heading, so a screen reader reads the step; the first view keeps the page's focus.
  // "Sending" is only for a moment: the focus stays, and moves to the step that comes after it, the same kind too.
  const heading = useRef<HTMLHeadingElement>(null)
  const shown = useRef(phase.kind)
  const sent = useRef(false)
  useLayoutEffect(() => {
    if (phase.kind === 'sending') {
      sent.current = true
      return
    }
    if (shown.current === phase.kind && !sent.current) return
    shown.current = phase.kind
    sent.current = false
    heading.current?.focus()
  }, [phase.kind])

  const setChain = (next: Chain) => {
    chainRef.current = next
    setChainState(next)
  }
  const placeName = (id: string | null, fallback?: string | null) => (id === null ? 'no place' : (names.get(id) ?? fallback ?? 'a place'))
  const note = (text: string) => setLog((rows) => [{ key: (logKey.current += 1), text }, ...rows])

  /** The next step; `said` is the notice shown with it. */
  function toStep(next: Next, said?: string) {
    setPlace(null)
    setWriting(null)
    if (next.kind === 'move') void send(next, undefined, said)
    else {
      if (said) setNotice(said)
      setPhase({ kind: next.kind === 'done' ? 'spool' : 'place' })
    }
  }

  async function send(next: Move, archives?: string, said?: string) {
    setPhase({ kind: 'sending' })
    setNotice(said ?? null)
    const code = spoolCode(heldOf(chainRef.current, next.spoolId) ?? { number: 0 })
    try {
      const moved = await moveSpool(
        next.spoolId,
        next.target,
        next.ifOccupied === 'replace' && next.expectedOccupantId
          ? { ifOccupied: 'replace', expectedOccupantId: next.expectedOccupantId, expectedLocationId: next.expectedLocationId }
          : { ifOccupied: 'refuse', expectedLocationId: next.expectedLocationId },
      )
      // The server's name of the place the spool is in now.
      note(`${code} → ${next.target === null ? 'no place' : (moved.names?.location ?? placeName(next.target))}`)
      if (archives) note(`${archives} archived`)
      const after = afterMove(chainRef.current, moved)
      setChain(after.chain)
      toStep(after.next)
    } catch (cause) {
      await refused(next, code, cause, archives)
    }
  }

  async function refused(next: Move, code: string, cause: unknown, archives?: string) {
    const failed = (again: unknown) => setPhase({ kind: 'failed', next, said: reasonOf(again), retry: unanswered(again), archives })
    const stale = staleOf(cause)
    if (stale) {
      try {
        const spool = await getSpool(next.spoolId)
        // The spool is in the target already (a move whose answer was lost went through): the move is done.
        if (!stale.archived && !spool.archived_at && (spool.location_id ?? null) === next.target) {
          const there = next.target === null ? 'no place' : (spool.names?.location ?? placeName(next.target))
          note(`${code} → ${there}`)
          if (archives) note(`${archives} archived`)
          const after = afterMove(chainRef.current, spool)
          setChain(after.chain)
          toStep(after.next, `${code} is already in ${there}.`)
          return
        }
        if (stale.archived || spool.archived_at) {
          const left = without(chainRef.current, next.spoolId)
          setChain(left)
          setPlace(null)
          if (!left.current) {
            setNotice(`${code} was archived.`)
            setPhase({ kind: 'spool' })
            return
          }
          // A move goes out only after its target is named, so the spool left current has one; its move waits for the person too.
          const waits = left.current as Waiting
          setPhase({ kind: 'stale', next: { kind: 'move', spoolId: waits.spool.id, target: waits.target, expectedLocationId: waits.readIn, ifOccupied: 'refuse' }, said: `${code} was archived.` })
          return
        }
        const again = reread(chainRef.current, spool)
        setChain(again.chain)
        const where = spool.location_id ? `${code} is now in ${spool.names?.location ?? 'another place'}.` : `${code} is in no place now.`
        if (spool.location_id && spool.names?.location) addName(spool.location_id, spool.names.location)
        if (again.next.kind === 'move') setPhase({ kind: 'stale', next: again.next, said: where })
        else {
          setNotice(where)
          setPhase({ kind: 'place' })
        }
      } catch (again) {
        failed(again)
      }
      return
    }
    const code409 = problemCode(cause)
    if (code409 === 'box_full') {
      const occupantId = (cause as ApiError).problem.occupant_id
      try {
        const occupant = await getSpool(String(occupantId))
        setPhase({ kind: 'full', next, occupant, cycle: isWaiting(chainRef.current, occupant.id) })
      } catch (again) {
        failed(again)
      }
      return
    }
    if (code409 === 'move_conflict') setPhase({ kind: 'conflict', next, archives })
    else failed(cause)
  }

  function spoolNamed(spool: Spool | null) {
    if (!spool) return
    if (spool.location_id && spool.names?.location) addName(spool.location_id, spool.names.location)
    setChain({ current: { spool, readIn: spool.location_id ?? null }, waiting: chainRef.current.waiting })
    setNotice(null)
    setPhase({ kind: 'place' })
  }

  function placeNamed(named: LocationDetail | null) {
    if (!named) return
    addName(named.id, named.name)
    setPlace(named)
  }

  function moveTo(target: LocationDetail) {
    const waits = phase.kind === 'place' && phase.forId ? chainRef.current.waiting.find((one) => one.spool.id === phase.forId) : undefined
    // A waiting spool goes to the place named instead of to no place; its own target and the chain stay.
    if (waits) return void send({ kind: 'move', spoolId: waits.spool.id, target: target.id, expectedLocationId: waits.readIn, ifOccupied: 'refuse' })
    const { chain: named, next } = nameTarget(chainRef.current, target.id)
    setChain(named)
    if (next.kind === 'move') void send(next)
  }

  /** "Choose another place" names a place for the spool whose move did not go through, current or waiting. */
  function chooseAnother(spoolId: string) {
    setPlace(null)
    setPhase(isWaiting(chainRef.current, spoolId) ? { kind: 'place', forId: spoolId } : { kind: 'place' })
  }

  function clear() {
    setChain(EMPTY)
    setPlace(null)
    setWriting(null)
    setNotice(null)
    setPhase({ kind: 'spool' })
  }

  const current = chain.current
  // The spool the place step names a place for.
  const subject = phase.kind === 'place' && phase.forId ? (entryOf(chain, phase.forId) ?? current) : current
  const busy = phase.kind === 'sending'
  // The text that says why the step is shown, read with its heading.
  const described = phase.kind === 'stale' || phase.kind === 'full' || phase.kind === 'conflict' || phase.kind === 'failed'

  return (
    <div className="mx-auto flex min-h-full w-full max-w-md min-w-0 flex-col gap-5 px-4 pt-4 pb-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold tracking-tight">Swap spools</h1>
        <p className="text-sm text-muted-foreground">Name a spool, then the place it goes to. Then the next spool.</p>
      </div>

      {nfc === 'embedded' && (
        <div className="flex flex-col gap-2 rounded-lg bg-muted px-3 py-2.5">
          <p>Inside Bambuddy the page cannot use tags or the camera. Its own tab can.</p>
          <Button type="button" variant="outline" className="h-11 self-start" onClick={() => openExternal(window.location.href)}>
            <ExternalLinkIcon /> Open the swap page in its own tab
          </Button>
        </div>
      )}
      {nfc === 'no_api' && <p className="text-sm text-muted-foreground">Tags need Chrome on Android or the ScadBuddy app.</p>}

      <section aria-labelledby="swap-step" className="flex flex-col gap-4 rounded-xl border bg-card p-4 shadow-xs">
        <Ticket spool={subject?.spool ?? null} readIn={subject ? placeName(subject.readIn, subject.spool.names?.location) : null} place={phase.kind === 'place' ? (place?.name ?? null) : null} />
        <h2 id="swap-step" ref={heading} tabIndex={-1} aria-describedby={described ? 'swap-said' : undefined} className="text-xl font-semibold tracking-tight text-balance outline-none">
          {HEADINGS[phase.kind]}
        </h2>
        {notice && (
          <p role="status" className="border-l-[3px] border-warn py-0.5 pl-3 text-warn">
            {notice}
          </p>
        )}

        {phase.kind === 'spool' && <IdentifySpool nfc={nfc} onIdentified={spoolNamed} />}

        {phase.kind === 'place' && subject && (
          <>
            <div className="flex flex-wrap gap-2">
              {session && (
                <Button type="button" variant="outline" className="h-11" aria-label={`Write tag for ${spoolCode(subject.spool)}`} disabled={writing !== null} onClick={() => setWriting('spool')}>
                  <NfcIcon /> Write tag
                </Button>
              )}
              {chain.waiting.length === 0 && (
                <Button type="button" variant="ghost" className="h-11" disabled={writing !== null} onClick={clear}>
                  Change spool
                </Button>
              )}
            </div>
            {session && writing === 'spool' && <WriteTag session={session} spool={subject.spool} onClose={() => setWriting(null)} />}
            {writing === null && !place && <IdentifyPlace nfc={nfc} onIdentified={placeNamed} />}
            {place && (
              <div className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <PackageIcon aria-hidden="true" className="size-4 text-muted-foreground" />
                  <span className="font-medium tabular-nums">{place.code}</span>
                  <span>{place.name}</span>
                  <Button type="button" variant="link" className="ml-auto h-11 px-0" disabled={writing !== null} onClick={() => setPlace(null)}>
                    Change place
                  </Button>
                </div>
                {session && writing === null && (
                  <Button type="button" variant="outline" className="h-11" aria-label={`Write tag for ${place.name}`} onClick={() => setWriting('place')}>
                    <NfcIcon /> Write tag
                  </Button>
                )}
                {session && writing === 'place' && <WritePlaceTag session={session} place={place} onClose={() => setWriting(null)} />}
                <Button type="button" className="h-12 text-base" disabled={writing !== null} onClick={() => moveTo(place)}>
                  Move
                </Button>
              </div>
            )}
          </>
        )}

        {busy && <p role="status" className="text-sm text-muted-foreground">Sending the move…</p>}

        {phase.kind === 'stale' && (
          <div className="flex flex-col gap-3">
            <p id="swap-said">{phase.said}</p>
            <p className="text-sm text-muted-foreground">Check the spool, then move it again.</p>
            <Actions>
              <Button type="button" className="h-12 text-base" onClick={() => void send(phase.next)}>
                Move {spoolCode(heldOf(chain, phase.next.spoolId) ?? { number: 0 })} to {placeName(phase.next.target)}
              </Button>
              <Button type="button" variant="outline" className="h-11" onClick={() => chooseAnother(phase.next.spoolId)}>
                Choose another place
              </Button>
            </Actions>
          </div>
        )}

        {phase.kind === 'full' && <FullBox phase={phase} box={placeName(phase.next.target)} chain={chain} send={(next, archives) => void send(next, archives)} setChain={setChain} onMoveFirst={() => toStep({ kind: 'name_target' })} onAnother={() => chooseAnother(phase.next.spoolId)} />}

        {phase.kind === 'conflict' && (
          <div className="flex flex-col gap-3">
            <p id="swap-said">Another move changed the same spools at the same time. Nothing moved.</p>
            <Actions>
              <Button type="button" className="h-12 text-base" onClick={() => void send(phase.next, phase.archives)}>
                Try again
              </Button>
              <Button type="button" variant="outline" className="h-11" onClick={() => chooseAnother(phase.next.spoolId)}>
                Choose another place
              </Button>
            </Actions>
          </div>
        )}

        {phase.kind === 'failed' && (
          <div className="flex flex-col gap-3">
            <p id="swap-said" role="alert" className="text-warn">
              {phase.said}
            </p>
            <Actions>
              {phase.retry && (
                <Button type="button" className="h-12 text-base" onClick={() => void send(phase.next, phase.archives)}>
                  Try again
                </Button>
              )}
              <Button type="button" variant="outline" className="h-11" onClick={() => chooseAnother(phase.next.spoolId)}>
                Choose another place
              </Button>
            </Actions>
          </div>
        )}
      </section>

      {chain.waiting.length > 0 && (
        <section aria-labelledby="swap-waiting" className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <h2 id="swap-waiting" className="text-sm font-semibold">
              Waiting
            </h2>
            <Button type="button" variant="ghost" className="h-11" disabled={busy} onClick={clear}>
              Clear
            </Button>
          </div>
          <ol aria-label="Waiting moves" className="flex flex-col gap-1">
            {[...chain.waiting].reverse().map((one) => (
              <li key={one.spool.id} className="flex items-center gap-2 rounded-md bg-muted px-3 py-2">
                {(one.spool.effective?.rgba ?? one.spool.rgba) && <RgbaSwatch rgba={(one.spool.effective?.rgba ?? one.spool.rgba)!} size="sm" />}
                <span>
                  {spoolCode(one.spool)} waits for {placeName(one.target)}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}

      <section aria-labelledby="swap-log" className="flex flex-col gap-2">
        <h2 id="swap-log" className="text-sm font-semibold">
          Done this visit
        </h2>
        {log.length === 0 && <p className="text-sm text-muted-foreground">No spool moved yet.</p>}
        <ul aria-label="Moves done" className="flex flex-col divide-y">
          {log.map((row) => (
            <li key={row.key} className="py-1.5 tabular-nums">
              {row.text}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

function Actions({ children }: { children: ReactNode }) {
  return <div className="flex flex-col gap-2">{children}</div>
}

/**
 * The move as far as the person named it: the spool and where it was read, then the place. An
 * empty slot is dashed until it is named; nothing is filled in for the person.
 */
function Ticket({ spool, readIn, place }: { spool: Spool | null; readIn: string | null; place: string | null }) {
  const rgba = spool ? (spool.effective?.rgba ?? spool.rgba) : null
  return (
    <div className="flex flex-col items-stretch gap-1">
      <div className={cn('flex min-h-14 items-center gap-3 rounded-lg px-3 py-2', spool ? 'bg-muted' : 'border-2 border-dashed text-muted-foreground')}>
        {rgba ? <RgbaSwatch rgba={rgba} /> : <span aria-hidden="true" className="size-6 rounded-full border-2 border-dashed" />}
        {spool ? (
          <span className="flex min-w-0 flex-col">
            <span className="text-lg font-semibold tabular-nums">{spoolCode(spool)}</span>
            <span className="truncate text-sm text-muted-foreground">
              {colourOf(spool)}, in {readIn}
              {spool.grams_left != null ? `, ${grams(spool.grams_left)} left` : ''}
            </span>
          </span>
        ) : (
          <span>A spool</span>
        )}
      </div>
      <ArrowDownIcon aria-hidden="true" className="mx-auto size-4 text-muted-foreground" />
      <div className={cn('flex min-h-14 items-center gap-3 rounded-lg px-3 py-2', place ? 'bg-primary/10' : 'border-2 border-dashed text-muted-foreground')}>
        <PackageIcon aria-hidden="true" className="size-5 shrink-0" />
        <span className={place ? 'text-lg font-semibold' : undefined}>{place ?? 'Its new place'}</span>
      </div>
    </div>
  )
}

/**
 * A full dry box (409 `box_full`): its spool, and what the person may do. "Archive" replaces it
 * with the occupant named (`expected_occupant_id`, #7167); "Move first" makes it the next spool to
 * place, and the first one waits; a box whose spool waits in the chain (a cycle) offers to take
 * that spool out first.
 */
function FullBox({
  phase,
  box,
  chain,
  send,
  setChain,
  onMoveFirst,
  onAnother,
}: {
  phase: Extract<Phase, { kind: 'full' }>
  box: string
  chain: Chain
  send: (next: Move, archives?: string) => void
  setChain: (chain: Chain) => void
  onMoveFirst: () => void
  onAnother: () => void
}) {
  const { next, occupant, cycle } = phase
  // Only the current spool can wait for the box: a waiting spool's move offers no "Move first".
  const first = next.spoolId === chain.current?.spool.id
  const code = spoolCode(occupant)
  const rgba = occupant.effective?.rgba ?? occupant.rgba
  return (
    <div className="flex flex-col gap-3">
      <p id="swap-said" className="flex flex-wrap items-center gap-2 rounded-lg bg-muted px-3 py-2">
        {rgba && <RgbaSwatch rgba={rgba} size="sm" />}
        <span>
          {code} is in {box} now{occupant.grams_left != null ? `, with ${grams(occupant.grams_left)} left` : ''}.
        </span>
      </p>
      <Actions>
        {cycle ? (
          <>
            <p className="text-sm text-muted-foreground">{code} waits to go somewhere else. Take it out of {box} first: it waits in no place, then goes on.</p>
            <Button
              type="button"
              className="h-12 text-base"
              onClick={() => {
                const out = takeOutFirst(chain, occupant.id)
                if (out.kind === 'move') send(out)
              }}
            >
              Take {code} out first
            </Button>
          </>
        ) : (
          <>
            {first && (
              <Button
                type="button"
                className="h-12 text-base"
                onClick={() => {
                  setChain(moveFirst(chain, next.target!, occupant))
                  onMoveFirst()
                }}
              >
                Move {code} first
              </Button>
            )}
            <Button type="button" variant="destructive" className="h-11" onClick={() => send({ ...next, ifOccupied: 'replace', expectedOccupantId: occupant.id }, code)}>
              Archive {code}
            </Button>
            <p className="text-warn">
              Archive takes {code} out of {box} for good: it is archived at 0 g
              {occupant.grams_left != null && occupant.grams_left > 0 ? `, and its ${grams(occupant.grams_left)} are booked as non-print waste.` : '.'}
            </p>
          </>
        )}
        <Button type="button" variant="outline" className="h-11" onClick={onAnother}>
          Choose another place
        </Button>
      </Actions>
    </div>
  )
}
