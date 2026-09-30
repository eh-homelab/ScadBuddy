import { useEffect } from 'react'
import { api } from '../api/client'
import type { Settings } from '../api/types'
import { isEmbedded } from './embed'

/**
 * #775 — where a browser reaches Bambuddy. `bambuddy_url` is the server's address for
 * it, which may be one only the server can reach (an in-cluster Service), so links use
 * `bambuddy_web_urls` when set: the first is the default, and the others are more
 * hostnames of the same Bambuddy. The server builds its links on the default; a page
 * framed by one of the others moves them there, since that is the Bambuddy the user is
 * looking at.
 */
type LinkSettings = Pick<Settings, 'bambuddy_url' | 'bambuddy_web_urls'>

let current: string[] = []
let loaded = false

/** The web URLs, default first, without trailing slashes; else `bambuddy_url`. */
export function webUrls(settings: LinkSettings | null | undefined): string[] {
  const listed = (settings?.bambuddy_web_urls ?? '')
    .split(',')
    .map((url) => url.trim().replace(/\/+$/, ''))
    .filter(Boolean)
  if (listed.length > 0) return listed
  const api = settings?.bambuddy_url?.trim().replace(/\/+$/, '')
  return api ? [api] : []
}

/** The settings page calls this after a save, as the shell does once loaded. */
export function setBambuddyLinks(settings: LinkSettings): void {
  loaded = true
  current = webUrls(settings)
}

/** Tests only. */
export function resetBambuddyLinks(): void {
  current = []
  loaded = false
}

function loadBambuddyLinks(): void {
  if (loaded) return
  loaded = true
  api.getSettings().then(setBambuddyLinks, () => {
    loaded = false
  })
}

/** Mounted once, by the app shell. */
export function useLoadBambuddyLinks(): void {
  useEffect(loadBambuddyLinks, [])
}

/**
 * The origin of the page framing this one, or null at the top level. Bambuddy's
 * sandbox keeps the frame cross-origin to it, so this is what the browser reports:
 * `ancestorOrigins` (Chromium, WebKit), else the referrer.
 */
export function framingOrigin(embedded = isEmbedded()): string | null {
  if (!embedded) return null
  const ancestors = window.location.ancestorOrigins
  if (ancestors && ancestors.length > 0) return ancestors[0] ?? null
  try {
    return document.referrer ? new URL(document.referrer).origin : null
  } catch {
    return null
  }
}

function originOf(url: string): string | null {
  try {
    return new URL(url).origin
  } catch {
    return null
  }
}

/** The web URL a link should be built on: the framing one when listed, else the default. */
export function bambuddyBase(urls: string[] = current, parent = framingOrigin()): string | null {
  return (parent && urls.find((url) => originOf(url) === parent)) || urls[0] || null
}

/** `url`, built on the default web URL by the server, moved to {@link bambuddyBase}. */
export function bambuddyLink(url: string, urls: string[] = current, parent = framingOrigin()): string {
  const fallback = urls[0]
  const base = bambuddyBase(urls, parent)
  if (!fallback || !base || base === fallback) return url
  const rest = url.slice(fallback.length)
  if (!url.startsWith(fallback) || (rest !== '' && !/^[/?#]/.test(rest))) return url
  return base + rest
}
