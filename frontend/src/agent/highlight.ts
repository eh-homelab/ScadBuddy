import { AgentToolError } from './types'

export const TOUCH_CLASS = 'sb-agent-touch'
export const TOUCH_MS = 1200

const timers = new WeakMap<Element, ReturnType<typeof setTimeout>>()

function reducedMotion(): boolean {
  return typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false
}

/**
 * Shows the user what the agent just touched: the element scrolls into view and gets a
 * short outline (`.sb-agent-touch` in index.css, which drops the pulse under
 * `prefers-reduced-motion`). Purely visual; the change itself went through state.
 */
export function touch(element: Element | null | undefined) {
  if (!element) return
  if (typeof element.scrollIntoView === 'function') {
    element.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' })
  }
  const previous = timers.get(element)
  if (previous) clearTimeout(previous)
  element.classList.add(TOUCH_CLASS)
  timers.set(
    element,
    setTimeout(() => {
      element.classList.remove(TOUCH_CLASS)
      timers.delete(element)
    }, TOUCH_MS),
  )
}

/** Touch `find()`'s element once React has committed the change that shows it. */
export function touchAfterRender(find: () => Element | null | undefined) {
  setTimeout(() => touch(find()), 0)
}

/**
 * Polls `read` until it returns something other than `undefined`. Handlers use it to
 * wait for React to catch up with a change (a debounce, a render, a check) — reading the
 * page's own latest state, so the wait ends on exactly what the user sees.
 */
export async function waitFor<T>(
  read: () => T | undefined,
  { timeout, what, interval = 50 }: { timeout: number; what: string; interval?: number },
): Promise<T> {
  const started = Date.now()
  for (;;) {
    const value = read()
    if (value !== undefined) return value
    if (Date.now() - started >= timeout) {
      throw new AgentToolError('timeout', `Gave up after ${timeout} ms waiting for ${what}.`)
    }
    await new Promise((resolve) => setTimeout(resolve, interval))
  }
}

/**
 * Waits for React to commit a change a handler just asked for, so the tool's answer —
 * and the caller's next tool — see the page as the user now does.
 */
export function committed(check: () => boolean, what = 'the page to update'): Promise<true> {
  return waitFor(() => (check() ? true : undefined), { timeout: 2000, what, interval: 10 })
}
