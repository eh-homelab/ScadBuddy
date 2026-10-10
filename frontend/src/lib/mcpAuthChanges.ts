import { useSyncExternalStore } from 'react'

// #1921 — Settings saved a new MCP auth mode: every McpAuthBanner on the page (the one
// atop Settings, and the assistant panel's, which stays mounted while hidden) reads
// the mode again.

let version = 0
const listeners = new Set<() => void>()

export function mcpAuthChanged(): void {
  version += 1
  for (const listener of listeners) listener()
}

/** A number that changes each time the mode is saved, for an effect's dependencies. */
export function useMcpAuthVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => version,
  )
}
