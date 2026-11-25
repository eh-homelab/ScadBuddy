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

/**
 * The viewers' mesh loader: a 502, 503 or 504 is tried again with a backoff, since the first
 * read of a done render can find its files still on their way from the store (#6968). Any
 * other failure, and the last try's, is the caller's error.
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
        setTimeout(attempt, delay)
      })
    attempt()
  }
}
