-- #308 (#609 review): the prints API picks each archive's owner with
-- DISTINCT ON (archive_id) ... ORDER BY archive_id DESC, first_seen, output_id
-- (`bambuddy.print_links`). An index in that order lets a page read the owners
-- newest first instead of sorting every link below its cursor.
CREATE INDEX output_bambuddy_prints_owner
    ON output_bambuddy_prints (archive_id DESC, first_seen, output_id);
