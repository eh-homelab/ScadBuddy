const KEY = 'scadbuddy:stale-chunk-reload'

/** A message naming the module that failed: an absolute URL or a `.js` path. */
const NAMES_A_URL = /\w+:\/\/|\.js\b/

/** Matchers for the optional chunks whose import is in flight. */
const optionalLoads = new Set<RegExp>()

/**
 * Runs the dynamic import of a chunk the page can do without (the tracing SDK). Its
 * failure is often not staleness (blockers match names like `tracing-<hash>.js`), and a
 * reload would lose the user's first edits and fail again, so a `vite:preloadError`
 * about it does not reload. Vite dispatches that event before the import rejects, with
 * the error as `payload`; Chromium's and Firefox's messages name the failing URL, which
 * `chunk` must match. Safari's (`Importing a module script failed.`) names none, so while
 * an optional load is in flight an error naming no URL is taken as its failure too: a
 * stale chunk failing in that same window skips one reload and recovers on the next. An
 * error naming any other chunk is treated as stale as before. The rejection still
 * reaches the caller.
 */
export async function loadOptionalChunk<T>(load: () => Promise<T>, chunk: RegExp): Promise<T> {
  optionalLoads.add(chunk)
  try {
    return await load()
  } finally {
    optionalLoads.delete(chunk)
  }
}

function isOptional(event: Event): boolean {
  const payload = (event as Event & { payload?: unknown }).payload
  const message = payload instanceof Error ? payload.message : String(payload ?? '')
  if (optionalLoads.size > 0 && !NAMES_A_URL.test(message)) return true
  return [...optionalLoads].some((chunk) => chunk.test(message))
}

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
    if (isOptional(event)) return
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
