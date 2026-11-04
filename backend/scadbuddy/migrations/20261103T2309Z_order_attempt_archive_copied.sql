-- #5273: whether an attempt's archive facts were copied once (spec 2026-10-18 §8.2), so a
-- later copy never writes the times again over ones a person corrected. Before this file
-- the first copy was told by no seconds or grams copied yet, which an archive giving
-- neither never ended. An attempt that copied something then is marked copied.
ALTER TABLE order_item_fulfilments
    ADD COLUMN archive_copied boolean NOT NULL DEFAULT false;

UPDATE order_item_fulfilments SET archive_copied = true
 WHERE archive_ids <> '{}'
   AND (print_run_id IS NOT NULL OR actual_seconds IS NOT NULL OR actual_grams IS NOT NULL);
