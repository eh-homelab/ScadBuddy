-- #5282 (storefront phase 3, spec 2026-11-02 §5.4): a visitor's renders. A row is what one
-- visitor asked for: `counted` when the render cost the shop a run, and not when the
-- visitor joined an open render or reused a finished one of the same inputs. The render
-- limits count the rows of a visitor and of a client address; the concurrency limit
-- counts the counted rows whose job is still open. A visitor reads only the jobs its rows
-- name. `colours` is the visitor's choice of listing filament for each colour slot, which
-- the estimate prices.
CREATE TABLE storefront_renders (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  visitor_id text NOT NULL REFERENCES storefront_visitors (id) ON DELETE CASCADE,
  client text NOT NULL,
  listing_id text NOT NULL,
  render_key text NOT NULL,
  job_id text,
  counted boolean NOT NULL,
  colours jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX storefront_renders_visitor ON storefront_renders (visitor_id, created_at);
CREATE INDEX storefront_renders_client ON storefront_renders (client, created_at);
CREATE INDEX storefront_renders_counted ON storefront_renders (counted, created_at);
CREATE INDEX storefront_renders_job ON storefront_renders (job_id);

-- The part volumes of a render job's 3MF, analysed once for every visitor who asks for
-- its estimate (plan point 5).
CREATE TABLE storefront_job_parts (
  job_id text PRIMARY KEY,
  parts jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
