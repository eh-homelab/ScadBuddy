-- #4754: `20261026T2151Z_rename_order_attachments.sql` kept the old name `attachments`
-- as a view for the previous build's replicas while the API rolled out. Every replica
-- now runs a build with the rename (#4761), and nothing reads or writes the old name,
-- so the view goes, and `order_attachments` may take new columns again.
DROP VIEW IF EXISTS attachments;
