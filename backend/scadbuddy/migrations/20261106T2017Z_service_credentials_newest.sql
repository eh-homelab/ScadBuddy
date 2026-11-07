-- #5298: a caller's newest sealed credential is the one with the latest `expires_at`, then
-- the latest `created_at`, so two minted at one instant (a scope change at the API's start
-- beside a due rotation) always hand over the same row. `_insert` keeps `created_at`
-- strictly increasing per caller. The column is added at the end, so an old reader's
-- query is unchanged.
CREATE OR REPLACE VIEW service_credentials_agent AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at, created_at
    FROM service_credentials
    WHERE caller = 'agent' AND proof = 'legacy' AND revoked_at IS NULL AND expires_at > now();

CREATE OR REPLACE VIEW service_credentials_print AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at, created_at
    FROM service_credentials
    WHERE caller = 'print' AND proof = 'legacy' AND revoked_at IS NULL AND expires_at > now();

CREATE OR REPLACE VIEW service_credentials_projects AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at, created_at
    FROM service_credentials
    WHERE caller = 'projects' AND proof = 'legacy' AND revoked_at IS NULL AND expires_at > now();
