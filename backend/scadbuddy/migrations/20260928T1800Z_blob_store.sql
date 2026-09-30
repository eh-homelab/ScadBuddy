-- The blob store's index (spec 2026-09-27 §6.2, §6.3). The bytes live in a backend (the
-- data volume, or Bambuddy's library); what each blob is, where it is and when it was
-- last wanted live here, so a fetch is by id, never a folder scan, and the caps and the
-- Settings usage are one query. `key` is the directory key the activities use
-- (`piece_key`, `src-<slug>-<revision>`, `asset-<sha256>`, `font-<dir>`).
CREATE TABLE store_blobs (
    key         text PRIMARY KEY,
    sha256      text NOT NULL,
    kind        text NOT NULL,
    backend     text NOT NULL,
    backend_id  text NOT NULL,
    size        bigint NOT NULL,
    slug        text,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    touched_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX store_blobs_sha ON store_blobs (sha256, kind, backend);
CREATE INDEX store_blobs_object ON store_blobs (backend, backend_id);
CREATE INDEX store_blobs_kind ON store_blobs (kind, touched_at);

-- The Bambuddy folders ScadBuddy created (or adopted) under its inbox. A delete is
-- refused unless the file sits in one recorded with role 'work' (#316).
CREATE TABLE store_folders (
    inbox_id   bigint NOT NULL,
    slug       text NOT NULL,
    role       text NOT NULL CHECK (role IN ('template', 'work')),
    folder_id  bigint NOT NULL UNIQUE,
    PRIMARY KEY (inbox_id, slug, role)
);
