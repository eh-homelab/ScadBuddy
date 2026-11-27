-- #8041: fleetd on a machine authenticates with a personal access token of scope `fleet`,
-- as bambuddy-fs does with `read` and `write`. A `fleet` token reaches only core's
-- `fleet-rotator` routes.
ALTER TABLE personal_access_tokens DROP CONSTRAINT personal_access_tokens_scope_check;
ALTER TABLE personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_scope_check CHECK (scope IN ('read', 'write', 'fleet'));

-- One token is one machine: a machine that a `fleet` token registered first records that
-- token's id, and only that token refreshes the row or writes its sessions. A machine that a
-- bootstrap proof registered (#8003) keeps its proof_name. Each row names exactly one owner.
-- No foreign key: a token row is forgotten some time after it is revoked
-- (auth/personal_tokens.py KEEP), and the machine row stays.
ALTER TABLE fleet_machines ALTER COLUMN proof_name DROP NOT NULL;
ALTER TABLE fleet_machines ADD COLUMN token_id text CHECK (token_id ~ '^[0-9a-f]{16}$');
ALTER TABLE fleet_machines
    ADD CONSTRAINT fleet_machines_one_owner CHECK (num_nonnulls(proof_name, token_id) = 1);
