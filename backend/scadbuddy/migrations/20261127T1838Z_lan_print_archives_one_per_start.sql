-- #8312: one archive per LAN start, held by the database. `record_lan_start_in` checked
-- it with NOT EXISTS (#8306), so two records of one start at the same moment (the orders
-- sweep and a person opening the print) could both write a row, and an order attempt
-- then summed both prints. A start's earliest record is kept (`first_seen`, then the
-- lower archive id) and any later one is dropped; prod had none on 2026-11-27 (24 rows,
-- 24 starts).
DELETE FROM lan_print_archives a
    USING lan_print_archives b
    WHERE a.run_id = b.run_id
      AND a.subtask_name = b.subtask_name
      AND (b.first_seen, b.archive_id) < (a.first_seen, a.archive_id);

CREATE UNIQUE INDEX lan_print_archives_one_per_start
    ON lan_print_archives (run_id, subtask_name);
