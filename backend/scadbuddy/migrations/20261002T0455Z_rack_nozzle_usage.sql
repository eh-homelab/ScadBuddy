-- #836: rack hotend usage (spec docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md §4).
-- Serials live in these three tables and nowhere else (spec §7).
CREATE TABLE rack_nozzle_seen (
    serial        text        PRIMARY KEY,
    printer_id    integer     NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rack_nozzle_picks (
    queue_item_id integer     NOT NULL,
    group_id      integer     NOT NULL,
    printer_id    integer     NOT NULL,
    serial        text        NOT NULL,
    picked_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (queue_item_id, group_id)
);

CREATE TABLE rack_nozzle_prints (
    archive_id    integer     NOT NULL,
    group_id      integer     NOT NULL,
    serial        text        NOT NULL,
    settled_at    timestamptz NOT NULL,
    print_seconds bigint,
    grams         numeric,
    PRIMARY KEY (archive_id, group_id)
);

CREATE INDEX rack_nozzle_prints_serial ON rack_nozzle_prints (serial);
