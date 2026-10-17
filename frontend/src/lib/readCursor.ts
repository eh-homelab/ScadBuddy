/**
 * #1039 — what a read saw of the server's event bus.
 *
 * Every API answer carries `CURSOR_HEADER`: the id of the last event the server had
 * heard when the request arrived, so that event and everything before it is reflected
 * in the answer (backend `api/realtime.py`, "The cursor"). A `ReadScope` collects the
 * cursors of the requests one read makes. The realtime client can then ask whether
 * anything for a topic happened since, rather than read again to find out.
 *
 * A request belongs to every scope open when it starts: `inReadScope` opens one for
 * the synchronous part of a call, which is where a read's requests start. A request a
 * read makes after an `await` belongs to no scope; it starts after the read's first
 * requests, so their cursors still cover it. A read that joins a request someone else
 * started (a shared one) must `adopt` that request's scope, or its scope would vouch
 * for data it never saw a cursor for.
 */
export const CURSOR_HEADER = 'X-ScadBuddy-Last-Event'

/** The most cursors one read hands on: the server's `CHECK_MAX_SINCE`. */
export const MAX_CURSORS = 8

interface Answer {
  /** Unset until the request answers; null when the answer had no cursor. */
  cursor?: string | null
}

export class ReadScope {
  private readonly answers: Answer[] = []
  private readonly adopted: ReadScope[] = []

  /** The distinct cursors of every request made in this scope, or undefined unless
   * there was at least one and each has answered with a cursor. */
  cursors(): string[] | undefined {
    const seen = new Set<string>()
    if (!this.collect(seen)) return undefined
    return seen.size > 0 && seen.size <= MAX_CURSORS ? [...seen] : undefined
  }

  private collect(seen: Set<string>): boolean {
    for (const answer of this.answers) {
      if (typeof answer.cursor !== 'string') return false
      seen.add(answer.cursor)
    }
    return this.adopted.every((scope) => scope.collect(seen))
  }

  /** @internal */
  add(answer: Answer): void {
    this.answers.push(answer)
  }

  /** @internal */
  adopt(scope: ReadScope): void {
    if (scope !== this) this.adopted.push(scope)
  }
}

const open: ReadScope[] = []

/** Runs `fn`, counting the requests it starts synchronously toward `scope`. */
export function inReadScope<T>(scope: ReadScope, fn: () => T): T {
  open.push(scope)
  try {
    return fn()
  } finally {
    open.pop()
  }
}

/** Called by the API client as a request starts: a function to call with its answer. */
export function trackRequest(): (headers: Headers | undefined) => void {
  if (open.length === 0) return () => {}
  const answer: Answer = {}
  for (const scope of open) scope.add(answer)
  return (headers) => {
    answer.cursor = headers?.get(CURSOR_HEADER) ?? null
  }
}

/** Every scope open now also vouches only for what `scope`'s requests saw. */
export function adoptScope(scope: ReadScope): void {
  for (const parent of open) parent.adopt(scope)
}
