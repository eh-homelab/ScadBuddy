-- #5494 (spec 2026-11-05 §6.2, §6.5, §8 step 1): credentials minted by the exchange
-- (`POST /api/v1/auth/service-token`) beside the sealed 30-day ones. An exchanged row
-- names how its caller proved itself (`proof`) and, for a Kubernetes proof, the pod it
-- is bound to; it has no sealed copy, since nobody reads it back. `proof` defaults to
-- `legacy` so an API pod of the build before this one, still running during the roll,
-- keeps inserting its sealed rows unchanged.
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render'));

ALTER TABLE service_credentials
    ADD COLUMN proof    text NOT NULL DEFAULT 'legacy'
        CHECK (proof IN ('legacy', 'k8s', 'bootstrap', 'inprocess')),
    ADD COLUMN pod_name text,
    ADD COLUMN pod_uid  text;

ALTER TABLE service_credentials
    ALTER COLUMN secret_sealed DROP NOT NULL,
    ALTER COLUMN dek_sealed DROP NOT NULL,
    ALTER COLUMN kek_id DROP NOT NULL;

-- A legacy row is sealed whole; an exchanged one holds no part of a seal. A pod is
-- named for a Kubernetes proof only, and then always.
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_sealed_iff_legacy CHECK (
    (proof = 'legacy' AND secret_sealed IS NOT NULL AND dek_sealed IS NOT NULL AND kek_id IS NOT NULL)
    OR (proof <> 'legacy' AND secret_sealed IS NULL AND dek_sealed IS NULL AND kek_id IS NULL)
);
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_pod_iff_k8s CHECK (
    (proof = 'k8s' AND pod_name IS NOT NULL AND pod_uid IS NOT NULL)
    OR (proof <> 'k8s' AND pod_name IS NULL AND pod_uid IS NULL)
);

-- The exchange's rate limits count a caller's and a pod's recent mints.
CREATE INDEX service_credentials_recent ON service_credentials (caller, created_at DESC);
CREATE INDEX service_credentials_pod ON service_credentials (pod_uid) WHERE pod_uid IS NOT NULL;

-- The views hand a caller its sealed row only: an exchanged row has nothing to open.
CREATE OR REPLACE VIEW service_credentials_agent AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at
    FROM service_credentials
    WHERE caller = 'agent' AND proof = 'legacy' AND revoked_at IS NULL AND expires_at > now();

CREATE OR REPLACE VIEW service_credentials_print AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at
    FROM service_credentials
    WHERE caller = 'print' AND proof = 'legacy' AND revoked_at IS NULL AND expires_at > now();

CREATE OR REPLACE VIEW service_credentials_projects AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at
    FROM service_credentials
    WHERE caller = 'projects' AND proof = 'legacy' AND revoked_at IS NULL AND expires_at > now();

-- §6.5: a suspended caller's exchanges are refused and its credentials do not resolve,
-- until a person resumes it. Written by the CRUD service only, from Settings.
CREATE TABLE service_caller_suspensions (
    caller       text        PRIMARY KEY CHECK (caller IN ('agent', 'print', 'projects', 'render')),
    suspended_at timestamptz NOT NULL DEFAULT now(),
    suspended_by text        NOT NULL
);
