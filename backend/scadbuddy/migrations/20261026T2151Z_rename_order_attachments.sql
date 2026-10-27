-- #4678: an order's files are `order_attachments`, not `attachments`. A print's
-- attachments are `print_attachments` (`scadbuddy/attachments/`), so the bare name
-- read as everyone's. A rename moves no row: the indexes, the constraints and
-- `order_items.delivered_attachment_id`'s foreign key follow the table.
ALTER TABLE attachments RENAME TO order_attachments;
ALTER INDEX attachments_order RENAME TO order_attachments_order;
ALTER INDEX attachments_subject RENAME TO order_attachments_subject;
ALTER TABLE order_attachments RENAME CONSTRAINT attachments_pkey TO order_attachments_pkey;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_subject_check TO order_attachments_subject_check;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_kind_check TO order_attachments_kind_check;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_role_check TO order_attachments_role_check;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_filename_check TO order_attachments_filename_check;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_size_bytes_check TO order_attachments_size_bytes_check;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_sha256_check TO order_attachments_sha256_check;
ALTER TABLE order_attachments
  RENAME CONSTRAINT attachments_caption_check TO order_attachments_caption_check;

-- An order's delete must not take its attachments' rows and leave their `blob_refs`
-- holds, which would keep the blobs for good: nothing cascades a hold. No route
-- deletes an order; one that does must remove each attachment through
-- `OrderStore.delete_attachment` first, and this foreign key refuses it otherwise.
ALTER TABLE order_attachments DROP CONSTRAINT attachments_order_id_fkey;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_order_id_fkey
  FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE RESTRICT;

-- The old name, for the replicas of the previous build while the API rolls out
-- (`RollingUpdate`): a plain view of one table is automatically updatable, so their
-- reads and writes keep working. A later migration drops it (#4754). `SELECT *` is
-- expanded when the view is made, so its columns are frozen: no migration may add a
-- column to `order_attachments` before #4754 drops the view, or an old replica's
-- insert through it would leave that column at its default. Nothing new reads it.
CREATE VIEW attachments AS SELECT * FROM order_attachments;
