import type { Sql } from 'postgres'
import type { InputEntry, Owner } from '../sessions/protocol.js'

// The durable entries' projection, `ai_pending_input` (durable-agents spec §6.6, "Two
// reads, two sources"): what GET /api/v1/ai/pending-input, the badge's read, unions
// with the classic stores in one Postgres read, never a Query per workflow. The
// agent-durable worker's open_input / resolve_input write it; this only reads.
//
// An entry past its `expires_at` is still listed while its run's worker is down (it
// resolves it when it returns), marked `expiring`: the badge does not count it.

export type PendingInputEntry = InputEntry & {
  /** A durable entry past its timer whose worker has not yet resolved it; not counted as waiting. */
  expiring?: true
}

/** A session as the gate needs it: its mode, and who owns and started it. */
export type GateSession = {
  mode: 'classic' | 'durable'
  owner: Owner
  creator: Pick<Owner, 'kind' | 'id'>
}

type Row = {
  request_id: string
  kind: 'approval' | 'answer'
  session_id: string
  tool: string
  summary: string
  input_hash: string | null
  prompt: string
  requested_by: Owner | null
  responders: ('browser' | 'grant')[]
  attention: { reason: string; on_timeout: string | null } | null
  created_at: Date
  expires_at: Date
  expiring: boolean
}

/** Which entries: of one session; of sessions a principal owns or started; approvals of any session. */
export type ProjectionFilter = {
  sessionId?: string
  /** Only sessions this principal owns or started… */
  ownedBy?: Pick<Owner, 'kind' | 'id'>
  /** …except approvals, which a grant holder sees in every session. */
  allApprovals?: boolean
}

export class PendingProjection {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async entries(filter: ProjectionFilter = {}): Promise<PendingInputEntry[]> {
    const sql = this.#sql
    const owner = filter.ownedBy
    const rows = await sql<Row[]>`
      SELECT p.request_id, p.kind, p.session_id, p.tool, p.summary, p.input_hash, p.prompt, p.requested_by,
             p.responders, p.attention, p.created_at, p.expires_at, (p.expires_at <= now()) AS expiring
      FROM ai_pending_input p JOIN ai_sessions s ON s.id = p.session_id
      WHERE (${filter.sessionId ?? null}::uuid IS NULL OR p.session_id = ${filter.sessionId ?? null}::uuid)
        AND (${owner === undefined}
             OR (${filter.allApprovals === true} AND p.kind = 'approval')
             OR (s.owner_kind = ${owner?.kind ?? null} AND s.owner_id = ${owner?.id ?? null})
             OR (s.creator_kind = ${owner?.kind ?? null} AND s.creator_id = ${owner?.id ?? null}))
      ORDER BY p.created_at, p.request_id
      LIMIT 500`
    return rows.map((r) => ({
      id: r.request_id,
      kind: r.kind,
      session_id: r.session_id,
      tool: r.tool,
      summary: r.summary,
      input_hash: r.input_hash,
      prompt: r.prompt,
      requested_by: r.requested_by,
      responders: r.responders,
      created_at: r.created_at.toISOString(),
      expires_at: r.expires_at.toISOString(),
      ...(r.attention ? { attention: r.attention } : {}),
      ...(r.expiring ? { expiring: true as const } : {}),
    }))
  }

  /** The session, or undefined when there is none. */
  async session(id: string): Promise<GateSession | undefined> {
    const [row] = await this.#sql<
      { mode: GateSession['mode']; owner_kind: Owner['kind']; owner_id: string; owner_label: string; creator_kind: Owner['kind']; creator_id: string }[]
    >`SELECT mode, owner_kind, owner_id, owner_label, creator_kind, creator_id FROM ai_sessions WHERE id = ${id}`
    return row
      ? {
          mode: row.mode,
          owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label },
          creator: { kind: row.creator_kind, id: row.creator_id },
        }
      : undefined
  }
}
