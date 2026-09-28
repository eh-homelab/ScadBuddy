const KEY = 'scadbuddy:stale-chunk-reload'
/** A second failure this soon after a reload is not a stale page; reloading again would loop. */
const LOOP_WINDOW_MS = 10_000

/**
 * Reloads the page when a lazy chunk fails to load (#395).
 *
 * A page loaded before a deploy (Bambuddy's sidebar frame stays open for days) still
 * names the old build's chunk hashes, which the server no longer has. Vite fires
 * `vite:preloadError` for a failed dynamic import; a reload fetches the current
 * `index.html` and with it the current chunk names. The last reload's time is kept in
 * sessionStorage so a chunk that is missing from the *new* build too surfaces its error
 * instead of reloading forever, while a later deploy in the same tab still recovers.
 *
 * Returns a function that removes the listener.
 */
export function installStaleChunkReload(
  target: Window = window,
  reload: () => void = () => target.location.reload(),
  now: () => number = Date.now,
): () => void {
  const onPreloadError = (event: Event) => {
    const last = read(target)
    if (last !== null && now() - last < LOOP_WINDOW_MS) return
    if (!write(target, now())) return
    event.preventDefault()
    reload()
  }
  target.addEventListener('vite:preloadError', onPreloadError)
  return () => target.removeEventListener('vite:preloadError', onPreloadError)
}

function read(target: Window): number | null {
  try {
    const value = Number(target.sessionStorage.getItem(KEY))
    return value > 0 ? value : null
  } catch {
    return null
  }
}

/** Without storage there is no loop guard, so a reload is not risked at all. */
function write(target: Window, time: number): boolean {
  try {
    target.sessionStorage.setItem(KEY, String(time))
    return true
  } catch {
    return false
  }
}
