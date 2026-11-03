-- #4253 (spec 2026-10-18 §8.4, §6.1): the printer an attempt's run used, as the registry
-- named it then: its serial (the key, #3823), name and Bambuddy printer id. Copied when
-- the run is attached, so a printer renamed, relinked or deleted since leaves the record
-- as it was printed. `printer` (the registry id) stays beside them. Null on an attempt
-- recorded by hand and on a run that named no registry printer.
ALTER TABLE order_item_fulfilments
  ADD COLUMN printer_serial text,
  ADD COLUMN printer_name text,
  ADD COLUMN bambuddy_printer_id integer;
