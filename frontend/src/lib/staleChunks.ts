const KEY = 'scadbuddy:stale-chunk-reload'

/**
 * Reloads the page when a lazy chunk fails to load (#395).
 *
 * A page loaded before a deploy (Bambuddy's sidebar frame stays open for days) still
 * names the old build's chunk hashes, which the server no longer has. Vite fires
 * `vite:preloadError` for a failed dynamic import; a reload fetches the current
 * `index.html` and with it the current chunk names.
 *
 * The guard is the build, not the clock. Before reloading, the build that failed is
 * kept in sessionStorage; a failure in that same build again means the reload brought
 * nothing new, so the error surfaces instead. However long a reload takes, a deploy
 * costs at most two reloads (the stale build, then a broken new one), and a later
 * deploy in the same tab still recovers.
 *
 * `build` names the running build. In a production bundle this module's URL is the
 * hashed entry chunk, which changes whenever any chunk it can load changes.
 *
 * Returns a function that removes the listener.
 */
export function installStaleChunkReload(
  target: Window = window,
  reload: () => void = () => target.location.reload(),
  build: string = import.meta.url,
): () => void {
  const onPreloadError = (event: Event) => {
    if (read(target) === build) return
    if (!write(target, build)) return
    event.preventDefault()
    reload()
  }
  target.addEventListener('vite:preloadError', onPreloadError)
  return () => target.removeEventListener('vite:preloadError', onPreloadError)
}

function read(target: Window): string | null {
  try {
    return target.sessionStorage.getItem(KEY)
  } catch {
    return null
  }
}

/** Without storage there is no loop guard, so a reload is not risked at all. */
function write(target: Window, build: string): boolean {
  try {
    target.sessionStorage.setItem(KEY, build)
    return true
  } catch {
    return false
  }
}
