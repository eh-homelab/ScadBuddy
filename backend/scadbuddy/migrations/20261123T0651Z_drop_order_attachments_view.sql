-- #6960: `20261122T0426Z_entities.sql` (#6950) kept the old name `order_attachments` as a
-- view, with a trigger that fills `entity_id` from `subject`, for the previous build's
-- replicas while the API rolled out. Every API replica now runs a build with #6950 (prod
-- pod images sha-e41fe68 and sha-9b46a0f, both after #6950's merge c1430cd80, checked
-- 2026-11-23), and that build sets `entity_id` on every write, so the view, the trigger
-- and its function go.
DROP VIEW IF EXISTS order_attachments;
DROP TRIGGER IF EXISTS entity_attachments_fill_entity ON entity_attachments;
DROP FUNCTION IF EXISTS entity_attachments_fill_entity();
