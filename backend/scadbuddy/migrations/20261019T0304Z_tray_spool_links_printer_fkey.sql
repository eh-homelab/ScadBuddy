-- #4190: a tray link names a registry printer. `tray_spool_links` (#4180,
-- 20261018T1744Z_physical_spools.sql) and `printers` (#3823, 20261018T1659Z_printers.sql)
-- are both on main now, so the key that neither could carry alone: deleting a printer
-- deletes its tray links; the primary key's leading `printer_id` serves the cascade. A
-- merge of two printer rows repoints the links first (`printers/store.py`
-- `PRINTER_REFERENCES`, `spools.store.repoint_tray_links`). The DELETE is a safeguard: no
-- writer has run without the key (Task 6c's `api/tray_spools.py` follows this file).
DELETE FROM tray_spool_links l WHERE NOT EXISTS (SELECT 1 FROM printers p WHERE p.id = l.printer_id);
ALTER TABLE tray_spool_links ADD CONSTRAINT tray_spool_links_printer_fkey
    FOREIGN KEY (printer_id) REFERENCES printers (id) ON DELETE CASCADE;
