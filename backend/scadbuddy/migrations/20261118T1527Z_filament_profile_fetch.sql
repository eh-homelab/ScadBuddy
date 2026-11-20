-- #6625: the profile-fetch worker is a service caller of its own, `profile-fetch`, with
-- credentials from the exchange only (`auth/service_auth.py` `ServiceCaller`).
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch'));

-- A type's id on 3dfilamentprofiles.com, which the profile-fetch worker exports from.
ALTER TABLE filament_spool_types ADD COLUMN tdfp_id integer CHECK (tdfp_id > 0);

-- One fetch of a type's slicer profiles. `id` is the run id (the workflow id is
-- `profile-fetch:<run_id>`). `files` maps each fetched key to its blob key and preset
-- name, and, once imported, the Bambuddy preset id it bound; the blobs are held in
-- `blob_refs` (holder `profile_fetch`, holder id the run id). `bambuddy_before` is the
-- filament preset ids Bambuddy listed before the run's first import, so a retry after a
-- lost answer still knows which presets the run made. `bambuddy_made` is the preset ids
-- the import made (listed after it, absent from `bambuddy_before`, named as one of the
-- run's files), kept as soon as the import is listed, so a later fetch of the type can
-- bind them even when this run failed; it is empty when Bambuddy refused the import. `import_lease_until` is the lease one import
-- request holds on the run: another request claims it only once it has expired.
CREATE TABLE filament_profile_fetches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type_id uuid NOT NULL REFERENCES filament_spool_types (id) ON DELETE CASCADE,
    tdfp_id integer NOT NULL CHECK (tdfp_id > 0),
    targets jsonb NOT NULL DEFAULT '[]',
    files jsonb NOT NULL DEFAULT '{}',
    bambuddy_before jsonb,
    bambuddy_made jsonb,
    import_lease_until timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'imported', 'partial', 'failed')),
    missing jsonb NOT NULL DEFAULT '[]',
    k_values integer NOT NULL DEFAULT 0,
    notes jsonb NOT NULL DEFAULT '[]',
    error text,
    started_by text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE UNIQUE INDEX filament_profile_fetches_one_running
    ON filament_profile_fetches (type_id) WHERE status = 'running';

CREATE FUNCTION filament_profile_fetches_release_blobs() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM blob_refs WHERE holder_kind = 'profile_fetch' AND holder_id = OLD.id::text;
    RETURN OLD;
END;
$$;

CREATE TRIGGER filament_profile_fetches_release_blobs
    AFTER DELETE ON filament_profile_fetches
    FOR EACH ROW EXECUTE FUNCTION filament_profile_fetches_release_blobs();
