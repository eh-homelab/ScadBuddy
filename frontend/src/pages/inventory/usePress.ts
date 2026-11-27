import { useRef, useState } from 'react'
import { ApiError, newRequestId } from '../../api/client'
import type { OperationAccepted } from '../../api/types'
import { inventoryProblemMessage } from '../../lib/problems'
import type { WriteOptions } from './api'

/** `value` as JSON with every object's keys in order, so one body always reads the same. */
const stable = (value: unknown): string =>
  JSON.stringify(value, (_key, part: unknown) =>
    part && typeof part === 'object' && !Array.isArray(part) ? Object.fromEntries(Object.entries(part).sort(([a], [b]) => a.localeCompare(b))) : part,
  ) ?? ''

/** A refusal: the server answered the write with a 4xx problem, so it did not run (or `RESULT_GONE`). */
const refused = (cause: unknown) => cause instanceof ApiError && cause.status >= 400 && cause.status < 500

/**
 * #6949 K1 — one inventory write's press. `busy` is true until the server answers or accepts
 * the write (its 202); `running` holds the accepted operation until the write settles, for
 * `StillWorking`. `what` is the write and its body: a press of the same `what` sends the
 * earlier press's key again, so the server attaches it to the same operation and does the
 * write once; another body (a form edited after its 202) gets a key of its own. A key is
 * dropped once its write succeeded or was refused; after an answer that never came (or a
 * follow that stopped) it is kept, so "try again" cannot do the write twice (`command`).
 */
export function usePress() {
  const [busy, setBusy] = useState(false)
  const [running, setRunning] = useState<OperationAccepted | null>(null)
  const keys = useRef(new Map<string, string>())

  async function press<T>(write: (options: WriteOptions) => Promise<T>, what: unknown = ''): Promise<T> {
    const id = stable(what)
    const key = keys.current.get(id) ?? newRequestId()
    keys.current.set(id, key)
    setBusy(true)
    try {
      const done = await write({
        key,
        onAccepted: (operation) => {
          setRunning(operation)
          setBusy(false)
        },
      })
      keys.current.delete(id)
      return done
    } catch (cause) {
      if (refused(cause)) keys.current.delete(id)
      throw cause
    } finally {
      setRunning(null)
      setBusy(false)
    }
  }

  return { busy, running, press }
}

/** The problem's message for a person: a known inventory code's sentence, else the server's detail. */
export const failed = (cause: unknown, fallback: string) => (cause instanceof ApiError ? (inventoryProblemMessage(cause.problem) ?? cause.detail) : fallback)

/** The Activities tab's view of an operation, as `StillWorking`'s link. */
export const activityHref = (id: string) => `/activity?q=${encodeURIComponent(id)}`
