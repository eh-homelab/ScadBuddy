import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PrintChoices, ProcessValues } from '../api/types'

/**
 * #2998 — every value of the process preset `choices` resolve to, read only while
 * `wanted` (a "Slicer settings" list was opened) and again when the choices change.
 * `values` is `null` until read, or when they could not be read (`unread` says why).
 */
export function useProcessValues(wanted: boolean, choices: PrintChoices | null) {
  const key = wanted && choices ? JSON.stringify(choices) : null
  const [read, setRead] = useState<{ key: string; answer: ProcessValues | null; error: string | null } | null>(null)
  useEffect(() => {
    if (key === null) return
    let live = true
    api
      .processValues(JSON.parse(key) as PrintChoices)
      .then((answer) => live && setRead({ key, answer, error: null }))
      .catch((cause: unknown) =>
        live && setRead({ key, answer: null, error: cause instanceof Error ? cause.message : String(cause) }),
      )
    return () => {
      live = false
    }
  }, [key])
  const current = read !== null && read.key === key ? read : null
  const answer = current?.answer ?? null
  return {
    values: answer?.resolved ? (answer.values ?? {}) : null,
    unread: current === null ? null : (current.error ?? (answer?.resolved ? null : (answer?.reason ?? 'not read'))),
    loading: key !== null && current === null,
  }
}
