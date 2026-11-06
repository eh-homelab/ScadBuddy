-- Design revisions (#5280, storefront spec 2026-11-02 §4, §6): an item's design as a
-- numbered row of its own. The item's reference columns and `colours` stay, as a copy of
-- its current revision (`current_revision_id`), so every reader of them keeps reading.
-- A `request` item is what a customer asked for, with no design until its first revision.
CREATE TABLE order_item_revisions (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE,
  number integer NOT NULL CHECK (number >= 1),
  slug text NOT NULL,
  commit text NOT NULL CHECK (commit ~ '^[0-9a-f]{40}$'),
  inputs jsonb NOT NULL,
  libraries jsonb NOT NULL DEFAULT '[]',
  template_name text NOT NULL,
  render_key text NOT NULL,
  colours jsonb NOT NULL DEFAULT '[]',
  note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 4000),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by jsonb NOT NULL,
  sent_at timestamptz,
  answer text CHECK (answer IN ('accepted', 'changes_requested', 'superseded')),
  answer_note text CHECK (char_length(answer_note) <= 4000),
  answered_at timestamptz,
  answered_by jsonb,
  CHECK ((answer IS NULL) = (answered_at IS NULL)),
  UNIQUE (item_id, number)
);

ALTER TABLE order_items
  ADD COLUMN current_revision_id text REFERENCES order_item_revisions (id),
  ADD COLUMN approval text NOT NULL DEFAULT 'together'
    CHECK (approval IN ('design_first', 'together'));

ALTER TABLE order_items DROP CONSTRAINT order_items_kind_check;
ALTER TABLE order_items
  ADD CONSTRAINT order_items_kind_check CHECK (kind IN ('model', 'custom', 'request'));

-- A request item prints, and holds its whole reference or none of it.
ALTER TABLE order_items DROP CONSTRAINT order_items_reference;
ALTER TABLE order_items
  ADD CONSTRAINT order_items_reference CHECK (
    CASE kind
      WHEN 'model' THEN slug IS NOT NULL AND commit IS NOT NULL AND inputs IS NOT NULL
        AND template_name IS NOT NULL AND render_key IS NOT NULL AND requires_printing
      WHEN 'request' THEN requires_printing AND (
        (slug IS NULL AND commit IS NULL AND inputs IS NULL AND template_name IS NULL
          AND render_key IS NULL AND output_id IS NULL)
        OR (slug IS NOT NULL AND commit IS NOT NULL AND inputs IS NOT NULL
          AND template_name IS NOT NULL AND render_key IS NOT NULL))
      ELSE slug IS NULL AND commit IS NULL AND inputs IS NULL AND template_name IS NULL
        AND render_key IS NULL AND output_id IS NULL
    END
  );

ALTER TABLE order_item_quotes
  ADD COLUMN revision_id text REFERENCES order_item_revisions (id);

ALTER TABLE order_item_fulfilments
  ADD COLUMN purpose text NOT NULL DEFAULT 'production'
    CHECK (purpose IN ('production', 'test')),
  ADD COLUMN revision_id text REFERENCES order_item_revisions (id),
  ADD CONSTRAINT order_item_fulfilments_test_revision
    CHECK (purpose = 'production' OR revision_id IS NOT NULL);

-- A file may be attached to a revision, `order_item_revision:<id>` (its render, a test
-- print), and a test print's file to the attempt that made it (`fulfilment_id`). As with
-- an attempt's, `attempt_item_id` is the subject's item; there is no foreign key.
ALTER TABLE order_attachments DROP CONSTRAINT order_attachments_subject_check;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_subject_check
  CHECK (subject ~ '^(order|order_item|order_item_fulfilment|order_item_revision):[^:]+$');
ALTER TABLE order_attachments DROP CONSTRAINT order_attachments_role_check;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_role_check
  CHECK (role IN ('reference', 'issue', 'finished', 'packed', 'timelapse', 'render',
    'test_print', 'other'));
ALTER TABLE order_attachments DROP CONSTRAINT order_attachments_attempt_item_check;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_attempt_item_check
  CHECK ((subject LIKE 'order_item_fulfilment:%' OR subject LIKE 'order_item_revision:%')
    = (attempt_item_id IS NOT NULL));
ALTER TABLE order_attachments ADD COLUMN fulfilment_id text;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_fulfilment_check
  CHECK (fulfilment_id IS NULL OR subject LIKE 'order_item_revision:%');

-- The order a media row was made for (no foreign key: the order's rows are another
-- feature's).
ALTER TABLE template_media
  ADD COLUMN source_commit text,
  ADD COLUMN source_revision_id text;

-- Every model item's design so far is its revision 1, which its quotes were made for.
INSERT INTO order_item_revisions
  (item_id, number, slug, commit, inputs, libraries, template_name, render_key, colours,
   created_at, created_by)
SELECT i.id, 1, i.slug, i.commit, i.inputs, i.libraries, i.template_name, i.render_key,
       i.colours, i.created_at, o.created_by
FROM order_items i JOIN orders o ON o.id = i.order_id
WHERE i.kind = 'model';

UPDATE order_items i SET current_revision_id = r.id
FROM order_item_revisions r WHERE r.item_id = i.id AND r.number = 1;

UPDATE order_item_quotes q SET revision_id = i.current_revision_id
FROM order_items i WHERE i.id = q.order_item_id AND i.kind = 'model';
