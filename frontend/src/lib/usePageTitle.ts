import { useEffect, useState, type RefObject } from 'react'

export const APP_TITLE = 'ScadBuddy'

/**
 * The page's title for the tab (#2454): its first `<h1>`, as `Library · ScadBuddy`, so
 * each route is named in history, in tabs and to a screen reader on navigation. Read
 * from the page rather than declared by it, so a page that renders its heading late (a
 * model's name once it loads) or a new page is named with nothing to add.
 *
 * The heading's whole `textContent` is taken, nested markup included (WorkflowRunPage's
 * `name vN` link): a badge or button put inside a page's `<h1>` changes its tab title too
 * (usePageTitle.test.tsx pins it). Rendered Markdown (`data-markdown`) is skipped, so a
 * README's own heading never names the tab (#3392).
 *
 * `main` must be the always-rendered `<main>`: the effect runs once, on mount, so a
 * `<main>` rendered later would leave the title `ScadBuddy` with nothing to say why.
 */
export function usePageTitle(main: RefObject<HTMLElement | null>): string {
  const [heading, setHeading] = useState('')
  useEffect(() => {
    const element = main.current
    if (!element) return
    const read = () => setHeading(pageHeading(element)?.textContent?.replace(/\s+/g, ' ').trim() ?? '')
    read()
    // Every change under <main> reads the heading again, the Customize page's frequent
    // ones included. Considered and kept: one querySelector, and React drops a setState
    // with the same string, so an unchanged heading costs no render.
    const observer = new MutationObserver(read)
    observer.observe(element, { childList: true, subtree: true, characterData: true })
    return () => observer.disconnect()
  }, [main])
  return heading ? `${heading} · ${APP_TITLE}` : APP_TITLE
}

function pageHeading(main: HTMLElement): HTMLHeadingElement | undefined {
  for (const h1 of main.querySelectorAll('h1')) if (!h1.closest('[data-markdown]')) return h1
  return undefined
}
