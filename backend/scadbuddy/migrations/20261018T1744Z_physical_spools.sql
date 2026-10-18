-- Physical spools and the spool providers' records of them (#3818 tier A, plan
-- 2026-10-18-hot-path-without-bambuddy D1). A tray holds one physical spool; each spool
-- provider (Bambuddy's inventory now, ScadBuddy's own later) keeps its own record of it,
-- so a physical spool has at most one record per provider (1:N across providers). A
-- spool is keyed by what the printer reports when it can: the AMS tray's `tray_uuid`,
-- else its RFID `tag_uid`; an untagged spool reports all zeros, which is never stored.
CREATE TABLE physical_spools (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tray_uuid text UNIQUE,
    tag_uid text UNIQUE,
    material text,
    colour text,
    tray_info_idx text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz
);

-- One provider's record of a spool. `physical_spool_id` NULL: not linked yet, offered for
-- linking (`linked_by` then means nothing: a deleted spool leaves it). Written only by
-- the provider's refresh (`SpoolStore.replace_records`) and a person's manual link.
CREATE TABLE spool_provider_records (
    provider text NOT NULL,
    record_id text NOT NULL,
    physical_spool_id uuid REFERENCES physical_spools (id) ON DELETE SET NULL,
    linked_by text CHECK (linked_by IN ('rfid', 'manual')),
    snapshot jsonb NOT NULL,
    refreshed_at timestamptz NOT NULL,
    PRIMARY KEY (provider, record_id),
    UNIQUE (physical_spool_id, provider)
);

-- One row per provider: when its records were last refreshed, and the last failure.
CREATE TABLE spool_provider_refreshes (
    provider text PRIMARY KEY,
    refreshed_at timestamptz,
    failed_at timestamptz,
    failure text
);

-- Manual links for untagged trays only; an RFID tray resolves by its keys. `printer_id`
-- is the printer registry's id (`printers.id`, text, plan 2026-10-16 Task 2), which is
-- not on main yet: the foreign key (ON DELETE CASCADE) is a later migration's, once
-- both are. `fingerprint` is the tray's `material|colour|state` when linked
-- (`bambuddy/trays.py` `unknown_trays`); a tray that no longer matches it reads as
-- unlinked.
CREATE TABLE tray_spool_links (
    printer_id text NOT NULL,
    ams_id integer NOT NULL,
    tray_id integer NOT NULL,
    physical_spool_id uuid NOT NULL REFERENCES physical_spools (id) ON DELETE CASCADE,
    fingerprint text NOT NULL,
    linked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (printer_id, ams_id, tray_id)
);
CREATE INDEX tray_spool_links_spool ON tray_spool_links (physical_spool_id);
