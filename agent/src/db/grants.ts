import type { Sql } from 'postgres'

// #5210 (the follow-up to #4975 phase 4, spec 2026-11-01 §7.2): the `ai_*` tables are the
// agent's. Its role creates and migrates them (`db/migrations.ts`), so it owns them, and
// only the owner can grant them. So the agent grants, at every start after its
// migrations, what the backend's roles read and write of them; the backend grants the
// agent what it touches of the backend's (`backend/scadbuddy/auth/caller_grants.py`
// `AGENT_GRANTS`). On one shared role (the deployment before it makes the roles) there
// is nobody else to grant, and nothing is done.

type Grants = Readonly<Record<string, readonly string[]>>

/** The API's own role: a run's payload keys (`workflows/payload_codec.py`; it runs the
 * projects worker in-process, and serves its CRUD routes), what `flows/store.py` reads of
 * a session's tiers and the approval timeout, the activity list's (`activity/index.py`,
 * `activity/kinds.py`), a flow's images (`flows/images.py`) and forgetting a run
 * (`flows/forget.py`). */
export const API_AI_GRANTS: Grants = {
  ai_payload_keys: ['SELECT', 'INSERT', 'DELETE'],
  ai_forgotten_subjects: ['SELECT', 'INSERT'],
  ai_sessions: ['SELECT'],
  ai_mcp_tokens: ['SELECT'],
  ai_settings: ['SELECT'],
  ai_flow_blobs: ['SELECT', 'DELETE'],
  ai_operations: ['SELECT'],
  ai_activity_kinds: ['SELECT'],
}

/** The projects worker's role: it holds no database (ScadBuddy#5298), so the agent takes back
 * the `ai_*` grants it once held (#5298 Task 11), as the API takes back its own
 * (`backend/scadbuddy/auth/revoke.py`). */
export const PROJECTS_ROLE = 'scadbuddy_projects'

/** Postgres folds an unquoted name; these are quoted, so only the plain ones are allowed. */
const NAME = /^[a-z_][a-z0-9_]*$/

async function grant(sql: Sql, role: string, grants: Grants): Promise<void> {
  // USAGE on the schema is the API's to grant (`grant_role`); the agent need not own it.
  if (!NAME.test(role)) throw new Error(`refusing to grant a role named ${JSON.stringify(role)}`)
  for (const [table, privileges] of Object.entries(grants)) {
    if (!NAME.test(table)) throw new Error(`refusing to grant a table named ${JSON.stringify(table)}`)
    await sql.unsafe(`GRANT ${privileges.join(', ')} ON "${table}" TO "${role}"`)
  }
}

/**
 * Grant the API's role (the owner of `scadbuddy_migrations`) its `ai_*` tables, when the
 * agent's role is not the API's, and take back from `scadbuddy_projects`, when it exists
 * and is not the agent's, whatever the agent granted it. The roles granted.
 */
export async function grantBackendRoles(sql: Sql, projectsRole: string = PROJECTS_ROLE): Promise<string[]> {
  const granted: string[] = []
  const [api] = await sql<{ owner: string | null; mine: boolean | null }[]>`
    SELECT tableowner AS owner, pg_has_role(current_user, tableowner, 'MEMBER') AS mine
    FROM pg_tables WHERE schemaname = current_schema() AND tablename = 'scadbuddy_migrations'`
  if (api?.owner && api.mine === false) {
    await grant(sql, api.owner, API_AI_GRANTS)
    granted.push(api.owner)
  }
  await revokeRole(sql, projectsRole)
  return granted
}

/** The `pg_advisory_xact_lock` key every grant and revoke of the backend's roles takes, so
 * the API and this sidecar starting together run them one after the other, never into
 * "tuple concurrently updated". It must equal `backend/scadbuddy/render/worker_role.py`
 * `GRANT_LOCK` ("SCADRNDR" in ASCII); `test/grants.pg.test.ts` checks that. */
export const GRANT_LOCK = 0x5343_4144_524e_4452n

/**
 * Revoke everything a role the agent's belongs to granted `role` on the current schema's
 * relations, in one transaction under `GRANT_LOCK`; the relations taken back. Idempotent:
 * a missing role, the agent's own role and a role granted nothing are no-ops. The API
 * revokes its own grants and the schema's USAGE (`backend/scadbuddy/auth/revoke.py`); a
 * grant is revoked only by its grantor.
 */
export async function revokeRole(sql: Sql, role: string): Promise<string[]> {
  if (!NAME.test(role)) throw new Error(`refusing to revoke a role named ${JSON.stringify(role)}`)
  return sql.begin(async tx => {
    await tx`SELECT pg_advisory_xact_lock(${GRANT_LOCK.toString()}::bigint)`
    // Its own role or one it is a member of, through pg_auth_members (pg_has_role counts every
    // role for a superuser); the backend's `auth/revoke.py` MINE asks the same.
    const [found] = await tx<{ mine: boolean }[]>`
      WITH RECURSIVE mine(oid) AS (
        SELECT oid FROM pg_roles WHERE rolname = current_user
        UNION SELECT m.roleid FROM pg_auth_members m JOIN mine ON m.member = mine.oid)
      SELECT r.oid IN (SELECT oid FROM mine) AS mine FROM pg_roles r WHERE r.rolname = ${role}`
    if (!found || found.mine) return []
    const relations = await tx<{ name: string; sequence: boolean }[]>`
      SELECT DISTINCT c.relname AS name, c.relkind = 'S' AS sequence FROM pg_class c, aclexplode(c.relacl) a
      WHERE c.relnamespace = current_schema()::regnamespace
        AND a.grantee = ${role}::regrole AND pg_has_role(current_user, a.grantor, 'MEMBER')
      ORDER BY c.relname`
    for (const { name, sequence } of relations) {
      if (!NAME.test(name)) throw new Error(`refusing to revoke a table named ${JSON.stringify(name)}`)
      await tx.unsafe(`REVOKE ALL ON ${sequence ? 'SEQUENCE' : 'TABLE'} "${name}" FROM "${role}"`)
    }
    return relations.map(r => r.name)
  })
}
