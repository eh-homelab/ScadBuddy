-- #4253 (spec 2026-10-18 §8.1): the Bambuddy project mirroring an order and each of its
-- line items, nested under the order's. `subject` is `order:<id>` or `order_item:<id>`;
-- `project_id` is Bambuddy's. Ensured on accept, and for an item again before it prints,
-- whose queue item then carries the project. Nothing deletes a project, and a cancel keeps
-- the row: a reopened order goes on in the projects it had.
CREATE TABLE order_projects (
  subject text PRIMARY KEY,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  project_id integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX order_projects_order ON order_projects (order_id);
