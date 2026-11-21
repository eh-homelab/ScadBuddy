import { type CrudTarget, crudOf } from '../api/crudStores.js'
import { type PairingStore, REQUEST_TTL_MS } from '../bridge/pairings.js'
import type { AttentionNotice } from './dispatcher.js'

// #6399: the requests that wait on a person outside the agent's pending input, for Web Push
// and the channels (dispatcher.ts `waiting`). The flow grants are the API's: read through its
// CRUD route (backend api/crud_agent_notify.py `push_waiting`, area `notify`), by id only. The
// pairing requests are this service's own (bridge/pairings.ts). Each notice's id is the one the
// page's "Needs you" gives it (frontend `lib/needsYou.ts`), so a push and the page's own
// notification share a tag.

/** The flow grants waiting for a person, as the API lists them; none until it serves area `notify`. */
export function crudGrantRequests(target: CrudTarget, available: () => boolean = () => true): () => Promise<AttentionNotice[]> {
  const call = crudOf(target)
  return async () => {
    if (!available()) return []
    const { requests } = await call<{ requests: { id: string; reason: string; since: string }[] }>('the flow grants waiting', '/push/waiting')
    return requests.map((r) => ({ id: r.id, sessionId: null, reason: r.reason, since: Date.parse(r.since) }))
  }
}

/**
 * Takes a timed poll's lease through the API (backend `push_poll_lease`, area `notify`): one
 * row per name, so one replica a window polls. Until the API serves area `notify`, every
 * replica polls (each send is still claimed once).
 */
export function crudPollLease(target: CrudTarget, available: () => boolean = () => true): (name: string, seconds: number) => Promise<boolean> {
  const call = crudOf(target)
  return async (name, seconds) => {
    if (!available()) return true
    const { held } = await call<{ held: boolean }>('the poll lease', '/push/poll-lease', { method: 'POST', body: { name, seconds } })
    return held
  }
}

/** The pairing requests waiting for a person to type their code. */
export function pairingRequests(store: Pick<PairingStore, 'pending'>): () => Promise<AttentionNotice[]> {
  // A request started REQUEST_TTL_MS before it expires.
  return async () =>
    (await store.pending()).map((p) => ({ id: `pairing:${p.id}`, sessionId: null, reason: 'pairing', since: p.expiresAt.getTime() - REQUEST_TTL_MS }))
}

/** Every source's requests; a source that fails is left out, and the rest still go. */
export function waitingNotices(
  sources: readonly (() => Promise<AttentionNotice[]>)[],
  log: (message: string) => void = console.error,
): () => Promise<AttentionNotice[]> {
  return async () => {
    const read = await Promise.allSettled(sources.map((source) => source()))
    return read.flatMap((r) => {
      if (r.status === 'fulfilled') return r.value
      log(`notifications: a waiting-request read failed (${(r.reason as Error).message})`)
      return []
    })
  }
}
