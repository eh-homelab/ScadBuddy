-- #4975 phase 5 (spec 2026-11-01 §5.6): the agent reads a person's session itself, for
-- `/api/v1/ai/*`, which the ingress sends to it. Only what that check needs: no subject,
-- issuer, email, address or user agent. Live rows only. Granted to the agent's own role
-- `scadbuddy_agent`, where the deployment made it (`auth/service_credentials.py`
-- `grant_caller_views`).
CREATE VIEW auth_sessions_agent AS
    SELECT id_hash, principal, name, kind, idle_expires_at, absolute_expires_at
    FROM auth_sessions
    WHERE revoked_at IS NULL AND idle_expires_at > now() AND absolute_expires_at > now();
