import type { Sql } from 'postgres'

// #5210 (the follow-up to #4975 phase 4, spec 2026-11-01 §7.2): the `ai_*` tables are the
// agent's. Its role created them, so it owns them, and only the owner can grant them. So
// the agent grants, at every start once its schema is current, what the backend's roles
// read and write of them; the backend grants the agent what it touches of the backend's
// (`backend/scadbuddy/auth/caller_grants.py` `AGENT_GRANTS`, empty since #5299 step 10). On one shared role (the
// deployment before it makes the roles, and production) there is nobody else to grant,
// and nothing is done. Since #5299 D6 the API applies the agent's migrations, so a table a
// later file makes is the API's role's: per-caller roles need the API and the agent on
// one role until the agent's data moves behind CRUD routes (#5299).

type Grants = Readonly<Record<string, readonly string[]>>

/** The API's own role: a run's payload keys (`workflows/payload_codec.py`; it runs the
 * projects worker in-process, and serves its CRUD routes), what `flows/store.py` reads of
 * a session's tiers and the approval timeout, the activity list's (`activity/index.py`,
 * `activity/kinds.py`), a flow's images (`flows/images.py`) and forgetting a run
 * (`flows/forget.py`). And the agent's own settings, Claude credentials and payload keys,
 * which the API serves this agent over its CRUD routes, sealing and opening them with the KEK
 * and auditing each reveal (#5299 step 4, `api/crud_agent_secrets.py`), and forgetting a subject,
 * the key deletes no forget makes and a flow run's images, which it keeps for this agent (#5299
 * step 6, `api/crud_agent_forget.py`: the sweep reads `ai_session_entries`): the CRUD service's
 * grants on the tables it takes over (#5299 D1), never another service's on its tables. A
 * KEK rotation re-wraps the credentials and payload keys there (UPDATE). And the small stores
 * it serves over its CRUD routes (#5299 step 5, `api/crud_agent_stores.py` and
 * `api/crud_agent_notify.py`): the activity kinds, the browser's approved origins, the
 * session tabs, tab owners and tab images, the pairings, the MCP tokens, and the Web Push
 * records and notification channels, which it seals, opens and re-wraps with the KEK. And the
 * workspace files (#5299 step 7, `api/crud_agent_workspace.py`): their rows and shared bytes
 * (locked FOR KEY SHARE and FOR UPDATE, which need UPDATE), each write's `workspace.file`
 * events with the session's `event_seq` bump and its row lock (UPDATE on `ai_sessions`). And a
 * session's images, read through its fork chain, a durable tool call's images, the staged
 * attachments and the saved http_request bodies (#5299 step 7, `api/crud_agent_blobs.py`: a
 * write sets an image's `created_at`, and a queued message's images are deleted, #6413; a
 * fenced write locks the session's row FOR SHARE). And the plugin stores (#5299 step 5, `api/crud_agent_access.py`,
 * `api/crud_agent_plugins.py` and `api/crud_agent_packages.py`): the tool access, the tool
 * listings, the upstream MCP sessions, the remote plugins and their sign-ins (sealed, opened
 * and re-wrapped with the KEK) and the plugin packages. The leases of #5299 D3 are the API's
 * own table (`agent_leases`, a backend migration), so they need no grant here. And a
 * session's background tasks, a Monitor's events, a Workflow's journal and child chats, and the
 * agent's command record with its activity row (#5299 step 8,
 * `api/crud_agent_tasks.py`; `ai_sessions` is read for a Workflow's open child chats). And a
 * session's row as the session manager reads and writes it, a failed fork discarded with its
 * transcript rows and its key, and what its tool calls touched (#5299 step 9,
 * `api/crud_agent_sessions.py`). And a session's transcript, sealed and opened by the API
 * (#5299 step 9, part b, `api/crud_agent_transcripts.py`), and its event log
 * (`api/crud_agent_events.py`). */
export const API_AI_GRANTS: Grants = {
  ai_payload_keys: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_forgotten_subjects: ['SELECT', 'INSERT'],
  ai_sessions: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_mcp_tokens: ['SELECT', 'INSERT', 'UPDATE'],
  ai_settings: ['SELECT', 'INSERT', 'UPDATE'],
  ai_credentials: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_audit: ['INSERT'],
  ai_flow_blobs: ['SELECT', 'INSERT', 'DELETE'],
  ai_session_entries: ['SELECT', 'INSERT', 'DELETE'],
  ai_operations: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_activity_kinds: ['SELECT', 'INSERT', 'UPDATE'],
  ai_browser_session_origins: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_session_tabs: ['SELECT', 'INSERT', 'UPDATE'],
  ai_tab_owners: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_tab_images: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_browser_pairings: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_push_vapid: ['SELECT', 'INSERT', 'UPDATE'],
  ai_push_subscriptions: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_notifications_sent: ['SELECT', 'INSERT', 'DELETE'],
  ai_notify_channels: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_session_files: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_session_file_bytes: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_session_events: ['SELECT', 'INSERT'],
  ai_session_blobs: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_session_tool_images: ['SELECT', 'INSERT', 'DELETE'],
  ai_attachments: ['SELECT', 'INSERT', 'DELETE'],
  ai_session_http_bodies: ['SELECT', 'INSERT', 'DELETE'],
  ai_access: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_tool_listings: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_plugin_sessions: ['SELECT', 'INSERT', 'DELETE'],
  ai_plugins: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_plugin_oauth: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_plugin_oauth_flows: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_plugin_packages: ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ai_background_tasks: ['SELECT', 'INSERT', 'UPDATE'],
  ai_background_events: ['SELECT', 'INSERT', 'UPDATE'],
  ai_workflow_calls: ['SELECT', 'INSERT'],
  ai_workflow_children: ['SELECT', 'INSERT'],
  ai_session_resources: ['SELECT', 'INSERT'],
}

/** The sequences behind the API's inserts: `ai_background_events.seq` and `ai_session_entries.id`
 * are `bigserial`s, whose `nextval` needs USAGE (the other tables' ids are given or
 * `GENERATED … AS IDENTITY`). */
export const API_AI_SEQUENCE_GRANTS: Grants = {
  ai_background_events_seq_seq: ['USAGE'],
  ai_session_entries_id_seq: ['USAGE'],
}

/** The projects worker's role: it holds no database (ScadBuddy#5298), so the agent takes back
 * the `ai_*` grants it once held (#5298 Task 11), as the API takes back its own
 * (`backend/scadbuddy/auth/revoke.py`). */
export const PROJECTS_ROLE = 'scadbuddy_projects'

/** Postgres folds an unquoted name; these are quoted, so only the plain ones are allowed. */
const NAME = /^[a-z_][a-z0-9_]*$/

async function grant(sql: Sql, role: string, grants: Grants, kind: 'TABLE' | 'SEQUENCE' = 'TABLE'): Promise<void> {
  // USAGE on the schema is the API's to grant (`grant_role`); the agent need not own it.
  if (!NAME.test(role)) throw new Error(`refusing to grant a role named ${JSON.stringify(role)}`)
  for (const [table, privileges] of Object.entries(grants)) {
    if (!NAME.test(table)) throw new Error(`refusing to grant a ${kind.toLowerCase()} named ${JSON.stringify(table)}`)
    await sql.unsafe(`GRANT ${privileges.join(', ')} ON ${kind} "${table}" TO "${role}"`)
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
    await grant(sql, api.owner, API_AI_SEQUENCE_GRANTS, 'SEQUENCE')
    granted.push(api.owner)
  }
  await revokeRole(sql, projectsRole)
  return granted
}

/** The `pg_advisory_xact_lock` key every grant and revoke of the backend's roles takes, so
 * the API and this sidecar starting together run them one after the other, never into
 * "tuple concurrently updated". It must equal `backend/scadbuddy/auth/caller_grants.py`
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
