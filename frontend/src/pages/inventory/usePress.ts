import { useRef, useState } from 'react'
import { ApiError, newRequestId } from '../../api/client'
import type { OperationAccepted } from '../../api/types'
import { inventoryProblemMessage } from '../../lib/problems'
import type { WriteOptions } from './api'

/**
 * #6949 K1 — one inventory write's press. `busy` is true until the server answers or accepts
 * the write (its 202); `running` holds the accepted operation until the write settles, for
 * `StillWorking`. A press of the same write (`what`) while it runs sends the first press's
 * key again, so the server attaches it to the same operation and does the write once; a
 * press of another write gets a key of its own.
 */
export function usePress() {
  const [busy, setBusy] = useState(false)
  const [running, setRunning] = useState<OperationAccepted | null>(null)
  const keys = useRef(new Map<string, string>())

  async function press<T>(write: (options: WriteOptions) => Promise<T>, what = ''): Promise<T> {
    const key = keys.current.get(what) ?? newRequestId()
    keys.current.set(what, key)
    setBusy(true)
    try {
      return await write({
        key,
        onAccepted: (operation) => {
          setRunning(operation)
          setBusy(false)
        },
      })
    } finally {
      keys.current.delete(what)
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
