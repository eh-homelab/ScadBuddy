import { useSyncExternalStore } from 'react'
import type { McpAuthMode } from '../api/mcpTokens'

// #781 — the token the Tokens card just minted, for the Connect snippet beside it
// (#1910). Held only until Done or until the card leaves the page, so the plaintext
// lives no longer than it did when the snippet sat inside that card.

let token: string | null = null
const listeners = new Set<() => void>()

export function setMintedMcpToken(next: string | null): void {
  if (next === token) return
  token = next
  for (const listener of listeners) listener()
}

export function useMintedMcpToken(): string | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => token,
  )
}

// The Auth mode the Tokens card resolved, while it is mounted: the Connect snippet uses it
// in place of reading the mode a second time, and reads for itself only without the card.
let authMode: { mode: McpAuthMode | null } | undefined
const authListeners = new Set<() => void>()

export function setTokensCardAuthMode(next: McpAuthMode | null | undefined): void {
  const value = next === undefined ? undefined : { mode: next }
  if (value?.mode === authMode?.mode && Boolean(value) === Boolean(authMode)) return
  authMode = value
  for (const listener of authListeners) listener()
}

export function useTokensCardAuthMode(): { mode: McpAuthMode | null } | undefined {
  return useSyncExternalStore(
    (listener) => {
      authListeners.add(listener)
      return () => authListeners.delete(listener)
    },
    () => authMode,
  )
}

/** For tests: forget everything the store holds, so one test's token never reaches the next. */
export function resetMcpMinted(): void {
  token = null
  authMode = undefined
}
