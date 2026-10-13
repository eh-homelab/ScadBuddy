import { useEffect, useState, type RefObject } from 'react'

export const APP_TITLE = 'ScadBuddy'

/**
 * The page's title for the tab (#2454): its first `<h1>`, as `Library · ScadBuddy`, so
 * each route is named in history, in tabs and to a screen reader on navigation. Read
 * from the page rather than declared by it, so a page that renders its heading late (a
 * model's name once it loads) or a new page is named with nothing to add.
 */
export function usePageTitle(main: RefObject<HTMLElement | null>): string {
  const [heading, setHeading] = useState('')
  useEffect(() => {
    const element = main.current
    if (!element) return
    const read = () => setHeading(element.querySelector('h1')?.textContent?.replace(/\s+/g, ' ').trim() ?? '')
    read()
    const observer = new MutationObserver(read)
    observer.observe(element, { childList: true, subtree: true, characterData: true })
    return () => observer.disconnect()
  }, [main])
  return heading ? `${heading} · ${APP_TITLE}` : APP_TITLE
}
