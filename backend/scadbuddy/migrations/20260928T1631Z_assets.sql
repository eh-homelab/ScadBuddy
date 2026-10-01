-- #591: the upload store's metadata (`library.assets.AssetStore`), formerly a
-- data/assets/<id>.json sidecar per blob, its last use (that sidecar's mtime) and
-- its running usage (data/.assets.usage.json, now count(*) / sum(size) here).
-- The bytes stay on disk as data/assets/<id>.<kind>. Nothing is copied over: a blob
-- with no row is an orphan, and the sweep removes it after the grace period.
CREATE TABLE assets (
    id           text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
    name         text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('svg', 'png')),
    size         bigint NOT NULL CHECK (size >= 0),
    width        integer,
    height       integer,
    created_at   timestamptz NOT NULL,
    last_used_at timestamptz NOT NULL
);
CREATE INDEX assets_last_used ON assets (last_used_at);
