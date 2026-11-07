-- Orders phase 3 (#4251, spec 2026-10-18 §6.2, plan 2026-11-03 Ruling 3): a file may be
-- attached to a fulfilment attempt, `order_item_fulfilment:<id>` (an archive's finish
-- photo or timelapse, a photo of the print). `attempt_item_id` is that attempt's item,
-- so the order's one read lists the file under its item too; it is NULL for any other
-- subject, whose item (if any) the subject itself names. As with `subject`, there is
-- no foreign key: the lifecycle is the one writer and checks the attempt is the item's.
ALTER TABLE order_attachments DROP CONSTRAINT order_attachments_subject_check;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_subject_check
  CHECK (subject ~ '^(order|order_item|order_item_fulfilment):[^:]+$');
ALTER TABLE order_attachments ADD COLUMN attempt_item_id text;
ALTER TABLE order_attachments
  ADD CONSTRAINT order_attachments_attempt_item_check
  CHECK ((subject LIKE 'order_item_fulfilment:%') = (attempt_item_id IS NOT NULL));
