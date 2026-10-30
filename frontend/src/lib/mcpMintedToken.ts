import { useSyncExternalStore } from 'react'

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
