// How long an action waits on the row that records it (#1076): the audit log
// (audit/log.ts) and what a session touched (sessions/touched.ts). Recording
// never fails the action, and past this it no longer holds it up either: the
// write goes on in the background, and the wait that ran out is reported.

/** The longest a recorded action waits for its row. */
export const RECORD_WAIT_MS = 5_000

/** A record write still running when its wait ran out; it may yet land. */
export class WriteTimeout extends Error {
  constructor(what: string, ms: number) {
    super(`${what} took longer than ${ms} ms; the action went on without waiting for it`)
    this.name = 'WriteTimeout'
  }
}

/**
 * Waits for `write` (which must not reject: it reports its own failure) at most
 * `ms`, then calls `late` with a WriteTimeout and returns, leaving it running.
 */
export async function waitAtMost(write: Promise<void>, ms: number, what: string, late: (err: WriteTimeout) => void): Promise<void> {
  let timer: NodeJS.Timeout | undefined
  const timedOut = new Promise<'late'>((resolve) => {
    timer = setTimeout(() => resolve('late'), ms)
  })
  try {
    if ((await Promise.race([write, timedOut])) === 'late') late(new WriteTimeout(what, ms))
  } finally {
    clearTimeout(timer)
  }
}
