-- #6153 (spec 2026-11-13 §5.4, §5.6): the price of a storefront variant, from a slice
-- through the back office's quote path. One row per variant. `price_key` is a sha256 of
-- the render's 3MF content, the price-book filament per slot, the shop slice profile and
-- the dialog choices, and the price book and machine rate as they stood: a change to any
-- of them is another key, so an old price is never read again. The row holds no job id,
-- so it outlives the render prune, and no bytes: the sliced file is not kept.
--
-- `running` while a slice runs (`operation_key` names its operation), `done` with the
-- price, or `failed` with the reason. At most `storefront_slices_concurrent` rows are
-- `running` at once (`orders/storefront/slice_price.py` `SlicePrices.claim`).
CREATE TABLE storefront_slice_prices (
  price_key text PRIMARY KEY,
  model_sha256 text NOT NULL,
  status text NOT NULL CHECK (status IN ('running', 'done', 'failed')),
  attempt integer NOT NULL DEFAULT 1,
  -- Each claim's own key, which keys its operation, so a later claim never meets an
  -- earlier claim's operation (#6370 review).
  claim_key text,
  operation_key text,
  currency text,
  unit_price_minor bigint,
  total_minor bigint,
  unit_cost_minor bigint,
  lines jsonb,
  print_seconds integer,
  grams jsonb,
  printer text,
  failure text,
  started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at timestamptz,
  CHECK (status <> 'done' OR (currency IS NOT NULL AND total_minor IS NOT NULL
    AND unit_price_minor IS NOT NULL AND lines IS NOT NULL)),
  CHECK (status <> 'failed' OR failure IS NOT NULL)
);

CREATE INDEX storefront_slice_prices_running ON storefront_slice_prices (started_at)
  WHERE status = 'running';
