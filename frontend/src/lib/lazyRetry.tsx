import { lazy, type ComponentType } from 'react'

/** Renews each page whose import was rejected, until `retryFailedPages` runs them. */
const failed = new Set<() => void>()

/**
 * #3241 — `lazy()`, except that a rejected import is not kept for good. React caches the
 * rejected promise, so going back to a page whose chunk failed would show the same error
 * even once the network is back. `retryFailedPages`, called by the page boundary once it
 * has caught the failure, gives each such page a fresh `lazy()`: the boundary's reset or
 * a later visit tries the import again. Not sooner: renewing it while React retries the
 * render that rejected would loop for as long as the chunk stays broken.
 *
 * Whether that retry reaches the network is the browser's call: some keep a failed
 * `import()` of a URL in their module map and reject a second one at once. There the
 * stale-chunk reload (`lib/staleChunks.ts`), or PageFailed's full page reload, is still
 * what recovers the page (#3753).
 */
export function lazyRetry<P extends object>(
  load: () => Promise<{ default: ComponentType<P> }>,
): ComponentType<P> {
  let current: ComponentType<P>
  const renew = () => {
    current = lazy(attempt)
  }
  const attempt = (): Promise<{ default: ComponentType<P> }> =>
    load().catch((error: unknown) => {
      failed.add(renew)
      throw error
    })
  renew()
  function Retrying(props: P) {
    const Page = current
    return <Page {...props} />
  }
  return Retrying
}

/** Give every page whose import was rejected a fresh import on its next mount. */
export function retryFailedPages(): void {
  for (const renew of failed) renew()
  failed.clear()
}
