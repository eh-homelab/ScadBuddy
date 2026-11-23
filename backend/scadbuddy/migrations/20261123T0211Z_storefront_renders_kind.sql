-- #6710, #7075 review: a storefront_renders row is a visitor's render (`kind` 'render', as
-- every row before) or a file listing's price read that claimed a slice ('slice'), which
-- counts against the visitor's rates but is no render job: it names no job, and the
-- shop's render concurrency counts 'render' rows only. A slice row written before this
-- file named its price key as `job_id` 'slice:<key>'; it becomes a 'slice' row.
ALTER TABLE storefront_renders
  ADD COLUMN kind text NOT NULL DEFAULT 'render' CHECK (kind IN ('render', 'slice'));

UPDATE storefront_renders SET kind = 'slice', job_id = NULL WHERE job_id LIKE 'slice:%';
