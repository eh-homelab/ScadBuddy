-- #6950: a registry of the things a file or link can hang off, so attachments and links
-- are not tied to orders. An `entities` row is made when the first file or link attaches;
-- the owner's table has no foreign key to it (the owners are other features' rows).
CREATE TABLE entities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL CHECK (kind ~ '^[a-z_]{1,40}$'),
  ref text NOT NULL CHECK (length(ref) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (kind, ref)
);

CREATE TABLE entity_links (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
  url text NOT NULL CHECK (url ~* '^https?://' AND length(url) <= 2000),
  kind text NOT NULL CHECK (kind ~ '^[a-z_]{1,32}$'),
  title text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
  note text NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
  position integer NOT NULL DEFAULT 0,
  source text NOT NULL DEFAULT 'manual',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by jsonb NOT NULL
);
CREATE INDEX entity_links_entity ON entity_links (entity_id, position);

-- Order attachments become entity attachments: any entity may own a file. `order_id`
-- stays for an order's own files (the order's reads and its delete guard) and is null for
-- another entity's.
ALTER TABLE order_attachments RENAME TO entity_attachments;
ALTER INDEX order_attachments_order RENAME TO entity_attachments_order;
ALTER INDEX order_attachments_subject RENAME TO entity_attachments_subject;
ALTER TABLE entity_attachments RENAME CONSTRAINT order_attachments_pkey TO entity_attachments_pkey;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_subject_check TO entity_attachments_subject_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_kind_check TO entity_attachments_kind_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_role_check TO entity_attachments_role_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_filename_check TO entity_attachments_filename_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_size_bytes_check TO entity_attachments_size_bytes_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_sha256_check TO entity_attachments_sha256_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_caption_check TO entity_attachments_caption_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_order_id_fkey TO entity_attachments_order_id_fkey;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_attempt_item_check TO entity_attachments_attempt_item_check;
ALTER TABLE entity_attachments
  RENAME CONSTRAINT order_attachments_fulfilment_check TO entity_attachments_fulfilment_check;

ALTER TABLE entity_attachments
  ADD COLUMN entity_id uuid REFERENCES entities (id) ON DELETE RESTRICT;

INSERT INTO entities (kind, ref)
SELECT DISTINCT split_part(subject, ':', 1), substring(subject FROM position(':' IN subject) + 1)
FROM entity_attachments;

UPDATE entity_attachments a
SET entity_id = e.id
FROM entities e
WHERE e.kind = split_part(a.subject, ':', 1)
  AND e.ref = substring(a.subject FROM position(':' IN a.subject) + 1);

ALTER TABLE entity_attachments ALTER COLUMN entity_id SET NOT NULL;
ALTER TABLE entity_attachments ALTER COLUMN order_id DROP NOT NULL;

ALTER TABLE entity_attachments DROP CONSTRAINT entity_attachments_subject_check;
ALTER TABLE entity_attachments
  ADD CONSTRAINT entity_attachments_subject_check CHECK (subject ~ '^[a-z_]+:.+$');
ALTER TABLE entity_attachments DROP CONSTRAINT entity_attachments_role_check;
ALTER TABLE entity_attachments
  ADD CONSTRAINT entity_attachments_role_check CHECK (role ~ '^[a-z_]{1,32}$');

CREATE INDEX entity_attachments_entity ON entity_attachments (entity_id, created_at);

-- The old name, for the replicas of the previous build while the API rolls out
-- (`RollingUpdate`), as `20261026T2151Z_rename_order_attachments.sql` kept `attachments`:
-- a plain view of one table is automatically updatable, so their reads and writes keep
-- working. It names the columns that build reads and writes, and only an order's files
-- (another entity's have no `order_id`, which that build's rows require). That build
-- names no `entity_id`, so the trigger below fills it from `subject` (making the
-- `entities` row), and moves it when a write changes `subject` alone (that build's
-- `release_print_claim`). A later migration drops the view, the trigger and its function
-- once every replica runs this build; until then no migration may add a column that an
-- old replica's insert through the view must set.
CREATE VIEW order_attachments AS
SELECT id, order_id, subject, kind, role, filename, media_type, size_bytes, sha256, blob_key,
  caption, created_at, created_by, attempt_item_id, fulfilment_id
FROM entity_attachments
WHERE order_id IS NOT NULL;

CREATE FUNCTION entity_attachments_fill_entity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  entity_kind text := split_part(NEW.subject, ':', 1);
  entity_ref text := substring(NEW.subject FROM position(':' IN NEW.subject) + 1);
BEGIN
  IF TG_OP = 'INSERT' AND NEW.entity_id IS NOT NULL THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.subject = OLD.subject OR NEW.entity_id <> OLD.entity_id) THEN
    RETURN NEW;
  END IF;
  INSERT INTO entities (kind, ref) VALUES (entity_kind, entity_ref)
  ON CONFLICT (kind, ref) DO NOTHING;
  SELECT id INTO NEW.entity_id FROM entities WHERE kind = entity_kind AND ref = entity_ref;
  RETURN NEW;
END
$$;

CREATE TRIGGER entity_attachments_fill_entity
BEFORE INSERT OR UPDATE OF subject ON entity_attachments
FOR EACH ROW EXECUTE FUNCTION entity_attachments_fill_entity();
