import { useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { Settings } from '../api/types'

/**
 * #6232 — a link to ScadBuddy that the UI shows, links or copies uses the host the user
 * is on: `window.location.origin`. The browser's own origin is the only host used, so no
 * request header becomes a link. Inside Bambuddy's frame the page is still on
 * ScadBuddy's origin (the sandbox has `allow-same-origin`), so this holds there too.
 *
 * `public_url` stays the server's link for what leaves the browser (Bambuddy's sidebar
 * entry, the edit link on a sent file, mail, MCP clients). A link the backend or the
 * agent built on it, or on one of `SCADBUDDY_ALLOWED_ORIGINS`, is one of ScadBuddy's own
 * origins: {@link appUrl} moves it to the current one, as `bambuddyLink` moves
 * Bambuddy's links.
 */
type LinkSettings = Pick<Settings, 'public_url' | 'bootstrap'>

let known: string[] = []
let loaded = false
const listeners = new Set<() => void>()

function originOf(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url.trim())
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.origin : null
  } catch {
    return null
  }
}

/** ScadBuddy's own origins: `public_url`'s and each of `SCADBUDDY_ALLOWED_ORIGINS`. */
export function appOrigins(settings: LinkSettings | null | undefined): string[] {
  const allowed = settings?.bootstrap?.find((entry) => entry.name === 'allowed_origins')?.value ?? ''
  const origins = [settings?.public_url, ...allowed.split(',')].map(originOf)
  return [...new Set(origins.filter((origin): origin is string => origin !== null))]
}

/** The settings page calls this after a save, as the shell does once loaded. */
export function setAppLinks(settings: LinkSettings): void {
  loaded = true
  known = appOrigins(settings)
  for (const listener of listeners) listener()
}

/** Tests only. */
export function resetAppLinks(): void {
  known = []
  loaded = false
  for (const listener of listeners) listener()
}

function loadAppLinks(): void {
  if (loaded) return
  loaded = true
  api.getSettings().then(setAppLinks, () => {
    loaded = false
  })
}

/** Mounted once, by the app shell. */
export function useLoadAppLinks(): void {
  useEffect(loadAppLinks, [])
}

/**
 * The URL to show for a ScadBuddy link:
 *
 * - a path (`/m/x?preset=1`) is the current origin plus the path;
 * - an absolute URL on one of ScadBuddy's own origins keeps its path, query and hash
 *   and gets the current origin;
 * - any other URL, or text that is not a URL, is left as it is.
 */
export function appUrl(pathOrUrl: string, origins: string[] = known, here: string = window.location.origin): string {
  if (pathOrUrl.startsWith('/') && !pathOrUrl.startsWith('//') && !pathOrUrl.startsWith('/\\')) {
    return `${here}${pathOrUrl}`
  }
  let parsed: URL
  try {
    parsed = new URL(pathOrUrl)
  } catch {
    return pathOrUrl
  }
  if (parsed.origin === here || !origins.includes(parsed.origin)) return pathOrUrl
  return `${here}${parsed.pathname}${parsed.search}${parsed.hash}`
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** {@link appUrl} in a component, rendered again once the known origins load. */
export function useAppUrl(pathOrUrl: string): string {
  const origins = useSyncExternalStore(subscribe, () => known)
  return appUrl(pathOrUrl, origins)
}
