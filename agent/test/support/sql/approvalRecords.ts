import type { Sql } from 'postgres'
import type { FlowRunReads } from '../../../src/flows/backendReads.js'
import { RecordedApprovals, RowSessionOwners } from '../../../src/temporal/toolActivities.js'
import { PgSessionRows } from './sessionRows.js'

// The tool activities' approval and owner reads straight on the test database, the statements
// the API's `pending-input` and `sessions` routes run, for tests with no API (#5299).

/** `RowSessionOwners` on the session rows' SQL. */
export class PgSessionOwners extends RowSessionOwners {
  constructor(sql: Sql) {
    super(new PgSessionRows(sql))
  }
}

/** `RecordedApprovals` with the route's read (`approval`) run on the test database. */
export class PgApprovalRecords extends RecordedApprovals {
  constructor(sql: Sql, grants: Pick<FlowRunReads, 'grantInForce'>) {
    super(grants, {
      approval: async (requestId: string, inputHash: string) => {
        const [row] = await sql<{ responder: { kind?: unknown; id?: unknown } }[]>`
          SELECT r.responder FROM ai_input_responses r
          JOIN ai_audit a ON a.request_id = r.request_id AND a.kind = 'approval' AND a.action = 'approved'
          WHERE r.request_id = ${requestId} AND r.kind = 'approval' AND r.outcome = 'approved'
            AND a.input_hash = ${inputHash}
          LIMIT 1`
        return row ? row.responder : null
      },
    })
  }
}
