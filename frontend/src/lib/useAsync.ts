import { useCallback, useEffect, useState } from 'react'

export interface AsyncState<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
  setData: (next: T) => void
}

interface Snapshot<T> {
  key: string
  data?: T
  error?: Error
}

/**
 * Runs `load` on mount and whenever `deps` change. No cache — the app is small
 * enough that a fetch per page beats carrying a query library.
 *
 * `loading` is derived from whether the settled snapshot belongs to the current
 * key, so nothing is set synchronously inside the effect.
 */
export function useAsync<T>(load: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [nonce, setNonce] = useState(0)
  const key = `${JSON.stringify(deps)}#${nonce}`
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({ key: '' })

  useEffect(() => {
    let cancelled = false
    load()
      .then((data) => {
        if (!cancelled) setSnapshot({ key, data })
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setSnapshot({ key, error: cause instanceof Error ? cause : new Error(String(cause)) })
        }
      })
    return () => {
      cancelled = true
    }
    // `load` is a fresh closure every render; `key` encodes its real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  const setData = useCallback((data: T) => setSnapshot((s) => ({ ...s, data, error: undefined })), [])

  const settled = snapshot.key === key
  return {
    data: settled ? snapshot.data : undefined,
    error: settled ? snapshot.error : undefined,
    loading: !settled,
    reload,
    setData,
  }
}
