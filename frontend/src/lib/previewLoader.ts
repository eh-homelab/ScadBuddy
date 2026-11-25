import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

/** The waits before a preview that is not ready yet is asked for again (#6968). */
export const RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000]

/** A gateway's answer, or a server's "not ready yet": the preview may be there in a moment. */
const PASSING = new Set([502, 503, 504])

/** Whether ``error`` is three.js's `HttpError` (`FileLoader`) of a status that may pass. */
function passes(error: unknown): boolean {
  const response = (error as { response?: { status?: unknown } } | null)?.response
  return typeof response?.status === 'number' && PASSING.has(response.status)
}

/** Each URL's retry that waits on its timer, with the failure it ends with if stopped. */
const waiting = new Map<string, { timer: ReturnType<typeof setTimeout>; fail: () => void }>()

/**
 * Stops ``url``'s waiting retry, if one waits: its load fails at once, so a viewer that
 * unmounts or moves to another URL never keeps asking for the old one. True when one was
 * stopped: the loader's cache then holds that failure, which the caller clears.
 */
export function stopRetries(url: string): boolean {
  const entry = waiting.get(url)
  if (entry === undefined) return false
  waiting.delete(url)
  clearTimeout(entry.timer)
  entry.fail()
  return true
}

/**
 * The viewers' mesh loader: a 502, 503 or 504 is tried again with a backoff, since the first
 * read of a done render can find its files still on their way from the store (#6968). Any
 * other failure, and the last try's, is the caller's error; `stopRetries` ends a wait.
 */
export class PreviewLoader extends GLTFLoader {
  override load(
    url: string,
    onLoad: Parameters<GLTFLoader['load']>[1],
    onProgress?: Parameters<GLTFLoader['load']>[2],
    onError?: Parameters<GLTFLoader['load']>[3],
  ): void {
    let tries = 0
    const attempt = () =>
      super.load(url, onLoad, onProgress, (error) => {
        const delay = RETRY_DELAYS_MS[tries]
        if (delay === undefined || !passes(error)) {
          onError?.(error)
          return
        }
        tries += 1
        const timer = setTimeout(() => {
          waiting.delete(url)
          attempt()
        }, delay)
        waiting.set(url, { timer, fail: () => onError?.(error) })
      })
    attempt()
  }
}
