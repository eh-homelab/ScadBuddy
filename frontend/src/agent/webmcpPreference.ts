import { useSyncExternalStore } from 'react'

/**
 * Whether this browser's built-in agent may use ScadBuddy's tools through WebMCP.
 * Off unless the user turns it on: AI design spec §8.5 has an external agent pair
 * before it drives a tab, and a browser's own agent is external to ScadBuddy.
 *
 * Per browser, in localStorage. Storage that throws (private mode, blocked site data, a
 * sandboxed iframe) reads as off and a write there only lasts until reload.
 */
export const WEBMCP_STORAGE_KEY = 'scadbuddy.webmcp'

const listeners = new Set<() => void>()
let fallback = false

export function isWebMcpEnabled(): boolean {
  try {
    return window.localStorage.getItem(WEBMCP_STORAGE_KEY) === 'on'
  } catch {
    return fallback
  }
}

export function setWebMcpEnabled(enabled: boolean) {
  fallback = enabled
  try {
    if (enabled) window.localStorage.setItem(WEBMCP_STORAGE_KEY, 'on')
    else window.localStorage.removeItem(WEBMCP_STORAGE_KEY)
  } catch {
    // Kept in memory for this page only.
  }
  for (const listener of listeners) listener()
}

/** Changes from this tab, and from another tab of the same browser (`storage`). */
export function subscribeWebMcp(listener: () => void): () => void {
  listeners.add(listener)
  const onStorage = (event: StorageEvent) => {
    if (event.key === WEBMCP_STORAGE_KEY || event.key === null) listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

export function useWebMcpEnabled(): boolean {
  return useSyncExternalStore(subscribeWebMcp, isWebMcpEnabled, () => false)
}
