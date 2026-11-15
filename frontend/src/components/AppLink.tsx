import type { AnchorHTMLAttributes } from 'react'
import { useAppUrl } from '../lib/appLinks'

/**
 * #6232 — an anchor whose `href` is a ScadBuddy path or URL, shown on the host the user
 * is on (`lib/appLinks.ts` `appUrl`). Any other URL is passed through unchanged.
 */
export function AppLink({ href, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  return <a href={useAppUrl(href)} {...rest} />
}
