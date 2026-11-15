-- #5299 D3: the agent's leases (`api/crud_agent_access.py`). One row per lock the agent
-- holds across work that is not the database's: a plugin's connection test (key
-- `tool-listing:<source>`, which replaces the session advisory lock of
-- `agent/src/access/listings.ts`) and a plugin sign-in's token refresh (key
-- `plugin-oauth:<plugin>`, which replaces the `FOR UPDATE` held across the token endpoint
-- in `plugins/oauthStore.ts`). Held by `token` until `expires_at`, renewed while the work
-- runs, as `copy_leases` (#5298 Task 27). The API's table alone: the agent takes, renews
-- and releases a lease through `…/crud/agent/leases/{key}`.
CREATE TABLE IF NOT EXISTS agent_leases (
    key        text PRIMARY KEY,
    token      uuid NOT NULL,
    expires_at timestamptz NOT NULL
);
