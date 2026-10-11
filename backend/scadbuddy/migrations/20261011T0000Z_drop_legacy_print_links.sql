-- #1772: `20261006T2300Z_print_subjects.sql` (#1771) copied `output_bambuddy_prints` and
-- `library_bambuddy_prints` into `print_links` / `print_sends`, and kept the old tables
-- with triggers forwarding a still-draining previous release's writes. Every image
-- deployed since reads and writes only the new tables, so the old ones, their
-- forwarding triggers and functions, and their indexes (`*_owner` among them) go.
-- Irreversible: an image older than #1771 fails on its first print link once this ran.
DROP TRIGGER IF EXISTS output_bambuddy_prints_forward ON output_bambuddy_prints;
DROP TRIGGER IF EXISTS library_bambuddy_prints_forward ON library_bambuddy_prints;
DROP FUNCTION IF EXISTS print_links_forward_output();
DROP FUNCTION IF EXISTS print_links_forward_library();
DROP INDEX IF EXISTS output_bambuddy_prints_owner;
DROP TABLE IF EXISTS output_bambuddy_prints;
DROP TABLE IF EXISTS library_bambuddy_prints;
