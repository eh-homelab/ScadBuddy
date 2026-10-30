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

// The Tokens card, while it is mounted: `undefined` is no card on the page. With a card,
// `mode` is the Auth mode it resolved, or `undefined` until it knows. The Connect snippet
// uses that mode in place of reading it a second time, and reads for itself only without
// the card (#5016).
type TokensCard = { mode: McpAuthMode | null | undefined }
let card: TokensCard | undefined
const authListeners = new Set<() => void>()

export function setTokensCard(next: TokensCard | undefined): void {
  if (next?.mode === card?.mode && Boolean(next) === Boolean(card)) return
  card = next
  for (const listener of authListeners) listener()
}

/** Whether a Tokens card is mounted now, read when a load runs, not when it rendered. */
export function tokensCardMounted(): boolean {
  return card !== undefined
}

export function useTokensCard(): TokensCard | undefined {
  return useSyncExternalStore(
    (listener) => {
      authListeners.add(listener)
      return () => authListeners.delete(listener)
    },
    () => card,
  )
}

/** For tests: forget everything the store holds, so one test's token never reaches the next. */
export function resetMcpMinted(): void {
  token = null
  card = undefined
}
