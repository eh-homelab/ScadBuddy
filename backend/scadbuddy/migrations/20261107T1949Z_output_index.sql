-- Recent renders across models (#4777, order operations spec 2026-11-07 §3.1): an index
-- of the saved outputs, so `GET /outputs` lists, filters and pages every model's outputs
-- in one query. The output's files (`data/outputs/<slug>/<id>/meta.json`) stay its
-- record and the bytes' home; a row is written when an output is saved, renamed or
-- deleted (`library/output_index.py`), and the API's start fills in any output with no
-- row and drops any row whose output is gone. `params_text` is the parameters that
-- differ from the template's defaults, as `name=value` words, for the `q` filter.
CREATE TABLE output_index (
  output_id text PRIMARY KEY,
  slug text NOT NULL,
  name text,
  created_at timestamptz NOT NULL,
  params_text text NOT NULL DEFAULT '',
  indexed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX output_index_created ON output_index (created_at DESC, output_id DESC);
CREATE INDEX output_index_slug ON output_index (slug, created_at DESC);

-- The `ordered` filter asks whether any order item names the output.
CREATE INDEX IF NOT EXISTS order_items_output ON order_items (output_id)
  WHERE output_id IS NOT NULL;
