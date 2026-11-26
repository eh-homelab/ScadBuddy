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

/** Each URL's loads that may still ask again: each with the timer of its wait, when one
 * waits, and whether it was stopped, which the try in flight reads when it fails. */
interface Load {
  timer?: ReturnType<typeof setTimeout>
  stopped: boolean
  fail: () => void
}
const loads = new Map<string, Set<Load>>()

/**
 * Stops every load of ``url`` that may still ask again, whether it waits on its timer or its
 * try is in flight: each fails at once, and a try in flight that fails asks no more. So a
 * viewer that unmounts or moves to another URL never keeps asking for the old one. It stops
 * another viewer's load of the same URL too: `useLoader` shares one load per URL between
 * viewers, and the cache it fails is shared as well. True when one was stopped: the loader's
 * cache then holds that failure, which the caller clears.
 */
export function stopRetries(url: string): boolean {
  const running = loads.get(url)
  if (running === undefined) return false
  loads.delete(url)
  for (const load of running) {
    load.stopped = true
    clearTimeout(load.timer)
    load.fail()
  }
  return true
}

/**
 * The viewers' mesh loader: a 502, 503 or 504 is tried again with a backoff, since the first
 * read of a done render can find its files still on their way from the store (#6968). Any
 * other failure, and the last try's, is the caller's error; `stopRetries` ends a load.
 */
export class PreviewLoader extends GLTFLoader {
  override load(
    url: string,
    onLoad: Parameters<GLTFLoader['load']>[1],
    onProgress?: Parameters<GLTFLoader['load']>[2],
    onError?: Parameters<GLTFLoader['load']>[3],
  ): void {
    let tries = 0
    const load: Load = {
      stopped: false,
      fail: () => onError?.(new Error(`The preview's load was stopped: ${url}`)),
    }
    const done = () => {
      const running = loads.get(url)
      running?.delete(load)
      if (running?.size === 0) loads.delete(url)
    }
    const attempt = () =>
      super.load(
        url,
        (gltf) => {
          if (load.stopped) return
          done()
          onLoad(gltf)
        },
        onProgress,
        (error) => {
          if (load.stopped) return
          const delay = RETRY_DELAYS_MS[tries]
          if (delay === undefined || !passes(error)) {
            done()
            onError?.(error)
            return
          }
          tries += 1
          load.timer = setTimeout(attempt, delay)
        },
      )
    loads.set(url, (loads.get(url) ?? new Set()).add(load))
    attempt()
  }
}
