import { type Owner, sameOwner } from '../sessions/protocol.js'
import type { Role } from './validate.js'

// Who is answering, as the respond route decides it (durable-agents spec §6.6,
// "Authorization is the route's"): an Update validator can read neither Postgres nor
// a grant, so the route reads the session's current owner and the principal's grant
// and passes the outcome as `role`. Ownership wins: a non-browser principal that owns
// or started the session is `owner`, never `grant`, even holding the grant, so it can
// never approve a call in its own session (as ApprovalService.authorize refuses).
// Undefined: the principal may not answer anything here.

export function roleOf(
  principal: Pick<Owner, 'kind' | 'id'>,
  session: { owner: Pick<Owner, 'kind' | 'id'>; creator: Pick<Owner, 'kind' | 'id'> } | null,
  hasGrant: boolean,
): Role | undefined {
  if (principal.kind === 'browser') return 'browser'
  if (session && (sameOwner(principal, session.owner) || sameOwner(principal, session.creator))) return 'owner'
  return hasGrant ? 'grant' : undefined
}
