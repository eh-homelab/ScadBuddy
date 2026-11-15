-- #6371: who saved an output. `back_office` (a person, the assistant, an order or a quote)
-- or `storefront` (the storefront slice run, `orders/storefront/slice_price.py`, which
-- prices a visitor's variant). `GET /outputs` and the other back-office lists of renders
-- leave the storefront's out unless asked for them. The output's `meta.json` keeps the
-- same mark (`OutputMeta.source`).
ALTER TABLE output_index ADD COLUMN source text NOT NULL DEFAULT 'back_office'
  CHECK (source IN ('back_office', 'storefront'));

-- The rows of outputs saved before the mark. SQL cannot tell which of them the storefront
-- slice run saved: its id is a uuid5 of the render key, which only the output's files
-- give. So the rows named "Storefront: …" go, and the API's start indexes them again
-- (`OutputIndex.sync`): it marks an output the storefront's only when its id is
-- `storefront_output_id` of its own render key, and keeps every other as the back office's.
DELETE FROM output_index WHERE name LIKE 'Storefront: %';
