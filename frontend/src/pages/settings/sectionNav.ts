import { useEffect, useRef, useState, type RefObject } from 'react'
import { useLocation, useNavigate } from 'react-router'

/** How long a deep link keeps its section in view while the sections below load (#2345). */
export const SETTLE_MS = 4000
/** Room left between the sticky nav and the section's top. */
const GAP = 8

/** The section a `#hash` names, decoded; a malformed escape is taken as written (#2113). */
export function sectionId(hash: string): string {
  const raw = hash.slice(1)
  try {
    return decodeURIComponent(raw)
  } catch {
    return raw
  }
}

/**
 * Scrolls only `pane` so `target` sits just under `nav` (#2345). `scrollIntoView` scrolls
 * every scrollable ancestor, the window too, which pushed the app header off the screen.
 */
export function scrollPaneTo(pane: HTMLElement, nav: HTMLElement | null, target: HTMLElement): void {
  const top =
    target.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop - (nav?.offsetHeight ?? 0) - GAP
  const next = Math.max(0, top)
  if (typeof pane.scrollTo === 'function') pane.scrollTo({ top: next })
  else pane.scrollTop = next
}

/** The last of `ids` whose section has reached the bottom of the sticky nav. */
export function sectionInView(pane: HTMLElement, nav: HTMLElement | null, ids: readonly string[]): string | null {
  const line = pane.getBoundingClientRect().top + (nav?.offsetHeight ?? 0) + GAP * 2
  let current: string | null = null
  for (const id of ids) {
    const section = document.getElementById(id)
    if (!section) continue
    if (section.getBoundingClientRect().top <= line) current = id
    else break
  }
  return current ?? ids[0] ?? null
}

/** Shows section `id` in the pane, and focuses its heading with `focus`; false when it is not there. */
function showSection(pane: HTMLElement | null, nav: HTMLElement | null, id: string, focus: boolean): boolean {
  const target = document.getElementById(id)
  if (!pane || !target) return false
  scrollPaneTo(pane, nav, target)
  if (focus) document.getElementById(`${id}-heading`)?.focus({ preventScroll: true })
  return true
}

/**
 * #2345, #2346 — the Settings section nav. A section link pushes its `#hash` (so the
 * address is a deep link, and Back returns to the previous section), scrolls only the
 * pane, and moves focus to the section's heading. A deep link is kept in view while the
 * sections above it finish loading, until the reader scrolls. `current` is the section
 * in view, for `aria-current`.
 */
export function useSectionNav(
  pane: RefObject<HTMLElement | null>,
  nav: RefObject<HTMLElement | null>,
  ids: readonly string[],
  loaded: boolean,
) {
  const { hash } = useLocation()
  const navigate = useNavigate()
  const focusNext = useRef(false)
  const [current, setCurrent] = useState<string | null>(null)

  useEffect(() => {
    if (!loaded || hash.length < 2) return
    const id = sectionId(hash)
    const focus = focusNext.current
    focusNext.current = false
    const element = pane.current
    if (!showSection(element, nav.current, id, focus)) return
    const content = element?.lastElementChild
    if (!element || !content || typeof ResizeObserver === 'undefined') return
    // The sections above it load on their own and push it down: follow it until the
    // reader takes over, or a short while has passed.
    const observer = new ResizeObserver(() => showSection(element, nav.current, id, false))
    observer.observe(content)
    const stop = () => observer.disconnect()
    const timer = window.setTimeout(stop, SETTLE_MS)
    const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'] as const
    for (const name of events) element.addEventListener(name, stop, { once: true, passive: true })
    return () => {
      stop()
      window.clearTimeout(timer)
      for (const name of events) element.removeEventListener(name, stop)
    }
  }, [loaded, hash, pane, nav])

  useEffect(() => {
    const element = pane.current
    if (!loaded || !element) return
    const update = () => setCurrent(sectionInView(element, nav.current, ids))
    update()
    element.addEventListener('scroll', update, { passive: true })
    // A section that loads above the one in view moves it without scrolling the pane.
    const content = element.lastElementChild
    const observer = content && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(update) : null
    if (content) observer?.observe(content)
    return () => {
      element.removeEventListener('scroll', update)
      observer?.disconnect()
    }
  }, [loaded, pane, nav, ids])

  /**
   * A section link's activation. With `inPlace` (the page has unsaved changes) it only
   * scrolls and focuses: a pushed `#hash` would land above the leave guard's sentinel
   * entry, so Back would pop into the sentinel and seem to do nothing.
   */
  const go = (id: string, { inPlace = false }: { inPlace?: boolean } = {}) => {
    if (inPlace || sectionId(hash) === id) showSection(pane.current, nav.current, id, true)
    else {
      focusNext.current = true
      navigate({ hash: `#${id}` })
    }
    setCurrent(id)
  }

  return { current, go }
}
