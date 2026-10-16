-- Which template each held output belongs to (#1007, #1806). The output-hold reaper
-- releases a hold only when its output's slug directory was listed: a slug directory
-- that is missing (not yet copied onto a new volume, say) must not read as every one of
-- its outputs deleted. A hold with no row here is never released by the reaper.
CREATE TABLE IF NOT EXISTS output_hold_slugs (
    output_id text PRIMARY KEY,
    slug text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
