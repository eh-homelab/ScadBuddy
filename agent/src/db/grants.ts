import type { Sql } from 'postgres'

// #5210 (the follow-up to #4975 phase 4, spec 2026-11-01 §7.2): the `ai_*` tables are the
// agent's. Its role creates and migrates them (`db/migrations.ts`), so it owns them, and
// only the owner can grant them. So the agent grants, at every start after its
// migrations, what the backend's roles read and write of them; the backend grants the
// agent what it touches of the backend's (`backend/scadbuddy/auth/caller_grants.py`
// `AGENT_GRANTS`). On one shared role (the deployment before it makes the roles) there
// is nobody else to grant, and nothing is done.

type Grants = Readonly<Record<string, readonly string[]>>

/** The projects worker (`--queue projects`): none. Its payload keys, a session's tiers and
 * the approval timeout come through the API's CRUD routes (ScadBuddy#5298); the grants it
 * held for them stay on a live role until #5298 Task 11 revokes them. */
export const PROJECTS_AI_GRANTS: Grants = {}

/** The API's own role: a run's payload keys (`workflows/payload_codec.py`; it runs the
 * projects worker in-process, and serves its CRUD routes), what `flows/store.py` reads of
 * a session's tiers and the approval timeout, the activity list's (`activity/index.py`,
 * `activity/kinds.py`), a flow's images (`flows/images.py`) and forgetting a run
 * (`flows/forget.py`). */
export const API_AI_GRANTS: Grants = {
  ...PROJECTS_AI_GRANTS,
  ai_payload_keys: ['SELECT', 'INSERT', 'DELETE'],
  ai_forgotten_subjects: ['SELECT', 'INSERT'],
  ai_sessions: ['SELECT'],
  ai_mcp_tokens: ['SELECT'],
  ai_settings: ['SELECT'],
  ai_flow_blobs: ['SELECT', 'DELETE'],
  ai_operations: ['SELECT'],
  ai_activity_kinds: ['SELECT'],
}

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
 * Grant the backend's roles their `ai_*` tables, when the agent's role is not theirs: the
 * API's (the owner of `scadbuddy_migrations`) and `scadbuddy_projects`, each only when it
 * exists. The roles granted.
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
  const [projects] = await sql<{ mine: boolean }[]>`
    SELECT pg_has_role(current_user, rolname, 'MEMBER') AS mine FROM pg_roles WHERE rolname = ${projectsRole}`
  if (projects && !projects.mine) {
    await grant(sql, projectsRole, PROJECTS_AI_GRANTS)
    granted.push(projectsRole)
  }
  return granted
}
